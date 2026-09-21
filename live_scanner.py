"""
live_scanner.py
Binance USDM Futures Bot — Main 24/7 Scanner Loop.

Runs in one of two modes, selected by LIVE_ENABLED in .env:
  LIVE  — orders are placed on Binance.
  PAPER — LIVE_ENABLED=false. Positions are tracked, managed by SL/TP, closed
          and notified exactly as live ones, but order_engine simulates the
          fill instead of calling Binance. No order ever reaches the exchange.

This is distinct from the standalone `cs` project, which has its own
paper_engine; here PAPER is the same code path as LIVE minus order placement.

Loop cycle (every SCAN_INTERVAL seconds):
  1. Refresh symbol list (hourly via cache)
  2. Fetch BTC/ETH/SOL 1h → classify regime
  3. If regime changed → log + notify Telegram
  4. Determine permitted strategies for current regime
  5. Fast cycle if positions open (10s) or full cycle (60s)
  6. Fetch OHLCV data for open symbols (fast) or all symbols (full)
  7. Reconcile with Binance — adopt orphaned positions, detect manual closes
  8. Manage all open live positions (trail, SL/TP checks)
  9. If full cycle: scan for signals, execute entries
  10. Sleep until next cycle

Position cap rules:
  - MAX_CONCURRENT = 3 live positions at once
  - MAX_ENTRIES_PER_CYCLE = from data/settings.json (default 2)
  - Next trade only opens after one of the 3 closes

Risk gates checked before any new entry:
  - Persistent daily/weekly loss cap (survives restarts)
  - Session P&L floor
  - Max concurrent live positions (3)
  - Symbol already has an open live position
  - Symbol in loss cooldown (15min after a loss)
  - Regime age gate (10min after regime change)

Graceful shutdown:
  SIGTERM → close all open live positions at market, write session log, exit.
"""

import os
import sys
import json
import time
import signal
import logging
from datetime import datetime, timezone
import pandas as pd
from dotenv import load_dotenv

# ── Load .env before any module imports that read env vars ────────────────────
# .env now carries only secrets and LIVE_ENABLED; every tunable lives in
# data/settings.json (see modules/settings_manager.py) and is hot-reloaded.
_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))

from modules import settings_manager as cfg
cfg.migrate_from_env()      # first run after upgrade: seed settings.json from .env

# ── Logging setup (goes to stdout → captured by journald) ─────────────────────
_LOG_LEVEL = cfg.get("LOG_LEVEL")
logging.basicConfig(
    level    = getattr(logging, _LOG_LEVEL, logging.INFO),
    format   = "%(asctime)s | %(name)-18s | %(levelname)-8s | %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
    stream   = sys.stdout,
    force    = True,
)
log = logging.getLogger("LiveScanner")

# ── Project imports ───────────────────────────────────────────────────────────
from modules.auth_manager      import validate_credentials, test_binance_connection
from modules.symbol_filter     import get_top_symbols
from modules.watchlist         import get_watchlist, get_focused_watchlist
from modules.data_hub          import (
    fetch_all_symbols, fetch_btc_reference, fetch_active_positions_data
)
from modules.regime_engine     import (
    classify_regime, REGIME_STRATEGY_PERMISSIONS
)
from modules.risk_engine       import (
    compute_position_size, compute_liq_price,
    is_daily_floor_hit, is_position_cap_hit, is_strategy_cap_hit,
    is_loss_cap_hit, record_trade_pnl, get_loss_status,
)
from modules.order_engine      import (
    OrderEngine, LIVE_ENABLED, get_account_equity, update_stop_order,
    preload_exchange_specs,
)
from modules                   import paper_equity
from modules.strategies.strategy_factory import StrategyFactory
from live_logger               import LiveLogger, utc_to_ist
from telegram_notifier         import (
    notify_startup  as _tg_notify_startup,
    notify_signal   as _tg_notify_signal,
    notify_exit     as _tg_notify_exit,
    notify_session_summary as _tg_notify_session_summary,
    notify_error    as _tg_notify_error,
    notify_regime_change   as _tg_notify_regime_change,
    notify_daily_report    as _tg_notify_daily_report,
)
from discord_notifier          import (
    notify_startup  as _dc_notify_startup,
    notify_signal   as _dc_notify_signal,
    notify_exit     as _dc_notify_exit,
    notify_session_summary as _dc_notify_session_summary,
    notify_error    as _dc_notify_error,
    notify_regime_change   as _dc_notify_regime_change,
    notify_daily_report    as _dc_notify_daily_report,
)

# ── Telegram toggle ──────────────────────────────────────────────────────────
# TELEGRAM_ENABLED=false (Settings tab) disables Telegram notifications.
# Discord always sends. _tg() wraps Telegram-only; _safe() sends to both.
def _tg(fn, *args, **kwargs):
    """Call a telegram_notifier function only if Telegram is enabled."""
    if cfg.get("TELEGRAM_ENABLED"):
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            log.warning(f"Telegram notification failed: {exc}")

def notify_startup(*a, **kw):
    _tg(_tg_notify_startup, *a, **kw)
    try: _dc_notify_startup(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_startup failed: {exc}")

def notify_signal(*a, **kw):
    _tg(_tg_notify_signal, *a, **kw)
    try: _dc_notify_signal(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_signal failed: {exc}")

def notify_exit(*a, **kw):
    _tg(_tg_notify_exit, *a, **kw)
    try: _dc_notify_exit(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_exit failed: {exc}")

def notify_session_summary(*a, **kw):
    _tg(_tg_notify_session_summary, *a, **kw)
    try: _dc_notify_session_summary(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_session_summary failed: {exc}")

def notify_error(*a, **kw):
    _tg(_tg_notify_error, *a, **kw)
    try: _dc_notify_error(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_error failed: {exc}")

def notify_regime_change(*a, **kw):
    _tg(_tg_notify_regime_change, *a, **kw)
    try: _dc_notify_regime_change(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_regime_change failed: {exc}")

def notify_daily_report(*a, **kw):
    _tg(_tg_notify_daily_report, *a, **kw)
    try: _dc_notify_daily_report(*a, **kw)
    except Exception as exc: log.warning(f"Discord notify_daily_report failed: {exc}")
from modules.ml_engine         import (
    get_ml_adjustments, commit_ml_signal, log_outcome as ml_log_outcome,
    check_milestone_alert, get_ml_status,
)
# LLM regime classification and the LLM advisor were REMOVED 2026-08-11.
# Both were already disabled via config and contributed nothing measurable; they
# were removed so the live engine matches the backtest exactly, making
# live-vs-backtest divergence attributable to the engine rather than to an
# optional subsystem. See docs/LLM_REMOVED.md to restore either.

# ── Runtime configuration ─────────────────────────────────────────────────────
# These module globals are a SNAPSHOT of data/settings.json, refreshed by
# _refresh_settings() at the top of every loop cycle. Nothing below is read
# from .env any more, so an edit from the dashboard, Telegram or Discord takes
# effect on the next cycle without a restart. The rest of this file uses the
# names exactly as before.

# SCAN_INTERVAL / FAST_INTERVAL — how often to look for new entries / manage
# open positions. TOP_N_SYMBOLS — universe size when focused mode is off.
SCAN_INTERVAL   = cfg.get("SCAN_INTERVAL_SECONDS")
FAST_INTERVAL   = cfg.get("FAST_INTERVAL_SECONDS")
TOP_N_SYMBOLS   = cfg.get("TOP_N_SYMBOLS")

# ── Focused mode: scan ONLY the top-N curated coins (both sides, all strategies)
FOCUSED_MODE    = cfg.get("FOCUSED_MODE")
_FOCUSED_N      = cfg.get("FOCUSED_SIZE")

# Account equity — starts from settings, then refreshed from Binance every
# EQUITY_REFRESH_INTERVAL in LIVE mode. Default is $10 (not $1000) — the safe
# floor if the setting is missing.
ACCOUNT_EQUITY  = cfg.get("ACCOUNT_EQUITY_USDT")

# Immutable starting figure for PAPER mode. ACCOUNT_EQUITY drifts as simulated
# trades close; this stays put so paper_equity can tell start from realized.
PAPER_STARTING_EQUITY = ACCOUNT_EQUITY

# ── Bar-close management (experiment) ────────────────────────────────────────
# When true, manage() is evaluated against the last COMPLETED 15m bar instead
# of the 5s mark price — matching how backtest_optimizer scores trades.
#
# Why this exists: live showed 43.5% SL_HIT / 8.7% TP_HIT while the backtest on
# the same strategy showed 24.2% / 38.1%. The backtest samples once per 15m bar
# and therefore cannot see intra-bar stop-outs; live sees every tick. This flag
# lets the two be compared on equal terms.
#
# The ORIGINAL stop is deliberately still checked on tick even when this is on:
# Binance holds a STOP_MARKET at that level and fires it at tick resolution no
# matter what the bot does. Ignoring it in paper would make paper LESS like
# live, not more. Only TP, trailing and breakeven move to bar closes — those
# are bot-side and have no exchange order behind them.
MANAGE_ON_BAR_CLOSE = cfg.get("MANAGE_ON_BAR_CLOSE")

# How often to refresh ACCOUNT_EQUITY from Binance (seconds)
EQUITY_REFRESH_INTERVAL = 60   # 1 minutes

# Log a fast-cycle heartbeat every Nth pass instead of every pass. At the
# default 5s interval this is roughly one line per minute while a position is
# open, rather than ~24. Exits, entries, warnings and errors are unaffected.
FAST_LOG_EVERY = max(1, int(60 / max(1, FAST_INTERVAL)))

# EOD daily report time (UTC hour). 0 = midnight UTC = 05:30 IST
EOD_REPORT_HOUR_UTC = 0

# Minimum signal strength to act on (0–1 from strategy.scan())
#
# Only meaningful for strategies that actually VARY their strength. CSM is
# pinned at 1.0 deliberately: measured on 1,337 trades / 90d, momentum inside
# the 4-5x band does not predict expectancy (r = +0.02, kept-vs-blocked
# t = +1.39) and the response is an inverted U — edge peaks at 4.4-4.8x and
# FALLS at 4.8-5.0x — so a monotonic |mom|-4 score ranks the worst bucket
# highest. Pinning CSM at 1.0 means this gate never blocks it.
MIN_STRENGTH = cfg.get("MIN_STRENGTH")
# MIN_STRENGTH=0 disables the gate entirely and lets every signal through,
# which is how the bot behaved before 2026-08-20.

# Tag applied to logs and notifications. With LIVE_ENABLED=false the bot still
# tracks positions and runs SL/TP management — order_engine simulates the fill
# rather than calling Binance — so those trades are labelled PAPER, not LIVE.
TRADE_MODE = "LIVE" if LIVE_ENABLED else "PAPER"

# Max new entries per full scan cycle.
# = 1 ensures trades open sequentially — next trade only after one closes.
# With MAX_CONCURRENT=3: cycles build up 1→2→3, then cap blocks new entries
# until a close frees a slot.
MAX_ENTRIES_PER_CYCLE = cfg.get("MAX_ENTRIES_PER_CYCLE")

# Risk gates mirrored from settings so the loop reads plain globals.
MAX_CONCURRENT       = cfg.get("MAX_CONCURRENT")
MAX_TOTAL_MARGIN_PCT = cfg.get("MAX_TOTAL_MARGIN_PCT")
MAX_TRADE_LOSS_PCT   = cfg.get("MAX_TRADE_LOSS_PCT")

# Keys whose change must force a symbol-universe rebuild on the next cycle.
_UNIVERSE_KEYS = ("FOCUSED_MODE", "FOCUSED_SIZE", "TOP_N_SYMBOLS", "MIN_COIN_AGE_DAYS")

_settings_seen: dict = {}


def _refresh_settings() -> set:
    """
    Re-snapshot the hot settings into this module's globals.

    Called once per loop cycle. Returns the set of keys whose value changed
    since the previous call, so the loop can react (e.g. rebuild the symbol
    list when the universe shape changes). Logs every change so the journal
    shows exactly when an operator edit took effect.
    """
    global SCAN_INTERVAL, FAST_INTERVAL, TOP_N_SYMBOLS, FOCUSED_MODE, _FOCUSED_N
    global MANAGE_ON_BAR_CLOSE, FAST_LOG_EVERY, MIN_STRENGTH, MAX_ENTRIES_PER_CYCLE
    global MAX_CONCURRENT, MAX_TOTAL_MARGIN_PCT, MAX_TRADE_LOSS_PCT
    global CLOSE_ON_SHUTDOWN, MAX_RESUME_AGE_HOURS
    global ACCOUNT_EQUITY, PAPER_STARTING_EQUITY

    current = cfg.get_all()
    changed = {k for k, v in current.items() if _settings_seen.get(k, v) != v}
    _settings_seen.update(current)

    for k in sorted(changed):
        log.info(f"[SETTINGS] {k} changed -> {current[k]!r} (applied live)")

    SCAN_INTERVAL         = current["SCAN_INTERVAL_SECONDS"]
    FAST_INTERVAL         = current["FAST_INTERVAL_SECONDS"]
    FAST_LOG_EVERY        = max(1, int(60 / max(1, FAST_INTERVAL)))
    TOP_N_SYMBOLS         = current["TOP_N_SYMBOLS"]
    FOCUSED_MODE          = current["FOCUSED_MODE"]
    _FOCUSED_N            = current["FOCUSED_SIZE"]
    MANAGE_ON_BAR_CLOSE   = current["MANAGE_ON_BAR_CLOSE"]
    MIN_STRENGTH          = current["MIN_STRENGTH"]
    MAX_ENTRIES_PER_CYCLE = current["MAX_ENTRIES_PER_CYCLE"]
    MAX_CONCURRENT        = current["MAX_CONCURRENT"]
    MAX_TOTAL_MARGIN_PCT  = current["MAX_TOTAL_MARGIN_PCT"]
    MAX_TRADE_LOSS_PCT    = current["MAX_TRADE_LOSS_PCT"]
    CLOSE_ON_SHUTDOWN     = current["CLOSE_ON_SHUTDOWN"]
    MAX_RESUME_AGE_HOURS  = current["MAX_RESUME_AGE_HOURS"]

    if "LOG_LEVEL" in changed:
        logging.getLogger().setLevel(
            getattr(logging, current["LOG_LEVEL"], logging.INFO))

    # A new starting balance is a new PAPER experiment: reset the simulated
    # ledger to it. In LIVE mode Binance is the source of truth, so the
    # setting only matters when the balance fetch fails.
    if "ACCOUNT_EQUITY_USDT" in changed and not LIVE_ENABLED:
        PAPER_STARTING_EQUITY = current["ACCOUNT_EQUITY_USDT"]
        ACCOUNT_EQUITY = paper_equity.summary(PAPER_STARTING_EQUITY)["equity"]
        log.info(f"Paper equity reset to ${ACCOUNT_EQUITY:.2f} USDT")

    return changed

# Minimum regime age before scanning for new entries.
# Prevents trading on regime flickers. Regime must be stable for this many
# minutes before signals are acted on.
# Reduced from 20 → 5 → 1 min — regime_engine.py hysteresis (TREND_CONFIRM_BARS=2,
# MIN_HOLD_MINUTES=15) prevents flickering at source; the gate only needs to
# absorb the 60s scan-cycle jitter.
MIN_REGIME_AGE_MINUTES = 1

# Loss cooldown: after a loss on a symbol, block re-entry for this long.
LOSS_COOLDOWN_MINUTES = 15

# Per-symbol loss cooldown tracker {symbol: datetime_of_loss}
_symbol_loss_cooldown: dict[str, datetime] = {}

# Loss-cap notification dedup — tracks which cap reasons we already alerted.
# Cleared when the cap resets (new UTC day / week).
_loss_cap_notified: set[str] = set()

# Persistent cooldown file — survives restarts
_COOLDOWN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "loss_cooldown.json"
)


# ── Open-position persistence ────────────────────────────────────────────────
# OrderEngine keeps `active` in MEMORY only, so a restart used to lose every
# open position. Two separate mechanisms destroyed them:
#
#   1. SIGTERM ran _force_close_all(reason="SHUTDOWN"), closing everything.
#   2. Even without that, OrderEngine.__init__ starts with active=[], so
#      reconcile_with_exchange() would see the still-open Binance positions as
#      ORPHANS and market-close them on the first cycle.
#
# Persisting `active` and reloading it BEFORE the first reconcile fixes both:
# the positions are known, so they are not orphans, and the strategy's own
# manage() resumes on them.
_POSITIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "open_positions.json"
)

# Close all positions on SIGTERM. Default TRUE — the safe behaviour, and what
# the bot has always done. Set false to survive restarts.
CLOSE_ON_SHUTDOWN = cfg.get("CLOSE_ON_SHUTDOWN")

# Refuse to resume positions older than this. A position left unmanaged for
# hours has had no SL/TP checks from the bot; in LIVE the exchange stop still
# protects it, but in PAPER nothing does. Resuming a very stale position is
# usually worse than booking it.
MAX_RESUME_AGE_HOURS = cfg.get("MAX_RESUME_AGE_HOURS")


def _save_positions(live) -> None:
    """Persist open positions so a restart can resume them."""
    try:
        os.makedirs(os.path.dirname(_POSITIONS_FILE), exist_ok=True)
        with open(_POSITIONS_FILE, "w") as f:
            json.dump({
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "mode": TRADE_MODE,
                "positions": live.active,
            }, f, indent=2, default=str)
    except Exception as exc:
        log.warning(f"Could not persist open positions: {exc}")


def _load_positions(live) -> int:
    """
    Restore open positions saved by a previous run. Returns the count restored.

    Must run BEFORE the first reconcile_with_exchange(), or the restored
    positions are treated as orphans and closed.
    """
    if not os.path.exists(_POSITIONS_FILE):
        return 0
    try:
        with open(_POSITIONS_FILE) as f:
            blob = json.load(f)
    except Exception as exc:
        log.warning(f"Could not read {_POSITIONS_FILE}: {exc}")
        return 0

    saved = blob.get("positions") or []
    if not saved:
        return 0

    # A PAPER file must not be resumed into a LIVE run, or the bot would
    # believe it holds positions the exchange has never heard of.
    saved_mode = blob.get("mode", TRADE_MODE)
    if saved_mode != TRADE_MODE:
        log.warning(
            f"Saved positions are from {saved_mode} mode but this run is "
            f"{TRADE_MODE} — NOT resuming {len(saved)} position(s)."
        )
        return 0

    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(blob["saved_at"])).total_seconds() / 3600
    except Exception:
        age_h = 0.0
    if age_h > MAX_RESUME_AGE_HOURS:
        log.warning(
            f"Saved positions are {age_h:.1f}h old (limit {MAX_RESUME_AGE_HOURS}h) "
            f"— NOT resuming {len(saved)} position(s). Check Binance manually."
        )
        notify_error(
            f"⚠️ {len(saved)} saved position(s) were {age_h:.1f}h old and were "
            f"NOT resumed.\nCheck Binance for open positions."
        )
        return 0

    live.active.extend(saved)
    log.warning(
        f"Resumed {len(saved)} open position(s) from the previous run "
        f"({age_h:.1f}h old): "
        + ", ".join(f"{p.get('symbol')} {p.get('direction')}" for p in saved)
    )
    return len(saved)


def _load_cooldown() -> None:
    """Load symbol loss cooldown from disk into _symbol_loss_cooldown."""
    global _symbol_loss_cooldown
    try:
        if os.path.exists(_COOLDOWN_FILE):
            with open(_COOLDOWN_FILE, encoding="utf-8") as _jf:
                raw = json.load(_jf)
            now = datetime.now(timezone.utc)
            cutoff_secs = LOSS_COOLDOWN_MINUTES * 60
            loaded = {}
            for sym, ts_str in raw.items():
                try:
                    ts = datetime.fromisoformat(ts_str)
                    # Only restore cooldowns that are still active
                    if (now - ts).total_seconds() < cutoff_secs:
                        loaded[sym] = ts
                except Exception:
                    pass
            _symbol_loss_cooldown = loaded
            if loaded:
                log.info(
                    f"Loaded {len(loaded)} active symbol cooldowns from disk"
                )
    except Exception as exc:
        log.warning(f"Cooldown load failed: {exc}")


def _save_cooldown() -> None:
    """Persist symbol loss cooldown dict to disk."""
    try:
        os.makedirs(os.path.dirname(_COOLDOWN_FILE), exist_ok=True)
        data = {sym: ts.isoformat() for sym, ts in _symbol_loss_cooldown.items()}
        with open(_COOLDOWN_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning(f"Cooldown save failed: {exc}")

# Regime change timestamp (for age gate)
_regime_changed_at: datetime = datetime.now(timezone.utc)

# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown flag
# ─────────────────────────────────────────────────────────────────────────────
_shutdown = False

def _handle_sigterm(signum, frame):
    global _shutdown
    log.warning("SIGTERM received — initiating graceful shutdown...")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigterm)


# ─────────────────────────────────────────────────────────────────────────────
# Position management
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Trade context capture
# ─────────────────────────────────────────────────────────────────────────────
#
# Everything known about a trade at entry and at exit is stamped onto the
# position dict, so the ledger (live_logger.log_exit), the ML outcome row and
# the dashboard all carry it. Before this, the ledger had no regime at all and
# the ML log only had the entry regime, which made "how does CSM do in
# BEAR_TREND" a reconstruction job from REGIME_CHANGE events.

# Latest regime dict from classify_regime(); refreshed every full cycle so the
# exit path can stamp the regime in force when a trade closes.
_current_regime: dict = {}

# Settings that shape a trade, snapshotted at entry so trades stay comparable
# across config changes.
_ENTRY_SETTINGS_KEYS = (
    "GLOBAL_LEVERAGE", "MAX_LEVERAGED_LOSS_PCT", "MIN_STRENGTH", "MAX_CONCURRENT",
    "MAX_PER_STRATEGY", "KRONOS_GATE", "KRONOS_PF_THR",
    "CSM_MOM_LO", "CSM_MOM_HI", "CSM_SL_ATR_LONG", "CSM_SL_ATR_SHORT",
    "CSM_TP_ATR_LONG", "CSM_TP_ATR_SHORT", "CSM_PROFIT_LADDER", "CSM_MAX_HOLD_MIN",
    "NASOS_SL_MODE", "NASOS_SL_FLAT", "NASOS_TP_ATR",
)


def _stamp_entry_context(pos: dict, sig: dict, size: dict, regime: dict,
                         regime_age_min: float, ml_adj: dict, liq_price: float,
                         n_open_before: int) -> None:
    """Record the full decision context on the position at open time."""
    try:
        now = datetime.now(timezone.utc)
        sig_px = float(sig.get("entry_price") or 0.0)
        fill   = float(pos.get("entry_price") or 0.0)
        sign   = 1.0 if pos.get("direction") == "LONG" else -1.0
        pos.update({
            # market context
            "regime_entry":         regime.get("regime"),
            "regime_age_min_entry": round(float(regime_age_min), 1),
            "btc_price_entry":      regime.get("btc_price"),
            "btc_trend_entry":      regime.get("btc_trend"),
            "eth_trend_entry":      regime.get("eth_trend"),
            "sol_trend_entry":      regime.get("sol_trend"),
            "funding_entry":        regime.get("funding"),
            "hour_utc_entry":       now.hour,
            "dow_entry":            now.weekday(),
            "n_open_before":        n_open_before,
            # the signal as the strategy produced it
            "signal_price":         sig_px,
            "signal_strength":      sig.get("strength"),
            "signal_reason":        sig.get("reason"),
            "normalized_mom":       sig.get("normalized_mom"),
            "vol_ratio":            sig.get("vol_ratio"),
            "sl_pct_planned":       size.get("sl_pct"),
            # adverse fill vs the signal price, as a fraction (positive = worse)
            "fill_slippage_pct":    round(sign * (fill - sig_px) / sig_px, 6) if sig_px > 0 else None,
            "liq_price":            liq_price,
            # Kronos
            "kronos_pred_fav":      sig.get("kronos_pred_fav"),
            "kronos_ts":            sig.get("kronos_ts"),
            # ML engine
            "ml_risk_mult":         ml_adj.get("risk_mult"),
            "ml_win_prob":          ml_adj.get("win_prob"),
            "ml_gate_action":       ml_adj.get("gate_action"),
            "ml_sl_atr_mult":       ml_adj.get("sl_atr_mult"),
            # settings in force
            "settings_entry":       {k: cfg.get(k) for k in _ENTRY_SETTINGS_KEYS},
            # excursion tracking (updated every manage tick)
            "mfe": 0.0, "mae": 0.0, "sl_moves": 0,
        })
    except Exception as exc:
        log.warning(f"entry context stamp failed [{pos.get('symbol')}]: {exc}")


def _track_excursion(pos: dict, df_1m) -> None:
    """Peak favourable / adverse excursion since entry, as fractions of entry.

    Uses the last bar's high/low, clamped to the close on the entry bar so a
    pre-fill wick cannot leak in (same guard CSM uses for its HWM).
    """
    try:
        bar   = df_1m.iloc[-1]
        entry = float(pos["entry_price"])
        close = float(bar["close"]); hi = float(bar["high"]); lo = float(bar["low"])
        try:
            bar_ts = pd.to_datetime(bar["timestamp"] if "timestamp" in bar else bar.name, utc=True)
            if bar_ts <= pd.to_datetime(pos["entry_time"], utc=True):
                hi = lo = close
        except Exception:
            pass
        if pos.get("direction") == "LONG":
            fav, adv = (hi - entry) / entry, (entry - lo) / entry
        else:
            fav, adv = (entry - lo) / entry, (hi - entry) / entry
        pos["mfe"] = round(max(float(pos.get("mfe") or 0.0), fav), 6)
        pos["mae"] = round(max(float(pos.get("mae") or 0.0), adv), 6)
    except Exception:
        pass


def _stamp_exit_context(pos: dict) -> None:
    """
    Write duration_min and the exit-side context into the position dict
    before log_exit() / ml_log_outcome() read it. Called on EVERY close path.

    duration_min: the dict never had this key at close time — log_exit()
    computed it internally without writing it back, so ML outcome rows had
    duration_min=0, which broke Phase 3's trail sizing.
    """
    try:
        entry_t  = datetime.fromisoformat(pos["entry_time"])
        exit_str = pos.get("exit_time")
        exit_t   = datetime.fromisoformat(exit_str) if exit_str else datetime.now(timezone.utc)
        pos["duration_min"] = round((exit_t - entry_t).total_seconds() / 60, 1)
    except Exception:
        pos.setdefault("duration_min", 0.0)
    try:
        pos.setdefault("exit_time", datetime.now(timezone.utc).isoformat())
        pos["regime_exit"]   = _current_regime.get("regime")
        pos["btc_price_exit"] = _current_regime.get("btc_price")
        pos.setdefault("mfe", round(float(pos.get("hwm") or 0.0), 6))
        pos.setdefault("mae", 0.0)
        pos.setdefault("sl_moves", 0)
        pos["regime_changed_in_trade"] = (
            pos.get("regime_entry") is not None
            and pos.get("regime_exit") is not None
            and pos["regime_entry"] != pos["regime_exit"]
        )
    except Exception as exc:
        log.warning(f"exit context stamp failed [{pos.get('symbol')}]: {exc}")


def _book_realized_pnl(pos: dict) -> None:
    """
    Apply a closed trade's net P&L to the running balance.

    LIVE  : no-op — Binance already holds the money, and ACCOUNT_EQUITY is
            refreshed from the exchange on its own schedule.
    PAPER : add the net P&L to the persisted simulated balance so equity
            compounds, position sizing scales with performance, and the
            dashboard's wallet balance reflects cumulative results.
    """
    global ACCOUNT_EQUITY
    if LIVE_ENABLED:
        return
    pnl_net = pos.get("pnl_usdt_net", pos.get("pnl_usdt", 0.0))
    try:
        ACCOUNT_EQUITY = paper_equity.record(PAPER_STARTING_EQUITY, float(pnl_net))
    except Exception as exc:
        log.warning(f"Could not update paper equity: {exc}")


def _manage_on_bar_close(strategy, pos: dict, df_1m, session_pnl: float) -> dict:
    """
    Evaluate manage() against the last COMPLETED 15m bar rather than the tick.

    Two-stage, mirroring what actually enforces each level in live:

      1. The CURRENT stop is checked against the live mark price. Binance holds
         a STOP_MARKET there and fires it intra-bar; the bot must model that or
         paper results will be optimistic in a way live never is.
      2. Everything else — TP, trailing, breakeven — is evaluated on a frame
         truncated to the last completed bar, so intra-bar noise cannot trigger
         it. This is the cadence backtest_optimizer uses.

    2026-08-11: stage 1 used to test `initial_sl_price` — correct while the
    exchange only ever held the ENTRY stop, because trailing lived in bot
    memory. order_engine.update_stop_order() now pushes every breakeven/trail
    move to Binance, so the exchange holds the CURRENT level and stage 1 must
    test that instead. Leaving it on the entry stop made paper the only one of
    the three engines without intra-bar protection on a trailed stop:

        backtest  _intrabar_exit() tests the live sl_price, fills AT it
        live      Binance STOP_MARKET at the trailed level, fills near trigger
        paper     (before this change) entry stop only -> exits at bar close

    That gap produced LITUSDT: a stop resting ABOVE entry that still booked
    -0.967%, because price broke it mid-bar and the exit landed on the close.

    The fill is taken AT the stop price, not at the observed tick. A resting
    STOP_MARKET triggers the moment price touches, so the stop level is the
    honest model of the fill; booking the bar-close price would charge paper
    for latency the exchange does not have. Real fills still slip past the
    trigger — that is what SLIPPAGE_PCT is for, not this.

    Falls back to normal tick management if no completed bar is available.
    """
    from modules.strategies.base_strategy import make_exit

    # ── 1. Exchange-enforced stop, at tick ────────────────────────────────────
    try:
        tick_px = float(df_1m["close"].iloc[-1])
        cur_sl  = float(pos.get("sl_price") or 0.0)
        if cur_sl <= 0:
            cur_sl = float(pos.get("initial_sl_price") or 0.0)
        if cur_sl > 0:
            hit = (tick_px <= cur_sl) if pos["direction"] == "LONG" \
                  else (tick_px >= cur_sl)
            if hit:
                # Name it the way stop_exit_reason() would, so a trailed-out
                # winner is not logged as a stop-loss.
                try:
                    reason = strategy.stop_exit_reason(pos)
                except Exception:
                    reason = "SL_HIT"
                return make_exit(cur_sl, reason)
    except Exception as e:
        log.warning(f"[MANAGE] tick stop-check failed for {pos.get('symbol', '?')}: {e}")

    # ── 2. Bot-side logic, on completed bars ──────────────────────────────────
    bar_df = _truncate_to_bar_close(df_1m, strategy.TRAIL_BAR_MINUTES)
    if bar_df is None or bar_df.empty:
        return strategy.manage(pos, df_1m, session_pnl)
    return strategy.manage(pos, bar_df, session_pnl)


def _check_hard_levels(pos: dict, df_1m) -> dict | None:
    """
    Enforce the position's OWN sl_price / tp_price, for every strategy.

    Returns an exit dict when a level is touched, otherwise None.

    Why this is here and not in each strategy
    -----------------------------------------
    It used to be each strategy's own responsibility, and two of the three
    production strategies never did it. Neither freqtrade port reads sl_price
    or tp_price anywhere in manage() — their 8% stop and ATR target existed
    only as numbers on the position dict, read by nothing.

    Consequences, both measured:

      * In PAPER there was no stop at ALL. A position 40% under water — five
        times past its stop — returned {'exit': False} from both ports.
      * In LIVE the exchange STOP_MARKET still capped the loss at -8%, but the
        exchange holds no take-profit, so the TARGET was unenforced in BOTH
        modes and a position could only leave via the port's own sell signal.

    Same 30d data, stop enforcement as the only variable:

        NASOS_V4    with stop   147 trades   3.0h median   worst  -8.08%
                    without      17 trades   414h median   worst -87.96%
                                 (100% of positions ran to end-of-data)

    NASOS stopped being a strategy and became buy-and-hold.

    Enforcing it at this choke point rather than per strategy makes the whole
    class of bug structurally impossible: a new strategy cannot forget to
    check a stop it does not own.

    Fill convention
    ---------------
    Fills AT the level, not at the observed tick, and a bar touching BOTH
    levels resolves to the stop. That is what a resting STOP_MARKET does, what
    _manage_on_bar_close() stage 1 does, and what the backtest harness's
    _intrabar_exit() does — so live, paper and backtest finally agree.

    Uses the bar HIGH/LOW rather than the close, so a touch between fetches is
    not missed. On fast cycles data_hub folds the live mark price into that
    high/low, so this sees the tick.
    """
    if df_1m is None or getattr(df_1m, "empty", True):
        return None

    from modules.strategies.base_strategy import BaseStrategy, make_exit

    try:
        bar     = df_1m.iloc[-1]
        hi, lo  = float(bar["high"]), float(bar["low"])
        is_long = pos.get("direction") == "LONG"
        sl      = float(pos.get("sl_price") or 0.0)
        tp      = float(pos.get("tp_price") or 0.0)

        # Stop first — it wins a bar that touches both (pessimistic).
        if sl > 0 and ((lo <= sl) if is_long else (hi >= sl)):
            # Name it the way the strategy would, so a trailed-out winner is
            # not logged as a stop-loss.
            try:
                reason = BaseStrategy.stop_exit_reason(pos)
            except Exception:
                reason = "SL_HIT"
            return make_exit(sl, reason)

        if tp > 0 and ((hi >= tp) if is_long else (lo <= tp)):
            return make_exit(tp, "TP_HIT")

    except Exception as exc:
        # Fail OPEN: a bad frame must not fabricate an exit. The strategy's own
        # manage() still runs below, and in LIVE the exchange stop is untouched.
        log.warning(f"[LEVELS] check failed for {pos.get('symbol', '?')}: {exc}")

    return None


def _truncate_to_bar_close(df, minutes: int = 15):
    """
    Cut a 1m frame down to the last COMPLETED `minutes`-bar close.

    Drops the synthetic mark-price row data_hub appends (volume 0) so a live
    tick is never mistaken for a bar close, then keeps everything up to and
    including the last 1m candle that ends on a bar boundary.
    """
    try:
        if df is None or df.empty or "timestamp" not in df.columns:
            return None
        d = df[df["volume"] > 0] if "volume" in df.columns else df
        if d.empty:
            return None
        # The final row is the candle Binance is still building — see the
        # matching note in BaseStrategy.trail_reference_price(). Without this,
        # the row picked out below as a completed bar close is a live tick for
        # one minute in every fifteen, which is exactly what bar-close
        # management is supposed to exclude.
        d = d.iloc[:-1]
        if d.empty:
            return None
        n = max(1, int(minutes))
        closes = d.index[d["timestamp"].dt.minute % n == (n - 1)]
        if len(closes) == 0:
            return None
        return d.loc[:closes[-1]]
    except Exception:
        return None


def _positions_view(active: list, symbol_data: dict) -> list:
    """
    Build a dashboard-facing copy of the open positions with live unrealized
    P&L and elapsed duration filled in.

    Both fields are otherwise dead on an OPEN position: pnl_equity_pct is set
    to 0.0 by open_position() and only written again by close_position(), and
    duration_min is only written by _stamp_exit_context() on the close paths.
    The dashboard therefore showed 0.00% / 0 min for the whole life of a trade.

    Returns copies — the real position dicts are never mutated, so nothing here
    can corrupt the P&L that close_position() computes for accounting.
    """
    now  = datetime.now(timezone.utc)
    view = []

    for pos in active:
        p = dict(pos)

        # Elapsed time since entry
        try:
            entry_t = datetime.fromisoformat(pos["entry_time"])
            p["duration_min"] = round((now - entry_t).total_seconds() / 60, 1)
        except Exception:
            p["duration_min"] = 0.0

        # Mark to the latest 1m close, when we have data for this symbol
        sym_data = (symbol_data or {}).get(pos["symbol"])
        last_px  = None
        if sym_data and sym_data[0] is not None and not sym_data[0].empty:
            try:
                last_px = float(sym_data[0]["close"].iloc[-1])
            except Exception:
                last_px = None

        if last_px:
            entry = float(pos["entry_price"])
            sign  = 1.0 if pos["direction"] == "LONG" else -1.0
            p["current_price"] = last_px
            p["pnl_pct"]       = round(sign * (last_px - entry) / entry, 6)
            pnl_usdt           = float(pos.get("contracts", 0.0)) * (last_px - entry) * sign
            p["pnl_usdt"]      = round(pnl_usdt, 4)
            # Gross of fees — this is an unrealized mark, not a booked result.
            p["pnl_equity_pct"] = round(
                pnl_usdt / ACCOUNT_EQUITY, 6
            ) if ACCOUNT_EQUITY > 0 else 0.0
            margin = float(pos.get("margin_req", 0.0))
            p["roi_pct"] = round(pnl_usdt / margin, 6) if margin > 0 else 0.0

        view.append(p)

    return view


def _manage_positions(
    live:        OrderEngine,
    symbol_data: dict,
    logger:      LiveLogger,
) -> None:
    """
    Call manage() on every open live position.
    Closes positions that trigger SL/TP/trail/hard-stop.
    Records P&L and loss cooldown on close.
    """
    for pos in list(live.active):   # copy — list shrinks mid-loop on close
        sym   = pos["symbol"]
        df_1m = symbol_data.get(sym, (None, None, None))[0]
        if df_1m is None or df_1m.empty:
            # Data fetch failed — SL/TP checks are SKIPPED this cycle.
            # Track consecutive failures per symbol to detect persistent issues.
            fail_key = f"_data_fail_{sym}"
            fails    = pos.get(fail_key, 0) + 1
            pos[fail_key] = fails
            if fails == 1 or fails % 10 == 0:
                log.warning(
                    f"[MANAGE] {sym} data unavailable — "
                    f"SL/TP check SKIPPED (consecutive: {fails})"
                )
            if fails >= 30:
                notify_error(
                    f"⚠️ {sym} data fetch failed {fails}× in a row\n"
                    f"SL checks are being skipped — check Binance manually"
                )
            continue
        # Reset failure counter on success
        pos.pop(f"_data_fail_{sym}", None)
        _track_excursion(pos, df_1m)

        # ── Hard leveraged-loss ceiling ──────────────────────────────────────
        # Closes the position the moment its ROI (price move x leverage — the
        # figure Binance shows on the position card) reaches MAX_TRADE_LOSS_PCT,
        # whatever the strategy's own stop says.
        #
        # Checked at TICK resolution and BEFORE strategy.manage(), because this
        # is a risk backstop: it must not wait for a bar close, and it must win
        # over any strategy that would hold on.
        #
        # Consequence worth knowing: at high leverage this fires on a small
        # price move (5x -> -0.60%), so on losing trades the strategy's own exit
        # machinery — breakeven, trailing, the CSM ladder — never runs.
        if MAX_TRADE_LOSS_PCT > 0:
            try:
                _px    = float(df_1m["close"].iloc[-1])
                _entry = float(pos["entry_price"])
                _sign  = 1.0 if pos.get("direction") == "LONG" else -1.0
                _move  = _sign * (_px - _entry) / _entry
                _lev   = float(pos.get("leverage", 1) or 1)
                _roi   = _move * _lev
                if _roi <= -MAX_TRADE_LOSS_PCT:
                    log.warning(
                        f"[MAX_LOSS] {sym} {pos.get('direction')} ROI "
                        f"{_roi*100:.2f}% (price {_move*100:.2f}% x{_lev:.0f}) "
                        f"hit the {MAX_TRADE_LOSS_PCT*100:.1f}% ceiling — closing"
                    )
                    ok = live.close_position(pos, _px, "MAX_LOSS",
                                             account_equity=ACCOUNT_EQUITY)
                    if ok is True:
                        # BEFORE log_exit/notify_exit: both read duration_min, and this is
                        # what writes it. Called after them, every exit notification
                        # reported "Duration: 0.0 min" while the web ledger (which
                        # recomputes it internally) showed the true value.
                        _stamp_exit_context(pos)
                        logger.log_exit(pos)
                        notify_exit(pos, TRADE_MODE)
                        _book_realized_pnl(pos)
                        record_trade_pnl(pos.get("pnl_equity_pct",
                                                 pos.get("pnl_pct", 0.0)))
                        ml_log_outcome(pos)
                        _symbol_loss_cooldown[sym] = datetime.now(timezone.utc)
                        _save_cooldown()
                    elif ok is False:
                        notify_error(
                            f"❗ MAX_LOSS close FAILED: {sym} {pos.get('direction')}\n"
                            f"ROI {_roi*100:.2f}% — check Binance, manual close needed"
                        )
                    continue
            except Exception as exc:
                log.warning(f"[MAX_LOSS] check failed for {sym}: {exc}")

        # Snapshot the stop BEFORE manage() runs — breakeven and trailing
        # mutate pos["sl_price"] in place, and the exchange stop has to follow
        # it or the trailed level exists only in this process. See
        # order_engine.update_stop_order().
        _sl_before = float(pos.get("sl_price") or 0.0)

        # ── Universal stop / target enforcement ──────────────────────────────
        # Runs BEFORE the strategy's own manage(), for EVERY strategy. See
        # _check_hard_levels() for why this cannot live inside the strategies.
        #
        # Ordering is deliberate: if a level is already touched there is
        # nothing for manage() to usefully do — moving a stop that has been
        # breached is moot, and the exchange has already filled in LIVE.
        #
        # This also changes CSM's fill convention: CSM's own manage() books the
        # observed tick, whereas this books the level. The level is what the
        # exchange, _manage_on_bar_close() and the backtest harness all use, so
        # CSM was the outlier — this brings it into line rather than away.
        strategy = StrategyFactory.get(pos.get("strategy", ""))
        result   = _check_hard_levels(pos, df_1m)

        if result is not None:
            pass
        elif strategy is None:
            # Adopted position — use a default 2.5% hard stop manage
            from modules.strategies.base_strategy import no_exit, make_exit
            entry     = pos["entry_price"]
            direction = pos["direction"]
            latest    = df_1m.iloc[-1]
            c_close   = float(latest["close"])
            pnl = (c_close - entry) / entry if direction == "LONG" \
                  else (entry - c_close) / entry
            if pnl <= -0.025:
                result = make_exit(c_close, "HARD_STOP")
            else:
                c_high = float(latest["high"])
                c_low  = float(latest["low"])
                sl     = pos.get("sl_price", 0.0)
                if direction == "LONG" and sl > 0 and c_low <= sl:
                    result = make_exit(sl, "SL_HIT")
                elif direction == "SHORT" and sl > 0 and c_high >= sl:
                    result = make_exit(sl, "SL_HIT")
                else:
                    result = no_exit()
        else:
            session_pnl = live.session_pnl_pct
            if MANAGE_ON_BAR_CLOSE:
                result = _manage_on_bar_close(strategy, pos, df_1m, session_pnl)
            else:
                result = strategy.manage(pos, df_1m, session_pnl)

        # LLM Advisor post-trade exit removed 2026-08-11 — see docs/LLM_REMOVED.md.
        # It could force an exit at market on a CHOPPY reading, an exit path the
        # backtest has no equivalent for. Exits are now strategy.manage() plus
        # the exchange stop only, matching the backtest exactly.
        # (Removed exit reasons: LLM_CHOPPY_EXIT, LLM_BIAS_FLIP — historical
        #  trade logs may still contain them.)

        # ── Keep the server-side stop in step with the bot-side stop ─────────
        # If manage() moved sl_price (breakeven armed, or the trail stepped up)
        # push it to Binance so the level is enforced at TICK level rather than
        # only when manage() next runs. Without this the trailed stop is bot
        # memory only: price can break it mid-bar and the exit lands wherever
        # the bar happens to close. Measured cost of that gap across 63 live
        # stop exits was -1.82 USDT, ~14% of gross P&L.
        #
        # No-op in paper (update_stop_order returns False when LIVE_ENABLED is
        # false), and it never widens a stop.
        if not result.get("exit"):
            _sl_after = float(pos.get("sl_price") or 0.0)
            if _sl_after > 0 and _sl_after != _sl_before:
                pos["sl_moves"] = int(pos.get("sl_moves", 0)) + 1
                try:
                    update_stop_order(pos, _sl_after)
                except Exception as exc:
                    # A failed move leaves the previous stop live, so this is
                    # degraded protection, not absent protection.
                    log.warning(f"[STOP] Trail sync failed [{pos.get('symbol')}]: {exc}")
            continue

        ep     = result["exit_price"]
        reason = result["exit_reason"]

        ok = live.close_position(pos, ep, reason, account_equity=ACCOUNT_EQUITY)

        if ok is True:
            # BEFORE log_exit/notify_exit: both read duration_min, and this is
            # what writes it. Called after them, every exit notification
            # reported "Duration: 0.0 min" while the web ledger (which
            # recomputes it internally) showed the true value.
            _stamp_exit_context(pos)
            logger.log_exit(pos)
            notify_exit(pos, TRADE_MODE)
            _book_realized_pnl(pos)
            record_trade_pnl(pos.get("pnl_equity_pct", pos.get("pnl_pct", 0.0)))
            ml_log_outcome(pos)           # ML Phase 1: log trade outcome
            # Check if a milestone was just reached (50/100/200 trades)
            _ml_alert = check_milestone_alert()
            if _ml_alert:
                notify_error(_ml_alert)  # reuse error channel for alerts
            # Register loss cooldown
            if pos.get("pnl_equity_pct", 0.0) < 0:
                _symbol_loss_cooldown[sym] = datetime.now(timezone.utc)
                _save_cooldown()
                log.info(
                    f"[Cooldown] {sym} loss registered — "
                    f"blocked for {LOSS_COOLDOWN_MINUTES}min"
                )
        elif ok is False:
            fail_count = pos.get("close_fail_count", 0)
            notify_error(
                f"❗ Close order FAILED (attempt #{fail_count})\n"
                f"Symbol: {sym} {pos.get('direction')}\n"
                f"Reason: {reason}\n"
                f"Check Binance — manual close may be needed"
            )
        # ok is None → position gone (liquidated/manual) — reconciler handles


def _force_close_all(
    live:        OrderEngine,
    symbol_data: dict,
    logger:      LiveLogger,
    reason:      str = "SHUTDOWN",
) -> None:
    """Close all open live positions at current market price."""
    log.warning(f"Force-closing all positions. Reason: {reason}")
    for pos in list(live.active):
        sym   = pos["symbol"]
        df_1m = symbol_data.get(sym, (None, None, None))[0]
        ep    = float(df_1m["close"].iloc[-1]) \
                if (df_1m is not None and not df_1m.empty) \
                else pos["entry_price"]

        ok = live.close_position(pos, ep, reason, account_equity=ACCOUNT_EQUITY)
        if ok is True:
            # BEFORE log_exit/notify_exit: both read duration_min, and this is
            # what writes it. Called after them, every exit notification
            # reported "Duration: 0.0 min" while the web ledger (which
            # recomputes it internally) showed the true value.
            _stamp_exit_context(pos)
            logger.log_exit(pos)
            notify_exit(pos, TRADE_MODE)
            _book_realized_pnl(pos)
            record_trade_pnl(pos.get("pnl_equity_pct", pos.get("pnl_pct", 0.0)))
            ml_log_outcome(pos)
        elif ok is False:
            notify_error(
                f"❗ Force-close FAILED during {reason}\n"
                f"Symbol: {sym} {pos.get('direction')}\n"
                f"Check Binance — manual close may be needed"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Signal scanning
# ─────────────────────────────────────────────────────────────────────────────

def _scan_for_signals(
    symbols:     list[str],
    symbol_data: dict,
    regime:      dict,
    live:        OrderEngine,
) -> list[dict]:
    """
    Run all permitted strategies across all symbols.
    Returns signals ranked by strength (desc).
    """


    regime_name      = regime.get("regime", "RANGING")
    permitted_strats = StrategyFactory.get_permitted(regime, REGIME_STRATEGY_PERMISSIONS)

    if not permitted_strats:
        log.info(f"No strategies permitted in regime: {regime_name}")
        return []

    # Regime age gate — no entries for MIN_REGIME_AGE_MINUTES after a change
    regime_age_min = (
        datetime.now(timezone.utc) - _regime_changed_at
    ).total_seconds() / 60
    if regime_age_min < MIN_REGIME_AGE_MINUTES:
        log.info(
            f"Regime {regime_name} only {regime_age_min:.1f}min old "
            f"(min {MIN_REGIME_AGE_MINUTES}min) — waiting for stability"
        )
        return []

    now = datetime.now(timezone.utc)
    candidates = []
    near_misses = {}   # strategy_id -> best near-miss dict this cycle

    # ── Load the list gates ONCE per cycle ────────────────────────────────────
    # is_blacklisted()/is_allowed() re-read and re-parse their JSON on EVERY
    # call. At 150 symbols x 3 strategies that is 450 disk reads — measured at
    # 71ms per scan versus 0.2ms when hoisted here (382x). symbol_filter carries
    # the same warning for the same reason.
    #
    # Both fail OPEN. A corrupt or unreadable list must not silently halt
    # trading; an empty whitelist means "allow everything", never "allow
    # nothing".
    try:
        from modules.blacklist import _load as _bl_load
        _bl = _bl_load()
    except Exception as exc:
        log.warning(f"[Blacklist] load failed, not filtering: {exc}")
        _bl = {}
    try:
        from modules.whitelist import _load as _wl_load, _allowed_in as _wl_allowed
        _wl = _wl_load()
    except Exception as exc:
        log.warning(f"[Whitelist] load failed, not filtering: {exc}")
        _wl, _wl_allowed = {}, (lambda *_a: True)
    # AUTO blacklist — coins that repeatedly hard-stop (3 in 72h, or cumulative
    # loss past threshold). 7-day ban escalating to permanent on a 2nd offence.
    # order_engine.record_hard_stop() has always been WRITING these bans, but
    # the only reader was symbol_filter.get_top_symbols(), which FOCUSED_MODE
    # never calls — so the circuit breaker recorded faithfully and protected
    # nothing. get_blacklist() also expires stale entries as a side effect, so
    # calling it here keeps the file self-cleaning.
    try:
        from modules.symbol_blacklist import get_blacklist as _auto_bl_load
        _abl = set(_auto_bl_load())
    except Exception as exc:
        log.warning(f"[AutoBlacklist] load failed, not filtering: {exc}")
        _abl = set()
    if _bl or _wl or _abl:
        log.info(f"[Lists] blacklist={len(_bl)} | auto-blacklist={len(_abl)} | "
                 f"whitelist={len(_wl)}"
                 f"{' (RESTRICTING)' if _wl else ''}")

    for sym in symbols:
        data = symbol_data.get(sym)
        if data is None:
            continue
        df_1m, df_15m, df_1h = data

        # Skip symbols already open
        if live.is_symbol_active(sym):
            continue

        # Auto-blacklist is symbol-level, so test it once here rather than once
        # per strategy inside the loop below.
        if sym.upper() in _abl:
            log.debug(f"[AutoBlacklist] {sym} banned (repeated hard stops)")
            continue

        # Skip symbols in loss cooldown
        last_loss = _symbol_loss_cooldown.get(sym)
        if last_loss is not None:
            elapsed = (now - last_loss).total_seconds() / 60
            if elapsed < LOSS_COOLDOWN_MINUTES:
                remaining = LOSS_COOLDOWN_MINUTES - elapsed
                log.debug(
                    f"[Cooldown] {sym} blocked {remaining:.0f}min remaining"
                )
                continue

        for strategy in permitted_strats:
            # ── List gates ────────────────────────────────────────────────────
            # Checked HERE because _scan_for_signals is the one choke point both
            # FOCUSED_MODE and top-N mode pass through. symbol_filter enforces a
            # blacklist too, but only inside get_top_symbols(), which
            # FOCUSED_MODE=true never calls — so before this, blacklisting a
            # coin had no effect on the running config at all.
            #
            # Blacklist WINS over whitelist: deny beats allow, so a stale
            # whitelist entry can never resurrect a coin you deliberately
            # banned. Both dicts are loaded once per cycle above.
            _sid = strategy.STRATEGY_ID
            if _bl:
                _e = _bl.get(sym.upper())
                if _e and ("*" in _e or _sid.upper() in [x.upper() for x in _e]):
                    log.debug(f"[Blacklist] {sym} blocked for {_sid}")
                    continue
            if _wl and not _wl_allowed(_wl, sym, _sid):
                log.debug(f"[Whitelist] {sym} not permitted for {_sid}")
                continue

            try:
                sig = strategy.scan(sym, df_1m, df_15m, df_1h, regime)
                if sig is None:
                    continue
                if sig.get("near_miss"):
                    # Track the closest near-miss per strategy for the cycle summary
                    sid = strategy.STRATEGY_ID
                    existing = near_misses.get(sid)
                    if existing is None or sig.get("closeness", 0) > existing.get("closeness", 0):
                        near_misses[sid] = sig
                elif sig.get("strength", 1.0) >= MIN_STRENGTH:
                    candidates.append(sig)
            except Exception as exc:
                log.warning(f"scan() exception [{sym} {strategy.STRATEGY_ID}]: {exc}")

    if not candidates and near_misses:
        parts = [f"{sid}: {nm['symbol']} {nm['detail']}"
                 for sid, nm in sorted(near_misses.items())]
        log.info(f"[NEAR-MISS] {' | '.join(parts)}")

    candidates.sort(key=lambda s: s.get("strength", 1.0), reverse=True)

    # Queue Kronos candidates AFTER the sort. The worker scores strictly in
    # queue order and _execute_entries() tries candidates strongest-first, so
    # queuing inside the scan loop left the candidate that actually executes
    # waiting behind weaker ones — the cause of the 12-27s score latencies and
    # the fail-open entries in EXPERIMENT_LOG 0.5.15 / 0.5.18. Cost: the worker
    # loses the tail of the scan loop as head start (sub-2s, CPU-only work on
    # already-fetched data); buys the top-ranked candidate first place in line.
    for _c in candidates:
        if _c.get("strategy") in KRONOS_SHADOW_STRATEGIES:
            try:
                from kronos.shadow_client import log_candidate
                _c["kronos_ts"] = log_candidate(
                    _c.get("symbol"),
                    _c.get("direction"),
                    _c.get("entry_price"),
                    _c.get("strength"),
                    strategy=_c.get("strategy"),
                )
            except Exception:
                pass

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Kronos gate
# ─────────────────────────────────────────────────────────────────────────────
#
# The Kronos worker (kronos/shadow_worker.py, own venv, own process) scores
# every CSM candidate the scan queues. With KRONOS_GATE=live a CSM entry is
# skipped when its pred_fav is below KRONOS_PF_THR. Evidence and the decision
# rule are in docs/EXPERIMENT_LOG.md 0.4 (N=81: blocked set PF 0.55).
#
# FAIL-OPEN by design: no score within KRONOS_GATE_WAIT_SEC (worker down,
# model reloading, insufficient context) means the entry proceeds exactly as
# before the gate existed, and no further waiting happens that cycle so a dead
# worker costs one timeout, not one per signal.

# Strategies whose candidates are queued for Kronos scoring (shadow data), and
# the subset the live gate actually acts on. NASOS_V4 is shadow-only until it
# has its own verdict — the 0.025 threshold was measured on CSM and does not
# transfer. Module constants — changing them needs a restart; deliberately
# not a hot setting so the gated set cannot be widened from a bot command.
KRONOS_SHADOW_STRATEGIES = ("CSM", "NASOS_V4", "TSMOM_4H", "REBALANCING_PREMIUM")
KRONOS_GATE_STRATEGIES   = ("CSM",)


def _kronos_gate(sig: dict, waited_out: dict) -> bool:
    """True = entry may proceed. Only KRONOS_GATE_STRATEGIES are ever gated."""
    if sig.get("strategy") not in KRONOS_GATE_STRATEGIES:
        return True
    mode = cfg.get("KRONOS_GATE")
    if mode == "off":
        return True

    sym, ts = sig.get("symbol"), sig.get("kronos_ts")
    score = None
    if ts:
        try:
            from kronos.shadow_client import wait_for_score
            wait = 0 if waited_out.get("hit") else cfg.get("KRONOS_GATE_WAIT_SEC")
            score = wait_for_score(sym, ts, wait)
        except Exception as exc:
            log.warning(f"[KRONOS] score lookup failed [{sym}]: {exc}")

    if score is None or score.get("err") or score.get("pred_fav") is None:
        if not waited_out.get("hit"):
            waited_out["hit"] = True
            log.warning(
                f"[KRONOS] no score for {sym} within "
                f"{cfg.get('KRONOS_GATE_WAIT_SEC')}s "
                f"({(score or {}).get('err', 'worker silent')}) — fail-open, "
                f"not waiting again this cycle"
            )
        sig["kronos_pred_fav"] = None
        return True

    pf  = float(score["pred_fav"])
    thr = cfg.get("KRONOS_PF_THR")
    sig["kronos_pred_fav"] = pf
    if pf >= thr:
        log.info(f"[KRONOS] {sym} pred_fav={pf:+.4f} >= {thr:.3f} — pass")
        return True

    if mode == "shadow":
        log.info(f"[KRONOS] shadow: {sym} pred_fav={pf:+.4f} < {thr:.3f} — "
                 f"would gate, trading anyway")
        return True

    # Journal only. A block is the gate's normal outcome ~76% of the time
    # (8-12 per cycle); the entry notification already reports what traded
    # and shadow_scores.jsonl holds every verdict, so a per-block Telegram
    # ping was pure noise and arrived labelled "Bot Error".
    log.info(f"[KRONOS] GATED {sym} {sig.get('direction')} pred_fav={pf:+.4f} "
             f"< {thr:.3f} — entry skipped")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Entry execution
# ─────────────────────────────────────────────────────────────────────────────

def _execute_entries(
    signals:     list[dict],
    live:        OrderEngine,
    logger:      LiveLogger,
    regime:      dict,
    symbol_data: dict | None = None,
) -> int:
    """
    Open live positions for top signals. Returns count of entries made.
    Risk gates re-checked per signal.
    ML engine called per signal for feature logging and adaptive sizing.
    """
    entries = 0
    _kronos_waited_out: dict = {}    # one scoring timeout per cycle, see _kronos_gate

    # Regime age (for ML features)
    regime_age_min = (
        datetime.now(timezone.utc) - _regime_changed_at
    ).total_seconds() / 60

    for sig in signals:
        if entries >= MAX_ENTRIES_PER_CYCLE:
            break

        # ── Loss cap gate (persistent) ────────────────────────────────────────
        cap_hit, cap_reason = is_loss_cap_hit()
        if cap_hit:
            log.warning(f"Loss cap active — no new entries: {cap_reason}")
            break

        # ── Session floor gate ────────────────────────────────────────────────
        if is_daily_floor_hit(live.session_pnl_pct):
            log.warning(
                f"Session floor hit ({live.session_pnl_pct*100:.2f}%) "
                f"— no new entries"
            )
            break

        # ── Concurrent position cap ───────────────────────────────────────────
        n_open = live.n_open()
        if is_position_cap_hit(n_open):
            log.info(f"Position cap hit ({n_open}/{MAX_CONCURRENT}) — skipping")
            break

        # ── Per-strategy slot limit ──────────────────────────────────────────
        strat_id = sig.get("strategy", "")
        if is_strategy_cap_hit(strat_id, live.active):
            log.info(f"[{sig['symbol']}] {strat_id} strategy cap reached — skipping")
            continue

        # ── Skip if symbol already open ───────────────────────────────────────
        if live.is_symbol_active(sig["symbol"]):
            continue

        # ── Kronos gate (CSM only; see _kronos_gate) ─────────────────────────
        if not _kronos_gate(sig, _kronos_waited_out):
            continue

        # ── LLM Advisor Veto — DISABLED ──────────────────────────────────────
        # Removed: The 15-min stale LLM bias was vetoing valid early breakout
        # entries, causing late entries that buy local tops. The LLM Advisor
        # still handles early exits in _manage_positions() (CHOPPY/bias flip).

        # ── ML adjustments (all phases) ─────────────────────────────────────
        # Phase 1: logs features (always)
        # Phase 2+: returns risk_mult for adaptive sizing
        # Phase 3+: may return SL/trail override
        # Phase 4+: may gate (block) low-confidence signals
        #
        # Pass the correct strategy leverage — not hardcoded 5.
        # FF and GRID use 3× — wrong leverage in ML feature log corrupts
        # sl_dist_pct and other notional-linked features for those strategies.
        from modules.risk_engine import get_leverage as _get_leverage
        _strategy_lev = _get_leverage(sig.get("strategy", ""))
        _sd  = (symbol_data or {}).get(sig["symbol"])
        _df1  = _sd[0] if _sd else None
        _df15 = _sd[1] if _sd else None
        ml_adj = get_ml_adjustments(
            sig, {"leverage": _strategy_lev}, regime, regime_age_min,
            df_1m=_df1, df_15m=_df15,
        )

        # Phase 4: signal quality gate
        if ml_adj.get("gate_action") == "SKIP":
            log.info(
                f"[ML] Signal gated: {sig['symbol']} {sig['strategy']} "
                f"P(win)={ml_adj.get('win_prob', -1):.3f}"
            )
            continue

        risk_mult = ml_adj.get("risk_mult", 1.0)

        # ── Phase 3: SL override ─────────────────────────────────────────────
        # ML may suggest a different SL ATR multiplier based on historical data.
        # Validate the resulting SL before applying:
        #   - Must produce sl_pct >= MIN_SL_PCT (0.5%) — risk_engine gate
        #   - Must produce sl_pct <= HARD_STOP_PCT (2.5%) — hard stop fires first otherwise
        ml_sl_mult = ml_adj.get("sl_atr_mult")
        if ml_sl_mult is not None and sig.get("atr", 0) > 0:
            atr       = sig["atr"]
            entry     = sig["entry_price"]
            direction = sig["direction"]
            if direction == "LONG":
                new_sl = entry - ml_sl_mult * atr
            else:
                new_sl = entry + ml_sl_mult * atr
            new_sl_pct = abs(entry - new_sl) / entry
            # Import thresholds from risk_engine
            from modules.risk_engine import min_sl_pct, HARD_STOP_PCT
            MIN_SL_PCT = min_sl_pct()
            if MIN_SL_PCT <= new_sl_pct <= HARD_STOP_PCT:
                sig["sl_price"] = new_sl
                log.debug(
                    f"[ML P3] {sig['symbol']} SL override: "
                    f"{ml_sl_mult:.2f}×ATR → {new_sl:.4f} "
                    f"({new_sl_pct*100:.2f}%)"
                )
            else:
                log.debug(
                    f"[ML P3] {sig['symbol']} SL override rejected: "
                    f"{new_sl_pct*100:.2f}% out of range "
                    f"({MIN_SL_PCT*100:.1f}%–{HARD_STOP_PCT*100:.1f}%) — "
                    f"keeping original SL"
                )

        # ── Size computation ──────────────────────────────────────────────────
        size = compute_position_size(
            account_equity = ACCOUNT_EQUITY,
            entry_price    = sig["entry_price"],
            sl_price       = sig["sl_price"],
            strategy_id    = sig["strategy"],
            risk_mult      = risk_mult,
        )
        if not size["valid"]:
            log.warning(f"Size invalid [{sig['symbol']}]: {size['reason']}")
            continue
        size["account_equity"] = ACCOUNT_EQUITY

        # ── Aggregate margin cap ──────────────────────────────────────────────
        # Checked on the PROJECTED total, not just what is already open —
        # testing existing margin alone still let three positions reach 86% of
        # equity, because each was under the ceiling at the moment it opened.
        # `continue`, not `break`: a smaller signal later in the list may still
        # fit under the cap.
        _open_margin = sum(float(p.get("margin_req", 0.0)) for p in live.active)
        _projected   = _open_margin + float(size["margin_req"])
        _ceiling     = ACCOUNT_EQUITY * MAX_TOTAL_MARGIN_PCT
        if _projected > _ceiling:
            log.info(
                f"[{sig['symbol']}] margin ${_open_margin:.2f} + "
                f"${size['margin_req']:.2f} = ${_projected:.2f} exceeds "
                f"${_ceiling:.2f} ({MAX_TOTAL_MARGIN_PCT*100:.0f}% total cap) "
                f"— skipping"
            )
            continue

        direction = sig["direction"]
        liq_price = compute_liq_price(
            entry_price = sig["entry_price"],
            leverage    = size["leverage"],
            direction   = direction,
        )

        # ── Open position ─────────────────────────────────────────────────────
        # Identical path for LIVE and PAPER. order_engine.place_order() returns
        # a simulated FILLED when LIVE_ENABLED is false, so nothing reaches
        # Binance in PAPER mode — but the position is tracked, managed by
        # SL/TP, closed, and notified exactly as a live one would be.
        # (Previously the dry-run branch only logged a line, so no position was
        # ever registered: the duplicate-symbol guard never tripped and the same
        # signal re-fired every cycle, producing no P&L to evaluate.)
        n_open_before = live.n_open()
        live_pos = live.open_position(sig, size)
        if live_pos:
            # Commit ML signal to JSONL ONLY after open succeeds — prevents
            # orphan rows (ML pre-scoring of signals that never trade).
            commit_ml_signal(ml_adj, sig, size, regime)
            # Store ML metadata on position for outcome logging + manage()
            live_pos["ml_signal_id"]  = ml_adj.get("signal_id", "")
            live_pos["ml_trail_mult"] = ml_adj.get("trail_atr_mult")
            _stamp_entry_context(live_pos, sig, size, regime, regime_age_min,
                                 ml_adj, liq_price, n_open_before)
            logger.log_signal(
                symbol      = sig["symbol"],
                strategy    = sig["strategy"],
                direction   = direction,
                entry_price = sig["entry_price"],
                sl_price    = sig["sl_price"],
                tp_price    = sig.get("tp_price"),
                leverage    = size["leverage"],
                notional    = size["notional"],
                liq_price   = liq_price,
                mode        = TRADE_MODE,
                reason      = sig.get("reason", ""),
            )
            notify_signal(sig, size, TRADE_MODE, liq_price)
            log.info(
                f"[{TRADE_MODE}] Opened {sig['symbol']} {direction} "
                f"@ {sig['entry_price']} | {sig.get('reason','')}"
            )
            entries += 1

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # _regime_changed_at MUST be declared global. Without it, the two
    # assignments below bound a main()-local instead, leaving the module
    # global pinned at import time. _scan_for_signals() and
    # _execute_entries() read the global, so the regime-age stability gate
    # only ever fired during the first MIN_REGIME_AGE_MINUTES of process
    # life and never after an actual regime change, and ml_engine was
    # trained on process uptime under the name 'regime_age_min'.
    global ACCOUNT_EQUITY, _regime_changed_at, _current_regime

    log.info("=" * 60)
    log.info("  Binance USDM Futures Bot — Starting")
    log.info(f"  Scan interval : {SCAN_INTERVAL}s (full) / {FAST_INTERVAL}s (fast)")
    log.info(f"  Account equity: ${ACCOUNT_EQUITY:,.2f} USDT (from settings.json)")
    log.info(f"  Live trading  : {'ON ⚡' if LIVE_ENABLED else 'OFF (dry run)'}")
    if FOCUSED_MODE:
        log.info(f"  Mode          : FOCUSED — top-{_FOCUSED_N} coins only")
    else:
        log.info(f"  Max symbols   : {TOP_N_SYMBOLS}")
    log.info(f"  Max concurrent: {MAX_CONCURRENT} positions")
    from modules.risk_engine import max_per_strategy
    log.info(f"  Per-strategy caps: {max_per_strategy()}")
    log.info(f"  Settings file : {cfg.SETTINGS_FILE} (hot-reloaded)")
    log.info("=" * 60)

    if not validate_credentials():
        log.error("Missing Binance credentials — cannot start. Check .env file.")
        sys.exit(1)

    # ── Binance pre-flight check ──────────────────────────────────────────────
    # Catches the most common reasons Binance "doesn't work":
    #   - LIVE_ENABLED=false  → dry run (no real orders)
    #   - Hedge mode OFF       → all orders fail with positionSide error
    #   - API key lacks Futures permission → -2015
    #   - Wrong ACCOUNT_EQUITY_USDT value
    log.info("Running Binance pre-flight checks...")
    preflight = test_binance_connection()
    for issue in preflight["issues"]:
        if "DRY RUN" in issue or "ACCOUNT_EQUITY" in issue:
            log.warning(f"[Preflight] ⚠  {issue}")
        else:
            log.error(f"[Preflight] ❌ {issue}")
    if preflight["info"]:
        inf = preflight["info"]
        log.info(
            f"[Preflight] Balance: {inf.get('balance_usdt','?')} USDT | "
            f"Hedge mode: {'ON' if inf.get('hedge_mode') else 'OFF'} | "
            f"LIVE_ENABLED: {inf.get('live_enabled')} | "
            f"canTrade: {inf.get('can_trade','?')}"
        )
    if not preflight["ok"]:
        blocking = [i for i in preflight["issues"] if "DRY RUN" not in i and "ACCOUNT_EQUITY" not in i]
        log.error("Pre-flight FAILED — blocking issues found:")
        for b in blocking:
            log.error(f"  → {b}")
        log.error("Fix the issues above then restart the bot.")
        notify_error(
            "🚨 csb pre-flight FAILED — bot stopped\n\n"
            + "\n".join(f"• {b}" for b in blocking)
        )
        sys.exit(1)

    if preflight["issues"]:  # non-blocking warnings
        notify_error(
            "⚠️ csb startup warnings (non-blocking):\n"
            + "\n".join(f"• {i}" for i in preflight["issues"])
        )

    # ── Establish starting equity ─────────────────────────────────────────────
    # LIVE : Binance is the source of truth for the CURRENT balance. The period
    #        baseline the dashboard measures P&L against is persisted separately
    #        (paper_equity.live_baseline) — setting it to the live balance here
    #        re-pinned it on every restart, so "total P&L" only ever showed the
    #        minutes since the last bounce.
    # PAPER: no exchange to ask, so apply the persisted simulated P&L on top of
    #        configured figure. Without this, paper equity never moved and a
    #        multi-day run showed no cumulative performance.
    if LIVE_ENABLED:
        live_bal = get_account_equity()
        if live_bal > 0:
            global PAPER_STARTING_EQUITY
            ACCOUNT_EQUITY = live_bal
            PAPER_STARTING_EQUITY = float(
                paper_equity.live_baseline(live_bal)["starting_equity"]
            )
            log.info(f"Account equity from Binance: ${ACCOUNT_EQUITY:.4f} USDT "
                     f"(period baseline ${PAPER_STARTING_EQUITY:.4f})")
        else:
            log.warning(
                f"Could not fetch live balance — keeping configured value "
                f"${ACCOUNT_EQUITY:.2f} USDT"
            )
    else:
        _paper = paper_equity.summary(PAPER_STARTING_EQUITY)
        ACCOUNT_EQUITY = _paper["equity"]
        log.info(
            f"Paper equity: ${ACCOUNT_EQUITY:.2f} USDT "
            f"(start ${_paper['starting_equity']:.2f}, "
            f"realized {_paper['realized_pnl_usdt']:+.2f} over "
            f"{_paper['trades']} trades)"
        )

    # ── Load persistent symbol cooldowns from last session ────────────────────
    _load_cooldown()

    # ── Initialise ────────────────────────────────────────────────────────────
    live   = OrderEngine()
    logger = LiveLogger()

    # Restore positions from a previous run BEFORE the first reconcile, or
    # reconcile_with_exchange() treats them as orphans and closes them.
    _resumed = _load_positions(live)
    if _resumed:
        notify_error(
            f"🔄 Resumed {_resumed} open position(s) after restart.\n"
            f"Management continues from the strategy's own exit logic."
        )

    if FOCUSED_MODE:
        # Focused mode: scan ONLY top-N coins — entire symbol universe
        symbols = get_focused_watchlist(_FOCUSED_N)
        if not symbols:
            log.error("Focused watchlist empty — check network / API")
            sys.exit(1)
        log.info(f"FOCUSED MODE active: scanning {len(symbols)} coins only — {symbols}")
    else:
        # Normal mode: top-200 scan with watchlist priority reordering
        symbols = get_top_symbols()
        if not symbols:
            log.error("Could not fetch symbol list — check network / API")
            sys.exit(1)
        _wl = get_watchlist()
        _wl_set = set(_wl)
        symbols = _wl + [s for s in symbols if s not in _wl_set]
        log.info(f"Symbol list loaded: {len(symbols)} symbols ({len(_wl)} watchlist priority)")

    # Preload contract LOT_SIZE stepSize & PRICE_FILTER tickSize for fast 0ms order execution
    preload_exchange_specs(symbols)

    # LLM advisor background thread removed 2026-08-11 — see docs/LLM_REMOVED.md.

    btc_ref = fetch_btc_reference()
    regime  = classify_regime(
        btc_1h = btc_ref.get("btc_1h"),
        eth_1h = btc_ref.get("eth_1h"),
        sol_1h = btc_ref.get("sol_1h"),
    )
    _current_regime    = regime
    current_regime     = regime.get("regime", "RANGING")
    _regime_changed_at = datetime.now(timezone.utc)

    # Show permitted strategies in startup message
    from modules.strategies.strategy_factory import StrategyFactory
    permitted = StrategyFactory.get_permitted(regime, REGIME_STRATEGY_PERMISSIONS)
    permitted_ids = [s.STRATEGY_ID for s in permitted]
    notify_startup(regime, len(symbols), len(symbols[:TOP_N_SYMBOLS]), permitted_ids)
    logger.log_event("STARTUP", f"Regime={current_regime} symbols={len(symbols)} scan={TOP_N_SYMBOLS}")

    ls = get_loss_status()
    log.info(
        f"Loss caps | Day: {ls['day_pnl']:+.2f}% (cap {ls['daily_cap']:.0f}%) | "
        f"Week: {ls['week_pnl']:+.2f}% (cap {ls['weekly_cap']:.0f}%)"
    )
    cap_hit, cap_reason = is_loss_cap_hit()
    if cap_hit:
        log.warning(f"LOSS CAP ALREADY ACTIVE at startup: {cap_reason}")
        _loss_cap_notified.add(cap_reason)   # don't re-spam on first cycle
        notify_error(f"🚫 Bot started with active loss cap\n{cap_reason}")

    last_symbol_refresh = time.monotonic()
    last_equity_refresh = time.monotonic()
    # Scan clock, independent of the management clock. Seeded in the past so
    # the first cycle scans immediately rather than waiting SCAN_INTERVAL.
    last_scan           = time.monotonic() - SCAN_INTERVAL
    last_report_date    = ""
    cycle = 0

    # Pre-init so regime-change → grid-dissolve branch never NameErrors
    # on cycle 1 (symbol_data is populated later in each cycle at sec. 3).
    # The dissolve branch already handles empty/missing safely via fallback
    # to pos["entry_price"].
    symbol_data: dict = {}

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────────
    while not _shutdown:
        cycle        += 1
        t_start       = time.monotonic()
        now_utc       = datetime.now(timezone.utc)
        # Pick up operator edits (dashboard / Telegram / Discord) for this
        # cycle. A change to the universe shape forces the hourly symbol
        # refresh below to run now rather than up to an hour later.
        if _refresh_settings() & set(_UNIVERSE_KEYS):
            last_symbol_refresh = time.monotonic() - 3601

        # ── Scan / manage decoupling ──────────────────────────────────────────
        # Management and scanning run on independent clocks.
        #
        # Previously `is_fast = has_open`, which meant a single open position
        # switched every subsequent cycle to fast mode — and since the scan +
        # entry block is gated on `not is_fast`, the bot stopped looking for new
        # signals entirely until that position closed. MAX_CONCURRENT=3 was
        # unreachable and only one symbol ever traded at a time.
        #
        # Now: every cycle manages open positions at FAST_INTERVAL, and a full
        # scan runs on its own SCAN_INTERVAL timer whenever a slot is free.
        #
        # `is_fast` is gated on the SCAN CLOCK ALONE — never on slots_free. It
        # used to be `not (due_for_scan and slots_free)`, which deadlocked: one
        # flag controlled BOTH "skip scanning" and "fetch shallow data", so once
        # every slot filled, do_scan was permanently False, every cycle went
        # down the 100-bar fast path, and the freqtrade ports — which need >=275
        # 1m bars to evaluate an exit — could never produce one. Nothing closed,
        # so no slot freed, so nothing ever scanned again. The only escapes were
        # a CSM position closing or the exchange stop firing at -8%.
        #
        # Separating the two means a full-depth fetch still happens on the scan
        # clock when slots are full; the fetch below simply narrows to the open
        # symbols, since there is nothing to scan for.
        slots_free   = live.n_open() < MAX_CONCURRENT
        due_for_scan = (time.monotonic() - last_scan) >= SCAN_INTERVAL
        if due_for_scan:
            last_scan = time.monotonic()

        # Look for NEW entries only when a slot is actually free.
        do_scan = due_for_scan and slots_free

        # `is_fast` means "lightweight cycle" — shallow data, manage only.
        is_fast = not due_for_scan

        # Convert UTC to IST for display
        now_ist = utc_to_ist(now_utc.isoformat())

        # Fast cycles run every FAST_INTERVAL seconds purely to check SL/TP, so
        # they have nothing to report unless a position actually exits. Logging
        # a header + footer on every pass buried the meaningful lines under
        # ~1000 identical entries per hour of open position. Emit a periodic
        # heartbeat instead — exits, entries and errors still log immediately.
        _fast_heartbeat = is_fast and (cycle % FAST_LOG_EVERY == 0)
        if not is_fast:
            log.info(
                f"── Full cycle {cycle} | {now_ist} "
                f"──────────────────────────────"
            )

        try:
            # ── 0. Daily EOD report ───────────────────────────────────────────
            today_str = now_utc.strftime("%Y-%m-%d")
            if now_utc.hour == EOD_REPORT_HOUR_UTC and today_str != last_report_date:
                last_report_date = today_str
                ls = get_loss_status()
                ml_status = get_ml_status()
                notify_daily_report(
                    live_summary = live.session_summary(),
                    regime       = regime,
                    loss_status  = ls,
                    report_date  = today_str,
                    ml_status    = ml_status,
                )
                log.info(f"Daily EOD report sent for {today_str}")

            # ── 1. Symbol list refresh (once per hour, full cycle only) ───────
            if not is_fast and time.monotonic() - last_symbol_refresh > 3600:
                if FOCUSED_MODE:
                    symbols = get_focused_watchlist(_FOCUSED_N)
                    log.info(f"Focused symbol list refreshed: {len(symbols)} coins — {symbols}")
                else:
                    symbols = get_top_symbols(force_refresh=True)
                    _wl = get_watchlist()
                    _wl_set = set(_wl)
                    symbols = _wl + [s for s in symbols if s not in _wl_set]
                    log.info(f"Symbol list refreshed: {len(symbols)} symbols ({len(_wl)} watchlist priority)")
                last_symbol_refresh = time.monotonic()

            # ── 1b. Account equity refresh (every 15 min, live mode only) ─────
            if (not is_fast and LIVE_ENABLED
                    and time.monotonic() - last_equity_refresh > EQUITY_REFRESH_INTERVAL):
                fresh_eq = get_account_equity()
                if fresh_eq > 0:
                    ACCOUNT_EQUITY = fresh_eq
                    log.info(f"Account equity refreshed: ${ACCOUNT_EQUITY:.4f} USDT")
                last_equity_refresh = time.monotonic()

            # ── 2. Regime classification (full cycle only) ────────────────────
            if not is_fast:
                btc_ref = fetch_btc_reference()
                regime  = classify_regime(
                    btc_1h = btc_ref.get("btc_1h"),
                    eth_1h = btc_ref.get("eth_1h"),
                    sol_1h = btc_ref.get("sol_1h"),
                )
                _current_regime = regime
                new_regime = regime.get("regime", "RANGING")

                # LLM regime override removed 2026-08-11 — see docs/LLM_REMOVED.md.
                # The regime is now purely rule-based (modules/regime_engine.py),
                # which is exactly what backtest_optimizer.build_regime_series
                # replays, so live and backtest classify identically.

                if new_regime != current_regime:
                    log.info(f"REGIME CHANGE: {current_regime} → {new_regime}")
                    logger.log_regime_change(
                        current_regime, new_regime, regime.get("btc_price", 0)
                    )
                    notify_regime_change(current_regime, new_regime, regime)
                    current_regime     = new_regime
                    _regime_changed_at = datetime.now(timezone.utc)


            # ── 3. Fetch market data ──────────────────────────────────────────
            open_syms = [p["symbol"] for p in live.active]

            if is_fast:
                symbol_data = fetch_active_positions_data(open_syms)
            else:
                # Pass regime so data_hub can conditionally fetch 1h candles
                # (needed by FF strategy in OVERHEATED/OVERSOLD regimes).
                #
                # Union with open symbols: a position's symbol can drop out of
                # the top-N watchlist while still open, and without its candles
                # _manage_positions() cannot evaluate SL/TP for it that cycle.
                #
                # Computed here, on the freshly classified regime, and reused
                # for depth/1h below.
                _permitted = StrategyFactory.get_permitted(
                    regime, REGIME_STRATEGY_PERMISSIONS
                )
                if do_scan and _permitted:
                    scan_syms = list(symbols[:TOP_N_SYMBOLS])
                    scan_syms += [s for s in open_syms if s not in scan_syms]
                else:
                    # Nothing to scan FOR — slots are full, or no strategy is
                    # permitted (regime matrix, or every one /disabled). The
                    # open positions still need full-depth data or the ports'
                    # manage() cannot run, so fetch deep for those symbols
                    # only. Sweeping the whole watchlist here cost ~1,900 of
                    # the 2,400/min weight (150 symbols at NASOS's 1,500-bar
                    # depth because one NASOS position was open) for a scan
                    # that returned "No strategies permitted" a line later.
                    scan_syms = list(open_syms)

                # Fetch 1h only if a strategy permitted THIS cycle actually
                # reads it. Driven by REQUIRES_1H rather than by regime, so a
                # 1h-dependent strategy can never again be starved of its data.
                #
                # Depth and 1h are derived from the permitted strategies AND
                # from the strategies already HOLDING a position. Permitted
                # alone was wrong: in RANGING the permitted set is CSM only
                # (depth 200), so an open NASOS_V4 position got 200
                # 1m bars -> 40 resampled 5m bars -> under the `< 55` guard ->
                # unmanaged for the entire regime.
                _data_srcs = list(_permitted)
                for _p in live.active:
                    _held = StrategyFactory.get(_p.get("strategy", ""))
                    if _held is not None and _held not in _data_srcs:
                        _data_srcs.append(_held)
                _need_1h = any(
                    getattr(s, "REQUIRES_1H", False) for s in _data_srcs
                )
                _depth_1m = max(
                    (getattr(s, "REQUIRES_1M_DEPTH", 200) for s in _data_srcs),
                    default=200,
                )
                _depth_1h = max(
                    (getattr(s, "REQUIRES_1H_DEPTH", 50) for s in _data_srcs),
                    default=50,
                )
                symbol_data = fetch_all_symbols(
                    scan_syms, regime=current_regime, need_1h=_need_1h,
                    depth_1m=_depth_1m, depth_1h=_depth_1h,
                )

            # ── 3b. Process square-off queue from web dashboard ────────────────
            _sq_file = os.path.join(_ROOT, "data", "square_off_queue.json")
            if os.path.exists(_sq_file):
                try:
                    with open(_sq_file, "r") as _sqf:
                        _sq_queue = json.load(_sqf)
                except Exception:
                    _sq_queue = []
                if _sq_queue:
                    os.remove(_sq_file)
                    for _sq in _sq_queue:
                        _sq_sym = _sq.get("symbol", "")
                        _sq_dir = _sq.get("direction", "")
                        _sq_pos = next(
                            (p for p in live.active
                             if p["symbol"] == _sq_sym and p["direction"] == _sq_dir),
                            None
                        )
                        if _sq_pos is None:
                            log.warning(f"[SQUARE_OFF] No matching position: {_sq_sym} {_sq_dir}")
                            continue
                        _sq_df = symbol_data.get(_sq_sym, (None, None, None))[0]
                        _sq_ep = (float(_sq_df["close"].iloc[-1])
                                  if _sq_df is not None and not _sq_df.empty
                                  else _sq_pos["entry_price"])
                        log.info(f"[SQUARE_OFF] Closing {_sq_sym} {_sq_dir} @ {_sq_ep:.4f} (web dashboard)")
                        _sq_ok = live.close_position(
                            _sq_pos, _sq_ep, "SQUARE_OFF",
                            account_equity=ACCOUNT_EQUITY,
                        )
                        if _sq_ok is True:
                            # BEFORE log_exit/notify_exit: both read duration_min, and this is
                            # what writes it. Called after them, every exit notification
                            # reported "Duration: 0.0 min" while the web ledger (which
                            # recomputes it internally) showed the true value.
                            _stamp_exit_context(_sq_pos)
                            logger.log_exit(_sq_pos)
                            notify_exit(_sq_pos, TRADE_MODE)
                            _book_realized_pnl(_sq_pos)
                            record_trade_pnl(_sq_pos.get("pnl_equity_pct", _sq_pos.get("pnl_pct", 0.0)))
                            ml_log_outcome(_sq_pos)
                            if _sq_pos.get("pnl_equity_pct", 0.0) < 0:
                                _symbol_loss_cooldown[_sq_sym] = datetime.now(timezone.utc)
                                _save_cooldown()
                        elif _sq_ok is False:
                            notify_error(
                                f"❗ SQUARE_OFF failed: {_sq_sym} {_sq_dir}\n"
                                f"Check Binance — manual close may be needed"
                            )

            # ── 4. Reconcile with Binance ─────────────────────────────────────
            # Detects manual closes + adopts orphaned positions from Binance
            manually_closed = live.reconcile_with_exchange(
                account_equity = ACCOUNT_EQUITY,
                symbol_data    = symbol_data,
            )
            for pos in manually_closed:
                # BEFORE log_exit/notify_exit: both read duration_min, and this is
                # what writes it. Called after them, every exit notification
                # reported "Duration: 0.0 min" while the web ledger (which
                # recomputes it internally) showed the true value.
                _stamp_exit_context(pos)
                logger.log_exit(pos)
                notify_exit(pos, TRADE_MODE)
                _book_realized_pnl(pos)
                record_trade_pnl(pos.get("pnl_equity_pct", pos.get("pnl_pct", 0.0)))
                ml_log_outcome(pos)
                if pos.get("pnl_equity_pct", 0.0) < 0:
                    _symbol_loss_cooldown[pos["symbol"]] = datetime.now(timezone.utc)
                    _save_cooldown()

            # ── 5. Manage open positions ──────────────────────────────────────
            _manage_positions(live, symbol_data, logger)

            # ── 6. Entry cycle only: loss cap → scan → entries ────────────────
            # Gated on `do_scan`, not `not is_fast`. A full-depth cycle with
            # every slot occupied still refreshes data for the open positions
            # above, but must not scan for entries it cannot take.
            if do_scan:
                cap_hit, cap_reason = is_loss_cap_hit()
                if cap_hit:
                    log.warning(f"LOSS CAP ACTIVE — {cap_reason} — no new entries")
                    # Dedup: only alert once per unique cap reason to prevent
                    # Telegram spam every 60s for the rest of the capped day/week
                    if cap_reason not in _loss_cap_notified:
                        _loss_cap_notified.add(cap_reason)
                        notify_error(f"🚫 Loss cap active — trading paused\n{cap_reason}")
                else:
                    # Cap lifted (new day/week) — clear the dedup set so the
                    # next cap hit will alert again
                    _loss_cap_notified.clear()

                if not cap_hit:
                    if is_daily_floor_hit(live.session_pnl_pct):
                        log.warning(
                            f"Session floor hit "
                            f"({live.session_pnl_pct*100:.2f}%) — no new entries"
                        )
                    else:
                        signals = _scan_for_signals(
                            symbols[:TOP_N_SYMBOLS], symbol_data, regime, live
                        )
                        if signals:
                            log.info(
                                f"Scan: {len(signals)} signal(s) | "
                                f"Regime: {current_regime}"
                            )
                            n_entered = _execute_entries(
                                signals, live, logger, regime,
                                symbol_data=symbol_data,
                            )
                            if n_entered:
                                log.info(f"Entries this cycle: {n_entered}")
                        else:
                            log.info(f"Scan: no signals | Regime: {current_regime}")

        except Exception as exc:
            msg = f"Cycle {cycle} unhandled exception: {exc}"
            log.error(msg, exc_info=True)
            notify_error(msg)

        # Persist open positions so a restart can resume them (see
        # _save_positions). Written every cycle so a hard kill loses at most
        # one cycle of stop movement.
        _save_positions(live)

        # ── State Dump for Web Server ─────────────────────────────────────────
        try:
            state_file = os.path.join(_ROOT, "data", "active_state.json")
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            _total_pnl_pct = (
                (ACCOUNT_EQUITY - PAPER_STARTING_EQUITY) / PAPER_STARTING_EQUITY
                if PAPER_STARTING_EQUITY > 0 else 0.0
            )
            with open(state_file, "w") as f:
                json.dump({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "regime": current_regime,
                    "session_pnl_pct": live.session_pnl_pct,
                    "total_pnl_pct": _total_pnl_pct,
                    "starting_equity": PAPER_STARTING_EQUITY,
                    "active_positions": _positions_view(live.active, symbol_data),
                    "n_open": live.n_open(),
                    "account_equity": ACCOUNT_EQUITY,
                    # LIVE or PAPER — the dashboard must state which, or
                    # simulated positions read as real money.
                    "mode": TRADE_MODE,
                    "running": True,
                }, f)
        except Exception as exc:
            log.warning(f"Failed to dump active_state.json: {exc}")

        # ── Sleep ─────────────────────────────────────────────────────────────
        # Cadence follows whether anything needs managing, NOT whether we just
        # scanned: with a position open we keep the 5s loop so SL/TP is checked
        # promptly, and the scan timer fires on top of it every SCAN_INTERVAL.
        elapsed   = time.monotonic() - t_start
        interval  = FAST_INTERVAL if live.n_open() > 0 else SCAN_INTERVAL
        sleep_s   = max(0, interval - elapsed)

        if not is_fast:
            log.info(
                f"Cycle {cycle} done in {elapsed:.1f}s | "
                f"Open: live={live.n_open()} | Scan | "
                f"Sleeping {sleep_s:.0f}s"
            )
        elif _fast_heartbeat:
            # One line per minute summarising what the fast loop is watching,
            # with live marks — more useful than the tick it replaces.
            marks = []
            for p in _positions_view(live.active, symbol_data):
                marks.append(
                    f"{p['symbol']} {p['direction']} "
                    f"{p.get('pnl_equity_pct', 0.0)*100:+.2f}% "
                    f"({p.get('pnl_usdt', 0.0):+.2f}) "
                    f"{p.get('duration_min', 0)}m"
                )
            log.info(
                f"[MANAGING] {live.n_open()} open | "
                + (" | ".join(marks) if marks else "—")
            )

        # Sleep in 1s slices so SIGTERM is honoured within a second. The handler
        # only sets _shutdown, and Python retries time.sleep() after a signal
        # (PEP 475), so a single sleep(sleep_s) ran to completion first: with no
        # positions open that is the full SCAN_INTERVAL, and a stop pressed just
        # after a cycle took ~70s to land — long enough to trip the Telegram
        # bot's timeout and report a clean stop as a failure.
        _deadline = time.monotonic() + sleep_s
        while not _shutdown:
            _left = _deadline - time.monotonic()
            if _left <= 0:
                break
            time.sleep(min(1.0, _left))

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    log.info("Shutdown signal received — closing all positions...")
    # Fetch a final price for each open symbol so the force-close books the
    # real exit, not the entry price.
    #
    # This called live.open_symbols(), which does not exist on OrderEngine.
    # The AttributeError was swallowed by the except below, leaving final_data
    # empty — so _force_close_all fell back to pos["entry_price"] and every
    # SHUTDOWN trade was recorded with 0.00% price move and a net loss equal to
    # the fee, discarding whatever the position was actually worth.
    final_data = {}
    open_syms = [p["symbol"] for p in live.active]
    if open_syms:
        try:
            final_data = fetch_active_positions_data(open_syms)
        except Exception as exc:
            log.warning(f"Final data fetch failed: {exc}")
        missing = [s for s in open_syms if s not in final_data]
        if missing:
            log.warning(
                f"No exit price for {missing} — these will be booked at entry "
                f"price, understating their P&L"
            )

    if CLOSE_ON_SHUTDOWN:
        _force_close_all(live, final_data, logger, reason="SHUTDOWN")
    elif live.n_open():
        # Leave them open and persist, so the next start resumes them. In LIVE
        # the exchange still holds a STOP_MARKET at the current level; in PAPER
        # nothing guards them until the bot is back.
        _save_positions(live)
        log.warning(
            f"CLOSE_ON_SHUTDOWN=false — leaving {live.n_open()} position(s) "
            f"open for the next run: "
            + ", ".join(f"{p['symbol']} {p['direction']}" for p in live.active)
        )
        notify_error(
            f"⏸ Shutdown with {live.n_open()} position(s) STILL OPEN "
            f"(CLOSE_ON_SHUTDOWN=false).\n"
            f"They are unmanaged until the bot restarts."
            + ("" if LIVE_ENABLED
               else "\n⚠️ PAPER mode — no exchange stop protects them.")
        )

    # ── Final state dump ──────────────────────────────────────────────────────
    # active_state.json is only written at the end of each cycle, so without
    # this the file keeps the LAST cycle's snapshot — showing positions that
    # shutdown has just closed. Telegram reported them closed (notify_exit runs
    # at close time) while the dashboard kept rendering them open, indefinitely.
    # Write the post-close state so the two agree.
    try:
        state_file = os.path.join(_ROOT, "data", "active_state.json")
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        _total_pnl_pct = (
            (ACCOUNT_EQUITY - PAPER_STARTING_EQUITY) / PAPER_STARTING_EQUITY
            if PAPER_STARTING_EQUITY > 0 else 0.0
        )
        with open(state_file, "w") as f:
            json.dump({
                "timestamp":        datetime.now(timezone.utc).isoformat(),
                "regime":           current_regime,
                "session_pnl_pct":  live.session_pnl_pct,
                "total_pnl_pct":    _total_pnl_pct,
                "starting_equity":  PAPER_STARTING_EQUITY,
                "active_positions": _positions_view(live.active, final_data),
                "n_open":           live.n_open(),
                "account_equity":   ACCOUNT_EQUITY,
                "mode":             TRADE_MODE,
                # Explicit flag so the dashboard can say "stopped" rather than
                # inferring it from a stale timestamp.
                "running":          False,
            }, f)
        log.info(f"Final state written — {live.n_open()} open positions")
    except Exception as exc:
        log.warning(f"Failed to write final active_state.json: {exc}")

    live_sum = live.session_summary()
    logger.write_session_summary(live_sum, regime)
    logger.log_event("SESSION_END", "Graceful shutdown complete")
    notify_session_summary(live_sum, regime)

    # Auto trade analysis on session end (if session had trades)
    if live_sum.get("total_trades", 0) > 0:
        try:
            from modules.trade_analyzer import run_and_send
            run_and_send(days=3)
        except Exception as exc:
            log.warning(f"Auto-analysis failed: {exc}")

    log.info("Binance futures bot stopped cleanly.")


if __name__ == "__main__":
    main()


