"""
What is regime information WORTH? That bounds any LLM contribution, since the
LLM's only lever is the regime label.

  1. current config      - CSM permitted in all 3 regimes -> label is inert
  2. regime-scaled risk  - use the label to SIZE positions (the plausible use)
  3. oracle              - perfect foresight per trade (unreachable ceiling)
"""
import os, sys, glob, logging
from collections import defaultdict
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
import pandas as pd, numpy as np, backtest_optimizer as bt
from modules.risk_engine import compute_position_size, max_total_margin_pct, max_concurrent
MAX_TOTAL_MARGIN_PCT = max_total_margin_pct()
MAX_CONCURRENT = max_concurrent()
logging.disable(logging.WARNING)

syms = sorted(os.path.basename(p).split("_15m_")[0] for p in glob.glob("data/*_15m_90d.csv"))
syms = [s for s in syms if not bt.is_equity(s)]
tr = bt.collect_trades(syms, ["CSM","NASOS_V4","ELLIOT_V8"], None, days=90,
                       cache_path="data/bt_cache_90d_CSM-NASOS-SMA-ELLIOT.json")
tr = [t for t in tr if t["strategy"] == "CSM"]          # deployed config
last = max(t["entry_time"] for t in tr)


def sim(trades, mult_fn, capital=100.0, slots=MAX_CONCURRENT):
    """Portfolio replay with a per-trade risk multiplier."""
    ts = sorted(trades, key=lambda t: t["entry_time"])
    eq = peak = capital; mdd = 0.0
    open_pos, used = [], 0.0
    taken = 0
    def close_due(now):
        nonlocal eq, peak, mdd, used
        keep = []
        for p in open_pos:
            if p["exit_time"] <= now:
                eq += p["pnl"]; used -= p["margin"]
                eq = max(eq, 0.0)
                peak = max(peak, eq)
                mdd = max(mdd, (peak-eq)/peak if peak > 0 else 0)
            else: keep.append(p)
        open_pos[:] = keep
        if not open_pos: used = 0.0
    for t in ts:
        now = t["entry_time"]; close_due(now)
        if eq <= 0: break
        if len(open_pos) >= slots: continue
        sl = t.get("initial_sl_price") or t.get("sl_price")
        if not sl: continue
        m = mult_fn(t)
        if m <= 0: continue
        s = compute_position_size(eq, float(t["entry_price"]), float(sl), "CSM", risk_mult=m)
        if not s.get("valid"): continue
        notion, marg = float(s["notional"]), float(s["margin_req"])
        if used + marg > eq * MAX_TOTAL_MARGIN_PCT: continue
        pnl = max(notion*(t["pnl_pct"] - bt.ROUND_TRIP_FEE), -marg)
        used += marg; taken += 1
        open_pos.append({"exit_time": t["exit_time"], "pnl": pnl, "margin": marg})
    if open_pos: close_due(max(p["exit_time"] for p in open_pos))
    return eq, (eq-capital)/capital*100, mdd*100, taken


# regime edge, 90d CSM:  BEAR +1.218%  RANGING +0.698%  BULL +0.359%
SCALED = {"BEAR_TREND": 1.5, "RANGING": 1.0, "BULL_TREND": 0.5}
AGGR   = {"BEAR_TREND": 2.0, "RANGING": 1.0, "BULL_TREND": 0.3}

for w in (30, 60, 90):
    sub = [t for t in tr if t["entry_time"] >= last - pd.Timedelta(days=w)]
    print("=" * 78)
    print("%d-DAY WINDOW   (%d CSM trades)" % (w, len(sub)))
    print("=" * 78)
    rows = [
        ("1. flat sizing (current)",      lambda t: 1.0),
        ("2. regime-scaled 1.5/1.0/0.5",  lambda t: SCALED.get(t.get("regime_at_entry"), 1.0)),
        ("3. regime-scaled 2.0/1.0/0.3",  lambda t: AGGR.get(t.get("regime_at_entry"), 1.0)),
        ("4. ORACLE (perfect foresight)", lambda t: 1.0 if t["net_pct"] > 0 else 0.0),
    ]
    base = None
    for name, fn in rows:
        fin, ret, dd, n = sim(sub, fn)
        if base is None: base = ret
        delta = ret - base
        print("  %-32s taken=%-5d  return %+9.2f%%  maxDD %5.1f%%   vs flat %+8.2f pp"
              % (name, n, ret, dd, delta))
    print()

print("=" * 78)
print("READING THIS")
print("=" * 78)
print("Row 2/3 = the ENTIRE value of using regime for sizing, assuming today's")
print("rule-based labels are already correct. An LLM cannot exceed this by")
print("improving labels; it can only capture some fraction of any gap between")
print("the rule engine's labels and the truth.")
print()
print("Row 4 = perfect foresight (skip every loser). Unreachable, shown only")
print("to scale the others against what omniscience would be worth.")
