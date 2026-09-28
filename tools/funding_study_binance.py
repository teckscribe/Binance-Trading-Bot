"""
tools/funding_study_binance.py
Does "short the crowded funding" work on BINANCE?  (DCB EXPERIMENT_LOG 1.39/1.42)

DCB measured, on Delta over a year: short a coin whose funding rate is >= 0.10%
per payment, hold 24h, stop = entry + 1.5 x ATR(14, 1h) x sqrt(24).
Result +1.51%/trade (PF 1.37, 506 trades) -- but the walk-forward FAILED twice
and 5 small coins carried 87% of the profit. Nothing about that is evidence for
Binance: different listings, different crowd, 8h funding on nearly everything,
and Binance caps funding on most contracts.

THE RULE IS FIXED BEFORE RESULTS ARE SEEN, identical to the Delta variant
thr0.10_SHORT_24h, so this is a replication and not a new search:

  decision times : every funding settlement (Binance settles 00/08/16 UTC on
                   8h contracts; the schedule is read per contract, not assumed)
  signal         : the rate SETTLED at T, i.e. known at T. Short if >= RATE_THR.
  entry          : the 1h close at T (no look-ahead: that bar has closed)
  stop           : entry + STOP_K x ATR(14 closed 1h bars at T) x sqrt(HOLD_H),
                   checked against 1h HIGHS, gap-aware (a bar that opens beyond
                   the stop fills at the open, not the stop)
  exit           : HOLD_H hours after entry, at that bar's close
  costs          : TAKER_FEE per side + SLIP per side, and signed funding for
                   every settlement inside the hold (a short RECEIVES positive
                   funding)

Control, as in the Delta study: each trade is paired with a random OTHER coin
shorted at the same T for the same hold. If the year simply paid shorts, the
twins capture that and the edge must beat them.

Usage:
    python tools/funding_study_binance.py --days 365            # full run
    python tools/funding_study_binance.py --days 120 --quick    # smaller
Writes data/funding_study_binance.json (all trades) and prints the report.
"""
from __future__ import annotations

import os
import sys
import json
import time
import random
import argparse
import datetime as dt
from collections import defaultdict

import requests

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(_ROOT, "data")
BASE = "https://fapi.binance.com"

RATE_THR = 0.0010        # 0.10% per settlement
HOLD_H = 24
STOP_K = 1.5
TAKER_FEE = 0.0004       # Binance USDM taker
SLIP = 0.0003            # per side, as the Delta study assumed
EXCLUDE_SUFFIX = ("BUSD",)   # tokenised equities etc. are filtered by name below

SESSION = requests.Session()


def _get(path, **params):
    for attempt in range(5):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (418, 429):
                time.sleep(2 + 3 * attempt)
                continue
            return None
        except Exception:
            time.sleep(1 + attempt)
    return None


def perp_symbols():
    info = _get("/fapi/v1/exchangeInfo") or {}
    out = []
    for s in info.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and not s["symbol"].endswith(EXCLUDE_SUFFIX)):
            out.append(s["symbol"])
    return sorted(out)


def funding_history(symbol, start_ms, end_ms):
    """All settlements in the window, oldest first: [(ms, rate), ...]."""
    rows, cur = [], start_ms
    while cur < end_ms:
        b = _get("/fapi/v1/fundingRate", symbol=symbol, startTime=cur,
                 endTime=end_ms, limit=1000)
        if not b:
            break
        rows += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in b]
        if len(b) < 1000:
            break
        cur = int(b[-1]["fundingTime"]) + 1
    seen, out = set(), []
    for t, r in sorted(rows):
        if t not in seen:
            seen.add(t)
            out.append((t, r))
    return out


def klines_1h(symbol, start_ms, end_ms):
    """{open_ms: (open, high, low, close)} for closed 1h bars."""
    out, cur = {}, start_ms
    while cur < end_ms:
        b = _get("/fapi/v1/klines", symbol=symbol, interval="1h",
                 startTime=cur, endTime=end_ms, limit=1500)
        if not b:
            break
        for k in b:
            out[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        if len(b) < 1500:
            break
        cur = int(b[-1][0]) + 3_600_000
    return out


def atr14(bars, t_ms):
    """ATR(14) over the 14 bars CLOSED at or before t_ms. None if short."""
    keys = [k for k in bars if k + 3_600_000 <= t_ms]
    if len(keys) < 15:
        return None
    keys.sort()
    trs = []
    for i in range(len(keys) - 14, len(keys)):
        _, h, l, _ = bars[keys[i]]
        pc = bars[keys[i - 1]][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / 14.0


def simulate(symbol, bars, funds, t_ms):
    """
    Short at the close of the bar ending at t_ms; stop / 24h exit on 1h bars.
    Returns the net return as a fraction, or None if the data is incomplete.
    """
    entry_bar = t_ms - 3_600_000
    if entry_bar not in bars:
        return None
    entry = bars[entry_bar][3]
    a = atr14(bars, t_ms)
    if not a or entry <= 0 or a <= 0:
        return None
    stop = entry + STOP_K * a * (HOLD_H ** 0.5)

    exit_px, hit = None, False
    for i in range(HOLD_H):
        k = t_ms + i * 3_600_000
        b = bars.get(k)
        if b is None:
            return None
        o, h, l, c = b
        if o >= stop:                      # gapped through: fill at the open
            exit_px, hit = o, True
            break
        if h >= stop:
            exit_px, hit = stop, True
            break
        exit_px = c
    if exit_px is None:
        return None

    gross = (entry - exit_px) / entry                       # short
    end_ms = t_ms + HOLD_H * 3_600_000
    fund = sum(r for ts, r in funds if t_ms <= ts < end_ms)  # short RECEIVES +r
    cost = 2 * (TAKER_FEE + SLIP)
    return {"symbol": symbol, "t": t_ms, "entry": entry, "stop": stop,
            "exit": exit_px, "stop_hit": hit, "gross": gross, "funding": fund,
            "net": gross + fund - cost,
            "stop_dist": (stop - entry) / entry}


def pf(xs):
    g = sum(x for x in xs if x > 0)
    l = -sum(x for x in xs if x < 0)
    return g / l if l > 0 else float("inf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--quick", action="store_true", help="cap the coin list")
    a = ap.parse_args()

    end = int(time.time() * 1000)
    start = end - a.days * 86_400_000
    syms = perp_symbols()
    if a.quick:
        syms = syms[:150]
    print(f"{len(syms)} USDT perps | window {a.days}d")

    # 1. funding history everywhere -- cheap, and it tells us which coins matter
    print("fetching funding history...")
    fh, hits = {}, defaultdict(list)
    for i, s in enumerate(syms, 1):
        f = funding_history(s, start, end)
        if f:
            fh[s] = f
            for t, r in f:
                if r >= RATE_THR:
                    hits[s].append(t)
        if i % 100 == 0:
            print(f"  {i}/{len(syms)}  coins with a signal so far: {len(hits)}")
    n_sig = sum(len(v) for v in hits.values())
    print(f"coins with >=1 signal: {len(hits)}   total signals: {n_sig}")
    if not n_sig:
        print("no signals in the window — nothing to test")
        return

    # 2. 1h bars only for coins that ever signalled, plus a twin pool
    need = sorted(hits)
    twin_pool = [s for s in fh if s not in hits][:120]
    print(f"fetching 1h bars for {len(need)} signal coins + {len(twin_pool)} twins...")
    bars = {}
    for i, s in enumerate(need + twin_pool, 1):
        bars[s] = klines_1h(s, start - 20 * 3_600_000, end)
        if i % 40 == 0:
            print(f"  {i}/{len(need)+len(twin_pool)}")

    # 3. simulate the rule, and a random twin short at the same moment
    random.seed(11)
    trades, twins = [], []
    for s in need:
        for t in hits[s]:
            r = simulate(s, bars.get(s, {}), fh.get(s, []), t)
            if r:
                r["rate"] = dict(fh[s]).get(t)
                trades.append(r)
                cands = [c for c in twin_pool if bars.get(c)]
                if cands:
                    c = random.choice(cands)
                    w = simulate(c, bars[c], fh.get(c, []), t)
                    if w:
                        twins.append(w)

    if not trades:
        print("signals found but no simulable trade (missing bars)")
        return
    net = [t["net"] for t in trades]
    tnet = [t["net"] for t in twins]
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "funding_study_binance.json"), "w") as f:
        json.dump({"trades": trades, "twins": twins}, f)

    print("\n" + "=" * 78)
    print(f"RULE: short funding >= {RATE_THR*100:.2f}%, hold {HOLD_H}h, "
          f"stop {STOP_K}xATR(1h)x sqrt({HOLD_H}), fees {TAKER_FEE*100:.2f}%/side + slip {SLIP*100:.2f}%/side")
    print("=" * 78)
    print(f"  trades {len(net)}   mean {100*sum(net)/len(net):+.3f}%/trade   PF {pf(net):.2f}   "
          f"win {100*sum(1 for x in net if x>0)/len(net):.1f}%   total {100*sum(net):+.1f}%")
    if tnet:
        print(f"  random twins {len(tnet)}  mean {100*sum(tnet)/len(tnet):+.3f}%/trade  PF {pf(tnet):.2f}"
              f"   edge over twins {100*(sum(net)/len(net)-sum(tnet)/len(tnet)):+.3f}%/trade")
    fu = [t["funding"] for t in trades]
    sd = sorted(t["stop_dist"] for t in trades)
    print(f"  funding received  mean {100*sum(fu)/len(fu):+.3f}%/trade")
    print(f"  stop distance     median {100*sd[len(sd)//2]:.1f}%  p90 {100*sd[int(.9*len(sd))]:.1f}%")
    print(f"  stopped out       {100*sum(1 for t in trades if t['stop_hit'])/len(trades):.0f}% of trades")

    # outlier dependency (AGENTS.md: >35% from top 3 = fragile)
    srt = sorted(net, reverse=True)
    top3 = sum(srt[:3])
    print(f"  top-3 trades      {100*top3/sum(net) if sum(net) else 0:.0f}% of total P&L "
          f"({'FRAGILE by the 35% rule' if sum(net) and top3/sum(net) > 0.35 else 'passes the 35% rule'})")

    # concentration
    bysym = defaultdict(float)
    for t in trades:
        bysym[t["symbol"]] += t["net"]
    top5 = sorted(bysym.values(), reverse=True)[:5]
    print(f"  top-5 coins       {100*sum(top5)/sum(net) if sum(net) else 0:.0f}% of total P&L "
          f"(of {len(bysym)} coins traded)")

    # monthly
    bym = defaultdict(list)
    for t in trades:
        bym[dt.datetime.utcfromtimestamp(t["t"]/1000).strftime("%Y-%m")].append(t["net"])
    pos = sum(1 for m in bym if sum(bym[m]) > 0)
    print(f"  months positive   {pos}/{len(bym)}")
    for m in sorted(bym):
        v = bym[m]
        print(f"    {m}  n {len(v):>4}  mean {100*sum(v)/len(v):+7.3f}%  total {100*sum(v):+8.1f}%")


if __name__ == "__main__":
    main()
