"""
live_logger.py
Structured per-session logger with strict per-strategy separation.

STRICT RULE: Every signal and trade is written to its own strategy
folder immediately and independently of the session log.

Log structure:
  logs/
  ├── live/
  │   ├── session_YYYYMMDD_HHMMSS.json   ← all strategies, this session
  │   └── session_YYYYMMDD_HHMMSS.txt    ← human-readable session summary
  └── strategies/
      ├── TP/
      │   ├── YYYY-MM-DD.json
      │   └── YYYY-MM-DD.txt
      ├── FF/
      │   ├── YYYY-MM-DD.json
      │   └── YYYY-MM-DD.txt
      ├── ORB/
      │   ├── YYYY-MM-DD.json
      │   └── YYYY-MM-DD.txt
      └── GRID/
          ├── YYYY-MM-DD.json
          └── YYYY-MM-DD.txt

Design decisions:
  - Strategy files use APPEND-ONLY writes to individual records (NDJSON lines)
    to avoid read-modify-write corruption on crash.
  - TXT summary is rebuilt from JSON on every trade close.
  - Session log is a separate aggregate — not linked to strategy files.
  - Unknown strategy ID → logged to session only, warning emitted.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("LiveLogger")

_MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
LOG_DIR      = os.path.join(_MODULE_DIR, "logs", "live")
STRATEGY_DIR = os.path.join(_MODULE_DIR, "logs", "strategies")

# Derived from the factory so this list cannot drift from what actually trades.
#
# It had drifted badly: the hardcoded list was ["TP","FF","DB","GRID","BBR"] —
# the pre-2026 strategy set. _append_strategy_record() rejects any strategy not
# in this list, so per-strategy logs were being SILENTLY DISCARDED for CSM, WKD,
# VRP, LIQ, OIB and FF_V2 (six of the seven strategies actually in production),
# while directories were created for four strategies that never trade.
def _production_strategy_ids() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
        if ids:
            return ids
    except Exception:
        pass
    # Fallback if the factory cannot be imported (keeps logging working).
    return ["CSM", "NASOS_V4"]


STRATEGIES = _production_strategy_ids()
# Retired: ORB 2026-04-20; LF 2026-05-25; BGD 2026-06-09; BKD 2026-06-29;
# TP/FF/DB/GRID/BBR superseded by the current factory set.


# ─── Directory setup ──────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    for s in STRATEGIES:
        os.makedirs(os.path.join(STRATEGY_DIR, s), exist_ok=True)


# ─── Serialisation ────────────────────────────────────────────────────────────

def _serial(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_to_ist(utc_time_str: str) -> str:
    """
    Convert UTC ISO timestamp to IST (Indian Standard Time, UTC+05:30) for display.
    
    Input:  "2026-04-16T00:40:09.949650+00:00"
    Output: "2026-04-16 06:10:09 IST"
    
    Keeps internal logic in UTC (required for Funding Fade strategy).
    This is DISPLAY-ONLY for user readability.
    """
    try:
        utc_dt = datetime.fromisoformat(utc_time_str)
        # IST is UTC+05:30
        ist_dt = utc_dt + timedelta(hours=5, minutes=30)
        return ist_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception as e:
        log.warning(f"[utc_to_ist] Conversion failed for '{utc_time_str}': {e}")
        # Return a properly formatted IST timestamp as fallback
        try:
            now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            return now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
        except Exception:
            return "N/A"


# ─── Strategy file paths ──────────────────────────────────────────────────────
# Files are named STRATEGY_YYYY-MM-DD.json / .txt so the strategy name is
# visible in the filename even when files are copied out of their folder.
# Example: logs/strategies/GRID/GRID_2026-03-29.json

def _strat_json(strategy: str, date: str = None) -> str:
    d = date or _today_utc()
    return os.path.join(STRATEGY_DIR, strategy, f"{strategy}_{d}.json")


def _strat_txt(strategy: str, date: str = None) -> str:
    d = date or _today_utc()
    return os.path.join(STRATEGY_DIR, strategy, f"{strategy}_{d}.txt")


# ─── Append one record to strategy JSON (NDJSON — one line per record) ────────

def _append_strategy_record(strategy: str, record: dict) -> None:
    """
    Append one JSON record as a single line to the strategy daily file.
    NDJSON (newline-delimited JSON) — each record is one line.
    This is append-only — no read-modify-write — safe against crashes.
    Unknown strategies are rejected with a warning.
    """
    if strategy not in STRATEGIES:
        log.warning(f"Unknown strategy '{strategy}' — not written to strategy log")
        return
    path = _strat_json(strategy)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=_serial) + "\n")
    except Exception as exc:
        log.error(f"[STRICT LOG] Failed to write to {path}: {exc}")


# ─── Read all records for one strategy day (for TXT rebuild) ─────────────────

def _read_strategy_records(strategy: str, date: str = None) -> list[dict]:
    """Read all NDJSON records from a strategy daily file."""
    path = _strat_json(strategy, date)
    if not os.path.exists(path):
        return []
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception as exc:
        log.error(f"Failed to read strategy log [{strategy}]: {exc}")
    return records


# ─── Rebuild TXT summary for one strategy ────────────────────────────────────

def _rebuild_strategy_txt(strategy: str, date: str = None) -> None:
    """
    Rebuild the human-readable TXT summary from the NDJSON file.
    Called after every trade close so the TXT is always current.
    """
    d       = date or _today_utc()
    records = _read_strategy_records(strategy, d)
    trades  = [r for r in records if r.get("type") == "EXIT"]
    signals = [r for r in records if r.get("type") == "SIGNAL"]

    if not trades and not signals:
        return   # Nothing to write yet

    wins      = [t for t in trades if t.get("pnl_pct_100", 0) >= 0]
    losses    = [t for t in trades if t.get("pnl_pct_100", 0) < 0]
    total_net  = sum(t.get("pnl_pct_100", 0) for t in trades)
    total_fees = sum(t.get("fee_usdt", 0.0) for t in trades)
    total_gross= sum(t.get("pnl_equity_pct_gross", t.get("pnl_pct_100", 0)) for t in trades)
    wr         = len(wins) / len(trades) * 100 if trades else 0.0

    sep  = "=" * 64
    sep2 = "-" * 64
    lines = [
        "",
        sep,
        f"  STRATEGY LOG — {strategy}",
        f"  Date    : {d} (UTC)",
        f"  Updated : {_utcnow()}",
        sep,
        "",
        f"  Signals fired : {len(signals)}",
        f"  Trades closed : {len(trades)}  ({len(wins)}W / {len(losses)}L)",
        f"  Win rate      : {wr:.1f}%",
        f"  Gross P&L     : {total_gross:+.3f}%",
        f"  Total fees    : −${total_fees:.3f} USDT  (Binance 0.08% round-trip)",
        f"  Net P&L       : {total_net:+.3f}%",
        "",
        sep2,
        f"  {'Time':<20} {'Symbol':<12} {'Dir':<6} {'Entry':>9} "
        f"{'Exit':>9} {'Gross':>7} {'Fee':>6} {'Net':>7} {'Reason'}",
        sep2,
    ]

    for t in trades:
        net_pnl   = t.get("pnl_pct_100", 0)
        gross_pnl = t.get("pnl_equity_pct_gross", net_pnl)
        fee_u     = t.get("fee_usdt", 0.0)
        emoji     = "✅" if net_pnl >= 0 else "❌"
        t_time    = t.get("time", "")[:19].replace("T", " ")
        lines.append(
            f"  {emoji} {t_time:<18} {t.get('symbol',''):<12} "
            f"{t.get('direction',''):<6} "
            f"{t.get('entry_price', 0):>9.4f} "
            f"{t.get('exit_price', 0):>9.4f} "
            f"{gross_pnl:>+6.3f}% "
            f"${fee_u:>4.2f} "
            f"{net_pnl:>+6.3f}% "
            f"{t.get('exit_reason','')}"
        )

    lines += ["", sep, ""]

    path = _strat_txt(strategy, d)
    try:
        with open(path, "w") as f:
            f.write("\n".join(lines))
    except Exception as exc:
        log.error(f"Failed to write strategy TXT [{strategy}]: {exc}")


# ─── Main logger class ────────────────────────────────────────────────────────

# Keys carried from the position dict into every EXIT record when present.
_CONTEXT_KEYS = (
    "entry_time", "exit_time", "contracts", "margin_req", "risk_usdt",
    "account_equity", "initial_sl_price", "atr", "liq_price",
    "regime_entry", "regime_exit", "regime_changed_in_trade",
    "regime_age_min_entry", "btc_price_entry", "btc_price_exit",
    "btc_trend_entry", "eth_trend_entry", "sol_trend_entry", "funding_entry",
    "hour_utc_entry", "dow_entry", "n_open_before",
    "signal_price", "signal_strength", "signal_reason", "normalized_mom",
    "vol_ratio", "sl_pct_planned", "fill_slippage_pct",
    "kronos_pred_fav", "kronos_ts",
    "ml_signal_id", "ml_risk_mult", "ml_win_prob", "ml_gate_action",
    "ml_sl_atr_mult", "ml_trail_mult",
    "mfe", "mae", "sl_moves", "settings_entry",
)


class LiveLogger:
    """
    Session-scoped logger. One instance per bot run.

    STRICT RULE: Every signal and trade is written to:
      1. The strategy's dedicated daily NDJSON file  (append-only)
      2. The strategy's rebuilt daily TXT summary     (after every trade)
      3. The session aggregate JSON                   (in-memory + flush)
    """

    def __init__(self):
        _ensure_dirs()
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._json_path  = os.path.join(LOG_DIR, f"session_{self._session_id}.json")
        self._txt_path   = os.path.join(LOG_DIR, f"session_{self._session_id}.txt")

        self.signals : list[dict] = []
        self.trades  : list[dict] = []
        self.events  : list[dict] = []

        self.log_event("SESSION_START", f"Log session {self._session_id} opened")
        log.info(f"LiveLogger started")
        log.info(f"  Session log  → {self._json_path}")
        log.info(f"  Strategy logs   {STRATEGY_DIR}/{{{','.join(STRATEGIES)}}}/")

    # ─── Signal entry ─────────────────────────────────────────────────────────

    def log_signal(
        self,
        symbol:      str,
        strategy:    str,
        direction:   str,
        entry_price: float,
        sl_price:    float,
        tp_price:    float | None,
        leverage:    int,
        notional:    float,
        liq_price:   float,
        mode:        str,
        reason:      str = "",
    ) -> None:
        record = {
            "type":        "SIGNAL",
            "time":        _utcnow(),
            "session":     self._session_id,
            "mode":        mode,
            "symbol":      symbol,
            "strategy":    strategy,
            "direction":   direction,
            "entry_price": entry_price,
            "sl_price":    sl_price,
            "sl_pct":      round(abs(entry_price - sl_price) / entry_price * 100, 3),
            "tp_price":    tp_price,
            "leverage":    leverage,
            "notional":    notional,
            "liq_price":   liq_price,
            "reason":      reason,
        }

        # 1. Strategy file (strict — always first)
        _append_strategy_record(strategy, record)

        # 2. Session aggregate
        self.signals.append(record)
        self._flush()

        log.info(
            f"[{mode}] SIGNAL {symbol} {strategy} {direction} "
            f"@ {entry_price:.4f} | SL: {sl_price:.4f} ({record['sl_pct']:.2f}%) | "
            f"Lev: {leverage}× | ${notional:.2f} | {reason}"
        )

    # ─── Trade exit ───────────────────────────────────────────────────────────

    def log_exit(self, position: dict) -> None:
        pnl_pct    = position.get("pnl_pct", 0.0)
        pnl_equity = position.get("pnl_equity_pct", pnl_pct)
        strategy   = position.get("strategy", "?")

        try:
            entry_t  = datetime.fromisoformat(position["entry_time"])
            exit_t   = datetime.now(timezone.utc)
            duration = round((exit_t - entry_t).total_seconds() / 60, 1)
        except Exception:
            duration = 0.0

        record = {
            "type":                  "EXIT",
            "time":                  _utcnow(),
            "session":               self._session_id,
            "mode":                  position.get("mode", "?"),
            "symbol":                position["symbol"],
            "strategy":              strategy,
            "direction":             position["direction"],
            "entry_price":           position["entry_price"],
            "exit_price":            position.get("exit_price", 0.0),
            "sl_price":              position.get("sl_price", 0.0),
            "tp_price":              position.get("tp_price"),
            "pnl_pct":               pnl_pct,
            "pnl_pct_100":           round(pnl_pct * 100, 4),
            "pnl_equity_pct":        round(pnl_equity * 100, 4),         # net (after fees)
            "pnl_equity_pct_gross":  round(position.get("pnl_equity_pct_gross", pnl_equity) * 100, 4),
            "pnl_usdt":              position.get("pnl_usdt", 0.0),      # gross
            "pnl_usdt_net":          position.get("pnl_usdt_net", position.get("pnl_usdt", 0.0)),
            "fee_usdt":              position.get("fee_usdt", 0.0),
            "exit_reason":           position.get("exit_reason", "?"),
            "leverage":              position.get("leverage", 1),
            "notional":              position.get("notional", 0.0),
            "duration_min":          duration,
            # Production strategies set 'be_hit'; only trend_pullback (deleted 2026-09-14)
            # sets 'be_active'. Reading just 'be_active' left this field False
            # in every record, so the logs could not distinguish a trade that
            # reached breakeven from one stopped out cold.
            "be_active":             position.get(
                "be_hit", position.get("be_active", False)
            ),
            "hwm":                   round(float(position.get("hwm", 0.0) or 0.0), 6),
            "exit_source":           position.get("exit_source", "bot"),
        }
        # Full entry/exit context stamped by live_scanner (_stamp_entry_context /
        # _stamp_exit_context). Copied when present so older positions resumed
        # from disk without these keys still log cleanly.
        for k in _CONTEXT_KEYS:
            if k in position:
                record[k] = position[k]

        # 1. Strategy file — append record (strict, crash-safe)
        _append_strategy_record(strategy, record)

        # 2. Rebuild strategy TXT summary (human-readable, always current)
        _rebuild_strategy_txt(strategy)

        # 3. Session aggregate
        self.trades.append(record)
        self._flush()

        emoji = "✅" if pnl_pct >= 0 else "❌"
        log.info(
            f"[{record['mode']}] {emoji} EXIT {record['symbol']} "
            f"{strategy} {record['direction']} | "
            f"Price: {pnl_pct*100:+.3f}% | "
            f"Equity: {pnl_equity*100:+.3f}% | "
            f"{record['exit_reason']} | {duration:.0f}m"
        )

    # ─── Regime change ────────────────────────────────────────────────────────

    def log_regime_change(self, old: str, new: str, btc_price: float) -> None:
        self.log_event("REGIME_CHANGE", f"{old} → {new} | BTC: ${btc_price:,.2f}")

    # ─── Generic event ────────────────────────────────────────────────────────

    def log_event(self, event_type: str, msg: str) -> None:
        entry = {
            "type":       "EVENT",
            "event_type": event_type,
            "time":       _utcnow(),
            "msg":        msg,
        }
        self.events.append(entry)
        log.info(f"[EVENT:{event_type}] {msg}")
        self._flush()

    # ─── Session summary TXT ──────────────────────────────────────────────────

    def write_session_summary(
        self,
        live_summary:  dict,
        regime:        dict,
    ) -> None:
        sep = "=" * 56
        r   = regime.get("regime", "?")
        now = _utcnow()

        def _strat_block(summary: dict) -> list[str]:
            lines = []
            for sid, s in summary.get("by_strategy", {}).items():
                n   = s.get("trades", 0)
                w   = s.get("wins", 0)
                wr  = w / n * 100 if n > 0 else 0
                avg = s.get("pnl_pct_sum", 0) / n * 100 if n > 0 else 0
                lines.append(
                    f"    {sid:5s} {n:3d} trades | WR {wr:5.1f}% | avg {avg:+.3f}%"
                )
            return lines if lines else ["    No trades"]

        lines = [
            "", sep,
            f"  csb — SESSION SUMMARY",
            f"  Session  : {self._session_id}",
            f"  Ended    : {now}",
            f"  Regime   : {r}",
            f"  BTC      : ${regime.get('btc_price', 0):,.2f}",
            sep, "",
            "  LIVE",
            f"  Trades : {live_summary.get('total_trades',0)} "
            f"({live_summary.get('wins',0)}W/{live_summary.get('losses',0)}L)",
            f"  Win %  : {live_summary.get('win_rate',0):.1f}%",
            f"  P&L    : {live_summary.get('session_pnl',0):+.3f}%",
            *_strat_block(live_summary), "",
            sep, "",
            "  Strategy log files:",
        ]
        for s in STRATEGIES:
            path = _strat_txt(s)
            exists = "✅" if os.path.exists(path) else "—"
            lines.append(f"    {exists} {s:5s} → logs/strategies/{s}/{s}_{_today_utc()}.txt")

        output = "\n".join(lines)
        print(output)
        try:
            with open(self._txt_path, "w") as f:
                f.write(output)
            log.info(f"Session summary → {self._txt_path}")
        except Exception as exc:
            log.error(f"Session TXT write failed: {exc}")
        self._flush()

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _flush(self) -> None:
        data = {
            "session_id": self._session_id,
            "signals":    self.signals,
            "trades":     self.trades,
            "events":     self.events,
        }
        try:
            with open(self._json_path, "w") as f:
                json.dump(data, f, indent=2, default=_serial)
        except Exception as exc:
            log.error(f"Session log flush failed:{exc}")


