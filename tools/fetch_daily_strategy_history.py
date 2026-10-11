"""Fetch causal warm-up history for the daily EMA/Donchian backtest.

The normal CSB cache contains 90 days of 1m and 40 days of 15m data. The
daily strategies need more than 100 calendar days to prime EMA100, so this
small utility stores a separate 1h history set without touching the existing
backtest files.
"""
import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests


URL = "https://fapi.binance.com/fapi/v1/klines"
COLS = ["open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore"]


def fetch(symbol: str, days: int) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows = []
    while cursor < end_ms:
        response = requests.get(URL, params={
            "symbol": symbol, "interval": "1h", "startTime": cursor,
            "endTime": end_ms, "limit": 1500,
        }, timeout=20)
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor:
            raise RuntimeError(f"non-advancing Binance response for {symbol}")
        cursor = next_cursor
        if len(batch) < 1500:
            break
        time.sleep(0.2)
    if not rows:
        raise RuntimeError(f"no history returned for {symbol}")
    frame = pd.DataFrame(rows, columns=COLS)
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    return frame[["open_time", "open", "high", "low", "close", "volume"]].drop_duplicates(
        "open_time").sort_values("open_time")


def top_symbols(count: int) -> list[str]:
    response = requests.get(
        "https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=30
    )
    response.raise_for_status()
    rows = [r for r in response.json()
            if r.get("symbol", "").endswith("USDT") and
            not any(x in r["symbol"] for x in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))]
    rows.sort(key=lambda r: float(r.get("quoteVolume", 0.0)), reverse=True)
    return [r["symbol"] for r in rows[:count]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--top", type=int, default=None,
                        help="fetch the top N USDT perpetuals by 24h quote volume")
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()
    os.makedirs("data", exist_ok=True)
    if args.top:
        symbols = top_symbols(args.top)
    elif args.symbols:
        symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    else:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def one(symbol):
        path = os.path.join("data", f"{symbol}_1h_{args.days}d.csv")
        if os.path.exists(path):
            return symbol, sum(1 for _ in open(path, encoding="utf-8")) - 1, path
        frame = fetch(symbol, args.days)
        frame.to_csv(path, index=False)
        return symbol, len(frame), path

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(one, symbol): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol = jobs[job]
            try:
                name, count, path = job.result()
                print(f"{name}: {count} hourly bars -> {path}", flush=True)
            except Exception as exc:
                print(f"{symbol}: FAILED {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    main()
