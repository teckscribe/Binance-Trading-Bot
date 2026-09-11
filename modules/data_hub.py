"""
modules/data_hub.py
Parallel data fetch orchestrator.

Conditional optimisation: 1h candles are fetched per-symbol ONLY when
the regime is OVERHEATED or OVERSOLD (the only regimes where the FF
strategy is permitted and needs >=25 1h bars).

In BULL_TREND / BEAR_TREND / RANGING the bot skips 1h per-symbol, saving
~100 API calls per full cycle (100 symbols × 1 timeframe = 100 calls).

fetch_btc_reference() always fetches BTC + ETH + SOL 1h for the
3-coin regime classifier (independent of per-symbol 1h fetch).
"""

import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

from modules.data_feed import fetch_candles, fetch_funding_rate, fetch_open_interest, fetch_mark_price

log = logging.getLogger("DataHub")

MAX_CONCURRENT = 10
MIN_REQ_GAP    = 0.1

_req_lock = threading.Lock()
_last_req  = [0.0]


def _rate_gated_fetch(symbol, timeframe, limit):
    with _req_lock:
        elapsed = time.monotonic() - _last_req[0]
        if elapsed < MIN_REQ_GAP:
            time.sleep(MIN_REQ_GAP - elapsed)
        _last_req[0] = time.monotonic()
    return fetch_candles(symbol, timeframe, limit)


# Regimes in which per-symbol 1h data is needed (FF strategy).
_REGIMES_NEEDING_1H = frozenset({"OVERHEATED", "OVERSOLD"})


# ─── 1h candle cache ─────────────────────────────────────────────────────────
# A 1h candle only changes once an hour, but the scan runs every 60s — so
# re-fetching 100 symbols' 1h data every cycle is ~100 wasted API calls per
# minute and pushed the scan from 20s to 43s, leaving little of the 60s budget.
#
# Only the newest (still-forming) bar moves within the hour, and CSM uses these
# bars for a 24-hour momentum measure, where a few minutes of staleness on one
# bar is immaterial. Entry price, SL and TP all come from fresh 15m data.
_ONE_H_TTL   = max(60, int(os.getenv("ONE_H_CACHE_TTL_SEC", "600")))
_1h_cache    = {}                  # symbol -> (monotonic_ts, DataFrame)
_1h_lock     = threading.Lock()
_1h_stats    = {"hit": 0, "miss": 0}


def _get_1h(symbol):
    """Return cached 1h candles for `symbol`, fetching only when stale."""
    now = time.monotonic()

    with _1h_lock:
        entry = _1h_cache.get(symbol)
        if entry is not None and (now - entry[0]) < _ONE_H_TTL:
            _1h_stats["hit"] += 1
            # Copy so a caller can never mutate what the next cycle will reuse.
            return entry[1].copy()

    df = _rate_gated_fetch(symbol, "1h", limit=50)

    with _1h_lock:
        _1h_stats["miss"] += 1
        # Never cache an empty/failed fetch — that would starve CSM for the
        # whole TTL. Leaving it uncached means we simply retry next cycle.
        if df is not None and not df.empty:
            _1h_cache[symbol] = (now, df)
            # Hand back a copy on the miss path too. Returning the stored
            # object itself would let a caller mutate what every subsequent
            # cache hit reuses for the rest of the TTL.
            return df.copy()
    return df


def prune_1h_cache(keep: set) -> int:
    """Drop cached symbols no longer on the watchlist. Returns count removed."""
    with _1h_lock:
        stale = [s for s in _1h_cache if s not in keep]
        for s in stale:
            _1h_cache.pop(s, None)
    return len(stale)


# ─── 15m candle: NEVER cached ────────────────────────────────────────────────
# A 15m cache was tried and removed 2026-09-05. It cannot be made both useful
# and safe:
#   - Useful requires a TTL >= the 60s scan interval, but the scan is the only
#     caller and it runs >= 60s apart, so any shorter TTL is a guaranteed 0%
#     hit (saved nothing).
#   - Safe forbids a TTL that hits at all: CSM takes its ENTRY PRICE, SL, TP and
#     ATR from df_15m['close'].iloc[-1] (the live forming bar), so a cache hit
#     would anchor a live entry and its stop to a stale candle.
# Binance returns all 200 bars in one call, so there is no partial fetch to
# save either. 15m is always fetched fresh.


# ─── BTC reference cache ─────────────────────────────────────────────────────
# BTC/ETH/SOL 1h + funding + OI for regime classification.  The 1h bars change
# once per hour and funding/OI update every ~8h/5m respectively.  A 300s TTL
# avoids re-fetching on every 60s scan cycle (saves 5 API calls × ~4 reuses).
_BTC_REF_TTL   = max(60, int(os.getenv("BTC_REF_CACHE_TTL_SEC", "60")))
_btc_ref_cache = {"ts": 0.0, "data": None}
_btc_ref_lock  = threading.Lock()


def _fetch_symbol(symbol, need_1h: bool = False, depth_1m: int = 200):
    """
    Fetch 1m + 15m always. 1h only when a permitted strategy declares
    REQUIRES_1H — and then via the TTL cache rather than every cycle.

    Args:
        symbol   : e.g. 'BTCUSDT'
        need_1h  : True iff a strategy scanning this cycle reads df_1h.
        depth_1m : number of 1m bars to fetch (default 200; strategies
                   that resample 1m→5m need 1500).
    """
    df_1m  = _rate_gated_fetch(symbol, "1m",  limit=depth_1m)
    df_15m = _rate_gated_fetch(symbol, "15m", limit=200)   # always fresh — see note above
    df_1h  = _get_1h(symbol) if need_1h else pd.DataFrame()
    return symbol, df_1m, df_15m, df_1h


def fetch_all_symbols(symbols, max_workers=MAX_CONCURRENT, regime: str = "",
                      need_1h: bool | None = None, depth_1m: int = 200):
    """
    Fetch 1m + 15m (+ optionally 1h) candles for all symbols in parallel.

    Args:
        symbols     : list of symbol strings
        max_workers : thread pool size
        regime      : current regime name. Legacy fallback for deciding the 1h
                      fetch when `need_1h` is not supplied.
        need_1h     : explicit override. The caller knows which strategies are
                      permitted this cycle and whether any declares
                      REQUIRES_1H, which is the real dependency — regime is
                      only a proxy and got it wrong for CSM (it needs 1h in
                      every regime, but the regime gate only allowed
                      OVERHEATED/OVERSOLD, so CSM never fired).
        depth_1m    : number of 1m bars to fetch (default 200; strategies
                      that resample 1m→5m need 1500).
    """
    if need_1h is None:
        need_1h = regime in _REGIMES_NEEDING_1H
    result     = {}
    ok_count   = 0
    fail_count = 0

    log.info(
        f"Fetching data for {len(symbols)} symbols ({max_workers} workers) "
        f"| 1h per-symbol: {'YES' if need_1h else 'no'} (regime={regime or '-'})"
    )
    t_start = time.monotonic()

    # Drop stale 1h entries for symbols that have rotated off the watchlist.
    if need_1h:
        prune_1h_cache(set(symbols))
        _hits_before = _1h_stats["hit"]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_symbol, sym, need_1h, depth_1m): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                symbol, df_1m, df_15m, df_1h = future.result()
                if df_1m is None or df_1m.empty:
                    fail_count += 1
                    continue
                result[symbol] = (df_1m, df_15m, df_1h)
                ok_count += 1
            except Exception as exc:
                log.warning(f"Fetch failed [{sym}]: {exc}")
                fail_count += 1

    elapsed = time.monotonic() - t_start
    cache_note = ""
    if need_1h:
        hits = _1h_stats["hit"] - _hits_before
        cache_note = f" | 1h cache: {hits}/{len(symbols)} reused"
    log.info(
        f"Data fetch complete: {ok_count} ok, {fail_count} failed "
        f"in {elapsed:.1f}s{cache_note}"
    )
    return result


def fetch_btc_reference():
    """
    Fetch 1h candles for BTC + ETH + SOL for 3-coin regime classification.
    Also fetches BTC funding rate and open interest.

    Returns cached result within _BTC_REF_TTL (default 300s) since 1h bars
    and funding/OI change slowly relative to the 60s scan cycle.

    Returns:
        dict with keys: btc_1h, eth_1h, sol_1h, funding, oi
    """
    now = time.monotonic()
    with _btc_ref_lock:
        if _btc_ref_cache["data"] is not None and (now - _btc_ref_cache["ts"]) < _BTC_REF_TTL:
            log.debug("BTC reference: cache hit")
            return _btc_ref_cache["data"]

    log.debug("Fetching BTC/ETH/SOL 1h reference data...")
    btc_1h  = fetch_candles("BTCUSDT", "1h", limit=50)
    eth_1h  = fetch_candles("ETHUSDT", "1h", limit=50)
    sol_1h  = fetch_candles("SOLUSDT", "1h", limit=50)
    funding = fetch_funding_rate("BTCUSDT")
    oi      = fetch_open_interest("BTCUSDT")

    result = {
        "btc_1h":  btc_1h,
        "eth_1h":  eth_1h,
        "sol_1h":  sol_1h,
        "funding": funding if funding is not None else 0.0,
        "oi":      oi,
    }

    # Only cache if we got valid BTC data (the critical piece).
    if btc_1h is not None and not btc_1h.empty:
        with _btc_ref_lock:
            _btc_ref_cache["ts"] = now
            _btc_ref_cache["data"] = result

    return result


def fetch_active_positions_data(symbols: list) -> dict:
    """
    Fast data fetch for open position management cycles.

    Only fetches 1m candles — 15m and 1h are not needed for SL/TP/trail
    checks which only use df_1m. Also fetches the current mark price for
    each symbol so TP/SL can be evaluated against the live tick price,
    not just the last closed candle.

    The mark price is injected as a synthetic final row in the 1m DataFrame:
      - timestamp : now (UTC)
      - open      : mark price
      - high      : max(last candle high, mark price)  ← catches TP touch
      - low       : min(last candle low,  mark price)  ← catches SL touch
      - close     : mark price
      - volume    : 0 (unknown intra-candle)

    This means manage() will see the mark price as the current "close"
    and the high/low will correctly reflect any TP or SL touch between
    candle closes — which is exactly what we need at 15s intervals.

    Args:
        symbols : list of symbol strings e.g. ['LTCUSDT', 'DOGEUSDT']

    Returns:
        dict {symbol: (df_1m, empty_df, empty_df)} — same schema as
        fetch_all_symbols() so _manage_positions() works unchanged.
    """
    if not symbols:
        return {}

    result = {}
    log.debug(f"Fast fetch for {len(symbols)} active symbols: {symbols}")

    def _fetch_one_fast(symbol):
        # 400, not 100. The freqtrade ports resample 1m->5m in manage() and
        # bail on `len(df_5m) < 55`, i.e. they need >=275 1m bars. At 100 they
        # returned no_exit() on EVERY fast cycle — thousands per hour, all
        # no-ops — so an open NASOS_V4 / ELLIOT_V8 position was never evaluated
        # between full cycles. 400 gives 80 resampled bars, enough for the
        # HMA(50) and RSI(20) those exits read.
        df_1m = _rate_gated_fetch(symbol, "1m", limit=400)
        mark  = fetch_mark_price(symbol)
        return symbol, df_1m, mark

    with ThreadPoolExecutor(max_workers=min(len(symbols), MAX_CONCURRENT)) as ex:
        futures = {ex.submit(_fetch_one_fast, s): s for s in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                symbol, df_1m, mark = future.result()
                if df_1m is None or df_1m.empty:
                    continue

                # Inject mark price as live tick row if available
                if mark is not None and mark > 0:
                    last = df_1m.iloc[-1]
                    tick_row = {
                        "timestamp": pd.Timestamp.now(tz="UTC"),
                        "open":      mark,
                        "high":      max(float(last["high"]), mark),
                        "low":       min(float(last["low"]),  mark),
                        "close":     mark,
                        "volume":    0.0,
                    }
                    df_1m = pd.concat(
                        [df_1m, pd.DataFrame([tick_row])],
                        ignore_index=True,
                    )
                    log.debug(
                        f"[FastFetch] {symbol} mark={mark:.6f} | "
                        f"candle high={last['high']:.6f} low={last['low']:.6f}"
                    )

                # Return same tuple schema as fetch_all_symbols
                result[symbol] = (df_1m, pd.DataFrame(), pd.DataFrame())

            except Exception as exc:
                log.warning(f"Fast fetch failed [{sym}]: {exc}")

    return result



