"""
tools/replay/store.py — historical data served the way Binance served it.

The whole point of Tier 2 is that the replay must hand the strategies the SAME
frames the live scanner received at that instant. Binance's klines endpoint
always includes the currently-forming candle as the last row, so a query at
10:07 for 15m bars returns ... 09:45(closed), 10:00(FORMING, covering 10:00-10:06).

The existing backtest never modelled that: it only ever passed completed bars,
which is what let the 1h look-ahead hide for so long. Here, 15m and 1h are
DERIVED from 1m so the forming bar is real rather than assumed away.

Two series per symbol, because the live engine reads both and they differ:
    trade klines      -> what every indicator is computed from
    markPriceKlines   -> what SL/TP are actually tested against
Measured on ADAUSDT the two closes differ by up to 0.21%, and the mark low
dips below the trade low by up to 0.054% — i.e. real stops fire on candles
that the trade series says never touched them.

Design note on cost: naively resampling 1m->15m inside the loop is O(bars) per
query, ~4.3M queries for a 30-day/100-symbol run. Instead every timeframe is
precomputed once as complete bars plus cumulative open/high/low/close/volume
arrays, so an as-of query is a searchsorted plus a constant-time partial-bar
assembly.
"""

import os
import numpy as np
import pandas as pd

DATA_DIR = "data"
_OHLCV = ["open", "high", "low", "close", "volume"]
_TF_MIN = {"1m": 1, "15m": 15, "1h": 60}


def _read(symbol: str, tag: str, days: int, data_dir: str = None):
    path = os.path.join(data_dir or DATA_DIR, f"{symbol}_{tag}_{days}d.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["open_time"],
                     usecols=["open_time"] + _OHLCV).set_index("open_time")
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df if len(df) else None


class _Timeframe:
    """
    One symbol, one timeframe, precomputed for O(1) as-of queries.

    `starts`  bar open times (int64 ns)
    `bars`    complete OHLCV for each bar
    `cum_*`   running high/low/volume WITHIN each bar, indexed by 1m offset,
              so a partial bar can be assembled without touching the 1m rows
    """

    def __init__(self, m1: pd.DataFrame, minutes: int, m1_ts: np.ndarray):
        self.minutes = minutes
        # Shared across all three timeframes of a symbol — it is the same
        # array. Storing it per timeframe cost ~0.7 MB/symbol for nothing.
        self.m1_ts = m1_ts

        if minutes == 1:
            # Every 1m row IS its own bar, so the running-state arrays would be
            # verbatim copies of the columns and bar_of_m1 a plain arange.
            # Special-casing saves ~2 MB per symbol per 30 days.
            self.starts = m1_ts
            self.col = {c: m1[c].to_numpy(np.float64) for c in _OHLCV}
            self.index = pd.to_datetime(self.starts, utc=True)
            self.run = None
            self.bar_of_m1 = None
            return

        floor = m1.index.floor(f"{minutes}min")
        g = m1.groupby(floor)

        agg = g.agg(open=("open", "first"), high=("high", "max"),
                    low=("low", "min"), close=("close", "last"),
                    volume=("volume", "sum"))
        self.starts = agg.index.values.astype("datetime64[ns]").astype(np.int64)
        # Precomputed tz-aware index. Building this per query via
        # pd.to_datetime cost 216 us — 36% of the whole as-of path — and the
        # slice we need is contiguous (the partial bar's start IS starts[b]),
        # so there is nothing to concatenate either.
        self.index = pd.to_datetime(self.starts, utc=True)
        # Column-wise, so a query never materialises a 2-D array and then
        # splits it again; pd.DataFrame(2D) + insert cost 113 + 341 us.
        # Note there is deliberately no 2-D `bars` copy kept alongside this.
        arr = agg[_OHLCV].to_numpy(dtype=np.float64)
        self.col = {c: np.ascontiguousarray(arr[:, k])
                    for k, c in enumerate(_OHLCV)}

        # Running state within each bar, aligned to the 1m rows.
        self.run = (g["open"].transform("first").to_numpy(np.float64),
                    g["high"].cummax().to_numpy(np.float64),
                    g["low"].cummin().to_numpy(np.float64),
                    m1["close"].to_numpy(np.float64),
                    g["volume"].cumsum().to_numpy(np.float64))
        # Which bar each 1m row belongs to
        self.bar_of_m1 = np.searchsorted(
            self.starts,
            floor.values.astype("datetime64[ns]").astype(np.int64), "right") - 1

    def as_of(self, now_ns: int, limit: int) -> pd.DataFrame:
        """
        Frame as the exchange would have returned it at `now_ns`.

        `now_ns` is the instant of the query. The 1m row covering that instant
        is included as a PARTIAL final bar, exactly as Binance does.
        """
        i = np.searchsorted(self.m1_ts, now_ns, "right") - 1
        if i < 0:
            return pd.DataFrame(columns=["timestamp"] + _OHLCV)

        if self.run is None:                     # 1m: no partial bar exists
            lo = max(0, i - limit + 1)
            return pd.DataFrame(
                {"timestamp": self.index[lo:i + 1],
                 **{c: self.col[c][lo:i + 1] for c in _OHLCV}}, copy=False)

        b = int(self.bar_of_m1[i])               # bar containing this minute
        lo = max(0, b - limit + 1)
        n = b - lo + 1                           # closed bars + the forming one

        # Build column-wise. Each column is one allocation; the closed bars are
        # a contiguous copy and only the final element is overwritten with the
        # partial value.
        data = {"timestamp": self.index[lo:b + 1]}
        for k, c in enumerate(_OHLCV):
            col = np.empty(n, dtype=np.float64)
            col[:n - 1] = self.col[c][lo:b]
            col[n - 1] = self.run[k][i]
            data[c] = col

        # data_feed._to_dataframe() produces a 'timestamp' COLUMN on a
        # RangeIndex. Strategies and trail_reference_price() both depend on
        # that exact shape, so reproduce it rather than a DatetimeIndex.
        return pd.DataFrame(data, copy=False)


class ReplayStore:
    """Serves every symbol/timeframe the live scanner asks for, as of sim-now."""

    def __init__(self, symbols, days: int, verbose: bool = True,
                 data_dir: str = None):
        self.days = days
        # Lets a second tree (e.g. a control arm running pre-fix code) share one
        # fetched dataset instead of duplicating ~600 MB of 1m CSVs.
        self.data_dir = data_dir or DATA_DIR
        self.tf: dict[str, dict[str, _Timeframe]] = {}
        self.mark_ts: dict[str, np.ndarray] = {}
        self.mark_ohlc: dict[str, np.ndarray] = {}
        self.symbols: list[str] = []
        skipped = []

        for s in symbols:
            m1 = _read(s, "1m", days, self.data_dir)
            mk = _read(s, "mark1m", days, self.data_dir)
            if m1 is None or mk is None:
                skipped.append(s)
                continue
            m1_ts = m1.index.values.astype("datetime64[ns]").astype(np.int64)
            self.tf[s] = {tf: _Timeframe(m1, n, m1_ts) for tf, n in _TF_MIN.items()}
            self.mark_ts[s] = mk.index.values.astype("datetime64[ns]").astype(np.int64)
            self.mark_ohlc[s] = mk[["high", "low", "close"]].to_numpy(np.float64)
            self.symbols.append(s)

        if verbose:
            print(f"ReplayStore: {len(self.symbols)} symbols loaded, "
                  f"{len(skipped)} skipped for missing 1m/mark data")
            if skipped:
                print(f"  missing: {', '.join(skipped[:10])}"
                      + (" ..." if len(skipped) > 10 else ""))

        if not self.symbols:
            raise SystemExit(
                "No symbols have 1m + mark data. Run:  python tools/fetch_1m.py "
                f"--days {days}")

        any_tf = self.tf[self.symbols[0]]["1m"]
        self.first_ns = int(any_tf.m1_ts[0])
        self.last_ns = int(any_tf.m1_ts[-1])

    # ── live-facing API ──────────────────────────────────────────────────────

    def candles(self, symbol: str, timeframe: str, limit: int, now_ns: int):
        t = self.tf.get(symbol, {}).get(timeframe)
        if t is None:
            return pd.DataFrame(columns=["timestamp"] + _OHLCV)
        return t.as_of(now_ns, limit)

    def mark(self, symbol: str, now_ns: int):
        """Mark price at sim-now — the series live tests SL/TP against."""
        ts = self.mark_ts.get(symbol)
        if ts is None:
            return None
        i = np.searchsorted(ts, now_ns, "right") - 1
        return float(self.mark_ohlc[symbol][i, 2]) if i >= 0 else None

    def mark_range(self, symbol: str, now_ns: int):
        """(high, low) of the mark bar covering sim-now — intra-minute extremes."""
        ts = self.mark_ts.get(symbol)
        if ts is None:
            return None
        i = np.searchsorted(ts, now_ns, "right") - 1
        if i < 0:
            return None
        h, l, _ = self.mark_ohlc[symbol][i]
        return float(h), float(l)


if __name__ == "__main__":
    import sys, time
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else ["BTCUSDT", "ADAUSDT"]

    t0 = time.monotonic()
    st = ReplayStore(syms, days)
    print(f"load: {time.monotonic() - t0:.2f}s")

    mid = (st.first_ns + st.last_ns) // 2
    now = pd.Timestamp(mid, tz="UTC")
    print(f"\nsim-now = {now}")
    for tf in ("1m", "15m", "1h"):
        df = st.candles(syms[0], tf, 5, mid)
        last = df.iloc[-1]
        age = (now - last["timestamp"]).total_seconds() / 60
        print(f"  {tf:<4} last bar opens {last['timestamp']}  "
              f"({age:.0f} min old — FORMING)  close={last['close']:.4f}")
    print(f"  mark price = {st.mark(syms[0], mid)}")

    t0 = time.monotonic()
    n = 20000
    for k in range(n):
        st.candles(syms[0], "15m", 200, mid + k * 60_000_000_000)
    dt = (time.monotonic() - t0) / n * 1e6
    print(f"\nas_of query: {dt:.0f} us  "
          f"-> 30d x 100 symbols x 3 tf = {dt * 43200 * 100 * 3 / 1e6 / 60:.0f} min "
          f"of pure data serving")
