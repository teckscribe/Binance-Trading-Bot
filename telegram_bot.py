"""
telegram_bot.py
Crypto Trade Telegram Control Bot.

Runs as a separate systemd service (csb-bot.service).

Commands:
  /start  — show main menu
  /status — live bot status + loss cap state
  /menu   — show control buttons

Inline buttons:
  ▶ Start     → sudo systemctl start csb
  ⏹ Stop      → sudo systemctl stop csb
  🔄 Restart   → sudo systemctl restart csb
  📊 Status    → service status + loss caps
  🛡 Loss caps → daily/weekly P&L vs caps
  💰 Capital   → view/change ACCOUNT_EQUITY_USDT in .env

Capital change flow:
  Tap 💰 Capital → shows current value + asks to type new amount
  User types e.g. "500" → bot validates → writes data/settings.json → confirms
  Scanner must be restarted for new capital to take effect.

Security: all input rejected from any chat_id other than TELEGRAM_CHAT_ID.
"""

import os
import re
import logging
import subprocess
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

load_dotenv()

from modules import settings_manager as cfg
cfg.migrate_from_env()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))


# Strategy IDs come from the factory — never hardcode a list here. Hardcoded
# copies of this list drifted out of sync in SIX places (logger, leverage,
# analyzer, overrides, backtester, binance_status), each failing silently.
def _production_strats() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
        if ids:
            return ids
    except Exception:
        pass
    return ["CSM", "NASOS_V4", "ELLIOT_V8"]


PROD_STRATS = _production_strats()
STRAT_EMOJI = {"CSM": "🟢", "NASOS_V4": "🟡", "ELLIOT_V8": "🟣"}
SERVICE   = "csb"

_BOT_DIR  = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("TelegramBot")

# Conversation state — tracks which user is mid-input
# Key: chat_id, Value: what we are waiting for e.g. "capital"
_waiting_for: dict[int, str] = {}


# ─── Security ─────────────────────────────────────────────────────────────────

def _allowed(update: Update) -> bool:
    cid = update.effective_chat.id
    if cid != CHAT_ID:
        log.warning(f"Rejected from unknown chat_id={cid}")
        return False
    return True


# ─── Settings access ──────────────────────────────────────────────────────────
# Tunables live in data/settings.json (modules/settings_manager.py) and the
# scanner re-reads them every cycle, so edits below apply without a restart.
# Only LIVE_ENABLED is still a .env value — flipping it restarts the scanner.

def _setting(key: str) -> str:
    return cfg.get_str(key)


def _set_setting(key: str, value) -> tuple[bool, str]:
    ok, err = cfg.set_value(key, value, source="telegram")
    if ok:
        log.info(f"settings.json updated: {key}={value}")
    else:
        log.error(f"settings.json update refused: {err}")
    return ok, err


def _live_enabled() -> bool:
    return cfg.env_get("LIVE_ENABLED", "false").lower() == "true"


# ─── Systemctl helpers ────────────────────────────────────────────────────────

def _systemctl(action: str) -> tuple[bool, str]:
    try:
        # stop needs longer timeout — graceful shutdown closes all positions
        # restart = stop + start, so needs same 70s budget as stop alone
        timeout = 70 if action in ("stop", "restart") else 20
        result = subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/systemctl", action, SERVICE],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, f"systemctl {action} {SERVICE} → OK"
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as exc:
        return False, str(exc)


def _service_status() -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", SERVICE],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        emoji = {"active": "🟢", "inactive": "🔴", "failed": "🔴"}.get(state, "⚪")
        return f"{emoji} Scanner: <b>{state}</b>"
    except Exception:
        return "⚪ Scanner: <b>unknown</b>"


def _loss_status() -> str:
    """
    Day + Week P&L display.
    Day from Binance live (matches app); week from internal tracker.
    Falls back to legacy closed-trade tracker if Binance API fails.
    """
    try:
        try:
            from modules.risk_engine    import DAILY_LOSS_CAP, WEEKLY_LOSS_CAP
            from modules.binance_status import get_pnl_summary
        except Exception:
            DAILY_LOSS_CAP, WEEKLY_LOSS_CAP = -0.05, -0.10
            get_pnl_summary = None
        daily_cap_pct  = DAILY_LOSS_CAP  * 100
        weekly_cap_pct = WEEKLY_LOSS_CAP * 100

        # Read week_pnl from internal tracker (risk_engine accumulator)
        import json
        fpath = os.path.join(_BOT_DIR, "data", "loss_tracker.json")
        week_pnl = 0.0
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    week_pnl = json.load(f).get("week_pnl", 0.0) * 100
            except Exception:
                pass

        # ── Primary: Binance-direct day-PnL ─────────────────────────────
        summary = get_pnl_summary() if get_pnl_summary else None
        if summary is not None:
            day_pnl     = summary["day_pnl_pct"]
            day_usdt    = summary["day_pnl_usdt"]
            margin_bal  = summary["margin_balance"]
            unrealized  = summary["unrealized_pnl"]
            day_ok      = "✅" if day_pnl > daily_cap_pct  else "🔴 CAP HIT"
            week_ok     = "✅" if week_pnl > weekly_cap_pct else "🔴 CAP HIT"
            return (
                f"  {day_ok}  Day  P&amp;L : <b>{day_pnl:+.2f}%</b>  "
                f"(${day_usdt:+.4f}, cap {daily_cap_pct:+.0f}%)\n"
                f"  {week_ok}  Week P&amp;L : <b>{week_pnl:+.2f}%</b>  "
                f"(cap {weekly_cap_pct:+.0f}%)\n"
                f"  💰 Wallet : <b>${margin_bal:.4f}</b>  "
                f"(unrealized: ${unrealized:+.4f})\n"
                f"  <i>source: binance live (day) / tracker (week)</i>"
            )

        # ── Fallback: legacy closed-trade tracker ────────────────────────
        if not os.path.exists(fpath):
            return "  No loss data yet"
        with open(fpath) as f:
            data = json.load(f)
        day_pnl = data.get("day_pnl", 0.0) * 100
        day_ok  = "✅" if day_pnl  > daily_cap_pct  else "🔴 CAP HIT"
        week_ok = "✅" if week_pnl > weekly_cap_pct else "🔴 CAP HIT"
        return (
            f"  {day_ok}  Day  P&amp;L : <b>{day_pnl:+.2f}%</b>  "
            f"(cap {daily_cap_pct:+.0f}%)\n"
            f"  {week_ok}  Week P&amp;L : <b>{week_pnl:+.2f}%</b>  "
            f"(cap {weekly_cap_pct:+.0f}%)\n"
            f"  <i>source: closed-trade tracker (binance unreachable)</i>"
        )
    except Exception as exc:
        return f"  Error reading loss data: {exc}"


def _capital_status() -> str:
    return _setting("ACCOUNT_EQUITY_USDT")

def _leverage_status() -> str:
    val = cfg.get("GLOBAL_LEVERAGE")
    return str(val) if val > 0 else "Auto"


def _max_concurrent_status() -> str:
    return _setting("MAX_CONCURRENT")


def _max_per_strategy_status() -> str:
    return _setting("MAX_PER_STRATEGY")


def _known_strategy_ids() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        return [s.STRATEGY_ID for s in StrategyFactory.get_all()]
    except Exception:
        return []


def validate_max_per_strategy(text: str) -> tuple[bool, str, str]:
    """
    Validate a MAX_PER_STRATEGY string like "CSM:1,NASOS_V4:2,ELLIOT_V8:2".

    Returns (ok, normalised_value, message). The message carries WARNINGS even
    when ok is True — a cap above MAX_CONCURRENT is inert, and a total below it
    leaves slots that can never fill, and both are easy to write by accident.
    """
    known = _known_strategy_ids()
    pairs, seen = [], set()

    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if ":" not in chunk:
            return False, "", (f"'{chunk}' is not STRATEGY:NUMBER. "
                               f"Example: <code>CSM:1,NASOS_V4:2</code>")
        sid, _, raw = chunk.partition(":")
        sid = sid.upper()
        if known and sid not in known:
            return False, "", (f"Unknown strategy '<b>{sid}</b>'.\n"
                               f"Available: {', '.join(known)}")
        if sid in seen:
            return False, "", f"'<b>{sid}</b>' appears more than once."
        try:
            n = int(raw)
        except ValueError:
            return False, "", f"'{raw}' after {sid}: is not a whole number."
        if n < 0:
            return False, "", f"{sid} cap cannot be negative."
        seen.add(sid)
        pairs.append((sid, n))

    if not pairs:
        return False, "", "Nothing to set. Example: <code>CSM:1,NASOS_V4:2,ELLIOT_V8:2</code>"

    value = ",".join(f"{s}:{n}" for s, n in pairs)

    warn = []
    mc = cfg.get("MAX_CONCURRENT")
    over = [f"{s}:{n}" for s, n in pairs if n > mc]
    if over:
        warn.append(f"⚠️ {', '.join(over)} exceed MAX_CONCURRENT={mc} — "
                    f"those caps can never bind.")
    total = sum(n for _, n in pairs)
    if total < mc:
        warn.append(f"⚠️ Caps total {total} but MAX_CONCURRENT={mc} — "
                    f"{mc - total} slot(s) can never be filled.")
    missing = [s for s in known if s not in seen]
    if missing:
        warn.append(f"⚠️ Not listed: {', '.join(missing)} — "
                    f"unlisted strategies are UNCAPPED.")

    return True, value, "\n".join(warn)


def _reset_loss_tracker(scope: str = "day") -> tuple[bool, str]:
    """
    Reset the persistent loss tracker.

    Args:
        scope : 'day' — zero out day_pnl + day_start_equity (only mode in use)

    Weekly reset removed 2026-05-11 (weekly cap deprecated).

    Returns:
        (success: bool, message: str)
    """
    import json
    fpath = os.path.join(_BOT_DIR, "data", "loss_tracker.json")
    try:
        now  = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week  = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

        # Load existing data so we can preserve values we're not resetting
        try:
            if os.path.exists(fpath):
                with open(fpath) as f:
                    data = json.load(f)
            else:
                data = {}
        except Exception:
            data = {}

        data["day"]              = today
        data["day_pnl"]          = 0.0
        # Force re-snapshot of day_start_equity so binance_status starts a
        # fresh baseline on next equity refresh (otherwise day-PnL would
        # still measure against pre-reset wallet).
        data["day_start_equity"] = 0.0

        # Weekly cap removed 2026-05-11. Leave week fields untouched (for
        # backward-compat with any legacy reader); they're no longer enforced.
        if scope == "all":
            data["week"]     = week
            data["week_pnl"] = 0.0

        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w") as f:
            json.dump(data, f, indent=2)

        log.info(f"Loss tracker reset (scope={scope}): {data}")
        return True, json.dumps(data, indent=2)

    except Exception as exc:
        log.error(f"Loss tracker reset failed: {exc}")
        return False, str(exc)


def _trade_summary() -> str:
    """
    Read all strategy NDJSON logs and return a formatted trade summary
    covering all-time data across every strategy.
    """
    import json
    from glob import glob

    STRATS     = PROD_STRATS
    strat_dir  = os.path.join(_BOT_DIR, "logs", "strategies")

    total_trades = 0
    total_wins   = 0
    total_pnl    = 0.0
    mc_trades    = 0           # MANUAL_CLOSE bucket — segregated as unreliable
    mc_pnl       = 0.0         # (pre-fix used current mark price → fake PnL)
    by_strat     = {}

    for strat in STRATS:
        folder = os.path.join(strat_dir, strat)
        if not os.path.exists(folder):
            continue

        s_trades = 0
        s_wins   = 0
        s_pnl    = 0.0

        for fpath in sorted(glob(os.path.join(folder, "*.json"))):
            try:
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if r.get("type") != "EXIT":
                            continue
                        pnl = r.get("pnl_pct_100", 0.0)
                        # Filter out entries corrupted by the legacy equity
                        # bug (max realistic pnl_pct_100 on this account is
                        # ±20%; anything beyond ±50% is junk).
                        if abs(pnl) > 50.0:
                            continue
                        # Segregate MANUAL_CLOSE — pre-fix entries used the
                        # current mark price, which produced fake PnL on
                        # any position closed via reconcile. The new code
                        # tags fills via manual_close_src; old logs lack it.
                        # Until cleanly tagged, exclude MC from the totals.
                        is_mc = (r.get("exit_reason") == "MANUAL_CLOSE")
                        src   = r.get("manual_close_src", "")
                        if is_mc and src != "binance_fills":
                            mc_trades += 1
                            mc_pnl    += pnl
                            continue
                        s_trades += 1
                        s_pnl    += pnl
                        if pnl >= 0:
                            s_wins += 1
            except Exception:
                continue

        if s_trades > 0:
            by_strat[strat] = {
                "trades": s_trades,
                "wins":   s_wins,
                "pnl":    s_pnl,
            }
        total_trades += s_trades
        total_wins   += s_wins
        total_pnl    += s_pnl

    if total_trades == 0:
        return "  No trades recorded yet.\n  Run the scanner for a few days first."

    total_losses = total_trades - total_wins
    total_wr     = total_wins / total_trades * 100
    avg_pnl      = total_pnl / total_trades

    strat_emoji  = STRAT_EMOJI
    lines = [
        f"  Total    : <b>{total_trades}</b> trades  "
        f"({total_wins}W / {total_losses}L)",
        f"  Win rate : <b>{total_wr:.1f}%</b>",
        f"  Total P&amp;L : <b>{total_pnl:+.3f}%</b>",
        f"  Avg/trade: <b>{avg_pnl:+.3f}%</b>",
        "",
        "  <b>By strategy:</b>",
    ]

    for strat, s in sorted(by_strat.items(),
                            key=lambda x: -x[1]["trades"]):
        wr  = s["wins"] / s["trades"] * 100
        avg = s["pnl"]  / s["trades"]
        em  = strat_emoji.get(strat, "⚪")
        lines.append(
            f"  {em} {strat:<5}  {s['trades']:>3} trades | "
            f"WR {wr:5.1f}% | avg {avg:+.3f}%"
        )

    # Append MANUAL_CLOSE footer if any were segregated
    if mc_trades > 0:
        lines.append("")
        lines.append(
            f"  <i>Excluded: {mc_trades} MANUAL_CLOSE trades "
            f"(unreliable PnL ≈ {mc_pnl:+.2f}%)</i>"
        )

    # ── Binance-direct accurate per-strategy PnL (today + week) ──────────
    # Independent of bot's calculated pnl_pct_100. Matches Binance app exactly.
    try:
        from modules.binance_status import get_per_strategy_pnl
        for label, period in [("Today", "today"), ("Week", "week")]:
            data = get_per_strategy_pnl(period)
            if not data:
                continue
            tot = data.get("TOTAL", {})
            if tot.get("trades", 0) == 0:
                continue
            lines.append("")
            lines.append(f"  ━━━ <b>Binance {label} (live)</b> ━━━")
            lines.append(
                f"  Total: <b>{tot['trades']}</b> trades "
                f"({tot['wins']}W / {tot['trades']-tot['wins']}L) | "
                f"Net: <b>${tot['net_usdt']:+.4f}</b>"
            )
            for strat in PROD_STRATS:
                if strat not in data:
                    continue
                d = data[strat]
                if d["trades"] == 0:
                    continue
                em = strat_emoji.get(strat, "⚪")
                wr_pct = (d["wins"]/d["trades"]*100) if d["trades"] else 0
                lines.append(
                    f"  {em} {strat:<5}  {d['trades']:>3}t | "
                    f"WR {wr_pct:4.0f}% | "
                    f"net ${d['net_usdt']:+.4f}"
                )
            if abs(data.get("unmatched_usdt", 0)) > 0.001:
                lines.append(
                    f"  <i>unmatched: ${data['unmatched_usdt']:+.4f} "
                    f"(events not linked to a strategy log)</i>"
                )
    except Exception as exc:
        lines.append(f"  <i>Binance live: unavailable ({exc})</i>")

    return "\n".join(lines)


# ─── Keyboards ────────────────────────────────────────────────────────────────

# Menu tree. Four categories, each with its own submenu. Every submenu carries a
# Back button; the main menu carries Close. Every state-CHANGING action routes
# through a Confirm/Cancel step (see _confirm_keyboard) so no single tap can
# stop the scanner, flip Paper->Live, or wipe loss caps.
MENU_TITLES = {
    "menu_bot":    "🤖 Bot Controls",
    "menu_trade":  "💹 Trade Controls",
    "menu_analyze": "📊 Analyze",
    "menu_web":    "🌐 Web Server",
}


# ── HTML send with graceful degradation ──────────────────────────────────────
# trade_analyzer builds an HTML message (<b> tags) that also carries literal
# text like "<3%" and "SL < 3%". An unescaped "<" makes Telegram reject the
# WHOLE message with
#     Can't parse entities: unsupported start tag "3%:" at byte offset 557
# and the user saw "Analysis failed: ..." instead of their report.
#
# The literals are escaped at source now, but any future "<" added to that
# module would resurface it. Falling back to tag-stripped plain text turns a
# total failure into a cosmetic one.
async def _reply_html(send, text: str, **kw):
    try:
        return await send(text, parse_mode="HTML", **kw)
    except Exception as exc:
        if "parse entities" not in str(exc).lower():
            raise
        log.warning(f"HTML parse failed, falling back to plain text: {exc}")
        plain = re.sub(r"</?(b|strong|i|em|u|ins|s|strike|del|code|pre)>", "", text)
        plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return await send(plain, **kw)


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 Bot",     callback_data="menu_bot"),
            InlineKeyboardButton("💹 Trade",   callback_data="menu_trade"),
            InlineKeyboardButton("🎛 Strategies", callback_data="strategies"),
        ],
        [
            InlineKeyboardButton("📊 Analyze",    callback_data="menu_analyze"),
            InlineKeyboardButton("🌐 Web Server", callback_data="menu_web"),
        ],
        [
            InlineKeyboardButton("✖ Close",       callback_data="close_menu"),
        ],
    ])


def _bot_controls_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ Start",   callback_data="start_scanner"),
            InlineKeyboardButton("⏹ Stop",    callback_data="stop_scanner"),
        ],
        [
            InlineKeyboardButton("🔄 Restart", callback_data="restart_scanner"),
            InlineKeyboardButton("📊 Status",  callback_data="status"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
    ])


def _trade_controls_keyboard() -> InlineKeyboardMarkup:
    # Mode label carries the CURRENT state so the submenu doubles as a readout.
    # Kept short so three buttons fit one row without truncating on mobile.
    mode = "LIVE" if _live_enabled() else "PAPER"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔀 {mode}",   callback_data="trade_mode"),
            InlineKeyboardButton("💰 Capital",   callback_data="capital"),
            InlineKeyboardButton("⚙️ Leverage",  callback_data="leverage"),
        ],
        [
            InlineKeyboardButton("🎰 Max Slots",  callback_data="max_concurrent"),
            InlineKeyboardButton("📐 Per-Strategy", callback_data="max_per_strategy"),
        ],
        [
            InlineKeyboardButton("⚠️ Reset Caps", callback_data="reset_caps"),
            InlineKeyboardButton("🧹 Clean up",   callback_data="cleanup"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
    ])


def _analyze_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Analyze",   callback_data="analyze"),
            InlineKeyboardButton("📋 Watchlist", callback_data="watchlist"),
            InlineKeyboardButton("🛡 Whitelist", callback_data="whitelist"),
        ],
        [
            InlineKeyboardButton("🚫 Black List", callback_data="blacklist"),
            InlineKeyboardButton("🛠 Optimise",   callback_data="optimize"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
    ])


def _webserver_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ Start",    callback_data="ngrok_start"),
            InlineKeyboardButton("⏹ Stop",     callback_data="ngrok_stop"),
            InlineKeyboardButton("🔗 Get URL", callback_data="ngrok_url"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
    ])


# Kept as an alias so any older call site keeps working.
def _ngrok_keyboard() -> InlineKeyboardMarkup:
    return _webserver_keyboard()


# Which submenu each action belongs to. Cancel and post-action Back return here
# rather than dumping the user at the main menu.
_PARENT = {
    "start_scanner": "menu_bot",   "stop_scanner": "menu_bot",
    "restart_scanner": "menu_bot", "status": "menu_bot",
    "trade_mode": "menu_trade",    "capital": "menu_trade",
    "leverage": "menu_trade",      "reset_caps": "menu_trade",
    "cleanup": "menu_trade",
    "analyze": "menu_analyze",     "watchlist": "menu_analyze",
    "whitelist": "menu_analyze",   "blacklist": "menu_analyze",
    "optimize": "menu_analyze",
    "ngrok_start": "menu_web",     "ngrok_stop": "menu_web",
    "ngrok_url": "menu_web",
}

_SUBMENU_KEYBOARD = {
    "menu_bot":     _bot_controls_keyboard,
    "menu_trade":   _trade_controls_keyboard,
    "menu_analyze": _analyze_keyboard,
    "menu_web":     _webserver_keyboard,
}


def _back_keyboard(parent: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅ Back", callback_data=parent)
    ]])




def _strategies_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu: toggle each strategy on/off via runtime override."""
    try:
        from modules.strategy_overrides import get_all, KNOWN_STRATEGIES
        states = get_all()
    except Exception:
        states = {}
        KNOWN_STRATEGIES = PROD_STRATS
    emoji = STRAT_EMOJI
    rows = []
    for s in KNOWN_STRATEGIES:
        st = states.get(s, {"disabled": False})
        flag = "⛔" if st["disabled"] else "✅"
        rows.append([InlineKeyboardButton(
            f"{emoji.get(s,'⚪')} {s}  {flag}",
            callback_data=f"strat_toggle:{s}"
        )])
    rows.append([
        InlineKeyboardButton("📋 Regime Matrix", callback_data="regime_matrix"),
        InlineKeyboardButton("⬅ Back",          callback_data="back_main"),
    ])
    return InlineKeyboardMarkup(rows)


def _blacklist_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu: show blacklisted coins with remove buttons."""
    try:
        from modules.blacklist import get_all
        data = get_all()
    except Exception:
        data = {}
    rows = []
    for coin in sorted(data):
        strats = data[coin]
        label = f"(all)" if "*" in strats else f"({','.join(strats)})"
        rows.append([InlineKeyboardButton(
            f"🚫 {coin} {label}  ❌",
            callback_data=f"bl_remove:{coin}"
        )])
    rows.append([
        InlineKeyboardButton("➕ Add coin", callback_data="bl_add"),
        InlineKeyboardButton("⬅ Back",     callback_data="menu_analyze"),
    ])
    return InlineKeyboardMarkup(rows)


def _whitelist_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu: show whitelisted coins with remove buttons."""
    try:
        from modules.whitelist import get_all
        data = get_all()
    except Exception:
        data = {}
    rows = []
    for coin in sorted(data):
        strats = data[coin]
        label = f"(all)" if "*" in strats else f"({','.join(strats)})"
        rows.append([InlineKeyboardButton(
            f"🟢 {coin} {label}  ❌",
            callback_data=f"wl_remove:{coin}"
        )])
    rows.append([
        InlineKeyboardButton("➕ Add coin", callback_data="wl_add"),
        InlineKeyboardButton("⬅ Back",     callback_data="menu_analyze"),
    ])
    return InlineKeyboardMarkup(rows)


def _watchlist_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu: show watchlisted coins with remove buttons."""
    try:
        from modules.watchlist import get_watchlist_info
        info = get_watchlist_info()
        data = info.get("manual_adds", [])
    except Exception:
        data = []
    rows = []
    for coin in sorted(data):
        rows.append([InlineKeyboardButton(
            f"🟢 {coin}  ❌",
            callback_data=f"watch_remove:{coin}"
        )])
    rows.append([
        InlineKeyboardButton("➕ Add coin", callback_data="watch_add"),
        InlineKeyboardButton("⬅ Back",     callback_data="menu_analyze"),
    ])
    return InlineKeyboardMarkup(rows)


def _format_regime_matrix() -> str:
    """Build a fixed-width table of REGIME × STRATEGY permissions."""
    try:
        from modules.regime_engine import REGIME_STRATEGY_PERMISSIONS
        from modules.strategy_overrides import get_all
    except Exception as exc:
        return f"  Matrix unavailable: {exc}"

    strats   = PROD_STRATS
    overrides = get_all()
    disabled = [s for s in strats if overrides.get(s, {}).get("disabled")]

    lines = ["<pre>"]
    # Header
    lines.append("Regime         " + "  ".join(f"{s:>4}" for s in strats))
    lines.append("─" * 52)
    for regime, perms in REGIME_STRATEGY_PERMISSIONS.items():
        cells = []
        for s in strats:
            allowed_regime = perms.get(s, False)
            allowed_user   = s not in disabled
            if allowed_regime and allowed_user:
                cells.append("  ✓ ")
            elif allowed_regime and not allowed_user:
                cells.append("  ⛔")          # regime says yes, override blocks
            else:
                cells.append("  · ")
        lines.append(f"{regime:<14} " + "  ".join(cells))
    lines.append("</pre>")

    if disabled:
        lines.append(f"\n⛔ <b>Override-disabled:</b> {', '.join(disabled)}")
        lines.append("<i>(blocked regardless of regime matrix)</i>")
    else:
        lines.append("\n<i>No runtime overrides active.</i>")

    lines.append("\n<i>✓ = active in regime  ⛔ = override-blocked  · = not in matrix</i>")
    return "\n".join(lines)


def _confirm_keyboard(action: str, parent: str | None = None) -> InlineKeyboardMarkup:
    """
    Confirm/Cancel pair.

    Two callback conventions coexist deliberately:
      parent is None -> legacy form, fires "confirm_<action>" and Cancel returns
                        to the main menu. Used by start/stop/restart, whose
                        handlers predate the menu tree.
      parent given   -> fires "do:<action>" and Cancel returns to that submenu,
                        so the user lands back where they were.

    Keep BOTH. An earlier revision of this file defined the two-argument form
    separately, which Python silently shadowed with this one — every two-arg
    call raised TypeError at runtime while the module still imported cleanly.
    """
    if parent is None:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Yes, {action}", callback_data=f"confirm_{action}"),
            InlineKeyboardButton("❌ Cancel",          callback_data="cancel"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"do:{action}"),
        InlineKeyboardButton("❌ Cancel",  callback_data=parent),
    ]])


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text(
        "🤖 <b>csb — Crypto Scanner Control</b>\n\n"
        "Use the buttons below to control the trading bot.",
        reply_markup=_main_keyboard(),
        parse_mode="HTML",
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text(
        "⚙️ <b>Control Panel</b>",
        reply_markup=_main_keyboard(),
        parse_mode="HTML",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Fetch live balance from Binance (fall back to the configured equity)
    live_bal = None
    try:
        from modules.binance_status import get_pnl_summary
        summary = get_pnl_summary()
        if summary:
            live_bal = summary["margin_balance"]
    except Exception:
        pass
    cap_str = f"${live_bal:.4f}" if live_bal is not None else f"${_capital_status()}"

    text = (
        f"📊 <b>Bot Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_service_status()}\n"
        f"💰 Capital  : <b>{cap_str}</b> USDT\n\n"
        f"<b>Loss Caps:</b>\n"
        f"{_loss_status()}\n\n"
        f"🕐 {now}"
    )
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=_main_keyboard())


# ─── Blacklist command ───────────────────────────────────────────────────────

async def cmd_blacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /blacklist                      — show all
    /blacklist add XLM              — block globally
    /blacklist add XLM GRID         — block only for GRID
    /blacklist remove XLM           — remove entirely
    /blacklist remove XLM GRID      — remove only GRID block
    """
    if not _allowed(update):
        return

    from modules.blacklist import add, remove, get_all

    args = (update.message.text or "").split()

    if len(args) < 2:
        data = get_all()
        if not data:
            await update.message.reply_text("📋 Blacklist is empty.")
        else:
            lines = []
            for coin, strats in sorted(data.items()):
                if "*" in strats:
                    lines.append(f"  • {coin} (all)")
                else:
                    lines.append(f"  • {coin} ({','.join(strats)})")
            await update.message.reply_text(
                f"🚫 <b>Blacklist ({len(data)} coins):</b>\n" + "\n".join(lines),
                parse_mode="HTML",
            )
        return

    action = args[1].lower()
    if len(args) < 3:
        await update.message.reply_text(
            "Usage:\n"
            "/blacklist add XLM — block all\n"
            "/blacklist add XLM GRID — block specific\n"
            "/blacklist remove XLM"
        )
        return

    symbol = args[2].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # Optional strategy list: "GRID" or "GRID,BBR"
    strat_list = None
    if len(args) >= 4:
        strat_list = [s.strip().upper() for s in args[3].split(",") if s.strip()]

    if action == "add":
        desc = add(symbol, strat_list)
        await update.message.reply_text(f"🚫 {desc}")
    elif action in ("remove", "rm", "del"):
        if remove(symbol, strat_list):
            if strat_list:
                await update.message.reply_text(f"✅ {symbol} unblocked for {','.join(strat_list)}")
            else:
                await update.message.reply_text(f"✅ {symbol} removed from blacklist.")
        else:
            await update.message.reply_text(f"⚠️ {symbol} not in blacklist.")
    else:
        await update.message.reply_text("Usage: /blacklist add|remove SYMBOL [STRAT,STRAT]")


# ─── Analyze command ─────────────────────────────────────────────────────────

async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /analyze      — 3-day analysis (default)
    /analyze 7    — 7-day analysis
    """
    if not _allowed(update):
        return
    args = (update.message.text or "").split()
    days = 3
    if len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            pass
    days = max(1, min(days, 30))

    await update.message.reply_text("⏳ Running trade analysis...")
    try:
        from modules.trade_analyzer import run
        msg = run(days=days)
        await _reply_html(update.message.reply_text, msg)
    except Exception as exc:
        await update.message.reply_text(f"❌ Analysis failed: {exc}")


# ─── Optimize command ─────────────────────────────────────────────────────────

async def cmd_optimize(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /optimize     — Run weekly optimization on Watchlist and Blacklist
    """
    if not _allowed(update):
        return
    await update.message.reply_text("⏳ Running weekly list optimization...")
    try:
        from modules.list_optimizer import run_optimization
        msg = run_optimization()
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await update.message.reply_text(msg[i:i+4000], parse_mode="HTML")
        else:
            await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"❌ Optimization failed: {exc}")


# ─── Message handler (free-text input for capital) ────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles free-text replies.
    Only active when _waiting_for[chat_id] is set by a button press.
    """
    if not _allowed(update):
        return

    cid  = update.effective_chat.id
    text = (update.message.text or "").strip()

    # ── Capital input ─────────────────────────────────────────────────────────
    if _waiting_for.get(cid) == "capital":
        _waiting_for.pop(cid, None)

        # Validate: must be a positive number, max 6 digits, no symbols
        clean = text.replace(",", "").replace("$", "").strip()
        try:
            amount = float(clean)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid amount. Please type a number only, e.g. <code>1000</code>",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "❌ Amount must be greater than zero.",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            return

        if amount > 1_000_000:
            await update.message.reply_text(
                "❌ Amount exceeds $1,000,000 — please check the value.",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            return

        old = _capital_status()
        ok, err = _set_setting("ACCOUNT_EQUITY_USDT", f"{amount:.2f}")

        if ok:
            await update.message.reply_text(
                f"✅ <b>Capital updated</b>\n\n"
                f"  Old : <s>${old}</s> USDT\n"
                f"  New : <b>${amount:,.2f}</b> USDT\n\n"
                f"✨ Applies on the scanner's next cycle — no restart needed.\n"
                f"<i>In PAPER mode this resets the simulated ledger to the new "
                f"starting balance.</i>",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            log.info(f"Capital changed via Telegram: {old} → {amount:.2f}")
        else:
            await update.message.reply_text(
                f"❌ Could not save: {err}",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
        return

    # ── Max concurrent input ──────────────────────────────────────────────────
    if _waiting_for.get(cid) == "max_concurrent":
        _waiting_for.pop(cid, None)
        try:
            n = int(text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid. Type a whole number, e.g. <code>3</code>",
                parse_mode="HTML", reply_markup=_trade_controls_keyboard())
            return
        if not (1 <= n <= 10):
            await update.message.reply_text(
                f"❌ MAX_CONCURRENT must be between 1 and 10 (got {n}).\n\n"
                f"<i>Above 10 the aggregate margin cap "
                f"(MAX_TOTAL_MARGIN_PCT) would block most entries anyway.</i>",
                parse_mode="HTML", reply_markup=_trade_controls_keyboard())
            return

        old = _max_concurrent_status()
        ok, err = _set_setting("MAX_CONCURRENT", n)
        if not ok:
            await update.message.reply_text(
                f"❌ Could not save: {err}",
                parse_mode="HTML", reply_markup=_trade_controls_keyboard())
            return

        # Re-validate the per-strategy caps against the NEW slot count: caps
        # that were sensible at the old value can be inert or starving now.
        caps = _setting("MAX_PER_STRATEGY")
        note = ""
        if caps:
            _ok, _v, warn = validate_max_per_strategy(caps)
            if warn:
                note = f"\n\n{warn}"

        await update.message.reply_text(
            f"✅ <b>Max Concurrent updated</b>\n\n"
            f"  Old : <s>{old}</s>\n"
            f"  New : <b>{n}</b>\n\n"
            f"✨ Applies on the scanner's next cycle — no restart needed.{note}",
            parse_mode="HTML", reply_markup=_trade_controls_keyboard())
        log.info(f"MAX_CONCURRENT changed via Telegram: {old} -> {n}")
        return

    # ── Per-strategy caps input ───────────────────────────────────────────────
    if _waiting_for.get(cid) == "max_per_strategy":
        _waiting_for.pop(cid, None)
        ok, value, msg = validate_max_per_strategy(text)
        if not ok:
            await update.message.reply_text(
                f"❌ {msg}", parse_mode="HTML",
                reply_markup=_trade_controls_keyboard())
            return

        old = _max_per_strategy_status()
        ok, err = _set_setting("MAX_PER_STRATEGY", value)
        if not ok:
            await update.message.reply_text(
                f"❌ Could not save: {err}",
                parse_mode="HTML", reply_markup=_trade_controls_keyboard())
            return

        await update.message.reply_text(
            f"✅ <b>Per-Strategy Caps updated</b>\n\n"
            f"  Old : <code>{old}</code>\n"
            f"  New : <code>{value}</code>\n\n"
            f"✨ Applies on the scanner's next cycle — no restart needed."
            + (f"\n\n{msg}" if msg else ""),
            parse_mode="HTML", reply_markup=_trade_controls_keyboard())
        log.info(f"MAX_PER_STRATEGY changed via Telegram: {old} -> {value}")
        return

    # ── Leverage input ────────────────────────────────────────────────────────
    if _waiting_for.get(cid) == "leverage":
        _waiting_for.pop(cid, None)

        clean = text.replace("x", "").replace("X", "").strip()
        try:
            amount = int(clean)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid amount. Please type an integer only, e.g. <code>5</code>",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "❌ Leverage must be greater than zero.",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            return

        if amount > 50:
            await update.message.reply_text(
                "❌ Leverage exceeds hard cap of 50x.",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            return

        old = _leverage_status()
        ok, err = _set_setting("GLOBAL_LEVERAGE", amount)

        if ok:
            await update.message.reply_text(
                f"✅ <b>Global Leverage updated</b>\n\n"
                f"  Old : <s>{old}x</s>\n"
                f"  New : <b>{amount}x</b>\n\n"
                f"✨ Applies from the next signal — no restart needed.\n"
                f"<i>Note: the 10% leveraged-loss cap still applies, so wide-SL "
                f"signals may trade below this figure.</i>",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            log.info(f"Leverage changed via Telegram: {old}x → {amount}x")
        else:
            await update.message.reply_text(
                f"❌ Could not save: {err}",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
        return

    # ── Blacklist add input ──────────────────────────────────────────────────
    if _waiting_for.get(cid) == "blacklist_add":
        _waiting_for.pop(cid, None)
        parts = text.split()
        if not parts:
            await update.message.reply_text(
                "❌ No coin specified.", reply_markup=_main_keyboard()
            )
            return
        symbol = parts[0].upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        strat_list = None
        if len(parts) >= 2:
            strat_list = [s.strip().upper() for s in parts[1].split(",") if s.strip()]

        from modules.blacklist import add
        desc = add(symbol, strat_list)
        await update.message.reply_text(
            f"🚫 {desc}",
            reply_markup=_blacklist_keyboard(),
        )
        return

    # ── Whitelist add input ──────────────────────────────────────────────────
    if _waiting_for.get(cid) == "whitelist_add":
        _waiting_for.pop(cid, None)
        parts = text.split()
        if not parts:
            await update.message.reply_text(
                "❌ No coin specified.", reply_markup=_main_keyboard()
            )
            return
        symbol = parts[0].upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        strat_list = None
        if len(parts) >= 2:
            strat_list = [s.strip().upper() for s in parts[1].split(",") if s.strip()]

        from modules.whitelist import add
        desc = add(symbol, strat_list)
        await update.message.reply_text(
            f"🛡 {desc}",
            reply_markup=_whitelist_keyboard(),
        )
        return

    # ── Watchlist add input ──────────────────────────────────────────────────
    if _waiting_for.get(cid) == "watchlist_add":
        _waiting_for.pop(cid, None)
        parts = text.split()
        if not parts:
            await update.message.reply_text(
                "❌ No coin specified.", reply_markup=_main_keyboard()
            )
            return
        symbol = parts[0].upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        from modules.watchlist import add_to_watchlist
        if add_to_watchlist(symbol):
            await update.message.reply_text(
                f"🟢 <b>{symbol}</b> locked into permanent Watchlist.",
                parse_mode="HTML",
                reply_markup=_watchlist_keyboard(),
            )
        else:
            await update.message.reply_text(
                f"{symbol} is already in the Watchlist.",
                reply_markup=_watchlist_keyboard(),
            )
        return

    # Not waiting for anything — show menu
    await update.message.reply_text(
        "Use /menu to open the control panel.",
        reply_markup=_main_keyboard(),
    )


# ─── Callback handler ─────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _allowed(update):
        return

    cid  = update.effective_chat.id
    data = query.data

    # ── Cancel ────────────────────────────────────────────────────────────────
    if data == "cancel":
        _waiting_for.pop(cid, None)
        await query.edit_message_text(
            "❌ Cancelled.", reply_markup=_main_keyboard()
        )

    # ── Submenus ──────────────────────────────────────────────────────────────
    elif data in _SUBMENU_KEYBOARD:
        _waiting_for.pop(cid, None)
        await query.edit_message_text(
            f"{MENU_TITLES[data]}\n━━━━━━━━━━━━━━━━━━━━━\nSelect an option below.",
            parse_mode="HTML",
            reply_markup=_SUBMENU_KEYBOARD[data](),
        )

    # ── Close ─────────────────────────────────────────────────────────────────
    elif data == "close_menu":
        _waiting_for.pop(cid, None)
        await query.edit_message_text(
            "✅ Menu closed.\n\nSend /menu to open it again."
        )

    # ── Trade mode (PAPER <-> LIVE) — ask first ───────────────────────────────
    elif data == "trade_mode":
        cur_live = _live_enabled()
        cur, nxt = ("LIVE", "PAPER") if cur_live else ("PAPER", "LIVE")
        warn = ("\n\n⚠️ <b>LIVE places REAL orders on Binance with real money.</b>"
                if nxt == "LIVE" else
                "\n\n<i>PAPER simulates fills — nothing reaches Binance.</i>")
        await query.edit_message_text(
            f"🔀 <b>Switch trade mode?</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : <b>{cur}</b>\n"
            f"Change to : <b>{nxt}</b>{warn}\n\n"
            f"<i>The scanner is restarted automatically so the change takes "
            f"effect — LIVE_ENABLED is only read at startup.</i>",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("trade_mode", "menu_trade"),
        )

    elif data == "do:trade_mode":
        cur_live = _live_enabled()
        nxt = "false" if cur_live else "true"
        label = "PAPER" if cur_live else "LIVE"
        if not cfg.env_set("LIVE_ENABLED", nxt):
            await query.edit_message_text(
                "❌ Could not write .env — mode unchanged.",
                parse_mode="HTML", reply_markup=_trade_controls_keyboard(),
            )
        else:
            await query.edit_message_text(
                f"⏳ Switched to <b>{label}</b> — restarting scanner...",
                parse_mode="HTML",
            )
            ok, out = _systemctl("restart")
            tail = "✅ Scanner restarted." if ok else f"⚠️ Restart failed: {out}\nRestart manually."
            await query.edit_message_text(
                f"🔀 Trade mode is now <b>{label}</b>.\n\n{tail}",
                parse_mode="HTML", reply_markup=_trade_controls_keyboard(),
            )
        log.info(f"Trade mode switched to {label} via Telegram")

    # ── Web server start/stop — ask first ─────────────────────────────────────
    elif data in ("ngrok_start", "ngrok_stop"):
        act = "start" if data == "ngrok_start" else "stop"
        await query.edit_message_text(
            f"🌐 <b>{act.title()} the web server?</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"This will {act} <code>csb-ngrok.service</code>, which serves the "
            f"public dashboard URL.",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(f"ngrok_{act}", "menu_web"),
        )

    elif data in ("do:ngrok_start", "do:ngrok_stop"):
        action = "start" if data.endswith("start") else "stop"
        await query.edit_message_text(f"⏳ {action.title()}ing web server...", parse_mode="HTML")
        try:
            import subprocess
            proc = subprocess.run(
                ["sudo", "systemctl", action, "csb-ngrok.service"],
                capture_output=True, text=True, timeout=10
            )
            msg = "✅ Started" if action == "start" else "⏹ Stopped"
            if proc.returncode != 0:
                msg = f"❌ Failed to {action} web server: {proc.stderr}"
        except Exception as e:
            msg = f"❌ Error: {e}"
        await query.edit_message_text(
            msg, parse_mode="HTML", reply_markup=_webserver_keyboard(),
        )
        log.info(f"Web server {action} via Telegram")

    # ── Status ────────────────────────────────────────────────────────────────
    elif data == "status":
        now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        live_bal = None
        try:
            from modules.binance_status import get_pnl_summary
            summary = get_pnl_summary()
            if summary:
                live_bal = summary["margin_balance"]
        except Exception:
            pass
        cap_str = f"${live_bal:.4f}" if live_bal is not None else f"${_capital_status()}"
        text = (
            f"📊 <b>Bot Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_service_status()}\n"
            f"💰 Capital  : <b>{cap_str}</b> USDT\n\n"
            f"<b>Loss Caps:</b>\n"
            f"{_loss_status()}\n\n"
            f"🕐 {now}"
        )
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=_main_keyboard())

    # ── Loss caps ─────────────────────────────────────────────────────────────
    elif data == "loss_caps":
        text = (
            f"🛡 <b>Loss Cap Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_loss_status()}\n\n"
            f"Day resets  : 00:00 UTC (05:30 IST)\n"
            f"Week resets : Monday 00:00 UTC\n\n"
            f"<i>Caps apply to LIVE trades only.\n"
            f"Paper trades run uncapped for analysis.</i>"
        )
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=_main_keyboard())

    # ── Trade report ──────────────────────────────────────────────────────────
    elif data == "trade_report":
        now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = (
            f"📈 <b>Trade Report — All Time</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_trade_summary()}\n\n"
            f"🕐 {now}\n"
            f"<i>Live trades (LIVE_ENABLED=true)</i>"
        )
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=_main_keyboard())

    # ── Strategies sub-menu (enable/disable runtime overrides) ────────────────
    elif data == "strategies":
        try:
            from modules.strategy_overrides import get_all
            states = get_all()
        except Exception:
            states = {}
        lines = ["🎛 <b>Strategy Controls</b>", "━━━━━━━━━━━━━━━━━━━━━"]
        any_disabled = False
        for s, st in states.items():
            flag = "⛔ DISABLED" if st["disabled"] else "✅ enabled"
            lines.append(f"  <b>{s}</b> — {flag}")
            if st["disabled"]:
                any_disabled = True
                if st.get("set_at"):
                    lines.append(f"    <i>since {st['set_at'][:16]}</i>")
        if not any_disabled:
            lines.append("\n<i>All strategies follow the regime matrix.</i>")
        lines.append("\n<i>Tap a strategy to toggle.</i>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_strategies_keyboard()
        )

    elif data.startswith("strat_toggle:"):
        sname = data.split(":", 1)[1]
        try:
            from modules.strategy_overrides import toggle
            new_state = toggle(sname)
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Toggle failed: {exc}",
                reply_markup=_main_keyboard()
            )
            return
        verb = "DISABLED" if new_state else "ENABLED"
        emoji = "⛔" if new_state else "✅"
        # Re-render the menu with updated state
        try:
            from modules.strategy_overrides import get_all
            states = get_all()
        except Exception:
            states = {}
        lines = [
            f"{emoji} <b>{sname}</b> {verb}",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]
        for s, st in states.items():
            flag = "⛔ DISABLED" if st["disabled"] else "✅ enabled"
            lines.append(f"  <b>{s}</b> — {flag}")
        lines.append("\n<i>Tap a strategy to toggle.</i>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_strategies_keyboard()
        )

    elif data == "regime_matrix":
        text = (
            "📋 <b>Regime × Strategy Matrix</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_format_regime_matrix()}"
        )
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=_strategies_keyboard()
        )



    # ── Analyze ───────────────────────────────────────────────────────────
    elif data == "analyze":
        await query.edit_message_text("⏳ Running trade analysis...")
        try:
            from modules.trade_analyzer import run
            msg = run(days=3)
            await _reply_html(
                query.edit_message_text, msg,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📊 7-day", callback_data="analyze_7d"),
                        InlineKeyboardButton("📊 14-day", callback_data="analyze_14d"),
                    ],
                    [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
                ])
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Analysis failed: {exc}",
                reply_markup=_main_keyboard()
            )

    elif data in ("analyze_7d", "analyze_14d"):
        days = 7 if data == "analyze_7d" else 14
        await query.edit_message_text(f"⏳ Running {days}-day analysis...")
        try:
            from modules.trade_analyzer import run
            msg = run(days=days)
            await _reply_html(
                query.edit_message_text, msg,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
                ])
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Analysis failed: {exc}",
                reply_markup=_main_keyboard()
            )


    # ── Optimize ─────────────────────────────────────────────────────────────
    elif data == "optimize":
        await query.edit_message_text(
            "🛠 <b>Run list optimisation?</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "This REWRITES the watchlist and blacklist from recent trade "
            "performance. It can take a minute.",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard("optimize", "menu_analyze"),
        )

    elif data == "do:optimize":
        await query.edit_message_text("⏳ Running weekly list optimization...")
        try:
            from modules.list_optimizer import run_optimization
            msg = run_optimization()
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    await query.message.reply_text(msg[i:i+4000], parse_mode="HTML")
            else:
                await query.edit_message_text(
                    msg, parse_mode="HTML",
                    reply_markup=_analyze_keyboard()
                )
            log.info("Weekly list optimization triggered via Telegram")
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Optimization failed: {exc}",
                reply_markup=_analyze_keyboard()
            )

    # ── Blacklist UI ────────────────────────────────────────────────────────
    elif data == "blacklist":
        try:
            from modules.blacklist import get_all
            bl = get_all()
        except Exception:
            bl = {}
        if not bl:
            lines = ["🚫 <b>Blacklist</b>", "━━━━━━━━━━━━━━━━━━━━━", "", "<i>No coins blacklisted.</i>"]
        else:
            lines = ["🚫 <b>Blacklist</b>", "━━━━━━━━━━━━━━━━━━━━━"]
            for coin, strats in sorted(bl.items()):
                s = "all" if "*" in strats else ",".join(strats)
                lines.append(f"  <b>{coin}</b> — {s}")
        lines.append("\n<i>Tap a coin to remove, or add a new one.</i>")
        lines.append("<i>Use /blacklist add COIN STRAT for strategy-specific.</i>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_blacklist_keyboard()
        )

    elif data.startswith("bl_remove:"):
        coin = data.split(":", 1)[1]
        try:
            from modules.blacklist import remove
            remove(coin)
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Remove failed: {exc}",
                reply_markup=_main_keyboard()
            )
            return
        await query.edit_message_text(
            f"✅ <b>{coin}</b> removed from blacklist.",
            parse_mode="HTML",
            reply_markup=_blacklist_keyboard()
        )

    elif data == "bl_add":
        _waiting_for[cid] = "blacklist_add"
        await query.edit_message_text(
            "🚫 <b>Add to Blacklist</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Type the coin name to block globally:\n"
            "  <code>XLM</code> — blocks XLMUSDT for all strategies\n\n"
            "Or specify strategies:\n"
            "  <code>XLM GRID</code> — blocks only for GRID\n",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]),
        )

    # ── Whitelist UI ────────────────────────────────────────────────────────
    elif data == "whitelist":
        # There is no modules/whitelist.py in this codebase. Say so rather than
        # catching ImportError and rendering "no coins whitelisted", which reads
        # as a working-but-empty feature and hides that it was never wired up.
        try:
            from modules.whitelist import get_all
            wl = get_all()
        except ImportError:
            await query.edit_message_text(
                "🛡 <b>Whitelist</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
                "⚠️ Not available — there is no <code>modules/whitelist.py</code> "
                "in this build.\n\nThe nearest working equivalent is "
                "<b>Watchlist → Locked Coins</b>, which pins coins into the scan "
                "list permanently.",
                parse_mode="HTML", reply_markup=_analyze_keyboard(),
            )
            return
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Whitelist failed to load: {exc}",
                reply_markup=_analyze_keyboard(),
            )
            return
        if not wl:
            lines = ["🛡 <b>Whitelist</b>", "━━━━━━━━━━━━━━━━━━━━━", "", "<i>No coins whitelisted. All coins allowed by default.</i>"]
        else:
            lines = ["🛡 <b>Whitelist (Strategy-Specific)</b>", "━━━━━━━━━━━━━━━━━━━━━"]
            for coin, strats in sorted(wl.items()):
                s = "all" if "*" in strats else ",".join(strats)
                lines.append(f"  <b>{coin}</b> — {s}")
        lines.append("\n<i>Tap a coin to remove, or add a new one.</i>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_whitelist_keyboard()
        )

    elif data.startswith("wl_remove:"):
        coin = data.split(":", 1)[1]
        try:
            from modules.whitelist import remove
            remove(coin)
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Remove failed: {exc}",
                reply_markup=_main_keyboard()
            )
            return
        await query.edit_message_text(
            f"✅ <b>{coin}</b> removed from whitelist.",
            parse_mode="HTML",
            reply_markup=_whitelist_keyboard()
        )

    elif data == "wl_add":
        _waiting_for[cid] = "whitelist_add"
        await query.edit_message_text(
            "🛡 <b>Add to Whitelist</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Type the coin name to whitelist:\n"
            "  <code>XLM</code> — whitelist XLMUSDT for all strategies\n\n"
            "Or specify strategies:\n"
            "  <code>XLM GRID</code> — whitelist only for GRID\n",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]),
        )

    # ── Watchlist UI ────────────────────────────────────────────────────────
    elif data == "watchlist":
        try:
            from modules.watchlist import get_watchlist_info
            info = get_watchlist_info()
            manual = info.get("manual_adds", [])
            auto = info.get("auto", [])
        except Exception:
            manual = []
            auto = []
        lines = ["📋 <b>Watchlist (Permanent Scan List)</b>", "━━━━━━━━━━━━━━━━━━━━━"]
        if manual:
            lines.append("<b>Locked Coins:</b>")
            for c in manual:
                lines.append(f"  🟢 <b>{c}</b>")
        else:
            lines.append("<i>No coins manually locked.</i>")
        lines.append(f"\n<i>Auto-tracking {len(auto)} volume leaders.</i>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_watchlist_keyboard()
        )

    elif data.startswith("watch_remove:"):
        coin = data.split(":", 1)[1]
        try:
            from modules.watchlist import remove_from_watchlist
            ok, msg = remove_from_watchlist(coin)
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Remove failed: {exc}",
                reply_markup=_main_keyboard()
            )
            return
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=_watchlist_keyboard()
        )

    elif data == "watch_add":
        _waiting_for[cid] = "watchlist_add"
        await query.edit_message_text(
            "📋 <b>Add to Watchlist</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Type the coin name to lock into the permanent watchlist:\n"
            "  <code>SOL</code> — locks SOLUSDT\n",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]),
        )

    elif data == "back_main":
        await query.edit_message_text(
            "📋 <b>Main Menu</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
            "Select an option below.",
            parse_mode="HTML",
            reply_markup=_main_keyboard()
        )

    # ── Capital — show current + prompt for new ────────────────────────────────
    elif data == "capital":
        cap = _capital_status()
        _waiting_for[cid] = "capital"   # arm the message handler
        await query.edit_message_text(
            f"💰 <b>Trading Capital</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : <b>${cap}</b> USDT\n\n"
            f"Type the new amount in USDT and send it.\n"
            f"Example: <code>1000</code> or <code>2500.50</code>\n\n"
            f"<i>The scanner must be restarted after changing capital.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]),
        )

    # ── Max concurrent positions ──────────────────────────────────────────────
    elif data == "max_concurrent":
        _waiting_for[cid] = "max_concurrent"
        await query.edit_message_text(
            f"🎰 <b>Max Concurrent Positions</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : <b>{_max_concurrent_status()}</b>\n"
            f"Per-strategy caps : <code>{_max_per_strategy_status()}</code>\n\n"
            f"Total positions the bot may hold at once, across all strategies.\n\n"
            f"Type a whole number (1-10) and send it.\n"
            f"Example: <code>3</code>\n\n"
            f"<i>Requires a scanner RESTART — this is read once at startup.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="menu_trade")
            ]]),
        )

    # ── Per-strategy slot caps ────────────────────────────────────────────────
    elif data == "max_per_strategy":
        _waiting_for[cid] = "max_per_strategy"
        known = _known_strategy_ids()
        await query.edit_message_text(
            f"📐 <b>Per-Strategy Slot Caps</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : <code>{_max_per_strategy_status()}</code>\n"
            f"Max concurrent : <b>{_max_concurrent_status()}</b>\n\n"
            f"How many of the concurrent slots each strategy may hold. Stops one "
            f"strategy taking every slot.\n\n"
            f"Loaded strategies: <b>{', '.join(known) if known else 'unavailable'}</b>\n\n"
            f"Type <code>STRATEGY:N</code> pairs separated by commas.\n"
            f"Example: <code>CSM:1,NASOS_V4:2,ELLIOT_V8:2</code>\n\n"
            f"<i>A strategy left out of the list is UNCAPPED.\n"
            f"Requires a scanner RESTART.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="menu_trade")
            ]]),
        )

    # ── Leverage — show current + prompt for new ────────────────────────────────
    elif data == "leverage":
        lev = _leverage_status()
        _waiting_for[cid] = "leverage"   # arm the message handler
        await query.edit_message_text(
            f"⚙️ <b>Global Leverage</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : <b>{lev}x</b>\n\n"
            f"Type the new leverage multiplier (max 50) and send it.\n"
            f"Example: <code>5</code> or <code>10</code>\n\n"
            f"<i>The scanner must be restarted after changing leverage.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]),
        )

    # ── Start ─────────────────────────────────────────────────────────────────
    elif data == "start_scanner":
        await query.edit_message_text(
            "▶ <b>Start scanner?</b>\n\n"
            "This will start <code>csb.service</code>.\n"
            "If already running, this has no effect.",
            reply_markup=_confirm_keyboard("start"),
            parse_mode="HTML",
        )

    # ── Stop ──────────────────────────────────────────────────────────────────
    elif data == "stop_scanner":
        await query.edit_message_text(
            "⏹ <b>Stop scanner?</b>\n\n"
            "Sends SIGTERM → graceful shutdown.\n"
            "Open positions will be closed at market.\n"
            "Session log will be written.",
            reply_markup=_confirm_keyboard("stop"),
            parse_mode="HTML",
        )

    # ── Restart ───────────────────────────────────────────────────────────────
    elif data == "restart_scanner":
        await query.edit_message_text(
            "🔄 <b>Restart scanner?</b>\n\n"
            "Stops then starts <code>csb.service</code>.\n"
            "Open positions in memory will be lost.\n"
            "Loss cap state is preserved (written to disk).",
            reply_markup=_confirm_keyboard("restart"),
            parse_mode="HTML",
        )

    # ── Confirm: start ────────────────────────────────────────────────────────
    elif data == "confirm_start":
        await query.edit_message_text(
            "⏳ Starting <code>csb.service</code>...", parse_mode="HTML"
        )
        ok, msg = _systemctl("start")
        if ok:
            await query.message.reply_text(
                "✅ <b>Scanner started.</b>",
                parse_mode="HTML", reply_markup=_main_keyboard(),
            )
        else:
            await query.message.reply_text(
                f"❌ <b>Start failed.</b>\n<code>{msg}</code>",
                parse_mode="HTML",
            )
        log.info(f"Start triggered via Telegram: {msg}")

    # ── Confirm: stop ─────────────────────────────────────────────────────────
    elif data == "confirm_stop":
        await query.edit_message_text(
            "⏳ <b>Stopping scanner...</b>\n\n"
            "Closing all open positions and writing session log.\n"
            "<i>This may take up to 60 seconds — please wait.</i>",
            parse_mode="HTML"
        )
        ok, msg = _systemctl("stop")
        if ok:
            await query.message.reply_text(
                "⏹ <b>Scanner stopped.</b>\n"
                "All positions closed. Session log written.",
                parse_mode="HTML", reply_markup=_main_keyboard(),
            )
        else:
            await query.message.reply_text(
                f"❌ <b>Stop failed.</b>\n<code>{msg}</code>",
                parse_mode="HTML",
            )
        log.info(f"Stop triggered via Telegram: {msg}")

    # ── Confirm: restart ──────────────────────────────────────────────────────
    elif data == "confirm_restart":
        await query.edit_message_text(
            "⏳ <b>Restarting scanner...</b>\n\n"
            "Stopping gracefully then starting fresh.\n"
            "<i>This may take up to 60 seconds — please wait.</i>",
            parse_mode="HTML"
        )
        ok, msg = _systemctl("restart")
        if ok:
            await query.message.reply_text(
                "🔄 <b>Scanner restarted.</b>\n"
                "Re-fetching data and resuming scan.",
                parse_mode="HTML", reply_markup=_main_keyboard(),
            )
        else:
            await query.message.reply_text(
                f"❌ <b>Restart failed.</b>\n<code>{msg}</code>",
                parse_mode="HTML",
            )
        log.info(f"Restart triggered via Telegram: {msg}")

    # ── Reset caps — show current state + options ─────────────────────────────
    elif data == "reset_caps":
        await query.edit_message_text(
            f"⚠️ <b>Reset Loss Caps</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Current state:</b>\n"
            f"{_loss_status()}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Reset Day only",    callback_data="confirm_reset_day")],
                [InlineKeyboardButton("📅📅 Reset Day + Week", callback_data="confirm_reset_all")],
                [InlineKeyboardButton("❌ Cancel",             callback_data="cancel")],
            ]),
        )

    # ── Confirm: reset day only ───────────────────────────────────────────────
    elif data == "confirm_reset_day":
        ok, result = _reset_loss_tracker("day")
        if ok:
            # Auto-restart scanner so it immediately reads clean tracker
            restart_ok, restart_msg = _systemctl("restart")
            restart_note = (
                "🔄 Scanner restarted — clean tracker active."
                if restart_ok else
                f"⚠️ Scanner restart failed: {restart_msg}\nRestart manually with 🔄 Restart."
            )
            await query.edit_message_text(
                f"✅ <b>Day loss cap reset.</b>\n\n"
                f"<code>{result}</code>\n\n"
                f"{restart_note}",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            log.info(f"Loss tracker day reset via Telegram. Scanner restart: {restart_ok}")
        else:
            await query.edit_message_text(
                f"❌ <b>Reset failed.</b>\n<code>{result}</code>",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )

    # ── Confirm: reset day + week ─────────────────────────────────────────────
    elif data == "confirm_reset_all":
        ok, result = _reset_loss_tracker("all")
        if ok:
            restart_ok, restart_msg = _systemctl("restart")
            restart_note = (
                "🔄 Scanner restarted — clean tracker active."
                if restart_ok else
                f"⚠️ Scanner restart failed: {restart_msg}\nRestart manually with 🔄 Restart."
            )
            await query.edit_message_text(
                f"✅ <b>Day + Week loss caps reset.</b>\n\n"
                f"<code>{result}</code>\n\n"
                f"{restart_note}",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            log.info(f"Loss tracker full reset via Telegram. Scanner restart: {restart_ok}")
        else:
            await query.edit_message_text(
                f"❌ <b>Reset failed.</b>\n<code>{result}</code>",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )

    # ── Cleanup ───────────────────────────────────────────────────────────────
    elif data == "cleanup":
        await query.edit_message_text(
            "🧹 <b>Cleanup</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Archive old logs, trim ML JSONL, prune expired cooldowns.\n\n"
            "<i>Safe to run while bot is live.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Run Cleanup", callback_data="confirm_cleanup")],
                [InlineKeyboardButton("❌ Cancel",       callback_data="cancel")],
            ]),
        )

    elif data == "confirm_cleanup":
        await query.edit_message_text("⏳ Running cleanup...", parse_mode="HTML")
        report = _run_cleanup()
        await query.edit_message_text(
            f"🧹 <b>Cleanup Done</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{report}",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )

    # ── Ngrok Control ─────────────────────────────────────────────────────────
    elif data == "ngrok_control":
        await query.edit_message_text(
            "🌐 <b>Ngrok Tunnel Control</b>\n\n"
            "Manage the web dashboard public URL.",
            parse_mode="HTML",
            reply_markup=_ngrok_keyboard(),
        )

    elif data == "ngrok_url":
        await query.edit_message_text("⏳ Fetching URL...", parse_mode="HTML")
        url_text = "No active tunnel found."
        try:
            from ngrok_runner import get_active_ngrok_url
            url = get_active_ngrok_url(force_refresh=True)
            if url:
                url_text = f"🔗 <b>Active URL:</b>\n{url.replace('http://', 'https://')}"
            else:
                url_text = "No active tunnel found for port 8102."
        except Exception as e:
            url_text = f"❌ <b>Error fetching URL:</b>\n{e}\n\n<i>Make sure the ngrok service is running.</i>"
        
        await query.edit_message_text(
            url_text,
            parse_mode="HTML",
            reply_markup=_ngrok_keyboard(),
        )

        log.info("Cleanup triggered via Telegram")


# ─── Cleanup helper ───────────────────────────────────────────────────────────

def _run_cleanup() -> str:
    """
    Inline cleanup: trim ML JSONL, archive old logs, prune expired cooldowns.
    Returns a human-readable summary string for Telegram.
    """
    import shutil
    from pathlib import Path
    from datetime import datetime, timezone, timedelta

    _PROJECT = Path(__file__).parent
    lines    = []


    # ── 2. Archive session logs older than 14 days ────────────────────────────
    session_dir = _PROJECT / "logs" / "live"
    arch_dir    = session_dir / "archive"
    cutoff      = datetime.now(timezone.utc) - timedelta(days=14)
    archived    = 0
    if session_dir.exists():
        arch_dir.mkdir(exist_ok=True)
        for f in session_dir.glob("session_*.json"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    shutil.move(str(f), arch_dir / f.name)
                    archived += 1
            except Exception:
                pass
    lines.append(f"📁 Session logs archived: {archived}")

    # ── 3. Prune expired cooldown entries ─────────────────────────────────────
    cooldown_path = _PROJECT / "data" / "loss_cooldown.json"
    pruned = 0
    if cooldown_path.exists():
        try:
            import json
            now = datetime.now(timezone.utc)
            data = json.loads(cooldown_path.read_text())
            before = len(data)
            # Cooldown stores time-of-loss (past); active if < 30 min ago
            data = {
                k: v for k, v in data.items()
                if (now - datetime.fromisoformat(v)).total_seconds() < 30 * 60
            }
            pruned = before - len(data)
            if pruned:
                cooldown_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            lines.append(f"⚠️ Cooldowns: {exc}")
    lines.append(f"🕐 Expired cooldowns pruned: {pruned}")

    # ── 4. Prune expired blacklist entries ────────────────────────────────────
    try:
        from modules.symbol_blacklist import get_blacklist  # triggers auto-clean
        active = get_blacklist()
        lines.append(f"🚫 Blacklist active: {len(active)} symbol(s)")
    except Exception as exc:
        lines.append(f"⚠️ Blacklist: {exc}")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("optimize",  cmd_optimize))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # MessageHandler must be last — catches all text not handled above
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram control bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()


