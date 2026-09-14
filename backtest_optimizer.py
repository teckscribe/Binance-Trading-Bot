"""
backtest_optimizer.py
Portfolio backtester for the CSB production strategies.

=============================================================================
REWRITE NOTES — what was wrong with the previous version and what changed
=============================================================================

1. NO LIQUIDATION FLOOR  [produced impossible results]
   Old:  capital += capital * (pnl_pct * leverage - 0.0008)
   With no bound, once (pnl_pct * leverage) < -1 the capital term flips sign
   and every later trade compounds a NEGATIVE balance. This is why the old
   harness reported "Final Capital: $-1,157,620.56" on a $20 account — for
   CSM, the one strategy that actually has an edge.
   New: equity is floored at 0 and the run halts (account blown).

2. NOT CHRONOLOGICAL  [not actually a portfolio]
   Old: concatenated every BTC trade, then every ETH trade, ... and compounded
   in that order. That is 20 sequential single-coin runs sharing one balance.
   New: all trades from all symbols merged and sorted by entry_time.

3. FEE CHARGED ON THE WRONG BASE  [understated costs 5x]
   Old: net_return = leveraged_return - 0.0008
   Taker fee is charged on NOTIONAL. At 5x the round-trip drag on equity is
   5 * 0.08% = 0.40%, not 0.08%.
   New: fees applied to notional.

4. RISK MODEL IGNORED  [simulated a system you do not run]
   Old: 100% of equity into every trade, no concurrency limit, no loss caps.
   New: enforces MAX_CONCURRENT, MAX_MARGIN_PCT, DAILY/WEEKLY loss caps from
   modules.risk_engine, and isolated-margin loss bounding per position.

5. NO CORPORATE-ACTION HANDLING  [scored a stock split as a -95% trade]
   KORUUSDT redenominated ~20:1 on 2026-07-15 (481.11 -> 23.71 in one 15m
   bar). The old harness treated it as a real price move.
   New: symbols with a single-bar move > SPLIT_THRESHOLD are quarantined.

6. HARDCODED MOCK REGIME  [regime logic never exercised]
   Old: one fixed regime string for the entire 30-day run.
   New: regime is classified per 1h bar by replaying the real decision
   primitives from modules.regime_engine against cached BTC/ETH/SOL data,
   with real historical funding rates fetched from Binance.

   Note on hysteresis: regime_engine.classify_regime() measures hold time
   against datetime.now(), which is meaningless when replaying history. This
   module reuses the engine's pure primitives (_coin_trend, _compute_adx,
   _sma_slope_pct, _decide_regime) and re-implements the hysteresis check
   against BAR time. The decision logic is the engine's; only the clock is
   corrected for backtesting.

=============================================================================
KNOWN LIMITS — read before drawing conclusions
=============================================================================
  * 15m bars are used as the intrabar price proxy for manage(). A true
    tick/1m backtest would fill stops differently; expect optimistic fills
    on volatile bars.
  * REGIME_STRATEGY_PERMISSIONS in regime_engine.py is currently all-True
    for every regime, so regime does not gate strategy selection. Of the 7
    production strategies only TP reads regime at all.
  * Single historical window. Results are in-sample. Do not tune on them.

Usage:
    python backtest_optimizer.py                       # full report
    python backtest_optimizer.py --strategies CSM,LIQ  # subset
    python backtest_optimizer.py --crypto-only         # drop equity tokens
    python backtest_optimizer.py --no-regime           # legacy mock regime
    python backtest_optimizer.py --slots 5 --margin 0.15
"""

import os
import sys
import json
import time
import argparse
import logging
from collections import defaultdict, Counter

import numpy as np
import pandas as pd
import requests

from dotenv import load_dotenv
load_dotenv()

from modules.strategies.cross_sectional_momentum  import CrossSectionalMomentum
from modules.strategies.freqtrade_port_nasos       import NASOSv4Port

from modules.risk_engine import (
    MAX_MARGIN_PCT, max_concurrent, daily_loss_cap, weekly_loss_cap,
)

# Snapshot the runtime-editable limits once for this offline run.
MAX_CONCURRENT  = max_concurrent()
DAILY_LOSS_CAP  = daily_loss_cap()
WEEKLY_LOSS_CAP = weekly_loss_cap()

logging.getLogger("RegimeEngine").setLevel(logging.WARNING)

# ── Config ───────────────────────────────────────────────────────────────────
DATA_DIR        = "data"
INITIAL_CAPITAL = 20.0
LEVERAGE        = 5
TAKER_FEE       = 0.0004
ROUND_TRIP_FEE  = TAKER_FEE * 2        # 0.08% of notional
WARMUP_BARS     = 200                  # sized for FF_V2's 200-EMA originally; harmless
SPLIT_THRESHOLD = 0.35                 # single-bar move flagged as corporate action

# ── Slippage ─────────────────────────────────────────────────────────────────
# Per SIDE, applied to entry and exit. Neither this harness nor the paper
# engine modelled it before; measured live it was -1.82 USDT over 63 stop
# exits, ~14% of gross P&L. Read from the same .env key the replay harness
# uses so backtest and paper cannot disagree about it.
SLIPPAGE_PCT    = max(0.0, float(os.getenv("SLIPPAGE_PCT", "0")))

# Mirrors live_scanner.MIN_STRENGTH so the harness can discard the same signals
# the live scanner does. Default 0 keeps historical runs comparable.
BT_MIN_STRENGTH = max(0.0, float(os.getenv("BT_MIN_STRENGTH", "0")))

# Regime gate: block specific strategies in specific regimes.
# Format: "RANGING:NASOS_V4;OVERHEATED:CSM" — semicolon-separated
# regime:strategy pairs. Empty string (default) = no gating.
BT_REGIME_GATE = os.getenv("BT_REGIME_GATE", "")
_regime_block = {}
for _chunk in BT_REGIME_GATE.split(";"):
    _chunk = _chunk.strip()
    if ":" not in _chunk:
        continue
    _reg, _strats = _chunk.split(":", 1)
    for _s in _strats.split(","):
        _s = _s.strip()
        if _s:
            _regime_block.setdefault(_reg.strip(), set()).add(_s)

def _bt_regime_ok(strategy_id, regime):
    return strategy_id not in _regime_block.get(regime, set())

# ── 1h look-ahead switch ─────────────────────────────────────────────────────
# FALSE (default) builds the hourly frame the way the LIVE scanner sees it.
# TRUE restores the pre-2026-08-12 behaviour, which sliced df_1h on the 15m
# bar's OPEN time and therefore handed the strategy an hourly bar that had not
# closed yet — up to 45 minutes of future price, on 3 of every 4 bars.
#
# That defect produced every headline in the project's documentation. Measured
# on 100 symbols over 90 days, CSM went from
#     +0.632%/trade  PF 1.74  ->  +6254% account      (look-ahead ON)
#     -0.046%/trade  PF 0.96  ->    -57% account      (look-ahead OFF)
# Keep it False. The flag exists only so historical numbers can be reproduced
# and the delta re-measured; it is not a tuning knob.
LOOKAHEAD_1H    = os.getenv("BT_LOOKAHEAD_1H", "false").lower() == "true"

# OIB and WKD deleted 2026-08-11 — their source files are gone, so they can no
# longer be backtested even with --strategies. See strategy_factory.py.
STRATEGY_CLASSES = {
    "CSM":   CrossSectionalMomentum,
    "NASOS_V4":   NASOSv4Port,
}

# Default to whatever the factory actually runs, so a backtest reflects
# production unless you deliberately ask for more via --strategies.
def _production_strategy_ids() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
        if ids:
            return ids
    except Exception:
        pass
    return ["CSM", "NASOS_V4"]


PRODUCTION_IDS = _production_strategy_ids()

STRATEGY_ORDER = PRODUCTION_IDS + [
    s for s in ["CSM"]
    if s not in PRODUCTION_IDS
]
TIER = {"CSM": "Elite",
        "NASOS_V4": "FT"}

MOCK_REGIME = {"CSM": "BULL_TREND",
               "NASOS_V4": "BULL_TREND"}

# Tokenised equities and leveraged ETFs on Binance USDM Futures. These only
# move during US cash-session hours and have no perpetual funding dynamics,
# so crypto-native logic is structurally invalid on them.
EQUITY_BASES = {"SNDK", "SKHYNIX", "SKHY", "MU", "KORU", "SPCX",
                "SNXX", "SOXS", "EWY", "SAMSUNG", "NVDA", "TSLA", "AAPL", "COIN"}


def is_equity(symbol: str) -> bool:
    return symbol[:-4] in EQUITY_BASES


# ═════════════════════════════════════════════════════════════════════════════
# DATA LAYER
# ═════════════════════════════════════════════════════════════════════════════

def load_data(symbol: str, interval: str, days: int = 30):
    """Load cached OHLCV. Returns a UTC-indexed DataFrame or None."""
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}_{days}d.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, parse_dates=["open_time"]).set_index("open_time")
    except Exception as exc:
        print(f"  ! {path}: {exc}")
        return None
    return df[~df.index.duplicated(keep="first")].sort_index()


def check_integrity(symbols, days=30):
    """
    Quarantine symbols with an unadjusted corporate action.

    A >35% move in a single 15m bar is a redenomination, not a tradeable
    price move. Scoring one as a trade produces a fabricated -95% result.
    Returns {symbol: (pct_move, before, after)}.
    """
    bad = {}
    for sym in symbols:
        df = load_data(sym, "15m", days)
        if df is None or df.empty:
            continue
        r = df["close"].pct_change()
        if (r.abs() > SPLIT_THRESHOLD).any():
            i = r.abs().idxmax()
            loc = df.index.get_loc(i)
            bad[sym] = (float(r.loc[i]), float(df["close"].iloc[loc - 1]),
                        float(df["close"].loc[i]))
    return bad


def load_funding(symbol: str, start_ms: int, end_ms: int, use_cache=True):
    """
    Real historical funding rates from Binance, cached to disk.
    Returns a UTC-indexed Series of funding rates, or an empty Series.
    """
    os.makedirs(os.path.join(DATA_DIR, "funding"), exist_ok=True)
    cache = os.path.join(DATA_DIR, "funding", f"{symbol}.csv")

    if use_cache and os.path.exists(cache):
        try:
            df = pd.read_csv(cache, parse_dates=["fundingTime"])
            s = df.set_index("fundingTime")["fundingRate"].astype(float)
            # parse_dates yields a tz-NAIVE index. The fresh-fetch path below
            # builds a tz-aware one, and build_regime_series compares this index
            # against tz-aware bar timestamps — so the cached path raised
            # "Cannot compare tz-naive and tz-aware datetime-like objects" and
            # took down regime classification entirely, while a cold cache
            # worked fine. Normalise to UTC so both paths agree, as the
            # docstring above already claims they do.
            if s.index.tz is None:
                s.index = s.index.tz_localize("UTC")
            else:
                s.index = s.index.tz_convert("UTC")
            return s
        except Exception:
            pass

    rows, cur = [], start_ms
    while cur < end_ms:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": symbol, "startTime": cur,
                        "endTime": end_ms, "limit": 1000},
                timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as exc:
            print(f"  ! funding fetch failed for {symbol}: {exc}")
            break
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1]["fundingTime"] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.25)

    if not rows:
        return pd.Series(dtype=float)

    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["fundingRate"] = df["fundingRate"].astype(float)
    df = df[["fundingTime", "fundingRate"]].drop_duplicates("fundingTime")
    df.to_csv(cache, index=False)
    return df.set_index("fundingTime")["fundingRate"]


# ═════════════════════════════════════════════════════════════════════════════
# REGIME — replayed per 1h bar using the live engine's own primitives
# ═════════════════════════════════════════════════════════════════════════════

def build_regime_series(days=30, verbose=True):
    """
    Classify the market regime at every 1h bar over the test window.

    Reuses regime_engine's real decision primitives. Hysteresis is
    re-implemented against BAR time because the live version measures against
    datetime.now(), which is meaningless in a historical replay.

    Returns {pd.Timestamp -> regime dict}, or None if data is unavailable.
    """
    from modules.regime_engine import (
        _coin_trend, _compute_adx, _sma_slope_pct, _decide_regime,
        HYSTERESIS_PCT, SMA_PERIOD, MIN_BARS_NEEDED,
    )

    btc = load_data("BTCUSDT", "1h", days)
    eth = load_data("ETHUSDT", "1h", days)
    sol = load_data("SOLUSDT", "1h", days)
    if btc is None or eth is None or sol is None:
        if verbose:
            print("  ! BTC/ETH/SOL 1h data missing — cannot classify regime")
        return None

    start_ms = int(btc.index[0].timestamp() * 1000)
    end_ms   = int(btc.index[-1].timestamp() * 1000)
    funding  = load_funding("BTCUSDT", start_ms, end_ms)
    if funding.empty and verbose:
        print("  ! no funding history — OVERHEATED/OVERSOLD cannot trigger")

    series, current, set_at = {}, "", None
    for i in range(MIN_BARS_NEEDED, len(btc)):
        ts   = btc.index[i]
        b    = btc.iloc[:i + 1]
        e    = eth[eth.index <= ts]
        s    = sol[sol.index <= ts]
        if len(e) < MIN_BARS_NEEDED or len(s) < MIN_BARS_NEEDED:
            continue

        f = 0.0
        if not funding.empty:
            prior = funding[funding.index <= ts]
            if len(prior):
                f = float(prior.iloc[-1])

        bt, et, st = _coin_trend(b, "BTC"), _coin_trend(e, "ETH"), _coin_trend(s, "SOL")
        raw = _decide_regime(bt, et, st, f, _compute_adx(b), _sma_slope_pct(b))

        # Hysteresis against bar time (live engine uses wall clock).
        price = float(b["close"].iloc[-1])
        sma20 = float(b["close"].rolling(SMA_PERIOD).mean().iloc[-1])
        if current and current != raw:
            hyst_ok = True
            if raw not in ("OVERHEATED", "OVERSOLD") and \
               current not in ("OVERHEATED", "OVERSOLD"):
                dist = (price - sma20) / sma20 if sma20 > 0 else 0
                if raw == "BULL_TREND" and dist < HYSTERESIS_PCT:
                    hyst_ok = False
                elif raw == "BEAR_TREND" and dist > -HYSTERESIS_PCT:
                    hyst_ok = False
                elif raw == "RANGING" and abs(dist) > HYSTERESIS_PCT:
                    hyst_ok = False
            # 1h bars always satisfy MIN_HOLD_MINUTES=15, so hold_ok is True.
            if not hyst_ok:
                raw = current

        if raw != current:
            current, set_at = raw, ts

        # Key by when this classification becomes KNOWABLE, not by the bar it
        # describes. `bt`/`adx`/`price` above all come from btc.iloc[:i+1] —
        # the bar OPENING at ts, which does not close until ts + 1h. Keying it
        # at ts let regime_at() return, for any moment inside that hour, a
        # label derived from the hour's closing price. Same defect as the 1h
        # frame look-ahead, one level up.
        #
        # Currently inert: CSM is permitted in BULL_TREND / BEAR_TREND /
        # RANGING, i.e. every regime the classifier produces in practice, so
        # the label cannot change which trades fire. It stops being inert the
        # moment any REGIME_STRATEGY_PERMISSIONS cell gates something.
        series[ts + pd.Timedelta(hours=1)] = {
            "regime": current, "funding": f,
            "btc_trend": bt, "eth_trend": et, "sol_trend": st,
            "btc_price": price, "sma20": sma20,
        }

    if verbose and series:
        dist = Counter(v["regime"] for v in series.values())
        tot  = len(series)
        print(f"  Regime distribution over {tot} hourly bars:")
        for k, v in dist.most_common():
            print(f"    {k:<12}{v:>5} bars  ({v/tot*100:>5.1f}%)")

    # Sorted key index so regime_at() can bisect instead of scanning. Without
    # this the lookup is O(bars) and runs once per 15m bar per symbol per
    # strategy — the dominant cost of a full run.
    return {"_keys": sorted(series), "_map": series} if series else None


_RANGING = {"regime": "RANGING", "funding": 0.0}


def regime_at(series, ts, fallback="RANGING"):
    """Most recent classified regime at or before ts. O(log n)."""
    # A bare regime name (legacy callers) means "use this regime throughout".
    if isinstance(series, str):
        return {"regime": series, "funding": 0.0}
    if not series:
        return _RANGING if fallback == "RANGING" else {"regime": fallback, "funding": 0.0}
    import bisect
    keys = series["_keys"]
    # The keys are tz-aware UTC (built from 1h CSVs loaded with utc=True), but
    # callers hand us whatever their own frame carries. run_backtest_1m passes
    # cur_1m.index[-1], which is tz-NAIVE, and bisect then raised
    # "Cannot compare tz-naive and tz-aware timestamps" — so the real-regime
    # path was dead for the 1m harness and every Tire 2 result to date came
    # from mock_regime_name instead. Normalise here rather than at each call
    # site, so any future caller is safe by default.
    if keys and getattr(ts, "tzinfo", None) is None and getattr(keys[0], "tzinfo", None) is not None:
        try:
            ts = ts.tz_localize("UTC")
        except Exception:
            import pandas as _pd
            ts = _pd.to_datetime(ts, utc=True)
    i = bisect.bisect_right(keys, ts) - 1
    if i < 0:
        return _RANGING if fallback == "RANGING" else {"regime": fallback, "funding": 0.0}
    return series["_map"][keys[i]]


# ═════════════════════════════════════════════════════════════════════════════
# PER-SYMBOL BACKTEST
# ═════════════════════════════════════════════════════════════════════════════

def _intrabar_exit(pos, bar):
    """
    Did price touch the stop or target INSIDE this bar?

    The backtest used to sample only the bar CLOSE via strategy.manage(), so a
    position whose stop was breached mid-bar but recovered by the close was
    scored as still open. Live samples the mark price every 5 seconds and the
    exchange holds a real STOP_MARKET order, so it exits the moment price
    trades through. That single difference produced the largest measured
    live/backtest divergence in the system: SL_HIT was 43.5% of live exits but
    only 24.2% of backtested ones — the backtest was letting losers recover
    that live had already closed.

    Returns (exit_price, "STOP"|"TARGET") or None. The caller resolves a STOP
    into SL_HIT / BE_HIT / TRAIL_HIT via the strategy's own stop_exit_reason(),
    because manage() moves sl_price in place as breakeven and trailing arm —
    labelling every stop touch "SL_HIT" would report trailed-out WINNERS as
    stop-losses (it produced a 62% SL_HIT rate alongside a 66% win rate).

    Tie-break: when a bar touches BOTH the stop and the target, the stop is
    assumed to have been hit first. 15m OHLC does not record the order of
    events within the bar, so this takes the pessimistic reading rather than
    inventing a favourable one. Resolving it properly needs 1m data.

    Fills are AT the stop/target price. Real stop-market fills slip past the
    trigger, so this is still slightly optimistic — see SLIPPAGE_PCT.
    """
    sl = pos.get("sl_price")
    tp = pos.get("tp_price")
    try:
        hi, lo = float(bar["high"]), float(bar["low"])
    except Exception:
        return None

    if pos.get("direction") == "LONG":
        if sl is not None and lo <= float(sl):
            return float(sl), "STOP"
        if tp is not None and hi >= float(tp):
            return float(tp), "TARGET"
    else:
        if sl is not None and hi >= float(sl):
            return float(sl), "STOP"
        if tp is not None and lo <= float(tp):
            return float(tp), "TARGET"
    return None


def _intrabar_exit_scan(pos, seg):
    """
    First stop/target touch anywhere in a MULTI-BAR segment.

    run_backtest_1m steps STEP minutes at a time but used to test the stop
    against `cur_1m.iloc[-1]` alone — one minute in sixty. A stop breached at
    :07 and recovered by :59 was never recorded, so the harness let losers run
    that live (holding a real STOP_MARKET order) had already closed. Measured
    on an identical signal set, that single-bar check inflated expectancy by
    ~0.16%/trade and understated the SL_HIT rate by ~13 points.

    The distortion scales with how reachable the stop is, so it flatters TIGHT
    stops far more than wide ones — which means it also distorts the RANKING
    between strategies, not just the levels.

    Returns (exit_price, "STOP"|"TARGET", bar_timestamp) or None. Returning the
    touching bar's timestamp (rather than the step boundary) also makes trade
    durations honest, which the duration-bucket analysis depends on.

    A vectorised min/max precheck skips the per-bar loop on the overwhelming
    majority of segments, where no level is reachable at all.
    """
    if seg is None or len(seg) == 0:
        return None

    sl = pos.get("sl_price")
    tp = pos.get("tp_price")
    if sl is None and tp is None:
        return None

    try:
        seg_hi = float(seg["high"].max())
        seg_lo = float(seg["low"].min())
    except Exception:
        return None

    if pos.get("direction") == "LONG":
        reachable = (sl is not None and seg_lo <= float(sl)) or \
                    (tp is not None and seg_hi >= float(tp))
    else:
        reachable = (sl is not None and seg_hi >= float(sl)) or \
                    (tp is not None and seg_lo <= float(tp))
    if not reachable:
        return None

    for ts, bar in seg.iterrows():
        hit = _intrabar_exit(pos, bar)
        if hit:
            return hit[0], hit[1], ts
    return None


_ONE_HOUR   = pd.Timedelta(hours=1)
_QUARTER    = pd.Timedelta(minutes=15)
_OHLCV      = ["open", "high", "low", "close", "volume"]


def _build_partial_hours(df_15m):
    """
    Pre-compute, for every 15m bar, the OHLCV of the hour SO FAR.

    The live scanner fetches 1h klines mid-hour, so the last row it sees is the
    still-forming bar: open = the hour's open, high/low = the extremes so far,
    close = the current price. Reproducing that per bar with a groupby inside
    the loop would be O(n^2); these are the same values computed once.
    """
    g = df_15m.index.floor("1h")
    return {
        "open":   df_15m["open"].groupby(g).transform("first").to_numpy(),
        "high":   df_15m["high"].groupby(g).cummax().to_numpy(),
        "low":    df_15m["low"].groupby(g).cummin().to_numpy(),
        "close":  df_15m["close"].to_numpy(),
        "volume": df_15m["volume"].groupby(g).cumsum().to_numpy(),
    }


def _hourly_as_of(df_1h, h_ts, h_np, partial, i, bar_close):
    """
    The 1h frame as the LIVE scanner would have received it at `bar_close`.

    = every hourly bar that has actually CLOSED by then, plus the current
    still-forming bar built from the 15m bars elapsed so far.

    The old code did `df_1h[df_1h.index <= now]` with `now` = the 15m bar's
    OPEN time, which includes the hourly bar covering that instant — a bar
    whose close is up to 45 minutes in the future of the entry price the same
    iteration then books. See LOOKAHEAD_1H.
    """
    # Bars closed by bar_close: open_time + 1h <= bar_close
    j = np.searchsorted(h_ts, (bar_close - _ONE_HOUR).to_datetime64(), "right")
    hour_start = bar_close.floor("1h")

    if hour_start >= bar_close:
        # bar_close lands exactly on an hour boundary (the :45 bar), so the
        # hour has just closed and is already in the slice — no partial row.
        return df_1h.iloc[max(0, j - _H_WINDOW): j]

    head = h_np[max(0, j - _H_WINDOW + 1): j]
    row  = [partial["open"][i], partial["high"][i], partial["low"][i],
            partial["close"][i], partial["volume"][i]]
    return pd.DataFrame(np.vstack([head, row]) if len(head) else [row],
                        columns=_OHLCV)


# How many hourly bars to hand the strategy. CSM reads c1h.iloc[-25] and a
# 14-period ATR, FF_V2 a 200-period EMA — but FF_V2 is not in production and
# the 1h frame is only used for momentum-style lookbacks. 48 covers every
# production strategy with margin; raise it if a strategy needs deeper 1h.
_H_WINDOW = 48


def _is_1m_strategy(strategy_class):
    """True if the strategy should replay through the 1m (Tire 2) harness.

    Was `> 200`, which silently excluded CSM: its REQUIRES_1M_DEPTH is exactly
    200, the default, so `200 > 200` was False and CSM fell through to
    run_backtest(), the legacy 15m replay. data/ holds only *_1m_*.csv — there
    are no 15m files at all — so load_data(symbol, "15m", days) returned None
    and run_backtest() returned [] for every symbol. CSM therefore reported
    "0 trades" on any run while generating thousands of signals, and the
    documented +0.300%/trade baseline could not be reproduced.

    run_backtest_1m() is explicitly built for CSM (it resamples 1m->15m and
    1m->1h precisely so CSM's df_15m/df_1h arguments can be served), so the
    boundary belongs at >=, not >.
    """
    return getattr(strategy_class, "REQUIRES_1M_DEPTH", 200) >= 200


def run_backtest_1m(strategy_class, symbol, regime_series=None,
                    mock_regime_name=None, days=30, verbose=False):
    """
    Replay a strategy that requires 1m data (freqtrade ports).

    These strategies resample 1m→5m internally, so they need raw 1m bars
    instead of the 15m bars used by the core strategies. Steps through the
    1m data every 5 bars (= one 5m candle at a time).
    """
    df_1m_raw = load_data(symbol, "1m", days)
    if df_1m_raw is None or len(df_1m_raw) < 1500:
        return []

    # Resample 1m → 1h for strategies that check df_1h (NASOS_V4, EI3_V2, CSM)
    ohlcv = df_1m_raw[["open", "high", "low", "close", "volume"]]
    df_1h = ohlcv.resample("1h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna()

    # Resample 1m → 15m for strategies that use df_15m (CSM)
    df_15m = ohlcv.resample("15min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna()

    strategy = strategy_class()
    sid      = strategy_class.STRATEGY_ID
    needs_1h  = getattr(strategy_class, "REQUIRES_1H", False)
    needs_15m = getattr(strategy_class, "REQUIRES_15M", False) or needs_1h

    trades, active = [], None
    WARMUP = 1500
    STEP   = 60
    WINDOW = 1500

    for i in range(WARMUP, len(df_1m_raw), STEP):
        start = max(0, i - WINDOW)
        cur_1m = df_1m_raw.iloc[start:i + 1]
        now    = cur_1m.index[-1]

        # ── Strict as-of: a bar is visible only once it has CLOSED ──────────
        # df_1h and df_15m are resampled from the FULL 1m series above, so a
        # bar is LABELLED by its open time but CONTAINS the whole interval.
        # Slicing on `index <= now` therefore admits the bar covering `now` --
        # at now=10:00 that is the 10:00 hourly bar, carrying up to 59 minutes
        # of future price. The 15m frame is worse: CSM takes its ENTRY PRICE
        # and its SL/TP ATR from that frame's last close.
        #
        # run_backtest() (the 15m path) was fixed for exactly this via
        # _hourly_as_of(). This 1m path never was, and had no switch.
        #
        # Measured, CSM 25 symbols / 90d, this line the only variable.
        #
        #   BASELINE config (69% LONG):
        #     leaked   1154 trades  WIN 54.1%  E +0.789%  PF 1.56
        #     strict   1156 trades  WIN 50.6%  E +0.367%  PF 1.22   -> 2.1x inflated
        #
        #   CONFIG A (100% SHORT):
        #     leaked    847 trades  WIN 63.9%  E -0.161%  PF 0.87
        #     strict    851 trades  WIN 62.4%  E +0.033%  PF 1.03   -> UNDERSTATED
        #
        # THE BIAS HAS A SIGN, AND IT FOLLOWS TRADE DIRECTION. The leaked entry
        # price is the close of a 15m bar still in progress, i.e. slightly into
        # the future of the move that triggered the signal. Momentum continues
        # on average, so that price is BETTER for a long and WORSE for a short.
        # Long-biased configs are flattered; short-only configs are penalised.
        # Do not describe this as a uniform inflation -- it is not.
        #
        # Either way the figures are wrong. Long-heavy results produced by this
        # function before 2026-08-29 are overstated (EXPERIMENT_LOG 17.13 /
        # 17.15 and the 17.21 regime table all predate the fix). NASOS_V4 reads
        # df_1h only via .iloc[-4:-1], so the port numbers are unaffected.
        #
        # BT_LOOKAHEAD_1H=true restores the old behaviour so historical runs
        # stay reproducible. It is not a tuning knob.
        if LOOKAHEAD_1H:
            cur_1h  = df_1h[df_1h.index <= now] if needs_1h else None
            cur_15m = df_15m[df_15m.index <= now] if needs_15m else None
        else:
            cur_1h  = df_1h[df_1h.index + _ONE_HOUR <= now] if needs_1h else None
            cur_15m = df_15m[df_15m.index + _QUARTER <= now] if needs_15m else None

        if mock_regime_name:
            regime = {"regime": mock_regime_name, "funding": 0.0}
        else:
            regime = dict(regime_at(regime_series, now + pd.Timedelta(minutes=1)))

        # ── Manage an open position ─────────────────────────────────────────
        if active:
            # Scan EVERY 1m bar since the previous step, not just the last one.
            # See _intrabar_exit_scan: the old single-bar check tested the stop
            # against one minute in STEP, silently letting losers recover.
            seg = df_1m_raw.iloc[max(start, i - STEP + 1):i + 1]
            hit = _intrabar_exit_scan(active, seg)
            if hit:
                px, kind, hit_ts = hit
                if kind == "TARGET":
                    reason = "TP_HIT"
                else:
                    try:
                        reason = strategy.stop_exit_reason(active)
                    except Exception:
                        reason = "SL_HIT"
                entry = float(active["entry_price"])
                pnl   = (px - entry) / entry if active["direction"] == "LONG" \
                        else (entry - px) / entry
                active.update(exit_price=px, exit_time=hit_ts, exit_reason=reason,
                              pnl_pct=pnl, intrabar=True)
                trades.append(active)
                active = None
                continue

            exit_sig = strategy.manage(active, cur_1m, session_pnl=0)
            if exit_sig and exit_sig.get("exit"):
                px    = float(exit_sig["exit_price"])
                entry = float(active["entry_price"])
                pnl   = (px - entry) / entry if active["direction"] == "LONG" \
                        else (entry - px) / entry
                active.update(exit_price=px, exit_time=now,
                              exit_reason=exit_sig["exit_reason"], pnl_pct=pnl)
                trades.append(active)
                active = None
            continue

        # ── Scan for a new entry ────────────────────────────────────────────
        try:
            signal = strategy.scan(symbol, cur_1m, cur_15m, cur_1h, regime)
        except Exception as exc:
            if verbose:
                print(f"  ! {sid}/{symbol} scan error at {now}: {exc}")
            continue

        if signal and not signal.get("near_miss"):
            # Model live_scanner's MIN_STRENGTH gate. Without this the harness
            # scores signals the live scanner would silently discard, so a
            # strategy whose edge comes ENTIRELY from that gate (NASOS_V4 is
            # -0.001% ungated and +0.262% gated) measures as worthless here.
            # Off by default so existing runs are unchanged; set BT_MIN_STRENGTH
            # to match live_scanner.MIN_STRENGTH to compare like for like.
            if float(signal.get("strength", 1.0)) < BT_MIN_STRENGTH:
                continue
            if not _bt_regime_ok(sid, regime.get("regime", "UNKNOWN")):
                continue
            signal["entry_time"] = now
            signal["regime_at_entry"] = regime.get("regime", "UNKNOWN")
            signal["initial_sl_price"] = signal.get("sl_price")
            active = signal

    # Force-close anything still open at the end of the sample.
    if active:
        px    = float(df_1m_raw.iloc[-1]["close"])
        entry = float(active["entry_price"])
        pnl   = (px - entry) / entry if active["direction"] == "LONG" \
                else (entry - px) / entry
        active.update(exit_price=px, exit_time=df_1m_raw.index[-1],
                      exit_reason="END_OF_DATA", pnl_pct=pnl)
        trades.append(active)

    for t in trades:
        t.pop("secondary_15m", None)
        t["strategy"] = sid
        t["symbol"]   = symbol
        t["net_pct"]  = t["pnl_pct"] - ROUND_TRIP_FEE - 2 * SLIPPAGE_PCT
        t["hours"]    = (t["exit_time"] - t["entry_time"]).total_seconds() / 3600

    if verbose:
        print(f"  {sid:<12}{symbol:<14}{len(trades):>4} trades")
    return trades


def run_backtest(strategy_class, symbol, regime_series=None,
                 mock_regime_name=None, days=30, verbose=False):
    """
    Replay one strategy over one symbol.

    Returns a list of closed-trade dicts, each carrying entry_time/exit_time
    so the portfolio layer can order them chronologically. Returns [] when
    data is unavailable.
    """
    df_15m = load_data(symbol, "15m", days)
    df_1h  = load_data(symbol, "1h",  days)
    if df_15m is None or df_1h is None or len(df_15m) < WARMUP_BARS + 10:
        return []

    strategy = strategy_class()
    sid      = strategy_class.STRATEGY_ID

    # Secondary asset for relative-strength strategies.
    secondary = "ETHUSDT" if symbol == "BTCUSDT" else "BTCUSDT"
    df_sec    = load_data(secondary, "15m", days)

    trades, active = [], None

    # Live-faithful 1h reconstruction (see _hourly_as_of / LOOKAHEAD_1H).
    _h_ts    = df_1h.index.values
    _h_np    = df_1h[_OHLCV].to_numpy()
    _df_1h_o = df_1h[_OHLCV]
    _partial = _build_partial_hours(df_15m)

    for i in range(WARMUP_BARS, len(df_15m)):
        cur_15m = df_15m.iloc[:i + 1]
        now     = cur_15m.index[-1]          # OPEN time of this 15m bar
        # The bar's entry price is its CLOSE, 15 minutes after `now`. Every
        # as-of decision is made against that instant, not against `now`.
        bar_close = now + _QUARTER

        if LOOKAHEAD_1H:
            cur_1h = df_1h[df_1h.index <= now]        # legacy, contaminated
        else:
            cur_1h = _hourly_as_of(_df_1h_o, _h_ts, _h_np, _partial, i, bar_close)

        if mock_regime_name:
            regime = {"regime": mock_regime_name, "funding": 0.0}
        else:
            # bar_close, not `now` — the regime known at the moment we fill.
            regime = dict(regime_at(regime_series, bar_close))

        if df_sec is not None:
            regime["secondary_15m"]     = df_sec[df_sec.index <= now]
            regime["secondary_symbol"]  = secondary

        # ── Manage an open position ──────────────────────────────────────────
        if active:
            # Intrabar stop/target FIRST. active["sl_price"] is whatever the
            # previous bar's manage() left it at (breakeven/trail move it), so
            # testing this bar's range against it is causally correct — the
            # stop was already resting there when the bar opened.
            hit = _intrabar_exit(active, cur_15m.iloc[-1])
            if hit:
                px, kind = hit
                if kind == "TARGET":
                    reason = "TP_HIT"
                else:
                    # Ask the strategy how to name this stop — it knows whether
                    # breakeven or trailing has moved it since entry.
                    try:
                        reason = strategy.stop_exit_reason(active)
                    except Exception:
                        reason = "SL_HIT"
                entry = float(active["entry_price"])
                pnl   = (px - entry) / entry if active["direction"] == "LONG" \
                        else (entry - px) / entry
                active.update(exit_price=px, exit_time=now, exit_reason=reason,
                              pnl_pct=pnl, intrabar=True)
                trades.append(active)
                active = None
                continue

            exit_sig = strategy.manage(active, cur_15m.tail(1), session_pnl=0)
            if exit_sig and exit_sig.get("exit"):
                px    = float(exit_sig["exit_price"])
                entry = float(active["entry_price"])
                pnl   = (px - entry) / entry if active["direction"] == "LONG" \
                        else (entry - px) / entry
                active.update(exit_price=px, exit_time=now,
                              exit_reason=exit_sig["exit_reason"], pnl_pct=pnl)
                trades.append(active)
                active = None
            continue

        # ── Scan for a new entry ─────────────────────────────────────────────
        try:
            signal = strategy.scan(symbol, None, cur_15m, cur_1h, regime)
        except Exception as exc:
            if verbose:
                print(f"  ! {sid}/{symbol} scan error at {now}: {exc}")
            continue

        # near_miss dicts are diagnostic-only (live scanner logs how close a
        # strategy came to firing). They carry no entry_price/direction, so
        # treating one as a fill would corrupt the sample and crash on exit.
        if signal and not signal.get("near_miss"):
            if not _bt_regime_ok(sid, regime.get("regime", "UNKNOWN")):
                continue
            signal["entry_time"] = now
            signal["regime_at_entry"] = regime.get("regime", "UNKNOWN")
            # Freeze the stop as it stood at entry. Breakeven/trailing mutate
            # sl_price in place, so by exit time it no longer describes the
            # risk that was taken — and position size is a function of the
            # ENTRY stop distance (notional = risk$ / SL%). Sizing off a
            # trailed stop would silently inflate every position.
            signal["initial_sl_price"] = signal.get("sl_price")
            active = signal

    # Force-close anything still open at the end of the sample.
    if active:
        px    = float(df_15m.iloc[-1]["close"])
        entry = float(active["entry_price"])
        pnl   = (px - entry) / entry if active["direction"] == "LONG" \
                else (entry - px) / entry
        active.update(exit_price=px, exit_time=df_15m.index[-1],
                      exit_reason="END_OF_DATA", pnl_pct=pnl)
        trades.append(active)

    # Strip DataFrames the strategies may have attached before returning.
    for t in trades:
        t.pop("secondary_15m", None)
        t["strategy"] = sid
        t["symbol"]   = symbol
        # Slippage is charged on BOTH sides. A market entry crosses the spread
        # and a STOP_MARKET fills past its trigger, so a round trip pays it
        # twice — same shape as the fee.
        t["net_pct"]  = t["pnl_pct"] - ROUND_TRIP_FEE - 2 * SLIPPAGE_PCT
        t["hours"]    = (t["exit_time"] - t["entry_time"]).total_seconds() / 3600

    if verbose:
        print(f"  {sid:<6}{symbol:<14}{len(trades):>4} trades")
    return trades


def collect_trades(symbols, strategies, regime_series, use_regime=True,
                   days=30, cache_path=None):
    """Run every requested strategy over every symbol."""
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            raw = json.load(f)
        for t in raw:
            t["entry_time"] = pd.Timestamp(t["entry_time"])
            t["exit_time"]  = pd.Timestamp(t["exit_time"])
        # The cache may hold a superset (all strategies / all symbols). Honour
        # the current --strategies and --symbols filters, otherwise a subset
        # run silently reports the full cached population.
        want_s, want_y = set(strategies), set(symbols)
        kept = [t for t in raw
                if t["strategy"] in want_s and t["symbol"] in want_y]
        print(f"Loaded {len(kept)} trades from cache {cache_path}"
              + (f" (filtered from {len(raw)})" if len(kept) != len(raw) else ""))
        missing = want_s - {t["strategy"] for t in kept}
        if missing:
            print(f"  ! cache has no trades for {sorted(missing)} — "
                  f"delete {cache_path} to regenerate")
        return kept

    out = []
    for sid in strategies:
        cls = STRATEGY_CLASSES[sid]
        runner = run_backtest_1m if _is_1m_strategy(cls) else run_backtest
        n0  = len(out)
        for sym in symbols:
            out.extend(runner(
                cls, sym,
                regime_series    = regime_series if use_regime else None,
                mock_regime_name = None if use_regime else MOCK_REGIME[sid],
                days             = days,
            ))
        print(f"  {sid:<12}{len(out) - n0:>6} trades")

    if cache_path:
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(out, f, default=str)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# METRICS
# ═════════════════════════════════════════════════════════════════════════════

def expectancy(trades):
    """Leverage-independent per-trade statistics, net of fees."""
    if not trades:
        return None
    n = np.array([t["net_pct"] for t in trades])
    w, l = n[n > 0], n[n <= 0]
    srt = np.sort(n)[::-1]

    streak = mx = 0
    for t in sorted(trades, key=lambda x: x["entry_time"]):
        streak = streak + 1 if t["net_pct"] <= 0 else 0
        mx = max(mx, streak)

    return {
        "n": len(n),
        "win": len(w) / len(n) * 100,
        "avg_w": w.mean() * 100 if len(w) else 0.0,
        "avg_l": l.mean() * 100 if len(l) else 0.0,
        "rr": abs(w.mean() / l.mean()) if len(w) and len(l) and l.mean() else 0.0,
        "exp": n.mean() * 100,
        "median": np.median(n) * 100,
        "sum": n.sum() * 100,
        "pf": w.sum() / abs(l.sum()) if len(l) and l.sum() else float("inf"),
        "worst": n.min() * 100,
        "best": n.max() * 100,
        "streak": mx,
        "hours": float(np.median([t["hours"] for t in trades])),
        "ex_top5": srt[5:].sum() * 100 if len(srt) > 5 else 0.0,
        "ex_top10": srt[10:].sum() * 100 if len(srt) > 10 else 0.0,
    }


def simulate_portfolio(trades, capital=INITIAL_CAPITAL, leverage=LEVERAGE,
                       slots=MAX_CONCURRENT, margin_pct=MAX_MARGIN_PCT,
                       daily_cap=DAILY_LOSS_CAP, weekly_cap=WEEKLY_LOSS_CAP,
                       label="", verbose=True):
    """
    Chronological portfolio replay with the live risk model enforced.

    Fixes vs. the old implementation: trades are ordered by entry_time across
    all symbols; equity is floored at zero; fees are charged on notional; and
    concurrency, margin and loss caps are respected.
    """
    if not trades:
        return None

    from modules.risk_engine import compute_position_size, max_total_margin_pct
    MAX_TOTAL_MARGIN_PCT = max_total_margin_pct()

    # ── Live gates this layer used to ignore ─────────────────────────────────
    # live_scanner enforces all three on every entry; without them the
    # backtest took trades production would have refused, which is most of the
    # documented "backtest 18.3 trades/day vs live ~11" gap.
    #
    #   cooldown   : after a LOSING trade on a symbol, that symbol is blocked
    #                for LOSS_COOLDOWN_MINUTES (live_scanner._symbol_loss_cooldown)
    #   per cycle  : at most MAX_ENTRIES_PER_CYCLE opened per scan cycle
    #   ranking    : candidates are sorted by `strength` before slots are
    #                handed out, not taken in arrival order
    #                (live_scanner._scan_for_signals sorts descending)
    COOLDOWN = pd.Timedelta(minutes=int(os.getenv("LOSS_COOLDOWN_MINUTES", "15")))
    from modules import settings_manager as _cfg
    PER_CYCLE = _cfg.get("MAX_ENTRIES_PER_CYCLE")

    # Group by entry timestamp = one scan cycle, then rank within it. Ties on
    # strength keep arrival order, which is what live does (Python sort is
    # stable and the scanner iterates symbols in watchlist order).
    ts = sorted(trades, key=lambda t: (t["entry_time"],
                                       -float(t.get("strength", 1.0) or 1.0)))
    eq = peak = capital
    mdd = 0.0
    open_pos, curve = [], []
    taken = slot_skip = cap_skip = 0
    size_skip = margin_skip = 0
    cooldown_skip = cycle_skip = 0
    cooldown_until: dict[str, pd.Timestamp] = {}
    cycle_at, cycle_n = None, 0
    used_margin = 0.0
    blown = False

    d0, cur_day,  d_blocked = eq, ts[0]["entry_time"].date(), False
    w0, cur_week, w_blocked = eq, ts[0]["entry_time"].isocalendar()[:2], False

    def close_due(now):
        nonlocal eq, peak, mdd, blown, used_margin
        keep = []
        for p in open_pos:
            if p["exit_time"] <= now:
                eq += p["pnl_usd"]
                used_margin -= p["margin"]          # release the posted margin
                if eq <= 0:
                    eq, blown = 0.0, True
                peak = max(peak, eq)
                mdd  = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
                curve.append((p["exit_time"], eq))
                # Live registers the cooldown on a LOSS only, and measures it
                # net — see live_scanner._manage_positions, which keys off
                # pos["pnl_equity_pct"] < 0 after fees.
                if p["pnl_usd"] < 0:
                    cooldown_until[p["symbol"]] = p["exit_time"] + COOLDOWN
            else:
                keep.append(p)
        open_pos[:] = keep
        if not open_pos:
            used_margin = 0.0                       # guard against fp drift

    for t in ts:
        now = t["entry_time"]
        close_due(now)
        if blown:
            break

        if now.date() != cur_day:
            cur_day, d0, d_blocked = now.date(), eq, False
        if now.isocalendar()[:2] != cur_week:
            cur_week, w0, w_blocked = now.isocalendar()[:2], eq, False

        if daily_cap is not None and d0 > 0 and (eq - d0) / d0 <= daily_cap:
            d_blocked = True
        if weekly_cap is not None and w0 > 0 and (eq - w0) / w0 <= weekly_cap:
            w_blocked = True

        if d_blocked or w_blocked:
            cap_skip += 1
            continue
        if len(open_pos) >= slots:
            slot_skip += 1
            continue

        # ── Per-cycle entry cap ──────────────────────────────────────────────
        # One scan cycle = one entry timestamp. Live opens at most
        # MAX_ENTRIES_PER_CYCLE per cycle even when slots are free, so a burst
        # of simultaneous signals fills over several cycles rather than at once.
        if now != cycle_at:
            cycle_at, cycle_n = now, 0
        if cycle_n >= PER_CYCLE:
            cycle_skip += 1
            continue

        # ── Per-symbol loss cooldown ─────────────────────────────────────────
        cd = cooldown_until.get(t["symbol"])
        if cd is not None and now < cd:
            cooldown_skip += 1
            continue

        # A symbol already open cannot be re-entered (live.is_symbol_active).
        if any(p["symbol"] == t["symbol"] for p in open_pos):
            slot_skip += 1
            continue

        # ── Size the position exactly as the live engine would ───────────────
        #
        # This previously used `margin = eq * MAX_MARGIN_PCT` and
        # `notional = margin * LEVERAGE`, i.e. a flat 30% margin at 5x = 1.5x
        # equity per position, 4.5x across 3 slots. Live sizes by RISK:
        # notional = risk$ / SL%, which on 2026-08-11 produced a mean notional
        # of 0.27x equity and 5.7% margin. The old model therefore sized every
        # position ~5.5x too large and reported returns inflated by roughly the
        # same factor — the difference between a "+650%" backtest and a
        # believable one.
        #
        # Calling the real function also brings the rest of the live risk model
        # with it, none of which was modelled before: the per-strategy leverage
        # step-down when SL% x leverage would breach MAX_LEVERAGED_LOSS_PCT,
        # the MIN_SL_PCT / 20% SL sanity rejections, and MIN_NOTIONAL_USDT.
        entry_px = float(t.get("entry_price") or 0)
        sl_px    = t.get("initial_sl_price") or t.get("sl_price")
        if not entry_px or sl_px is None:
            size_skip += 1
            continue

        size = compute_position_size(eq, entry_px, float(sl_px), t.get("strategy", ""))
        if not size.get("valid"):
            size_skip += 1
            continue

        notional = float(size["notional"])
        margin   = float(size["margin_req"])

        # Aggregate margin ceiling across concurrently open positions. The old
        # code applied margin_pct per position with no portfolio total, so
        # three slots could commit 90% of equity as margin.
        if used_margin + margin > eq * MAX_TOTAL_MARGIN_PCT:
            margin_skip += 1
            continue

        # Isolated margin: a position cannot lose more than the margin posted.
        pnl_usd  = max(notional * (t["pnl_pct"] - ROUND_TRIP_FEE
                                   - 2 * SLIPPAGE_PCT), -margin)
        used_margin += margin
        open_pos.append({"exit_time": t["exit_time"], "pnl_usd": pnl_usd,
                         "margin": margin, "symbol": t["symbol"]})
        taken += 1
        cycle_n += 1

    if not blown and open_pos:
        close_due(max(p["exit_time"] for p in open_pos))

    sharpe = 0.0
    if len(curve) > 2:
        s = pd.Series([e for _, e in curve],
                      index=pd.to_datetime([c for c, _ in curve]))
        r = s.resample("1D").last().ffill().pct_change().dropna()
        if len(r) > 1 and r.std() > 0:
            sharpe = float(r.mean() / r.std() * np.sqrt(365))

    ret = (eq - capital) / capital * 100
    res = {"label": label, "signals": len(ts), "taken": taken,
           "slot_skip": slot_skip, "cap_skip": cap_skip,
           "size_skip": size_skip, "margin_skip": margin_skip,
           "cooldown_skip": cooldown_skip, "cycle_skip": cycle_skip,
           "final": eq, "return_pct": ret, "max_dd": mdd * 100,
           "sharpe": sharpe, "blown": blown}

    if verbose:
        flag = "  ** BLOWN **" if blown else ""
        print(f"{label:<40}{taken:>6}{ret:>11.2f}%{mdd*100:>9.1f}%{sharpe:>8.2f}{flag}")
    return res


# ═════════════════════════════════════════════════════════════════════════════
# LEGACY-COMPATIBLE ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def run_portfolio_backtest(strategy_class, symbols, regime_series=None,
                           default_regime=None, days=30):
    """
    Kept for backward compatibility with existing callers.
    Now correct: chronological, fee-on-notional, risk-model-aware, floored.

    Back-compat note: the pre-rewrite signature was
        run_portfolio_backtest(strategy_class, symbols, default_regime="BULL_TREND")
    so existing callers (run_specific_backtest.py) pass a regime STRING third
    positionally. That now binds to regime_series and blew up in regime_at()
    with "string indices must be integers". Accept either.
    """
    if isinstance(regime_series, str):
        default_regime, regime_series = regime_series, None

    trades = []
    for sym in symbols:
        trades.extend(run_backtest(
            strategy_class, sym,
            regime_series    = regime_series,
            mock_regime_name = default_regime if regime_series is None else None,
            days             = days))

    sid = strategy_class.STRATEGY_ID
    e   = expectancy(trades)
    print(f"\n--- Portfolio Results: {sid} ({len(symbols)} coins) ---")
    if not e:
        print("No trades.\n")
        return trades

    print(f"Trades              : {e['n']}")
    print(f"Win rate            : {e['win']:.2f}%")
    print(f"Avg win / avg loss  : {e['avg_w']:+.2f}% / {e['avg_l']:+.2f}%  (R:R {e['rr']:.2f})")
    print(f"Expectancy per trade: {e['exp']:+.3f}%   [net of {ROUND_TRIP_FEE*100:.2f}% fees]")
    print(f"Profit factor       : {e['pf']:.2f}")
    print(f"Sum of net returns  : {e['sum']:+.1f}%")
    print(f"\n--- ${INITIAL_CAPITAL:.0f} capital, {LEVERAGE}x, "
          f"{MAX_CONCURRENT} slots, {MAX_MARGIN_PCT:.0%} margin ---")
    print(f"{'CONFIGURATION':<40}{'TAKEN':>6}{'RETURN':>11}{'MAXDD':>9}{'SHARPE':>8}")
    simulate_portfolio(trades, label=sid)
    print()
    return trades


# ═════════════════════════════════════════════════════════════════════════════
# REPORT
# ═════════════════════════════════════════════════════════════════════════════

def print_master(by_strat):
    print("\n" + "=" * 104)
    print("PER-TRADE EXPECTANCY  —  leverage-independent, net of fees")
    print("=" * 104)
    print(f"{'STRAT':<7}{'TIER':<7}{'N':>6}{'WIN%':>7}{'AVG W':>8}{'AVG L':>8}{'R:R':>6}"
          f"{'E[net]':>9}{'MEDIAN':>9}{'PF':>6}{'SUM':>10}{'HOLD':>7}"
          f"{'WORST':>9}{'STREAK':>7}  EDGE")
    print("-" * 104)
    rows = [(s, expectancy(by_strat[s])) for s in STRATEGY_ORDER if by_strat.get(s)]
    rows.sort(key=lambda x: -x[1]["exp"])
    for s, e in rows:
        print(f"{s:<7}{TIER[s]:<7}{e['n']:>6}{e['win']:>6.1f}%{e['avg_w']:>7.2f}%"
              f"{e['avg_l']:>7.2f}%{e['rr']:>6.2f}{e['exp']:>8.3f}%{e['median']:>8.3f}%"
              f"{e['pf']:>6.2f}{e['sum']:>9.1f}%{e['hours']:>6.1f}h{e['worst']:>8.2f}%"
              f"{e['streak']:>7}  {'YES' if e['exp'] > 0 else 'no'}")
    print("-" * 104)
    print("R:R = avg win / avg loss.  PF = profit factor (>1.00 profitable).")
    print("STREAK = longest consecutive losing run.")
    return rows


def print_robustness(by_strat, rows):
    print("\n" + "=" * 104)
    print("ROBUSTNESS  —  does the edge survive removing the best trades?")
    print("=" * 104)
    print(f"{'STRAT':<7}{'SUM':>11}{'ex TOP5':>11}{'ex TOP10':>11}{'BEST':>10}  VERDICT")
    print("-" * 104)
    for s, e in rows:
        if e["sum"] <= 0:
            v = "no edge to test"
        elif e["ex_top10"] > 0:
            v = "robust — broad edge"
        elif e["ex_top5"] > 0:
            v = "moderate — thins out"
        else:
            v = "FRAGILE — outlier-driven"
        print(f"{s:<7}{e['sum']:>10.1f}%{e['ex_top5']:>10.1f}%{e['ex_top10']:>10.1f}%"
              f"{e['best']:>9.2f}%  {v}")


def print_splits(by_strat, rows):
    print("\n" + "=" * 104)
    print("CRYPTO-NATIVE vs TOKENISED EQUITY")
    print("=" * 104)
    print(f"{'STRAT':<7}{'CRYPTO N':>10}{'CRYPTO E[]':>12}{'CRYPTO SUM':>12}"
          f"{'':4}{'EQUITY N':>10}{'EQUITY E[]':>12}{'EQUITY SUM':>12}")
    print("-" * 104)
    for s, _ in rows:
        c = expectancy([t for t in by_strat[s] if not is_equity(t["symbol"])])
        q = expectancy([t for t in by_strat[s] if is_equity(t["symbol"])])
        cs = f"{c['n']:>10}{c['exp']:>11.3f}%{c['sum']:>11.1f}%" if c else f"{0:>10}{'—':>12}{'—':>12}"
        qs = f"{q['n']:>10}{q['exp']:>11.3f}%{q['sum']:>11.1f}%" if q else f"{0:>10}{'—':>12}{'—':>12}"
        print(f"{s:<7}{cs}{'':4}{qs}")


def print_exits(by_strat, rows):
    print("\n" + "=" * 104)
    print("EXIT BEHAVIOUR")
    print("=" * 104)
    print(f"{'STRAT':<7}{'SL HIT':>16}{'TP HIT':>16}   OTHER")
    print("-" * 104)
    for s, _ in rows:
        c   = Counter(t["exit_reason"] for t in by_strat[s])
        tot = len(by_strat[s])
        sl, tp = c.get("SL_HIT", 0), c.get("TP_HIT", 0)
        other = ", ".join(f"{k}={v}" for k, v in c.items()
                          if k not in ("SL_HIT", "TP_HIT")) or "—"
        print(f"{s:<7}{sl:>7} ({sl/tot*100:>4.1f}%){tp:>8} ({tp/tot*100:>4.1f}%)   {other}")


def print_weekly(by_strat, rows, trades):
    weeks = sorted({t["entry_time"].isocalendar()[:2] for t in trades})
    print("\n" + "=" * 104)
    print("WEEKLY CONSISTENCY  —  sum of net returns per ISO week")
    print("=" * 104)
    print(f"{'STRAT':<7}" + "".join(f"{'W'+str(w[1]):>11}" for w in weeks) + f"{'POSITIVE':>12}")
    print("-" * 104)
    for s, _ in rows:
        cells, pos = "", 0
        for w in weeks:
            v = sum(t["net_pct"] for t in by_strat[s]
                    if t["entry_time"].isocalendar()[:2] == w) * 100
            pos += v > 0
            cells += f"{v:>10.1f}%"
        print(f"{s:<7}{cells}{pos:>7}/{len(weeks):<4}")


def print_regime_breakdown(trades):
    if not any(t.get("regime_at_entry") for t in trades):
        return
    print("\n" + "=" * 104)
    print("PERFORMANCE BY REGIME AT ENTRY")
    print("=" * 104)
    by = defaultdict(list)
    for t in trades:
        by[t.get("regime_at_entry", "UNKNOWN")].append(t)
    print(f"{'REGIME':<14}{'N':>7}{'WIN%':>8}{'E[net]':>10}{'SUM':>11}")
    print("-" * 104)
    for r, ts in sorted(by.items(), key=lambda x: -len(x[1])):
        e = expectancy(ts)
        print(f"{r:<14}{e['n']:>7}{e['win']:>7.1f}%{e['exp']:>9.3f}%{e['sum']:>10.1f}%")


MIN_SAMPLE = 20   # below this a per-cell expectancy is noise, not signal


def print_permission_matrix(by_strat, trades):
    """
    Strategy x regime expectancy — what REGIME_STRATEGY_PERMISSIONS should say.

    regime_engine.REGIME_STRATEGY_PERMISSIONS is currently all-True for every
    regime, so the "master switch" described in the project brief gates
    nothing. This matrix is the evidence needed to populate it.
    """
    if not any(t.get("regime_at_entry") for t in trades):
        return
    regimes = sorted({t.get("regime_at_entry") for t in trades} - {None, "UNKNOWN"})
    if not regimes:
        return

    print("\n" + "=" * 104)
    print("STRATEGY x REGIME  —  expectancy per trade (n) in each regime")
    print("=" * 104)
    print(f"{'STRAT':<7}" + "".join(f"{r:>21}" for r in regimes))
    print("-" * 104)
    grid = {}
    for s in STRATEGY_ORDER:
        if not by_strat.get(s):
            continue
        cells = ""
        for r in regimes:
            ts = [t for t in by_strat[s] if t.get("regime_at_entry") == r]
            if not ts:
                cells += f"{'—':>21}"
                grid[(s, r)] = None
                continue
            e = expectancy(ts)
            grid[(s, r)] = e
            mark = "" if e["n"] >= MIN_SAMPLE else "?"
            cells += f"{e['exp']:>14.3f}% {'(' + str(e['n']) + ')' + mark:>6}"
        print(f"{s:<7}{cells}")
    print("-" * 104)
    print(f"? = fewer than {MIN_SAMPLE} trades in that cell — too thin to act on.")

    print("\nSUGGESTED REGIME_STRATEGY_PERMISSIONS (positive expectancy, "
          f"n >= {MIN_SAMPLE}):")
    print("-" * 104)
    for r in regimes:
        keep = [s for s in STRATEGY_ORDER
                if grid.get((s, r)) and grid[(s, r)]["exp"] > 0
                and grid[(s, r)]["n"] >= MIN_SAMPLE]
        thin = [s for s in STRATEGY_ORDER
                if grid.get((s, r)) and grid[(s, r)]["exp"] > 0
                and grid[(s, r)]["n"] < MIN_SAMPLE]
        note = f"   (thin, unproven: {', '.join(thin)})" if thin else ""
        print(f'  "{r}": ' + "{" +
              ", ".join(f'"{s}": True' for s in keep) + "}" + note)
    print("-" * 104)
    print("Derived from ONE in-sample window. Validate out-of-sample before")
    print("editing regime_engine.py — this is a hypothesis, not a config file.")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="CSB portfolio backtester")
    ap.add_argument("--symbols",    default=None, help="comma-separated; default = focused watchlist")
    ap.add_argument("--strategies", default=None, help="comma-separated IDs; default = all 7")
    ap.add_argument("--days",       type=int,   default=30,
                    help="which cached CSV set to load (data/SYM_TF_<days>d.csv)")
    ap.add_argument("--last-days",  type=int,   default=None,
                    help="score only trades ENTERED in the last N days. Warmup "
                         "and regime still use the full history, so a 200-bar "
                         "EMA is fully primed. Use this for short windows "
                         "instead of fetching less data.")
    ap.add_argument("--capital",    type=float, default=INITIAL_CAPITAL)
    ap.add_argument("--leverage",   type=int,   default=LEVERAGE)
    ap.add_argument("--slots",      type=int,   default=MAX_CONCURRENT)
    ap.add_argument("--margin",     type=float, default=MAX_MARGIN_PCT)
    ap.add_argument("--daily-cap",  type=float, default=DAILY_LOSS_CAP)
    ap.add_argument("--weekly-cap", type=float, default=WEEKLY_LOSS_CAP)
    ap.add_argument("--no-caps",     action="store_true", help="disable loss caps")
    ap.add_argument("--no-regime",   action="store_true", help="use legacy fixed mock regime")
    ap.add_argument("--crypto-only", action="store_true", help="drop tokenised equities")
    ap.add_argument("--keep-corrupt", action="store_true", help="do not quarantine split symbols")
    ap.add_argument("--cache",       default=None, help="cache trades to this JSON path")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        from modules.watchlist import get_focused_watchlist
        from modules import settings_manager as _cfg
        symbols = get_focused_watchlist(_cfg.get("FOCUSED_SIZE"))

    # Default to the production set, not every strategy that has a class.
    strategies = [s.strip().upper() for s in args.strategies.split(",")] \
        if args.strategies else list(PRODUCTION_IDS)
    unknown = [s for s in strategies if s not in STRATEGY_CLASSES]
    if unknown:
        sys.exit(f"Unknown strategy IDs: {unknown}. Valid: {list(STRATEGY_CLASSES)}")

    print("=" * 104)
    print(f"CSB PORTFOLIO BACKTEST — {args.days} DAYS — {len(symbols)} SYMBOLS")
    print("=" * 104)

    # ── Data integrity ───────────────────────────────────────────────────────
    if not args.keep_corrupt:
        corrupt = check_integrity(symbols, args.days)
        if corrupt:
            print("\nQUARANTINED — unadjusted corporate action (single bar > "
                  f"{SPLIT_THRESHOLD:.0%}):")
            for s, (r, a, b) in corrupt.items():
                print(f"  {s}: {r*100:+.1f}% in one 15m bar "
                      f"({a:.6g} -> {b:.6g}, x{b/a:.4f})")
            symbols = [s for s in symbols if s not in corrupt]

    if args.crypto_only:
        dropped = [s for s in symbols if is_equity(s)]
        symbols = [s for s in symbols if not is_equity(s)]
        if dropped:
            print(f"\nDropped {len(dropped)} tokenised equities: {', '.join(dropped)}")

    crypto = [s for s in symbols if not is_equity(s)]
    equity = [s for s in symbols if is_equity(s)]
    print(f"\nCrypto-native ({len(crypto)}): {', '.join(crypto) or 'none'}")
    if equity:
        print(f"Tokenised equity ({len(equity)}): {', '.join(equity)}")

    daily  = None if args.no_caps else args.daily_cap
    weekly = None if args.no_caps else args.weekly_cap
    print(f"\nCapital ${args.capital:.0f} | {args.leverage}x | "
          f"fee {ROUND_TRIP_FEE*100:.2f}% round-trip on notional")
    print(f"Slots {args.slots} | margin/trade {args.margin:.0%} | "
          f"daily cap {daily if daily is None else f'{daily:.0%}'} | "
          f"weekly cap {weekly if weekly is None else f'{weekly:.0%}'}")

    # ── Regime ───────────────────────────────────────────────────────────────
    regime_series = None
    if args.no_regime:
        print("\nRegime: LEGACY MOCK (fixed per strategy) — regime logic not exercised")
    else:
        print("\nClassifying regime per 1h bar from cached BTC/ETH/SOL + real funding...")
        regime_series = build_regime_series(args.days)
        if regime_series is None:
            print("  falling back to legacy mock regime")
            args.no_regime = True

    # ── Run ──────────────────────────────────────────────────────────────────
    print("\nRunning strategies...")
    trades = collect_trades(symbols, strategies, regime_series,
                            use_regime=not args.no_regime,
                            days=args.days, cache_path=args.cache)
    if not trades:
        sys.exit("\nNo trades produced — check that data/ contains the CSVs "
                 "(run fetch_binance_data.py first).")

    # Restrict the SCORING window without starving indicator warmup. Trades are
    # generated over the full history (so the 200-bar EMA and the regime series
    # are primed), then filtered by entry time.
    if args.last_days:
        latest = max(t["exit_time"] for t in trades)
        cutoff = latest - pd.Timedelta(days=args.last_days)
        before = len(trades)
        trades = [t for t in trades if t["entry_time"] >= cutoff]
        print(f"\nScoring window: last {args.last_days} days "
              f"({cutoff:%Y-%m-%d %H:%M} -> {latest:%Y-%m-%d %H:%M} UTC)")
        print(f"  {before} signals over full history -> {len(trades)} in window")
        if not trades:
            sys.exit("No trades inside the scoring window.")

    print(f"\nTotal signals: {len(trades)}")

    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t["strategy"]].append(t)

    rows = print_master(by_strat)
    print_robustness(by_strat, rows)
    if equity and crypto:
        print_splits(by_strat, rows)
    print_exits(by_strat, rows)
    print_weekly(by_strat, rows, trades)
    if not args.no_regime:
        print_regime_breakdown(trades)
        print_permission_matrix(by_strat, trades)

    # ── Portfolio ────────────────────────────────────────────────────────────
    kw = dict(capital=args.capital, leverage=args.leverage, slots=args.slots,
              margin_pct=args.margin, daily_cap=daily, weekly_cap=weekly)

    print("\n" + "=" * 104)
    print("PORTFOLIO SIMULATION  —  chronological, risk model enforced, equity floored at 0")
    print("=" * 104)
    print(f"{'CONFIGURATION':<40}{'TAKEN':>6}{'RETURN':>11}{'MAXDD':>9}{'SHARPE':>8}")
    print("-" * 104)
    for s, _ in rows:
        simulate_portfolio(by_strat[s], label=f"  {s} alone", **kw)
    print("-" * 104)
    simulate_portfolio(trades, label=f"ALL {len(strategies)} strategies", **kw)
    if crypto and equity:
        simulate_portfolio([t for t in trades if not is_equity(t["symbol"])],
                           label="  crypto-native only", **kw)
        simulate_portfolio([t for t in trades if is_equity(t["symbol"])],
                           label="  tokenised equity only", **kw)
    winners = [s for s, e in rows if e["exp"] > 0]
    if winners and len(winners) < len(rows):
        simulate_portfolio([t for t in trades if t["strategy"] in winners],
                           label=f"  positive-expectancy only ({'+'.join(winners)})", **kw)

    # ── Capacity ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 104)
    print("CAPACITY  —  signal volume determines slot share, not signal quality")
    print("=" * 104)
    tot = len(trades)
    med = np.median([t["hours"] for t in trades])
    print(f"Signals in {args.days}d: {tot} | slots: {args.slots} | "
          f"median hold: {med:.1f}h | theoretical max ~"
          f"{args.days*24/med*args.slots:.0f} trades")
    print(f"\n{'STRAT':<7}{'SIGNALS':>9}{'SHARE':>8}{'E[net]':>10}  ")
    for s, e in sorted(rows, key=lambda x: -len(by_strat[x[0]])):
        n = len(by_strat[s])
        print(f"{s:<7}{n:>9}{n/tot*100:>7.1f}%{e['exp']:>9.3f}%  "
              f"{'#' * int(n/tot*50)}")
    print("=" * 104)


if __name__ == "__main__":
    main()

