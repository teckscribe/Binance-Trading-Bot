"""
kronos/whale_analyze.py  --  measure whether collected positioning data
separates CSM winners from losers.  Offline; run anytime.

Joins whale_scores.jsonl (positioning per CSM candidate, from whale_worker) with
executed CSM outcomes (data/ml/signals.jsonl x outcomes.jsonl) by symbol +
nearest time + entry-price, then reports PF by positioning-feature tercile and a
priority-ranking simulation.  Defers until enough matched trades exist.

A real edge shows a MONOTONIC PF trend across terciles in a direction that makes
sense, reproduced as the sample grows -- not a U-shape (noise).
"""
import os, sys, json
from datetime import datetime
import numpy as np
sys.path.insert(0, os.path.abspath("."))
try:
    from backtest_optimizer import ROUND_TRIP_FEE, SLIPPAGE_PCT
    COST = ROUND_TRIP_FEE + 2 * SLIPPAGE_PCT
except Exception:
    COST = 0.0011
BAR = 1 / 0.7
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
MIN_N = int(os.getenv("WHALE_MIN_N", "120"))


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8")] if os.path.exists(p) else []


def T(s):
    try: return datetime.fromisoformat(s)
    except Exception: return None


STRATEGIES = ("CSM", "NASOS_V4", "TSMOM_4H")


def main():
    whale_all = [r for r in load_jsonl(os.path.join(_HERE, "logs", "whale_scores.jsonl"))
                 if "err" not in r and r.get("top_ls") is not None]
    sigs = {s["signal_id"]: s for s in load_jsonl(os.path.join(_ROOT, "data", "ml", "signals.jsonl")) if "signal_id" in s}
    outs = {o["signal_id"]: o for o in load_jsonl(os.path.join(_ROOT, "data", "ml", "outcomes.jsonl")) if "signal_id" in o}
    for strategy in STRATEGIES:
        print(f"\n{'=' * 20} {strategy} {'=' * 20}")
        _analyze(strategy, whale_all, sigs, outs)


def _analyze(strategy, whale_all, sigs, outs):
    # Queue rows carry a strategy tag since 2026-09-14 (NASOS_V4 queued
    # alongside CSM); untagged rows are CSM. A trade only matches positioning
    # rows collected for a candidate of the same strategy.
    whale = [r for r in whale_all if r.get("strategy", "CSM") == strategy]
    byS = {}
    for r in whale:
        byS.setdefault(r["symbol"], []).append(r)

    rows = []
    for sid, s in sigs.items():
        if s.get("strategy") != strategy:
            continue
        o = outs.get(sid)
        if not o:
            continue
        pnl = o.get("pnl_equity_pct", o.get("pnl_pct"))
        if pnl is None:
            continue
        et = T(s.get("timestamp")); ep = s.get("entry_price")
        cands = byS.get(s.get("symbol"), [])
        if et is None or ep is None or not cands:
            continue
        best = min(cands, key=lambda r: (abs(float(r.get("entry_price") or 0) - float(ep)) / float(ep),
                                         abs((T(r["ts"]) - et).total_seconds())))
        if abs(float(best.get("entry_price") or 0) - float(ep)) / float(ep) > 0.005:
            continue
        sign = 1.0 if s.get("direction") == "LONG" else -1.0
        top = best.get("top_ls"); tak = best.get("taker_bs"); glob = best.get("glob_ls")
        rows.append({
            "net": float(pnl) - COST,
            "dir_top": sign * np.log(top) if top and top > 0 else 0.0,
            "dir_taker": sign * np.log(tak) if tak and tak > 0 else 0.0,
            "top_ls": top or 0.0,
            "oi_chg6": best.get("oi_chg6") or 0.0,
            "smart_crowd": (top / glob) if (top and glob and glob > 0) else 0.0,
        })

    print(f"whale_scores rows: {len(whale)}   matched {strategy} outcomes: {len(rows)}")
    if len(rows) < MIN_N:
        print(f"\nDEFERRED: need >= {MIN_N} matched {strategy} trades, have {len(rows)}.")
        print("  Re-run as whale_worker + live trades accumulate.")
        return

    def pf(sub):
        w = sum(x["net"] for x in sub if x["net"] > 0); l = -sum(x["net"] for x in sub if x["net"] <= 0)
        return (w / l) if l > 0 else float("inf")
    def mean(sub): return 100 * sum(x["net"] for x in sub) / len(sub) if sub else 0.0

    print(f"\nBASELINE  N {len(rows)}  PF {pf(rows):.2f}  mean {mean(rows):+.4f}%   BAR PF>{BAR:.3f}")
    for key in ["dir_top", "dir_taker", "top_ls", "oi_chg6", "smart_crowd"]:
        vals = [r[key] for r in rows]
        q33, q66 = np.percentile(vals, [33, 66])
        b = {"low ": [r for r in rows if r[key] <= q33],
             "mid ": [r for r in rows if q33 < r[key] <= q66],
             "high": [r for r in rows if r[key] > q66]}
        print(f"== {key} ==  " + "  ".join(f"{n}PF {pf(v):.2f}(N{len(v)})" for n, v in b.items()))

    ranked = sorted(rows, key=lambda r: r["dir_top"], reverse=True); h = len(ranked) // 2
    print(f"\nPRIORITY (rank by dir_top, keep top half): "
          f"top PF {pf(ranked[:h]):.2f} vs bot PF {pf(ranked[h:]):.2f} vs all {pf(rows):.2f}")


if __name__ == "__main__":
    main()

