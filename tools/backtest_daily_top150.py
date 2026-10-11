"""Fast causal replay for EMA100_TREND and DONCHIAN_55_20 across top-N data."""
import argparse
import glob
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from modules.daily_trend import features

FEE = 0.0008
STOP_ATR = 3.0


def replay(path: str):
    symbol = os.path.basename(path).split("_1h_")[0]
    hourly = pd.read_csv(path, parse_dates=["open_time"]).set_index("open_time").sort_index()
    hourly.index = pd.to_datetime(hourly.index, utc=True)
    daily = features(hourly.resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna())
    if len(daily) < 102 or not daily.ready.iloc[-1]:
        return symbol, [], "insufficient warmup"

    day_rows = {day: group for day, group in hourly.groupby(hourly.index.normalize())}
    trades = {"EMA100_TREND": [], "DONCHIAN_55_20": []}
    for strategy, target_col in (("EMA100_TREND", "ema_target"),
                                 ("DONCHIAN_55_20", "donchian_target")):
        active = None
        for i in range(1, len(daily)):
            decision_day = daily.index[i]
            current = daily.iloc[i]
            previous = daily.iloc[i - 1]
            if not current.ready or not previous.ready:
                continue
            side = int(current[target_col])
            prev_side = int(previous[target_col])
            entry_day = decision_day + pd.Timedelta(days=1)

            if active is None and side != 0 and side != prev_side and entry_day in day_rows:
                first = day_rows[entry_day].iloc[0]
                entry = float(first.close)
                atr = float(current.atr)
                stop = entry - side * STOP_ATR * atr
                if entry > 0 and atr > 0 and stop > 0:
                    active = dict(symbol=symbol, strategy=strategy,
                                  direction="LONG" if side == 1 else "SHORT",
                                  entry_price=entry, sl_price=stop,
                                  entry_time=first.name, signal_day=decision_day,
                                  atr=atr)

            if active is None:
                continue
            active_side = 1 if active["direction"] == "LONG" else -1
            for day, bars in day_rows.items():
                if day < entry_day or day < active["entry_time"].normalize():
                    continue
                if day > entry_day and day > decision_day:
                    bar = daily.loc[day] if day in daily.index else None
                    if bar is not None and bool(bar.ready):
                        target = int(bar[target_col])
                        if target != active_side:
                            last = bars.iloc[0]
                            px = float(last.close)
                            _close_trade(active, px, last.name, strategy, trades)
                            active = None
                            break
                        trail = (float(bar.high) - STOP_ATR * float(bar.atr)
                                 if active_side == 1 else
                                 float(bar.low) + STOP_ATR * float(bar.atr))
                        active["sl_price"] = (max(active["sl_price"], trail)
                                              if active_side == 1 else
                                              min(active["sl_price"], trail))
                for _, bar in bars.iterrows():
                    if bar.name <= active["entry_time"]:
                        continue
                    hit = (float(bar.low) <= active["sl_price"] if active_side == 1
                           else float(bar.high) >= active["sl_price"])
                    if hit:
                        _close_trade(active, float(active["sl_price"]), bar.name,
                                     strategy, trades, "TRAIL_STOP")
                        active = None
                        break
                if active is None:
                    break
        if active is not None:
            last = hourly.iloc[-1]
            _close_trade(active, float(last.close), last.name, strategy, trades,
                         "END_OF_DATA")
    return symbol, trades, "ok"


def _close_trade(active, price, timestamp, strategy, trades, reason="CONSENSUS_EXIT"):
    entry = float(active["entry_price"])
    pnl = ((price - entry) / entry if active["direction"] == "LONG"
           else (entry - price) / entry)
    row = dict(active, exit_price=price, exit_time=timestamp,
               exit_reason=reason, pnl_pct=pnl, net_pct=pnl - FEE)
    trades[strategy].append(row)


def report(rows):
    for strategy in ("EMA100_TREND", "DONCHIAN_55_20"):
        trades = [t for _, ts, _ in rows
                  if isinstance(ts, dict)
                  for t in ts[strategy]]
        wins = [t["net_pct"] for t in trades if t["net_pct"] > 0]
        losses = [t["net_pct"] for t in trades if t["net_pct"] <= 0]
        gross_w = sum(wins)
        gross_l = -sum(losses)
        pf = gross_w / gross_l if gross_l else float("inf")
        expectancy = float(np.mean([t["net_pct"] for t in trades])) if trades else 0.0
        print(f"{strategy}: trades={len(trades)} win={len(wins)/len(trades)*100 if trades else 0:.1f}% "
              f"expectancy={expectancy*100:.3f}% PF={pf:.2f} sum={sum(t['net_pct'] for t in trades)*100:.2f}%")
        print("  symbols:", ", ".join(sorted({t["symbol"] for t in trades}) or ["none"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--glob", default="data/*_1h_180d.csv")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    paths = sorted(glob.glob(args.glob))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(replay, paths))
    print(f"DATASETS: {len(paths)} | VALID: {sum(status == 'ok' for _, _, status in rows)}")
    report(rows)
    print("SKIPPED:", sum(status != "ok" for _, _, status in rows))


if __name__ == "__main__":
    main()
