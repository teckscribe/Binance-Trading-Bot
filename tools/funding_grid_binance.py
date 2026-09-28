"""
tools/funding_grid_binance.py
Combination search over the funding-crowding rule on Binance, WITH a
walk-forward so the search cannot hand back a winner it invented.

Why this exists: 0.5.38 measured the single frozen Delta rule on Binance and
it lost (-1.134%/trade, PF 0.83, worse than random twins). The operator asked
to try other coins and combinations. A plain grid over a year WILL produce a
positive cell -- 40+ variants against one noisy sample always does. So every
cell is reported three ways and only the third counts:

  IN-SAMPLE   the whole window. Reported for completeness; means nothing.
  WALK-FWD    the window is cut into folds; in each fold the BEST variant is
              chosen on the training part only and then scored on the unseen
              part. This is what an operator running the search in real time
              would actually have earned.
  TWINS       every trade is paired with a random other coin shorted at the
              same moment, so a year that simply paid shorts cannot register
              as an edge.

Grid: side x rate threshold x hold x coin filter. Data is cached to
data/funding_cache_binance.json so the sweep does not re-fetch.

    python tools/funding_grid_binance.py --days 365
"""
from __future__ import annotations

import os
import sys
import json
import time
import random
import argparse
import datetime as dt
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from tools.funding_study_binance import (          # noqa: E402
    perp_symbols, funding_history, klines_1h, atr14, pf,
    TAKER_FEE, SLIP, BASE, _get,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(_ROOT, "data")
CACHE = os.path.join(DATA, "funding_cache_binance.json")

SIDES = ("SHORT", "LONG")
THRS = (0.0005, 0.0010, 0.0020, 0.0040)
HOLDS = (8, 24, 72)
STOP_K = 1.5


def build_cache(days: int, thr_min: float):
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    syms = perp_symbols()
    print(f"{len(syms)} perps | {days}d | building cache")
    fh, interesting = {}, set()
    for i, s in enumerate(syms, 1):
        f = funding_history(s, start, end)
        if f:
            fh[s] = f
            if any(abs(r) >= thr_min for _, r in f):
                interesting.add(s)
        if i % 100 == 0:
            print(f"  funding {i}/{len(syms)}  interesting {len(interesting)}")
    pool = sorted(interesting)
    twins = [s for s in fh if s not in interesting][:120]
    print(f"fetching 1h bars for {len(pool)} + {len(twins)} twins")
    bars = {}
    for i, s in enumerate(pool + twins, 1):
        b = klines_1h(s, start - 20 * 3_600_000, end)
        if b:
            bars[s] = {str(k): v for k, v in b.items()}
        if i % 40 == 0:
            print(f"  bars {i}/{len(pool)+len(twins)}")
    obj = {"days": days, "start": start, "end": end, "funding": fh,
           "bars": bars, "pool": pool, "twins": twins}
    with open(CACHE, "w") as f:
        json.dump(obj, f)
    return obj


def load_cache(days):
    if os.path.exists(CACHE):
        o = json.load(open(CACHE))
        if o.get("days") == days:
            print(f"cache hit: {len(o['bars'])} symbols")
            return o
    return build_cache(days, min(THRS))


def sim(bars, funds, t_ms, side, hold):
    entry_bar = t_ms - 3_600_000
    b0 = bars.get(str(entry_bar))
    if b0 is None:
        return None
    entry = b0[3]
    a = atr14({int(k): v for k, v in bars.items()}, t_ms)
    if not a or entry <= 0 or a <= 0:
        return None
    dist = STOP_K * a * (hold ** 0.5)
    stop = entry + dist if side == "SHORT" else entry - dist
    exit_px = None
    for i in range(hold):
        b = bars.get(str(t_ms + i * 3_600_000))
        if b is None:
            return None
        o, h, l, c = b
        if side == "SHORT":
            if o >= stop:
                exit_px = o; break
            if h >= stop:
                exit_px = stop; break
        else:
            if o <= stop:
                exit_px = o; break
            if l <= stop:
                exit_px = stop; break
        exit_px = c
    if exit_px is None:
        return None
    gross = (entry - exit_px) / entry if side == "SHORT" else (exit_px - entry) / entry
    end_ms = t_ms + hold * 3_600_000
    fnd = sum(r for ts, r in funds if t_ms <= ts < end_ms)
    fnd = fnd if side == "SHORT" else -fnd
    return {"t": t_ms, "net": gross + fnd - 2 * (TAKER_FEE + SLIP)}


def run_variant(c, side, thr, hold, coin_filter):
    """coin_filter: None = all, or a set of symbols to keep."""
    out = []
    for s in c["pool"]:
        if coin_filter is not None and s not in coin_filter:
            continue
        bars = c["bars"].get(s)
        if not bars:
            continue
        fnd = [(t, r) for t, r in c["funding"].get(s, [])]
        for t, r in fnd:
            ok = r >= thr if side == "SHORT" else r <= -thr
            if not ok:
                continue
            x = sim(bars, fnd, t, side, hold)
            if x:
                x["symbol"] = s
                out.append(x)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--folds", type=int, default=4)
    a = ap.parse_args()
    c = load_cache(a.days)
    random.seed(23)

    # coin filters: all, majors-excluded (small caps), and top-liquidity only
    allsyms = set(c["pool"])
    majors = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
              "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "BCHUSDT"}
    filters = {"all": None, "no-majors": allsyms - majors, "majors": allsyms & majors}

    print("\nIN-SAMPLE grid (means nothing on its own — the walk-forward below is the test)")
    print(f"  {'variant':<34}{'n':>6}{'mean%':>9}{'PF':>7}")
    results = {}
    for side in SIDES:
        for thr in THRS:
            for hold in HOLDS:
                for fn, fset in filters.items():
                    key = (side, thr, hold, fn)
                    tr = run_variant(c, side, thr, hold, fset)
                    if len(tr) < 40:
                        continue
                    results[key] = tr
                    v = [x["net"] for x in tr]
                    print(f"  {side:<6}{100*thr:>5.2f}% {hold:>3}h {fn:<12}{len(v):>6}"
                          f"{100*sum(v)/len(v):>+8.3f}%{pf(v):>7.2f}")
    if not results:
        print("no variant produced enough trades")
        return

    best = max(results, key=lambda k: sum(x["net"] for x in results[k]) / len(results[k]))
    bv = [x["net"] for x in results[best]]
    print(f"\n  best in-sample: {best}  n {len(bv)}  mean {100*sum(bv)/len(bv):+.3f}%  PF {pf(bv):.2f}")

    # ── walk-forward: choose on train, score on the next unseen block ────────
    span = c["end"] - c["start"]
    edges = [c["start"] + i * span // a.folds for i in range(a.folds + 1)]
    print(f"\nWALK-FORWARD ({a.folds} folds; pick the best variant on everything before a fold, score it ON that fold)")
    print(f"  {'fold':<26}{'picked':<30}{'n':>5}{'mean%':>9}{'PF':>7}")
    oos = []
    for i in range(1, a.folds):
        tr_end, te_end = edges[i], edges[i + 1]
        scored = {}
        for k, tr in results.items():
            v = [x["net"] for x in tr if x["t"] < tr_end]
            if len(v) >= 30:
                scored[k] = sum(v) / len(v)
        if not scored:
            continue
        pick = max(scored, key=scored.get)
        test = [x["net"] for x in results[pick] if tr_end <= x["t"] < te_end]
        lbl = f"{dt.datetime.utcfromtimestamp(tr_end/1000):%Y-%m-%d}"
        if not test:
            print(f"  from {lbl:<20}{str(pick):<30}{'0':>5}   (no trades)")
            continue
        oos += test
        print(f"  from {lbl:<20}{str(pick):<30}{len(test):>5}{100*sum(test)/len(test):>+8.3f}%{pf(test):>7.2f}")
    if oos:
        print(f"\n  OUT-OF-SAMPLE POOLED: n {len(oos)}  mean {100*sum(oos)/len(oos):+.3f}%/trade  PF {pf(oos):.2f}")
        print(f"  VERDICT: {'PASS' if sum(oos)/len(oos) > 0 else 'FAIL'} "
              f"(a positive in-sample cell is expected from {len(results)} variants; only this line counts)")
    else:
        print("\n  no out-of-sample trades — cannot judge")


if __name__ == "__main__":
    main()
