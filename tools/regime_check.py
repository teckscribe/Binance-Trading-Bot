"""
Verify the regime engine against 90 days of historical BTC/ETH/SOL 1h data.

Checks:
  1. Does the LIVE code path (35-bar fetch window) produce the same regime
     labels as the BACKTEST path (expanding window)? The permission matrix was
     derived from the backtest labels, so a mismatch invalidates it.
  2. Regime distribution, flip frequency, dwell time.
  3. Sensitivity of each gate (ADX / slope / VWAP / funding).
"""
import os, sys
from collections import Counter, defaultdict

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

import pandas as pd
import numpy as np
import backtest_optimizer as bt
from modules.regime_engine import (
    _coin_trend, _compute_adx, _sma_slope_pct, _decide_regime,
    HYSTERESIS_PCT, SMA_PERIOD, MIN_BARS_NEEDED, ADX_REGIME_MIN,
    FUNDING_EXTREME,
)

DAYS = 90
LIVE_FETCH_LIMIT = MIN_BARS_NEEDED + 10          # what classify_regime() fetches

btc = bt.load_data("BTCUSDT", "1h", DAYS)
eth = bt.load_data("ETHUSDT", "1h", DAYS)
sol = bt.load_data("SOLUSDT", "1h", DAYS)
print("bars loaded: BTC=%d ETH=%d SOL=%d" % (len(btc), len(eth), len(sol)))
print("window: %s -> %s" % (btc.index[0], btc.index[-1]))

start_ms = int(btc.index[0].timestamp() * 1000)
end_ms   = int(btc.index[-1].timestamp() * 1000)
funding  = bt.load_funding("BTCUSDT", start_ms, end_ms)
print("funding rows: %d" % len(funding))
if len(funding):
    print("  funding range: %+.6f .. %+.6f  (EXTREME=+-%.4f)"
          % (funding.min(), funding.max(), FUNDING_EXTREME))
    ext_hi = int((funding > FUNDING_EXTREME).sum())
    ext_lo = int((funding < -FUNDING_EXTREME).sum())
    print("  funding beyond EXTREME: %d high (%.1f%%), %d low (%.1f%%)"
          % (ext_hi, ext_hi / len(funding) * 100, ext_lo, ext_lo / len(funding) * 100))


def run(window_mode):
    """window_mode: 'expanding' (backtest) or 'rolling' (live)."""
    series, current = {}, ""
    diag = []
    for i in range(MIN_BARS_NEEDED, len(btc)):
        ts = btc.index[i]
        if window_mode == "expanding":
            b = btc.iloc[:i + 1]
            e = eth[eth.index <= ts]
            s = sol[sol.index <= ts]
        else:                                     # rolling: mimic live fetch
            b = btc.iloc[max(0, i + 1 - LIVE_FETCH_LIMIT): i + 1]
            e = eth[eth.index <= ts].iloc[-LIVE_FETCH_LIMIT:]
            s = sol[sol.index <= ts].iloc[-LIVE_FETCH_LIMIT:]
        if len(e) < MIN_BARS_NEEDED or len(s) < MIN_BARS_NEEDED:
            continue

        f = 0.0
        if len(funding):
            prior = funding[funding.index <= ts]
            if len(prior):
                f = float(prior.iloc[-1])

        bt_, et_, st_ = _coin_trend(b, "BTC"), _coin_trend(e, "ETH"), _coin_trend(s, "SOL")
        adx   = _compute_adx(b)
        slope = _sma_slope_pct(b)
        raw   = _decide_regime(bt_, et_, st_, f, adx, slope)

        price = float(b["close"].iloc[-1])
        sma20 = float(b["close"].rolling(SMA_PERIOD).mean().iloc[-1])
        if current and current != raw:
            hyst_ok = True
            if raw not in ("OVERHEATED", "OVERSOLD") and \
               current not in ("OVERHEATED", "OVERSOLD"):
                dist = (price - sma20) / sma20 if sma20 > 0 else 0
                if raw == "BULL_TREND" and dist < HYSTERESIS_PCT:
                    hyst_ok = False
                elif raw == "BEAR_TREND" and dist > -HYSTERESIS_PCT:
                    hyst_ok = False
                elif raw == "RANGING" and abs(dist) > HYSTERESIS_PCT:
                    hyst_ok = False
            if not hyst_ok:
                raw = current
        if raw != current:
            current = raw
        series[ts] = current
        diag.append({"ts": ts, "adx": adx, "slope": slope, "btc": bt_,
                     "eth": et_, "sol": st_, "regime": current, "funding": f})
    return series, pd.DataFrame(diag)


exp_series, exp_diag = run("expanding")
liv_series, liv_diag = run("rolling")

def dist(series, label):
    c = Counter(series.values())
    tot = len(series)
    print("\n%s  (%d bars)" % (label, tot))
    for k, v in c.most_common():
        print("    %-12s %5d bars  %5.1f%%" % (k, v, v / tot * 100))
    return c

print("\n" + "=" * 78)
print("1. REGIME DISTRIBUTION — backtest labelling vs live labelling")
print("=" * 78)
dist(exp_series, "EXPANDING window  (what build_regime_series does)")
dist(liv_series, "ROLLING 35-bar    (what classify_regime does live)")

common = sorted(set(exp_series) & set(liv_series))
agree = sum(1 for t in common if exp_series[t] == liv_series[t])
print("\n  AGREEMENT: %d/%d bars = %.1f%%" % (agree, len(common), agree / len(common) * 100))

mism = Counter((exp_series[t], liv_series[t]) for t in common if exp_series[t] != liv_series[t])
if mism:
    print("\n  Top disagreements (backtest_label -> live_label):")
    for (a, b_), n in mism.most_common(8):
        print("    %-12s -> %-12s %5d bars (%.1f%%)" % (a, b_, n, n / len(common) * 100))

print("\n" + "=" * 78)
print("2. STABILITY — flips and dwell time (expanding / backtest labelling)")
print("=" * 78)
keys = sorted(exp_series)
flips = sum(1 for i in range(1, len(keys)) if exp_series[keys[i]] != exp_series[keys[i-1]])
print("  regime changes: %d over %d hours (1 per %.1f h)"
      % (flips, len(keys), len(keys) / max(flips, 1)))
runs, cur, n = [], exp_series[keys[0]], 1
for i in range(1, len(keys)):
    if exp_series[keys[i]] == cur:
        n += 1
    else:
        runs.append((cur, n)); cur, n = exp_series[keys[i]], 1
runs.append((cur, n))
by = defaultdict(list)
for r, ln in runs:
    by[r].append(ln)
print("  %-12s %6s %8s %8s %8s" % ("REGIME", "SPELLS", "MEAN_H", "MED_H", "MAX_H"))
for r in sorted(by, key=lambda k: -sum(by[k])):
    a = by[r]
    print("  %-12s %6d %8.1f %8.1f %8d"
          % (r, len(a), np.mean(a), np.median(a), max(a)))

print("\n" + "=" * 78)
print("3. GATE SENSITIVITY (expanding labelling)")
print("=" * 78)
d = exp_diag
print("  ADX:   median %.1f | %% bars below %d (forces RANGING): %.1f%%"
      % (d["adx"].median(), ADX_REGIME_MIN, (d["adx"] < ADX_REGIME_MIN).mean() * 100))
print("  ADX==0 (compute failed -> gate SKIPPED, fail-open): %d bars" % int((d["adx"] == 0).sum()))
print("  slope: median abs %.4f%%" % (d["slope"].abs().median() * 100))
print("  BTC trend: %s" % dict(Counter(d["btc"])))
print("  ETH trend: %s" % dict(Counter(d["eth"])))
print("  SOL trend: %s" % dict(Counter(d["sol"])))

print("\n" + "=" * 78)
print("4. WHAT THE PERMISSION MATRIX MEANS IN PRACTICE")
print("=" * 78)
PERM = {"BULL_TREND": ["CSM"], "BEAR_TREND": ["CSM", "LIQ", "VRP"],
        "RANGING": ["CSM"], "OVERHEATED": [], "OVERSOLD": []}
for label, series in (("backtest labelling", exp_series), ("live labelling", liv_series)):
    c = Counter(series.values()); tot = len(series)
    idle = sum(v for k, v in c.items() if not PERM.get(k, []))
    csm  = sum(v for k, v in c.items() if "CSM" in PERM.get(k, []))
    liq  = sum(v for k, v in c.items() if "LIQ" in PERM.get(k, []))
    print("  %-20s  CSM can trade %5.1f%% of hours | LIQ+VRP %5.1f%% | NOTHING trades %5.1f%%"
          % (label, csm / tot * 100, liq / tot * 100, idle / tot * 100))
