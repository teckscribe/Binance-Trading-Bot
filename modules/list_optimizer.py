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

# ── Optimization thresholds ──────────────────────────────────────────────────
#
# RETUNED 2026-08-20. The previous settings were MIN_TRADES=3 with the loser
# rule "WR <= 40 OR avg_pnl <= 0", and they were statistically indefensible:
#
#   With 3 trades a win rate can only be 0%, 33%, 67% or 100%, so "WR <= 40%"
#   means "0 or 1 win out of 3". At CSM's MEASURED 52.6% win rate the chance of
#   that happening to a perfectly good coin is
#       P(0 wins) + P(1 win) = 0.474^3 + 3(0.526)(0.474^2) = 46%
#   i.e. roughly HALF of all coins would be permanently banned from EVERY
#   strategy by chance alone. The OR on avg_pnl caught more still, and nothing
#   ever un-banned anything, so the tradeable universe could only shrink.
#
#   That was survivable only because modules/blacklist.py was write-only — no
#   trading code read it. Enforcement was added to live_scanner on 2026-08-20,
#   which armed this. Hence the retune.
#
# MIN_TRADES=20 is the smallest sample where a win rate is worth acting on: the
# standard error on a 50% rate at n=20 is 11pp, so a coin must land outside
# ~28-72% before it is distinguishable from the portfolio average at all.
MIN_TRADES = 20

WINNER_WR_PCT  = 65.0
WINNER_MIN_PNL = 0.0

# AND, not OR. A coin must be BOTH losing more often than the portfolio AND
# negative in expectancy before it is banned. Either condition alone is noise:
# a 45%-win-rate coin with positive expectancy is a normal, profitable coin,
# and the old OR banned it.
LOSER_WR_PCT  = 40.0
LOSER_MAX_PNL = 0.0

# Losers are removed from the watchlist and REPORTED, not auto-blacklisted.
# modules/blacklist.py has no expiry, so an automatic write there is permanent
# and unreviewable — the wrong default for a decision made from per-coin
# samples. The automatic, self-expiring ban path already exists and is driven
# by real risk events rather than sample statistics: modules/symbol_blacklist.py
# bans on repeated hard stops for 7 days, escalating to permanent on a second
# offence. Set AUTO_BLACKLIST_LOSERS=True only if you want this tool writing
# permanent bans as well.
AUTO_BLACKLIST_LOSERS = False


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
        elif wr <= LOSER_WR_PCT and avg_pnl <= LOSER_MAX_PNL:
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

    # Process Losers.
    # Always removed from the watchlist — that is reversible and low-stakes.
    # A permanent blacklist write is NOT, so it is off by default; see
    # AUTO_BLACKLIST_LOSERS above.
    for l in sorted(losers, key=lambda x: (x["wr"], x["trades"])):
        sym = l["symbol"]
        remove_from_watchlist(sym)
        if AUTO_BLACKLIST_LOSERS:
            add_to_blacklist(sym, None)
            added_black.append(f"🔴 **{sym}** ({l['wr']:.1f}% WR, {l['pnl']:+.2f}%) — BLACKLISTED")
        else:
            added_black.append(
                f"🟠 **{sym}** ({l['wr']:.1f}% WR, {l['pnl']:+.2f}%, n={l['trades']}) "
                f"— dropped from watchlist"
            )

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


