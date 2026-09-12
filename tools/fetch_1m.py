"""
tools/fetch_1m.py — 1-minute history for the Tier 2 replay harness.

Fetches BOTH series the live engine actually reads:

  klines           trade prices. What data_feed.fetch_candles() returns, and
                   therefore what every strategy computes its indicators from.
  markPriceKlines  mark prices. What data_hub.fetch_active_positions_data()
                   injects as the synthetic tick row, and what SL/TP are
                   really tested against every fast cycle. These are NOT the
                   same series — the mark price is an index-based fair value,
                   and on a thin alt it can differ from the last trade by more
                   than a stop distance.

Resume-safe: a symbol whose CSV already covers the requested range is skipped,
so an interrupted run can simply be restarted.

Run from the project root ON THE UBUNTU BOX — the Windows dev box is not
IP-whitelisted with Binance. (Public klines do work unauthenticated, so this
script will run anywhere; it belongs next to the live system regardless.)

    python tools/fetch_1m.py --days 30
    python tools/fetch_1m.py --days 30 --symbols BTCUSDT,ETHUSDT
    python tools/fetch_1m.py --days 90 --workers 4
"""

import os
import sys
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "https://fapi.binance.com"
LIMIT = 1500                # max bars per request
DATA_DIR = "data"

# /fapi/v1/klines costs weight 10 at limit>=1000, against a 2400/min ceiling.
# 200 req/min leaves comfortable headroom for the live scanner sharing the IP.
MAX_REQ_PER_MIN = 200
_REQ_GAP = 60.0 / MAX_REQ_PER_MIN

COLUMNS = ["open_time", "open", "high", "low", "close", "volume",
           "close_time", "quote_asset_volume", "number_of_trades",
           "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"]
NUMERIC = ["open", "high", "low", "close", "volume"]

_gate = threading.Lock()
_last = [0.0]


def _throttled_get(path, params, retries=4):
    """One rate-gated GET with backoff on 429/5xx."""
    for attempt in range(retries):
        with _gate:
            wait = _REQ_GAP - (time.monotonic() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            r = requests.get(BASE + path, params=params, timeout=20)
            if r.status_code == 429:
                # Binance tells us how long to back off; honour it.
                sleep_s = int(r.headers.get("Retry-After", 5))
                print(f"  ! 429 rate limited — sleeping {sleep_s}s")
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (2 ** attempt))
    return []


def fetch_series(path, symbol, start_ms, end_ms):
    """Page through a kline endpoint. Returns a DataFrame indexed by open_time."""
    rows, cur = [], start_ms
    while cur < end_ms:
        batch = _throttled_get(path, {"symbol": symbol, "interval": "1m",
                                      "startTime": cur, "endTime": end_ms,
                                      "limit": LIMIT})
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        if len(batch) < LIMIT:
            break

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=COLUMNS)
    # markPriceKlines returns 0 for volume/trade fields — harmless, we only
    # ever read OHLC from that series.
    df[NUMERIC] = df[NUMERIC].apply(pd.to_numeric, axis=1)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    df = df.drop(columns=["ignore"]).set_index("open_time")
    return df[~df.index.duplicated(keep="first")].sort_index()


def _covers(path, start, end, tolerance_min=90):
    """True if an existing CSV already spans the requested range."""
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_csv(path, usecols=["open_time"], parse_dates=["open_time"])
        if df.empty:
            return False
        lo, hi = df["open_time"].iloc[0], df["open_time"].iloc[-1]
        tol = pd.Timedelta(minutes=tolerance_min)
        return lo <= start + tol and hi >= end - tol
    except Exception:
        return False


def fetch_symbol(symbol, days, start_ms, end_ms, start_ts, end_ts):
    """Fetch both series for one symbol. Returns a short status string."""
    out = []
    for tag, path in (("1m", "/fapi/v1/klines"),
                      ("mark1m", "/fapi/v1/markPriceKlines")):
        csv = os.path.join(DATA_DIR, f"{symbol}_{tag}_{days}d.csv")
        if _covers(csv, start_ts, end_ts):
            out.append(f"{tag}=cached")
            continue
        try:
            df = fetch_series(path, symbol, start_ms, end_ms)
        except Exception as exc:
            out.append(f"{tag}=FAILED({type(exc).__name__})")
            continue
        if df.empty:
            out.append(f"{tag}=EMPTY")
            continue
        df.to_csv(csv)
        out.append(f"{tag}={len(df)}")
    return f"{symbol:<16} " + "  ".join(out)


def main():
    ap = argparse.ArgumentParser(description="Fetch 1m trade + mark price history")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated; default = focused watchlist")
    from modules import settings_manager as _cfg
    ap.add_argument("--size", type=int, default=_cfg.get("FOCUSED_SIZE"),
                    help="watchlist size when --symbols is not given")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        from modules.watchlist import get_focused_watchlist
        symbols = get_focused_watchlist(args.size)
    if not symbols:
        sys.exit("No symbols resolved — check network / watchlist.")

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    start_ts, end_ts = pd.Timestamp(start).tz_localize(None), pd.Timestamp(end).tz_localize(None)

    os.makedirs(DATA_DIR, exist_ok=True)

    bars = args.days * 1440
    reqs = -(-bars // LIMIT) * len(symbols) * 2
    print(f"{len(symbols)} symbols x {args.days}d x 2 series")
    print(f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")
    print(f"~{bars} bars/symbol/series, ~{reqs} requests, "
          f"~{reqs / MAX_REQ_PER_MIN:.0f} min at {MAX_REQ_PER_MIN} req/min\n")

    t0 = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_symbol, s, args.days, start_ms, end_ms,
                          start_ts, end_ts): s for s in symbols}
        for f in as_completed(futs):
            done += 1
            try:
                line = f.result()
            except Exception as exc:
                line = f"{futs[f]:<16} FAILED: {exc}"
            el = time.monotonic() - t0
            eta = el / done * (len(symbols) - done)
            print(f"[{done:>3}/{len(symbols)}] {line}   "
                  f"({el/60:.1f}m elapsed, ~{eta/60:.0f}m left)")

    print(f"\nDone in {(time.monotonic() - t0)/60:.1f} min.")
    total = sum(os.path.getsize(os.path.join(DATA_DIR, f))
                for f in os.listdir(DATA_DIR) if "1m_" in f)
    print(f"1m data on disk: {total/1e6:.0f} MB")


if __name__ == "__main__":
    main()
