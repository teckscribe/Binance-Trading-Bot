"""Coin-wise performance.  usage: coinwise.py <data_days> <window_days> [strategy]"""
import os, sys, glob, logging
from collections import defaultdict

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)
import pandas as pd, backtest_optimizer as bt
logging.disable(logging.WARNING)

DATA_DAYS = int(sys.argv[1]); WINDOW = int(sys.argv[2])
ONLY = sys.argv[3] if len(sys.argv) > 3 else None

syms = sorted(os.path.basename(p).split("_15m_")[0]
              for p in glob.glob("data/*_15m_%dd.csv" % DATA_DAYS))
syms = [s for s in syms if not bt.is_equity(s)]
tr = bt.collect_trades(syms, ["CSM", "NASOS_V4", "ELLIOT_V8"], None, days=DATA_DAYS,
                       cache_path="data/bt_cache_%dd_CSM-NASOS-SMA-ELLIOT.json" % DATA_DAYS)
last = max(t["entry_time"] for t in tr)
sub = [t for t in tr if t["entry_time"] >= last - pd.Timedelta(days=WINDOW)]
if ONLY:
    sub = [t for t in sub if t["strategy"] == ONLY]

by = defaultdict(list)
for t in sub:
    by[t["symbol"]].append(t)

rows = []
for sym, ts in by.items():
    e = bt.expectancy(ts)
    if e:
        rows.append((sym, e))

MIN_N = 20
scored = [(s, e) for s, e in rows if e["n"] >= MIN_N]
scored.sort(key=lambda r: r[1]["sum"])

hdr = "%-14s%6s%8s%10s%8s%11s" % ("SYMBOL", "N", "WIN%", "E[net]", "PF", "SUM")
print("=" * 70)
print("COIN-WISE  %d-day window%s  (only coins with >= %d trades)"
      % (WINDOW, "  strategy=%s" % ONLY if ONLY else "  all strategies", MIN_N))
print("=" * 70)
print("total coins traded: %d   scored: %d   trades: %d" % (len(rows), len(scored), len(sub)))
print()
print("WORST 15 - blacklist candidates")
print(hdr); print("-" * 70)
for s, e in scored[:15]:
    pf = "inf" if e["pf"] == float("inf") else "%.2f" % e["pf"]
    print("%-14s%6d%7.1f%%%+9.3f%%%8s%+10.1f%%" % (s, e["n"], e["win"], e["exp"], pf, e["sum"]))
print()
print("BEST 15")
print(hdr); print("-" * 70)
for s, e in scored[-15:][::-1]:
    pf = "inf" if e["pf"] == float("inf") else "%.2f" % e["pf"]
    print("%-14s%6d%7.1f%%%+9.3f%%%8s%+10.1f%%" % (s, e["n"], e["win"], e["exp"], pf, e["sum"]))

neg = [r for r in scored if r[1]["sum"] < 0]
pos = [r for r in scored if r[1]["sum"] >= 0]
print()
print("SPREAD: %d coins net-positive, %d net-negative" % (len(pos), len(neg)))
if scored:
    tot = sum(e["sum"] for _, e in scored)
    top5 = sum(e["sum"] for _, e in scored[-5:])
    bot5 = sum(e["sum"] for _, e in scored[:5])
    print("  total summed return %+.1f%%" % tot)
    print("  best 5 coins contribute %+.1f%%  (%.0f%% of total)" % (top5, top5 / tot * 100 if tot else 0))
    print("  worst 5 coins cost     %+.1f%%" % bot5)
    print("  excluding the worst 5 would give %+.1f%% (%+.1f%% better)" % (tot - bot5, -bot5))
