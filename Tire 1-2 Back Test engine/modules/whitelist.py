"""
modules/whitelist.py
Persistent symbol whitelist — RESTRICTS trading to specific coins.

Mirrors modules/blacklist.py in shape and storage so the two behave predictably
together, but the semantics are the inverse and that difference matters:

    blacklist  = deny list. Empty means "block nothing".
    whitelist  = allow list. Empty means "allow everything", NOT "allow nothing".

That empty-means-allow rule is deliberate and load-bearing. A whitelist that
blocked everything while empty would silently halt all trading the moment the
JSON file was missing, corrupt, or not yet created — which is exactly the kind
of silent, total failure this codebase has been bitten by before. Enforcement in
live_scanner also fails OPEN for the same reason.

Storage: data/whitelist.json (dict format, same as blacklist)

Format:
  {
    "BTCUSDT": ["*"],                  # allowed for every strategy
    "SOLUSDT": ["CSM"],                # allowed ONLY for CSM
    "ETHUSDT": ["CSM", "NASOS_V4"]     # allowed for CSM and NASOS_V4
  }

Interaction with the blacklist: the blacklist WINS. A coin on both lists is
blocked. Deny beating allow is the safe direction — you can always remove the
blacklist entry, whereas the reverse ordering lets a stale whitelist entry
resurrect a coin you deliberately banned.

Relationship to the other list mechanisms:
  watchlist.manual_adds  — ADDS coins to the scan universe (does not restrict)
  symbol_filter/blacklist — removes coins from the top-N universe
  FOCUSED_MODE + FOCUSED_SIZE=0 — an implicit whitelist (manual adds only)
This module is the explicit, per-strategy version of that last one.

Public API (identical signatures to blacklist.py):
  is_allowed(symbol, strategy=None) -> bool
  is_whitelisted(symbol, strategy=None) -> bool   (alias, reads naturally)
  add(symbol, strategies=None)          -> str
  remove(symbol, strategies=None)       -> bool
  get_all()                             -> dict
  is_active()                           -> bool   (any entries at all?)
"""

import os
import json
import logging

log = logging.getLogger("Whitelist")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FILE = os.path.join(_PROJECT_DIR, "data", "whitelist.json")


def _load() -> dict:
    """
    Read the whitelist from disk.

    Callers in a hot loop must call this ONCE per cycle and filter in memory —
    it opens and parses the file every time. Measured at 157us per call, so
    calling it per (symbol, strategy) costs ~71ms per scan against ~0.2ms when
    hoisted. symbol_filter._remove_blacklisted carries the same warning.
    """
    try:
        if os.path.exists(_FILE):
            with open(_FILE, encoding="utf-8") as _jf:
                raw = json.load(_jf)
            # Accept a bare list as "allowed for all strategies".
            if isinstance(raw, list):
                return {s.upper(): ["*"] for s in raw}
            return {k.upper(): v for k, v in raw.items()}
    except Exception as exc:
        log.warning(f"whitelist load failed: {exc}")
    return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        ordered = dict(sorted(data.items()))
        with open(_FILE, "w") as f:
            json.dump(ordered, f, indent=2)
    except Exception as exc:
        log.error(f"whitelist save failed: {exc}")


def is_active() -> bool:
    """True if the whitelist has any entries, i.e. it is actually restricting."""
    return bool(_load())


def is_allowed(symbol: str, strategy: str = None) -> bool:
    """
    May this symbol trade?

    An EMPTY whitelist allows everything — see the module docstring. Only once
    entries exist does it start restricting.
    """
    data = _load()
    if not data:
        return True
    return _allowed_in(data, symbol, strategy)


def _allowed_in(data: dict, symbol: str, strategy: str = None) -> bool:
    """
    In-memory form of is_allowed(), for hot loops that already hold the dict.

    live_scanner uses this so a full scan costs one disk read instead of
    symbols x strategies reads.
    """
    if not data:
        return True
    entry = data.get(symbol.upper())
    if entry is None:
        return False                      # restricting, and this coin is not listed
    if "*" in entry:
        return True
    if strategy and strategy.upper() in [s.upper() for s in entry]:
        return True
    return not strategy                   # listed per-strategy; no strategy given


# Reads naturally at some call sites; same function.
def is_whitelisted(symbol: str, strategy: str = None) -> bool:
    return is_allowed(symbol, strategy)


def add(symbol: str, strategies: list = None) -> str:
    """
    Allow `symbol`. With no strategies, allows it for all ("*").
    Returns a human-readable description of the resulting state.
    """
    symbol = symbol.upper()
    data = _load()
    if not strategies:
        data[symbol] = ["*"]
        _save(data)
        return f"{symbol} allowed for ALL strategies"

    strategies = [s.upper() for s in strategies]
    cur = data.get(symbol, [])
    if "*" in cur:
        return f"{symbol} is already allowed for ALL strategies"
    merged = sorted(set(cur) | set(strategies))
    data[symbol] = merged
    _save(data)
    return f"{symbol} allowed for {', '.join(merged)}"


def remove(symbol: str, strategies: list = None) -> bool:
    """
    Remove `symbol` from the whitelist, or just the named strategies.
    Returns True if anything changed.
    """
    symbol = symbol.upper()
    data = _load()
    if symbol not in data:
        return False

    if not strategies:
        del data[symbol]
        _save(data)
        return True

    strategies = [s.upper() for s in strategies]
    cur = [s for s in data[symbol] if s.upper() not in strategies]
    if cur == data[symbol]:
        return False
    if cur:
        data[symbol] = cur
    else:
        del data[symbol]
    _save(data)
    return True


def get_all() -> dict:
    return _load()


