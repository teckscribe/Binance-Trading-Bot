"""
strategies/cross_sectional_momentum.py
Volatility-normalised momentum. Despite the name it is NOT cross-sectional:
there is no ranking across the universe. It measures one symbol's 24h move in
ATR(1h) units and trades continuation.

Two configurations were selected from a 2,744-configuration sweep on a
LEAK-FREE 90d/100-symbol harness and then CONFIRMED against this class.
ACTIVE DEFAULT = CONFIG A.

    ── HYBRID CONFIG (active) ───────────────────────────────────────────────
    band 3.0-4.0 | LONG + SHORT | RANGING only
    ladder: stage 1 (+1.0% -> +0.15%) on bar close
            stage 2 (+2.5% -> +1.50%) & stage 3 (+4.0% -> +2.50%) on peak HWM
    SL 2.0xATR(15m) (floor 2%) | TP 4.0xATR | max hold 1440 min

    ── CONFIG B (alternative) ───────────────────────────────────────────────
    band 3.0-4.0 | SHORT only | RANGING only | NO stop ratchet
    SL 2.25xATR | TP 4.5xATR | max hold 1440 min
    env: CSM_MOM_LO=3.0 CSM_MOM_HI=4.0 CSM_SL_ATR_SHORT=2.25
         CSM_PROFIT_LADDER=off CSM_LEGACY_BE=false

Four arms, production code, 100 symbols / 90d / leak-free, 0.08% fees +
0.03%/side slippage:

    arm               N     WIN%   R:R    PF   E[net]   after-tax   streak
    BASELINE       2698    50.5%  1.03  1.05  +0.091%    -0.433%       13
    CONFIG_A        1817    66.3%  0.57  1.12  +0.141%    -0.239%       11   <- active
    CONFIG_B         644    46.1%  1.68  1.44  +1.229%    +0.026%       21
    CONFIG_B_trail   691    37.8%  2.26  1.37  +0.739%    -0.078%       12

READ THIS BEFORE CHANGING ANYTHING.

1. CONFIG A IS AFTER-TAX NEGATIVE under India's 115BBH reading (30% on gains,
   NO loss set-off), where the break-even profit factor is 1/0.7 = 1.429 and
   win rate does not enter the equation at all. A wins 66% of its trades and
   still loses money after tax, because R:R 0.57 makes its winners small
   relative to its losers. B is the only one of 2,744 configurations tested
   that clears 1.429. Config B dominates A under BOTH tax readings
   (-0.239% vs +0.026% under 115BBH; +0.141% vs +1.229% under
   speculative-business treatment, where losses CAN be set off).

   A is active because its equity curve is far easier to run live: max losing
   streak 11 vs 21, and a 66% win rate vs 46%. That is a real reason to start
   here. It is not a reason to stay here.

2. THE ORIGINAL 78.4% WIN RATE FOR CONFIG A WAS WRONG. It came from a fast
   simulator that manufactured 863 sub-0.4% ladder locks this class does not
   produce. Verified here, A is 66.3%. The simulator had been validated
   against the BASELINE config -- but not against a config whose exit
   mechanics it then changed. Do not trust a simulator number for this
   strategy that has not been re-confirmed against this class.

3. WIN RATE IS NOT A TARGET. Across 2,744 configurations, every route to a
   high win rate was paid for in R:R at close to the fair rate: at 74.5% WIN
   the break-even R:R is 0.342 and the best config delivered 0.34. Chasing
   win rate is how Config A ended up after-tax negative.

Baseline (pre-sweep) is CSM_MOM_LO=4.0 CSM_MOM_HI=5.0 CSM_ALLOW_LONG=true
CSM_SL_ATR_SHORT=3.0 CSM_TP_ATR_SHORT=6.0 CSM_MAX_HOLD_MIN=480, plus the old
ladder rungs, which are no longer in the file.
"""

import pandas as pd
from modules import settings_manager as cfg
from modules.strategies.base_strategy import BaseStrategy

# All CSM_* tunables are read from settings_manager at CALL time (see the top
# of scan() / _build_signal() / manage()), so a dashboard or bot edit applies
# on the next scan without a restart. Defaults and bounds live in
# settings_manager.SPEC:
#   CSM_MOM_LO / CSM_MOM_HI        entry band, |24h move| in ATR(1h) units
#   CSM_ALLOW_LONG / _SHORT        direction
#   CSM_SL_ATR_* / CSM_TP_ATR_*    stops / targets in ATR(15m) units
#   CSM_MIN_SL_PCT                 floor on the ATR-derived stop
#   CSM_MAX_HOLD_MIN               480 -> 1440: shorter holds cut the winners
#                                  that pay for a 54% loss rate
#   CSM_VOL_RATIO_MIN              last completed 1h volume vs 24h average
#   CSM_PROFIT_LADDER / CSM_LEGACY_BE   stop management, see manage()

# ── Profit ladder — ON for Hybrid Model ────────────────────────────────────
#
# TWO EXPRESSIONS OF THE SAME RUNGS. See EXPERIMENT_LOG 0.5.36.
#
# The absolute rungs below were calibrated when almost every trade sat on the
# 2% MIN_SL floor. The stop is 2xATR(15m) floored at 2%, so on a quiet major
# stage 1 (+1.0%) sits at half the risk distance -- sensible. On a volatile alt
# with ATR/entry 4.2% the stop is 8.4% and stage 1 sits at 12% of the risk
# distance: the trade risks 8.4% to lock 0.15%. Live checks this every second
# against the mark price, so any transient pop arms it; the hourly harness
# mostly never sees the pop. Measured live, CSM is PF 1.73 on floor-stop trades
# and 0.45 on >6% stops, with average WIN flat at ~+1.5% across every bucket
# while average loss tracks the stop.
#
# Expressed as fractions of the stop distance R, the absolute rungs ARE
# 0.5R/1.25R/2.0R with locks 0.075R/0.75R/1.25R -- exactly, at a 2% stop. So
# "atr" mode is not a new calibration: it is the same ladder, anchored to risk
# instead of to price, and identical to "pct" wherever the stop is at the floor.
#
# This mirrors BaseStrategy.breakeven_trigger(), which was ATR-scaled for this
# same reason; the ladder was left absolute.
#
# MEASURED AND REJECTED, 2026-09-26 (log 0.5.37). "atr" mode was built to fix
# the wide-stop collapse and it does NOT: on 84 symbols / 90d it moves R:R
# 0.77 -> 0.81 and avgW +2.16% -> +2.42% exactly as predicted, but PF falls
# 1.01 -> 0.98 and expectancy goes NEGATIVE (+0.0146% -> -0.0324%/trade).
# The wide-stop buckets get worse, not better (4-6%: 1.02 -> 0.90; >6%:
# 0.81 -> 0.70), because the early fixed lock was acting as PROTECTION there
# and removing it lets those trades run back to the full stop. What actually
# works is not taking the wide-stop trades at all -- CSM_MAX_SL_PCT.
# Kept, default "pct", so the measurement is reproducible. Do not enable
# without re-measuring.
_LADDER_RUNGS = [
    (0.010, 0.0015),      # Stage 1: +1.0% gain -> lock +0.15% (Risk-free Breakeven + fees)
    (0.025, 0.0150),      # Stage 2: +2.5% gain -> lock +1.50% profit
    (0.040, 0.0250),      # Stage 3: +4.0% gain -> lock +2.50% profit
]

# (trigger, lock) as multiples of the stop distance R. Equivalent to the rungs
# above at a 2% stop; scales with risk everywhere else.
_LADDER_RUNGS_R = [
    (0.50, 0.075),
    (1.25, 0.750),
    (2.00, 1.250),
]
TRAIL_ATR_MULT = 2.0

# Ladder rungs at or above this trigger gain use Peak HWM rather than bar-close,
# because a >=2.0% excursion is a genuine move whose retrace should be locked,
# whereas stage 1 (1.0%) sits inside entry noise and needs bar-close stability.
LADDER_HWM_TRIGGER_THRESHOLD = 0.020

# Breakeven trigger used ONLY by the legacy path (CSM_PROFIT_LADDER=off).
# ATR-scaled via BaseStrategy.breakeven_trigger(), floored at this percentage.
BE_TRIGGER = 0.015

class CrossSectionalMomentum(BaseStrategy):
    STRATEGY_ID = "CSM"

    # Momentum is measured over 24 hourly bars — without df_1h this strategy
    # returns None on its first line and can never produce a signal.
    REQUIRES_1H = True

    def scan(self, symbol: str, df_1m: pd.DataFrame, df_15m: pd.DataFrame, df_1h: pd.DataFrame, regime: dict) -> dict:
        if df_1h is None or len(df_1h) < 25 or df_15m is None or len(df_15m) < 20:
            return None

        MOM_LO, MOM_HI = cfg.get("CSM_MOM_LO"), cfg.get("CSM_MOM_HI")
        ALLOW_LONG, ALLOW_SHORT = cfg.get("CSM_ALLOW_LONG"), cfg.get("CSM_ALLOW_SHORT")
        VOL_RATIO_MIN = cfg.get("CSM_VOL_RATIO_MIN")

        c1h = df_1h['close'].astype(float)

        # CSM_CLOSED_BAR_MOM: measure momentum on CLOSED hourly bars only.
        #
        # Default (false) keeps the shipped behaviour: the reading includes the
        # FORMING hourly bar, which live moves every minute. Measured over
        # 14,640 intra-hour readings (0.5.28): median drift 0.208 ATR against a
        # 1.0-ATR-wide entry band, and 28 % of mid-hour signals are gone by the
        # hour's close. The harness only ever sees closed bars, so live trades a
        # population the backtest never evaluated.
        #
        # true drops the forming bar, so live and backtest measure the same
        # thing. Cost: entries lag by up to 59 min.
        if cfg.get("CSM_CLOSED_BAR_MOM") and len(df_1h) >= 26:
            c1h = c1h.iloc[:-1]
            df_1h = df_1h.iloc[:-1]

        # Calculate 24h price change
        price_change = c1h.iloc[-1] - c1h.iloc[-25]

        # Calculate ATR on 1h
        tr1 = df_1h['high'] - df_1h['low']
        tr2 = (df_1h['high'] - df_1h['close'].shift()).abs()
        tr3 = (df_1h['low'] - df_1h['close'].shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_1h = float(tr.rolling(14).mean().iloc[-1])

        # Normalized momentum (how many ATRs did it move in 24h)
        normalized_mom = price_change / atr_1h if atr_1h > 0 else 0

        # Volume expansion filter: use the last COMPLETED 1h candle (iloc[-2])
        # to avoid comparing an in-progress candle against full-hour averages,
        # which would block entries for the first 30-45 min of every hour.
        vol_ratio = 1.0
        if 'volume' in df_1h.columns and len(df_1h) >= 26:
            vol_1h = float(df_1h['volume'].iloc[-2])           # last completed candle
            avg_vol_24h = float(df_1h['volume'].iloc[-26:-2].mean())   # 24 completed candles before it
            if avg_vol_24h > 0:
                vol_ratio = vol_1h / avg_vol_24h
            if VOL_RATIO_MIN > 0 and avg_vol_24h > 0 and vol_1h < (VOL_RATIO_MIN * avg_vol_24h):
                return None

        # Sweet spot: 3-4x ATR in 24h. Below 3 is noise; above 4 tends to
        # reverse (pump-and-dump).
        if ALLOW_LONG and MOM_LO <= normalized_mom < MOM_HI:
            return self._build_signal(symbol, df_15m, "LONG", normalized_mom, vol_ratio)

        if ALLOW_SHORT and -MOM_HI < normalized_mom <= -MOM_LO:
            return self._build_signal(symbol, df_15m, "SHORT", normalized_mom, vol_ratio)

        # Near-miss: approaching the threshold in a direction we actually trade.
        abs_mom = abs(normalized_mom)
        _tradeable = (normalized_mom > 0 and ALLOW_LONG) or \
                     (normalized_mom < 0 and ALLOW_SHORT)
        if _tradeable and (MOM_LO * 0.70) <= abs_mom < MOM_LO:
            _dirs = "LONG " if normalized_mom > 0 else "SHORT "
            return {
                "near_miss": True,
                "strategy": self.STRATEGY_ID,
                "symbol": symbol,
                "closeness": abs_mom / MOM_LO,
                "detail": f"momentum={normalized_mom:+.2f}x ATR "
                          f"(needs {_dirs}{MOM_LO:.1f}-{MOM_HI:.1f})",
            }
        return None

    def _build_signal(self, symbol, df_15m, direction, normalized_mom, vol_ratio=1.0):
        c15 = df_15m['close'].astype(float)
        entry_price = float(c15.iloc[-1])

        # Calculate ATR for SL/TP on 15m
        tr1_15 = df_15m['high'] - df_15m['low']
        tr2_15 = (df_15m['high'] - df_15m['close'].shift()).abs()
        tr3_15 = (df_15m['low'] - df_15m['close'].shift()).abs()
        tr_15 = pd.concat([tr1_15, tr2_15, tr3_15], axis=1).max(axis=1)
        atr_15 = float(tr_15.rolling(14).mean().iloc[-1])

        MIN_SL_PCT = cfg.get("CSM_MIN_SL_PCT")
        SL_ATR_MULT, SL_ATR_MULT_SHORT = cfg.get("CSM_SL_ATR_LONG"), cfg.get("CSM_SL_ATR_SHORT")
        TP_ATR_MULT, TP_ATR_MULT_SHORT = cfg.get("CSM_TP_ATR_LONG"), cfg.get("CSM_TP_ATR_SHORT")

        min_sl_dist = entry_price * MIN_SL_PCT
        sl_mult = SL_ATR_MULT if direction == "LONG" else SL_ATR_MULT_SHORT
        raw_sl_dist = atr_15 * sl_mult

        if direction == "SHORT" and raw_sl_dist < min_sl_dist:
            return None

        sl_dist = max(raw_sl_dist, min_sl_dist)

        # CSM_MAX_SL_PCT: reject signals whose stop is wider than this fraction
        # of entry. 0 = off. Live CSM is PF 1.73 on stops <=2.5% and 0.45 on
        # stops >6% (0.5.36); this confines it to the region the backtest
        # actually measured, at the cost of most of its signal volume.
        _max_sl = cfg.get("CSM_MAX_SL_PCT")
        if _max_sl and sl_dist / entry_price > _max_sl:
            return None
        scale = sl_dist / raw_sl_dist if raw_sl_dist > 0 else 1.0
        effective_atr = atr_15 * scale
        tp_mult = TP_ATR_MULT if direction == "LONG" else TP_ATR_MULT_SHORT
        tp_dist = effective_atr * tp_mult

        if direction == "LONG":
            sl = entry_price - sl_dist
            tp = entry_price + tp_dist
        else:
            sl = entry_price + sl_dist
            tp = entry_price - tp_dist

        # Dynamic strength: scaled by volume expansion factor (0.5 to 2.5),
        # prioritizing highest-conviction volume breakouts on simultaneous signals.
        dynamic_strength = min(2.5, max(0.5, round(float(vol_ratio), 3)))

        return {
            "symbol": symbol,
            "strategy": self.STRATEGY_ID,
            "direction": direction,
            "entry_price": entry_price,
            "sl_price": sl,
            "tp_price": tp,
            "atr": effective_atr,
            "qty": 0.0,
            "normalized_mom": float(normalized_mom),
            "vol_ratio": float(vol_ratio),
            "strength": dynamic_strength,
            "reason": f"CSM (vol_ratio={vol_ratio:.2f}x, mom={normalized_mom:+.2f}x)",
        }

    def manage(self, position: dict, df_1m: pd.DataFrame, session_pnl: float) -> dict:
        df = df_1m
        if df is None or df.empty:
            return {"exit": False, "exit_price": 0.0, "exit_reason": ""}

        current_price = float(df['close'].iloc[-1])
        entry_price = float(position['entry_price'])
        direction = position.get("direction", "LONG")

        MAX_HOLD_MIN = cfg.get("CSM_MAX_HOLD_MIN")
        PROFIT_LADDER = _LADDER_RUNGS if cfg.get("CSM_PROFIT_LADDER") == "on" else []
        # "atr": rungs are multiples of this position's own stop distance R,
        # measured from the INITIAL stop so the ladder cannot chase its own
        # ratchet. Falls back to the absolute rungs when R is unavailable.
        _R = 0.0
        if PROFIT_LADDER and cfg.get("CSM_LADDER_MODE") == "atr":
            try:
                _isl = float(position.get("initial_sl_price") or 0.0)
                if _isl > 0 and entry_price > 0:
                    _R = abs(entry_price - _isl) / entry_price
            except (TypeError, ValueError):
                _R = 0.0
            if _R > 0:
                PROFIT_LADDER = [(t * _R, k * _R) for t, k in _LADDER_RUNGS_R]
        LEGACY_BREAKEVEN = cfg.get("CSM_LEGACY_BE")

        # --- Max hold time: close flat trades after MAX_HOLD_MIN to free slots ---
        #
        # pd.to_datetime, NOT datetime.fromisoformat. Live stores entry_time as
        # an ISO string; the backtest harness stores a pd.Timestamp
        # (backtest_optimizer.py:594). fromisoformat() raises TypeError on a
        # Timestamp, the bare except below swallowed it, duration_min stayed 0
        # — so this max-hold has NEVER fired in a backtest. pd.to_datetime
        # accepts both, so the two paths now agree.
        try:
            entry_t = pd.to_datetime(position["entry_time"], utc=True)
            bar_t = pd.to_datetime(
                df["timestamp"].iloc[-1] if "timestamp" in df.columns
                else df.index[-1],
                utc=True,
            )
            duration_min = (bar_t - entry_t).total_seconds() / 60
        except Exception:
            duration_min = 0
        if duration_min >= MAX_HOLD_MIN:
            return {"exit": True, "exit_price": current_price, "exit_reason": "MAX_HOLD"}

        # ── High-water mark tracking (Peak Favourable Excursion) ────────────
        # df.iloc[-1] contains the live mark/tick price on fast cycles.
        # Track the peak favourable move since entry.
        # GUARD: On the entry candle, pre-fill extremes could leak into HWM
        # and trigger phantom stop-outs.  Clamp to current_price if the bar
        # overlaps with entry_time.
        try:
            _bar = df.iloc[-1]
            _px_hi = float(_bar["high"])
            _px_lo = float(_bar["low"])
            # Check if this bar overlaps with entry time (entry candle guard)
            _bar_ts = _bar.get("timestamp", _bar.name if hasattr(_bar, "name") else None)
            if _bar_ts is not None:
                try:
                    _bar_ts = pd.to_datetime(_bar_ts, utc=True)
                    _entry_ts = pd.to_datetime(position["entry_time"], utc=True)
                    # If the bar timestamp <= entry_time, this is the entry candle
                    # — clamp extremes to current_price to avoid pre-fill leakage
                    if _bar_ts <= _entry_ts:
                        _px_hi = max(current_price, entry_price)
                        _px_lo = min(current_price, entry_price)
                except Exception:
                    pass
        except Exception:
            _px_hi = _px_lo = current_price

        _peak_gain = ((_px_hi - entry_price) / entry_price) if direction == "LONG" \
                     else ((entry_price - _px_lo) / entry_price)

        try:
            _prev_hwm = float(position.get("hwm", 0.0) or 0.0)
        except (TypeError, ValueError):
            _prev_hwm = 0.0

        hwm = max(_prev_hwm, _peak_gain)
        position["hwm"] = hwm

        # --- Profit ladder + ATR trailing stop ---
        #
        # Stage 1 (Breakeven Shield at +1.0% -> +0.15% lock) is evaluated on a
        # COMPLETED 15m bar close (`gain`) to prevent 1-second tick noise whipsaw
        # on entry. Stages 2 & 3 (+2.5% and +4.0% locks) are evaluated on Peak HWM
        # (`hwm`) to instantly lock substantial gains on intra-bar spikes.
        trail_px = self.trail_reference_price(df, position)
        if trail_px is None:
            trail_px = current_price

        gain = ((trail_px - entry_price) / entry_price) if direction == "LONG" \
               else ((entry_price - trail_px) / entry_price)

        if PROFIT_LADDER:
            # Ratchet the stop up through the ladder. Rungs are cumulative and
            # the stop only ever moves in the favourable direction.
            for trigger_pct, lock_pct in PROFIT_LADDER:
                # Stage 1's lock (0.85% below trigger) is inside entry noise — a tick-wick
                # at +1.0% would lock +0.15% and get stopped out on normal jitter. Stage 2/3
                # have wider lock gaps (1.0% / 1.5%) that tolerate normal retrace after a
                # peak, so they can safely fire on HWM.
                _hwm_thr = (LADDER_HWM_TRIGGER_THRESHOLD / 0.02) * _R if _R > 0                            else LADDER_HWM_TRIGGER_THRESHOLD
                eval_gain = gain if trigger_pct < _hwm_thr else hwm
                if eval_gain >= trigger_pct:
                    locked = entry_price * (1 + lock_pct) if direction == "LONG" \
                             else entry_price * (1 - lock_pct)
                    position["be_hit"] = True
                    position["sl_price"] = (
                        max(position["sl_price"], locked) if direction == "LONG"
                        else min(position["sl_price"], locked)
                    )
        elif LEGACY_BREAKEVEN:
            # LEGACY path, active when CSM_PROFIT_LADDER=off. This is the exact
            # pre-2026-08-20 behaviour: arm a breakeven stop at
            # entry +/- max(1xATR, 1.5%), parking the stop just past entry.
            #
            # It MUST live here rather than being implied by an empty ladder:
            # `be_hit` is what enables the ATR trail below, and only the ladder
            # sets it. Without this branch, switching the ladder off would leave
            # a position with no breakeven AND no trail — worse than either
            # design, and not the revert it looks like.
            breakeven_price = self.breakeven_trigger(position, BE_TRIGGER)
            armed = (trail_px > breakeven_price) if direction == "LONG" \
                    else (trail_px < breakeven_price)
            if armed and not position.get("be_hit", False):
                position["be_hit"] = True
                position["sl_price"] = (
                    max(position["sl_price"], entry_price * 1.001) if direction == "LONG"
                    else min(position["sl_price"], entry_price * 0.999)
                )

        # ATR trail on top of the ladder — takes over once the trade has run far
        # enough that the trail is tighter than the highest rung cleared.
        if position.get("be_hit", False):
            # `or`, not a .get() default: the default only applies when the key
            # is ABSENT, and _build_signal() can legitimately write atr=0.0 when
            # atr_15 computes to zero on a flat symbol. A zero ATR made the
            # trail land exactly on the current price, stopping the trade out on
            # the next tick the moment the ladder armed.
            atr = position.get("atr") or (trail_px * 0.01)
            trail_price = (trail_px - TRAIL_ATR_MULT * atr) if direction == "LONG" \
                          else (trail_px + TRAIL_ATR_MULT * atr)
            if direction == "LONG" and trail_price > position["sl_price"]:
                position["sl_price"] = trail_price
            elif direction == "SHORT" and trail_price < position["sl_price"]:
                position["sl_price"] = trail_price

        if direction == "LONG":
            if current_price >= position['tp_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": "TP_HIT"}
            elif current_price <= position['sl_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": self.stop_exit_reason(position)}
        else:
            if current_price <= position['tp_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": "TP_HIT"}
            elif current_price >= position['sl_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": self.stop_exit_reason(position)}

        return {"exit": False, "exit_price": 0.0, "exit_reason": ""}


