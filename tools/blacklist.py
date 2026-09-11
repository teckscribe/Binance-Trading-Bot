"""Is any 'bad coin' actually bad, or is it noise? And would cutting them help?"""
import os, sys, glob, logging, math
from collections import defaultdict
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
import pandas as pd, backtest_optimizer as bt
logging.disable(logging.WARNING)

syms = sorted(os.path.basename(p).split("_15m_")[0] for p in glob.glob("data/*_15m_90d.csv"))
syms = [s for s in syms if not bt.is_equity(s)]
tr = bt.collect_trades(syms, ["CSM","NASOS_V4","ELLIOT_V8"], None, days=90,
                       cache_path="data/bt_cache_90d_CSM-NASOS-SMA-ELLIOT.json")
last = max(t["entry_time"] for t in tr)

def window(days, strat="CSM"):
    return [t for t in tr if t["strategy"] == strat
            and t["entry_time"] >= last - pd.Timedelta(days=days)]

# ── 1. are the 'worst' coins statistically distinguishable from zero? ────────
sub = window(60)
by = defaultdict(list)
for t in sub: by[t["symbol"]].append(t)

rows = []
for s, ts in by.items():
    if len(ts) < 20: continue
    n = [t["net_pct"] for t in ts]
    mean = sum(n)/len(n)
    sd = (sum((x-mean)**2 for x in n)/(len(n)-1))**0.5
    se = sd/math.sqrt(len(n))
    rows.append((s, len(ts), mean*100, sd*100, (mean/se) if se else 0))
rows.sort(key=lambda r: r[2])

print("="*84)
print("1. ARE THE WORST COINS REALLY BAD?  (60d, CSM)")
print("="*84)
print("%-14s%6s%11s%10s%9s   %s" % ("SYMBOL","N","E[net]","SD","t-stat","verdict"))
print("-"*84)
for s,n,m,sd,t in rows[:10]:
    v = "REAL (p<0.05)" if abs(t) > 2 else "indistinguishable from zero"
    print("%-14s%6d%+10.3f%%%9.2f%%%+9.2f   %s" % (s,n,m,sd,t,v))
sig = [r for r in rows if r[4] < -2]
print()
print("coins with a statistically real NEGATIVE edge: %d of %d" % (len(sig), len(rows)))

# ── 2. would blacklisting have helped, out of sample? ───────────────────────
print()
print("="*84)
print("2. OUT-OF-SAMPLE TEST: blacklist using days 90-30, then trade days 30-0")
print("="*84)
train = [t for t in tr if t["strategy"]=="CSM"
         and t["entry_time"] < last - pd.Timedelta(days=30)]
test  = window(30)

tb = defaultdict(list)
for t in train: tb[t["symbol"]].append(t)
losers = {s for s,ts in tb.items() if len(ts) >= 20 and sum(x["net_pct"] for x in ts) < 0}
print("coins that LOST money in the training period (days 90-30): %d" % len(losers))
print("   %s" % ", ".join(sorted(losers)[:12]))

for label, keep in (("trade everything", None), ("blacklist the losers", losers)):
    t2 = test if keep is None else [t for t in test if t["symbol"] not in keep]
    r = bt.simulate_portfolio(t2, capital=100.0, verbose=False)
    e = bt.expectancy(t2)
    print("   %-24s n=%-5d E=%+.3f%%   portfolio %+.2f%%  (final $%.2f)"
          % (label, e["n"], e["exp"], r["return_pct"], r["final"]))

# did the losers stay losers?
teb = defaultdict(list)
for t in test: teb[t["symbol"]].append(t)
flip = [s for s in losers if s in teb and sum(x["net_pct"] for x in teb[s]) > 0]
print()
print("of the %d blacklisted coins, %d were PROFITABLE in the test period (%.0f%%)"
      % (len(losers), len(flip), len(flip)/len(losers)*100 if losers else 0))
