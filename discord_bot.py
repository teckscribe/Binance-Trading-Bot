"""
discord_bot.py
CSB — Discord Control Panel for Binance USDM Futures Bot.

Runs as a separate systemd service (csb-discord.service).
Replaces telegram_bot.py functionality via Discord slash commands + button views.

CRITICAL: All button callbacks must defer() first, then use followup.send().

Slash commands:
  /menu         — main control panel
  /status       — service status + loss caps
  /trades       — all-time trade report
  /blacklist    — view/add/remove blacklisted coins
  /analyze      — run trade analysis (default 3d)
  /restart      — restart scanner
  /stop         — stop scanner
  /start        — start scanner
  /resetcaps    — reset loss caps
  /cleanup      — archive logs + prune cooldowns
  /capital      — view/change ACCOUNT_EQUITY_USDT
  /strategies   — toggle strategies on/off
  /regime       — show regime × strategy matrix

Env vars:
  DISCORD_BOT_TOKEN
  DISCORD_GUILD_ID
  DISCORD_CHANNEL_ID
"""

import os
import re
import json
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from glob import glob

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID   = int(os.getenv("DISCORD_GUILD_ID", "0"))
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))


# Strategy IDs come from the factory — never hardcode a list here (hardcoded
# copies drifted out of sync in six other places, each failing silently).
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
SERVICE    = "csb"

_BOT_DIR  = os.path.dirname(os.path.abspath(__file__))
ENV_FILE  = os.path.join(_BOT_DIR, ".env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("DiscordBot")


# ─── .env read/write ──────────────────────────────────────────────────────────

def _read_env_value(key: str) -> str:
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _write_env_value(key: str, value: str) -> bool:
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}\n")
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        log.info(f".env updated: {key}={value}")
        return True
    except Exception as exc:
        log.error(f".env write failed: {exc}")
        return False


# ─── Systemctl helpers ────────────────────────────────────────────────────────

def _systemctl(action: str) -> tuple[bool, str]:
    try:
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
        return f"{emoji} Scanner: **{state}**"
    except Exception:
        return "⚪ Scanner: **unknown**"


def _capital_status() -> str:
    val = _read_env_value("ACCOUNT_EQUITY_USDT")
    return val if val else "not set"

def _max_concurrent_status() -> str:
    val = _read_env_value("MAX_CONCURRENT")
    return val if val else "3 (default)"


def _max_per_strategy_status() -> str:
    val = _read_env_value("MAX_PER_STRATEGY")
    return val if val else "not set (unlimited per strategy)"


def _known_strategy_ids() -> list:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        return [s.STRATEGY_ID for s in StrategyFactory.get_all()]
    except Exception:
        return []


def validate_max_per_strategy(text: str):
    """
    Validate "CSM:1,NASOS_V4:2,ELLIOT_V8:2" -> (ok, normalised, message).

    The message carries WARNINGS even when ok is True: a cap above
    MAX_CONCURRENT is inert, a total below it leaves slots that can never fill,
    and an omitted strategy is UNCAPPED. All three are easy to write by accident.
    """
    known = _known_strategy_ids()
    pairs, seen = [], set()

    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if ":" not in chunk:
            return False, "", f"`{chunk}` is not STRATEGY:NUMBER. Example: `CSM:1,NASOS_V4:2`"
        sid, _, raw = chunk.partition(":")
        sid = sid.upper()
        if known and sid not in known:
            return False, "", f"Unknown strategy **{sid}**.\nAvailable: {', '.join(known)}"
        if sid in seen:
            return False, "", f"**{sid}** appears more than once."
        try:
            n = int(raw)
        except ValueError:
            return False, "", f"`{raw}` after {sid}: is not a whole number."
        if n < 0:
            return False, "", f"{sid} cap cannot be negative."
        seen.add(sid)
        pairs.append((sid, n))

    if not pairs:
        return False, "", "Nothing to set. Example: `CSM:1,NASOS_V4:2,ELLIOT_V8:2`"

    value = ",".join(f"{s}:{n}" for s, n in pairs)
    warn = []
    try:
        mc = int(_read_env_value("MAX_CONCURRENT") or 3)
    except ValueError:
        mc = 3
    over = [f"{s}:{n}" for s, n in pairs if n > mc]
    if over:
        warn.append(f"⚠️ {', '.join(over)} exceed MAX_CONCURRENT={mc} — those caps can never bind.")
    total = sum(n for _, n in pairs)
    if total < mc:
        warn.append(f"⚠️ Caps total {total} but MAX_CONCURRENT={mc} — "
                    f"{mc - total} slot(s) can never be filled.")
    missing = [s for s in known if s not in seen]
    if missing:
        warn.append(f"⚠️ Not listed: {', '.join(missing)} — unlisted strategies are UNCAPPED.")
    return True, value, "\n".join(warn)


def _leverage_status() -> str:
    val = _read_env_value("GLOBAL_LEVERAGE")
    return val if val else "Auto"


# ─── Loss status ──────────────────────────────────────────────────────────────

def _loss_status() -> str:
    try:
        try:
            from modules.risk_engine    import DAILY_LOSS_CAP, WEEKLY_LOSS_CAP
            from modules.binance_status import get_pnl_summary
        except Exception:
            DAILY_LOSS_CAP, WEEKLY_LOSS_CAP = -0.05, -0.10
            get_pnl_summary = None
        daily_cap_pct  = DAILY_LOSS_CAP  * 100
        weekly_cap_pct = WEEKLY_LOSS_CAP * 100

        fpath = os.path.join(_BOT_DIR, "data", "loss_tracker.json")
        week_pnl = 0.0
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    week_pnl = json.load(f).get("week_pnl", 0.0) * 100
            except Exception:
                pass

        summary = get_pnl_summary() if get_pnl_summary else None
        if summary is not None:
            day_pnl     = summary["day_pnl_pct"]
            day_usdt    = summary["day_pnl_usdt"]
            margin_bal  = summary["margin_balance"]
            unrealized  = summary["unrealized_pnl"]
            day_ok      = "✅" if day_pnl > daily_cap_pct  else "🔴 CAP HIT"
            week_ok     = "✅" if week_pnl > weekly_cap_pct else "🔴 CAP HIT"
            return (
                f"{day_ok}  Day  P&L : **{day_pnl:+.2f}%**  "
                f"(${day_usdt:+.4f}, cap {daily_cap_pct:+.0f}%)\n"
                f"{week_ok}  Week P&L : **{week_pnl:+.2f}%**  "
                f"(cap {weekly_cap_pct:+.0f}%)\n"
                f"💰 Wallet : **${margin_bal:.4f}**  "
                f"(unrealized: ${unrealized:+.4f})\n"
                f"*source: binance live (day) / tracker (week)*"
            )

        if not os.path.exists(fpath):
            return "No loss data yet"
        with open(fpath) as f:
            data = json.load(f)
        day_pnl = data.get("day_pnl", 0.0) * 100
        day_ok  = "✅" if day_pnl  > daily_cap_pct  else "🔴 CAP HIT"
        week_ok = "✅" if week_pnl > weekly_cap_pct else "🔴 CAP HIT"
        return (
            f"{day_ok}  Day  P&L : **{day_pnl:+.2f}%**  "
            f"(cap {daily_cap_pct:+.0f}%)\n"
            f"{week_ok}  Week P&L : **{week_pnl:+.2f}%**  "
            f"(cap {weekly_cap_pct:+.0f}%)\n"
            f"*source: closed-trade tracker (binance unreachable)*"
        )
    except Exception as exc:
        return f"Error reading loss data: {exc}"


# ─── Trade summary ────────────────────────────────────────────────────────────

def _trade_summary() -> str:
    STRATS     = PROD_STRATS
    strat_dir  = os.path.join(_BOT_DIR, "logs", "strategies")

    total_trades = 0
    total_wins   = 0
    total_pnl    = 0.0
    mc_trades    = 0
    mc_pnl       = 0.0
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
                        if abs(pnl) > 50.0:
                            continue
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
            by_strat[strat] = {"trades": s_trades, "wins": s_wins, "pnl": s_pnl}
        total_trades += s_trades
        total_wins   += s_wins
        total_pnl    += s_pnl

    if total_trades == 0:
        return "No trades recorded yet.\nRun the scanner for a few days first."

    total_losses = total_trades - total_wins
    total_wr     = total_wins / total_trades * 100
    avg_pnl      = total_pnl / total_trades

    strat_emoji  = STRAT_EMOJI
    lines = [
        f"Total    : **{total_trades}** trades  ({total_wins}W / {total_losses}L)",
        f"Win rate : **{total_wr:.1f}%**",
        f"Total P&L : **{total_pnl:+.3f}%**",
        f"Avg/trade: **{avg_pnl:+.3f}%**",
        "",
        "**By strategy:**",
    ]

    for strat, s in sorted(by_strat.items(), key=lambda x: -x[1]["trades"]):
        wr  = s["wins"] / s["trades"] * 100
        avg = s["pnl"]  / s["trades"]
        em  = strat_emoji.get(strat, "⚪")
        lines.append(f"{em} {strat:<5}  {s['trades']:>3} trades | WR {wr:5.1f}% | avg {avg:+.3f}%")

    if mc_trades > 0:
        lines.append("")
        lines.append(f"*Excluded: {mc_trades} MANUAL_CLOSE trades (≈ {mc_pnl:+.2f}%)*")

    # Binance-direct per-strategy PnL
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
            lines.append(f"━━━ **Binance {label} (live)** ━━━")
            lines.append(
                f"Total: **{tot['trades']}** trades "
                f"({tot['wins']}W / {tot['trades']-tot['wins']}L) | "
                f"Net: **${tot['net_usdt']:+.4f}**"
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
                    f"{em} {strat:<5}  {d['trades']:>3}t | "
                    f"WR {wr_pct:4.0f}% | net ${d['net_usdt']:+.4f}"
                )
            if abs(data.get("unmatched_usdt", 0)) > 0.001:
                lines.append(f"*unmatched: ${data['unmatched_usdt']:+.4f}*")
    except Exception as exc:
        lines.append(f"*Binance live: unavailable ({exc})*")

    return "\n".join(lines)


# ─── Reset loss tracker ──────────────────────────────────────────────────────

def _reset_loss_tracker(scope: str = "day") -> tuple[bool, str]:
    fpath = os.path.join(_BOT_DIR, "data", "loss_tracker.json")
    try:
        now   = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week  = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

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
        data["day_start_equity"] = 0.0

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


# ─── Regime matrix ────────────────────────────────────────────────────────────

def _format_regime_matrix() -> str:
    try:
        from modules.regime_engine import REGIME_STRATEGY_PERMISSIONS
        from modules.strategy_overrides import get_all
    except Exception as exc:
        return f"Matrix unavailable: {exc}"

    strats    = PROD_STRATS
    overrides = get_all()
    disabled  = [s for s in strats if overrides.get(s, {}).get("disabled")]

    lines = ["```"]
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
                cells.append("  ⛔")
            else:
                cells.append("  · ")
        lines.append(f"{regime:<14} " + "  ".join(cells))
    lines.append("```")

    if disabled:
        lines.append(f"\n⛔ **Override-disabled:** {', '.join(disabled)}")
        lines.append("*(blocked regardless of regime matrix)*")
    else:
        lines.append("\n*No runtime overrides active.*")

    lines.append("\n*✓ = active in regime  ⛔ = override-blocked  · = not in matrix*")
    return "\n".join(lines)


# ─── Cleanup ──────────────────────────────────────────────────────────────────

def _run_cleanup() -> str:
    import shutil
    from pathlib import Path

    _PROJECT = Path(__file__).parent
    lines    = []

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

    cooldown_path = _PROJECT / "data" / "loss_cooldown.json"
    pruned = 0
    if cooldown_path.exists():
        try:
            now = datetime.now(timezone.utc)
            data = json.loads(cooldown_path.read_text())
            before = len(data)
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

    try:
        from modules.symbol_blacklist import get_blacklist
        active = get_blacklist()
        lines.append(f"🚫 Blacklist active: {len(active)} symbol(s)")
    except Exception as exc:
        lines.append(f"⚠️ Blacklist: {exc}")

    return "\n".join(lines)


# ─── Discord Bot ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)

GUILD_OBJ = discord.Object(id=GUILD_ID)


def _allowed(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == CHANNEL_ID


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "❌ Commands only work in the designated channel.", ephemeral=True
    )


# ─── Views ────────────────────────────────────────────────────────────────────

# ── Analysis output: HTML -> Discord markdown ────────────────────────────────
# modules/trade_analyzer builds ONE message for both bots, in Telegram's HTML
# flavour. Discord renders no HTML, so the raw text showed literal tags:
#     "📊 <b>Trade Analysis (14d)</b>"
# and, once the angle brackets in that module were escaped for Telegram, also
# literal entities:
#     "  &lt;3%: 53t avg=+0.92% WR=62%"
#
# Convert rather than strip, so bold survives. Tags are replaced BEFORE the
# entities are unescaped -- otherwise an unescaped "&lt;b&gt;" would turn into
# a tag and get eaten by the stray-tag sweep below.
_DISCORD_TAGS = [
    (r"</?b>", "**"), (r"</?strong>", "**"),
    (r"</?i>", "*"), (r"</?em>", "*"),
    (r"</?u>", "__"), (r"</?ins>", "__"),
    (r"</?code>", "`"), (r"</?pre>", "```"),
    (r"</?s>", "~~"), (r"</?strike>", "~~"), (r"</?del>", "~~"),
]


def _html_to_discord(text: str) -> str:
    for pat, rep in _DISCORD_TAGS:
        text = re.sub(pat, rep, text)
    text = re.sub(r"<[^>\n]{1,20}>", "", text)          # any stray tag
    return (text.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&"))


async def _send_analysis(interaction, msg: str, view=None):
    """
    Send an analysis report to Discord.

    Converts the HTML the analyzer emits, then chunks on LINE boundaries.
    The previous `msg[i:i+2000]` sliced blindly and could cut a word, a number
    or a markdown pair in half; Discord's hard limit is 2000, so 1900 leaves
    room without needing an exact count.
    """
    msg = _html_to_discord(msg)
    if len(msg) <= 1900:
        if view is not None:
            return await interaction.followup.send(msg, view=view)
        return await interaction.followup.send(msg)

    chunks, cur = [], ""
    for line in msg.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            if cur:
                chunks.append(cur)
            cur = line[:1900]
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)

    for n, ch in enumerate(chunks):
        if n == len(chunks) - 1 and view is not None:
            await interaction.followup.send(ch, view=view)
        else:
            await interaction.followup.send(ch)


class BlacklistAddModal(discord.ui.Modal, title="Add to Blacklist"):
    coin = discord.ui.TextInput(label="Coin symbol (e.g. XLM)", placeholder="XLM", max_length=20)
    strategy = discord.ui.TextInput(label="Strategy (optional, e.g. GRID)", required=False, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        # Defer then followup so the modal submit doesn't hit the 3s limit
        await interaction.response.defer()
        symbol = self.coin.value.strip().upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        strat_list = [self.strategy.value.strip().upper()] if self.strategy.value.strip() else None
        try:
            from modules.blacklist import add
            desc = add(symbol, strat_list)
            await interaction.followup.send(f"🚫 {desc}", view=MainMenuView())
        except Exception as exc:
            await interaction.followup.send(f"❌ Add failed: {exc}", view=MainMenuView())


class WatchlistAddModal(discord.ui.Modal, title="Add to Watchlist"):
    coin = discord.ui.TextInput(label="Coin symbol (e.g. SOL)", placeholder="SOL", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        symbol = self.coin.value.strip().upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        try:
            from modules.watchlist import add_to_watchlist
            if add_to_watchlist(symbol):
                await interaction.followup.send(f"🟢 **{symbol}** locked into permanent Watchlist.", view=WatchlistView())
            else:
                await interaction.followup.send(f"{symbol} is already in the Watchlist.", view=WatchlistView())
        except Exception as exc:
            await interaction.followup.send(f"❌ Add failed: {exc}", view=WatchlistView())


class WatchlistRemoveModal(discord.ui.Modal, title="Remove from Watchlist"):
    coin = discord.ui.TextInput(label="Coin symbol (e.g. SOL)", placeholder="SOL", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        symbol = self.coin.value.strip().upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        try:
            from modules.watchlist import remove_from_watchlist
            ok, msg = remove_from_watchlist(symbol)
            await interaction.followup.send(msg, view=WatchlistView())
        except Exception as exc:
            await interaction.followup.send(f"❌ Remove failed: {exc}", view=WatchlistView())


class WatchlistView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add Coin", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WatchlistAddModal())

    @discord.ui.button(label="➖ Remove Coin", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WatchlistRemoveModal())

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            from modules.watchlist import get_watchlist_info
            info = get_watchlist_info()
            manual = info.get("manual_adds", [])
            auto = info.get("auto", [])
            lines = ["📋 **Watchlist (Permanent Scan List)**", "━━━━━━━━━━━━━━━━━━━━━"]
            if manual:
                lines.append("**Locked Coins:**")
                for c in manual:
                    lines.append(f"🟢 **{c}**")
            else:
                lines.append("*No coins manually locked.*")
            lines.append(f"\n*Auto-tracking {len(auto)} volume leaders.*")
            await interaction.followup.send("\n".join(lines), view=WatchlistView())
        except Exception as exc:
            await interaction.followup.send(f"Error: {exc}", view=WatchlistView())

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("📋 **Main Menu**", view=MainMenuView())


class NgrokView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="▶ Start Ngrok", style=discord.ButtonStyle.primary, row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⏳ Starting ngrok...")
        try:
            import subprocess
            proc = subprocess.run(["sudo", "systemctl", "start", "csb-ngrok.service"], capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                await interaction.followup.send("✅ Ngrok started.", view=NgrokView())
            else:
                await interaction.followup.send(f"❌ Failed to start: {proc.stderr}", view=NgrokView())
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", view=NgrokView())

    @discord.ui.button(label="⏹ Stop Ngrok", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⏳ Stopping ngrok...")
        try:
            import subprocess
            proc = subprocess.run(["sudo", "systemctl", "stop", "csb-ngrok.service"], capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                await interaction.followup.send("⏹ Ngrok stopped.", view=NgrokView())
            else:
                await interaction.followup.send(f"❌ Failed to stop: {proc.stderr}", view=NgrokView())
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", view=NgrokView())

    @discord.ui.button(label="🔗 Get URL", style=discord.ButtonStyle.secondary, row=1)
    async def url_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⏳ Fetching URL...")
        try:
            from ngrok_runner import get_active_ngrok_url
            url = get_active_ngrok_url(force_refresh=True)
            if url:
                url_text = f"🔗 **Active URL:**\n{url.replace('http://', 'https://')}"
            else:
                url_text = "No active tunnel found for port 8102."
            await interaction.followup.send(url_text, view=NgrokView())
        except Exception as e:
            await interaction.followup.send(f"❌ **Error fetching URL:**\n{e}\n\n*Make sure ngrok is running.*", view=NgrokView())

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("📋 **Main Menu**", view=MainMenuView())



# ─── Menu tree ────────────────────────────────────────────────────────────────
# Four categories, each its own View. Every submenu carries Back; the main menu
# carries Close. Every state-CHANGING action routes through a Confirm/Cancel
# step, so no single tap can stop the scanner, flip Paper->Live or wipe caps.

class MainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🤖 Bot Controls", style=discord.ButtonStyle.primary, row=0)
    async def bot_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🤖 **Bot Controls**\n━━━━━━━━━━━━━━━━━━━━━\nSelect an option below.",
            view=BotControlsView(),
        )

    @discord.ui.button(label="💹 Trade Controls", style=discord.ButtonStyle.primary, row=0)
    async def trade_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "💹 **Trade Controls**\n━━━━━━━━━━━━━━━━━━━━━\nSelect an option below.",
            view=TradeControlsView(),
        )

    @discord.ui.button(label="🎛 Strategies", style=discord.ButtonStyle.primary, row=0)
    async def strategies_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            from modules.strategy_overrides import get_all
            states = get_all()
        except Exception:
            states = {}
        lines = ["🎛 **Strategy Controls**", "━━━━━━━━━━━━━━━━━━━━━"]
        for s, st in states.items():
            flag = "⛔ DISABLED" if st["disabled"] else "✅ enabled"
            lines.append(f"**{s}** — {flag}")
        lines.append("\n*Tap a strategy below to toggle.*")
        await interaction.followup.send("\n".join(lines), view=StrategyView())

    @discord.ui.button(label="📊 Analyze", style=discord.ButtonStyle.primary, row=1)
    async def analyze_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "📊 **Analyze**\n━━━━━━━━━━━━━━━━━━━━━\nSelect an option below.",
            view=AnalyzeMenuView(),
        )

    @discord.ui.button(label="🌐 Web Server", style=discord.ButtonStyle.primary, row=1)
    async def web_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🌐 **Web Server**\n━━━━━━━━━━━━━━━━━━━━━\nManage the public dashboard URL.",
            view=WebServerView(),
        )

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.secondary, row=2)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("✅ Menu closed. Use `/menu` to reopen.")


class BackButton(discord.ui.Button):
    """Back to the main menu. Shared by every submenu.

    `row` is passed explicitly so Back sits directly beneath the content rather
    than being pushed to the bottom of a 5-row grid.
    """
    def __init__(self, row: int = 2):
        super().__init__(label="⬅ Back", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.followup.send(
            "📋 **Main Menu**\n━━━━━━━━━━━━━━━━━━━━━\nSelect an option below.",
            view=MainMenuView(),
        )


class BotControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BackButton(row=1))

    @discord.ui.button(label="▶ Start", style=discord.ButtonStyle.success, row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "▶ **Start scanner?**\nThis will start `csb.service`.",
            view=ConfirmView("start"),
        )

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "⏹ **Stop scanner?**\nGraceful shutdown — closes all positions.",
            view=ConfirmView("stop"),
        )

    @discord.ui.button(label="🔄 Restart", style=discord.ButtonStyle.primary, row=0)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🔄 **Restart scanner?**\nStop then start. Loss cap state preserved.",
            view=ConfirmView("restart"),
        )

    @discord.ui.button(label="📊 Status", style=discord.ButtonStyle.secondary, row=0)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        live_bal = None
        try:
            from modules.binance_status import get_pnl_summary
            summary = get_pnl_summary()
            if summary:
                live_bal = summary["margin_balance"]
        except Exception:
            pass
        cap_str = f"${live_bal:.4f}" if live_bal is not None else f"${_capital_status()}"
        mode = "LIVE" if _read_env_value("LIVE_ENABLED").lower() == "true" else "PAPER"
        text = (
            f"📊 **Bot Status**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_service_status()}\n"
            f"🔀 Mode     : **{mode}**\n"
            f"💰 Capital  : **{cap_str}** USDT\n"
            f"⚙️ Leverage : **{_leverage_status()}x**\n\n"
            f"**Loss Caps:**\n"
            f"{_loss_status()}\n\n"
            f"🕐 {now}"
        )
        await interaction.followup.send(text, view=BotControlsView())


class TradeControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BackButton(row=3))

    @discord.ui.button(label="🔀 Trade Mode", style=discord.ButtonStyle.danger, row=0)
    async def mode_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cur_live = _read_env_value("LIVE_ENABLED").lower() == "true"
        cur, nxt = ("LIVE", "PAPER") if cur_live else ("PAPER", "LIVE")
        warn = ("\n\n⚠️ **LIVE places REAL orders on Binance with real money.**"
                if nxt == "LIVE" else
                "\n\n*PAPER simulates fills — nothing reaches Binance.*")
        await interaction.followup.send(
            f"🔀 **Switch trade mode?**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current   : **{cur}**\n"
            f"Change to : **{nxt}**{warn}\n\n"
            f"*The scanner is restarted automatically — LIVE_ENABLED is only "
            f"read at startup.*",
            view=TradeModeConfirmView(),
        )

    @discord.ui.button(label="💰 Capital", style=discord.ButtonStyle.secondary, row=0)
    async def capital_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            f"💰 **Trading Capital**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : **${_capital_status()}** USDT\n\n"
            f"Use `/capital <amount>` to change.",
            view=TradeControlsView(),
        )

    @discord.ui.button(label="⚙️ Leverage", style=discord.ButtonStyle.secondary, row=0)
    async def leverage_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            f"⚙️ **Global Leverage**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : **{_leverage_status()}x**\n\n"
            f"Use `/leverage <n>` to change.\n"
            f"*The 10% leveraged-loss cap still applies, so wide-SL trades are "
            f"stepped down regardless.*",
            view=TradeControlsView(),
        )

    @discord.ui.button(label="🎰 Max Slots", style=discord.ButtonStyle.secondary, row=1)
    async def maxconc_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            f"🎰 **Max Concurrent Positions**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : **{_max_concurrent_status()}**\n"
            f"Per-strategy caps : `{_max_per_strategy_status()}`\n\n"
            f"Total positions the bot may hold at once, across all strategies.\n\n"
            f"Change with `/max_concurrent <1-10>`\n"
            f"*Requires a scanner RESTART — read once at startup.*",
            view=TradeControlsView())

    @discord.ui.button(label="📐 Per-Strategy", style=discord.ButtonStyle.secondary, row=1)
    async def maxper_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        known = _known_strategy_ids()
        await interaction.followup.send(
            f"📐 **Per-Strategy Slot Caps**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current : `{_max_per_strategy_status()}`\n"
            f"Max concurrent : **{_max_concurrent_status()}**\n\n"
            f"How many of the concurrent slots each strategy may hold. Stops one "
            f"strategy taking every slot.\n\n"
            f"Loaded: **{', '.join(known) if known else 'unavailable'}**\n\n"
            f"Change with `/max_per_strategy CSM:1,NASOS_V4:2,ELLIOT_V8:2`\n"
            f"*A strategy left out is UNCAPPED. Requires a scanner RESTART.*",
            view=TradeControlsView())

    @discord.ui.button(label="⚠️ Reset Caps", style=discord.ButtonStyle.secondary, row=2)
    async def resetcaps_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            f"⚠️ **Reset Loss Caps**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Current state:**\n{_loss_status()}",
            view=ResetCapsView(),
        )

    @discord.ui.button(label="🧹 Clean up", style=discord.ButtonStyle.secondary, row=2)
    async def cleanup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🧹 **Run cleanup?**\n━━━━━━━━━━━━━━━━━━━━━\n"
            "Prunes stale state and log files.",
            view=SimpleConfirmView("cleanup"),
        )


class AnalyzeMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BackButton(row=2))

    @discord.ui.button(label="📈 Trade Analyze", style=discord.ButtonStyle.secondary, row=0)
    async def analyze_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⏳ Running 3-day trade analysis...")
        try:
            from modules.trade_analyzer import run
            msg = run(days=3)
            await _send_analysis(interaction, msg, view=AnalyzeView())
        except Exception as exc:
            await interaction.followup.send(f"❌ Analysis failed: {exc}",
                                            view=AnalyzeMenuView())

    @discord.ui.button(label="📋 Watchlist", style=discord.ButtonStyle.secondary, row=0)
    async def watchlist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            from modules.watchlist import get_watchlist_info
            info = get_watchlist_info()
            manual = info.get("manual_adds", [])
            auto = info.get("auto", [])
            lines = ["📋 **Watchlist (Permanent Scan List)**", "━━━━━━━━━━━━━━━━━━━━━"]
            if manual:
                lines.append("**Locked Coins:**")
                for c in manual:
                    lines.append(f"🟢 **{c}**")
            else:
                lines.append("*No coins manually locked.*")
            lines.append(f"\n*Auto-tracking {len(auto)} volume leaders.*")
            text = "\n".join(lines)
        except Exception as exc:
            text = f"Failed to load watchlist: {exc}"
        text += "\n\nTap a button below to manage."
        await interaction.followup.send(text, view=WatchlistView())

    @discord.ui.button(label="🛡 Whitelist", style=discord.ButtonStyle.secondary, row=0)
    async def whitelist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # modules/whitelist.py exists as of 2026-08-20 and is ENFORCED in
        # live_scanner._scan_for_signals. The ImportError branch below is kept
        # as a safety net: if the module is ever removed, say so plainly rather
        # than rendering an empty list, which reads as "nothing whitelisted"
        # and would hide a missing feature.
        #
        # NOTE the semantics: an EMPTY whitelist allows EVERYTHING. It only
        # starts restricting once it has entries.
        try:
            from modules.whitelist import get_all
            wl = get_all()
        except ImportError:
            await interaction.followup.send(
                "🛡 **Whitelist**\n━━━━━━━━━━━━━━━━━━━━━\n\n"
                "⚠️ Not available — there is no `modules/whitelist.py` in this "
                "build.\n\nThe nearest working equivalent is **Watchlist → "
                "Locked Coins**, which pins coins into the scan list permanently.",
                view=AnalyzeMenuView())
            return
        except Exception as exc:
            await interaction.followup.send(f"❌ Whitelist failed to load: {exc}",
                                            view=AnalyzeMenuView())
            return
        if not wl:
            text = ("🛡 **Whitelist**\n━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "*No coins whitelisted.*")
        else:
            lines = ["🛡 **Whitelist**", "━━━━━━━━━━━━━━━━━━━━━"]
            for coin in sorted(wl):
                lines.append(f"🟢 **{coin}**")
            text = "\n".join(lines)
        text += "\n\nUse `/whitelist add COIN` or `/whitelist remove COIN`"
        await interaction.followup.send(text, view=AnalyzeMenuView())

    @discord.ui.button(label="🚫 Black List", style=discord.ButtonStyle.secondary, row=1)
    async def blacklist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            from modules.blacklist import get_all
            bl = get_all()
        except Exception:
            bl = {}
        if not bl:
            text = "🚫 **Blacklist**\n━━━━━━━━━━━━━━━━━━━━━\n\n*No coins blacklisted.*"
        else:
            lines = ["🚫 **Blacklist**", "━━━━━━━━━━━━━━━━━━━━━"]
            for coin, strats in sorted(bl.items()):
                s = "all" if "*" in strats else ",".join(strats)
                lines.append(f"**{coin}** — {s}")
            text = "\n".join(lines)
        text += "\n\nUse `/blacklist add COIN [STRAT]` or `/blacklist remove COIN [STRAT]`"
        await interaction.followup.send(text, view=AnalyzeMenuView())

    @discord.ui.button(label="🛠 Optimise", style=discord.ButtonStyle.primary, row=1)
    async def optimize_lists_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🛠 **Run list optimisation?**\n━━━━━━━━━━━━━━━━━━━━━\n"
            "This REWRITES the watchlist and blacklist from recent trade "
            "performance. It can take a minute.",
            view=SimpleConfirmView("optimize"),
        )


class WebServerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BackButton(row=1))

    @discord.ui.button(label="▶ Start", style=discord.ButtonStyle.success, row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🌐 **Start the web server?**\nThis will start `csb-ngrok.service`.",
            view=SimpleConfirmView("ngrok_start"),
        )

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send(
            "🌐 **Stop the web server?**\nThe public dashboard URL will go offline.",
            view=SimpleConfirmView("ngrok_stop"),
        )

    @discord.ui.button(label="🔗 Get URL", style=discord.ButtonStyle.secondary, row=0)
    async def url_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            from ngrok_runner import get_active_ngrok_url
            url = get_active_ngrok_url(force_refresh=True)
            text = (f"🔗 **Active URL:**\n{url.replace('http://', 'https://')}"
                    if url else "No active tunnel found for port 8102.")
        except Exception as e:
            text = f"❌ **Error fetching URL:**\n{e}\n\n*Is the ngrok service running?*"
        await interaction.followup.send(text, view=WebServerView())


class TradeModeConfirmView(discord.ui.View):
    """Confirm a PAPER<->LIVE switch, then restart so it takes effect."""
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cur_live = _read_env_value("LIVE_ENABLED").lower() == "true"
        nxt   = "false" if cur_live else "true"
        label = "PAPER" if cur_live else "LIVE"
        if not _write_env_value("LIVE_ENABLED", nxt):
            await interaction.followup.send(
                "❌ Could not write .env — mode unchanged.", view=TradeControlsView())
            return
        await interaction.followup.send(f"⏳ Switched to **{label}** — restarting scanner...")
        ok, msg = _systemctl("restart")
        tail = "✅ Scanner restarted." if ok else f"⚠️ Restart failed:\n```{msg}```\nRestart manually."
        await interaction.followup.send(
            f"🔀 Trade mode is now **{label}**.\n\n{tail}", view=TradeControlsView())
        log.info(f"Trade mode switched to {label} via Discord")

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("❌ Cancelled.", view=TradeControlsView())


class SimpleConfirmView(discord.ui.View):
    """Confirm/Cancel for actions that are not systemctl scanner commands."""
    def __init__(self, action: str):
        super().__init__(timeout=60)
        self.action = action

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        a = self.action

        if a == "cleanup":
            report = _run_cleanup()
            await interaction.followup.send(
                f"🧹 **Cleanup Done**\n━━━━━━━━━━━━━━━━━━━━━\n{report}",
                view=TradeControlsView())
            log.info("Cleanup triggered via Discord")

        elif a == "optimize":
            await interaction.followup.send("⏳ Running weekly list optimization...")
            try:
                from modules.list_optimizer import run_optimization
                msg = run_optimization()
                await _send_analysis(interaction, msg, view=AnalyzeMenuView())
                log.info("Weekly list optimization triggered via Discord")
            except Exception as exc:
                await interaction.followup.send(f"❌ Optimization failed: {exc}",
                                                view=AnalyzeMenuView())

        elif a in ("ngrok_start", "ngrok_stop"):
            action = "start" if a.endswith("start") else "stop"
            await interaction.followup.send(f"⏳ {action.title()}ing web server...")
            try:
                import subprocess
                proc = subprocess.run(
                    ["sudo", "systemctl", action, "csb-ngrok.service"],
                    capture_output=True, text=True, timeout=10)
                out = "✅ Started" if action == "start" else "⏹ Stopped"
                if proc.returncode != 0:
                    out = f"❌ Failed to {action} web server:\n```{proc.stderr}```"
            except Exception as e:
                out = f"❌ Error: {e}"
            await interaction.followup.send(out, view=WebServerView())
            log.info(f"Web server {action} via Discord")

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        back = {"cleanup": TradeControlsView, "optimize": AnalyzeMenuView,
                "ngrok_start": WebServerView, "ngrok_stop": WebServerView}
        await interaction.followup.send(
            "❌ Cancelled.", view=back.get(self.action, MainMenuView)())

class ConfirmView(discord.ui.View):
    def __init__(self, action: str):
        super().__init__(timeout=60)
        self.action = action

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        action = self.action
        await interaction.followup.send(f"⏳ Running `systemctl {action} {SERVICE}`...")
        ok, msg = _systemctl(action)
        if ok:
            emoji = {"start": "▶", "stop": "⏹", "restart": "🔄"}.get(action, "✅")
            await interaction.followup.send(
                f"{emoji} **Scanner {action}ed.**", view=MainMenuView()
            )
        else:
            await interaction.followup.send(
                f"❌ **{action.title()} failed.**\n```{msg}```", view=MainMenuView()
            )
        log.info(f"{action.title()} triggered via Discord: {msg}")

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("❌ Cancelled.", view=BotControlsView())


class StrategyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        try:
            from modules.strategy_overrides import get_all, KNOWN_STRATEGIES
            states = get_all()
        except Exception:
            states = {}
            KNOWN_STRATEGIES = PROD_STRATS
        emoji_map = STRAT_EMOJI
        for s in KNOWN_STRATEGIES:
            st = states.get(s, {"disabled": False})
            flag = "⛔" if st["disabled"] else "✅"
            btn = discord.ui.Button(
                label=f"{emoji_map.get(s,'⚪')} {s} {flag}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"strat_toggle:{s}",
            )
            btn.callback = self._make_toggle_callback(s)
            self.add_item(btn)
        # Regime matrix button
        matrix_btn = discord.ui.Button(
            label="📋 Regime Matrix",
            style=discord.ButtonStyle.primary,
            custom_id="regime_matrix",
        )
        matrix_btn.callback = self._regime_matrix_callback
        self.add_item(matrix_btn)
        # Back button
        back_btn = discord.ui.Button(
            label="⬅ Back",
            style=discord.ButtonStyle.secondary,
            custom_id="back_main",
        )
        back_btn.callback = self._back_callback
        self.add_item(back_btn)

    def _make_toggle_callback(self, sname: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                from modules.strategy_overrides import toggle, get_all
                new_state = toggle(sname)
            except Exception as exc:
                await interaction.followup.send(f"❌ Toggle failed: {exc}")
                return
            verb = "DISABLED" if new_state else "ENABLED"
            emoji = "⛔" if new_state else "✅"
            try:
                states = get_all()
            except Exception:
                states = {}
            lines = [f"{emoji} **{sname}** {verb}", "━━━━━━━━━━━━━━━━━━━━━"]
            for s, st in states.items():
                flag = "⛔ DISABLED" if st["disabled"] else "✅ enabled"
                lines.append(f"**{s}** — {flag}")
            await interaction.followup.send("\n".join(lines), view=StrategyView())
        return callback

    async def _regime_matrix_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        text = f"📋 **Regime × Strategy Matrix**\n━━━━━━━━━━━━━━━━━━━━━\n{_format_regime_matrix()}"
        await interaction.followup.send(text, view=StrategyView())

    async def _back_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.followup.send("📋 **Main Menu**", view=MainMenuView())


class AnalyzeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="📊 7-day", style=discord.ButtonStyle.secondary)
    async def analyze_7d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⏳ Running 7-day analysis...")
        try:
            from modules.trade_analyzer import run
            msg = run(days=7)
            await _send_analysis(interaction, msg, view=MainMenuView())
        except Exception as exc:
            await interaction.followup.send(f"❌ Analysis failed: {exc}")

    @discord.ui.button(label="📊 14-day", style=discord.ButtonStyle.secondary)
    async def analyze_14d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⏳ Running 14-day analysis...")
        try:
            from modules.trade_analyzer import run
            msg = run(days=14)
            await _send_analysis(interaction, msg, view=MainMenuView())
        except Exception as exc:
            await interaction.followup.send(f"❌ Analysis failed: {exc}")

    @discord.ui.button(label="⬅ Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("📋 **Main Menu**", view=MainMenuView())


class ResetCapsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="📅 Reset Day only", style=discord.ButtonStyle.primary)
    async def reset_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok, result = _reset_loss_tracker("day")
        if ok:
            restart_ok, restart_msg = _systemctl("restart")
            note = "🔄 Scanner restarted." if restart_ok else f"⚠️ Restart failed: {restart_msg}"
            await interaction.followup.send(
                f"✅ **Day loss cap reset.**\n```{result}```\n{note}",
                view=MainMenuView(),
            )
        else:
            await interaction.followup.send(f"❌ Reset failed: `{result}`", view=MainMenuView())

    @discord.ui.button(label="📅📅 Reset Day + Week", style=discord.ButtonStyle.danger)
    async def reset_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok, result = _reset_loss_tracker("all")
        if ok:
            restart_ok, restart_msg = _systemctl("restart")
            note = "🔄 Scanner restarted." if restart_ok else f"⚠️ Restart failed: {restart_msg}"
            await interaction.followup.send(
                f"✅ **Day + Week loss caps reset.**\n```{result}```\n{note}",
                view=MainMenuView(),
            )
        else:
            await interaction.followup.send(f"❌ Reset failed: `{result}`", view=MainMenuView())

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("❌ Cancelled.", view=MainMenuView())



# ─── Slash commands ───────────────────────────────────────────────────────────

@tree.command(name="menu", description="Show main control panel", guild=GUILD_OBJ)
async def cmd_menu(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    await interaction.followup.send("🤖 **csb — Crypto Scanner Control**", view=MainMenuView())


@tree.command(name="status", description="Show bot status + loss caps", guild=GUILD_OBJ)
async def cmd_status(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
        f"📊 **Bot Status**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_service_status()}\n"
        f"💰 Capital  : **{cap_str}** USDT\n\n"
        f"**Loss Caps:**\n"
        f"{_loss_status()}\n\n"
        f"🕐 {now}"
    )
    await interaction.followup.send(text, view=MainMenuView())


@tree.command(name="trades", description="All-time trade report", guild=GUILD_OBJ)
async def cmd_trades(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"📈 **Trade Report — All Time**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_trade_summary()}\n\n"
        f"🕐 {now}"
    )
    if len(text) > 2000:
        for i in range(0, len(text), 2000):
            await interaction.followup.send(text[i:i+2000])
    else:
        await interaction.followup.send(text, view=MainMenuView())


@tree.command(name="max_concurrent", description="View or change max concurrent positions", guild=GUILD_OBJ)
@app_commands.describe(count="New limit, 1-10 (omit to view current)")
async def cmd_max_concurrent(interaction: discord.Interaction, count: int = None):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()

    if count is None:
        await interaction.followup.send(
            f"🎰 **Max Concurrent:** {_max_concurrent_status()}\n"
            f"Per-strategy caps: `{_max_per_strategy_status()}`\n\n"
            f"Use `/max_concurrent 3` to change.")
        return

    if not (1 <= count <= 10):
        await interaction.followup.send(
            f"❌ MAX_CONCURRENT must be between 1 and 10 (got {count}).\n"
            f"*Above 10 the aggregate margin cap would block most entries anyway.*")
        return

    old = _max_concurrent_status()
    if not _write_env_value("MAX_CONCURRENT", str(count)):
        await interaction.followup.send("❌ Failed to write .env — check permissions.")
        return

    # Per-strategy caps that were sensible at the old slot count may now be
    # inert or leave slots unfillable, so re-check them against the new value.
    note = ""
    caps = _read_env_value("MAX_PER_STRATEGY")
    if caps:
        _ok, _v, warn = validate_max_per_strategy(caps)
        if warn:
            note = f"\n\n{warn}"

    await interaction.followup.send(
        f"✅ **Max Concurrent updated**\n"
        f"  Old : ~~{old}~~\n  New : **{count}**\n\n"
        f"🔄 **Restart the scanner** — this is read once at startup.{note}")
    log.info(f"MAX_CONCURRENT changed via Discord: {old} -> {count}")


@tree.command(name="max_per_strategy", description="View or change per-strategy slot caps", guild=GUILD_OBJ)
@app_commands.describe(caps="e.g. CSM:1,NASOS_V4:2,ELLIOT_V8:2 (omit to view current)")
async def cmd_max_per_strategy(interaction: discord.Interaction, caps: str = None):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()

    if caps is None:
        known = _known_strategy_ids()
        await interaction.followup.send(
            f"📐 **Per-Strategy Caps:** `{_max_per_strategy_status()}`\n"
            f"Max concurrent: **{_max_concurrent_status()}**\n"
            f"Loaded: {', '.join(known) if known else 'unavailable'}\n\n"
            f"Use `/max_per_strategy CSM:1,NASOS_V4:2,ELLIOT_V8:2` to change.\n"
            f"*A strategy left out is UNCAPPED.*")
        return

    ok, value, msg = validate_max_per_strategy(caps)
    if not ok:
        await interaction.followup.send(f"❌ {msg}")
        return

    old = _max_per_strategy_status()
    if not _write_env_value("MAX_PER_STRATEGY", value):
        await interaction.followup.send("❌ Failed to write .env — check permissions.")
        return

    await interaction.followup.send(
        f"✅ **Per-Strategy Caps updated**\n"
        f"  Old : `{old}`\n  New : `{value}`\n\n"
        f"🔄 **Restart the scanner** for this to take effect."
        + (f"\n\n{msg}" if msg else ""))
    log.info(f"MAX_PER_STRATEGY changed via Discord: {old} -> {value}")


@tree.command(name="whitelist", description="View/add/remove whitelisted coins (restrict trading)", guild=GUILD_OBJ)
@app_commands.describe(
    action="add or remove",
    coin="Coin symbol (e.g. BTC)",
    strategy="Optional: restrict to one strategy (e.g. CSM)",
)
async def cmd_whitelist(
    interaction: discord.Interaction,
    action: str = None,
    coin: str = None,
    strategy: str = None,
):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()

    from modules.whitelist import add, remove, get_all

    if not action:
        wl = get_all()
        if not wl:
            await interaction.followup.send(
                "🛡 Whitelist is **inactive** — no entries, so all coins are "
                "allowed.\nAdding a coin switches to restrict-mode.")
        else:
            lines = [f"🛡 **Whitelist — RESTRICTING ({len(wl)} coins):**",
                     "*Only these coins may trade.*"]
            for c, strats in sorted(wl.items()):
                st = "all" if "*" in strats else ",".join(strats)
                lines.append(f"• {c} ({st})")
            await interaction.followup.send("\n".join(lines))
        return

    if not coin:
        await interaction.followup.send(
            "Usage: `/whitelist add BTC` or `/whitelist remove BTC CSM`")
        return

    symbol = coin.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    strat_list = [strategy.upper()] if strategy else None

    if action.lower() == "add":
        was_empty = not get_all()
        desc = add(symbol, strat_list)
        note = ("\n\n⚠️ The whitelist was empty; it is now **RESTRICTING** — "
                "only listed coins will trade." if was_empty else "")
        await interaction.followup.send(f"🛡 {desc}{note}")
    elif action.lower() in ("remove", "rm", "del"):
        if remove(symbol, strat_list):
            msg = (f"✅ {symbol} removed from whitelist for {strategy.upper()}"
                   if strategy else f"✅ {symbol} removed from whitelist")
            if not get_all():
                msg += "\n\n*Whitelist is now empty — all coins allowed again.*"
            await interaction.followup.send(msg)
        else:
            await interaction.followup.send(f"{symbol} was not on the whitelist.")
    else:
        await interaction.followup.send("Action must be `add` or `remove`.")
    log.info(f"Whitelist {action} {symbol} via Discord")


@tree.command(name="blacklist", description="View/add/remove blacklisted coins", guild=GUILD_OBJ)
@app_commands.describe(
    action="add or remove",
    coin="Coin symbol (e.g. XLM)",
    strategy="Optional: specific strategy (e.g. GRID)",
)
async def cmd_blacklist(
    interaction: discord.Interaction,
    action: str = None,
    coin: str = None,
    strategy: str = None,
):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()

    from modules.blacklist import add, remove, get_all

    if not action:
        bl = get_all()
        if not bl:
            await interaction.followup.send("📋 Blacklist is empty.")
        else:
            lines = [f"🚫 **Blacklist ({len(bl)} coins):**"]
            for c, strats in sorted(bl.items()):
                s = "all" if "*" in strats else ",".join(strats)
                lines.append(f"• {c} ({s})")
            await interaction.followup.send("\n".join(lines))
        return

    if not coin:
        await interaction.followup.send("Usage: `/blacklist add XLM` or `/blacklist remove XLM GRID`")
        return

    symbol = coin.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    strat_list = [strategy.upper()] if strategy else None

    if action.lower() == "add":
        desc = add(symbol, strat_list)
        await interaction.followup.send(f"🚫 {desc}")
    elif action.lower() in ("remove", "rm", "del"):
        if remove(symbol, strat_list):
            msg = f"✅ {symbol} unblocked for {strategy.upper()}" if strategy else f"✅ {symbol} removed"
            await interaction.followup.send(msg)
        else:
            await interaction.followup.send(f"⚠️ {symbol} not in blacklist.")
    else:
        await interaction.followup.send("Usage: `/blacklist add|remove COIN [STRATEGY]`")


@tree.command(name="watchlist", description="View/add/remove manually locked coins", guild=GUILD_OBJ)
@app_commands.describe(action="add or remove", coin="Coin symbol")
async def cmd_watchlist(interaction: discord.Interaction, action: str = None, coin: str = None):
    if not _allowed(interaction): return await _deny(interaction)
    await interaction.response.defer()
    from modules.watchlist import add_to_watchlist, remove_from_watchlist, get_watchlist_info

    if not action:
        info = get_watchlist_info()
        manual = info.get("manual_adds", [])
        if not manual:
            await interaction.followup.send("📋 No coins manually locked in Watchlist.")
        else:
            lines = [f"📋 **Locked Coins ({len(manual)}):**"]
            for c in manual:
                lines.append(f"• {c}")
            await interaction.followup.send("\n".join(lines))
        return

    if not coin:
        await interaction.followup.send("Usage: `/watchlist add SOL` or `/watchlist remove SOL`")
        return

    symbol = coin.upper() + ("" if coin.upper().endswith("USDT") else "USDT")

    if action.lower() == "add":
        if add_to_watchlist(symbol):
            await interaction.followup.send(f"🟢 {symbol} locked into the permanent Watchlist.")
        else:
            await interaction.followup.send(f"{symbol} is already in the Watchlist.")
    elif action.lower() in ("remove", "rm", "del"):
        ok, msg = remove_from_watchlist(symbol)
        await interaction.followup.send(msg)
    else:
        await interaction.followup.send("Usage: `/watchlist add|remove COIN`")


@tree.command(name="analyze", description="Run trade analysis", guild=GUILD_OBJ)
@app_commands.describe(days="Analysis window in days (default 3, max 30)")
async def cmd_analyze(interaction: discord.Interaction, days: int = 3):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    days = max(1, min(days, 30))
    await interaction.followup.send(f"⏳ Running {days}-day trade analysis...")
    try:
        from modules.trade_analyzer import run
        msg = run(days=days)
        await _send_analysis(interaction, msg, view=MainMenuView())
    except Exception as exc:
        await interaction.followup.send(f"❌ Analysis failed: {exc}")


@tree.command(name="optimize", description="Run weekly optimization on Watchlist and Blacklist", guild=GUILD_OBJ)
async def cmd_optimize(interaction: discord.Interaction):
    if not _allowed(interaction): return await _deny(interaction)
    await interaction.response.defer()
    await interaction.followup.send("⏳ Running weekly list optimization...")
    try:
        from modules.list_optimizer import run_optimization
        msg = run_optimization()
        await _send_analysis(interaction, msg, view=MainMenuView())
    except Exception as exc:
        await interaction.followup.send(f"❌ Optimization failed: {exc}")


@tree.command(name="start", description="Start the scanner", guild=GUILD_OBJ)
async def cmd_start(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    ok, msg = _systemctl("start")
    if ok:
        await interaction.followup.send("▶ **Scanner started.**", view=MainMenuView())
    else:
        await interaction.followup.send(f"❌ Start failed.\n```{msg}```")
    log.info(f"Start via Discord: {msg}")


@tree.command(name="stop", description="Stop the scanner (graceful)", guild=GUILD_OBJ)
async def cmd_stop(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    await interaction.followup.send("⏳ Stopping scanner (may take up to 60s)...")
    ok, msg = _systemctl("stop")
    if ok:
        await interaction.followup.send("⏹ **Scanner stopped.**", view=MainMenuView())
    else:
        await interaction.followup.send(f"❌ Stop failed.\n```{msg}```")
    log.info(f"Stop via Discord: {msg}")


@tree.command(name="restart", description="Restart the scanner", guild=GUILD_OBJ)
async def cmd_restart(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    await interaction.followup.send("⏳ Restarting scanner (may take up to 60s)...")
    ok, msg = _systemctl("restart")
    if ok:
        await interaction.followup.send("🔄 **Scanner restarted.**", view=MainMenuView())
    else:
        await interaction.followup.send(f"❌ Restart failed.\n```{msg}```")
    log.info(f"Restart via Discord: {msg}")


@tree.command(name="resetcaps", description="Reset loss caps", guild=GUILD_OBJ)
@app_commands.describe(scope="'day' or 'all' (day+week)")
async def cmd_resetcaps(interaction: discord.Interaction, scope: str = "day"):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    if scope not in ("day", "all"):
        scope = "day"
    ok, result = _reset_loss_tracker(scope)
    if ok:
        restart_ok, restart_msg = _systemctl("restart")
        note = "🔄 Scanner restarted." if restart_ok else f"⚠️ Restart failed: {restart_msg}"
        await interaction.followup.send(
            f"✅ **Loss caps reset ({scope}).**\n```{result}```\n{note}",
            view=MainMenuView(),
        )
    else:
        await interaction.followup.send(f"❌ Reset failed: `{result}`")


@tree.command(name="capital", description="View or change trading capital", guild=GUILD_OBJ)
@app_commands.describe(amount="New capital amount in USDT (omit to view current)")
async def cmd_capital(interaction: discord.Interaction, amount: float = None):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()

    if amount is None:
        cap = _capital_status()
        await interaction.followup.send(
            f"💰 **Capital:** ${cap} USDT\n\nUse `/capital 500` to change.",
            view=MainMenuView(),
        )
        return

    if amount <= 0 or amount > 1_000_000:
        await interaction.followup.send("❌ Amount must be between $0 and $1,000,000.")
        return

    old = _capital_status()
    ok  = _write_env_value("ACCOUNT_EQUITY_USDT", f"{amount:.2f}")
    if ok:
        await interaction.followup.send(
            f"✅ **Capital updated**\n\n"
            f"Old : ~~${old}~~ USDT\n"
            f"New : **${amount:,.2f}** USDT\n\n"
            f"⚠️ **Restart the scanner** to apply.",
            view=MainMenuView(),
        )
        log.info(f"Capital changed via Discord: {old} → {amount:.2f}")
    else:
        await interaction.followup.send("❌ Failed to write .env file.")

@tree.command(name="leverage", description="View or change global leverage", guild=GUILD_OBJ)
@app_commands.describe(amount="New leverage multiplier (omit to view current)")
async def cmd_leverage(interaction: discord.Interaction, amount: int = None):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()

    if amount is None:
        lev = _leverage_status()
        await interaction.followup.send(
            f"⚙️ **Global Leverage:** {lev}x\n\nUse `/leverage 5` to change.",
            view=MainMenuView(),
        )
        return

    if amount <= 0 or amount > 50:
        await interaction.followup.send("❌ Leverage must be between 1x and 50x.")
        return

    old = _leverage_status()
    ok  = _write_env_value("GLOBAL_LEVERAGE", f"{amount}")
    if ok:
        await interaction.followup.send(
            f"✅ **Global Leverage updated**\n\n"
            f"Old : ~~{old}x~~\n"
            f"New : **{amount}x**\n\n"
            f"✨ Applies from the next signal — no restart needed.\n"
            f"*Note: the 10% leveraged-loss cap still applies, so wide-SL "
            f"signals may trade below this figure.*",
            view=MainMenuView(),
        )
        log.info(f"Leverage changed via Discord: {old}x → {amount}x")
    else:
        await interaction.followup.send("❌ Failed to write .env file.")


@tree.command(name="strategies", description="Toggle strategies on/off", guild=GUILD_OBJ)
async def cmd_strategies(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    try:
        from modules.strategy_overrides import get_all
        states = get_all()
    except Exception:
        states = {}
    lines = ["🎛 **Strategy Controls**", "━━━━━━━━━━━━━━━━━━━━━"]
    for s, st in states.items():
        flag = "⛔ DISABLED" if st["disabled"] else "✅ enabled"
        lines.append(f"**{s}** — {flag}")
    lines.append("\n*Tap a strategy to toggle.*")
    await interaction.followup.send("\n".join(lines), view=StrategyView())


@tree.command(name="regime", description="Show regime × strategy matrix", guild=GUILD_OBJ)
async def cmd_regime(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    text = f"📋 **Regime × Strategy Matrix**\n━━━━━━━━━━━━━━━━━━━━━\n{_format_regime_matrix()}"
    await interaction.followup.send(text, view=MainMenuView())


@tree.command(name="cleanup", description="Archive logs + prune cooldowns", guild=GUILD_OBJ)
async def cmd_cleanup(interaction: discord.Interaction):
    if not _allowed(interaction):
        return await _deny(interaction)
    await interaction.response.defer()
    report = _run_cleanup()
    await interaction.followup.send(
        f"🧹 **Cleanup Done**\n━━━━━━━━━━━━━━━━━━━━━\n{report}",
        view=MainMenuView(),
    )
    log.info("Cleanup triggered via Discord")


# ─── Bot events ───────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    await tree.sync(guild=GUILD_OBJ)
    log.info(f"Discord bot ready: {client.user} | Guild: {GUILD_ID}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN or not GUILD_ID:
        raise ValueError("DISCORD_BOT_TOKEN or DISCORD_GUILD_ID missing in .env")
    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()



