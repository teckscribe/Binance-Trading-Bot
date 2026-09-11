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
    SL 2.0xATR(15m) (floor 2%) | TP 4.0xATR | max hold 240 min

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

import os
import pandas as pd
from modules.strategies.base_strategy import BaseStrategy


def _f(key, default):
    try: return float(os.getenv(key, str(default)))
    except ValueError: return default


def _b(key, default):
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ── Entry band (|normalised 24h momentum|, in ATR(1h) units) ────────────────
MOM_LO      = _f("CSM_MOM_LO", 3.0)
MOM_HI      = _f("CSM_MOM_HI", 4.0)

# ── Direction ───────────────────────────────────────────────────────────────
ALLOW_LONG  = _b("CSM_ALLOW_LONG",  True)
ALLOW_SHORT = _b("CSM_ALLOW_SHORT", True)

# ── Stops / targets ─────────────────────────────────────────────────────────
SL_ATR_MULT       = _f("CSM_SL_ATR_LONG",  2.0)
TP_ATR_MULT       = _f("CSM_TP_ATR_LONG",  4.0)
SL_ATR_MULT_SHORT = _f("CSM_SL_ATR_SHORT", 2.0)
TP_ATR_MULT_SHORT = _f("CSM_TP_ATR_SHORT", 4.0)

MIN_SL_PCT = _f("CSM_MIN_SL_PCT", 0.02)

# 480 -> 1440. Shorter holds cut the winners that pay for a 54% loss rate;
# median hold is 6.4h but the right tail runs much longer.
# NOTE: a CSM_MAX_HOLD_MIN line in .env OVERRIDES this default.
MAX_HOLD_MIN = int(_f("CSM_MAX_HOLD_MIN", 1440))

# ── Profit ladder — ON for Hybrid Model ────────────────────────────────────
PROFIT_LADDER = [] if os.getenv("CSM_PROFIT_LADDER", "on").lower() == "off" else [
    (0.010, 0.0015),      # Stage 1: +1.0% gain -> lock +0.15% (Risk-free Breakeven + fees)
    (0.025, 0.0150),      # Stage 2: +2.5% gain -> lock +1.50% profit
    (0.040, 0.0250),      # Stage 3: +4.0% gain -> lock +2.50% profit
]
TRAIL_ATR_MULT = 2.0

# Ladder rungs at or above this trigger gain use Peak HWM rather than bar-close,
# because a >=2.0% excursion is a genuine move whose retrace should be locked,
# whereas stage 1 (1.0%) sits inside entry noise and needs bar-close stability.
LADDER_HWM_TRIGGER_THRESHOLD = 0.020

# ── Volume expansion filter ──────────────────────────────────────────────────
# Requires current 1h breakout volume to be >= VOL_RATIO_MIN x 24h average
# hourly volume, filtering out low-liquidity fake-outs on illiquid alts.
VOL_RATIO_MIN = _f("CSM_VOL_RATIO_MIN", 1.0)

# ── Stop management when the ladder is empty ────────────────────────────────
# CSM_PROFIT_LADDER=off used to mean "fall back to the legacy breakeven", and
# there was NO way to express "run the initial SL/TP and nothing else".
#
# That third mode is not hypothetical: it is what the sweep actually measured
# for its highest-expectancy configuration, so without this flag that config
# CANNOT be reproduced by the live engine -- the else-branch would silently add
# a breakeven and a 2xATR trail the measurement never included.
#
# Set CSM_LEGACY_BE=false WITH CSM_PROFIT_LADDER=off to get fixed SL/TP + max
# hold only. To be clear about what that does and does not remove: the entry
# stop and target are untouched and still enforced (by manage(), by the
# universal check in live_scanner, and by the exchange STOP_MARKET in LIVE).
# What is removed is only the RATCHET -- nothing moves the stop after entry.
# That is a deliberate design, not the accidental no-stop state warned about in
# docs/EXPERIMENT_LOG.md 17.5.
LEGACY_BREAKEVEN = _b("CSM_LEGACY_BE", False)

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

        c1h = df_1h['close'].astype(float)

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

        # Volume expansion filter: check if breakout volume confirms the move
        vol_ratio = 1.0
        if 'volume' in df_1h.columns:
            vol_1h = float(df_1h['volume'].iloc[-1])
            avg_vol_24h = float(df_1h['volume'].iloc[-25:-1].mean()) if len(df_1h) >= 25 else 0.0
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

        min_sl_dist = entry_price * MIN_SL_PCT
        sl_mult = SL_ATR_MULT if direction == "LONG" else SL_ATR_MULT_SHORT
        raw_sl_dist = atr_15 * sl_mult

        if direction == "SHORT" and raw_sl_dist < min_sl_dist:
            return None

        sl_dist = max(raw_sl_dist, min_sl_dist)
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
        try:
            _bar = df.iloc[-1]
            _px_hi = float(_bar["high"])
            _px_lo = float(_bar["low"])
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
                eval_gain = gain if trigger_pct < LADDER_HWM_TRIGGER_THRESHOLD else hwm
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

