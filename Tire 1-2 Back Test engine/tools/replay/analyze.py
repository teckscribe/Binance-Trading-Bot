"""
tools/replay/analyze.py — turn a trade set into a ranked list of what to change.

Works on either source:
    Tier 1   data/bt_tier1_90d_CSM.json      (backtest_optimizer --cache)
    Tier 2   test/data/replay_30d.json       (driver.py --out)

Design note — why every lever is scored TWICE
---------------------------------------------
The single most expensive mistake in this project's history has been reading a
per-trade improvement as a profit improvement. With 3 slots against thousands
of slot-rejected signals, ANY filter that removes candidates raises expectancy
and can still lose money, because the trades it removes were holding slots that
now sit empty or get filled by something worse. EXPERIMENT_LOG §10 lists three
separate ideas killed by exactly this (blacklisting, hour filtering, LIQ/VRP).

So each lever below reports:
    E[net]      per-trade expectancy on notional  — the "quality" view
    RETURN      account return through the real risk model — the view that pays

A lever is only worth implementing when BOTH improve. Where they disagree, the
account column wins.

Usage:
    python tools/replay/analyze.py <trades.json> [--capital 100] [--top 12]
"""

import os
import sys
import json
import logging
import argparse
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
logging.disable(logging.CRITICAL)

# Windows consoles default to cp1252 and this report contains em dashes and
# Greek deltas. Ubuntu is UTF-8 already; this makes it portable either way.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backtest_optimizer import simulate_portfolio, expectancy, ROUND_TRIP_FEE
import modules.risk_engine as RE

BAR = "=" * 96
SUB = "-" * 96


# ── loading ──────────────────────────────────────────────────────────────────

def load(path):
    """Accept Tier 1 trade dicts or Tier 2 closed-position dicts."""
    raw = json.load(open(path))
    if isinstance(raw, dict):                     # {variant: [...]}
        raw = raw.get("B") or next(iter(raw.values()))
    out = []
    for t in raw:
        d = dict(t)
        # Tier 2 positions carry entry_time/exit_time as ISO strings and express
        # P&L in USDT; Tier 1 trades carry pnl_pct/net_pct on notional.
        for k in ("entry_time", "exit_time"):
            if d.get(k):
                d[k] = pd.Timestamp(d[k])
        if "net_pct" not in d:
            if "pnl_pct" in d:
                d["net_pct"] = float(d["pnl_pct"]) - ROUND_TRIP_FEE
            else:
                continue
        if d.get("entry_time") is None or d.get("exit_time") is None:
            continue
        # Tier 2 emits order_engine position dicts, which carry neither `hours`
        # (expectancy needs it) nor `initial_sl_price` in every case
        # (simulate_portfolio sizes off it). Derive both rather than making the
        # live engine carry backtest-shaped fields.
        if "hours" not in d:
            d["hours"] = (d["exit_time"] - d["entry_time"]).total_seconds() / 3600
        if not d.get("initial_sl_price"):
            d["initial_sl_price"] = d.get("sl_price")
        d.setdefault("strength", 1.0)
        out.append(d)
    return out


def acct(trades, capital):
    """Account return through the production risk model."""
    if not trades:
        return None
    return simulate_portfolio(
        trades, capital=capital, slots=RE.MAX_CONCURRENT,
        margin_pct=RE.MAX_MARGIN_PCT, daily_cap=RE.DAILY_LOSS_CAP,
        weekly_cap=RE.WEEKLY_LOSS_CAP, label="", verbose=False)


def line(tag, trades, capital, base=None, note=""):
    e = expectancy(trades)
    if not e:
        print(f"{tag:<40}{'no trades':>10}")
        return None
    a = acct(trades, capital)
    d_e = f"{e['exp'] - base[0]:+.3f}" if base else ""
    d_a = f"{a['return_pct'] - base[1]:+.1f}" if base else ""
    print(f"{tag:<40}{e['n']:>7}{e['win']:>7.1f}%{e['exp']:>9.3f}%{d_e:>9}"
          f"{a['return_pct']:>10.1f}%{d_a:>9}{a['max_dd']:>8.1f}%  {note}")
    return e["exp"], a["return_pct"]


# ── report sections ──────────────────────────────────────────────────────────

def headline(t, capital):
    e, a = expectancy(t), acct(t, capital)
    print(f"\n{BAR}\nHEADLINE\n{BAR}")
    print(f"  trades            {e['n']}")
    print(f"  win rate          {e['win']:.1f}%")
    print(f"  E[net]/trade      {e['exp']:+.3f}%  of notional, net of fees")
    print(f"  profit factor     {e['pf']:.2f}")
    print(f"  account return    {a['return_pct']:+.2f}%   (maxDD {a['max_dd']:.1f}%)")
    print(f"  taken / signals   {a['taken']} of {a['signals']}")
    skips = [("slots", a.get("slot_skip", 0)), ("loss caps", a.get("cap_skip", 0)),
             ("cooldown", a.get("cooldown_skip", 0)),
             ("per-cycle", a.get("cycle_skip", 0)),
             ("sizing", a.get("size_skip", 0)), ("margin", a.get("margin_skip", 0))]
    tot = sum(v for _, v in skips) or 1
    print(f"  rejected by       " + ", ".join(
        f"{k} {v} ({v/tot*100:.0f}%)" for k, v in skips if v))


def exits(t):
    print(f"\n{BAR}\nWHERE THE MONEY GOES\n{BAR}")
    print(f"{'exit':<14}{'n':>7}{'share':>8}{'mean':>10}{'sum':>11}  contribution")
    print(SUB)
    tot = sum(x["net_pct"] for x in t) * 100
    by = defaultdict(list)
    for x in t:
        by[x.get("exit_reason", "?")].append(x["net_pct"] * 100)
    for r, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
        s = sum(v)
        bar = "#" * min(40, int(abs(s) / max(abs(tot), 1e-9) * 20))
        print(f"{r:<14}{len(v):>7}{len(v)/len(t)*100:>7.0f}%{np.mean(v):>9.3f}%"
              f"{s:>10.1f}%  {bar}")
    print(SUB)
    be = by.get("BE_HIT", [])
    if be:
        print(f"BE_HIT is {len(be)/len(t)*100:.0f}% of trades and contributes "
              f"{sum(be):+.1f}% — every one of those is a trade that was ahead "
              f"and gave it back.")


def levers(t, capital):
    """Each candidate change, scored on BOTH axes."""
    base = (expectancy(t)["exp"], acct(t, capital)["return_pct"])
    print(f"\n{BAR}\nLEVERS — per-trade quality vs account return\n{BAR}")
    print(f"{'change':<40}{'n':>7}{'win':>8}{'E[net]':>9}{'ΔE':>9}"
          f"{'RETURN':>10}{'Δret':>9}{'maxDD':>8}")
    print(SUB)
    line("baseline — as measured", t, capital)
    print(SUB)

    # regime exclusions
    regs = sorted({x.get("regime_at_entry") for x in t} - {None, "UNKNOWN"})
    for r in regs:
        sub = [x for x in t if x.get("regime_at_entry") != r]
        if sub and len(sub) != len(t):
            line(f"drop {r}", sub, capital, base)

    # momentum floor — only if the strategy recorded it
    moms = [x["normalized_mom"] for x in t if x.get("normalized_mom") is not None]
    if len(moms) > len(t) * 0.5:
        for q in (0.25, 0.50, 0.75):
            thr = float(np.quantile(moms, q))
            sub = [x for x in t if (x.get("normalized_mom") or 0) >= thr]
            if sub:
                line(f"only momentum >= {thr:.1f} (top {100-q*100:.0f}%)",
                     sub, capital, base)
    else:
        print(f"{'momentum filter':<40}{'n/a — normalized_mom not recorded on these trades':>10}")

    # hour of day, IST (the one effect EXPERIMENT_LOG found statistically real)
    ist = [(x, (x["entry_time"] + pd.Timedelta(hours=5, minutes=30)).hour) for x in t]
    worst = sorted({h for _, h in ist})
    hr = {h: np.mean([x["net_pct"] for x, hh in ist if hh == h]) * 100
          for h in worst}
    bad = [h for h in worst if hr[h] < 0]
    if bad and len(bad) < len(worst):
        sub = [x for x, h in ist if h not in bad]
        line(f"drop {len(bad)} negative IST hours", sub, capital, base,
             note="in-sample fit — verify OOS")

    # slippage: not a lever, a cost that is currently unmodelled
    print(SUB)
    for sl in (0.0002, 0.0005, 0.0010):
        sub = [dict(x, pnl_pct=x["pnl_pct"] - 2 * sl,
                    net_pct=x["net_pct"] - 2 * sl) for x in t if "pnl_pct" in x]
        if sub:
            line(f"+ slippage {sl*100:.2f}%/side", sub, capital, base,
                 note="cost, not a choice")


def robustness(t):
    n = np.sort(np.array([x["net_pct"] for x in t]))[::-1]
    s = n.sum() * 100
    k = max(1, len(n) // 100)
    print(f"\n{BAR}\nROBUSTNESS — how concentrated is the result?\n{BAR}")
    print(f"  sum of net returns          {s:>9.1f}%")
    print(f"  excluding best 5 trades     {n[5:].sum()*100:>9.1f}%")
    print(f"  excluding best 10           {n[10:].sum()*100:>9.1f}%")
    print(f"  excluding best 1% ({k})      {n[k:].sum()*100:>9.1f}%")
    if s > 0 and n[k:].sum() <= 0:
        print("  -> the entire result rests on the top 1% of trades. Any change "
              "that clips\n     the right tail (breakeven, tighter TP) destroys it.")


def compare_live(t):
    """Reconcile the replay's exit mix against real paper trades."""
    import glob
    rows = []
    for p in glob.glob("logs/strategies/*/*.json"):
        for ln in open(p):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("type") == "EXIT":
                rows.append(d)
    if not rows:
        return
    print(f"\n{BAR}\nRECONCILIATION vs LIVE PAPER ({len(rows)} logged exits)\n{BAR}")
    a = Counter(x.get("exit_reason", "?") for x in t)
    b = Counter(x.get("exit_reason", "?") for x in rows)
    print(f"{'exit':<14}{'this run':>12}{'live paper':>13}{'gap':>10}")
    print(SUB)
    for k in sorted(set(a) | set(b)):
        pa, pb = a[k] / len(t) * 100, b[k] / len(rows) * 100
        flag = "  <-- investigate" if abs(pa - pb) > 12 else ""
        print(f"{k:<14}{pa:>11.0f}%{pb:>12.0f}%{pa-pb:>+9.0f}pp{flag}")
    print(SUB)
    print("A gap over ~12pp means the replay is NOT reproducing live and any")
    print("conclusion below it is unsafe. Reconcile first, tune second.")


def main():
    ap = argparse.ArgumentParser(description="Rank what to change, from a trade set")
    ap.add_argument("trades")
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--no-live-compare", action="store_true")
    a = ap.parse_args()

    t = load(a.trades)
    if not t:
        sys.exit("No usable trades in that file.")
    print(f"\nSource: {a.trades}   ({len(t)} trades, "
          f"{min(x['entry_time'] for x in t):%Y-%m-%d} -> "
          f"{max(x['exit_time'] for x in t):%Y-%m-%d})")

    headline(t, a.capital)
    exits(t)
    robustness(t)
    if not a.no_live_compare:
        compare_live(t)
    levers(t, a.capital)

    print(f"\n{BAR}\nHOW TO READ THIS\n{BAR}")
    print("Implement a lever only when ΔE and Δret are BOTH positive. A positive")
    print("ΔE with a negative Δret means the filter is removing trades whose")
    print("slots then go unused or to something worse — that has killed three")
    print("separate ideas in this project already (EXPERIMENT_LOG §3).")
    print("Everything here is in-sample on one window. Re-run on a second,")
    print("non-overlapping window before changing production.")


if __name__ == "__main__":
    main()
