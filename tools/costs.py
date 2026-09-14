"""What do slippage and funding actually cost? Measured on the real trade set."""
import os, sys, glob, logging
import statistics as st
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
import pandas as pd, backtest_optimizer as bt
logging.disable(logging.WARNING)

syms = sorted(os.path.basename(p).split("_15m_")[0] for p in glob.glob("data/*_15m_90d.csv"))
syms = [s for s in syms if not bt.is_equity(s)]
tr = bt.collect_trades(syms, ["CSM","NASOS_V4"], None, days=90,
                       cache_path="data/bt_cache_90d_CSM-NASOS.json")
tr = [t for t in tr if t["strategy"] == "CSM"]

# ── how long are trades held? does funding even apply? ──────────────────────
hrs = [t["hours"] for t in tr]
def crosses_funding(t):
    """Binance funds at 00:00 / 08:00 / 16:00 UTC."""
    a, b = t["entry_time"], t["exit_time"]
    n = 0
    cur = a.normalize()
    while cur <= b + pd.Timedelta(days=1):
        for h in (0, 8, 16):
            stamp = cur + pd.Timedelta(hours=h)
            if a < stamp <= b:
                n += 1
        cur += pd.Timedelta(days=1)
    return n

cross = [crosses_funding(t) for t in tr]
print("=" * 72)
print("HOW LONG ARE TRADES HELD?")
print("=" * 72)
print("  median %.1f h | mean %.1f h | 90th pct %.1f h | max %.1f h"
      % (st.median(hrs), st.mean(hrs), sorted(hrs)[int(len(hrs)*0.9)], max(hrs)))
print("  trades crossing >=1 funding stamp: %d of %d (%.1f%%)"
      % (sum(1 for c in cross if c), len(tr), sum(1 for c in cross if c)/len(tr)*100))
print("  average funding stamps crossed per trade: %.2f" % st.mean(cross))

# ── cost model ─────────────────────────────────────────────────────────────
FUND_RATE = 0.0001          # 0.01% per 8h, typical BTC-ish
base_e = sum(t["net_pct"] for t in tr)/len(tr)*100

print()
print("=" * 72)
print("EFFECT ON EXPECTANCY PER TRADE  (currently %+.3f%%)" % base_e)
print("=" * 72)
print("  %-34s %11s %11s" % ("cost added", "E[net]", "vs now"))
print("  " + "-"*58)
rows = []
for slip in (0.0, 0.0002, 0.0005, 0.0010):
    for fund_on in (False, True):
        adj = []
        for t, c in zip(tr, cross):
            v = t["net_pct"] - slip*2                      # entry + exit
            if fund_on:
                v -= c * FUND_RATE                         # long pays when funding +ve
            adj.append(v)
        e = sum(adj)/len(adj)*100
        label = "slip %.2f%%/side" % (slip*100) + (" + funding" if fund_on else "")
        rows.append((slip, fund_on, e, adj))
        print("  %-34s %+10.3f%% %+10.3f%%" % (label, e, e - base_e))

# ── effect on the actual account curve ─────────────────────────────────────
print()
print("=" * 72)
print("EFFECT ON 90-DAY ACCOUNT RETURN  (portfolio sim, $100 start)")
print("=" * 72)
print("  %-34s %12s %10s" % ("scenario", "return", "final $"))
print("  " + "-"*58)
for slip, fund_on, e, adj in rows:
    sub = []
    for t, v in zip(tr, adj):
        t2 = dict(t); t2["pnl_pct"] = t["pnl_pct"] - slip*2 - (
            crosses_funding(t)*FUND_RATE if fund_on else 0)
        sub.append(t2)
    r = bt.simulate_portfolio(sub, capital=100.0, verbose=False)
    label = "slip %.2f%%/side" % (slip*100) + (" + funding" if fund_on else "")
    print("  %-34s %+11.2f%% %10.2f" % (label, r["return_pct"], r["final"]))
