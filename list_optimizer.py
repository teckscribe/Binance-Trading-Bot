"""
list_optimizer.py
Automates the optimization of Watchlist and Blacklist based on ML trade outcomes.
Identifies winners (add to watchlist) and losers (add to blacklist).
"""

import os
import json
from collections import defaultdict

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIGNALS_FILE = os.path.join(_PROJECT_DIR, "data", "ml", "signals.jsonl")
_OUTCOMES_FILE = os.path.join(_PROJECT_DIR, "data", "ml", "outcomes.jsonl")

# Optimization Thresholds
MIN_TRADES = 3
WINNER_WR_PCT = 65.0
WINNER_MIN_PNL = 0.0

LOSER_WR_PCT = 40.0
LOSER_MAX_PNL = 0.0


def run_optimization() -> str:
    """
    Parses ML logs, identifies winners and losers, updates Watchlist and Blacklist.
    Returns a formatted markdown report.
    """
    if not os.path.exists(_SIGNALS_FILE) or not os.path.exists(_OUTCOMES_FILE):
        return "❌ ML log files not found. Cannot optimize."

    # 1. Load signals
    signals = {}
    with open(_SIGNALS_FILE, "r") as f:
        for line in f:
            if not line.strip(): continue
            try:
                sig = json.loads(line)
                signals[sig["signal_id"]] = sig
            except Exception:
                pass

    # 2. Merge with outcomes
    coin_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_sum": 0.0})

    with open(_OUTCOMES_FILE, "r") as f:
        for line in f:
            if not line.strip(): continue
            try:
                out = json.loads(line)
                sig_id = out.get("signal_id")
                if sig_id in signals:
                    sig = signals[sig_id]
                    sym = sig.get("symbol")
                    if not sym: continue
                    
                    pnl = out.get("pnl_pct", 0.0)
                    won = 1 if pnl > 0 else 0

                    coin_stats[sym]["trades"] += 1
                    coin_stats[sym]["wins"] += won
                    coin_stats[sym]["pnl_sum"] += pnl
            except Exception:
                pass

    if not coin_stats:
        return "❌ No completed trades found in ML logs."

    # 3. Categorize coins
    winners = []
    losers = []

    for sym, data in coin_stats.items():
        if data["trades"] < MIN_TRADES:
            continue
            
        wr = (data["wins"] / data["trades"]) * 100
        avg_pnl = (data["pnl_sum"] / data["trades"]) * 100
        
        info = {"symbol": sym, "trades": data["trades"], "wr": wr, "pnl": avg_pnl}

        if wr >= WINNER_WR_PCT and avg_pnl >= WINNER_MIN_PNL:
            winners.append(info)
        elif wr <= LOSER_WR_PCT or avg_pnl <= LOSER_MAX_PNL:
            losers.append(info)

    # 4. Apply changes
    from modules.watchlist import add_to_watchlist, remove_from_watchlist, get_watchlist_info
    from modules.blacklist import add as add_to_blacklist

    added_watch = []
    added_black = []
    
    # Process Winners
    for w in sorted(winners, key=lambda x: (-x["wr"], -x["trades"])):
        sym = w["symbol"]
        if add_to_watchlist(sym):
            added_watch.append(f"🟢 **{sym}** ({w['wr']:.1f}% WR, {w['pnl']:+.2f}%)")

    # Process Losers
    for l in sorted(losers, key=lambda x: (x["wr"], x["trades"])):
        sym = l["symbol"]
        # Remove from watchlist if they are there
        remove_from_watchlist(sym)
        # Add to blacklist for all strategies
        add_to_blacklist(sym, None)
        added_black.append(f"🔴 **{sym}** ({l['wr']:.1f}% WR, {l['pnl']:+.2f}%)")

    # 5. Build Report
    lines = [
        "🛠 **Weekly List Optimization Complete**",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Analyzed ML data. Minimum trades required: {MIN_TRADES}",
        ""
    ]

    if added_watch:
        lines.append(f"**Promoted to Watchlist ({len(added_watch)}):**")
        lines.extend([f"• {msg}" for msg in added_watch])
    else:
        lines.append("*No new coins qualified for the Watchlist.*")
        
    lines.append("")

    if added_black:
        lines.append(f"**Banned to Blacklist ({len(added_black)}):**")
        lines.extend([f"• {msg}" for msg in added_black])
    else:
        lines.append("*No new coins were blacklisted.*")
        
    current_watchlist = get_watchlist_info().get("manual_adds", [])
    lines.append("")
    lines.append(f"📋 Total permanently tracked coins: **{len(current_watchlist)}**")
    
    return "\n".join(lines)

