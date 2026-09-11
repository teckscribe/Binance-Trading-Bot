"""
modules/trade_analyzer.py
Automated daily trade analysis agent.

Reads session logs, detects patterns, generates actionable recommendations.
Sends summary to Telegram. Can run standalone or via scheduled task.

Analysis:
  1. Strategy P&L and R:R health check
  2. Coin-level winners/losers with blacklist suggestions
  3. Exit reason breakdown (what's killing profits)
  4. Direction bias (LONG vs SHORT)
  5. Time-of-day patterns
  6. Actionable recommendations

Usage:
  python -m modules.trade_analyzer          # standalone
  from modules.trade_analyzer import run    # from scheduler/bot
"""

import os
import json
import glob
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

log = logging.getLogger("TradeAnalyzer")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSIONS_DIR = os.path.join(_PROJECT_DIR, "logs", "live")


def _load_trades(days: int = 7) -> list:
    """Load trades from last N days of session logs."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y%m%d")
    trades = []

    for f in sorted(glob.glob(os.path.join(_SESSIONS_DIR, "session_*.json"))):
        fname = os.path.basename(f)
        # session_20260603_173424.json → extract date
        try:
            session_date = fname.split("_")[1]
        except IndexError:
            continue
        if session_date < cutoff_str:
            continue
        try:
            with open(f, encoding="utf-8") as _jf:
                d = json.load(_jf)
        except Exception:
            continue
        sid = d.get("session_id", "?")
        for t in d.get("trades", []):
            s = t.get("strategy", "")
            if s in ("LF", "ADOPTED", ""):
                continue
            t["_session"] = sid
            trades.append(t)
    return trades


def _analyze(trades: list) -> dict:
    """Run full analysis on trade list. Returns structured results."""
    if not trades:
        return {"empty": True}

    results = {}

    # ── Overall ──────────────────────────────────────────────────────────
    wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pct", 0) < 0]
    total_pnl = sum(t.get("pnl_pct", 0) for t in trades) * 100
    results["overall"] = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pnl": total_pnl,
        "avg_win": sum(t["pnl_pct"] for t in wins) / len(wins) * 100 if wins else 0,
        "avg_loss": sum(t["pnl_pct"] for t in losses) / len(losses) * 100 if losses else 0,
    }

    # ── By strategy ──────────────────────────────────────────────────────
    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t["strategy"]].append(t)

    strat_results = {}
    for strat, st_trades in by_strat.items():
        w = [t for t in st_trades if t["pnl_pct"] > 0]
        l = [t for t in st_trades if t["pnl_pct"] < 0]
        pnl = sum(t["pnl_pct"] for t in st_trades) * 100
        avg_w = sum(t["pnl_pct"] for t in w) / len(w) * 100 if w else 0
        avg_l = abs(sum(t["pnl_pct"] for t in l) / len(l) * 100) if l else 0
        rr = avg_w / avg_l if avg_l > 0 else 99
        needed_wr = 1 / (1 + rr) * 100 if rr > 0 else 100
        actual_wr = len(w) / len(st_trades) * 100 if st_trades else 0
        edge = actual_wr - needed_wr

        strat_results[strat] = {
            "n": len(st_trades), "wins": len(w), "losses": len(l),
            "pnl": pnl, "wr": actual_wr,
            "avg_win": avg_w, "avg_loss": -avg_l,
            "rr": rr, "needed_wr": needed_wr, "edge": edge,
        }
    results["strategies"] = strat_results

    # ── By coin ──────────────────────────────────────────────────────────
    by_coin = defaultdict(list)
    for t in trades:
        by_coin[t.get("symbol", "?")].append(t)

    coin_results = []
    for coin, ct in by_coin.items():
        w = len([t for t in ct if t["pnl_pct"] > 0])
        l = len([t for t in ct if t["pnl_pct"] < 0])
        pnl = sum(t["pnl_pct"] for t in ct) * 100
        strats = list(set(t["strategy"] for t in ct))
        coin_results.append({
            "coin": coin, "n": len(ct), "wins": w, "losses": l,
            "pnl": pnl, "strats": strats,
        })
    coin_results.sort(key=lambda x: x["pnl"])
    results["coins"] = coin_results

    # ── By exit reason ───────────────────────────────────────────────────
    by_exit = defaultdict(lambda: [0, 0, 0.0])
    for t in trades:
        er = t.get("exit_reason", "?")
        by_exit[er][0] += 1
        if t["pnl_pct"] > 0:
            by_exit[er][1] += 1
        by_exit[er][2] += t["pnl_pct"] * 100
    results["exits"] = dict(by_exit)

    # ── Direction ────────────────────────────────────────────────────────
    dir_results = {}
    for d in ("LONG", "SHORT"):
        dt = [t for t in trades if t.get("direction") == d]
        if dt:
            w = len([t for t in dt if t["pnl_pct"] > 0])
            pnl = sum(t["pnl_pct"] for t in dt) * 100
            dir_results[d] = {"n": len(dt), "wins": w, "pnl": pnl}
    results["direction"] = dir_results

    # ── Structural diagnostics ──────────────────────────────────────────
    diagnostics = []

    for t in trades:
        entry = t.get("entry_price", 0)
        sl = t.get("sl_price", 0)
        tp = t.get("tp_price", 0)
        lev = t.get("leverage", 1)
        direction = t.get("direction", "LONG")
        dur = t.get("duration_min", 0)
        sym = t.get("symbol", "?")
        strat = t.get("strategy", "?")
        reason = t.get("exit_reason", "?")

        if entry <= 0:
            continue

        sl_dist = abs(entry - sl)
        tp_dist = abs(entry - tp)
        sl_pct = sl_dist / entry * 100
        leveraged_sl = sl_pct * lev

        # Check 1: Tight SL (< 3% unleveraged)
        if sl_pct < 3.0 and t.get("pnl_pct", 0) < 0:
            diagnostics.append({
                "check": "TIGHT_SL",
                "symbol": sym, "strategy": strat, "direction": direction,
                "sl_pct": sl_pct, "leverage": lev,
                "leveraged_sl": leveraged_sl,
                "pnl_pct": t.get("pnl_pct", 0) * 100,
            })

        # Check 2: R:R ratio distortion (compare against strategy's design R:R)
        actual_rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if actual_rr > 0 and actual_rr < 1.5:
            diagnostics.append({
                "check": "RR_DISTORTION",
                "symbol": sym, "strategy": strat, "direction": direction,
                "actual_rr": actual_rr,
                "sl_pct": sl_pct, "tp_pct": tp_dist / entry * 100,
            })

        # Check 3: Blitz stop (SL hit within 10 minutes)
        if reason == "SL_HIT" and dur < 10:
            diagnostics.append({
                "check": "BLITZ_STOP",
                "symbol": sym, "strategy": strat, "direction": direction,
                "duration_min": dur, "sl_pct": sl_pct,
                "pnl_pct": t.get("pnl_pct", 0) * 100,
            })

        # Check 4: High leverage + tight SL combo
        if leveraged_sl > 10 and t.get("pnl_pct", 0) < 0:
            diagnostics.append({
                "check": "LEVERAGE_RISK",
                "symbol": sym, "strategy": strat, "direction": direction,
                "leverage": lev, "sl_pct": sl_pct,
                "leveraged_sl": leveraged_sl,
                "pnl_pct": t.get("pnl_pct", 0) * 100,
            })

    # Check 5: SL bucket analysis (wide SL vs tight SL profitability)
    sl_buckets = {"tight": [], "medium": [], "wide": []}
    for t in trades:
        entry = t.get("entry_price", 0)
        sl = t.get("sl_price", 0)
        if entry <= 0:
            continue
        sl_pct = abs(entry - sl) / entry * 100
        pnl = t.get("pnl_pct", 0) * 100
        if sl_pct < 3:
            sl_buckets["tight"].append(pnl)
        elif sl_pct < 6:
            sl_buckets["medium"].append(pnl)
        else:
            sl_buckets["wide"].append(pnl)

    bucket_stats = {}
    for bucket, pnls in sl_buckets.items():
        if pnls:
            bucket_stats[bucket] = {
                "n": len(pnls),
                "avg_pnl": sum(pnls) / len(pnls),
                "total_pnl": sum(pnls),
                "wr": len([p for p in pnls if p > 0]) / len(pnls) * 100,
            }

    results["diagnostics"] = diagnostics
    results["sl_buckets"] = bucket_stats

    # ── Recommendations ──────────────────────────────────────────────────
    recs = []

    # Strategies with negative edge
    for strat, sr in strat_results.items():
        if sr["edge"] < -5 and sr["n"] >= 10:
            recs.append(
                f"!! {strat}: edge={sr['edge']:+.0f}pp "
                f"(WR={sr['wr']:.0f}% need={sr['needed_wr']:.0f}%). "
                f"Review parameters or disable."
            )

    # Losing coins (>3 trades, net negative)
    for cr in coin_results:
        if cr["pnl"] < -2.0 and cr["n"] >= 3:
            strat_str = ",".join(cr["strats"])
            recs.append(
                f"XX {cr['coin']}: {cr['n']}t PnL={cr['pnl']:+.1f}% "
                f"({strat_str}). Consider blacklisting."
            )

    # Direction imbalance (only flag if the losing side has enough trades)
    for d_check in ("LONG", "SHORT"):
        dr_data = dir_results.get(d_check, {})
        if dr_data.get("n", 0) >= 5 and dr_data.get("pnl", 0) < -3:
            other = "LONG" if d_check == "SHORT" else "SHORT"
            recs.append(
                f"&gt;&gt; {d_check} losing: {dr_data['n']}t "
                f"PnL={dr_data['pnl']:+.1f}% vs "
                f"{other} {dir_results.get(other, {}).get('pnl', 0):+.1f}%"
            )

    # Exit reasons draining profit
    for er, (n, w, s) in by_exit.items():
        if s < -5.0 and n >= 3:
            recs.append(
                # "**" was a plain-text alert marker. It is ALSO Discord's
                # bold marker, so it opened an unclosed bold span and mangled
                # everything after it. Bracketed tag matches [TIGHT SL] etc.
                f"[EXIT] {er}: {n}t sum={s:+.1f}% -- review exit logic."
            )

    # Structural diagnostic recommendations
    tight_sl_count = len([d for d in diagnostics if d["check"] == "TIGHT_SL"])
    blitz_count = len([d for d in diagnostics if d["check"] == "BLITZ_STOP"])
    rr_count = len([d for d in diagnostics if d["check"] == "RR_DISTORTION"])
    lev_risk_count = len([d for d in diagnostics if d["check"] == "LEVERAGE_RISK"])

    if tight_sl_count > 0:
        tight_syms = list(set(
            d["symbol"] for d in diagnostics if d["check"] == "TIGHT_SL"
        ))[:5]
        recs.append(
            f"[TIGHT SL] {tight_sl_count} trades with SL &lt; 3%: "
            f"{', '.join(tight_syms)}. "
            f"Impact: stops hit too fast on noise. "
            f"Fix: increase MIN_SL_PCT or exclude low-ATR coins."
        )

    if blitz_count > 0:
        blitz_syms = list(set(
            d["symbol"] for d in diagnostics if d["check"] == "BLITZ_STOP"
        ))[:5]
        recs.append(
            f"[BLITZ STOP] {blitz_count} trades hit SL within 10 min: "
            f"{', '.join(blitz_syms)}. "
            f"Impact: entry timing or direction wrong. "
            f"Fix: add entry confirmation filter or widen SL."
        )

    if rr_count > 0:
        rr_all = [d for d in diagnostics if d["check"] == "RR_DISTORTION"]
        avg_rr = sum(d["actual_rr"] for d in rr_all) / len(rr_all)
        rr_syms = list(set(d["symbol"] for d in rr_all))[:5]
        recs.append(
            f"[R:R SKEW] {rr_count} trades with R:R below 1.5:1 "
            f"(avg {avg_rr:.2f}:1): {', '.join(rr_syms)}. "
            f"Impact: wins don't compensate losses. "
            f"Fix: ensure TP scales proportionally when SL floor activates."
        )

    if lev_risk_count > 0:
        lev_examples = [d for d in diagnostics if d["check"] == "LEVERAGE_RISK"][:3]
        recs.append(
            f"[LEV RISK] {lev_risk_count} trades with leveraged SL &gt; 10%: "
            f"e.g. {lev_examples[0]['symbol']} "
            f"{lev_examples[0]['leverage']}x @ {lev_examples[0]['sl_pct']:.1f}% SL = "
            f"{lev_examples[0]['leveraged_sl']:.0f}% leveraged loss. "
            f"Impact: outsized losses. Fix: cap leverage or widen SL floor."
        )

    # SL bucket insight
    tight_b = bucket_stats.get("tight", {})
    wide_b = bucket_stats.get("wide", {})
    if tight_b and wide_b:
        if tight_b.get("avg_pnl", 0) < wide_b.get("avg_pnl", 0) - 0.5:
            recs.append(
                f"[SL ANALYSIS] Tight SL (&lt;3%): {tight_b['n']}t "
                f"avg={tight_b['avg_pnl']:+.2f}% WR={tight_b['wr']:.0f}% | "
                f"Wide SL (&gt;6%): {wide_b['n']}t "
                f"avg={wide_b['avg_pnl']:+.2f}% WR={wide_b['wr']:.0f}%. "
                f"Wider stops are more profitable -- raise MIN_SL_PCT."
            )

    results["recommendations"] = recs
    return results


def _format_telegram(results: dict, days: int) -> str:
    """Format analysis results as Telegram HTML message."""
    if results.get("empty"):
        return "📊 <b>Trade Analysis</b>\n\nNo trades in the last {days} days."

    o = results["overall"]
    lines = [
        f"📊 <b>Trade Analysis ({days}d)</b>",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"Trades: {o['trades']} | W:{o['wins']} L:{o['losses']} | "
        f"WR: {o['wr']:.0f}%",
        f"PnL: <b>{o['pnl']:+.2f}%</b> | "
        f"Avg W:{o['avg_win']:+.2f}% L:{o['avg_loss']:+.2f}%",
        "",
        "📈 <b>Strategy Health</b>",
    ]

    for strat in sorted(results["strategies"]):
        sr = results["strategies"][strat]
        emoji = "✅" if sr["edge"] > 0 else "⚠️" if sr["edge"] > -5 else "❌"
        lines.append(
            f"  {emoji} {strat}: {sr['n']}t WR={sr['wr']:.0f}% "
            f"PnL={sr['pnl']:+.1f}% edge={sr['edge']:+.0f}pp"
        )

    # Top 3 losers + top 3 winners
    coins = results["coins"]
    losers = [c for c in coins if c["pnl"] < 0][:3]
    winners = [c for c in reversed(coins) if c["pnl"] > 0][:3]

    if losers:
        lines.append("")
        lines.append("🔴 <b>Worst Coins</b>")
        for c in losers:
            lines.append(
                f"  {c['coin']}: {c['n']}t {c['pnl']:+.1f}% "
                f"({','.join(c['strats'])})"
            )
    if winners:
        lines.append("")
        lines.append("🟢 <b>Best Coins</b>")
        for c in winners:
            lines.append(
                f"  {c['coin']}: {c['n']}t {c['pnl']:+.1f}% "
                f"({','.join(c['strats'])})"
            )

    # Direction
    dr = results.get("direction", {})
    if dr:
        lines.append("")
        lines.append("↕️ <b>Direction</b>")
        for d in ("LONG", "SHORT"):
            if d in dr:
                lines.append(
                    f"  {d}: {dr[d]['n']}t W={dr[d]['wins']} "
                    f"PnL={dr[d]['pnl']:+.1f}%"
                )

    # SL Bucket Analysis
    sl_b = results.get("sl_buckets", {})
    if sl_b:
        lines.append("")
        lines.append("SL BUCKET ANALYSIS")
        for bucket in ("tight", "medium", "wide"):
            b = sl_b.get(bucket)
            if b:
                label = {"tight": "&lt;3%", "medium": "3-6%", "wide": "&gt;6%"}[bucket]
                lines.append(
                    f"  {label}: {b['n']}t "
                    f"avg={b['avg_pnl']:+.2f}% WR={b['wr']:.0f}%"
                )

    # Structural Diagnostics summary
    diags = results.get("diagnostics", [])
    if diags:
        by_check = defaultdict(int)
        for d in diags:
            by_check[d["check"]] += 1
        lines.append("")
        lines.append("STRUCTURAL DIAGNOSTICS")
        for check, count in sorted(by_check.items()):
            lines.append(f"  {check}: {count} trades flagged")

    # Recommendations
    recs = results.get("recommendations", [])
    if recs:
        lines.append("")
        lines.append("RECOMMENDATIONS")
        for r in recs:
            lines.append(f"  {r}")

    return "\n".join(lines)


def run(days: int = 3) -> str:
    """
    Run analysis and return formatted Telegram message.
    Call from scheduler, bot, or standalone.
    """
    trades = _load_trades(days=days)
    results = _analyze(trades)
    return _format_telegram(results, days)


def run_and_send(days: int = 3) -> bool:
    """Run analysis and send to Telegram."""
    msg = run(days=days)
    try:
        from telegram_notifier import _send
        return _send(msg)
    except Exception as exc:
        log.error(f"Failed to send analysis: {exc}")
        return False


if __name__ == "__main__":
    print(run(days=7))



