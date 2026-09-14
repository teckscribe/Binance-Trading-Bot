"""
Single-window backtest report.   usage: btwin.py <data_days> <window_days>

Trades are cached to disk per data-window, so the expensive signal generation
happens ONCE per data set and every subsequent window loads instantly:

    btwin.py 10  3   <- generates the 10d cache, reports 3 days
    btwin.py 10  7   <- reuses it, instant
    btwin.py 90 15   <- generates the 90d cache, reports 15 days
    btwin.py 90 30   <- reuses it, instant
    btwin.py 90 60   <- reuses it
    btwin.py 90 90   <- reuses it

Reports two layers:
  PER-TRADE   expectancy on notional
  PORTFOLIO   account return with the live risk model enforced (real
              compute_position_size sizing, slots, margin ceiling, loss caps,
              compounding equity)
"""
import os, sys, glob, logging
from collections import defaultdict, Counter

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

import pandas as pd
import backtest_optimizer as bt

logging.getLogger("RiskEngine").setLevel(logging.ERROR)
for _n in list(logging.root.manager.loggerDict):
    if "risk" in _n.lower() or "regime" in _n.lower():
        logging.getLogger(_n).setLevel(logging.ERROR)

DATA_DAYS = int(sys.argv[1])
WINDOW    = int(sys.argv[2])
STRATS    = (sys.argv[3].split(",") if len(sys.argv) > 3 else ["CSM", "NASOS_V4"])
CAPITAL   = float(os.environ.get("BT_CAPITAL", "100"))

symbols = sorted(os.path.basename(p).split("_15m_")[0]
                 for p in glob.glob(os.path.join("data", "*_15m_%dd.csv" % DATA_DAYS)))
symbols = [s for s in symbols if not bt.is_equity(s)]

cache = os.path.join("data", "bt_cache_%dd_%s.json" % (DATA_DAYS, "-".join(sorted(STRATS))))

print("=" * 100)
print("%d-DAY WINDOW   (data: %dd, %d symbols, strategies: %s, capital $%.0f)"
      % (WINDOW, DATA_DAYS, len(symbols), ",".join(STRATS), CAPITAL))
print("=" * 100)
sys.stdout.flush()

if os.path.exists(cache):
    trades = bt.collect_trades(symbols, STRATS, None, days=DATA_DAYS, cache_path=cache)
else:
    print("generating trades (first run for this data window) ...")
    sys.stdout.flush()
    rs = bt.build_regime_series(days=DATA_DAYS, verbose=True)
    sys.stdout.flush()
    trades = bt.collect_trades(symbols, STRATS, rs, days=DATA_DAYS, cache_path=cache)
print("total trades in data set: %d" % len(trades))
if not trades:
    sys.exit(0)

last = max(t["entry_time"] for t in trades)
cut  = last - pd.Timedelta(days=WINDOW)
sub  = [t for t in trades if t["entry_time"] >= cut]
print("scoring %s -> %s   (%d trades entered)" % (cut, last, len(sub)))
print("-" * 100)
if not sub:
    sys.exit(0)

e  = bt.expectancy(sub)
pf = "inf" if e["pf"] == float("inf") else "%.2f" % e["pf"]
print("PER-TRADE  (on notional, net of %.2f%% fees)" % (bt.ROUND_TRIP_FEE * 100))
print("  n=%d  win=%.1f%%  E[net]=%+.3f%%  PF=%s  sum=%+.1f%%  median=%+.3f%%"
      % (e["n"], e["win"], e["exp"], pf, e["sum"], e["median"]))
print("  avg win %+.2f%% | avg loss %+.2f%% | worst %.2f%% | best %.2f%% | worst streak %d"
      % (e["avg_w"], e["avg_l"], e["worst"], e["best"], e["streak"]))
ex = Counter(t.get("exit_reason", "?") for t in sub)
ib = sum(1 for t in sub if t.get("intrabar"))
print("  exits: %s" % ", ".join("%s %d (%.0f%%)" % (k, v, v / len(sub) * 100)
                                for k, v in ex.most_common()))
print("  intrabar-resolved: %d (%.1f%%)" % (ib, ib / len(sub) * 100))

r = bt.simulate_portfolio(sub, capital=CAPITAL, label="%dd" % WINDOW, verbose=False)
if r:
    print()
    print("PORTFOLIO  (live risk model: real sizing, %d slots, margin ceiling, loss caps)"
          % bt.MAX_CONCURRENT)
    print("  signals=%d  taken=%d  skipped: slots=%d caps=%d size=%d margin=%d"
          % (r["signals"], r["taken"], r["slot_skip"], r["cap_skip"],
             r.get("size_skip", 0), r.get("margin_skip", 0)))
    print("  START $%.2f  ->  FINAL $%.2f    RETURN %+.2f%%    maxDD %.1f%%    Sharpe %.2f%s"
          % (CAPITAL, r["final"], r["return_pct"], r["max_dd"], r["sharpe"],
             "   ** BLOWN **" if r["blown"] else ""))
    if r["taken"]:
        print("  per taken trade: %+.4f%% of equity   |   %.1f trades/day"
              % (r["return_pct"] / r["taken"], r["taken"] / WINDOW))
    if WINDOW < 30:
        print("  (Sharpe is unreliable below ~30 days - too few daily points)")

by = defaultdict(list)
for t in sub:
    by[t["strategy"]].append(t)
if len(by) > 1:
    print()
    print("BY STRATEGY")
    for sid in sorted(by, key=lambda k: -bt.expectancy(by[k])["exp"]):
        se = bt.expectancy(by[sid])
        spf = "inf" if se["pf"] == float("inf") else "%.2f" % se["pf"]
        print("  %-5s n=%-6d win=%5.1f%%  E=%+.3f%%  PF=%-5s sum=%+.1f%%"
              % (sid, se["n"], se["win"], se["exp"], spf, se["sum"]))

byr = defaultdict(list)
for t in sub:
    byr[t.get("regime_at_entry", "?")].append(t)
print()
print("BY REGIME")
for rg in sorted(byr, key=lambda k: -len(byr[k])):
    re_ = bt.expectancy(byr[rg])
    print("  %-12s n=%-6d win=%5.1f%%  E=%+.3f%%" % (rg, re_["n"], re_["win"], re_["exp"]))

if len(by) > 1:
    print()
    print("STRATEGY x REGIME  (E per trade, n) - cells under 20 trades marked ?")
    regs = sorted(byr)
    print("  %-6s" % "" + "".join("%>22s".replace(">", "") % r for r in regs))
    for sid in sorted(by):
        cells = ""
        for rg in regs:
            ts = [t for t in by[sid] if t.get("regime_at_entry") == rg]
            if not ts:
                cells += "%22s" % "-"
                continue
            se = bt.expectancy(ts)
            cells += ("%14.3f%% (%d)%s" % (se["exp"], se["n"], "?" if se["n"] < 20 else " "))[:22].rjust(22)
        print("  %-6s%s" % (sid, cells))
print()
