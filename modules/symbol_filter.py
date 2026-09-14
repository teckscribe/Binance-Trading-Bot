"""
symbol_filter.py
Fetches and filters the top USDT-M Futures trading pairs on Binance.

Filtering criteria (applied in order):
  1. Symbol ends in USDT (USDM perpetuals only)
  2. 24h quote volume >= MIN_VOLUME_USDT  (liquidity floor)
  3. Last price > MIN_PRICE              (eliminates micro-cap dust)
  4. Exclude stablecoins and wrapped tokens (USDC, BUSD, etc.)
  5. Exclude tokenised US stock perpetuals (AAPL, TSLA, etc.)
  6. Exclude leveraged tokens (3LUSDT, BTCUPUSDT, etc.)
  7. Sort by 24h quote volume descending
  8. Keep top TOP_N symbols

Symbol list is cached to disk (data/symbols/top200.json) and
refreshed every CACHE_TTL_MINUTES minutes.

Binance USDM ticker endpoint:
  GET /fapi/v1/ticker/24hr  (public, no auth)
  Returns all USDM perpetual markets.
  Key fields per ticker:
    symbol       : e.g. 'BTCUSDT'
    lastPrice    : last trade price
    quoteVolume  : 24h volume in quote asset (USDT)
"""

import os
import re
import json
import time
import logging
import requests
from datetime import datetime, timezone

from modules.auth_manager import BASE_URL, public_headers

log = logging.getLogger("SymbolFilter")

# ─── Constants ────────────────────────────────────────────────────────────────
TOP_N             = 200
MIN_VOLUME_USDT   = 1_000_000     # Minimum 24h USDT volume ($1M)
MIN_PRICE         = 0.001         # Exclude sub-cent tokens
MIN_RANGE_PCT     = 0.04          # 4% minimum 24h high-low range — filters stable coins
CACHE_TTL_MINUTES = 60            # Refresh symbol list every hour

_MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_MODULE_DIR)
CACHE_PATH   = os.path.join(_PROJECT_DIR, "data", "symbols", "top200.json")

# Stablecoins and wrapped tokens — not tradeable as directional futures
EXCLUDED_BASES = {
    # Stablecoins
    "USDC", "BUSD", "TUSD", "USDD", "USDP", "FDUSD", "USDE",
    "DAI",  "FRAX", "LUSD", "CRVUSD", "EUR", "GBP",
    # Wrapped tokens
    "WBTC", "WETH", "WBNB",
    # Tokenised US / International stocks & ETFs
    "COIN",  "HOOD",  "TSLA",  "AAPL",  "NVDA",
    "GOOGL", "AMZN",  "MSFT",  "META",  "NFLX",
    "AMD",   "BABA",  "PYPL",  "UBER",  "SHOP",
    "SQ",    "SPOT",  "PLTR",  "ABNB",  "RBLX",
    "SNAP",  "INTC",  "ORCL",  "IBM",   "DIS",
    "SKHYNIX", "SKHY", "SNDK", "MU", "MUU", "SAMSUNG",
    "HEI", "AAOI", "NBIS", "CRCL", "QQQ", "SPY", "EWY",
    "KORU", "SOXS", "SPCX", "SNXX", "CYS", "UB", "HOME",
    "ESPORTS", "BZ",
    # Commodities / Precious Metals / Gold tokens
    "XAU",  "XAG",  "SOXL", "CL", "MRVL", "XAUT", "PAXG",
    # Leveraged token bases
    "BULL",  "BEAR",
}


# ─── Fetch ────────────────────────────────────────────────────────────────────

def _fetch_all_tickers() -> list[dict]:
    """
    Fetch all USDM Futures 24hr tickers from Binance.

    Returns:
        List of raw ticker dicts. Empty list on failure.
    """
    url = BASE_URL + "/fapi/v1/ticker/24hr"
    try:
        resp = requests.get(url, headers=public_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            log.error(f"Unexpected ticker response type: {type(data)}")
            return []
        return data
    except Exception as exc:
        log.error(f"Ticker fetch failed: {exc}")
        return []


# ─── Filter + Rank ────────────────────────────────────────────────────────────

def _filter_and_rank(tickers: list[dict]) -> list[str]:
    """
    Apply quality filters and return top N symbols sorted by volume.

    Args:
        tickers : Raw list from _fetch_all_tickers()

    Returns:
        List of symbol strings e.g. ['BTCUSDT', 'ETHUSDT', ...]
    """
    qualified = []

    for t in tickers:
        symbol = t.get("symbol", "")

        # USDM perpetuals end in USDT
        if not symbol.endswith("USDT"):
            continue

        try:
            last_price = float(t.get("lastPrice",  "0") or "0")
            volume_24h = float(t.get("quoteVolume", "0") or "0")
            high_24h   = float(t.get("highPrice",   "0") or "0")
            low_24h    = float(t.get("lowPrice",    "0") or "0")
        except (ValueError, TypeError):
            continue

        if low_24h > 0:
            range_pct = (high_24h - low_24h) / low_24h
            if range_pct < MIN_RANGE_PCT:
                continue

        # Extract base asset (everything before USDT)
        base = symbol[:-4]   # faster than .replace("USDT", "")

        # Exclude stablecoins, wrapped tokens and stock tokens
        if base in EXCLUDED_BASES:
            continue

        # Exclude leveraged/directional tokens by pattern:
        #   3LUSDT, 2SUSDT → starts with digits then L/S
        #   BTCUPUSDT, ETHDOWNUSDT → contains UP/DOWN
        if re.match(r"^\d+[LSls]", base):
            continue
        if any(kw in base.upper() for kw in ("UP", "DOWN", "HEDGE")):
            continue

        # Price floor
        if last_price < MIN_PRICE:
            continue

        # Volume floor
        if volume_24h < MIN_VOLUME_USDT:
            continue

        qualified.append((symbol, volume_24h))

    # Sort by 24h volume descending, take top N
    qualified.sort(key=lambda x: x[1], reverse=True)
    result = [sym for sym, _ in qualified[:TOP_N]]
    log.info(f"Volatility filter: {len(result)} symbols passed (min 24h range {MIN_RANGE_PCT*100:.0f}%)")
    return result


# ─── Cache management ─────────────────────────────────────────────────────────

def _load_cache() -> list[str] | None:
    """
    Load symbol list from disk cache if still fresh.

    Returns:
        List of symbols if cache is valid, None otherwise.
    """
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH) as f:
            cached = json.load(f)
        age_min = (time.time() - cached.get("saved_at", 0)) / 60
        if age_min > CACHE_TTL_MINUTES:
            log.info(f"Symbol cache is {age_min:.0f}m old — refreshing")
            return None
        symbols = cached.get("symbols", [])
        log.info(f"Symbol cache hit: {len(symbols)} symbols ({age_min:.0f}m old)")
        return symbols
    except Exception as exc:
        log.warning(f"Cache read failed: {exc}")
        return None


def _save_cache(symbols: list[str]) -> None:
    """Write symbol list to disk cache."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump({
                "saved_at": time.time(),
                "count":    len(symbols),
                "symbols":  symbols,
                "updated":  datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
        log.info(f"Symbol cache saved: {len(symbols)} symbols → {CACHE_PATH}")
    except Exception as exc:
        log.warning(f"Cache write failed: {exc}")


# ─── Public API ───────────────────────────────────────────────────────────────

def _remove_blacklisted(symbols: list[str]) -> list[str]:
    """
    Filter out symbols currently in the blacklist.
    Called on every get_top_symbols() return — not just on cache miss —
    so a symbol banned mid-session is excluded on the next scan cycle
    without waiting for the 1-hour cache refresh.
    Loads blacklist JSON once and filters in memory (avoids 200 disk reads).
    Fail-open: if blacklist module errors, the full list is returned.
    """
    try:
        from modules.symbol_blacklist import get_blacklist
        banned   = set(get_blacklist().keys())   # one JSON load for all symbols
        filtered = [s for s in symbols if s not in banned]
        removed  = len(symbols) - len(filtered)
        if removed:
            log.info(f"[Blacklist] Removed {removed} banned symbol(s) from scan list")
        return filtered
    except Exception as exc:
        log.warning(f"[Blacklist] filter failed (fail-open): {exc}")
        return symbols


def get_top_symbols(force_refresh: bool = False) -> list[str]:
    """
    Return top TOP_N USDM Futures symbols by 24h volume.
    Uses disk cache — refreshed every CACHE_TTL_MINUTES.

    Args:
        force_refresh : Skip cache and re-fetch from exchange.

    Returns:
        List of symbol strings. Falls back to stale cache on API failure.
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return _remove_blacklisted(cached)

    log.info("Fetching live ticker data from Binance for symbol ranking...")
    tickers = _fetch_all_tickers()

    if not tickers:
        # Fall back to stale cache rather than returning nothing
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH) as f:
                    stale = json.load(f).get("symbols", [])
                if stale:
                    log.warning("API failed — using stale symbol cache")
                    return _remove_blacklisted(stale)
            except Exception:
                pass
        log.error("No tickers fetched and no cache available")
        return []

    symbols = _filter_and_rank(tickers)
    if symbols:
        _save_cache(symbols)

    symbols = _remove_blacklisted(symbols)
    log.info(f"Symbol list ready: {len(symbols)} symbols (top by USDT volume)")
    return symbols


