"""
backtest_freqtrade_ports.py
Backtester for the 5 ported Freqtrade strategies (modules/strategies/freqtrade_port_*.py).

These strategies use the BaseStrategy scan()/manage() interface but need 1m data
(which they resample to 5m internally). The main backtest_optimizer.py passes
df_1m=None, so they never fire. This script feeds them cached 5m data with a
'timestamp' column — the resample is a no-op on already-5m-aligned data.

Usage:
    python backtest_freqtrade_ports.py                        # all 5, 30 days
    python backtest_freqtrade_ports.py --days 90              # 90 days
    python backtest_freqtrade_ports.py --strategies NASOS_V4  # one strategy
    python backtest_freqtrade_ports.py --symbols BTCUSDT,ETHUSDT
"""

import os
import sys
import time
import argparse
import logging
import warnings
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.WARNING)

# ── Imports from the project ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.strategies.freqtrade_port_nasos  import NASOSv4Port
from modules.strategies.freqtrade_port_ichi   import IchiV1Port

# ── Config ──────────────────────────────────────────────────────────────────
DATA_DIR        = os.path.join("data", "freqtrade_bt")
INITIAL_CAPITAL = 1000.0
LEVERAGE        = 5
TAKER_FEE       = 0.0004
ROUND_TRIP_FEE  = TAKER_FEE * 2
WARMUP_BARS     = 250        # enough for 200-period EMA on 5m

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


def fetch_top_futures_symbols(n=100):
    """Fetch top N Binance USDT-M futures symbols by 24h volume."""
    try:
        resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15)
        resp.raise_for_status()
        tickers = resp.json()
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        symbols = [t["symbol"] for t in usdt[:n]]
        print(f"  Fetched top {len(symbols)} USDT futures by 24h volume")
        return symbols
    except Exception as e:
        print(f"  ! Could not fetch futures symbols: {e}")
        return DEFAULT_SYMBOLS

STRATEGY_CLASSES = {
    "NASOS_V4":   NASOSv4Port,
    "ICHI_V1":    IchiV1Port,
}

TIER = {k: "Freqtrade" for k in STRATEGY_CLASSES}

MOCK_REGIME = {k: "BULL_TREND" for k in STRATEGY_CLASSES}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_5m(symbol, days):
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = os.path.join(DATA_DIR, f"{symbol}_5m_{days}d.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["open_time"])
        print(f"  {symbol} 5m: {len(df)} bars (cached)")
        return df

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows, cur = [], start_ms
    print(f"  Fetching {symbol} 5m from Binance...")
    while cur < end_ms:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "5m",
                        "startTime": cur, "endTime": end_ms, "limit": 1500},
                timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"  ! {symbol}: {e}")
            break
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        if len(batch) < 1500:
            break
        time.sleep(0.3)

    if not rows:
        return None
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "qv", "trades", "tbb", "tbq", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df[["open_time", "open", "high", "low", "close", "volume"]]
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    df.to_csv(cache, index=False)
    print(f"  {symbol} 5m: {len(df)} bars (fetched)")
    return df


def fetch_1h(symbol, days):
    cache = os.path.join(DATA_DIR, f"{symbol}_1h_{days}d.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["open_time"])
        return df

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows, cur = [], start_ms
    print(f"  Fetching {symbol} 1h from Binance...")
    while cur < end_ms:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1h",
                        "startTime": cur, "endTime": end_ms, "limit": 1500},
                timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            break
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        if len(batch) < 1500:
            break
        time.sleep(0.3)

    if not rows:
        return None
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "qv", "trades", "tbb", "tbq", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df[["open_time", "open", "high", "low", "close", "volume"]]
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _intrabar_exit(pos, bar):
    """
    Check if price touched stop or target INSIDE this bar using high/low.

    The old approach only checked the bar CLOSE, so a position whose stop was
    breached mid-bar but recovered by close was scored as still open. Live
    exchanges hold a real STOP_MARKET order that fires the moment price trades
    through. Tire 2 testing showed this was the #1 source of backtest
    optimism: SL_HIT was 43.5% of live exits but only 24.2% of close-only
    backtested ones.

    Tie-break: when a bar touches BOTH stop and target, the stop wins
    (pessimistic assumption).
    """
    sl = pos.get("sl_price")
    tp = pos.get("tp_price")
    try:
        hi, lo = float(bar["high"]), float(bar["low"])
    except Exception:
        return None

    if pos.get("direction") == "LONG":
        if sl is not None and lo <= float(sl):
            return float(sl), "STOP"
        if tp is not None and hi >= float(tp):
            return float(tp), "TARGET"
    else:
        if sl is not None and hi >= float(sl):
            return float(sl), "STOP"
        if tp is not None and lo <= float(tp):
            return float(tp), "TARGET"
    return None


def run_backtest(strategy_class, symbol, df_5m, df_1h, regime_name="BULL_TREND"):
    """
    Walk through 5m bars, calling scan() with a sliding window of data.
    The ported strategies expect df_1m with a 'timestamp' column;
    we pass 5m data renamed to 'timestamp' — resample is a no-op.

    Uses intrabar exit checking (bar high/low) for SL/TP instead of
    close-only, matching live exchange behavior.
    """
    if df_5m is None or len(df_5m) < WARMUP_BARS + 10:
        return []

    df = df_5m.copy()
    df['timestamp'] = df['open_time']

    df_1h_prep = None
    if df_1h is not None and not df_1h.empty:
        df_1h_prep = df_1h.copy()
        df_1h_prep['timestamp'] = df_1h_prep['open_time']

    strategy = strategy_class()
    sid = strategy_class.STRATEGY_ID

    trades = []
    active = None

    step = 12  # ~1 hour on 5m data
    WINDOW_SIZE = 1500  # fixed sliding window

    for i in range(WARMUP_BARS, len(df), step):
        start = max(0, i - WINDOW_SIZE + 1)
        window = df.iloc[start:i + 1].copy()
        now = window['timestamp'].iloc[-1]

        cur_1h = None
        if df_1h_prep is not None:
            cur_1h = df_1h_prep[df_1h_prep['timestamp'] <= now]

        regime = {"regime": regime_name, "funding": 0.0}

        # ── Manage open position ────────────────────────────────────────────
        if active:
            entry_price = float(active['entry_price'])
            direction = active.get('direction', 'LONG')

            # Check SL/TP against bar high/low (intrabar exit)
            hit = _intrabar_exit(active, window.iloc[-1])
            if hit:
                px, kind = hit
                reason = "TP_HIT" if kind == "TARGET" else "SL_HIT"
                pnl = (px - entry_price) / entry_price if direction == "LONG" \
                    else (entry_price - px) / entry_price
                active.update(exit_price=px, exit_time=now,
                              exit_reason=reason, pnl_pct=pnl)
                trades.append(active)
                active = None
                continue

            # Check strategy's manage() sell signal
            try:
                exit_sig = strategy.manage(active, window, session_pnl=0)
                if exit_sig and exit_sig.get("exit"):
                    px = float(exit_sig["exit_price"])
                    pnl = (px - entry_price) / entry_price if direction == "LONG" \
                        else (entry_price - px) / entry_price
                    active.update(exit_price=px, exit_time=now,
                                  exit_reason=exit_sig["exit_reason"], pnl_pct=pnl)
                    trades.append(active)
                    active = None
            except Exception:
                pass
            continue

        # ── Scan for entry ──────────────────────────────────────────────────
        try:
            signal = strategy.scan(symbol, window, window, cur_1h, regime)
        except Exception as exc:
            continue

        if signal and not signal.get("near_miss"):
            signal["entry_time"] = now
            signal["regime_at_entry"] = regime_name
            active = signal

    # Force-close open position
    if active:
        px = float(df['close'].iloc[-1])
        entry = float(active['entry_price'])
        pnl = (px - entry) / entry if active.get('direction') == 'LONG' \
            else (entry - px) / entry
        active.update(exit_price=px, exit_time=df['timestamp'].iloc[-1],
                      exit_reason="END_OF_DATA", pnl_pct=pnl)
        trades.append(active)

    # Attach metadata
    for t in trades:
        t["strategy"] = sid
        t["symbol"] = symbol
        t["net_pct"] = t["pnl_pct"] - ROUND_TRIP_FEE
        t["hours"] = (t["exit_time"] - t["entry_time"]).total_seconds() / 3600

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# METRICS
# ═══════════════════════════════════════════════════════════════════════════════

def expectancy(trades):
    if not trades:
        return None
    n = np.array([t["net_pct"] for t in trades])
    w, l = n[n > 0], n[n <= 0]
    srt = np.sort(n)[::-1]

    streak = mx = 0
    for t in sorted(trades, key=lambda x: x["entry_time"]):
        streak = streak + 1 if t["net_pct"] <= 0 else 0
        mx = max(mx, streak)

    return {
        "n": len(n),
        "win": len(w) / len(n) * 100,
        "avg_w": w.mean() * 100 if len(w) else 0.0,
        "avg_l": l.mean() * 100 if len(l) else 0.0,
        "rr": abs(w.mean() / l.mean()) if len(w) and len(l) and l.mean() else 0.0,
        "exp": n.mean() * 100,
        "median": np.median(n) * 100,
        "sum": n.sum() * 100,
        "pf": w.sum() / abs(l.sum()) if len(l) and l.sum() else float("inf"),
        "worst": n.min() * 100,
        "best": n.max() * 100,
        "streak": mx,
        "hours": float(np.median([t["hours"] for t in trades])),
    }


def simulate_portfolio(trades, capital=INITIAL_CAPITAL, leverage=LEVERAGE,
                       label="", verbose=True):
    if not trades:
        return None

    ts = sorted(trades, key=lambda t: t["entry_time"])
    eq = peak = capital
    mdd = 0.0
    curve = []

    for t in ts:
        margin = eq * 0.15  # 15% per trade
        notional = margin * leverage
        pnl_usd = max(notional * (t["pnl_pct"] - ROUND_TRIP_FEE), -margin)
        eq += pnl_usd
        if eq <= 0:
            eq = 0
            break
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
        curve.append((t["exit_time"], eq))

    sharpe = 0.0
    if len(curve) > 2:
        s = pd.Series([e for _, e in curve],
                      index=pd.to_datetime([c for c, _ in curve]))
        r = s.resample("1D").last().ffill().pct_change().dropna()
        if len(r) > 1 and r.std() > 0:
            sharpe = float(r.mean() / r.std() * np.sqrt(365))

    ret = (eq - capital) / capital * 100
    res = {"label": label, "signals": len(ts), "final": eq,
           "return_pct": ret, "max_dd": mdd * 100, "sharpe": sharpe}

    if verbose:
        print(f"  {label:<32}{len(ts):>6}{ret:>11.2f}%{mdd*100:>9.1f}%"
              f"{sharpe:>8.2f}  -> ${eq:.2f}")
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def print_report(by_strat, all_strategies, capital=INITIAL_CAPITAL, leverage=LEVERAGE):
    print("\n" + "=" * 100)
    print("PER-TRADE EXPECTANCY — net of fees")
    print("=" * 100)
    print(f"{'STRAT':<12}{'N':>6}{'WIN%':>7}{'AVG W':>8}{'AVG L':>8}{'R:R':>6}"
          f"{'E[net]':>9}{'PF':>6}{'SUM':>10}{'HOLD':>7}"
          f"{'WORST':>9}{'BEST':>9}{'STREAK':>7}  EDGE")
    print("-" * 100)

    rows = []
    for s in all_strategies:
        e = expectancy(by_strat.get(s, []))
        if e:
            rows.append((s, e))

    rows.sort(key=lambda x: -x[1]["exp"])

    for s, e in rows:
        edge = "YES" if e["exp"] > 0 else "no"
        print(f"{s:<12}{e['n']:>6}{e['win']:>6.1f}%{e['avg_w']:>7.2f}%"
              f"{e['avg_l']:>7.2f}%{e['rr']:>6.2f}{e['exp']:>8.3f}%"
              f"{e['pf']:>6.2f}{e['sum']:>9.1f}%{e['hours']:>6.1f}h"
              f"{e['worst']:>8.2f}%{e['best']:>8.2f}%{e['streak']:>7}  {edge}")

    # Exit reasons
    print("\n" + "=" * 100)
    print("EXIT BEHAVIOUR")
    print("=" * 100)
    for s, _ in rows:
        c = Counter(t["exit_reason"] for t in by_strat[s])
        tot = len(by_strat[s])
        parts = ", ".join(f"{k}={v} ({v/tot*100:.0f}%)" for k, v in c.most_common())
        print(f"  {s:<12}{parts}")

    # Per-symbol breakdown
    print("\n" + "=" * 100)
    print("PER-SYMBOL BREAKDOWN")
    print("=" * 100)
    print(f"{'STRAT':<12}{'SYMBOL':<12}{'N':>6}{'WIN%':>7}{'E[net]':>9}{'SUM':>10}")
    print("-" * 100)
    for s, _ in rows:
        by_sym = defaultdict(list)
        for t in by_strat[s]:
            by_sym[t["symbol"]].append(t)
        for sym in sorted(by_sym):
            se = expectancy(by_sym[sym])
            if se:
                print(f"{s:<12}{sym:<12}{se['n']:>6}{se['win']:>6.1f}%"
                      f"{se['exp']:>8.3f}%{se['sum']:>9.1f}%")

    # Portfolio simulation
    print("\n" + "=" * 100)
    print(f"PORTFOLIO SIMULATION — ${capital:.0f} capital, {leverage}x leverage")
    print("=" * 100)
    print(f"  {'CONFIG':<32}{'TAKEN':>6}{'RETURN':>11}{'MAXDD':>9}{'SHARPE':>8}")
    print("  " + "-" * 80)

    all_trades = []
    for s, _ in rows:
        simulate_portfolio(by_strat[s], capital=capital, leverage=leverage, label=s)
        all_trades.extend(by_strat[s])

    if len(rows) > 1:
        print("  " + "-" * 80)
        simulate_portfolio(all_trades, capital=capital, leverage=leverage, label="ALL COMBINED")

    winners = [s for s, e in rows if e["exp"] > 0]
    if winners and len(winners) < len(rows):
        w_trades = [t for t in all_trades if t["strategy"] in winners]
        simulate_portfolio(w_trades, capital=capital, leverage=leverage,
                           label=f"WINNERS ONLY ({'+'.join(winners)})")

    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Backtest ported Freqtrade strategies")
    ap.add_argument("--strategies", default=None,
                    help="Comma-separated IDs (NASOS_V4,ICHI_V1)")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated symbols (default: BTC,ETH,SOL,BNB,XRP)")
    ap.add_argument("--top", type=int, default=None,
                    help="Fetch top N futures by 24h volume (e.g. --top 50)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    ap.add_argument("--leverage", type=int, default=LEVERAGE)
    args = ap.parse_args()

    if args.top:
        symbols = fetch_top_futures_symbols(args.top)
    elif args.symbols and args.symbols.lower().startswith("top"):
        n = int(args.symbols[3:]) if len(args.symbols) > 3 else 100
        symbols = fetch_top_futures_symbols(n)
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = DEFAULT_SYMBOLS

    strategies = list(STRATEGY_CLASSES.keys())
    if args.strategies:
        strategies = [s.strip().upper() for s in args.strategies.split(",")]
        unknown = [s for s in strategies if s not in STRATEGY_CLASSES]
        if unknown:
            sys.exit(f"Unknown: {unknown}. Valid: {list(STRATEGY_CLASSES.keys())}")

    capital = args.capital
    leverage = args.leverage

    print("=" * 100)
    print(f"FREQTRADE PORT BACKTEST — {args.days} DAYS — {len(symbols)} SYMBOLS")
    print(f"Capital ${capital:.0f} | {leverage}x leverage | "
          f"fee {ROUND_TRIP_FEE*100:.2f}% RT on notional")
    print("=" * 100)

    # ── Fetch data ──────────────────────────────────────────────────────────
    print(f"\nLoading data for {len(symbols)} symbols...", flush=True)
    data_5m = {}
    data_1h = {}
    for idx, sym in enumerate(symbols, 1):
        print(f"  [{idx}/{len(symbols)}] {sym}...", end=" ", flush=True)
        df = fetch_5m(sym, args.days)
        if df is not None and len(df) > WARMUP_BARS + 50:
            data_5m[sym] = df
            data_1h[sym] = fetch_1h(sym, args.days)
            print(f"{len(df)} bars", flush=True)
        else:
            print("insufficient data, skipping", flush=True)

    if not data_5m:
        sys.exit("No data available.")

    # ── Run strategies ──────────────────────────────────────────────────────
    by_strat = defaultdict(list)

    for sid in strategies:
        cls = STRATEGY_CLASSES[sid]
        print(f"\nRunning {sid}...", flush=True)
        for sym in data_5m:
            trades = run_backtest(cls, sym, data_5m[sym], data_1h.get(sym))
            if trades:
                print(f"  {sym}: {len(trades)} trades", flush=True)
            by_strat[sid].extend(trades)
        print(f"  {sid} total: {len(by_strat[sid])} trades", flush=True)

    total = sum(len(v) for v in by_strat.values())
    print(f"\nTotal signals across all strategies: {total}")

    if total == 0:
        print("\nNo trades produced by any strategy.")
        print("These are dip-buying strategies — they need RSI_fast < 35 and price")
        print("below EMA offsets. The recent market may not have had deep enough dips.")
        sys.exit(0)

    # ── Report ──────────────────────────────────────────────────────────────
    print_report(by_strat, strategies, capital, leverage)

    print("\n" + "=" * 100)
    print("NOTES:")
    print("  - These strategies are LONG-only dip-buyers, designed for volatile markets")
    print("  - Entry requires RSI_fast < 35 + price below EMA offset — very selective")
    print("  - Results are in-sample; do not tune on them")
    print("  - ichiV1 uses Ichimoku cloud + trend fan — different entry logic")
    print("=" * 100)


if __name__ == "__main__":
    main()
