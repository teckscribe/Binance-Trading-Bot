"""
discord_notifier.py
Sends crypto futures signals and alerts to Discord via webhook.

CSB — Binance USDM Futures LIVE trading bot.

Drop-in replacement for telegram_notifier.py with identical function
signatures. Uses Discord Embeds for rich formatting.

Credentials loaded from .env:
  DISCORD_WEBHOOK_URL   -- #notifications channel webhook
"""

import os
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

_IST = timezone(timedelta(hours=5, minutes=30))

def _utc_to_ist(dt_utc: datetime) -> str:
    return dt_utc.astimezone(_IST).strftime("%Y-%m-%d %H:%M IST")

# Discord embed colors (decimal)
_COLOR_GREEN  = 0x2ECC71
_COLOR_RED    = 0xE74C3C
_COLOR_BLUE   = 0x3498DB
_COLOR_ORANGE = 0xE67E22
_COLOR_PURPLE = 0x9B59B6
_COLOR_GREY   = 0x95A5A6

_STRATEGY_META = {
    "CSM":        {"emoji": "🟢", "label": "Cross-Sectional Momentum"},
    "NASOS_V4":   {"emoji": "🟡", "label": "NASOS V4 Dip-Buy"},
    "TSMOM_4H":   {"emoji": "🔵", "label": "TSMOM 4H Trend"},
    "REBALANCING_PREMIUM": {"emoji": "🟣", "label": "Rebalancing Premium"},
}

_REGIME_EMOJI = {
    "BULL_TREND": "🐂",
    "BEAR_TREND": "🐻",
    "OVERHEATED": "🔥",
    "OVERSOLD":   "🧊",
    "RANGING":    "↔️",
}

_REGIME_COLOR = {
    "BULL_TREND": _COLOR_GREEN,
    "BEAR_TREND": _COLOR_RED,
    "OVERHEATED": _COLOR_ORANGE,
    "OVERSOLD":   _COLOR_BLUE,
    "RANGING":    _COLOR_PURPLE,
}


def _send_embed(embed: dict) -> bool:
    """POST a single embed to the Discord webhook."""
    if not WEBHOOK_URL:
        print("Discord webhook URL missing -- skipping notification")
        return False
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"Discord send failed: {exc}")
        return False


# ── Public notification functions (same signatures as telegram_notifier.py) ───

def notify_startup(
    regime: dict,
    n_symbols: int,
    n_scan: int = 0,
    permitted_strategies: list = None,
) -> None:
    r      = regime.get("regime", "?")
    emoji  = _REGIME_EMOJI.get(r, "❓")
    color  = _REGIME_COLOR.get(r, _COLOR_GREY)
    ts     = _utc_to_ist(datetime.now(timezone.utc))

    _trend_emoji = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "⚪"}
    btc_t = regime.get("btc_trend", "?")
    eth_t = regime.get("eth_trend", "?")
    sol_t = regime.get("sol_trend", "?")

    # Single source: _STRATEGY_META above. The previous local copy listed only
    # 4 strategies AND filtered unknown ones out — OIB never appeared here.
    # `is not None`, not `or`: [] means nothing is permitted and must print
    # as none; `or` fell back to listing every strategy as active.
    active = permitted_strategies if permitted_strategies is not None else list(_STRATEGY_META)
    strat_lines = "\n".join(
        f"{_STRATEGY_META.get(s, {}).get('emoji', '⚪')} **{s}** — "
        f"{_STRATEGY_META.get(s, {}).get('label', s)}"
        for s in active
    ) or "(none — disabled or not permitted in this regime)"

    scan_note = f"{n_scan} of {n_symbols}" if n_symbols > n_scan > 0 else str(n_symbols)

    embed = {
        "title": "🚀 Binance Futures Bot started  ⚡ LIVE",
        "color": color,
        "fields": [
            {"name": "Regime",   "value": f"{emoji} **{r}**",  "inline": True},
            {"name": "Scanning", "value": f"**{scan_note}** symbols", "inline": True},
            {"name": "BTC",  "value": f"{_trend_emoji.get(btc_t,'❓')} ${regime.get('btc_price',0):,.2f}  ({btc_t})", "inline": True},
            {"name": "ETH",  "value": f"{_trend_emoji.get(eth_t,'❓')} {eth_t}", "inline": True},
            {"name": "SOL",  "value": f"{_trend_emoji.get(sol_t,'❓')} {sol_t}", "inline": True},
            {"name": "Funding", "value": f"{regime.get('funding',0)*100:+.4f}%", "inline": True},
            {"name": f"Strategies active in {r}", "value": strat_lines, "inline": False},
        ],
        "footer": {"text": ts},
    }
    _send_embed(embed)


def _kronos_line(d: dict) -> str:
    """'+0.0312 (thr 0.025) PASS' for a CSM signal/position; '' otherwise.

    kronos_pred_fav is None when the entry proceeded without a score
    (fail-open) and absent for non-CSM strategies or when the gate is off.
    """
    if d.get("strategy") != "CSM" or "kronos_pred_fav" not in d:
        return ""
    pf = d.get("kronos_pred_fav")
    if pf is None:
        return "no score (fail-open)"
    try:
        from modules import settings_manager as cfg
        thr = float((d.get("settings_entry") or {}).get("KRONOS_PF_THR", cfg.get("KRONOS_PF_THR")))
    except Exception:
        thr = None
    tag = "" if thr is None else f" (thr {thr:.3f}) {'PASS' if pf >= thr else 'below'}"
    return f"{pf:+.4f}{tag}"


def notify_signal(signal: dict, size: dict, mode: str, liq_price: float) -> None:
    strat     = signal.get("strategy", "?")
    meta      = _STRATEGY_META.get(strat, {"emoji": "⚪", "label": strat})
    dirn      = signal["direction"]
    dir_emoji = "📈" if dirn == "LONG" else "📉"
    sl_dist   = abs(signal["entry_price"] - signal["sl_price"]) / signal["entry_price"] * 100
    color     = _COLOR_GREEN if dirn == "LONG" else _COLOR_RED

    tp = signal.get("tp_price")
    tp_str = f"{tp:.4f}" if tp else "Trail"

    embed = {
        "title": f"{meta['emoji']} {signal['symbol']} — {meta['label']}  ⚡ LIVE",
        "color": color,
        "fields": [
            {"name": "Direction", "value": f"{dir_emoji} **{dirn}**",         "inline": True},
            {"name": "Entry",     "value": f"**{signal['entry_price']:.4f}**", "inline": True},
            {"name": "SL",        "value": f"{signal['sl_price']:.4f}  ({sl_dist:.2f}%)", "inline": True},
            {"name": "TP",        "value": tp_str,                             "inline": True},
            {"name": "Leverage",  "value": f"**{size['leverage']}x**",         "inline": True},
            {"name": "Notional",  "value": f"**${size['notional']:.2f}**",     "inline": True},
            {"name": "Liq price", "value": f"{liq_price:.4f}",                 "inline": True},
            {"name": "Reason",    "value": signal.get("reason", "—"),          "inline": False},
        ],
    }
    k = _kronos_line(signal)
    if k:
        embed["fields"].append({"name": "Kronos", "value": f"**{k}**", "inline": False})
    _send_embed(embed)


def notify_exit(position: dict, mode: str = "LIVE") -> None:
    strat    = position.get("strategy", "?")
    meta     = _STRATEGY_META.get(strat, {"emoji": "⚪", "label": strat})
    pnl_pct  = position.get("pnl_pct", 0.0) * 100
    leverage = position.get("leverage", 1)
    gross_roi = pnl_pct * leverage

    pnl_net    = position.get("pnl_usdt_net", 0.0)
    fee        = position.get("fee_usdt", 0.0)
    margin_req = position.get("margin_req", 0.0)
    net_roi    = (pnl_net / margin_req * 100) if margin_req > 0 else gross_roi
    equity_pnl = position.get("pnl_equity_pct", 0.0) * 100

    win      = pnl_net >= 0
    emoji    = "✅" if win else "❌"
    color    = _COLOR_GREEN if win else _COLOR_RED

    embed = {
        "title": f"{emoji} {position['symbol']} CLOSED  ⚡ LIVE",
        "color": color,
        "fields": [
            {"name": "Strategy",  "value": f"{meta['emoji']} {meta['label']}", "inline": True},
            {"name": "Direction", "value": position["direction"],               "inline": True},
            {"name": "Entry",     "value": f"{position['entry_price']:.4f}",   "inline": True},
            {"name": "Exit",      "value": f"{position.get('exit_price',0):.4f}", "inline": True},
            {"name": "Net P&L",   "value": f"**{pnl_net:+.2f} USDT** (Fee: −{fee:.3f})", "inline": True},
            {"name": "Net ROI",   "value": f"**{net_roi:+.2f}%** ({leverage}x)", "inline": True},
            {"name": "Equity P&L","value": f"**{equity_pnl:+.3f}%**",           "inline": True},
            {"name": "Duration",  "value": f"{position.get('duration_min',0):.1f} min", "inline": True},
            {"name": "Reason",    "value": position.get("exit_reason", "?"),   "inline": True},
        ],
    }
    k = _kronos_line(position)
    if k:
        embed["fields"].append({"name": "Kronos", "value": k, "inline": True})
    if position.get("regime_entry"):
        rx = position.get("regime_exit")
        val = position["regime_entry"] + (f" → {rx}" if rx and rx != position["regime_entry"] else "")
        embed["fields"].append({"name": "Regime", "value": val, "inline": True})
    _send_embed(embed)


def notify_session_summary(live_summary: dict, regime: dict) -> None:
    r     = regime.get("regime", "?")
    emoji = _REGIME_EMOJI.get(r, "❓")
    pnl   = live_summary.get("session_pnl", 0.0)
    color = _COLOR_GREEN if pnl >= 0 else _COLOR_RED

    strat_rows = []
    for sid, s in live_summary.get("by_strategy", {}).items():
        n   = s.get("trades", 0)
        wr  = s["wins"] / n * 100 if n > 0 else 0
        avg = s.get("pnl_pct_sum", 0) / n * 100 if n > 0 else 0
        em  = _STRATEGY_META.get(sid, {}).get("emoji", "⚪")
        strat_rows.append(f"{em} **{sid}**: {n} trades | WR {wr:.0f}% | avg {avg:+.2f}%")

    embed = {
        "title": "📊 Session Summary",
        "color": color,
        "fields": [
            {"name": "Final Regime", "value": f"{emoji} **{r}**", "inline": True},
            {"name": "Trades", "value": f"{live_summary.get('total_trades',0)}  ({live_summary.get('wins',0)}W / {live_summary.get('losses',0)}L)", "inline": True},
            {"name": "Win Rate", "value": f"{live_summary.get('win_rate',0):.1f}%", "inline": True},
            {"name": "P&L",     "value": f"**{pnl:+.3f}%**", "inline": True},
            {"name": "By Strategy", "value": "\n".join(strat_rows) or "No trades", "inline": False},
        ],
    }
    _send_embed(embed)


def notify_error(msg: str) -> None:
    embed = {
        "title": "🚨 Bot Error",
        "description": msg,
        "color": _COLOR_RED,
    }
    _send_embed(embed)


def notify_regime_change(old: str, new: str, regime: dict) -> None:
    emoji = _REGIME_EMOJI.get(new, "❓")
    color = _REGIME_COLOR.get(new, _COLOR_GREY)
    _trend_emoji = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "⚪"}
    btc_t = regime.get("btc_trend", "?")
    eth_t = regime.get("eth_trend", "?")
    sol_t = regime.get("sol_trend", "?")

    embed = {
        "title": f"🔄 Regime Change: {old} → {new} {emoji}",
        "color": color,
        "fields": [
            {"name": "BTC",     "value": f"{_trend_emoji.get(btc_t,'❓')} ${regime.get('btc_price',0):,.2f}  ({btc_t})", "inline": True},
            {"name": "ETH",     "value": f"{_trend_emoji.get(eth_t,'❓')} {eth_t}", "inline": True},
            {"name": "SOL",     "value": f"{_trend_emoji.get(sol_t,'❓')} {sol_t}", "inline": True},
            {"name": "Funding", "value": f"{regime.get('funding',0)*100:+.4f}%", "inline": True},
        ],
    }
    _send_embed(embed)


def notify_daily_report(
    live_summary: dict,
    regime:       dict,
    loss_status:  dict,
    report_date:  str,
    ml_status:    dict | None = None,
) -> None:
    r      = regime.get("regime", "?")
    emoji  = _REGIME_EMOJI.get(r, "❓")
    pnl    = live_summary.get("session_pnl", 0.0)
    color  = _COLOR_GREEN if pnl >= 0 else _COLOR_RED

    strat_lines = []
    for sid, s in live_summary.get("by_strategy", {}).items():
        n   = s.get("trades", 0)
        w   = s.get("wins", 0)
        wr  = w / n * 100 if n > 0 else 0
        avg = s.get("pnl_pct_sum", 0) / n * 100 if n > 0 else 0
        em  = _STRATEGY_META.get(sid, {}).get("emoji", "⚪")
        strat_lines.append(f"{em} **{sid}**: {n} trades | WR {wr:.0f}% | avg {avg:+.2f}%")

    day_pnl  = loss_status.get("day_pnl",   0.0)
    week_pnl = loss_status.get("week_pnl",  0.0)
    day_cap  = loss_status.get("daily_cap",  -3.0)
    week_cap = loss_status.get("weekly_cap", -15.0)
    day_ok   = "✅" if day_pnl  > day_cap  else "🔴"
    week_ok  = "✅" if week_pnl > week_cap else "🔴"

    fields = [
        {"name": "Regime", "value": f"{emoji} **{r}**", "inline": True},
        {"name": "BTC",    "value": f"${regime.get('btc_price',0):,.2f}", "inline": True},
        {"name": "Trades", "value": f"{live_summary.get('total_trades',0)}  ({live_summary.get('wins',0)}W / {live_summary.get('losses',0)}L)", "inline": True},
        {"name": "Win Rate", "value": f"{live_summary.get('win_rate',0):.1f}%", "inline": True},
        {"name": "P&L",    "value": f"**{pnl:+.3f}%**", "inline": True},
        {"name": "By Strategy", "value": "\n".join(strat_lines) or "No trades today", "inline": False},
        {"name": "Loss Caps", "value": f"{day_ok} Day: {day_pnl:+.2f}%  (cap {day_cap:.0f}%)\n{week_ok} Week: {week_pnl:+.2f}%  (cap {week_cap:.0f}%)", "inline": False},
    ]



    embed = {
        "title": f"📅 Daily Report — {report_date}",
        "color": color,
        "fields": fields,
    }
    _send_embed(embed)


