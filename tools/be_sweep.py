"""
Sweep BE_ATR_MULT over the full 90-day CSM set.

Breakeven currently arms at 1x ATR = 50% of the stop distance and only 17-25%
of the way to target. Live is running 80% BE_HIT against 20% in the backtest.
This regenerates trades at each multiplier and reports both layers.
"""
import os, sys, glob, logging, importlib
from collections import Counter
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
import pandas as pd
import backtest_optimizer as bt
from modules.strategies.base_strategy import BaseStrategy
import modules.strategies.cross_sectional_momentum as csm_mod
logging.disable(logging.WARNING)

DAYS   = 90
MULTS  = [1.0, 1.5, 2.0, 2.5, 3.0, 99.0]     # 99 = breakeven effectively off
WINDOWS = [30, 90]

syms = sorted(os.path.basename(p).split("_15m_")[0]
              for p in glob.glob("data/*_15m_%dd.csv" % DAYS))
syms = [s for s in syms if not bt.is_equity(s)]

rs = bt.build_regime_series(days=DAYS, verbose=False)

rows = []
for m in MULTS:
    csm_mod.CrossSectionalMomentum.BE_ATR_MULT = m
    BaseStrategy.BE_ATR_MULT = m
    trades = bt.collect_trades(syms, ["CSM"], rs, days=DAYS)   # no cache: logic changed
    last = max(t["entry_time"] for t in trades)
    for w in WINDOWS:
        sub = [t for t in trades if t["entry_time"] >= last - pd.Timedelta(days=w)]
        e = bt.expectancy(sub)
        r = bt.simulate_portfolio(sub, capital=100.0, verbose=False)
        ex = Counter(t.get("exit_reason", "?") for t in sub)
        n = len(sub)
        rows.append(dict(mult=m, w=w, n=n, exp=e["exp"], win=e["win"],
                         pf=e["pf"], ret=r["return_pct"], dd=r["max_dd"],
                         taken=r["taken"],
                         be=ex.get("BE_HIT", 0)/n*100, sl=ex.get("SL_HIT", 0)/n*100,
                         tp=ex.get("TP_HIT", 0)/n*100, tr=ex.get("TRAIL_HIT", 0)/n*100))
    print("  done BE_ATR_MULT=%.1f" % m); sys.stdout.flush()

for w in WINDOWS:
    print()
    print("=" * 94)
    print("BE_ATR_MULT SWEEP — %d-day window   (current setting = 1.0)" % w)
    print("=" * 94)
    print("%-8s %7s %9s %7s %7s %10s %8s   %5s %5s %5s %5s"
          % ("MULT", "N", "E[net]", "WIN%", "PF", "RETURN", "maxDD", "SL%", "BE%", "TP%", "TR%"))
    print("-" * 94)
    for r in [x for x in rows if x["w"] == w]:
        pf = "inf" if r["pf"] == float("inf") else "%.2f" % r["pf"]
        tag = "  <- current" if r["mult"] == 1.0 else ("  <- BE off" if r["mult"] == 99.0 else "")
        label = "off" if r["mult"] == 99.0 else "%.1f" % r["mult"]
        print("%-8s %7d %+8.3f%% %6.1f%% %7s %+9.2f%% %7.1f%%   %5.0f %5.0f %5.0f %5.0f%s"
              % (label, r["n"], r["exp"], r["win"], pf, r["ret"], r["dd"],
                 r["sl"], r["be"], r["tp"], r["tr"], tag))
