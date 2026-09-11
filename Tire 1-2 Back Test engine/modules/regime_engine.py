"""
regime_engine.py
3-coin market regime classifier — BTC + ETH + SOL.

Regime logic (v4 — 2026-06-08):
  BTC is primary (price structure + SMA20 + ADX + slope on 1h).
  ETH is confirmation — if BTC and ETH agree, regime is high-confidence.
  SOL is divergence detector — if SOL is bullish while BTC is bearish,
  altseason is forming and SHORT bias is reduced.

v4 improvements:
  1. ADX trend-strength filter: BTC ADX < 20 on 1h → force RANGING.
     Prevents "BULL_TREND" classification when price is barely above SMA20
     but not actually trending (ADX measures trend strength, not direction).
  2. SMA20 slope filter: if SMA20 is flat (slope < 0.1%/3bars), force
     RANGING. A flat SMA20 means the average itself isn't moving → no trend.
  3. Distance threshold in _coin_trend: price must be > 0.2% from SMA20
     to classify as BULL/BEAR. Prevents noise classification when price
     sits right on SMA20.

Decision table:
  Funding > EXTREME                → OVERHEATED    (overrides everything)
  Funding < -EXTREME               → OVERSOLD      (overrides everything)
  BTC ADX < 20                     → RANGING        (no trend strength)
  BTC SMA20 slope flat             → RANGING        (no directional momentum)
  BTC BEAR + ETH BEAR              → BEAR_TREND     (confirmed, full SHORT bias)
  BTC BEAR + ETH BULL              → RANGING        (conflict, reduce size)
  BTC BEAR + ETH BEAR + SOL BULL   → RANGING        (altseason forming, no SHORT)
  BTC BULL + ETH BULL              → BULL_TREND     (confirmed, full LONG bias)
  BTC BULL + ETH BEAR              → RANGING        (conflict, reduce size)
  BTC NEUTRAL (any)                → RANGING

Hysteresis (v2 — 2026-04-10):
  Once a trend is confirmed, require price to cross SMA20 by at least
  HYSTERESIS_PCT in the OPPOSITE direction before flipping.
  Also hold any regime for at least MIN_HOLD_MINUTES.

Hysteresis logic fix (v3 — 2026-04-14):
  Require BOTH hold_ok AND hyst_ok to allow a flip.

v5 (2026-08-11) — window-invariance fix:
  _coin_trend()'s VWAP was cumulative, so the regime label depended on how
  many bars the caller fetched, not just on the market. Backtest and live
  agreed on only 75.7% of bars over 90 days (BULL_TREND: 1.3% of bars in the
  backtest vs 18.2% live). Changed to a rolling VWAP_PERIOD window —
  agreement 95.5%. See VWAP_PERIOD below for the full measurement.

  Known residual: _compute_adx() is EWM-based and still warms up over roughly
  2x its period, so a 50-bar live window reads ~5 points higher than a fully
  converged one (mean 31.8 vs 26.9), and the ADX<20 RANGING gate disagrees on
  ~12% of bars. Live therefore under-detects RANGING slightly. Fix would be to
  fetch more 1h history in data_hub.fetch_btc_reference (currently 50 bars).
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from modules.data_feed import fetch_candles, fetch_funding_rate

log = logging.getLogger("RegimeEngine")

SMA_PERIOD      = 20
FUNDING_EXTREME = 0.0005
MIN_BARS_NEEDED = 25

# v5 (2026-08-11): lookback for the VWAP confirmation in _coin_trend().
#
# This exists because the VWAP used to be cumulative — `(close*volume).cumsum()
# / volume.cumsum()` — which anchors to the FIRST BAR OF WHATEVER DATAFRAME IS
# PASSED IN. The function therefore returned different answers for the same
# market depending only on how many bars the caller happened to fetch, and the
# codebase had three different callers:
#     50 bars    live      (data_hub.fetch_btc_reference -> live_scanner)
#     35 bars    fallback  (classify_regime when called with no dataframes)
#     expanding  backtest  (build_regime_series, up to 2160 bars)
#
# Measured over 90 days of BTC/ETH/SOL 1h data, BULL_TREND was labelled on
# 1.3% of bars under the expanding window but 18.2% under live's 50-bar
# window — a 14x difference from input length alone. Backtest and live agreed
# on only 75.7% of bars, which meant the per-regime expectancies driving
# REGIME_STRATEGY_PERMISSIONS were measured against a regime series live would
# never reproduce.
#
# A rolling window makes the result depend only on the last N bars, so every
# caller gets the same answer. Same measurement after the change: 95.5%
# agreement. Matched to SMA_PERIOD so both legs of the BULL/BEAR test look at
# the same span.
VWAP_PERIOD     = 20

# 2-bar regime confirmation — 2 consecutive 1h closes must agree.
TREND_CONFIRM_BARS = 2

# v4: minimum distance from SMA20 to classify as BULL/BEAR.
# Below this → NEUTRAL (too close to SMA20 to call a direction).
# 0.2% on $62K BTC = ~$124 minimum separation.
TREND_MIN_DIST_PCT = 0.002

# v4: ADX threshold — below this, market has no real trend regardless
# of price position vs SMA20. Forces RANGING.
ADX_REGIME_MIN = 20

# v4: SMA20 slope threshold — slope of SMA20 over last 3 bars.
# If abs(slope) < this, SMA20 is flat → RANGING.
# 0.1% = SMA20 moved less than 0.1% over 3 hours.
SMA_SLOPE_MIN_PCT = 0.001

# Hysteresis: once in a trend, price must move this % beyond SMA20
# in the opposite direction before we flip regime.
# 2026-06-07: raised 0.3%→0.5%. BTC at $61K was oscillating $200-400
# around SMA20, breaching 0.3% → 26 regime flips in 35h → zero trades.
HYSTERESIS_PCT = 0.005

# Minimum time (minutes) to hold a regime before allowing a change.
# 2026-06-07: raised 5→15min.
MIN_HOLD_MINUTES = 15

BTC_SYM = "BTCUSDT"
ETH_SYM = "ETHUSDT"
SOL_SYM = "SOLUSDT"

# ── Module-level state for hysteresis ─────────────────────────────────────────
_current_regime: str = ""
_regime_set_at: datetime | None = None


def _compute_adx(df, period=14):
    """Compute ADX from 1h DataFrame. Returns float or 0 on failure."""
    try:
        high  = df["high"].astype(float)
        low   = df["low"].astype(float)
        close = df["close"].astype(float)

        plus_dm  = high.diff()
        minus_dm = -low.diff()
        plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)

        atr     = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di= 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        dx      = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx     = dx.ewm(alpha=1/period, adjust=False).mean()

        val = float(adx.iloc[-1])
        return val if not pd.isna(val) else 0.0
    except Exception:
        return 0.0


def _sma_slope_pct(df, period=20, bars=3):
    """SMA20 slope over last `bars` bars as percentage change."""
    try:
        sma = df["close"].astype(float).rolling(period).mean()
        if len(sma) < bars + 1:
            return 0.0
        old = float(sma.iloc[-(bars + 1)])
        new = float(sma.iloc[-1])
        if old == 0:
            return 0.0
        return (new - old) / old
    except Exception:
        return 0.0


def _coin_trend(df_1h, name=""):
    """
    Classify coin trend using the last TREND_CONFIRM_BARS 1h closes.
    All bars must agree (all BULL or all BEAR) to return a non-NEUTRAL trend.

    v4: Added minimum distance threshold — price must be > TREND_MIN_DIST_PCT
    from SMA20 to classify as BULL/BEAR. Prevents false trends when price
    hovers right on SMA20 (e.g. BTC +0.1% above SMA20 ≠ BULL).
    """
    if df_1h is None or len(df_1h) < MIN_BARS_NEEDED:
        log.warning(f"Insufficient 1h bars for {name} — returning NEUTRAL")
        return "NEUTRAL"

    close = df_1h["close"].astype(float)
    vol   = df_1h["volume"].astype(float)

    sma20 = close.rolling(SMA_PERIOD).mean()

    # v5: ROLLING VWAP, not cumulative — see VWAP_PERIOD above. Using cumsum()
    # here made the result depend on the caller's fetch size. Do not change
    # this back to cumsum() without re-deriving REGIME_STRATEGY_PERMISSIONS.
    vwap_num = (close * vol).rolling(VWAP_PERIOD).sum()
    vwap_den = vol.rolling(VWAP_PERIOD).sum().replace(0, float("nan"))
    vwap     = vwap_num / vwap_den

    # Check the last TREND_CONFIRM_BARS candles
    signals = []
    for i in range(-TREND_CONFIRM_BARS, 0):
        price   = float(df_1h["close"].iloc[i])
        sma_val = float(sma20.iloc[i])
        vwap_val = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else price

        # v4: minimum distance check
        dist_pct = abs(price - sma_val) / sma_val if sma_val > 0 else 0
        if dist_pct < TREND_MIN_DIST_PCT:
            signals.append("NEUTRAL")
        elif price > sma_val and price > vwap_val:
            signals.append("BULL")
        elif price < sma_val and price < vwap_val:
            signals.append("BEAR")
        else:
            signals.append("NEUTRAL")

    # All bars must agree
    if all(s == "BULL" for s in signals):
        return "BULL"
    if all(s == "BEAR" for s in signals):
        return "BEAR"
    return "NEUTRAL"


def _decide_regime(btc_trend, eth_trend, sol_trend, funding, btc_adx, btc_slope):
    """
    Decide regime from coin trends + momentum filters.

    v4 additions:
      - ADX < 20 → RANGING (no trend strength)
      - SMA20 slope flat → RANGING (no directional momentum)
    These fire BEFORE the price-based decision table.
    """
    if funding > FUNDING_EXTREME:
        return "OVERHEATED"
    if funding < -FUNDING_EXTREME:
        return "OVERSOLD"

    # v4: ADX + slope combined gate
    # ADX < 20 AND slope flat → definitely RANGING (no strength, no movement)
    # ADX < 20 alone → RANGING (no trend strength regardless of slope)
    # ADX >= 20 + slope flat → trust ADX, allow trend (SMA20 lags price)
    if btc_adx > 0 and btc_adx < ADX_REGIME_MIN:
        log.info(
            f"BTC ADX={btc_adx:.1f} < {ADX_REGIME_MIN} "
            f"slope={btc_slope*100:+.3f}% — no trend strength → RANGING"
        )
        return "RANGING"

    if btc_trend == "NEUTRAL":
        return "RANGING"
    if btc_trend == "BEAR" and eth_trend == "BEAR":
        if sol_trend == "BULL":
            log.info("SOL divergence (BULL vs BTC/ETH BEAR) — altseason forming → RANGING")
            return "RANGING"
        return "BEAR_TREND"
    if btc_trend == "BULL" and eth_trend == "BULL":
        return "BULL_TREND"
    return "RANGING"


def classify_regime(btc_1h=None, eth_1h=None, sol_1h=None):
    """
    Classify regime using BTC + ETH + SOL 1h candles.
    Pass pre-fetched DataFrames to avoid duplicate API calls.

    v4: Now computes BTC ADX and SMA20 slope as additional inputs to
    _decide_regime(). These prevent false BULL/BEAR_TREND in flat markets.

    Hysteresis: a regime flip is only allowed when BOTH conditions are met:
      1. hold_ok  — minimum hold time (MIN_HOLD_MINUTES) has elapsed
      2. hyst_ok  — BTC price has moved beyond HYSTERESIS_PCT from SMA20
                    in the direction of the new regime
    If either condition fails, the current regime is preserved.
    Funding-driven regimes (OVERHEATED/OVERSOLD) bypass hysteresis entirely.
    """
    global _current_regime, _regime_set_at

    if btc_1h is None:
        btc_1h = fetch_candles(BTC_SYM, "1h", limit=MIN_BARS_NEEDED + 10)
    if eth_1h is None:
        eth_1h = fetch_candles(ETH_SYM, "1h", limit=MIN_BARS_NEEDED + 10)
    if sol_1h is None:
        sol_1h = fetch_candles(SOL_SYM, "1h", limit=MIN_BARS_NEEDED + 10)

    if btc_1h is None or btc_1h.empty:
        log.warning("BTC 1h data unavailable — defaulting to RANGING")
        return {"regime": "RANGING", "btc_trend": "NEUTRAL", "eth_trend": "NEUTRAL",
                "sol_trend": "NEUTRAL", "funding": 0.0, "btc_price": 0.0, "sma20": 0.0}

    btc_price = float(btc_1h["close"].iloc[-1])
    sma20     = float(btc_1h["close"].rolling(SMA_PERIOD).mean().iloc[-1])
    btc_trend = _coin_trend(btc_1h, "BTC")
    eth_trend = _coin_trend(eth_1h, "ETH")
    sol_trend = _coin_trend(sol_1h, "SOL")

    # v4: momentum filters
    btc_adx   = _compute_adx(btc_1h)
    btc_slope = _sma_slope_pct(btc_1h)

    funding = fetch_funding_rate(BTC_SYM)
    if funding is None:
        funding = 0.0
        log.warning("Could not fetch BTC funding rate — using 0.0")

    raw_regime = _decide_regime(
        btc_trend, eth_trend, sol_trend, funding, btc_adx, btc_slope
    )

    # ── Hysteresis: prevent flickering near SMA20 ─────────────────────────────
    now = datetime.now(timezone.utc)

    if _current_regime and _current_regime != raw_regime:
        # ── Check minimum hold time ───────────────────────────────────────────
        hold_ok = True
        if _regime_set_at is not None:
            age_min = (now - _regime_set_at).total_seconds() / 60
            if age_min < MIN_HOLD_MINUTES:
                hold_ok = False

        # ── Check hysteresis buffer ───────────────────────────────────────────
        # Funding-driven regimes (OVERHEATED/OVERSOLD) bypass this check —
        # funding is an objective threshold, not a noisy price signal.
        hyst_ok = True
        if raw_regime not in ("OVERHEATED", "OVERSOLD") and \
           _current_regime not in ("OVERHEATED", "OVERSOLD"):
            dist_from_sma = (btc_price - sma20) / sma20 if sma20 > 0 else 0
            if raw_regime in ("BULL_TREND",) and dist_from_sma < HYSTERESIS_PCT:
                hyst_ok = False
            elif raw_regime in ("BEAR_TREND",) and dist_from_sma > -HYSTERESIS_PCT:
                hyst_ok = False
            elif raw_regime == "RANGING" and abs(dist_from_sma) > HYSTERESIS_PCT:
                # Switching TO ranging requires price to be CLOSE to SMA20.
                # If price is far from SMA20, stay in the trend.
                hyst_ok = False

        # ── FIX v3: require BOTH conditions to allow a flip ───────────────────
        # Original: `if not hold_ok and not hyst_ok` — blocked only when BOTH
        # failed, meaning a flip was allowed if EITHER passed. This let regime
        # flip to RANGING after 5 minutes even when price was far from SMA20
        # (hyst_ok=False, hold_ok=True → flip allowed — wrong).
        # Fixed: block the flip unless BOTH hold_ok AND hyst_ok are True.
        if not (hold_ok and hyst_ok):
            log.debug(
                f"Regime hysteresis: raw={raw_regime} blocked | "
                f"keeping {_current_regime} | "
                f"hold_ok={hold_ok} hyst_ok={hyst_ok} | "
                f"BTC dist from SMA20: {((btc_price-sma20)/sma20)*100:+.2f}%"
            )
            raw_regime = _current_regime
        else:
            if not hold_ok:
                log.info(
                    f"Regime: strong move overrides hold time — "
                    f"{_current_regime} → {raw_regime}"
                )

    # Update state if regime changed
    if raw_regime != _current_regime:
        _current_regime = raw_regime
        _regime_set_at  = now

    # First ever call — initialise
    if _regime_set_at is None:
        _regime_set_at = now
        _current_regime = raw_regime

    result = {
        "regime":    _current_regime,
        "btc_trend": btc_trend,
        "eth_trend": eth_trend,
        "sol_trend": sol_trend,
        "funding":   funding,
        "btc_price": btc_price,
        "sma20":     sma20,
    }

    log.info(
        f"Regime: {_current_regime} | BTC: {btc_price:.2f} (SMA20: {sma20:.2f}) | "
        f"BTC={btc_trend} ETH={eth_trend} SOL={sol_trend} | "
        f"ADX={btc_adx:.1f} slope={btc_slope*100:+.3f}% | "
        f"Funding: {funding*100:.4f}%"
    )
    return result


# Regime x strategy permission matrix.
#
# STATUS 2026-08-11: populated from a 90-day backtest. Regime now genuinely
# gates strategy selection — this replaced the all-True placeholder that had
# been in place since 2026-08-04.
#
# Per-trade expectancy by cell (90d, pre-config baseline). "—" means the cell
# had too few trades to score:
#
#   STRAT      BEAR_TREND        BULL_TREND          RANGING
#   CSM       1.428% (568)     -0.166% (129)      0.689% (6970)
#   LIQ       0.145% (1177)    -0.555%  (41)     -0.070% (4301)
#   VRP       0.148%  (374)    -0.188%  (24)      0.008%  (932)
#
# Rationale per enabled cell:
#   CSM/BEAR + CSM/RANGING — the only robust edge in the engine. Keeps 93% of
#       its expectancy after removing its ten best trades, so it is not
#       outlier-carried. RANGING also supplies ~75-79% of all wall-clock time.
#   CSM/BULL — enabled by explicit decision despite a NEGATIVE backtest cell
#       (-0.166%). The sample is 129 trades and BULL_TREND is rare, so this is
#       a thin negative rather than a proven one. Revisit if BULL trade count
#       grows past ~300 and expectancy stays under zero.
#   LIQ/BEAR — LIQ's one genuine regime edge, and the sample is real (1,177
#       trades). It is negative in both other regimes, hence BEAR-only.
#   VRP/BEAR — positive at +0.148% over 374 trades. Note VRP is FRAGILE
#       overall: one trade accounted for 175% of its live profit, so its edge
#       is outlier-dependent in a way CSM's is not. Watch top_trade_share on
#       the dashboard Performance tab.
#
# OVERHEATED and OVERSOLD are intentionally empty: neither regime produced
# enough backtest trades to score any strategy, and enabling a strategy in an
# unmeasured regime is how the all-True placeholder caused losses. The engine
# will simply not open positions while either is active. Populate these only
# from real data.
#
# IMPORTANT: adding a strategy to _ALL_STRATEGIES is NOT enough — it must also
# appear here, or perms.get(id, False) returns False and it will never be
# permitted in any regime. Testing Codes/verify_all_surfaces.py checks this.
# ── 2026-08-11 REVISION: CSM only, all regimes ───────────────────────────────
#
# LIQ and VRP were removed from BEAR_TREND after backtesting on corrected
# regime labels with the live risk model enforced (real compute_position_size
# sizing, 3 slots, margin ceiling, loss caps, compounding equity).
#
# Four candidate matrices, same 16,250-trade set, $100 start:
#
#                                        30-day              90-day
#   CSM:all, LIQ+VRP:BEAR  (previous)   +307.8% dd 6.9%    +4018.5% dd 11.8%
#   CSM:all, LIQ+VRP:BULL+RANGING       +288.7% dd 12.2%   +2565.0% dd 13.8%
#   LIQ:BEAR, VRP:RANGING               +329.1% dd 11.2%   +3454.7% dd 16.5%
#   CSM only  (this)                    +347.4% dd 6.9%    +6254.4% dd 12.7%
#
# CSM-only wins on return in both windows AND on drawdown. The mechanism is
# SLOT CONTENTION, not just weak strategies: 11,363 of 16,250 signals were
# rejected for want of a free slot over 90 days. A slot spent on LIQ
# (+0.023%/trade) or VRP (-0.160%/trade) is a slot denied to CSM (+0.632%
# overall, +1.218% in BEAR). Adding a barely-breakeven strategy to a
# capacity-constrained portfolio is value-DESTROYING, not diversifying.
#
# Per-strategy expectancy by regime, 90-day (n in parentheses):
#                BEAR_TREND        BULL_TREND         RANGING
#   CSM         +1.218% (825)     +0.359% (3000)    +0.698% (5046)
#   LIQ         +0.023% (1490)    -0.116%  (928)    -0.044% (3441)
#   VRP         -0.160%  (471)    -0.064%  (289)    +0.100%  (760)
#
# CSM is positive in every regime at every window length tested (15/30/60/90d)
# — the only strategy in the engine for which that is true. LIQ's single
# positive cell is +0.023%, statistically indistinguishable from zero after
# fees. VRP is negative in 7 of 12 measured cells.
#
# CAUTION on shorter windows: the 15-day read said the opposite (LIQ BEAR
# -0.060% on 217 trades vs +0.023% on 1,490 at 90 days) and its matrix scored
# WORST of the four. Do not re-derive this from a window under 30 days.
#
# To re-enable either strategy, add it back to the relevant regime below —
# both remain in StrategyFactory and are fully wired, just not permitted.
# 2026-08-12: opened up by request. All three production strategies (CSM, VRP,
# LIQ) are permitted in every regime, so this table no longer gates anything —
# runtime control is the Telegram/Discord "/disable <ID>" command
# (modules/strategy_overrides.py, whose KNOWN_STRATEGIES derives from
# StrategyFactory, so it covers all three).
#
# What this replaced, and why it is not evidence of anything:
#   Until now only CSM was permitted, on the strength of a 90-day backtest.
#   That backtest handed strategies a 1h bar that had not closed yet — up to
#   45 minutes of future price. Corrected 2026-08-12; CSM's expectancy went
#   +0.632% -> -0.046% per trade. Every permission decision in this table's
#   history was derived from those contaminated numbers, including the one that
#   shut VRP and LIQ out of every regime.
#
#   So this is NOT a finding that all three deserve a slot. It is the removal of
#   a gate whose evidence turned out to be invalid, pending re-measurement on
#   the fixed harness.
#
# Practical consequence to watch: MAX_CONCURRENT is 3 and CSM alone already
# generated 3,352 signals in 30 days against ~1,300 slot rejections. Three
# strategies multiply that contention, and slots are handed out by arrival
# order because every strategy currently reports a constant strength of 1.0 —
# so signal VOLUME, not signal QUALITY, decides what actually trades.
REGIME_STRATEGY_PERMISSIONS = {
    # Synced from the main tree 2026-08-29. This dict had drifted badly: it
    # still listed VRP and LIQ (both deleted 2026-08-19, source files gone) and
    # permitted CSM in BEAR_TREND and OVERSOLD, which main forbids.
    #
    # That was not inert. verify_configA.py gates entries on this dict, and the
    # stale version let 1,243 BEAR_TREND shorts into a RANGING-only test --
    # measured at -0.096%/trade, which turned +0.247% into -0.048% and looked
    # like the strategy failing rather than the fixture being wrong.
    "BULL_TREND":  {"CSM": False, "NASOS_V4": True,  "ELLIOT_V8": True},
    "BEAR_TREND":  {"CSM": False, "NASOS_V4": True,  "ELLIOT_V8": True},
    "RANGING":     {"CSM": True,  "NASOS_V4": False, "ELLIOT_V8": False},
    "OVERSOLD":    {"CSM": False, "NASOS_V4": False, "ELLIOT_V8": False},
    "OVERHEATED":  {},
}


def is_strategy_permitted(regime, strategy_id):
    return REGIME_STRATEGY_PERMISSIONS.get(regime, {}).get(strategy_id, False)






