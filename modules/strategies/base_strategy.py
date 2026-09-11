"""
strategies/base_strategy.py
Abstract base class for all trading strategies.

Mirrors base_scout.py from the NSE project.
Every strategy must implement:
  - STRATEGY_ID : str class constant
  - scan()      : evaluate one symbol, return signal dict or None
  - manage()    : update trailing stop / exits for an open position

Signal dict schema (returned by scan()):
  {
    'symbol'      : str,
    'strategy'    : str,     # STRATEGY_ID
    'direction'   : str,     # 'LONG' or 'SHORT'
    'entry_price' : float,
    'sl_price'    : float,   # initial stop loss
    'tp_price'    : float,   # initial take profit (None if trailing only)
    'atr'         : float,   # ATR at entry bar
    'strength'    : float,   # signal quality 0–1 (used for ranking)
    'reason'      : str,     # human-readable entry reason
  }

Exit dict schema (returned by manage()):
  {
    'exit'        : bool,
    'exit_price'  : float,
    'exit_reason' : str,     # 'SL_HIT' | 'TP_HIT' | 'TRAIL_STOP' | 'DAILY_FLOOR'
  }
"""

from abc import ABC, abstractmethod
import time
import logging
import pandas as pd

_log = logging.getLogger("BaseStrategy")


class BaseStrategy(ABC):

    STRATEGY_ID: str = ""

    # Does scan() actually read df_1h? data_hub skips the per-symbol 1h fetch
    # by default (~100 API calls/cycle), passing an empty DataFrame instead.
    # A strategy that needs 1h and does not declare it here will silently
    # return None on every scan — which is exactly what happened to CSM: the
    # fetch was gated on regime (an FF-era optimisation) rather than on who
    # actually needs the data, so CSM could never fire outside OVERHEATED /
    # OVERSOLD. Declare the dependency here and the scanner honours it.
    REQUIRES_1H: bool = False

    # Number of 1m bars scan() needs. Default 200 matches data_hub's fetch.
    # Strategies that resample 1m→5m internally need ≥1000 bars (~16.7 hours).
    REQUIRES_1M_DEPTH: int = 200

    # How far a trade must travel, in ATRs, before the breakeven stop arms.
    # Override per strategy if one needs to be quicker or slower.
    BE_ATR_MULT: float = 1.0

    # Bar size (minutes) on which breakeven/trailing decisions are evaluated.
    TRAIL_BAR_MINUTES: int = 15

    @classmethod
    def trail_reference_price(cls, df, position: dict | None = None) -> float | None:
        """
        Close of the most recently COMPLETED TRAIL_BAR_MINUTES bar.

        Hard SL and TP are checked against the live mark price every cycle —
        that is what the exchange enforces and what a stop actually means.
        Breakeven and trailing, however, were being sampled at the same 5s
        resolution, so a 0.8% intra-bar pop armed breakeven and the retrace
        stopped the trade out at ~0. That produced 84% BE_HIT live versus 29%
        in the backtest, amputating the right tail the edge depends on.

        Evaluating those decisions on completed bars instead makes them immune
        to intra-bar noise while leaving the hard stop at full resolution.

        Returns None when no completed bar can be identified — callers should
        fall back to the current price, which is what the backtest needs since
        it already passes 15m bars here.
        """
        if df is None or getattr(df, "empty", True):
            return None
        if "close" not in df.columns:
            return None

        try:
            d = df

            # Accept BOTH frame layouts. Live (data_feed._to_dataframe) builds a
            # 'timestamp' COLUMN over a RangeIndex; the backtest (load_data)
            # sets a DatetimeIndex and has no such column. Requiring the column
            # made this return None on every backtest call, so breakeven and
            # trailing silently never armed there — the exact live/backtest
            # divergence this method exists to prevent, hiding in the guard.
            if "timestamp" in d.columns:
                _ts = pd.to_datetime(d["timestamp"], utc=True)
            elif isinstance(d.index, pd.DatetimeIndex):
                _idx = d.index
                _ts = pd.Series(
                    _idx.tz_localize("UTC") if _idx.tz is None
                    else _idx.tz_convert("UTC"),
                    index=d.index,
                )
            else:
                return None
            d = d.assign(_ts=_ts)

            # data_hub appends a synthetic mark-price row with volume 0; it is
            # not a real candle and must not be mistaken for a bar close.
            if "volume" in d.columns:
                d = d[d["volume"] > 0]
            if d.empty:
                return None

            # ── Drop the still-forming candle ────────────────────────────────
            # Binance's klines endpoint ALWAYS returns the in-progress candle as
            # the final row. Verified against the live API: at 12:25:17 the last
            # row opened 12:25:00. So for one minute in every fifteen — whenever
            # the clock sits inside :14, :29, :44 or :59 — the row selected
            # below as "the completed 15m bar close" was actually a live tick.
            #
            # That is precisely what this method exists to prevent, and the
            # damage compounds: the caller RATCHETS the stop
            #     if trail_price > position["sl_price"]: sl_price = trail_price
            # so a single leaked tick raises the stop permanently. With
            # FAST_INTERVAL=1 those minutes are sampled ~60 times each, and
            # across a multi-hour hold the leak converges on the trade's high
            # water mark.
            #
            # Measured consequence: live closed 56% of trades at breakeven and
            # reached target 11% of the time, against 18% / 23% in the backtest
            # — CSM's entire edge is the trades that reach target, so this was
            # amputating the thing that pays for everything else.
            #
            # A row is complete iff a LATER row exists (a candle can only be
            # superseded once it has closed). Testing the data rather than the
            # wall clock keeps this correct in live, backtest and replay alike,
            # with no clock dependency.
            d = d.iloc[:-1]
            if d.empty:
                return None

            # Only bars that closed AFTER the position opened may inform its
            # breakeven/trailing. Observed live: a symbol re-entered moments
            # after exiting, the preceding bar had closed high, breakeven armed
            # instantly on data predating the trade and the stop fired — a
            # 0.0-minute BE_HIT. A stale bar must never move a new trade's stop.
            gated = False
            if position is not None:
                entry_t = position.get("entry_time")
                if entry_t:
                    try:
                        import pandas as _pd
                        cutoff = _pd.to_datetime(entry_t, utc=True)
                        d = d[d["_ts"] > cutoff]
                        gated = True
                    except Exception:
                        gated = False

            n = max(1, int(cls.TRAIL_BAR_MINUTES))
            closes = d[d["_ts"].dt.minute % n == (n - 1)] if not d.empty \
                     else d
            # A bar spanning 10:00-10:14 closes on the 1m candle opening 10:14.

            if closes.empty:
                if gated:
                    # Live 1m data, but no bar has closed since entry yet.
                    # Returning None would make the caller fall back to the tick
                    # price and arm breakeven on intra-bar noise during the
                    # trade's first bar — exactly what this method prevents.
                    # Entry price can never exceed the breakeven trigger, so
                    # nothing arms until a real bar closes.
                    entry_px = float(position.get("entry_price") or 0.0)
                    return entry_px or None
                # No timestamp granularity to work with (the backtest passes
                # 15m bars): let the caller fall back to current price.
                return None
            return float(closes["close"].iloc[-1])
        except Exception:
            return None

    @classmethod
    def breakeven_trigger(cls, position: dict, fallback_pct: float) -> float:
        """
        Price at which the breakeven stop arms, scaled to the symbol's ATR.

        The fixed percentage triggers (0.6–0.8%) were calibrated on nothing in
        particular and are far inside the noise band of a volatile alt. Live,
        checking a 5s mark price, 84% of CSM trades armed breakeven on a random
        pop and were then stopped out at ~0 on the retrace — versus 29% in the
        backtest, which only sees 15m closes and cannot observe the round trip.
        That amputates the right tail CSM's entire edge depends on (its average
        winner is +3.97%).

        Scaling by ATR means the trigger widens exactly where the noise is
        wider. `fallback_pct` is the strategy's original fixed trigger, used
        when ATR is missing or implausible.

        Returns the trigger PRICE, respecting position direction.
        """
        try:
            entry = float(position["entry_price"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        if entry <= 0:
            return 0.0

        atr  = float(position.get("atr", 0.0) or 0.0)
        move = atr * cls.BE_ATR_MULT

        # Never arm tighter than the original fixed trigger — that is the floor
        # this replaces, not a target. Cap at 5% so a bad ATR cannot disable
        # breakeven protection entirely.
        floor_move = entry * fallback_pct
        move = min(max(move, floor_move), entry * 0.05)

        if position.get("direction") == "SHORT":
            return entry - move
        return entry + move

    @staticmethod
    def stop_exit_reason(position: dict) -> str:
        """
        Classify which stop actually fired.

        Every strategy arms a breakeven stop at +0.6% and then trails it, but
        all three exits were reported as 'SL_HIT'. That made a trailed winner
        (+2.85% on one EPICUSDT trade) indistinguishable in the logs from a
        genuine stop-out, so no post-hoc analysis could separate them.

        Returns:
            'SL_HIT'    — original stop; the trade never reached breakeven.
            'BE_HIT'    — breakeven stop; price cleared +0.6% then came back.
            'TRAIL_HIT' — trailing stop; locked in more than breakeven.
        """
        # Production strategies set 'be_hit'; trend_pullback uses 'be_active'.
        armed = position.get("be_hit", position.get("be_active", False))
        if not armed:
            return "SL_HIT"

        try:
            entry = float(position["entry_price"])
            sl    = float(position["sl_price"])
        except (KeyError, TypeError, ValueError):
            return "TRAIL_HIT"
        if entry <= 0:
            return "TRAIL_HIT"

        # Breakeven parks the stop at entry ±0.1%; beyond that the trail moved.
        if position.get("direction") == "SHORT":
            return "BE_HIT" if sl >= entry * 0.9985 else "TRAIL_HIT"
        return "BE_HIT" if sl <= entry * 1.0015 else "TRAIL_HIT"

    @abstractmethod
    def scan(
        self,
        symbol:  str,
        df_1m:   pd.DataFrame,
        df_15m:  pd.DataFrame,
        df_1h:   pd.DataFrame,
        regime:  dict,
    ) -> dict | None:
        """
        Evaluate one symbol for entry.

        Args:
            symbol  : Trading pair e.g. 'ETHUSDT'
            df_1m   : 1-minute OHLCV DataFrame
            df_15m  : 15-minute OHLCV DataFrame
            df_1h   : 1-hour OHLCV DataFrame
            regime  : Output of regime_engine.classify_regime()

        Returns:
            Signal dict if an entry is valid, None otherwise.
        """
        ...

    @abstractmethod
    def manage(
        self,
        position: dict,
        df_1m:    pd.DataFrame,
        session_pnl: float,
    ) -> dict:
        """
        Evaluate an open position's exit conditions.

        Args:
            position    : Open position dict (stored in position_tracker)
            df_1m       : Latest 1m candle data for the position's symbol
            session_pnl : Aggregate realised PnL for the current session

        Returns:
            Exit dict with keys: exit (bool), exit_price, exit_reason
            If exit is False, may also include updated sl_price.
        """
        ...

    def __repr__(self) -> str:
        return f"<Strategy: {self.STRATEGY_ID}>"


# ─── Shared utilities used by multiple strategies ────────────────────────────

def bar_time(df: pd.DataFrame):
    """
    Return the timestamp of the most recent bar as a tz-aware pd.Timestamp (UTC),
    or None if unavailable.

    Why this exists
    ---------------
    The live path and the backtest path hand strategies DIFFERENTLY SHAPED
    frames, and time-based strategies broke on the difference:

      live      : data_feed._to_dataframe() builds a 'timestamp' COLUMN and
                  calls reset_index(drop=True) -> the index is a RangeIndex of
                  ints. `df.index[-1].weekday()` raises
                  "'int' object has no attribute 'weekday'".
      backtest  : CSVs are loaded with open_time as the INDEX -> the index is a
                  DatetimeIndex and .weekday() works.

    The result was that WKD threw on every symbol on every live scan and had
    never opened a single position in production, while backtesting fine.

    This helper reads whichever layout it is given, so strategies work in both.
    """
    if df is None or len(df) == 0:
        return None

    # OPEN time only. Never close_time.
    #
    # Backtest CSVs carry BOTH: open_time becomes the index and close_time
    # stays a column. An earlier version of this function listed close_time as
    # a fallback column, so on backtest frames it returned 20:14:59.999 for the
    # bar that opened at 20:00 — and WKD, which tests `minute == 0`, silently
    # stopped firing entirely (0 trades where it previously had 72).
    for col in ("timestamp", "open_time"):
        if col in df.columns:
            try:
                ts = pd.to_datetime(df[col].iloc[-1], utc=True)
                if not pd.isna(ts):
                    return ts
            except Exception:
                pass

    # Fallback: a DatetimeIndex (backtest path — index is open_time).
    try:
        ts = df.index[-1]
        if isinstance(ts, pd.Timestamp):
            return ts if ts.tzinfo else ts.tz_localize("UTC")
        ts = pd.to_datetime(ts, utc=True)
        if not pd.isna(ts):
            return ts
    except Exception:
        pass

    return None


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Compute ATR (Average True Range) from an OHLCV DataFrame.

    Uses simple TR = max(H-L, |H-Cprev|, |L-Cprev|).

    Returns 0.0 if not enough bars.
    """
    if df is None or len(df) < period + 1:
        return 0.0
    hi  = df["high"]
    lo  = df["low"]
    cp  = df["close"].shift(1)
    tr  = pd.concat([hi - lo, (hi - cp).abs(), (lo - cp).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Compute ADX (Average Directional Index) — measures trend STRENGTH,
    not direction. Range 0–100.

      ADX > 25 : trending market (TP and VB have edge)
      ADX < 20 : choppy/ranging (high chance of stop-out)
      ADX 20–25: transition zone — use with caution

    Formula:
      +DM  = max(high - prev_high, 0) if it exceeds max(prev_low - low, 0)
      -DM  = max(prev_low - low, 0) if it exceeds max(high - prev_high, 0)
      TR   = max(H-L, |H-Cprev|, |L-Cprev|)
      +DI  = 100 × EMA(+DM, period) / EMA(TR, period)
      -DI  = 100 × EMA(-DM, period) / EMA(TR, period)
      DX   = 100 × |+DI − -DI| / (+DI + -DI)
      ADX  = EMA(DX, period)

    Returns 0.0 if not enough bars or calculation fails.
    """
    if df is None or len(df) < period * 2 + 1:
        return 0.0

    try:
        hi = df["high"]
        lo = df["low"]
        cl = df["close"]

        # True Range
        prev_cl = cl.shift(1)
        tr = pd.concat([
            hi - lo,
            (hi - prev_cl).abs(),
            (lo - prev_cl).abs(),
        ], axis=1).max(axis=1)

        # Directional Movement
        up_move   = hi - hi.shift(1)
        down_move = lo.shift(1) - lo

        plus_dm  = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)

        plus_dm[ (up_move > down_move) & (up_move > 0)]   = up_move[  (up_move > down_move) & (up_move > 0)]
        minus_dm[(down_move > up_move) & (down_move > 0)]  = down_move[(down_move > up_move) & (down_move > 0)]

        # Smoothed with Wilder EMA (alpha = 1/period)
        alpha   = 1.0 / period
        atr_s   = tr.ewm(alpha=alpha,       adjust=False).mean()
        plus_s  = plus_dm.ewm(alpha=alpha,  adjust=False).mean()
        minus_s = minus_dm.ewm(alpha=alpha, adjust=False).mean()

        plus_di  = 100 * plus_s  / atr_s.replace(0, float("nan"))
        minus_di = 100 * minus_s / atr_s.replace(0, float("nan"))

        dx_denom = (plus_di + minus_di).replace(0, float("nan"))
        dx       = 100 * (plus_di - minus_di).abs() / dx_denom
        adx      = dx.ewm(alpha=alpha, adjust=False).mean()

        val = float(adx.iloc[-1])
        return val if not pd.isna(val) else 0.0

    except Exception as exc:
        _log.debug(f"compute_adx failed: {exc}")
        return 0.0


# ─── Open Interest trend cache ────────────────────────────────────────────────
# Tracks OI per symbol across consecutive scan() calls.
# Used to determine whether OI is rising (new positions entering)
# or falling (positions closing) — which tells us if a price move
# is driven by real conviction or just existing position unwinding.
#
# Cache entry: {symbol: (timestamp_float, oi_value_float)}
# TTL: 2 minutes — balances freshness vs API call count.
# At 160 symbols, only symbols that reach the final OI check are queried
# (~5–10 per cycle), not all 160.

_OI_CACHE_TTL = 120.0   # seconds
_oi_cache: dict[str, tuple[float, float]] = {}


def get_oi_trend(symbol: str, direction: str) -> str:
    """
    Determine whether Open Interest is rising or falling for a symbol,
    and whether that confirms or contradicts the intended trade direction.

    OI interpretation for volatile crypto:
      Price rising + OI rising  → real longs entering → CONFIRMS LONG
      Price rising + OI falling → shorts covering     → WEAK (no new buyers)
      Price falling + OI rising → real shorts entering → CONFIRMS SHORT
      Price falling + OI falling→ longs liquidating   → WEAK (may bounce)

    Since we only check this after price/EMA/RSI/MACD already confirm
    direction, we simplify:
      OI rising  → "RISING"   — new money entering, confirms the move
      OI flat    → "FLAT"     — inconclusive, allow trade (don't over-filter)
      OI falling → "FALLING"  — positions closing, move may lack follow-through
      Unknown    → "UNKNOWN"  — API failed, allow trade (fail-open)

    Args:
        symbol    : e.g. 'BTCUSDT'
        direction : 'LONG' or 'SHORT'

    Returns:
        'RISING' | 'FLAT' | 'FALLING' | 'UNKNOWN'
    """
    # Lazy import to avoid circular dependency — data_feed is a module sibling
    try:
        from modules.data_feed import fetch_open_interest
    except ImportError:
        try:
            from data_feed import fetch_open_interest
        except ImportError:
            return "UNKNOWN"

    now = time.monotonic()
    cached = _oi_cache.get(symbol)

    # Fetch fresh OI
    oi_now = fetch_open_interest(symbol)
    if oi_now is None:
        return "UNKNOWN"

    if cached is None:
        # First time seeing this symbol — store and allow trade
        _oi_cache[symbol] = (now, oi_now)
        return "UNKNOWN"

    cached_ts, oi_prev = cached
    _oi_cache[symbol] = (now, oi_now)   # always update cache

    # If cached value is stale (> TTL), treat as fresh baseline
    if now - cached_ts > _OI_CACHE_TTL * 3:
        return "UNKNOWN"

    # OI change threshold: 0.3% change to distinguish signal from noise
    if oi_prev <= 0:
        return "UNKNOWN"

    change_pct = (oi_now - oi_prev) / oi_prev * 100

    if change_pct > 0.3:
        return "RISING"
    elif change_pct < -0.3:
        return "FALLING"
    return "FLAT"


def no_exit() -> dict:
    """Convenience: return a 'no exit yet' result from manage()."""
    return {"exit": False, "exit_price": 0.0, "exit_reason": ""}


def make_exit(price: float, reason: str) -> dict:
    """Convenience: return an exit signal from manage()."""
    return {"exit": True, "exit_price": price, "exit_reason": reason}





