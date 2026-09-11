"""
telegram_notifier.py
Sends crypto futures signals and session summary to Telegram.

Adapted from the NSE project's telegram_notifier.py.
Key changes vs NSE version:
  - Strategy emojis (TP, FF, DB, GRID, BBR, BKD)
  - All timestamps converted to IST (UTC+05:30) for Indian Standard Time display
  - Position includes leverage and liquidation price
  - Regime shows funding rate and BTC trend
  - Live trading only

Credentials loaded from .env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from live_logger import utc_to_ist

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
BASE_URL  = f"https://api.telegram.org/bot{BOT_TOKEN}"

_STRATEGY_META = {
    "CSM":        {"emoji": "\U0001f7e2", "label": "Cross-Sectional Momentum"},
    "NASOS_V4":   {"emoji": "\U0001f7e1", "label": "NASOS V4 Dip-Buy"},
    "ELLIOT_V8":  {"emoji": "\U0001f7e3", "label": "Elliot V8 Dip-Buy"},
}

_REGIME_EMOJI = {
    "BULL_TREND":  "\U0001f402",
    "BEAR_TREND":  "\U0001f43b",
    "OVERHEATED":  "\U0001f525",
    "OVERSOLD":    "\U0001f9ca",
    "RANGING":     "↔️",
}


def _send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️  Telegram credentials missing — skipping notification")
        return False
    try:
        resp = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"⚠️  Telegram send failed: {e}")
        return False


def notify_startup(regime: dict, n_symbols: int, n_scan: int = 0, permitted_strategies: list = None) -> None:
    r      = regime.get("regime", "?")
    emoji  = _REGIME_EMOJI.get(r, "❓")
    now_utc = datetime.now(timezone.utc)
    ts     = utc_to_ist(now_utc.isoformat())

    _trend_emoji = {"BULL": "\U0001f7e2", "BEAR": "\U0001f534", "NEUTRAL": "⚪"}
    btc_t = regime.get("btc_trend", "?")
    eth_t = regime.get("eth_trend", "?")
    sol_t = regime.get("sol_trend", "?")

    # Single source: _STRATEGY_META above. The previous local copy listed only
    # 4 strategies AND filtered with `if s in _all_strats`, silently hiding any
    # strategy it didn't know (OIB never appeared in startup messages).
    active = permitted_strategies or list(_STRATEGY_META)
    strat_lines = "\n".join(
        f"  {_STRATEGY_META.get(s, {}).get('emoji', '⚪')} {s} — "
        f"{_STRATEGY_META.get(s, {}).get('label', s)}"
        for s in active
    ) or "  (none — waiting for regime)"

    scan_note = f"{n_scan}" if n_scan > 0 else f"{n_symbols}"
    if n_symbols > n_scan > 0:
        scan_note = f"{n_scan} of {n_symbols}"

    text   = (
        f"\U0001f680 <b>Binance Futures Bot started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"\U0001f550 {ts}\n"
        f"{emoji} Regime: <b>{r}</b>\n\n"
        f"<b>Regime inputs:</b>\n"
        f"  {_trend_emoji.get(btc_t,'❓')} BTC: <b>${regime.get('btc_price', 0):,.2f}</b>  ({btc_t})\n"
        f"  {_trend_emoji.get(eth_t,'❓')} ETH: <b>{eth_t}</b>\n"
        f"  {_trend_emoji.get(sol_t,'❓')} SOL: <b>{sol_t}</b>\n"
        f"  \U0001f4b0 Funding: <b>{regime.get('funding', 0)*100:+.4f}%</b>\n\n"
        f"\U0001f4ca Scanning: <b>{scan_note} symbols</b>\n\n"
        f"<b>Strategies active in {r}:</b>\n"
        f"{strat_lines}\n"
    )
    _send(text)


def notify_signal(signal: dict, size: dict, mode: str, liq_price: float) -> None:
    strat     = signal.get("strategy", "?")
    meta      = _STRATEGY_META.get(strat, {"emoji": "⚪", "label": strat})
    dirn      = signal["direction"]
    dir_emoji = "\U0001f4c8" if dirn == "LONG" else "\U0001f4c9"
    sl_dist   = abs(signal["entry_price"] - signal["sl_price"]) / signal["entry_price"] * 100
    mode_tag  = "\U0001f4dd PAPER" if mode == "PAPER" else "⚡ LIVE"

    text = (
        f"{meta['emoji']} <b>{signal['symbol']} — {meta['label']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} Direction : <b>{dirn}</b>  {mode_tag}\n"
        f"\U0001f4b5 Entry    : <b>{signal['entry_price']:.4f}</b>\n"
        f"\U0001f6d1 SL       : <b>{signal['sl_price']:.4f}</b>  ({sl_dist:.2f}%)\n"
        f"\U0001f3af TP       : <b>{signal.get('tp_price', 'Trail')}</b>\n"
        f"⚡ Leverage : <b>{size['leverage']}×</b>\n"
        f"\U0001f4b0 Notional : <b>${size['notional']:.2f}</b>\n"
        f"\U0001f480 Liq price: <b>{liq_price:.4f}</b>\n"
        f"\U0001f4dd Reason   : {signal.get('reason', '')}"
    )
    _send(text)


def notify_exit(position: dict, mode: str = "LIVE") -> None:
    """Send position close notification."""
    strat  = position.get("strategy", "?")
    meta   = _STRATEGY_META.get(strat, {"emoji": "⚪", "label": strat})
    
    pnl_pct  = position.get("pnl_pct", 0.0) * 100
    leverage = position.get("leverage", 1)
    gross_roi = pnl_pct * leverage
    
    pnl_net    = position.get("pnl_usdt_net", 0.0)
    fee        = position.get("fee_usdt", 0.0)
    margin_req = position.get("margin_req", 0.0)
    net_roi    = (pnl_net / margin_req * 100) if margin_req > 0 else gross_roi
    equity_pnl = position.get("pnl_equity_pct", 0.0) * 100

    emoji  = "✅" if pnl_net >= 0 else "❌"
    mode_tag = "\U0001f4dd PAPER" if mode == "PAPER" else "⚡ LIVE"
    pnl_dot = "\U0001f7e2" if pnl_net >= 0 else "\U0001f534"

    text = (
        f"{emoji} <b>{position['symbol']} CLOSED</b>  {mode_tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{meta['emoji']} Strategy   : {meta['label']}\n"
        f"\U0001f4ca Direction  : {position['direction']}\n"
        f"\U0001f4b5 Entry      : {position['entry_price']:.4f}\n"
        f"\U0001f4b5 Exit       : {position.get('exit_price', 0):.4f}\n"
        f"⏱ Duration   : {position.get('duration_min', 0):.1f} min\n"
        f"\U0001f51a Reason     : {position.get('exit_reason', '?')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Net P&amp;L</b>      : <b>{pnl_net:+.2f} USDT</b> (Fee: −{fee:.3f})\n"
        f"{pnl_dot} <b>Net ROI</b>    : <b>{net_roi:+.2f}%</b> ({leverage}x)\n"
        f"📈 <b>Equity P&amp;L</b> : <b>{equity_pnl:+.3f}%</b>"
    )
    _send(text)


def notify_session_summary(live_summary: dict, regime: dict) -> None:
    """Send session-end summary for live trades."""
    r     = regime.get("regime", "?")
    emoji = _REGIME_EMOJI.get(r, "❓")

    strat_rows = []
    for sid, s in live_summary.get("by_strategy", {}).items():
        n   = s.get("trades", 0)
        wr  = s["wins"] / n * 100 if n > 0 else 0
        avg = s.get("pnl_pct_sum", 0) / n * 100 if n > 0 else 0
        em  = _STRATEGY_META.get(sid, {}).get("emoji", "⚪")
        strat_rows.append(
            f"  {em} {sid}: {n} trades | WR {wr:.0f}% | avg {avg:+.2f}%"
        )
    strat_block = "\n".join(strat_rows) if strat_rows else "  No trades"

    pnl      = live_summary.get("session_pnl", 0.0)
    pnl_em   = "\U0001f7e2" if pnl >= 0 else "\U0001f534"

    text = (
        f"\U0001f4ca <b>Session Summary</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} Final regime: <b>{r}</b>\n\n"
        f"⚡ <b>LIVE TRADES</b>\n"
        f"  Trades : {live_summary.get('total_trades', 0)} "
        f"({live_summary.get('wins', 0)}W / {live_summary.get('losses', 0)}L)\n"
        f"  WR     : {live_summary.get('win_rate', 0):.1f}%\n"
        f"  {pnl_em} PnL  : <b>{pnl:+.3f}%</b>\n\n"
        f"{strat_block}"
    )
    _send(text)


def notify_error(msg: str) -> None:
    """Send error alert."""
    _send(f"\U0001f6a8 <b>Bot Error</b>\n{msg}")


def notify_regime_change(old: str, new: str, regime: dict) -> None:
    """Alert when market regime flips."""
    emoji = _REGIME_EMOJI.get(new, "❓")
    _trend_emoji = {"BULL": "\U0001f7e2", "BEAR": "\U0001f534", "NEUTRAL": "⚪"}
    btc_t = regime.get("btc_trend", "?")
    eth_t = regime.get("eth_trend", "?")
    sol_t = regime.get("sol_trend", "?")
    text  = (
        f"\U0001f504 <b>Regime Change</b>\n"
        f"  {old} → <b>{new}</b> {emoji}\n\n"
        f"  {_trend_emoji.get(btc_t,'❓')} BTC: <b>${regime.get('btc_price', 0):,.2f}</b>  ({btc_t})\n"
        f"  {_trend_emoji.get(eth_t,'❓')} ETH: <b>{eth_t}</b>\n"
        f"  {_trend_emoji.get(sol_t,'❓')} SOL: <b>{sol_t}</b>\n"
        f"  \U0001f4b0 Funding: {regime.get('funding', 0)*100:+.4f}%"
    )
    _send(text)


def notify_daily_report(
    live_summary: dict,
    regime:       dict,
    loss_status:  dict,
    report_date:  str,
    ml_status:    dict | None = None,
) -> None:
    r      = regime.get("regime", "?")
    emoji  = _REGIME_EMOJI.get(r, "❓")

    strat_lines = []
    for sid, s in live_summary.get("by_strategy", {}).items():
        n   = s.get("trades", 0)
        w   = s.get("wins", 0)
        wr  = w / n * 100 if n > 0 else 0
        avg = s.get("pnl_pct_sum", 0) / n * 100 if n > 0 else 0
        em  = _STRATEGY_META.get(sid, {}).get("emoji", "⚪")
        strat_lines.append(
            f"  {em} {sid}: {n} trades | WR {wr:.0f}% | avg {avg:+.2f}%"
        )
    strat_block = "\n".join(strat_lines) if strat_lines else "  No live trades today"

    day_pnl   = loss_status.get("day_pnl",   0.0)
    week_pnl  = loss_status.get("week_pnl",  0.0)
    day_cap   = loss_status.get("daily_cap",  -3.0)
    week_cap  = loss_status.get("weekly_cap", -15.0)
    day_ok    = "✅" if day_pnl  > day_cap  else "\U0001f534"
    week_ok   = "✅" if week_pnl > week_cap else "\U0001f534"

    live_pnl   = live_summary.get("session_pnl", 0.0)
    pnl_emoji  = "\U0001f7e2" if live_pnl >= 0 else "\U0001f534"



    text = (
        f"\U0001f4c5 <b>Daily Report — {report_date}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} Regime  : <b>{r}</b>\n"
        f"₿ BTC     : <b>${regime.get('btc_price', 0):,.2f}</b>\n\n"
        f"⚡ <b>LIVE TRADES</b>\n"
        f"  Total  : {live_summary.get('total_trades', 0)} "
        f"({live_summary.get('wins', 0)}W / {live_summary.get('losses', 0)}L)\n"
        f"  Win %  : {live_summary.get('win_rate', 0):.1f}%\n"
        f"  {pnl_emoji} P&amp;L   : <b>{live_pnl:+.3f}%</b>\n\n"
        f"{strat_block}\n\n"
        f"\U0001f6e1 <b>LOSS CAPS</b>\n"
        f"  {day_ok} Day   : {day_pnl:+.2f}%  (cap {day_cap:.0f}%)\n"
        f"  {week_ok} Week  : {week_pnl:+.2f}%  (cap {week_cap:.0f}%)"
    )
    _send(text)


