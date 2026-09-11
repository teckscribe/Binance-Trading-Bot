"""
watchlist.py
Maintains a priority watchlist of top-N symbols by 24h USDT volume on Binance.

Two operating modes
-------------------
Normal mode (FOCUSED_MODE=false):
  Top-20 by volume are promoted to positions 1-20 of the 150-symbol scan list.
  The remaining 130 symbols follow. All strategies run across all 150 symbols.

Focused mode (FOCUSED_MODE=true):
  Bot scans ONLY the top-FOCUSED_SIZE symbols (default 5).
  Entire symbol universe = these 5 coins. Grid trades both sides;
  TP/DB/BBR fire on trends; coverage is complete across all regimes.

Manual overrides (both modes): add/remove specific coins via Telegram.
Manual adds go first in the list, before auto-ranked coins.

Auto-refreshes every REFRESH_HOURS hours from Binance.
Persists to data/watchlist.json.

Public API
----------
get_watchlist()               -> list[str]   # top-20 priority list
get_focused_watchlist()       -> list[str]   # top-FOCUSED_SIZE only
refresh_watchlist()           -> list[str]   # force fetch from exchange
add_to_watchlist(symbol)      -> bool
remove_from_watchlist(symbol) -> tuple[bool, str]
get_watchlist_info()          -> dict
"""

import os
import re
import json
import time
import logging
import requests
from datetime import datetime, timezone

from modules.auth_manager import BASE_URL, public_headers

log = logging.getLogger("Watchlist")

# --- Constants ----------------------------------------------------------------
WATCHLIST_SIZE   = 20      # symbols kept in priority watchlist (normal mode)
FOCUSED_SIZE     = 5       # symbols used as full universe in focused mode
REFRESH_HOURS    = 8          # fixed UTC windows: 00:00, 08:00, 16:00
MIN_VOLUME_USDT  = 1_000_000   # $1M/day floor
MIN_PRICE        = 0.001
# Minimum listing age. 90 -> 30 (2026-08-30, by decision).
#
# Read from Binance exchangeInfo `onboardDate`; 0 disables the filter, and a
# symbol with no onboardDate is KEPT (fails open).
#
# WHAT LOOSENING THIS ADMITS. Newly-listed perps are exactly the high-ATR
# movers CSM's momentum band fires on, so this raises signal volume — but the
# coins it lets in were NOT in any backtest behind the current config:
#   - the 90d datasets were built from symbols already past 90 days, so no
#     measured expectancy covers the 30-90 day cohort
#   - new listings have thinner books than the 0.03%/side slippage modelled
#   - CSM is currently SHORT-only, and a new listing's early move is often a
#     violent pump, which a short is on the wrong side of
# Measured on the 30d set, only 4 of 18 traded symbols were under 90 days, so
# the filter was not costing much volume at the time.
MIN_COIN_AGE_DAYS = int(os.getenv("MIN_COIN_AGE_DAYS", "30"))
MIN_RANGE_PCT    = 0.04        # 4% minimum 24h range — skip stable coins

EXCLUDED_BASES = {
    # Stablecoins
    "USDC", "BUSD", "TUSD", "USDD", "USDP", "FDUSD",
    "DAI",  "FRAX", "LUSD", "CRVUSD",
    # Wrapped tokens
    "WBTC", "WETH", "WBNB",
    # Tokenised US stocks (original set)
    "COIN",  "HOOD",  "TSLA",  "AAPL",  "NVDA",
    "GOOGL", "AMZN",  "MSFT",  "META",  "NFLX",
    "AMD",   "BABA",  "PYPL",  "UBER",  "SHOP",
    "SQ",    "SPOT",  "PLTR",  "ABNB",  "RBLX",
    "SNAP",  "INTC",  "ORCL",  "IBM",   "DIS",
    # Tokenised stocks — Korean / Asian (Binance 2025-2026 additions)
    "MU",       # Micron Technology
    "MUU",      # Micron variant
    "SKHYNIX",  # SK Hynix
    "SKHY",     # SK Hynix variant
    "SNDK",     # SanDisk / Samsung NAND
    "SAMSUNG",  # Samsung Electronics
    "HEI",      # Heico Corporation
    "AAOI",     # Applied Optoelectronics
    "NBIS",     # Nebius Group
    "CRCL",     # Circle Internet Financial
    # Tokenised ETFs / indices
    "QQQ",      # Invesco QQQ (Nasdaq-100)
    "SPY",      # SPDR S&P 500
    "EWY",      # iShares MSCI South Korea
    "KORU",     # Direxion Daily South Korea Bull 3×
    "SOXS",     # Direxion Daily Semiconductor Bear 3×
    "SPCX",     # S&P 500 Communications
    "SNXX",     # Nuveen Short-Term Muni
    "CYS",      # CYS Investments (mortgage REIT)
    "UB",       # Ultra T-Bond ETF
    "HOME",     # Real-estate ETF
    "ESPORTS",  # Esports ETF
    # Commodities / real-world assets
    "XAU",  "XAG",  "CL",
    "BZ",       # Brent Crude
    "XAUT",     # Tether Gold
    "PAXG",     # PAX Gold
    # Leveraged / inverse token bases
    "SOXL", "BULL", "BEAR",
    # Misc non-crypto
    "MRVL",  "BTW",
}

_PROJECT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST_PATH = os.path.join(_PROJECT_DIR, "data", "watchlist.json")


# --- Internal helpers ---------------------------------------------------------

def _load() -> dict:
    """Load watchlist JSON from disk. Returns empty structure on missing/corrupt."""
    if not os.path.exists(WATCHLIST_PATH):
        return {"symbols": [], "manual_adds": [], "saved_at": 0, "source": "none"}
    try:
        with open(WATCHLIST_PATH) as f:
            return json.load(f)
    except Exception as exc:
        log.warning(f"Watchlist load failed: {exc}")
        return {"symbols": [], "manual_adds": [], "saved_at": 0, "source": "none"}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    try:
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning(f"Watchlist save failed: {exc}")


def _fetch_crypto_symbol_set() -> set[str]:
    """
    Return the set of USDM-Futures symbols that Binance classifies as pure crypto
    (underlyingType == "COIN", contractType == "PERPETUAL", status == "TRADING").

    This is the authoritative dynamic filter — no manual exclusion list needed.
    Falls back to an empty set on failure (caller then relies on EXCLUDED_BASES).
    """
    url = BASE_URL + "/fapi/v1/exchangeInfo"
    try:
        resp = requests.get(url, headers=public_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning(f"exchangeInfo fetch failed (crypto filter disabled): {exc}")
        return set()

    crypto = set()
    too_new = []
    now_ms = time.time() * 1000
    age_ms = MIN_COIN_AGE_DAYS * 86400 * 1000
    for sym in data.get("symbols", []):
        if (sym.get("underlyingType") == "COIN"
                and sym.get("contractType") == "PERPETUAL"
                and sym.get("status") == "TRADING"
                and sym.get("symbol", "").endswith("USDT")):
            onboard = sym.get("onboardDate", 0)
            if age_ms > 0 and onboard and (now_ms - onboard) < age_ms:
                too_new.append(sym["symbol"])
                continue
            crypto.add(sym["symbol"])

    if too_new:
        log.info(f"Exchange info: filtered {len(too_new)} coins younger than {MIN_COIN_AGE_DAYS}d: {too_new[:10]}")
    log.info(f"Exchange info: {len(crypto)} pure-crypto USDT perpetuals found")
    return crypto


def _fetch_top_by_volume(n: int) -> list[str]:
    """Fetch top N USDM-Futures symbols by 24h quoteVolume from Binance.

    Uses Binance's own underlyingType classification to exclude tokenised
    stocks, ETFs, and commodities dynamically.  Falls back to EXCLUDED_BASES
    if the exchangeInfo call fails.
    """
    crypto_symbols = _fetch_crypto_symbol_set()   # empty set = fallback mode

    url = BASE_URL + "/fapi/v1/ticker/24hr"
    try:
        resp = requests.get(url, headers=public_headers(), timeout=10)
        resp.raise_for_status()
        tickers = resp.json()
        if not isinstance(tickers, list):
            log.error(f"Watchlist fetch: unexpected response type {type(tickers)}")
            return []
    except Exception as exc:
        log.error(f"Watchlist fetch failed: {exc}")
        return []

    qualified = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        # Primary filter: Binance's own classification (dynamic)
        if crypto_symbols and symbol not in crypto_symbols:
            continue

        # Secondary filter: manual exclusion list (catches edge cases / fallback)
        base = symbol[:-4]
        if base in EXCLUDED_BASES:
            continue
        if not base.isascii() or not base.isalnum():
            continue
        if re.match(r"^\d+[LSls]", base):
            continue
        if any(kw in base.upper() for kw in ("UP", "DOWN", "HEDGE")):
            continue

        try:
            price    = float(t.get("lastPrice",   "0") or "0")
            volume   = float(t.get("quoteVolume",  "0") or "0")
            high_24h = float(t.get("highPrice",    "0") or "0")
            low_24h  = float(t.get("lowPrice",     "0") or "0")
        except (ValueError, TypeError):
            continue
        if price < MIN_PRICE or volume < MIN_VOLUME_USDT:
            continue
        if low_24h > 0 and (high_24h - low_24h) / low_24h < MIN_RANGE_PCT:
            continue
        qualified.append((symbol, volume))

    qualified.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in qualified[:n]]


# --- Public API ---------------------------------------------------------------

def _is_stale(saved_at: float) -> bool:
    """True if saved_at falls in an earlier UTC window than now.

    Windows are fixed 8-hour slots: 00-08, 08-16, 16-24 UTC.
    A refresh at 07:59 becomes stale at 08:00; one at 08:01 stays
    fresh until 16:00.
    """
    now = datetime.now(timezone.utc)
    current_window = now.hour // REFRESH_HOURS
    if saved_at <= 0:
        return True
    saved = datetime.fromtimestamp(saved_at, tz=timezone.utc)
    if saved.date() != now.date():
        return True
    return saved.hour // REFRESH_HOURS != current_window


def get_watchlist() -> list[str]:
    """
    Return the current watchlist (top-20 by volume + manual adds).
    Auto-refreshes at fixed UTC windows (00:00, 08:00, 16:00).
    Falls back to stale cache on API failure.
    """
    data = _load()

    if _is_stale(data.get("saved_at", 0)) or not data.get("symbols"):
        refreshed = _fetch_top_by_volume(WATCHLIST_SIZE)
        if refreshed:
            data["symbols"]    = refreshed
            data["saved_at"]   = time.time()
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            data["source"]     = "auto"
            _save(data)
            log.info(f"Watchlist auto-refreshed: {len(refreshed)} symbols")
        else:
            log.warning("Watchlist refresh failed -- using stale data")

    # Merge: manual adds go first, then auto-ranked (deduplicated)
    manual = data.get("manual_adds", [])
    base   = data.get("symbols", [])
    merged = manual + [s for s in base if s not in manual]
    return merged


def refresh_watchlist() -> list[str]:
    """Force-fetch top-20 from Binance and persist."""
    symbols = _fetch_top_by_volume(WATCHLIST_SIZE)
    data    = _load()
    if symbols:
        data["symbols"]    = symbols
        data["saved_at"]   = time.time()
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        data["source"]     = "manual_refresh"
        _save(data)
        log.info(f"Watchlist refreshed: {len(symbols)} symbols")
    return get_watchlist()


def get_focused_watchlist(n: int = FOCUSED_SIZE) -> list[str]:
    """
    Return the focused symbol universe: top-N by volume + manual adds.

    Used when FOCUSED_MODE=true — this list IS the entire scan universe.
    Manual adds are always included (placed first), then auto-ranked coins
    up to n total. If the exchange is unreachable, falls back to stale cache.

    Args:
        n: Maximum number of auto-ranked coins (default: FOCUSED_SIZE = 5).

    Returns:
        List of symbols. Manual adds may push total above n.
    """
    data = _load()

    if _is_stale(data.get("saved_at", 0)) or len(data.get("symbols", [])) < n:
        refreshed = _fetch_top_by_volume(max(WATCHLIST_SIZE, n))
        if refreshed:
            data["symbols"]    = refreshed
            data["saved_at"]   = time.time()
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            data["source"]     = "auto"
            _save(data)
            log.info(f"Focused watchlist auto-refreshed (top-{n} from {len(refreshed)})")
        else:
            log.warning("Focused watchlist refresh failed -- using stale data")

    manual = data.get("manual_adds", [])
    auto   = [s for s in data.get("symbols", []) if s not in manual][:n]
    merged = manual + auto
    log.debug(f"Focused watchlist: {merged}")
    return merged


def add_to_watchlist(symbol: str) -> bool:
    """
    Add a symbol to the manual watchlist.
    Returns True if added, False if already present.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    data   = _load()
    manual = data.get("manual_adds", [])
    if symbol in manual:
        return False
    manual.append(symbol)
    data["manual_adds"] = manual
    _save(data)
    log.info(f"Watchlist: manually added {symbol}")
    return True


def remove_from_watchlist(symbol: str) -> tuple[bool, str]:
    """
    Remove a symbol from the watchlist.
    Returns (success, message).
    Manual adds: removed permanently.
    Auto-ranked: removed from auto list only until next refresh.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    data   = _load()
    manual = data.get("manual_adds", [])
    auto   = data.get("symbols", [])

    if symbol in manual:
        manual.remove(symbol)
        data["manual_adds"] = manual
        _save(data)
        return True, f"{symbol} removed from manual watchlist"

    if symbol in auto:
        auto.remove(symbol)
        data["symbols"] = auto
        _save(data)
        return True, f"{symbol} removed (will return on next auto-refresh)"

    return False, f"{symbol} not in watchlist"


def get_watchlist_info() -> dict:
    """Return metadata about the current watchlist state."""
    data     = _load()
    saved_at = data.get("saved_at", 0)
    age_h    = (time.time() - saved_at) / 3600
    manual   = data.get("manual_adds", [])
    auto     = data.get("symbols", [])
    merged   = manual + [s for s in auto if s not in manual]
    focused  = manual + [s for s in auto if s not in manual][:FOCUSED_SIZE]
    return {
        "symbols":        merged,
        "focused":        focused,
        "auto":           auto,
        "manual_adds":    manual,
        "count":          len(merged),
        "focused_size":   FOCUSED_SIZE,
        "age_hours":      round(age_h, 1),
        "updated_at":     data.get("updated_at", "never"),
        "source":         data.get("source", "none"),
    }

