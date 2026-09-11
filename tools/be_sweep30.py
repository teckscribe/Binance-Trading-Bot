"""
Sweep BE_ATR_MULT over a 30-DAY scoring window.

Loads the 90-day CSVs but truncates to the last ~38 days before replay. The
per-symbol loop is O(bars^2) because run_backtest slices an expanding window,
so cutting 8,640 bars to ~3,650 is roughly a 6x speedup per multiplier — the
difference between ~26 min and ~4 min each.

38 days = 30 scored + ~8 days of headroom for WARMUP_BARS (200 x 15m = 2.1d)
and CSM's 25 hourly bars.
"""
import os, sys, glob, logging
from collections import Counter
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
import pandas as pd
import backtest_optimizer as bt
from modules.strategies.base_strategy import BaseStrategy
import modules.strategies.cross_sectional_momentum as csm_mod
logging.disable(logging.WARNING)

KEEP_DAYS = 38
SCORE     = 30
MULTS     = [1.0, 1.5, 2.0, 2.5, 3.0, 99.0]     # 99 => breakeven effectively off

# ── truncate every loaded frame to the last KEEP_DAYS ────────────────────────
_orig_load = bt.load_data
def _load_trunc(symbol, interval, days=30):
    df = _orig_load(symbol, interval, days)
    if df is None or df.empty:
        return df
    cut = df.index[-1] - pd.Timedelta(days=KEEP_DAYS)
    return df[df.index >= cut]
bt.load_data = _load_trunc

syms = sorted(os.path.basename(p).split("_15m_")[0]
              for p in glob.glob("data/*_15m_90d.csv"))
syms = [s for s in syms if not bt.is_equity(s)]
print("symbols: %d | data window: last %dd | scoring: last %dd" % (len(syms), KEEP_DAYS, SCORE))
sys.stdout.flush()

rs = bt.build_regime_series(days=90, verbose=False)
print("regime series built"); sys.stdout.flush()

rows = []
for m in MULTS:
    csm_mod.CrossSectionalMomentum.BE_ATR_MULT = m
    BaseStrategy.BE_ATR_MULT = m
    trades = bt.collect_trades(syms, ["CSM"], rs, days=90)
    last = max(t["entry_time"] for t in trades)
    sub  = [t for t in trades if t["entry_time"] >= last - pd.Timedelta(days=SCORE)]
    e = bt.expectancy(sub)
    r = bt.simulate_portfolio(sub, capital=100.0, verbose=False)
    ex = Counter(t.get("exit_reason", "?") for t in sub)
    n = len(sub)
    rows.append(dict(mult=m, n=n, exp=e["exp"], win=e["win"], pf=e["pf"],
                     ret=r["return_pct"], dd=r["max_dd"], taken=r["taken"],
                     be=ex.get("BE_HIT",0)/n*100, sl=ex.get("SL_HIT",0)/n*100,
                     tp=ex.get("TP_HIT",0)/n*100, tr=ex.get("TRAIL_HIT",0)/n*100))
    print("  BE_ATR_MULT=%-5s n=%-5d BE%%=%4.0f ret=%+8.2f%% dd=%5.1f%%"
          % (m, n, rows[-1]["be"], r["return_pct"], r["max_dd"]))
    sys.stdout.flush()

print()
print("=" * 96)
print("BE_ATR_MULT SWEEP — 30-DAY WINDOW, CSM only, $100 start")
print("=" * 96)
print("%-7s %7s %9s %7s %7s %10s %8s %7s   %5s %5s %5s %5s"
      % ("MULT","N","E[net]","WIN%","PF","RETURN","maxDD","ret/dd","SL%","BE%","TP%","TR%"))
print("-" * 96)
for r in rows:
    pf  = "inf" if r["pf"] == float("inf") else "%.2f" % r["pf"]
    lab = "off" if r["mult"] == 99.0 else "%.1f" % r["mult"]
    tag = "  <- current" if r["mult"] == 1.0 else ""
    rdd = r["ret"]/r["dd"] if r["dd"] else 0
    print("%-7s %7d %+8.3f%% %6.1f%% %7s %+9.2f%% %7.1f%% %7.1f   %5.0f %5.0f %5.0f %5.0f%s"
          % (lab, r["n"], r["exp"], r["win"], pf, r["ret"], r["dd"], rdd,
             r["sl"], r["be"], r["tp"], r["tr"], tag))
print()
print("ret/dd = return per unit of max drawdown — the risk-adjusted column.")
print("Backtest baseline exit mix (8,871 trades): SL 42% BE 20% TP 25% TRAIL 13%")
print("Live is currently running ~80% BE_HIT.")
