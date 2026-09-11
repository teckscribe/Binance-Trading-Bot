"""
data_feed.py
Binance USDM Futures OHLCV candle fetcher.

Binance USDM Futures candle endpoint:
  GET /fapi/v1/klines
  Params:
    symbol   : e.g. BTCUSDT
    interval : 1m | 15m | 1h | 4h | 1d
    limit    : max 1500 (default 500)

Candle response — each row is an array:
  [0]  openTime (ms)
  [1]  open
  [2]  high
  [3]  low
  [4]  close
  [5]  volume (base asset)
  [6]  closeTime (ms)
  [7]  quoteAssetVolume   ← we use this as 'volume' (USDT-denominated)
  [8]  numberOfTrades
  [9]  takerBuyBaseAssetVolume
  [10] takerBuyQuoteAssetVolume
  [11] ignore

Binance returns candles oldest-first (no reversal needed — opposite of Bitget).

Normalised output columns:
  timestamp (datetime UTC), open, high, low, close, volume

Other endpoints used:
  GET /fapi/v1/premiumIndex  — mark price + current funding rate (public)
  GET /fapi/v1/openInterest  — open interest per symbol (public)
"""

import time
import logging
import requests
import pandas as pd

from modules.auth_manager import SESSION, BASE_URL, public_headers

log = logging.getLogger("DataFeed")

MAX_RETRIES     = 2       # 1 initial + 1 retry on network error
RETRY_DELAY     = 0.5     # seconds base, doubled per retry
REQUEST_TIMEOUT = 8       # seconds per HTTP call

# Binance USDM Futures interval strings
GRAN = {
    "1m":  "1m",
    "15m": "15m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}


# ─── Low-level fetch ──────────────────────────────────────────────────────────

def _fetch_candles_raw(symbol: str, interval: str, limit: int = 200) -> list:
    """
    Fetch candles from Binance USDM Futures.
    Public endpoint — no auth needed.

    Args:
        symbol   : e.g. 'BTCUSDT'
        interval : Binance interval string e.g. '1m', '15m', '1h'
        limit    : number of bars (max 1500)

    Returns:
        List of raw candle arrays, oldest-first.
        Empty list on all failures.
    """
    url    = BASE_URL + "/fapi/v1/klines"
    params = {
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(
                url, params=params,
                headers=public_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 400:
                log.debug(f"No data [{symbol} {interval}] — 400, skipping")
                return []
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            wait = RETRY_DELAY * (2 ** (attempt - 1))
            log.warning(
                f"Candle fetch attempt {attempt}/{MAX_RETRIES} failed "
                f"[{symbol} {interval}]: {exc}. Retrying in {wait:.1f}s"
            )
            time.sleep(wait)

    log.debug(f"All fetch attempts failed for {symbol} {interval}")
    return []


# ─── Normalisation ────────────────────────────────────────────────────────────

def _to_dataframe(raw: list) -> pd.DataFrame:
    """
    Convert raw Binance candle array list to a clean DataFrame.

    Binance returns candles oldest-first — no reversal needed.
    We use quoteAssetVolume (index 7) as 'volume' for consistency
    with strategies that filter on USDT-denominated volume.

    Returns empty DataFrame on bad input.
    """
    if not raw:
        return pd.DataFrame()

    try:
        df = pd.DataFrame(raw, columns=[
            "ts", "open", "high", "low", "close",
            "vol_base", "close_time", "volume",
            "num_trades", "taker_buy_base", "taker_buy_quote", "_ignore",
        ])
        df = df.astype({
            "ts":       "int64",
            "open":     "float64",
            "high":     "float64",
            "low":      "float64",
            "close":    "float64",
            "volume":   "float64",
        })
        df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as exc:
        log.error(f"DataFrame parse failed: {exc}")
        return pd.DataFrame()


# ─── Public API ───────────────────────────────────────────────────────────────

def fetch_candles(symbol: str, timeframe: str = "1m", limit: int = 200) -> pd.DataFrame:
    """
    Fetch OHLCV candles for one symbol and timeframe.

    Args:
        symbol    : e.g. 'BTCUSDT'
        timeframe : '1m' | '15m' | '1h' | '4h' | '1d'
        limit     : bars to fetch (default 200, max 1500)

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
        Sorted ascending (oldest first). Empty DataFrame on failure.
    """
    interval = GRAN.get(timeframe)
    if interval is None:
        log.error(f"Unknown timeframe '{timeframe}'. Valid: {list(GRAN.keys())}")
        return pd.DataFrame()

    raw = _fetch_candles_raw(symbol, interval, limit)
    return _to_dataframe(raw)


def fetch_multi_timeframe(
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Fetch 1m, 15m, and 1h candles for one symbol.

    Returns:
        (df_1m, df_15m, df_1h) — each a DataFrame or empty on failure.
    """
    df_1m  = fetch_candles(symbol, "1m",  limit=200)
    df_15m = fetch_candles(symbol, "15m", limit=200)
    df_1h  = fetch_candles(symbol, "1h",  limit=200)
    return df_1m, df_15m, df_1h


def fetch_funding_rate(symbol: str) -> float | None:
    """
    Fetch the current funding rate for a USDM Futures symbol.

    Uses /fapi/v1/premiumIndex which returns both mark price and funding rate
    in one call (saving an API round-trip vs separate endpoints).

    Positive funding → longs pay shorts (market is long-heavy).
    Negative funding → shorts pay longs (market is short-heavy).

    Returns:
        Float funding rate (e.g. 0.0003 = 0.03%) or None on failure.
    """
    url    = BASE_URL + "/fapi/v1/premiumIndex"
    params = {"symbol": symbol}
    try:
        resp = SESSION.get(
            url, params=params,
            headers=public_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        # Binance returns empty string "" for fundingRate between funding intervals
        # (every 8 hours). Treat empty string as 0.0, not None.
        rate = data.get("fundingRate")
        if rate is None or rate == "":
            return 0.0
        return float(rate)
    except Exception as exc:
        log.warning(f"Funding rate fetch failed [{symbol}]: {exc}")
        return None


def fetch_mark_price(symbol: str) -> float | None:
    """
    Fetch the current mark price for a USDM Futures symbol.

    Used by fast cycle to synthesise a tick row for open positions
    without fetching full candle data.

    Returns:
        Float mark price or None on failure.
    """
    url    = BASE_URL + "/fapi/v1/premiumIndex"
    params = {"symbol": symbol}
    try:
        resp = SESSION.get(
            url, params=params,
            headers=public_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("markPrice")
        return float(price) if price is not None else None
    except Exception as exc:
        log.warning(f"Mark price fetch failed [{symbol}]: {exc}")
        return None


def fetch_open_interest(symbol: str) -> float | None:
    """
    Fetch current open interest (in contracts) for a symbol.

    Returns:
        Float OI value or None on failure.
    """
    url    = BASE_URL + "/fapi/v1/openInterest"
    params = {"symbol": symbol}
    try:
        resp = SESSION.get(
            url, params=params,
            headers=public_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data  = resp.json()
        oi    = data.get("openInterest")
        return float(oi) if oi is not None else None
    except Exception as exc:
        log.warning(f"OI fetch failed [{symbol}]: {exc}")
        return None


