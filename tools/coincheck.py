"""
Does carrying BTC+ETH+SOL for regime detection actually earn anything?

Rebuilds the regime series three ways over the same data, re-buckets every
cached trade by entry time under each, applies REGIME_STRATEGY_PERMISSIONS,
and runs the real portfolio simulation. The output is account return per
variant - an outcome comparison, not a labelling comparison.

usage: coincheck.py <data_days> <window_days>
"""
import os, sys, glob, bisect, logging
from collections import Counter

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

import pandas as pd
import backtest_optimizer as bt
from modules.regime_engine import (_coin_trend, _compute_adx,
    HYSTERESIS_PCT, SMA_PERIOD, MIN_BARS_NEEDED, ADX_REGIME_MIN,
    REGIME_STRATEGY_PERMISSIONS as PERM)

logging.disable(logging.WARNING)

DATA_DAYS = int(sys.argv[1])
WINDOW    = int(sys.argv[2])
STRATS    = ["CSM", "NASOS_V4"]
CAPITAL   = 100.0

cache = os.path.join("data", "bt_cache_%dd_%s.json" % (DATA_DAYS, "-".join(sorted(STRATS))))
if not os.path.exists(cache):
    print("no cache at %s" % cache); sys.exit(1)

symbols = sorted(os.path.basename(p).split("_15m_")[0]
                 for p in glob.glob(os.path.join("data", "*_15m_%dd.csv" % DATA_DAYS)))
symbols = [s for s in symbols if not bt.is_equity(s)]
trades = bt.collect_trades(symbols, STRATS, None, days=DATA_DAYS, cache_path=cache)

last = max(t["entry_time"] for t in trades)
cut  = last - pd.Timedelta(days=WINDOW)
trades = [t for t in trades if t["entry_time"] >= cut]
print("window %s -> %s   %d trades\n" % (cut, last, len(trades)))

btc = bt.load_data("BTCUSDT", "1h", DATA_DAYS)
eth = bt.load_data("ETHUSDT", "1h", DATA_DAYS)
sol = bt.load_data("SOLUSDT", "1h", DATA_DAYS)


def decide(variant, b_, e_, s_, adx):
    if adx > 0 and adx < ADX_REGIME_MIN:  return "RANGING"
    if b_ == "NEUTRAL":                    return "RANGING"
    if variant == "BTC":
        return "BEAR_TREND" if b_ == "BEAR" else "BULL_TREND"
    if b_ == "BEAR" and e_ == "BEAR":
        if variant == "BTC+ETH+SOL" and s_ == "BULL": return "RANGING"
        return "BEAR_TREND"
    if b_ == "BULL" and e_ == "BULL":       return "BULL_TREND"
    return "RANGING"


VARIANTS = ["BTC", "BTC+ETH", "BTC+ETH+SOL"]
series = {v: {} for v in VARIANTS}
cur = {v: "" for v in VARIANTS}

for i in range(MIN_BARS_NEEDED, len(btc)):
    ts = btc.index[i]; b = btc.iloc[:i+1]
    e  = eth[eth.index <= ts]; s = sol[sol.index <= ts]
    if len(e) < MIN_BARS_NEEDED or len(s) < MIN_BARS_NEEDED: continue
    b_, e_, s_ = _coin_trend(b), _coin_trend(e), _coin_trend(s)
    adx   = _compute_adx(b)
    price = float(b["close"].iloc[-1])
    sma   = float(b["close"].rolling(SMA_PERIOD).mean().iloc[-1])
    for v in VARIANTS:
        raw = decide(v, b_, e_, s_, adx)
        if cur[v] and cur[v] != raw:
            d, ok = ((price - sma) / sma if sma > 0 else 0), True
            if   raw == "BULL_TREND" and d <  HYSTERESIS_PCT: ok = False
            elif raw == "BEAR_TREND" and d > -HYSTERESIS_PCT: ok = False
            elif raw == "RANGING" and abs(d) > HYSTERESIS_PCT: ok = False
            if not ok: raw = cur[v]
        cur[v] = raw
        series[v][ts] = raw

keys = {v: sorted(series[v]) for v in VARIANTS}

def regime_at(v, when):
    ks = keys[v]
    i = bisect.bisect_right(ks, when) - 1
    return series[v][ks[i]] if i >= 0 else "RANGING"

print("=" * 96)
print("REGIME LABEL MIX")
print("=" * 96)
for v in VARIANTS:
    c = Counter(series[v].values()); n = len(series[v])
    print("  %-14s RANGING %5.1f%%  BEAR %5.1f%%  BULL %5.1f%%"
          % (v, c["RANGING"]/n*100, c["BEAR_TREND"]/n*100, c["BULL_TREND"]/n*100))

print()
print("=" * 96)
print("PER-STRATEGY EXPECTANCY IN THE CELL THAT GATES IT")
print("=" * 96)
print("  %-14s %-6s %8s %10s %9s" % ("VARIANT", "STRAT", "N", "E[net]", "PF"))
for v in VARIANTS:
    for sid in ("LIQ", "VRP"):
        ts_ = [t for t in trades
               if t["strategy"] == sid and regime_at(v, t["entry_time"]) == "BEAR_TREND"]
        if not ts_:
            print("  %-14s %-6s %8s %10s %9s" % (v, sid, 0, "-", "-")); continue
        e = bt.expectancy(ts_)
        pf = "inf" if e["pf"] == float("inf") else "%.2f" % e["pf"]
        print("  %-14s %-6s %8d %+9.3f%% %9s" % (v, sid, e["n"], e["exp"], pf))

print()
print("=" * 96)
print("ACCOUNT OUTCOME - permission matrix applied, real portfolio sim")
print("=" * 96)
print("  %-14s %8s %8s %10s %9s %8s" % ("VARIANT", "SIGNALS", "TAKEN", "RETURN", "maxDD", "FINAL"))
res = {}
for v in VARIANTS:
    allowed = []
    for t in trades:
        rg = regime_at(v, t["entry_time"])
        if PERM.get(rg, {}).get(t["strategy"], False):
            t2 = dict(t); t2["regime_at_entry"] = rg
            allowed.append(t2)
    r = bt.simulate_portfolio(allowed, capital=CAPITAL, label=v, verbose=False)
    res[v] = r
    if r:
        print("  %-14s %8d %8d %+9.2f%% %8.1f%% %8.2f"
              % (v, r["signals"], r["taken"], r["return_pct"], r["max_dd"], r["final"]))

print()
best = max(res, key=lambda v: res[v]["return_pct"] if res[v] else -1e9)
spread = (max(r["return_pct"] for r in res.values() if r)
          - min(r["return_pct"] for r in res.values() if r))
print("  best: %s     spread across variants: %.2f percentage points" % (best, spread))
print()
print("  NOTE: CSM is permitted in BULL/BEAR/RANGING, so its trades are identical")
print("  under all three variants. Any difference above comes from LIQ and VRP only.")
