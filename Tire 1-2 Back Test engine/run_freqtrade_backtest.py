"""
run_freqtrade_backtest.py
Lean backtester for 5 strategies in Tire 2 mode (1m intrabar SL/TP).

Runs each strategy one by one across all symbols with 1m data,
using the same position management, slippage, and fee model as
the main backtest_optimizer.

Usage:
  python run_freqtrade_backtest.py --days 10 --last-days 7
  python run_freqtrade_backtest.py --days 40 --last-days 30
  python run_freqtrade_backtest.py --days 40 --last-days 30 --symbols BTCUSDT,ETHUSDT
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import sys
import time
import argparse
import glob

import numpy as np
import pandas as pd

from backtest_optimizer import (
    load_data, run_backtest_1m, expectancy,
    ROUND_TRIP_FEE, SLIPPAGE_PCT, INITIAL_CAPITAL, LEVERAGE,
)
from modules.strategies.cross_sectional_momentum import CrossSectionalMomentum
from modules.strategies.freqtrade_port_nasos  import NASOSv4Port
from modules.strategies.freqtrade_port_elliot import ElliotV8Port

STRATEGIES = [
    ("CSM",        CrossSectionalMomentum),
    ("NASOS_V4",   NASOSv4Port),
    ("ELLIOT_V8",  ElliotV8Port),
]

MOCK_REGIME = {
    "CSM": "BULL_TREND",
    "NASOS_V4": "BULL_TREND",
    "ELLIOT_V8": "BULL_TREND",
}

TOP_20 = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "SUIUSDT", "TRXUSDT", "NEARUSDT", "LTCUSDT", "HYPEUSDT",
    "APTUSDT", "UNIUSDT", "ICPUSDT", "INJUSDT", "ARBUSDT",
]

TOP_50 = TOP_20 + [
    "OPUSDT", "FILUSDT", "ATOMUSDT", "FETUSDT", "ENAUSDT",
    "TAOUSDT", "ONDOUSDT", "LDOUSDT", "XLMUSDT", "HBARUSDT",
    "AAVEUSDT", "TRUMPUSDT", "XMRUSDT", "BCHUSDT", "FARTCOINUSDT",
    "PENGUUSDT", "1000PEPEUSDT", "1000BONKUSDT", "ETHFIUSDT", "MOVEUSDT",
    "CRVUSDT", "WLDUSDT", "ZECUSDT", "LITUSDT", "JTOUSDT",
    "PUMPUSDT", "WALUSDT", "RONINUSDT", "XAIUSDT", "PORTALUSDT",
]


def discover_symbols(days):
    pattern = os.path.join("data", f"*_1m_{days}d.csv")
    files = glob.glob(pattern)
    symbols = []
    for f in files:
        name = os.path.basename(f)
        sym = name.replace(f"_1m_{days}d.csv", "")
        symbols.append(sym)
    return sorted(symbols)


def run_strategy(sid, cls, symbols, days, last_days):
    print(f"\n{'='*80}")
    print(f"  {sid} — {len(symbols)} symbols — {days}d data, scoring last {last_days}d")
    print(f"{'='*80}")

    all_trades = []
    t0 = time.time()

    for idx, sym in enumerate(symbols):
        trades = run_backtest_1m(
            cls, sym,
            mock_regime_name=MOCK_REGIME[sid],
            days=days,
        )
        if trades:
            all_trades.extend(trades)
            print(f"  [{idx+1:3d}/{len(symbols)}] {sym:<16} {len(trades):>3} trades", flush=True)
        elif (idx + 1) % 20 == 0:
            print(f"  [{idx+1:3d}/{len(symbols)}] ... scanning ...", flush=True)

    elapsed = time.time() - t0

    # Trim to last N days
    if last_days and all_trades:
        latest = max(t["exit_time"] for t in all_trades)
        cutoff = latest - pd.Timedelta(days=last_days)
        before = len(all_trades)
        all_trades = [t for t in all_trades if t["entry_time"] >= cutoff]
        print(f"\n  Scoring window: last {last_days}d — {before} total -> {len(all_trades)} in window")

    e = expectancy(all_trades)
    if not e:
        print(f"\n  {sid}: 0 trades in scoring window ({elapsed:.0f}s)")
        return sid, 0, None

    print(f"\n  {sid} RESULTS ({elapsed:.0f}s):")
    print(f"  {'-'*60}")
    print(f"  Trades              : {e['n']}")
    print(f"  Win rate            : {e['win']:.1f}%")
    print(f"  Avg win / avg loss  : {e['avg_w']:+.2f}% / {e['avg_l']:+.2f}%  (R:R {e['rr']:.2f})")
    print(f"  Expectancy per trade: {e['exp']:+.3f}%  [net of {ROUND_TRIP_FEE*100:.2f}% fees + {SLIPPAGE_PCT*100:.3f}% slip]")
    print(f"  Profit factor       : {e['pf']:.2f}")
    print(f"  Sum of net returns  : {e['sum']:+.1f}%")
    print(f"  Median return       : {e['median']:+.3f}%")
    print(f"  Best / Worst trade  : {e['best']:+.2f}% / {e['worst']:+.2f}%")
    print(f"  Max losing streak   : {e['streak']}")
    print(f"  Median hold time    : {e['hours']:.1f}h")
    print(f"  Edge                : {'YES' if e['exp'] > 0 else 'NO'}")

    # Top symbols
    by_sym = {}
    for t in all_trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    sym_stats = []
    for sym, ts in by_sym.items():
        se = expectancy(ts)
        if se:
            sym_stats.append((sym, se))
    sym_stats.sort(key=lambda x: -x[1]["sum"])

    if sym_stats:
        print(f"\n  Top 5 symbols:")
        for sym, se in sym_stats[:5]:
            print(f"    {sym:<16} {se['n']:>3} trades  E[net]={se['exp']:+.3f}%  sum={se['sum']:+.1f}%")
        if len(sym_stats) > 5:
            print(f"  Bottom 3 symbols:")
            for sym, se in sym_stats[-3:]:
                print(f"    {sym:<16} {se['n']:>3} trades  E[net]={se['exp']:+.3f}%  sum={se['sum']:+.1f}%")

    return sid, e['n'], e


def main():
    ap = argparse.ArgumentParser(description="Tire 2 backtester (1m intrabar SL/TP)")
    ap.add_argument("--days", type=int, default=10,
                    help="CSV data set to load (10, 40, or 90)")
    ap.add_argument("--last-days", type=int, default=None,
                    help="Score only trades entered in the last N days")
    ap.add_argument("--strategy", default=None,
                    help="Run one strategy only (CSM, NASOS_V4, ELLIOT_V8)")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated symbol list to restrict backtest to")
    ap.add_argument("--top", type=int, default=None,
                    help="Use only top N coins (20 or 50)")
    args = ap.parse_args()

    all_symbols = discover_symbols(args.days)
    if not all_symbols:
        sys.exit(f"No *_1m_{args.days}d.csv files found in data/")

    if args.symbols:
        wanted = set(s.strip().upper() for s in args.symbols.split(","))
        symbols = [s for s in all_symbols if s in wanted]
        missing = wanted - set(symbols)
        if missing:
            print(f"Warning: {len(missing)} symbols have no 1m data: {','.join(sorted(missing))}")
        print(f"Filtering to {len(symbols)} specified symbols (of {len(wanted)} requested)")
    elif args.top == 20:
        symbols = [s for s in TOP_20 if s in all_symbols]
        print(f"Filtering to top 20 coins: {len(symbols)} available")
    elif args.top == 50:
        symbols = [s for s in TOP_50 if s in all_symbols]
        print(f"Filtering to top 50 coins: {len(symbols)} available")
    elif args.top:
        symbols = all_symbols[:args.top]
    else:
        symbols = all_symbols

    last_days = args.last_days or args.days
    print(f"Tire 2 Backtest — 1m Intrabar SL/TP")
    print(f"Data: {args.days}d CSVs | Scoring: last {last_days}d | Symbols: {len(symbols)}")
    print(f"Fees: {ROUND_TRIP_FEE*100:.2f}% RT | Slippage: {SLIPPAGE_PCT*100:.3f}%/side")

    strats = STRATEGIES
    if args.strategy:
        strats = [(s, c) for s, c in STRATEGIES if s == args.strategy.upper()]
        if not strats:
            sys.exit(f"Unknown strategy: {args.strategy}")

    results = []
    for sid, cls in strats:
        r = run_strategy(sid, cls, symbols, args.days, last_days)
        results.append(r)

    # Summary
    print(f"\n{'='*80}")
    print(f"  SUMMARY — {last_days}d scoring window")
    print(f"{'='*80}")
    print(f"  {'STRATEGY':<12} {'TRADES':>7} {'WIN%':>7} {'E[net]':>9} {'PF':>6} {'SUM':>10}  EDGE")
    print(f"  {'-'*12} {'-'*7} {'-'*7} {'-'*9} {'-'*6} {'-'*10}  {'-'*4}")

    for sid, n, e in results:
        if e:
            pf_str = f"{e['pf']:.2f}" if e['pf'] != float('inf') else "inf"
            edge = "YES" if e['exp'] > 0 else "no"
            print(f"  {sid:<12} {e['n']:>7} {e['win']:>6.1f}% {e['exp']:>8.3f}% {pf_str:>6} {e['sum']:>+9.1f}%  {edge}")
        else:
            print(f"  {sid:<12} {0:>7} {'--':>7} {'--':>9} {'--':>6} {'--':>10}  --")


if __name__ == "__main__":
    main()
