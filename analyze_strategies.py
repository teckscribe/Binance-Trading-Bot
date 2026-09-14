"""
analyze_strategies.py
Per-strategy performance analyzer for the cs (Crypto Scanner) bot.

Reads all accumulated strategy log files and produces a detailed
breakdown per strategy — win rate, avg P&L, best/worst trade,
exit reason distribution, avg hold time, regime performance.

Usage:
  ./venv/bin/python3 analyze_strategies.py            # all strategies, all dates
  ./venv/bin/python3 analyze_strategies.py --strategy TP
  ./venv/bin/python3 analyze_strategies.py --days 7   # last 7 days only
  ./venv/bin/python3 analyze_strategies.py --mode PAPER
"""

import os
import json
import argparse
from datetime import datetime, timezone, timedelta
from glob import glob

_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_DIR = os.path.join(_DIR, "logs", "strategies")


# Derived from the factory, not hardcoded.
#
# 2026-08-04: this list had drifted to the pre-2026 set
# ["TP","FF","DB","GRID","BBR"]. Because `--strategy ALL` iterates it and
# argparse validates `--strategy` against it, this tool reported NOTHING for
# CSM, WKD, VRP, LIQ, OIB and FF_V2 and rejected them as invalid arguments —
# i.e. the per-strategy performance report was blind to six of the seven
# strategies actually trading. Same drift existed in live_logger.py.
def _production_strategy_ids() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
        if ids:
            return ids
    except Exception:
        pass
    return ["CSM", "NASOS_V4"]


STRATEGIES = _production_strategy_ids()
# Retired: ORB 2026-04-20; LF 2026-05-25; BGD 2026-06-09; BKD 2026-06-29.
# Archived and no longer analysed by default: FF, DB, GRID, BBR.


# ─── Load trades ──────────────────────────────────────────────────────────────

def load_trades(
    strategies: list[str],
    days:       int  = 0,
    mode:       str  = "ALL",
) -> dict[str, list[dict]]:
    """
    Load all trade records from strategy daily NDJSON log files.
    Each line in the file is one JSON record (NDJSON format).
    Only EXIT records are loaded — SIGNAL records are skipped.
    """
    result = {s: [] for s in strategies}
    cutoff = None
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    for strat in strategies:
        strat_dir = os.path.join(STRATEGY_DIR, strat)
        if not os.path.exists(strat_dir):
            continue

        for fpath in sorted(glob(os.path.join(strat_dir, f"{strat}_*.json"))):
            fname = os.path.basename(fpath).replace(f"{strat}_", "").replace(".json", "")
            try:
                file_date = datetime.strptime(fname, "%Y-%m-%d").date()
            except ValueError:
                continue

            if cutoff and file_date < cutoff:
                continue

            try:
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Only load EXIT records for trade analysis
                        if record.get("type") != "EXIT":
                            continue
                        if mode == "ALL" or record.get("mode", "") == mode:
                            result[strat].append(record)
            except Exception as e:
                print(f"  WARNING: Could not read {fpath}: {e}")

    return result


# ─── Stats per strategy ───────────────────────────────────────────────────────

def compute_stats(trades: list[dict]) -> dict:
    """Compute full statistics for a list of trade records."""
    if not trades:
        return {"n": 0}

    pnls      = [t["pnl_pct_100"] for t in trades]
    wins      = [p for p in pnls if p >= 0]
    losses    = [p for p in pnls if p < 0]
    durations = [t.get("duration_min", 0) for t in trades]

    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    directions = {}
    for t in trades:
        d = t.get("direction", "?")
        directions[d] = directions.get(d, 0) + 1

    # Best and worst trade
    best  = max(trades, key=lambda t: t["pnl_pct_100"])
    worst = min(trades, key=lambda t: t["pnl_pct_100"])

    # Consecutive wins/losses
    max_consec_wins = max_consec_losses = 0
    cur_w = cur_l = 0
    for p in pnls:
        if p >= 0:
            cur_w += 1; cur_l = 0
            max_consec_wins = max(max_consec_wins, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_consec_losses = max(max_consec_losses, cur_l)

    return {
        "n":               len(trades),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(len(wins) / len(trades) * 100, 1),
        "total_pnl":       round(sum(pnls), 3),
        "avg_pnl":         round(sum(pnls) / len(pnls), 3),
        "avg_win":         round(sum(wins) / len(wins), 3)   if wins   else 0,
        "avg_loss":        round(sum(losses) / len(losses), 3) if losses else 0,
        "best_pnl":        round(best["pnl_pct_100"], 3),
        "best_symbol":     best["symbol"],
        "worst_pnl":       round(worst["pnl_pct_100"], 3),
        "worst_symbol":    worst["symbol"],
        "avg_duration":    round(sum(durations) / len(durations), 1),
        "max_consec_wins": max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "exit_reasons":    exit_reasons,
        "directions":      directions,
        "profit_factor":   round(
            sum(wins) / abs(sum(losses)), 2
        ) if losses and sum(losses) != 0 else float("inf"),
    }


# ─── Print report ─────────────────────────────────────────────────────────────

def print_report(all_trades: dict[str, list[dict]], mode: str) -> None:
    sep  = "=" * 62
    sep2 = "-" * 62

    print(f"\n{sep}")
    print(f"  cs — STRATEGY PERFORMANCE REPORT")
    print(f"  Mode: {mode} | Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{sep}\n")

    total_all = sum(len(v) for v in all_trades.values())
    if total_all == 0:
        print("  No trades found. Run the bot for a few days then re-analyze.")
        return

    strat_meta = {
        # Production
        "CSM":        "🟢 Cross-Sectional Momentum",
        "NASOS_V4":   "🟡 NASOS V4 Dip-Buy",
        # Archived (labels kept so old logs still render nicely)
        "SMA_OFFSET": "⚪ SMA Offset Dip-Buy (archived)",
        "LIQ":   "⚪ Liquidation Cascades (archived)",
        "VRP":   "⚪ Volatility Risk Premium (archived)",
        "EI3_V2":"⚪ EI3 V2 Cofi (archived)",
        "TP":    "⚪ Trend Pullback (archived)",
        "FF_V2": "⚪ Funding Fade V2 (archived)",
        "FF":    "⚪ Funding Rate Fade (archived)",
        "DB":    "⚪ Donchian Breakout (archived)",
        "GRID":  "⚪ Grid Trading (archived)",
        "BBR":   "⚪ Bollinger Band Reversion (archived)",
    }

    for strat, trades in all_trades.items():
        print(f"  {strat_meta.get(strat, strat)}")
        print(f"  {sep2}")

        if not trades:
            print("    No trades recorded yet.\n")
            continue

        s = compute_stats(trades)

        # Win/loss bar
        bar_len = 30
        w_bars  = round(s["win_rate"] / 100 * bar_len)
        bar     = "█" * w_bars + "░" * (bar_len - w_bars)

        print(f"    Trades      : {s['n']}  ({s['wins']}W / {s['losses']}L)")
        print(f"    Win rate    : {s['win_rate']:.1f}%  [{bar}]")
        print(f"    Total P&L   : {s['total_pnl']:+.3f}%")
        print(f"    Avg P&L     : {s['avg_pnl']:+.3f}% per trade")
        print(f"    Avg win     : {s['avg_win']:+.3f}%  |  Avg loss : {s['avg_loss']:+.3f}%")
        print(f"    Profit factor: {s['profit_factor']:.2f}  (>1.5 = good)")
        print(f"    Best trade  : {s['best_pnl']:+.3f}%  [{s['best_symbol']}]")
        print(f"    Worst trade : {s['worst_pnl']:+.3f}%  [{s['worst_symbol']}]")
        print(f"    Avg hold    : {s['avg_duration']:.0f} min")
        print(f"    Max consec  : {s['max_consec_wins']} wins / {s['max_consec_losses']} losses")

        # Exit reasons
        print(f"    Exit reasons:")
        for reason, count in sorted(s["exit_reasons"].items(),
                                     key=lambda x: -x[1]):
            pct = count / s["n"] * 100
            print(f"      {reason:<22} {count:3d}  ({pct:.0f}%)")

        # Direction split
        if len(s["directions"]) > 1:
            print(f"    Direction   : ", end="")
            for d, c in s["directions"].items():
                print(f"{d} {c}  ", end="")
            print()

        print()

    # ── Cross-strategy comparison ──────────────────────────────────────────────
    print(f"  {sep2}")
    print(f"  CROSS-STRATEGY COMPARISON")
    print(f"  {sep2}")
    print(f"  {'Strategy':<8} {'Trades':>6} {'WR%':>6} {'AvgPnL':>8} {'PF':>6} {'TotalPnL':>10}")
    print(f"  {'-'*8} {'-'*6} {'-'*6} {'-'*8} {'-'*6} {'-'*10}")

    ranked = []
    for strat, trades in all_trades.items():
        if not trades:
            continue
        s = compute_stats(trades)
        ranked.append((strat, s))

    ranked.sort(key=lambda x: x[1]["total_pnl"], reverse=True)

    for strat, s in ranked:
        pf_str = f"{s['profit_factor']:.2f}" if s['profit_factor'] != float('inf') else "∞"
        print(
            f"  {strat:<8} {s['n']:>6} {s['win_rate']:>5.1f}% "
            f"{s['avg_pnl']:>+8.3f}% {pf_str:>6} {s['total_pnl']:>+9.3f}%"
        )

    print(f"\n{sep}\n")

    # ── Tuning recommendations ────────────────────────────────────────────────
    print("  TUNING SIGNALS")
    print(f"  {sep2}")
    for strat, trades in all_trades.items():
        if not trades:
            continue
        s = compute_stats(trades)
        issues = []

        if s["n"] < 10:
            issues.append(f"Only {s['n']} trades — need more data before tuning")
        else:
            if s["win_rate"] < 40:
                issues.append("Win rate < 40% — entry conditions too loose or wrong regime")
            if s["profit_factor"] < 1.0:
                issues.append("Profit factor < 1.0 — losses exceed wins in size")
            sl_exits = s["exit_reasons"].get("SL_HIT", 0)
            tp_exits = s["exit_reasons"].get("TP_HIT", 0)
            if sl_exits > 0 and tp_exits == 0:
                issues.append("0 TP hits — TP target may be too far, consider tightening")
            if tp_exits > sl_exits * 3:
                issues.append("Very high TP rate — SL may be too tight, widening could help")
            hs_exits = s["exit_reasons"].get("HARD_STOP", 0)
            if hs_exits > s["n"] * 0.3:
                issues.append(f"Hard stop hitting {hs_exits}/{s['n']} times — position sizing issue")

        if issues:
            print(f"\n  {strat}:")
            for i in issues:
                print(f"    ⚠  {i}")
        else:
            print(f"\n  {strat}: ✅ No major issues detected")

    print(f"\n{sep}\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="cs strategy performance analyzer")
    parser.add_argument("--strategy", choices=STRATEGIES + ["ALL"], default="ALL",
                        help="Strategy to analyze (default: ALL)")
    parser.add_argument("--days", type=int, default=0,
                        help="Analyze last N days only (default: all time)")
    parser.add_argument("--mode", choices=["PAPER", "LIVE", "ALL"], default="LIVE",
                        help="Trade mode to analyze (default: LIVE)")
    args = parser.parse_args()

    strats = STRATEGIES if args.strategy == "ALL" else [args.strategy]
    trades = load_trades(strats, days=args.days, mode=args.mode)
    print_report(trades, args.mode)


if __name__ == "__main__":
    main()

