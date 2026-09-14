"""
Is IST 23:00-07:00 really the profitable window?

Binance klines are UTC; IST = UTC+5:30. Trades are bucketed by ENTRY time,
since that is the moment a time filter would act on.
"""
import os, sys, glob, logging, math
from collections import defaultdict
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
import pandas as pd, backtest_optimizer as bt
logging.disable(logging.WARNING)

STRAT = sys.argv[1] if len(sys.argv) > 1 else "CSM"

syms = sorted(os.path.basename(p).split("_15m_")[0] for p in glob.glob("data/*_15m_90d.csv"))
syms = [s for s in syms if not bt.is_equity(s)]
tr = bt.collect_trades(syms, ["CSM","NASOS_V4"], None, days=90,
                       cache_path="data/bt_cache_90d_CSM-NASOS.json")
tr = [t for t in tr if t["strategy"] == STRAT] if STRAT != "ALL" else tr

IST = pd.Timedelta(hours=5, minutes=30)
for t in tr:
    t["ist"] = t["entry_time"] + IST

print("strategy: %s   trades: %d" % (STRAT, len(tr)))
print("UTC range: %s -> %s" % (min(t["entry_time"] for t in tr), max(t["entry_time"] for t in tr)))

# ── 1. per-IST-hour ─────────────────────────────────────────────────────────
by = defaultdict(list)
for t in tr:
    by[t["ist"].hour].append(t)

print()
print("=" * 76)
print("1. EXPECTANCY BY IST HOUR OF ENTRY")
print("=" * 76)
print("%-6s%7s%8s%11s%8s%11s  %s" % ("IST", "N", "WIN%", "E[net]", "PF", "SUM", ""))
print("-" * 76)
for h in range(24):
    ts = by.get(h, [])
    if not ts:
        continue
    e = bt.expectancy(ts)
    pf = "inf" if e["pf"] == float("inf") else "%.2f" % e["pf"]
    star = " <<< in your window" if (h >= 23 or h < 7) else ""
    print("%02d:00%7d%7.1f%%%+10.3f%%%8s%+10.1f%%  %s" % (h, e["n"], e["win"], e["exp"], pf, e["sum"], star))

# ── 2. the specific window ──────────────────────────────────────────────────
def inwin(t):
    h = t["ist"].hour
    return h >= 23 or h < 7

A = [t for t in tr if inwin(t)]
B = [t for t in tr if not inwin(t)]

def stat(ts):
    n = [t["net_pct"] for t in ts]
    m = sum(n)/len(n)
    sd = (sum((x-m)**2 for x in n)/(len(n)-1))**0.5
    return m, sd, sd/math.sqrt(len(n))

print()
print("=" * 76)
print("2. IST 23:00-07:00  vs  REST OF DAY")
print("=" * 76)
for label, ts in (("IN  window (23:00-07:00)", A), ("OUT of window (07:00-23:00)", B)):
    e = bt.expectancy(ts)
    pf = "inf" if e["pf"] == float("inf") else "%.2f" % e["pf"]
    print("  %-30s n=%-6d win=%5.1f%%  E=%+.3f%%  PF=%-6s sum=%+.1f%%"
          % (label, e["n"], e["win"], e["exp"], pf, e["sum"]))

ma, sa, ea = stat(A); mb, sb, eb = stat(B)
diff = ma - mb
se = math.sqrt(ea**2 + eb**2)
t_stat = diff/se if se else 0
print()
print("  difference: %+.4f%% per trade   t = %+.2f   %s"
      % (diff*100, t_stat,
         "SIGNIFICANT (p<0.05)" if abs(t_stat) > 2 else "NOT significant - could be chance"))
print("  window holds %.1f%% of all trades" % (len(A)/len(tr)*100))

# ── 3. does it survive out of sample? ───────────────────────────────────────
last = max(t["entry_time"] for t in tr)
train = [t for t in tr if t["entry_time"] <  last - pd.Timedelta(days=30)]
test  = [t for t in tr if t["entry_time"] >= last - pd.Timedelta(days=30)]

tb = defaultdict(list)
for t in train:
    tb[t["ist"].hour].append(t)
best = sorted((bt.expectancy(v)["exp"], h) for h, v in tb.items() if len(v) >= 30)
best_hours = {h for _, h in best[-8:]}

print()
print("=" * 76)
print("3. OUT-OF-SAMPLE: pick best 8 IST hours on days 90-30, trade days 30-0")
print("=" * 76)
print("  best 8 hours learned from training: %s" % sorted(best_hours))
print("  your proposed window (23-07)      : %s" % sorted({23,0,1,2,3,4,5,6}))
print("  overlap: %d of 8 hours" % len(best_hours & {23,0,1,2,3,4,5,6}))
print()
for label, ts in (("trade all hours", test),
                  ("trade only the learned best 8", [t for t in test if t["ist"].hour in best_hours]),
                  ("trade only 23:00-07:00 IST",    [t for t in test if inwin(t)])):
    if not ts:
        continue
    e = bt.expectancy(ts)
    r = bt.simulate_portfolio(ts, capital=100.0, verbose=False)
    print("  %-32s n=%-5d E=%+.3f%%   portfolio %+8.2f%%  maxDD %4.1f%%"
          % (label, e["n"], e["exp"], r["return_pct"], r["max_dd"]))
