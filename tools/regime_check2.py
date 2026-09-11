"""
Quantify how regime labels depend on the LENGTH of the dataframe passed in,
and test a window-invariant fix.

_coin_trend() computes VWAP with cumsum(), so its value depends on where the
dataframe happens to start. Three anchors exist in the codebase:
    50 bars   - live (data_hub.fetch_btc_reference -> live_scanner)
    35 bars   - classify_regime() fallback when called with None
    expanding - backtest_optimizer.build_regime_series (up to 2160 bars)
"""
import os, sys
from collections import Counter

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

import pandas as pd, numpy as np
import backtest_optimizer as bt
import modules.regime_engine as RE
from modules.regime_engine import (
    _compute_adx, _sma_slope_pct, _decide_regime,
    HYSTERESIS_PCT, SMA_PERIOD, MIN_BARS_NEEDED, TREND_MIN_DIST_PCT,
    TREND_CONFIRM_BARS,
)

DAYS = 90
btc = bt.load_data("BTCUSDT", "1h", DAYS)
eth = bt.load_data("ETHUSDT", "1h", DAYS)
sol = bt.load_data("SOLUSDT", "1h", DAYS)
fund = bt.load_funding("BTCUSDT", int(btc.index[0].timestamp()*1000),
                                  int(btc.index[-1].timestamp()*1000))


def coin_trend_rolling_vwap(df, vwap_period=SMA_PERIOD):
    """Window-invariant variant: rolling VWAP instead of cumulative."""
    if df is None or len(df) < MIN_BARS_NEEDED:
        return "NEUTRAL"
    close, vol = df["close"].astype(float), df["volume"].astype(float)
    sma20 = close.rolling(SMA_PERIOD).mean()
    pv = (close * vol).rolling(vwap_period).sum()
    vv = vol.rolling(vwap_period).sum().replace(0, np.nan)
    vwap = pv / vv
    signals = []
    for i in range(-TREND_CONFIRM_BARS, 0):
        price = float(close.iloc[i]); sma_val = float(sma20.iloc[i])
        vwap_val = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else price
        dist = abs(price - sma_val) / sma_val if sma_val > 0 else 0
        if dist < TREND_MIN_DIST_PCT:            signals.append("NEUTRAL")
        elif price > sma_val and price > vwap_val: signals.append("BULL")
        elif price < sma_val and price < vwap_val: signals.append("BEAR")
        else:                                     signals.append("NEUTRAL")
    if all(s == "BULL" for s in signals): return "BULL"
    if all(s == "BEAR" for s in signals): return "BEAR"
    return "NEUTRAL"


def run(mode, trend_fn=RE._coin_trend):
    series, current = {}, ""
    for i in range(MIN_BARS_NEEDED, len(btc)):
        ts = btc.index[i]
        if mode == "expanding":
            b, e, s = btc.iloc[:i+1], eth[eth.index <= ts], sol[sol.index <= ts]
        else:
            n = int(mode)
            b = btc.iloc[max(0, i+1-n): i+1]
            e = eth[eth.index <= ts].iloc[-n:]
            s = sol[sol.index <= ts].iloc[-n:]
        if len(e) < MIN_BARS_NEEDED or len(s) < MIN_BARS_NEEDED:
            continue
        f = 0.0
        if len(fund):
            prior = fund[fund.index <= ts]
            if len(prior): f = float(prior.iloc[-1])
        raw = _decide_regime(trend_fn(b), trend_fn(e), trend_fn(s), f,
                             _compute_adx(b), _sma_slope_pct(b))
        price = float(b["close"].iloc[-1])
        sma20 = float(b["close"].rolling(SMA_PERIOD).mean().iloc[-1])
        if current and current != raw:
            ok = True
            if raw not in ("OVERHEATED","OVERSOLD") and current not in ("OVERHEATED","OVERSOLD"):
                d = (price - sma20)/sma20 if sma20 > 0 else 0
                if   raw == "BULL_TREND" and d <  HYSTERESIS_PCT: ok = False
                elif raw == "BEAR_TREND" and d > -HYSTERESIS_PCT: ok = False
                elif raw == "RANGING" and abs(d) > HYSTERESIS_PCT: ok = False
            if not ok: raw = current
        if raw != current: current = raw
        series[ts] = current
    return series


CASES = [
    ("expanding", "expanding  (backtest)",        RE._coin_trend),
    ("50",        "50 bars    (LIVE, real path)", RE._coin_trend),
    ("35",        "35 bars    (classify fallback)", RE._coin_trend),
    ("100",       "100 bars   (arbitrary)",       RE._coin_trend),
]
res = {}
print("=" * 84)
print("A. SAME CODE, DIFFERENT INPUT LENGTH -> DIFFERENT REGIME (cumulative VWAP)")
print("=" * 84)
print("%-32s %9s %11s %11s" % ("input window", "RANGING", "BEAR_TREND", "BULL_TREND"))
for key, label, fn in CASES:
    s = run(key, fn); res[key] = s
    c = Counter(s.values()); tot = len(s)
    print("%-32s %8.1f%% %10.1f%% %10.1f%%"
          % (label, c["RANGING"]/tot*100, c["BEAR_TREND"]/tot*100, c["BULL_TREND"]/tot*100))

base = res["50"]                      # live is the reference
print("\n  agreement vs LIVE (50-bar):")
for key, label, _ in CASES:
    if key == "50": continue
    common = sorted(set(base) & set(res[key]))
    a = sum(1 for t in common if base[t] == res[key][t])
    print("    %-32s %.1f%%" % (label, a/len(common)*100))

print("\n" + "=" * 84)
print("B. PROPOSED FIX — rolling 20-bar VWAP (window-invariant)")
print("=" * 84)
fixed = {}
print("%-32s %9s %11s %11s" % ("input window", "RANGING", "BEAR_TREND", "BULL_TREND"))
for key, label in [("expanding","expanding  (backtest)"), ("50","50 bars    (LIVE)"), ("35","35 bars")]:
    s = run(key, coin_trend_rolling_vwap); fixed[key] = s
    c = Counter(s.values()); tot = len(s)
    print("%-32s %8.1f%% %10.1f%% %10.1f%%"
          % (label, c["RANGING"]/tot*100, c["BEAR_TREND"]/tot*100, c["BULL_TREND"]/tot*100))
common = sorted(set(fixed["expanding"]) & set(fixed["50"]))
a = sum(1 for t in common if fixed["expanding"][t] == fixed["50"][t])
print("\n  backtest vs live agreement AFTER fix: %.1f%%   (before: %.1f%%)"
      % (a/len(common)*100,
         sum(1 for t in sorted(set(res['expanding']) & set(base))
             if res['expanding'][t] == base[t]) / len(sorted(set(res['expanding']) & set(base))) * 100))

print("\n" + "=" * 84)
print("C. HOURS EACH STRATEGY MAY TRADE UNDER THE NEW MATRIX")
print("=" * 84)
PERM = {"BULL_TREND":["CSM"], "BEAR_TREND":["CSM","LIQ","VRP"],
        "RANGING":["CSM"], "OVERHEATED":[], "OVERSOLD":[]}
for tag, s in [("current code / backtest labels", res["expanding"]),
               ("current code / LIVE labels",     res["50"]),
               ("after fix  / LIVE labels",       fixed["50"])]:
    c = Counter(s.values()); tot = len(s)
    liq = sum(v for k,v in c.items() if "LIQ" in PERM.get(k,[]))
    idle= sum(v for k,v in c.items() if not PERM.get(k,[]))
    print("  %-32s LIQ+VRP active %5.1f%% of hours | idle %4.1f%%"
          % (tag, liq/tot*100, idle/tot*100))
