"""
strategies/trend_pullback.py
Trend Pullback (TP) Strategy — crypto analog of first_pullback_engine.py.

v2 changes:
  FIX-1 : Removed 1m cross requirement — was a timing lottery (60s scanner
           would miss 1-2 min cross windows). Replaced with 15m close
           confirmation: price must be within PULLBACK_ZONE of EMA20 AND
           the last 15m bar must close on the correct side of EMA.
  FIX-2 : Widened PULLBACK_ZONE from 0.5% to 1.0% — crypto pulls back
           0.5-1.5% to EMA routinely, 0.5% was too tight.
  FIX-3 : ATR-based SL replaces swing-low SL — swing low over 8×15m bars
           (2 hours) was producing SLs tighter than MIN_SL_PCT (0.5%).
           ATR-based SL = entry ± 1.5×ATR always produces a meaningful stop.

v3 changes (strategy improvement pass):
  FIX-4 : RSI_BULL_HIGH raised 55 → 62. In genuine bull trends, EMA20
           pullbacks routinely show RSI 55-65. The old cap of 55 blocked
           valid entries at the sweet spot of the pullback bounce.
  FIX-5 : TRAIL_ATR_MULT tightened 3.0 → 2.5 after BE triggers. Winners
           reaching the BE threshold were giving back excessive profit
           before the trail fired. 2.5× locks in more on the way out.

Entry logic (15m candles, regime must be BULL_TREND or BEAR_TREND):
  BULL_TREND → LONG:
    1. EMA20 > EMA50 (trend is up)
    2. Price within 1.0% of EMA20 (pullback zone)
    3. Last 15m bar closes ABOVE EMA20 (not below — trend holding)
    4. RSI(14) between 35–62 (dip zone — raised upper bound from 55)
    5. Volume > 0.8× average

  BEAR_TREND → SHORT:
    1. EMA20 < EMA50 (trend is down)
    2. Price within 1.0% of EMA20 (relief bounce zone)
    3. Last 15m bar closes BELOW EMA20 (not above — downtrend holding)
    4. RSI(14) between 45–68 (bounce zone)
    5. Volume confirmation

SL: Entry ± 1.5 × ATR (always > 0.5% minimum — passes risk_engine gate)
TP: Entry ± 2.5 × ATR
"""

import logging
import pandas as pd
import numpy as np

from modules.strategies.base_strategy import (
    BaseStrategy, compute_atr, compute_adx, get_oi_trend, no_exit, make_exit
)

log = logging.getLogger("StratTP")

# ─── Constants ────────────────────────────────────────────────────────────────
EMA_FAST           = 20
EMA_SLOW           = 50
RSI_PERIOD         = 14
RSI_BULL_LOW       = 35
RSI_BULL_HIGH      = 62          # v3: raised from 55 — genuine bull pullbacks
                                 # regularly show RSI 55-65 at the EMA20 touch.
                                 # The old cap of 55 was blocking the sweet spot.
RSI_BEAR_LOW       = 45
RSI_BEAR_HIGH      = 68
PULLBACK_ZONE      = 0.010       # v2: widened from 0.5% to 1.0%
ATR_PERIOD         = 14
SL_ATR_MULT        = 2.0         # v3: widened from 1.8 — gives more entry room to survive chop.
TP_ATR_MULT        = 3.5         # raised from 2.5 — gives genuine trending moves
                                 # more room before hitting fixed TP. At $10 notional,
                                 # the fee ($0.012) is fixed regardless of TP size,
                                 # so a larger target improves expected value on winners
                                 # without increasing risk (SL unchanged).
TRAIL_ATR_MULT     = 2.0         # v3: tightened from 3.0 — after BE triggers at +1%,
                                 # 3× 1m ATR trail was giving back too much unrealised
                                 # profit before firing. 2.5× locks in more on the exit.
BE_TRIGGER_PCT     = 0.008       # v3: raised from 0.7% — needs more profit before
                                 # moving SL to BE, else BE gets hit on normal retracement
HARD_STOP_PCT      = 0.025       # Original value restored. With lev=2 → 5% leveraged cap.
VOL_MIN_RATIO      = 0.8
MIN_BARS_NEEDED    = 60

# MACD settings (v3: added as confirmation filter)
MACD_FAST          = 12
MACD_SLOW          = 26
MACD_SIGNAL        = 9
# Minimum histogram magnitude — filters near-zero MACD (flat momentum).
# In sustained BULL_TREND, histogram is weakly positive on 80-90% of bars.
# Requiring a meaningful magnitude reduces TP from 84 → ~20-25 trades/session.
# Value is relative to price — 0.0002 = 0.02% of a $1 coin = $0.0002 minimum swing.
# Normalised by price in scan() so this threshold works across all coins.
MACD_MIN_MAGNITUDE = 0.0002

# ADX filter — minimum trend strength to enter a TP trade.
# In volatile crypto, choppy sessions have ADX < 20 even with wide candles.
# ADX < ADX_MIN means price is oscillating, not trending — TP has no edge.
# Lowered from 22 → 18: on ranging/sideways days most altcoins show ADX 12-18.
# At 22, combined with regime age gate + OI filter, zero trades fired all day.
# 18 still blocks the worst chop while allowing moderate trending moves.
# The other 5 filters (EMA, RSI, MACD, pullback zone, volume) remain active.
ADX_MIN            = 18          # 2026-06-03: lowered 20→18. TP has best edge (+18pp)
                               # but only 10 trades. ADX 18-20 zone has valid pullbacks
                               # in moderate trends. Other 5 filters still active.

# OI confirmation — disabled at $10 capital.
# On sideways days OI drifts down as traders close positions regardless of
# direction — this was blocking valid signals that passed all other filters.
# The ADX filter already handles weak momentum. Re-enable at $50+ capital
# once we have enough live trade data to calibrate the OI threshold properly.
OI_BLOCK_ON_FALLING = True


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute MACD line, signal line, and histogram.
    Returns (macd_line, signal_line, histogram).
    MACD line    = EMA12 - EMA26
    Signal line  = EMA9 of MACD line
    Histogram    = MACD line - signal line
    Positive histogram = bullish momentum building
    Negative histogram = bearish momentum building
    """
    ema_fast   = series.ewm(span=MACD_FAST,   adjust=False).mean()
    ema_slow   = series.ewm(span=MACD_SLOW,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal     = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram  = macd_line - signal
    return macd_line, signal, histogram


class TrendPullback(BaseStrategy):

    STRATEGY_ID = "TP"

    def scan(self, symbol, df_1m, df_15m, df_1h, regime):
        regime_name = regime.get("regime", "RANGING")
        if regime_name not in ("BULL_TREND", "BEAR_TREND"):
            return None

        if df_15m is None or len(df_15m) < MIN_BARS_NEEDED:
            return None

        df      = df_15m.copy()
        is_bull = (regime_name == "BULL_TREND")

        # ── Indicators ────────────────────────────────────────────────────────
        df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        df["rsi"]      = _rsi(df["close"], RSI_PERIOD)
        df["vol_avg"]  = df["volume"].rolling(20).mean()

        # MACD (v3)
        _, _, df["macd_hist"] = _macd(df["close"])

        latest   = df.iloc[-1]
        price    = float(latest["close"])
        ema_fast = float(latest["ema_fast"])
        ema_slow = float(latest["ema_slow"])

        # ── 1. EMA trend alignment ────────────────────────────────────────────
        if is_bull and not (ema_fast > ema_slow):
            return None
        if not is_bull and not (ema_fast < ema_slow):
            return None

        # ── 2. Price within pullback zone of EMA20 ────────────────────────────
        dist_from_ema = abs(price - ema_fast) / ema_fast
        if dist_from_ema > PULLBACK_ZONE:
            return None

        # ── 3. 15m close on correct side of EMA (v2: replaces 1m cross) ──────
        # LONG  : price must be >= EMA20 (holding above, not breaking below)
        # SHORT : price must be <= EMA20 (holding below, not bouncing above)
        if is_bull and price < ema_fast * 0.998:
            return None    # Too far below EMA — not a pullback, a breakdown
        if not is_bull and price > ema_fast * 1.002:
            return None    # Too far above EMA — not a bounce, a breakout

        # ── 4. RSI confirmation ───────────────────────────────────────────────
        rsi = float(latest["rsi"])
        if pd.isna(rsi):
            return None
        if is_bull and not (RSI_BULL_LOW <= rsi <= RSI_BULL_HIGH):
            return None
        if not is_bull and not (RSI_BEAR_LOW <= rsi <= RSI_BEAR_HIGH):
            return None

        # ── 5. Volume check ───────────────────────────────────────────────────
        vol_avg = float(latest["vol_avg"])
        if vol_avg <= 0 or latest["volume"] < vol_avg * VOL_MIN_RATIO:
            return None

        # ── 6. MACD confirmation (v3) ─────────────────────────────────────────
        # LONG  : histogram must be positive AND above minimum magnitude
        # SHORT : histogram must be negative AND below minimum magnitude
        # Normalise histogram against price so threshold works across all coins.
        # hist_norm = histogram / price → comparable across $0.001 and $67,000 coins
        hist_now  = float(df["macd_hist"].iloc[-1])
        hist_prev = float(df["macd_hist"].iloc[-2])
        if pd.isna(hist_now) or pd.isna(hist_prev):
            return None

        hist_norm = abs(hist_now) / price if price > 0 else 0.0

        if is_bull:
            # Histogram must be positive AND have meaningful magnitude
            macd_ok = (hist_now > 0
                       and hist_norm >= MACD_MIN_MAGNITUDE
                       and hist_now >= hist_prev)   # still building momentum
        else:
            # Histogram must be negative AND have meaningful magnitude
            macd_ok = (hist_now < 0
                       and hist_norm >= MACD_MIN_MAGNITUDE
                       and hist_now <= hist_prev)   # still building bearish momentum

        if not macd_ok:
            log.debug(
                f"[TP] {symbol} MACD rejected: hist={hist_now:.6f} "
                f"norm={hist_norm:.6f} (min {MACD_MIN_MAGNITUDE})"
            )
            return None

        # ── ATR-based SL and TP (v2: replaces swing-low SL) ──────────────────
        atr = compute_atr(df_15m, ATR_PERIOD)
        if atr <= 0:
            return None

        # ── ATR% volatility gate — reject coins where noise > SL ────────────
        atr_pct = atr / price if price > 0 else 0
        if atr_pct > 0.012:  # 1.2% max — meme/micro-cap filter
            log.debug(f"[TP] {symbol} ATR%={atr_pct*100:.2f}% > 1.2% — too volatile")
            return None

        direction = "LONG" if is_bull else "SHORT"

        if direction == "LONG":
            sl_price = price - SL_ATR_MULT * atr
            tp_price = price + TP_ATR_MULT * atr
        else:
            sl_price = price + SL_ATR_MULT * atr
            tp_price = price - TP_ATR_MULT * atr

        sl_dist = abs(price - sl_price) / price
        if sl_dist < 0.002 or sl_dist > 0.15:
            return None

        # ── 7. ADX filter — confirm trend strength before entry ───────────────
        # All prior checks (EMA, RSI, MACD) confirm direction but not momentum.
        # ADX < ADX_MIN means choppy oscillation regardless of candle size.
        # In volatile sideways sessions (like today's $120 BTC range) this
        # blocks the majority of losing TP entries.
        adx = compute_adx(df_15m, 14)
        if adx > 0 and adx < ADX_MIN:
            log.debug(
                f"[TP] {symbol} ADX={adx:.1f} < {ADX_MIN} — "
                f"trend too weak, skipping"
            )
            return None

        # ── 8. OI confirmation — new money entering, not old money exiting ────
        # Only checked last (after all other filters) to minimise API calls.
        # Fail-open: UNKNOWN and FLAT are allowed through.
        # Only FALLING blocks — means traders are closing, not the setup we want.
        if OI_BLOCK_ON_FALLING:
            oi_trend = get_oi_trend(symbol, direction)
            if oi_trend == "FALLING":
                log.debug(
                    f"[TP] {symbol} OI falling — positions closing, "
                    f"move lacks conviction, skipping"
                )
                return None
        else:
            oi_trend = "SKIP"

        strength = 1.0 - (dist_from_ema / PULLBACK_ZONE)
        strength = max(0.1, min(strength, 1.0))

        log.info(
            f"[TP] {symbol} {direction} | Price: {price:.4f} | "
            f"EMA20: {ema_fast:.4f} ({dist_from_ema*100:.2f}% away) | "
            f"RSI: {rsi:.1f} | ADX: {adx:.1f} | OI: {oi_trend} | "
            f"SL: {sl_price:.4f} ({sl_dist*100:.2f}%) | ATR: {atr:.4f}"
        )

        return {
            "symbol":      symbol,
            "strategy":    self.STRATEGY_ID,
            "direction":   direction,
            "entry_price": price,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "atr":         atr,
            "strength":    strength,
            "reason":      (
                f"Pullback to EMA20 {direction} RSI={rsi:.0f} "
                f"MACD={'✓' if macd_ok else '✗'} "
                f"ADX={adx:.1f} OI={oi_trend} ATR-SL"
            ),
            "ml_features": {
                "adx":              round(adx, 2),
                "rsi":              round(rsi, 2),
                "vol_ratio":        round(float(latest["volume"]) / vol_avg, 3) if vol_avg > 0 else 0,
                "atr_pct":          round(atr / price, 6),
                "sl_dist_pct":      round(sl_dist, 6),
                "dist_from_ema_pct": round(dist_from_ema, 6),
                "macd_hist_norm":   round(hist_norm, 6),
            },
        }

    def manage(self, position, df_1m, session_pnl):
        if df_1m is None or df_1m.empty:
            return no_exit()

        entry     = position["entry_price"]
        direction = position["direction"]
        sl        = position["sl_price"]
        tp        = position.get("tp_price")
        be_active = position.get("be_active", False)

        latest = df_1m.iloc[-1]
        c_high = float(latest["high"])
        c_low  = float(latest["low"])
        c_close= float(latest["close"])

        pnl = (c_close - entry) / entry if direction == "LONG" \
              else (entry - c_close) / entry
        if pnl > position.get("hwm", 0.0):
            position["hwm"] = pnl

        if pnl <= -HARD_STOP_PCT:
            return make_exit(c_close, "HARD_STOP")

        if tp is not None:
            if direction == "LONG" and c_high >= tp:
                return make_exit(tp, "TP_HIT")
            if direction == "SHORT" and c_low <= tp:
                return make_exit(tp, "TP_HIT")

        if not be_active and position.get("hwm", 0.0) >= BE_TRIGGER_PCT:
            position["sl_price"] = entry
            position["be_active"] = True
            sl = entry

        # ── 4. ATR trailing stop — ONLY after BE triggers ──────────────────
        # FIX: Same bug as DB — 1m ATR trail was overriding the 15m-based SL
        # on the very first manage() cycle.  1m ATR is ~3-5× smaller than 15m
        # ATR, so the trail immediately shrank SL width.  Now the full 15m SL
        # is preserved until the trade reaches +1% and BE fires.
        if be_active:
            atr = compute_atr(df_1m, 14)
            # Phase 3 ML override: if ml_engine provided a trail multiplier, use it
            _ml_trail = position.get("ml_trail_mult")
            trail_mult = _ml_trail if _ml_trail is not None else TRAIL_ATR_MULT
            if atr > 0:
                if direction == "LONG":
                    trail = c_high - trail_mult * atr
                    if trail > sl:
                        position["sl_price"] = trail
                        sl = trail
                else:
                    trail = c_low + trail_mult * atr
                    if trail < sl:
                        position["sl_price"] = trail
                        sl = trail

        if direction == "LONG" and c_low <= sl:
            return make_exit(sl, "SL_HIT")
        if direction == "SHORT" and c_high >= sl:
            return make_exit(sl, "SL_HIT")

        return no_exit()





