"""
risk_engine.py
Futures-specific risk management for Binance USDM Futures.

No equivalent exists in the NSE project — futures leverage and
liquidation are crypto-specific concepts introduced here.

Risk model:
  - Isolated margin mode (recommended — limits loss to deposited margin)
  - Fixed leverage per strategy (configurable)
  - Risk-per-trade = fixed % of total account equity
  - Position size computed from risk amount / (entry - SL distance)
  - Hard daily loss floor (same concept as DAILY_FLOOR in orb_engine.py)

Position sizing formula:
  risk_usdt  = account_equity × RISK_PCT_PER_TRADE
  contracts  = risk_usdt / (entry_price × sl_distance_pct)
  notional   = contracts × entry_price
  margin_req = notional / leverage

Liquidation price (isolated margin, simplified):
  LONG :  liq_price ≈ entry × (1 - 1/leverage + maintenance_margin)
  SHORT:  liq_price ≈ entry × (1 + 1/leverage - maintenance_margin)

Maintenance margin rate for Binance: ~0.5% on most contracts.
"""

import logging
import os

from modules import settings_manager as cfg

log = logging.getLogger("RiskEngine")

# ─── Constants ────────────────────────────────────────────────────────────────
RISK_PCT_PER_TRADE  = 0.01       # Risk 1% of equity per trade
MAX_LEVERAGE        = 50         # Hard cap at 50× (increased to support dynamic global overrides)
                                 # At $10 × 20% margin × 5× = $10 notional (above Binance $5 min)
                                 # Raising above 5× risks liquidation < 20% from entry
MAINTENANCE_MARGIN  = 0.005      # 0.5% — Binance standard for major perps

# ─── Runtime-editable limits ──────────────────────────────────────────────────
# Every tunable below is a FUNCTION, not a constant, so a change made from the
# dashboard or a bot applies on the next call with no restart. Bounds are
# enforced by settings_manager.SPEC, so no clamping is needed here.

def max_concurrent() -> int:
    return cfg.get("MAX_CONCURRENT")


# ─── Loss caps ────────────────────────────────────────────────────────────────
# Three layers of loss protection — each independently blocks new entries.
# Open positions always run to their own SL — never panic-closed by loss caps.
# All caps persist to disk so they survive bot restarts.

def daily_loss_cap() -> float:       # −10% daily → no new entries until next UTC day
    return cfg.get("DAILY_LOSS_CAP")


def weekly_loss_cap() -> float:      # −15% weekly → no new entries until next UTC Monday
    return cfg.get("WEEKLY_LOSS_CAP")


def session_loss_floor() -> float:
    # Matched to daily cap: on micro accounts the old -3% floor killed sessions
    # after 3 losses, before the daily cap could kick in.
    return cfg.get("SESSION_LOSS_FLOOR")


# Margin cap — max % of equity to commit as isolated margin on one trade.
# Aggregate margin cap across ALL open positions. MAX_MARGIN_PCT below is
# PER TRADE, so with MAX_CONCURRENT=3 nothing stopped 3 x 30% = 90% of equity
# being committed at once — observed live at 86% when CSM sized three
# tight-stop majors at $27-30 margin each. This is the ceiling on total
# simultaneous exposure.
def max_total_margin_pct() -> float:
    return cfg.get("MAX_TOTAL_MARGIN_PCT")


MAX_MARGIN_PCT      = 0.30       # Per-trade margin cap (was 0.20). Raised so 2× lev
                                 #  positions fit on $10 equity. With 2× lev and $5
                                 #  MIN_NOTIONAL the per-trade margin is $2.50 → needs ≥25%
                                 #  cap. 0.30 allows one safe position; scales with capital.

# Leveraged-loss cap per trade — backstop safety gate in calculate_position_size.
# Rejects entries where sl_pct × leverage exceeds this. Set to 10% as a final
# safety net for unusually wide SLs; per-strategy lev × HARD_STOP is sized
# below this (TP=5%, DB=7.5%, GRID=7.5%, FF=3.6%, BBR=6%).
#
# Configurable, because it is what actually determines realised leverage:
# GLOBAL_LEVERAGE is only a ceiling, and this cap clips it per trade via
# floor(cap / SL%). With cap=10%, CSM's 2x-ATR stops (3-8% wide) get clipped
# to 1-2x, while the backtest assumes a flat 5x — so live and backtest are
# not comparable until this is raised to match.
#
#   cap 0.10 (default) -> 5x needs SL <= 2.0%   — safest
#   cap 0.25           -> 5x needs SL <= 5.0%
#   cap 0.50           -> 5x needs SL <= 10.0%  — effectively uncapped at 5x
#
# Raising it means a stop-out costs proportionally more of the margin posted.
def max_leveraged_loss_pct() -> float:
    return cfg.get("MAX_LEVERAGED_LOSS_PCT")

# Minimum SL distance — reject signals where SL is unrealistically tight.
#
# Raised 0.5% -> 1.5% after live evidence: in a quiet market CSM's ">3x ATR in
# 24h" trigger starts firing on low-volatility majors (ETH/DOGE/HYPE at ~0.3%
# ATR), producing 0.58-0.74% stops. Two problems compound:
#   1. A 0.6% stop on ETH sits INSIDE normal tick noise — it is hit before the
#      thesis can play out. All three such trades stopped out at -0.6/-0.75%.
#   2. notional = risk$ / SL%, so a 0.58% stop sized a $148 position on $100
#      equity — 1.4x account, $29.66 margin, three of them = 86% committed.
# Rejecting them here keeps CSM on the higher-ATR alts its measured edge came
# from, and stops tiny stops ballooning position size.
def min_sl_pct() -> float:
    return cfg.get("MIN_SL_PCT")

# Maximum SL distance — reject signals whose stop is further than this from
# entry. This is the per-trade loss ceiling, and it applies to EVERY strategy.
#
# Added 2026-08-22 after live paper trades exited at -16.8%, -16.0% and -13.8%.
# Those were NOT stop failures: BMTUSDT entered at 0.04021 with sl_price
# 0.03346 — the stop was PLACED 16.78% away and filled almost exactly there.
#
# Cause: CSM sizes its stop as max(2 x ATR(15m), 3%), which has a floor but no
# CEILING. On a high-ATR alt, 2 x ATR reached ~16.8% of price. The old guard
# below only rejected beyond 20%, which is not a risk limit in any useful
# sense. Measured over 302 live trades: 47.7% carried stops wider than 3%, and
# 56 trades lost more than -3%, summing -472%.
#
# It also breaks the leverage cap. A 16.8% stop at 2x is a 33.6% leveraged
# loss, well past MAX_LEVERAGED_LOSS_PCT (10%).
#
# REJECT rather than CLAMP, deliberately. Clamping a 16% ATR-implied stop down
# to 3% puts it at ~0.36 x ATR — deep inside normal noise — and the documented
# duration profile says trades resolving under 2 hours are where money dies
# (see docs/EXPERIMENT_LOG.md 14.6). That would trade one big loss for many
# small ones. A setup whose natural stop does not fit the risk budget is a
# setup to skip.
#
# ── WHY 0.10 AND NOT 0.03 ────────────────────────────────────────────────────
# A 3% ceiling was tried first, because live trades showed -13% to -17% exits.
# Measured, it is the wrong trade:
#
#   CSM, 30 symbols / 90d:
#     all signals      1362 trades  E=+0.669%  sum +911.1%  worst -23.87%
#     only sl<=3%       257 trades  E=+0.292%  sum   +75.0%  worst  -3.14%
#   -> rejects 81% of signals, and the REJECTED ones averaged +0.757%
#      (sum +836.2%). It costs ~92% of CSM's profit.
#
# The premise was also wrong. A -17% PRICE move is not a -17% account loss,
# because notional = risk_usdt / sl_pct makes a wide stop produce a SMALL
# position. Over 302 live trades:
#
#            worst price move   worst EQUITY impact
#                    -21.15%              -3.32%
#     trades < -3%:       56                   1
#
#   BMTUSDT: -16.85% price -> -1.67% of equity -> -0.84 USDT.
#
# The relationship is INVERTED from intuition: DOGEUSDT lost -3.32% of equity
# on only a -6.07% price move, because its ~1.8% stop bought a much larger
# position. Tighter stops mean bigger positions mean MORE equity at risk, so a
# low MAX_SL_PCT pushes toward exactly the trades that hurt the account most.
#
# 0.10 caps the genuine tail (~12% of live trades sat above an 8% stop) while
# leaving the ATR-sized stops that carry the edge. To bound ACCOUNT loss, lower
# RISK_PCT_PER_TRADE instead — that is the knob that actually controls it.
def max_sl_pct() -> float:
    return max(min_sl_pct(), cfg.get("MAX_SL_PCT"))

# ── Hard per-trade loss ceiling, enforced on EXIT ────────────────────────────
# Close any open position whose LEVERAGED loss reaches this, regardless of what
# the strategy's own stop says. This is the "ROI" figure Binance shows on the
# position card: price_move x leverage.
#
# Unlike MAX_SL_PCT (an ENTRY filter that rejects signals), this is a backstop
# on the exit side, so it does not discard trades — it truncates them. Set to 0
# to disable.
#
# ⚠️ READ THIS BEFORE LOWERING IT. Leverage compresses the price tolerance:
#
#     leverage   at 5% ceiling   at 3% ceiling
#         5x         -1.00%          -0.60%   <- inside tick noise
#         4x         -1.25%          -0.75%
#         3x         -1.67%          -1.00%
#         2x         -2.50%          -1.50%
#         1x         -5.00%          -3.00%
#
# Default is 0 (DISABLED) — it was measured to cost money at 0.05; see below. At GLOBAL_LEVERAGE=5 a 3% ceiling closes trades on a 0.6%
# wobble, and the documented duration profile says sub-2h resolutions are where
# money dies (docs/EXPERIMENT_LOG.md 14.6). 5% gives a 1.0% tolerance at 5x,
# which is outside most tick noise. For the ceiling to mean a PRICE move of the
# same size, pair it with GLOBAL_LEVERAGE=1.
#
# It also fires BEFORE the strategy's stop in most configurations, which means
# the strategy's own exit logic — breakeven, trailing, the CSM profit ladder —
# never gets to run on losing trades.
# Maximum SL distance used by ML P3 SL-override validation in live_scanner.py.
# ML override is rejected if the resulting sl_pct exceeds this value — ensures
# the ML-suggested SL doesn't widen beyond the widest per-strategy hard stop
# (TP=2.5%, DB=2.5%). Imported as: from modules.risk_engine import HARD_STOP_PCT
HARD_STOP_PCT       = 0.025      # 2.5% — upper bound for ML P3 SL override

# Per-strategy leverage — targeted caps on worst-case leveraged loss.
# TP cut 5→2 (was 12.8% leveraged HARD_STOPs). DB cut 5→3 (was 15%).
#
# 2026-08-04: this table had drifted to the pre-2026 strategy set
# (TP/FF/DB/GRID/BBR). Six of the seven strategies actually in production were
# absent, so get_leverage() silently returned the default of 3 for all of them
# — per-strategy leverage was effectively unconfigured. Production IDs added
# below at the previous default so behaviour is unchanged; tune deliberately.
STRATEGY_LEVERAGE = {
    # ── Production ───────────────────────────────────────────────────────────
    "CSM":   3,
    "NASOS_V4":   3,
    "TSMOM_4H":   3,
    "REBALANCING_PREMIUM": 3,
    # ── Archived (retained so historical positions still size correctly) ─────
    "SMA_OFFSET": 3,
    "LIQ":  3,
    "VRP":  3,
    "FF_V2": 3,
    "TP":   2,
    "EI3_V2": 3,
    "FF":   3,
    "DB":   3,
    "GRID": 3,
    "BBR":  3,
}

MIN_NOTIONAL_USDT = 5.0          # Binance minimum order size in USDT


# ─── Leverage ─────────────────────────────────────────────────────────────────

def get_leverage(strategy_id: str) -> int:
    """
    Return the configured leverage for a given strategy.
    GLOBAL_LEVERAGE > 0 overrides all strategies; 0 uses the table above.

    Picked up live — a change via the dashboard or the Telegram/Discord
    /leverage command applies from the next signal onward, no restart needed.

    Returns:
        Integer leverage, capped at MAX_LEVERAGE.
    """
    global_lev = cfg.get("GLOBAL_LEVERAGE")
    if global_lev > 0:
        return max(1, min(global_lev, MAX_LEVERAGE))

    lev = STRATEGY_LEVERAGE.get(strategy_id, 3)
    return min(lev, MAX_LEVERAGE)


# ─── Position sizing ──────────────────────────────────────────────────────────

def compute_position_size(
    account_equity:  float,
    entry_price:     float,
    sl_price:        float,
    strategy_id:     str,
    risk_mult:       float = 1.0,
) -> dict:
    """
    Compute position size in contracts given risk parameters.

    Args:
        account_equity : Total USDT equity in the account
        entry_price    : Planned entry price
        sl_price       : Stop loss price
        strategy_id    : 'CSM' | 'NASOS_V4' | str — looked up in STRATEGY_LEVERAGE
        risk_mult      : ML confidence multiplier (0.3–1.0). Scales risk_usdt
                         without changing leverage or margin cap. Default 1.0
                         (no adjustment). Set by ml_engine Phase 2+.

    Returns:
        dict with keys:
          contracts    : float — position size in base-asset contracts
          notional     : float — position value in USDT
          margin_req   : float — USDT margin to be deposited (isolated)
          leverage     : int
          risk_usdt    : float — amount at risk on this trade
          valid        : bool  — False if size is below minimum

    On invalid input (zero SL distance, zero equity), returns valid=False dict.
    """
    if account_equity <= 0:
        log.error("account_equity must be positive")
        return _invalid_size("Zero or negative equity")

    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0 or entry_price <= 0:
        log.error(f"Invalid prices: entry={entry_price}, sl={sl_price}")
        return _invalid_size("Zero SL distance or zero entry price")

    sl_pct = sl_distance / entry_price
    MIN_SL_PCT = min_sl_pct()
    MAX_SL_PCT = max_sl_pct()
    MAX_LEVERAGED_LOSS_PCT = max_leveraged_loss_pct()

    # ── Minimum SL distance check ─────────────────────────────────────────────
    # SL tighter than 0.5% is noise — price will hit it on normal spread/wick
    if sl_pct < MIN_SL_PCT:
        log.warning(
            f"SL distance {sl_pct*100:.3f}% is below minimum {MIN_SL_PCT*100:.1f}% "
            f"— signal rejected (too tight, noise will trigger it)"
        )
        return _invalid_size(f"SL {sl_pct*100:.3f}% below minimum {MIN_SL_PCT*100:.1f}%")

    # Per-trade loss ceiling. Was a hardcoded 20%, which let a 16.8% stop
    # through and produced a -16.85% exit. See MAX_SL_PCT above.
    if sl_pct > MAX_SL_PCT:
        log.warning(
            f"SL distance {sl_pct*100:.2f}% exceeds maximum {MAX_SL_PCT*100:.1f}% "
            f"— signal rejected (loss per trade would breach the risk ceiling)"
        )
        return _invalid_size(f"SL {sl_pct*100:.2f}% above maximum {MAX_SL_PCT*100:.1f}%")

    leverage   = get_leverage(strategy_id)

    # ── Leveraged-loss cap ────────────────────────────────────────────────────
    # Keep sl_pct × leverage within MAX_LEVERAGED_LOSS_PCT (=10%). Strategies
    # size their SL from ATR with no knowledge of the leverage they'll trade at,
    # so on high-volatility symbols the configured leverage breaches the cap.
    # Step leverage down to fit instead of rejecting — the position is still
    # risk-sized to RISK_PCT_PER_TRADE below, so a lower leverage only means
    # more margin posted, not more risk. Reject only if even 1× breaches it.
    if sl_pct * leverage > MAX_LEVERAGED_LOSS_PCT:
        # Nudge before truncating. sl_pct comes from a float subtraction, so a
        # nominal 5.00% stop computes as 0.050000000000000044 and
        # 10%/sl_pct = 1.9999999999999984 — int() then yields 1x instead of 2x,
        # silently halving leverage at every exact boundary (2%→4x not 5x,
        # 2.5%→3x not 4x). The epsilon is far smaller than any real SL
        # difference, so it only rescues these representation artefacts.
        capped_lev = int(MAX_LEVERAGED_LOSS_PCT / sl_pct + 1e-9)
        if capped_lev < 1:
            log.warning(
                f"[{strategy_id}] SL {sl_pct*100:.2f}% exceeds "
                f"{MAX_LEVERAGED_LOSS_PCT*100:.1f}% leveraged-loss cap even at 1× "
                f"— rejecting"
            )
            return _invalid_size(
                f"SL {sl_pct*100:.2f}% exceeds "
                f"{MAX_LEVERAGED_LOSS_PCT*100:.1f}% leveraged-loss cap at 1×"
            )
        log.info(
            f"[{strategy_id}] SL {sl_pct*100:.2f}% × lev {leverage}× = "
            f"{sl_pct*leverage*100:.2f}% > cap {MAX_LEVERAGED_LOSS_PCT*100:.1f}% "
            f"— reducing leverage {leverage}× → {capped_lev}×"
        )
        leverage = capped_lev

    risk_usdt  = account_equity * RISK_PCT_PER_TRADE * risk_mult

    # Contracts = how much base asset we need so that sl_distance × qty = risk_usdt
    contracts  = risk_usdt / sl_distance    # base asset units
    notional   = contracts * entry_price    # USDT value
    margin_req = notional / leverage        # isolated margin required

    # ── Margin cap ────────────────────────────────────────────────────────────
    # If margin exceeds MAX_MARGIN_PCT of equity, scale down contracts.
    # This prevents tiny SL distances from creating oversized positions.
    max_margin  = account_equity * MAX_MARGIN_PCT
    if margin_req > max_margin:
        log.warning(
            f"Margin {margin_req:.2f} USDT exceeds cap {max_margin:.2f} USDT "
            f"({MAX_MARGIN_PCT*100:.0f}% of equity) — scaling down position"
        )
        margin_req = max_margin
        notional   = margin_req * leverage
        contracts  = notional / entry_price

    if notional < MIN_NOTIONAL_USDT:
        # ── Minimum notional boost ─────────────────────────────────────────────
        # With small accounts (e.g. $10), risk-based sizing often produces
        # notional < $5 for wide-SL signals. Rather than reject the trade,
        # scale UP contracts to MIN_NOTIONAL_USDT if the required margin
        # stays within the MAX_MARGIN_PCT cap.
        #
        # Example: $10 equity, SL=3%, MIN_NOTIONAL=$5
        #   risk-based notional = $10 × 0.01 / 0.03 = $3.33 → below $5
        #   boost notional to $5, margin = $5 / 5× = $1.00 ≤ $2 cap → accept
        #   actual risk on this trade = $5 × 0.03 = $0.15 = 1.5% equity (acceptable)
        #
        # If even MIN_NOTIONAL requires margin > cap, we truly can't trade it.
        boost_margin = MIN_NOTIONAL_USDT / leverage
        if boost_margin <= max_margin:
            log.info(
                f"Boosting notional {notional:.2f} → {MIN_NOTIONAL_USDT} USDT "
                f"(min notional, margin {boost_margin:.2f}/{max_margin:.2f} USDT)"
            )
            notional   = MIN_NOTIONAL_USDT
            margin_req = boost_margin
            contracts  = notional / entry_price
        else:
            log.warning(
                f"Notional {notional:.2f} USDT is below Binance minimum "
                f"({MIN_NOTIONAL_USDT} USDT) and boost would exceed margin cap "
                f"({boost_margin:.2f} > {max_margin:.2f}) — skipping"
            )
            return _invalid_size("Below minimum notional, margin cap prevents boost")

    actual_risk = contracts * sl_distance
    return {
        "contracts":  round(contracts, 6),
        "notional":   round(notional, 2),
        "margin_req": round(margin_req, 2),
        "leverage":   leverage,
        "risk_usdt":  round(actual_risk, 2),
        "sl_pct":     round(sl_pct, 5),
        "valid":      True,
        "reason":     "",
    }


def _invalid_size(reason: str) -> dict:
    return {
        "contracts": 0.0, "notional": 0.0, "margin_req": 0.0,
        "leverage": 1, "risk_usdt": 0.0, "sl_pct": 0.0,
        "valid": False, "reason": reason,
    }


# ─── Liquidation price ────────────────────────────────────────────────────────

def compute_liq_price(
    entry_price:  float,
    leverage:     int,
    direction:    str,    # 'LONG' or 'SHORT'
) -> float:
    """
    Approximate isolated-margin liquidation price.

    This is a simplified formula. Actual Binance liquidation also
    factors in the mark price feed, insurance fund, and cross-margin
    contribution — use this as a conservative estimate only.

    Args:
        entry_price : Position entry price
        leverage    : Integer leverage (e.g. 10)
        direction   : 'LONG' or 'SHORT'

    Returns:
        Float liquidation price. Returns 0.0 on bad input.
    """
    if entry_price <= 0 or leverage <= 0:
        return 0.0

    if direction == "LONG":
        # Price drops to wipe out margin: entry × (1 - 1/lev + MM)
        liq = entry_price * (1 - (1 / leverage) + MAINTENANCE_MARGIN)
    else:
        # Price rises to wipe out margin: entry × (1 + 1/lev - MM)
        liq = entry_price * (1 + (1 / leverage) - MAINTENANCE_MARGIN)

    return round(liq, 6)


# ─── Persistent loss tracker ──────────────────────────────────────────────────
# Written to disk after every closed trade.
# Survives bot restarts — loss caps remain active even after a crash + restart.

import json
from datetime import datetime, timezone

_MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_MODULE_DIR)
_LOSS_FILE   = os.path.join(_PROJECT_DIR, "data", "loss_tracker.json")

# In-memory cache for loss tracker — avoids re-reading JSON from disk on every
# is_loss_cap_hit() call (once per signal).  Invalidated by _save_tracker().
#
# Validity key is (mtime, size), not mtime alone: two writes in the same mtime
# tick that changed a value would almost certainly change the byte length too,
# so the pair makes a same-tick stale read effectively impossible.
# CSB_NO_FILE_CACHE=true bypasses the cache entirely (authoritative disk reads)
# — a one-flag switch to rule caching out when debugging stale state.
_tracker_cache = {"key": None, "data": None}
_NO_FILE_CACHE = os.getenv("CSB_NO_FILE_CACHE", "false").strip().lower() in ("1", "true", "yes", "on")


def _file_key(path):
    """(mtime, size) validity key for a cached file, or None if unstatable."""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_week() -> str:
    """ISO week string e.g. '2026-W13' — resets every Monday UTC."""
    n = datetime.now(timezone.utc)
    return f"{n.isocalendar()[0]}-W{n.isocalendar()[1]:02d}"


def _load_tracker() -> dict:
    """Load persisted loss state from disk, with mtime-based caching.

    Returns the in-memory copy when the file hasn't changed since the last
    read, avoiding a JSON parse per signal per scan cycle.
    """
    try:
        if os.path.exists(_LOSS_FILE):
            key = _file_key(_LOSS_FILE)
            if (not _NO_FILE_CACHE and key is not None
                    and _tracker_cache["key"] == key
                    and _tracker_cache["data"] is not None):
                return _tracker_cache["data"]
            with open(_LOSS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _tracker_cache["key"] = key
            _tracker_cache["data"] = data
            return data
    except Exception as exc:
        log.error(
            f"Loss tracker load failed ({_LOSS_FILE}): {exc} — "
            f"loss caps reset to zero for this process"
        )
    return {"day": _utc_today(), "day_pnl": 0.0,
            "week": _utc_week(), "week_pnl": 0.0}


def _save_tracker(data: dict) -> None:
    os.makedirs(os.path.dirname(_LOSS_FILE), exist_ok=True)
    try:
        with open(_LOSS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        # Update in-memory cache so the next _load_tracker() is a hit.
        _tracker_cache["data"] = data
        _tracker_cache["key"]  = _file_key(_LOSS_FILE)
    except Exception as exc:
        log.error(f"Loss tracker save failed: {exc}")
        _tracker_cache["key"]  = None
        _tracker_cache["data"] = None


def record_trade_pnl(pnl_pct: float) -> dict:
    """
    Record a closed trade's P&L into the persistent daily + weekly tracker.
    Call this after every trade closes (both paper and live).

    Args:
        pnl_pct : Fractional P&L e.g. -0.03 = -3%

    Returns:
        Updated tracker dict with keys: day_pnl, week_pnl,
        daily_cap_hit, weekly_cap_hit.
    """
    data = _load_tracker()

    # Reset day bucket if UTC date changed
    today = _utc_today()
    if data.get("day") != today:
        log.info(f"New UTC day {today} — daily loss tracker reset")
        data["day"]     = today
        data["day_pnl"] = 0.0

    # Reset week bucket if ISO week changed
    week = _utc_week()
    if data.get("week") != week:
        log.info(f"New UTC week {week} — weekly loss tracker reset")
        data["week"]     = week
        data["week_pnl"] = 0.0

    data["day_pnl"]  = round(data.get("day_pnl",  0.0) + pnl_pct, 6)
    data["week_pnl"] = round(data.get("week_pnl", 0.0) + pnl_pct, 6)
    _save_tracker(data)

    DAILY_LOSS_CAP, WEEKLY_LOSS_CAP = daily_loss_cap(), weekly_loss_cap()
    daily_hit  = data["day_pnl"]  <= DAILY_LOSS_CAP
    weekly_hit = data["week_pnl"] <= WEEKLY_LOSS_CAP

    if daily_hit:
        log.warning(
            f"DAILY LOSS CAP HIT: {data['day_pnl']*100:.2f}% "
            f"(cap: {DAILY_LOSS_CAP*100:.0f}%) — no new entries today"
        )
    if weekly_hit:
        log.warning(
            f"WEEKLY LOSS CAP HIT: {data['week_pnl']*100:.2f}% "
            f"(cap: {WEEKLY_LOSS_CAP*100:.0f}%) — no new entries this week"
        )

    return {
        "day_pnl":       data["day_pnl"],
        "week_pnl":      data["week_pnl"],
        "daily_cap_hit": daily_hit,
        "weekly_cap_hit": weekly_hit,
    }


def is_loss_cap_hit() -> tuple[bool, str]:
    """
    Check if any loss cap is currently active.
    Call at the start of each scan cycle before scanning for signals.

    Returns:
        (blocked: bool, reason: str)
        blocked = True means no new entries allowed.
    """
    data = _load_tracker()

    # Each bucket is evaluated INDEPENDENTLY, and a stale bucket simply
    # contributes zero. It must never short-circuit the other one.
    #
    # This used to early-`return False, ""` the moment the stored day was not
    # today, BEFORE the weekly test below ever ran. The weekly cap therefore
    # only blocked entries on the same UTC day it was breached: at the next
    # midnight it silently lifted while week_pnl was still past the cap, and
    # trading resumed for the rest of the week with no weekly protection at
    # all. Reproduced directly — week_pnl -12.00% against a -10% cap returned
    # (False, '') when the breach was recorded on any prior day.
    day_pnl  = data.get("day_pnl",  0.0) if data.get("day")  == _utc_today() else 0.0
    week_pnl = data.get("week_pnl", 0.0) if data.get("week") == _utc_week()  else 0.0

    DAILY_LOSS_CAP, WEEKLY_LOSS_CAP = daily_loss_cap(), weekly_loss_cap()
    if week_pnl <= WEEKLY_LOSS_CAP:
        return True, (
            f"Weekly loss cap active: {week_pnl*100:.2f}% "
            f"(cap {WEEKLY_LOSS_CAP*100:.0f}%) — "
            f"resumes next Monday UTC"
        )
    if day_pnl <= DAILY_LOSS_CAP:
        return True, (
            f"Daily loss cap active: {day_pnl*100:.2f}% "
            f"(cap {DAILY_LOSS_CAP*100:.0f}%) — "
            f"resumes tomorrow UTC"
        )
    return False, ""


def get_loss_status() -> dict:
    """Return current loss tracker state for logging/Telegram."""
    data = _load_tracker()
    return {
        "day_pnl":   round(data.get("day_pnl",  0.0) * 100, 3),
        "week_pnl":  round(data.get("week_pnl", 0.0) * 100, 3),
        "daily_cap": daily_loss_cap()  * 100,
        "weekly_cap": weekly_loss_cap() * 100,
    }


# ─── Simple gates (used in live_scanner.py) ───────────────────────────────────

def is_daily_floor_hit(session_pnl_pct: float) -> bool:
    """
    Session-level floor check (in-memory, resets on restart).
    Uses SESSION_LOSS_FLOOR — distinct from the persistent daily cap.
    """
    return session_pnl_pct <= session_loss_floor()


def max_per_strategy() -> dict:
    """{strategy_id: cap} from MAX_PER_STRATEGY, current as of this call."""
    return cfg.parse_caps(cfg.get("MAX_PER_STRATEGY"))


def is_position_cap_hit(n_open: int) -> bool:
    """Return True if maximum concurrent positions are already open."""
    return n_open >= max_concurrent()


def is_strategy_cap_hit(strategy_id: str, active_positions: list) -> bool:
    """Return True if this strategy has reached its per-strategy slot limit."""
    cap = max_per_strategy().get(strategy_id)
    if cap is None:
        return False
    count = sum(1 for p in active_positions if p.get("strategy") == strategy_id)
    return count >= cap




