"""
modules/symbol_blacklist.py
Persistent symbol blacklist — bans coins that repeatedly hard-stop.

Trigger (either condition):
  A. HARD_STOP_THRESHOLD hard stops on a symbol within HARD_STOP_WINDOW_HOURS
  B. Cumulative equity loss on a symbol >= LOSS_THRESHOLD_PCT

Ban escalation:
  1st offence → BLACKLIST_DAYS (7-day) temporary ban
  2nd offence → PERMANENT ban (symbol never trades again unless manually cleared)

Storage: data/symbol_blacklist.json
  {
    "XYZUSDT": {
      "reason":         "3 hard stops in 72h (loss -4.20%)",
      "added_at":       "2026-05-17T10:00:00+00:00",
      "expires_at":     "PERMANENT",   ← or ISO date for temp ban
      "hard_stop_count": 3,
      "total_loss_pct":  -0.042,
      "offence":         2
    }
  }

Hit history: data/symbol_hardstop_history.json
  {
    "XYZUSDT": {
      "hits":      [{"ts": "...", "pnl_pct": -0.025, "reason": "HARD_STOP"}, ...],
      "ban_count": 1    ← persists across ban cycles; drives escalation
    }
  }
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("SymbolBlacklist")

# ─── Config ───────────────────────────────────────────────────────────────────
HARD_STOP_THRESHOLD    = 3       # hard stops within window → blacklist
HARD_STOP_WINDOW_HOURS = 72      # rolling window — 3 days covers multiple sessions
                                 # and regime changes; 24h was too short (resets
                                 # after one bad day, misses multi-day patterns)
LOSS_THRESHOLD_PCT     = -0.03   # cumulative loss -3% equity on symbol → blacklist
BLACKLIST_DAYS         = 7       # ban duration

# Exit reasons treated as hard stops
HARD_STOP_REASONS = {"HARD_STOP", "GRID_HARD_STOP"}

# ─── Paths ────────────────────────────────────────────────────────────────────
_PROJECT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BLACKLIST_FILE = os.path.join(_PROJECT_DIR, "data", "symbol_blacklist.json")
_HISTORY_FILE   = os.path.join(_PROJECT_DIR, "data", "symbol_hardstop_history.json")


# ─── I/O ─────────────────────────────────────────────────────────────────────

def _load_blacklist() -> dict:
    try:
        if os.path.exists(_BLACKLIST_FILE):
            with open(_BLACKLIST_FILE, encoding="utf-8") as _jf:
                return json.load(_jf)
    except Exception as exc:
        log.warning(f"Blacklist load failed: {exc}")
    return {}


def _save_blacklist(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_BLACKLIST_FILE), exist_ok=True)
        with open(_BLACKLIST_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.error(f"Blacklist save failed: {exc}")


def _load_history() -> dict:
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, encoding="utf-8") as _jf:
                return json.load(_jf)
    except Exception as exc:
        log.warning(f"History load failed: {exc}")
    return {}


def _save_history(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        with open(_HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.error(f"History save failed: {exc}")


# ─── Core API ─────────────────────────────────────────────────────────────────

def record_hard_stop(symbol: str, pnl_equity_pct: float, exit_reason: str) -> bool:
    """
    Record a hard-stop hit for a symbol. Evaluates blacklist triggers.

    Ban escalation:
      1st offence → 7-day temporary ban
      2nd offence → PERMANENT ban

    Args:
        symbol          : e.g. 'XYZUSDT'
        pnl_equity_pct  : negative float, e.g. -0.025
        exit_reason     : 'HARD_STOP' or 'GRID_HARD_STOP'

    Returns:
        True if symbol was newly blacklisted, False otherwise.
    """
    if exit_reason not in HARD_STOP_REASONS:
        return False

    now     = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # ── Load history — structure: {symbol: {hits: [...], ban_count: int}} ─────
    history    = _load_history()
    sym_hist   = history.setdefault(symbol, {"hits": [], "ban_count": 0})

    # Support old flat-list format from previous runs
    if isinstance(sym_hist, list):
        sym_hist   = {"hits": sym_hist, "ban_count": 0}
        history[symbol] = sym_hist

    sym_hist["hits"].append({
        "ts":      now_iso,
        "pnl_pct": round(pnl_equity_pct, 6),
        "reason":  exit_reason,
    })
    # Prune hits older than rolling window
    cutoff = (now - timedelta(hours=HARD_STOP_WINDOW_HOURS)).isoformat()
    sym_hist["hits"] = [h for h in sym_hist["hits"] if h["ts"] >= cutoff]
    _save_history(history)

    # ── Skip if already blacklisted ───────────────────────────────────────────
    blacklist = _load_blacklist()
    if symbol in blacklist:
        return False

    recent_hits  = sym_hist["hits"]
    stop_count   = len(recent_hits)
    total_loss   = sum(h["pnl_pct"] for h in recent_hits)

    triggered      = False
    trigger_reason = ""

    if stop_count >= HARD_STOP_THRESHOLD:
        triggered      = True
        trigger_reason = (
            f"{stop_count} hard stops in {HARD_STOP_WINDOW_HOURS}h "
            f"(loss {total_loss*100:.2f}%)"
        )
    elif total_loss <= LOSS_THRESHOLD_PCT:
        triggered      = True
        trigger_reason = (
            f"cumulative loss {total_loss*100:.2f}% on {stop_count} hard stop(s)"
        )

    if triggered:
        # ── Ban escalation ────────────────────────────────────────────────────
        ban_count = sym_hist.get("ban_count", 0)
        sym_hist["ban_count"] = ban_count + 1
        _save_history(history)

        if ban_count >= 1:
            # 2nd+ offence — permanent ban
            expires    = "PERMANENT"
            ban_label  = "PERMANENT"
            log_suffix = "PERMANENT BAN (repeat offender)"
        else:
            # 1st offence — 7-day ban
            expires    = (now + timedelta(days=BLACKLIST_DAYS)).isoformat()
            ban_label  = expires[:10]
            log_suffix = f"expires {ban_label}"

        blacklist[symbol] = {
            "reason":          trigger_reason,
            "added_at":        now_iso,
            "expires_at":      expires,
            "hard_stop_count": stop_count,
            "total_loss_pct":  round(total_loss, 6),
            "offence":         ban_count + 1,
        }
        _save_blacklist(blacklist)
        log.warning(
            f"[Blacklist] 🚫 {symbol} BLACKLISTED — {trigger_reason} | "
            f"{log_suffix}"
        )
        return True

    log.info(
        f"[Blacklist] {symbol} hard stop recorded "
        f"({stop_count}/{HARD_STOP_THRESHOLD} in {HARD_STOP_WINDOW_HOURS}h, "
        f"loss {total_loss*100:.2f}%/{LOSS_THRESHOLD_PCT*100:.0f}%)"
    )
    return False


def is_blacklisted(symbol: str) -> bool:
    """
    Return True if symbol is currently blacklisted (temporary or permanent).
    Expired temporary bans are cleaned up on read.
    Permanent bans always return True.
    """
    blacklist = _load_blacklist()
    entry     = blacklist.get(symbol)
    if entry is None:
        return False

    expires_at = entry.get("expires_at", "")

    # Permanent ban — never expires
    if expires_at == "PERMANENT":
        return True

    # Temporary ban — check expiry
    now = datetime.now(timezone.utc)
    if expires_at and datetime.fromisoformat(expires_at) <= now:
        # Expired — remove from active blacklist but preserve ban_count in history
        del blacklist[symbol]
        _save_blacklist(blacklist)
        log.info(
            f"[Blacklist] {symbol} 7-day ban expired — "
            f"removed (ban_count preserved, next offence = PERMANENT)"
        )
        return False

    return True


def get_blacklist() -> dict:
    """
    Return active (non-expired) blacklist entries.
    {symbol: {reason, added_at, expires_at, hard_stop_count, total_loss_pct}}
    """
    blacklist = _load_blacklist()
    now       = datetime.now(timezone.utc)
    active    = {}
    changed   = False

    for sym, entry in list(blacklist.items()):
        exp = entry.get("expires_at", "")
        if exp == "PERMANENT":
            active[sym] = entry   # never expires
        elif exp and datetime.fromisoformat(exp) <= now:
            del blacklist[sym]
            changed = True
        else:
            active[sym] = entry

    if changed:
        _save_blacklist(blacklist)

    return active


def clear_symbol(symbol: str) -> bool:
    """
    Manually remove a symbol from the blacklist.
    Returns True if it was present, False if not found.
    """
    blacklist = _load_blacklist()
    if symbol not in blacklist:
        return False
    del blacklist[symbol]
    _save_blacklist(blacklist)

    # Also wipe its hit history so it starts fresh
    history = _load_history()
    if symbol in history:
        del history[symbol]
        _save_history(history)

    log.info(f"[Blacklist] {symbol} manually cleared")
    return True


def clear_all() -> list[str]:
    """Clear all blacklisted symbols. Returns list of cleared symbols."""
    blacklist = _load_blacklist()
    symbols   = list(blacklist.keys())
    _save_blacklist({})
    _save_history({})
    log.info(f"[Blacklist] All cleared: {symbols}")
    return symbols


def get_status() -> dict:
    """Summary for Telegram /status and daily report."""
    active = get_blacklist()
    return {
        "count":   len(active),
        "symbols": {
            sym: {
                "reason":     e["reason"],
                "expires":    "PERMANENT" if e["expires_at"] == "PERMANENT"
                              else e["expires_at"][:10],
                "stops":      e["hard_stop_count"],
                "loss_pct":   round(e["total_loss_pct"] * 100, 2),
                "offence":    e.get("offence", 1),
            }
            for sym, e in active.items()
        },
    }

