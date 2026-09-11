"""
modules/blacklist.py
Persistent symbol blacklist — blocks specific coins from trading.

Storage: data/blacklist.json (dict format)

Format:
  {
    "HYPEUSDT": ["*"],           # blocked globally (all strategies)
    "BNBUSDT":  ["BGD"],         # blocked only for BGD
    "ETHUSDT":  ["BGD", "GRID"]  # blocked for BGD and GRID
  }

Public API:
  is_blacklisted(symbol, strategy=None) -> bool
  add(symbol, strategies=None)          -> str (description of what was set)
  remove(symbol, strategies=None)       -> bool
  get_all()                             -> dict
"""

import os
import json
import logging

log = logging.getLogger("Blacklist")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FILE = os.path.join(_PROJECT_DIR, "data", "blacklist.json")


def _load() -> dict:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, encoding="utf-8") as _jf:
                raw = json.load(_jf)
            # Migrate old list format → dict
            if isinstance(raw, list):
                return {s.upper(): ["*"] for s in raw}
            return {k.upper(): v for k, v in raw.items()}
    except Exception as exc:
        log.warning(f"blacklist load failed: {exc}")
    return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        ordered = dict(sorted(data.items()))
        with open(_FILE, "w") as f:
            json.dump(ordered, f, indent=2)
    except Exception as exc:
        log.error(f"blacklist save failed: {exc}")


def is_blacklisted(symbol: str, strategy: str = None) -> bool:
    """
    Check if symbol is blacklisted.
    If strategy is provided, checks strategy-specific block.
    If strategy is None, returns True only if globally blocked.
    """
    data = _load()
    entry = data.get(symbol.upper())
    if entry is None:
        return False
    if "*" in entry:
        return True  # globally blocked
    if strategy and strategy.upper() in [s.upper() for s in entry]:
        return True
    return False


def add(symbol: str, strategies: list = None) -> str:
    """
    Add symbol to blacklist.
    strategies=None or ["*"] → global block
    strategies=["BGD","GRID"] → block only those strategies
    Returns description string.
    """
    symbol = symbol.upper()
    data = _load()
    strats = [s.upper() for s in strategies] if strategies else ["*"]

    existing = data.get(symbol, [])
    if "*" in existing and "*" in strats:
        return f"{symbol} already globally blacklisted"

    if "*" in strats:
        data[symbol] = ["*"]
        desc = f"{symbol} globally blacklisted"
    else:
        # Merge with existing
        merged = list(set(existing + strats))
        if "*" in merged:
            merged = ["*"]
        data[symbol] = sorted(merged)
        desc = f"{symbol} blacklisted for {','.join(sorted(strats))}"

    _save(data)
    log.info(f"[Blacklist] {desc}")
    return desc


def remove(symbol: str, strategies: list = None) -> bool:
    """
    Remove symbol from blacklist.
    strategies=None → remove entirely
    strategies=["BGD"] → remove only BGD block (keep others)
    """
    symbol = symbol.upper()
    data = _load()
    if symbol not in data:
        return False

    if strategies is None:
        del data[symbol]
    else:
        existing = data[symbol]
        if "*" in existing:
            # Can't partially remove a global block — remove entirely
            del data[symbol]
        else:
            remaining = [s for s in existing if s.upper() not in [x.upper() for x in strategies]]
            if remaining:
                data[symbol] = remaining
            else:
                del data[symbol]

    _save(data)
    log.info(f"[Blacklist] Removed {symbol} {strategies or 'entirely'}")
    return True


def get_all() -> dict:
    return _load()


