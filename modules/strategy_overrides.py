"""
modules/strategy_overrides.py
Persistent strategy on/off overrides — lets Telegram bot disable strategies
at runtime without restarting csb.

Storage: data/strategy_overrides.json (JSON, hot-reloaded each call).

Override semantics:
  - If strategy is in overrides as disabled=True, is_strategy_permitted()
    returns False regardless of regime matrix.
  - If absent or disabled=False, the regime matrix decides as before.

Public API:
  is_disabled(strategy)         -> bool
  set_disabled(strategy, on)    -> writes file, returns new state
  toggle(strategy)              -> flip state, returns new state
  get_all()                     -> dict of {strategy: {...}}
"""

import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("StratOverride")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FILE        = os.path.join(_PROJECT_DIR, "data", "strategy_overrides.json")

# Derived from the factory, not hardcoded.
#
# 2026-08-04: this was ["TP","FF","DB","GRID","BBR"] — the pre-2026 set.
# set_disabled() rejects any strategy not in this list, so the Telegram/Discord
# "disable strategy" control silently refused to disable CSM, WKD, VRP, LIQ,
# OIB or FF_V2 — i.e. six of the seven strategies actually trading could not be
# turned off at runtime. Fourth place this same stale list was found.
def _production_strategy_ids() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
        if ids:
            return ids
    except Exception:
        pass
    return ["CSM", "NASOS_V4", "ELLIOT_V8"]


KNOWN_STRATEGIES = _production_strategy_ids()

# Validity key is (mtime, size); CSB_NO_FILE_CACHE=true bypasses the cache.
# This file is written by the Telegram/Discord bot process and read by the
# scanner process, so the scanner relies on the key changing to see a /disable.
_override_cache = {"key": None, "data": {}}
_NO_FILE_CACHE = os.getenv("CSB_NO_FILE_CACHE", "false").strip().lower() in ("1", "true", "yes", "on")


def _file_key(path):
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _load() -> dict:
    """Load overrides with (mtime,size)-based caching — called per-signal via is_disabled()."""
    try:
        if os.path.exists(_FILE):
            key = _file_key(_FILE)
            if not _NO_FILE_CACHE and key is not None and _override_cache["key"] == key:
                return _override_cache["data"]
            with open(_FILE, encoding="utf-8") as _jf:
                data = json.load(_jf)
            _override_cache["key"] = key
            _override_cache["data"] = data
            return data
    except Exception as exc:
        log.warning(f"override load failed: {exc}")
    return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        with open(_FILE, "w") as f:
            json.dump(data, f, indent=2)
        _override_cache["key"]  = _file_key(_FILE)
        _override_cache["data"] = data
    except Exception as exc:
        log.error(f"override save failed: {exc}")
        _override_cache["key"] = None


def is_disabled(strategy: str) -> bool:
    """Return True if user has explicitly disabled this strategy."""
    data = _load()
    entry = data.get(strategy, {})
    return bool(entry.get("disabled", False))


def set_disabled(strategy: str, disabled: bool, reason: str = "") -> bool:
    """
    Set disabled state for a strategy. Returns the new state (True=disabled).
    Logs the change with timestamp + reason.
    """
    if strategy not in KNOWN_STRATEGIES:
        log.warning(f"Unknown strategy: {strategy}")
        return False
    data = _load()
    data[strategy] = {
        "disabled":  bool(disabled),
        "set_at":    datetime.now(timezone.utc).isoformat(),
        "reason":    reason or ("manual disable" if disabled else "manual enable"),
    }
    _save(data)
    log.info(
        f"[Override] {strategy} "
        f"{'DISABLED' if disabled else 'ENABLED'} "
        f"(reason: {data[strategy]['reason']})"
    )
    return bool(disabled)


def toggle(strategy: str) -> bool:
    """Flip disabled state. Returns new state."""
    return set_disabled(strategy, not is_disabled(strategy))


def get_all() -> dict:
    """Return {strategy: {disabled, set_at, reason}} for all known strategies."""
    data = _load()
    out = {}
    for s in KNOWN_STRATEGIES:
        entry = data.get(s, {})
        out[s] = {
            "disabled":  bool(entry.get("disabled", False)),
            "set_at":    entry.get("set_at", ""),
            "reason":    entry.get("reason", ""),
        }
    return out

