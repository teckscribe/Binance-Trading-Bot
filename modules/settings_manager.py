"""
modules/settings_manager.py
Runtime-editable settings, stored in data/settings.json and hot-reloaded.

Why this exists
---------------
Every tunable used to live in .env and was read with os.getenv() at import
time, so a change made from Telegram, Discord or the web dashboard needed a
scanner restart to take effect. Three separate .env writers had grown up, two
of them not crash-safe. This module replaces all of that:

  * ONE spec (SPEC below) — key, type, bounds, default, group, help text.
    The dashboard renders its editor from it and every writer validates
    against it, so an out-of-range value cannot reach the file.
  * ONE reader — get(key) returns a typed value, re-reading the file only
    when its (mtime, size) changes, and checking that at most once per
    second, so it is cheap enough to call per signal.
  * ONE writer — update() validates, rewrites atomically (temp + os.replace)
    so a crash mid-write can never leave a truncated file.

Precedence, highest first
-------------------------
  1. An explicit shell override: a key present in os.environ whose value did
     NOT come from .env. This is how backtest sweeps work
     (CSM_MOM_LO=3.5 python backtest_optimizer.py) and it needs no code.
     Set CSB_SETTINGS_FROM_ENV=1 to make EVERY key resolve this way.
  2. data/settings.json
  3. .env (only for a key absent from settings.json — i.e. one added to
     SPEC after the file was created)
  4. The SPEC default

What stays in .env
------------------
Secrets and identity (API keys, bot tokens, chat/guild ids, webhooks, the
TOTP secret) and LIVE_ENABLED, which is a deliberate on-server act and is
never exposed to an internet-facing editor. env_get()/env_set() below are
the one sanctioned way to touch those from the bots.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("Settings")

_PROJECT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(_PROJECT_DIR, "data", "settings.json")
ENV_FILE      = os.path.join(_PROJECT_DIR, ".env")

_TRUE  = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


# ─── Spec ─────────────────────────────────────────────────────────────────────
# type: int | float | bool | choice | csv_caps
# hot : True  = the running scanner picks it up without a restart
#       False = read once at process start (say so in the UI)

def _s(key, group, type_, default, label, help_="", hot=True, **kw):
    d = {"key": key, "group": group, "type": type_, "default": default,
         "label": label, "help": help_, "hot": hot}
    d.update(kw)
    return d


SPEC = [
    # ── Account ──────────────────────────────────────────────────────────────
    _s("ACCOUNT_EQUITY_USDT", "Account", "float", 10.0, "Account equity (USDT)",
       "Base for position sizing. Risk per trade is 1% of this. In PAPER mode "
       "a change resets the simulated ledger to the new starting balance.",
       min=1, max=1000000, step="0.01"),

    # ── Scanner ──────────────────────────────────────────────────────────────
    _s("SCAN_INTERVAL_SECONDS", "Scanner", "int", 60, "Scan interval (s)",
       "How often the bot looks for NEW entries.", min=10, max=3600),
    _s("FAST_INTERVAL_SECONDS", "Scanner", "int", 10, "Manage interval (s)",
       "How often open positions are checked for SL/TP.", min=1, max=300),
    _s("TOP_N_SYMBOLS", "Scanner", "int", 100, "Top N symbols",
       "Universe size when focused mode is off.", min=10, max=400),
    _s("FOCUSED_MODE", "Scanner", "bool", False, "Focused mode",
       "Trade a curated watchlist instead of the full top-N."),
    _s("FOCUSED_SIZE", "Scanner", "int", 5, "Focused size",
       "Watchlist length when focused mode is on.", min=1, max=400),
    _s("MIN_COIN_AGE_DAYS", "Scanner", "int", 30, "Min coin age (days)",
       "Contracts listed more recently than this are excluded.", min=0, max=3650),
    _s("MAX_ENTRIES_PER_CYCLE", "Scanner", "int", 2, "Max entries per cycle",
       "New positions opened per scan. At 1 the highest-scoring strategy "
       "takes the slot nearly every cycle.", min=1, max=10),
    _s("MIN_STRENGTH", "Scanner", "float", 0.50, "Min signal strength",
       "0 = act on every signal. Only the freqtrade ports vary; CSM is "
       "pinned at 1.0.", min=0.0, max=1.0, step="0.05"),
    _s("MANAGE_ON_BAR_CLOSE", "Scanner", "bool", False, "Manage on bar close",
       "Experiment: evaluate TP/trail/breakeven on the last completed 15m bar "
       "instead of the live tick. The exchange stop is always tick-checked."),

    # ── Risk & sizing ────────────────────────────────────────────────────────
    _s("MAX_CONCURRENT", "Risk", "int", 3, "Max concurrent positions",
       "Total open positions across all strategies.", min=1, max=20),
    _s("MAX_PER_STRATEGY", "Risk", "csv_caps", "CSM:3,NASOS_V4:3,ELLIOT_V8:3",
       "Per-strategy caps", "Format: CSM:2,NASOS_V4:2,ELLIOT_V8:2"),
    _s("MAX_TOTAL_MARGIN_PCT", "Risk", "float", 0.60, "Max total margin (fraction)",
       "Ceiling on combined margin as a fraction of equity.",
       min=0.05, max=1.0, step="0.01"),
    _s("GLOBAL_LEVERAGE", "Risk", "int", 0, "Global leverage (max)",
       "Upper bound only — effective leverage is min(this, floor("
       "MAX_LEVERAGED_LOSS_PCT / stop distance)). 0 = use the per-strategy "
       "table.", min=0, max=50),
    _s("MAX_LEVERAGED_LOSS_PCT", "Risk", "float", 0.10, "Max leveraged loss (fraction)",
       "Caps stop distance x leverage. 0.10 = 10%.", min=0.01, max=1.0, step="0.01"),
    _s("MIN_SL_PCT", "Risk", "float", 0.015, "Min stop distance (fraction)",
       "ENTRY filter — signals with tighter stops are rejected as noise.",
       min=0.001, max=0.5, step="0.001"),
    _s("MAX_SL_PCT", "Risk", "float", 0.10, "Max stop distance (fraction)",
       "ENTRY filter — signals with wider stops are rejected. 0.03 removes "
       "~92% of CSM's trades.", min=0.01, max=0.5, step="0.01"),
    _s("MAX_TRADE_LOSS_PCT", "Risk", "float", 0.0, "Per-trade loss ceiling (0 = off)",
       "EXIT guard on LEVERAGED loss. Measured harmful at 0.05 — it turned two "
       "of three strategies negative. Leave at 0.", min=0.0, max=1.0, step="0.01"),
    _s("DAILY_LOSS_CAP", "Risk", "float", -0.10, "Daily loss cap (fraction)",
       "No new entries for the rest of the UTC day once realised P&L reaches "
       "this.", min=-0.5, max=-0.01, step="0.01"),
    _s("WEEKLY_LOSS_CAP", "Risk", "float", -0.15, "Weekly loss cap (fraction)",
       "No new entries until next UTC Monday once realised P&L reaches this.",
       min=-0.5, max=-0.01, step="0.01"),
    _s("SESSION_LOSS_FLOOR", "Risk", "float", -0.10, "Session loss floor (fraction)",
       "In-memory floor for the current process; resets on restart.",
       min=-0.5, max=-0.01, step="0.01"),

    # ── Position handling ────────────────────────────────────────────────────
    _s("CLOSE_ON_SHUTDOWN", "Positions", "bool", True, "Close positions on shutdown",
       "On = flat after every restart. Off = positions are persisted and resumed."),
    _s("MAX_RESUME_AGE_HOURS", "Positions", "float", 12.0, "Max resume age (h)",
       "Refuse to resume positions left unmanaged longer than this.",
       min=1, max=168, step="1"),

    # ── CSM ──────────────────────────────────────────────────────────────────
    _s("CSM_ALLOW_LONG", "CSM", "bool", True, "Allow LONG"),
    _s("CSM_ALLOW_SHORT", "CSM", "bool", True, "Allow SHORT"),
    _s("CSM_MOM_LO", "CSM", "float", 3.0, "Momentum band low (x ATR)",
       "Entry when |24h move| / ATR(1h) is in [low, high).", min=0.5, max=20, step="0.1"),
    _s("CSM_MOM_HI", "CSM", "float", 4.0, "Momentum band high (x ATR)",
       min=0.5, max=20, step="0.1"),
    _s("CSM_SL_ATR_LONG", "CSM", "float", 2.0, "SL x ATR(15m), long", min=0.1, max=20, step="0.05"),
    _s("CSM_TP_ATR_LONG", "CSM", "float", 4.0, "TP x ATR(15m), long", min=0.1, max=20, step="0.05"),
    _s("CSM_SL_ATR_SHORT", "CSM", "float", 2.0, "SL x ATR(15m), short", min=0.1, max=20, step="0.05"),
    _s("CSM_TP_ATR_SHORT", "CSM", "float", 4.0, "TP x ATR(15m), short", min=0.1, max=20, step="0.05"),
    _s("CSM_MIN_SL_PCT", "CSM", "float", 0.02, "Min stop distance (fraction)",
       "Floor on the ATR-derived stop. Shorts whose raw stop is under this "
       "are skipped.", min=0.001, max=0.5, step="0.001"),
    _s("CSM_MAX_HOLD_MIN", "CSM", "int", 1440, "Max hold (min)",
       "480 = 8h, 1440 = 24h.", min=30, max=10080),
    _s("CSM_PROFIT_LADDER", "CSM", "choice", "on", "Profit ladder",
       "on = lock +0.15% at +1%, +1.5% at +2.5%, +2.5% at +4%. "
       "off = breakeven then 2xATR trail.", options=["on", "off"]),
    _s("CSM_VOL_RATIO_MIN", "CSM", "float", 1.0, "Min volume ratio",
       "Last completed 1h volume must be >= this x the 24h hourly average. "
       "0 = off.", min=0.0, max=20, step="0.1"),
    _s("CSM_LEGACY_BE", "CSM", "bool", False, "Legacy breakeven",
       "Only used when the ladder is off."),

    # ── NASOS ────────────────────────────────────────────────────────────────
    _s("NASOS_SL_MODE", "NASOS", "choice", "flat", "Stop mode",
       "flat = the ported 8%. atr = NASOS_SL_ATR x ATR(1m).", options=["flat", "atr"]),
    _s("NASOS_SL_FLAT", "NASOS", "float", 0.08, "Flat stop (fraction)",
       min=0.005, max=0.5, step="0.005"),
    _s("NASOS_SL_ATR", "NASOS", "float", 6.0, "SL x ATR(1m)", min=0.1, max=20, step="0.1"),
    _s("NASOS_TP_ATR", "NASOS", "float", 3.0, "TP x ATR(1m)", min=0.1, max=20, step="0.1"),
    _s("PORT_MAX_HOLD_MIN", "NASOS", "int", 0, "Max hold for ports (min)",
       "Time exit for the freqtrade ports. 0 = off (historical behaviour).",
       min=0, max=10080),

    # ── Machine learning ─────────────────────────────────────────────────────
    _s("ML_PHASE", "ML", "int", 1, "ML phase", min=1, max=4),
    _s("ML_SHADOW", "ML", "bool", True, "ML shadow mode",
       "On = ML logs and predicts but never affects trading."),

    # ── Notifications & logging ──────────────────────────────────────────────
    _s("TELEGRAM_ENABLED", "Notifications", "bool", True, "Telegram notifications",
       "Toggle only — the bot token is not editable here."),
    _s("NGROK_ENABLED", "Notifications", "bool", True, "Ngrok tunnel",
       "Toggle only — the authtoken is not editable here. Read when the ngrok "
       "service starts.", hot=False),
    _s("LOG_LEVEL", "Notifications", "choice", "INFO", "Log level",
       options=["DEBUG", "INFO", "WARNING", "ERROR"]),
]

SPEC_BY_KEY = {e["key"]: e for e in SPEC}
KEYS = [e["key"] for e in SPEC]


# ─── Validation / coercion ────────────────────────────────────────────────────

def validate(key: str, raw) -> tuple:
    """(ok, typed_value, error). Authoritative — every writer goes through it."""
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        return False, None, f"{key}: not a known setting"
    t = spec["type"]

    if isinstance(raw, bool):
        s = "true" if raw else "false"
    else:
        s = str(raw).strip()
    if s == "":
        return False, None, f"{key}: value required"

    if t == "bool":
        if s.lower() in _TRUE:
            return True, True, ""
        if s.lower() in _FALSE:
            return True, False, ""
        return False, None, f"{key}: expected true or false"

    if t == "choice":
        for opt in spec["options"]:
            if s.lower() == opt.lower():
                return True, opt, ""
        return False, None, f"{key}: must be one of {', '.join(spec['options'])}"

    if t in ("int", "float"):
        try:
            v = int(float(s)) if t == "int" else float(s)
        except ValueError:
            return False, None, f"{key}: not a valid {t}"
        if t == "int" and float(s) != v:
            return False, None, f"{key}: must be a whole number"
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            return False, None, f"{key}: below minimum {lo}"
        if hi is not None and v > hi:
            return False, None, f"{key}: above maximum {hi}"
        return True, (v if t == "int" else round(v, 6)), ""

    if t == "csv_caps":
        # STRATEGY:int,... — a malformed value silently disables every
        # per-strategy cap, so it is parsed strictly rather than trusted.
        parts = [p for p in s.split(",") if p.strip()]
        if not parts:
            return False, None, f"{key}: empty"
        clean, seen = [], set()
        for part in parts:
            if ":" not in part:
                return False, None, f"{key}: '{part.strip()}' is not STRATEGY:N"
            name, num = part.split(":", 1)
            name, num = name.strip().upper(), num.strip()
            if not re.fullmatch(r"[A-Z0-9_]+", name):
                return False, None, f"{key}: bad strategy name '{name}'"
            if name in seen:
                return False, None, f"{key}: '{name}' appears more than once"
            if not num.isdigit() or int(num) > 20:
                return False, None, f"{key}: '{name}' cap must be 0-20"
            seen.add(name)
            clean.append(f"{name}:{int(num)}")
        return True, ",".join(clean), ""

    return False, None, f"{key}: unknown type {t}"


def to_str(key: str, value) -> str:
    """Canonical string form (what .env used to hold, and what the UI shows)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_caps(value: str) -> dict:
    """'CSM:2,NASOS_V4:1' -> {'CSM': 2, 'NASOS_V4': 1}."""
    out = {}
    for pair in str(value or "").split(","):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        try:
            out[k.strip().upper()] = int(v.strip())
        except ValueError:
            continue
    return out


# ─── .env access (secrets + LIVE_ENABLED only) ────────────────────────────────

def _read_env_file() -> dict:
    out = {}
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def env_get(key: str, default: str = "") -> str:
    """Read one key straight from .env on disk (not the stale process env)."""
    return _read_env_file().get(key, default)


def env_set(key: str, value: str) -> bool:
    """Rewrite one key in .env, preserving every other line, atomically."""
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError as exc:
        log.error(f".env read failed: {exc}")
        return False

    nl = "\r\n" if (lines and lines[0].endswith("\r\n")) else "\n"
    out, found = [], False
    for line in lines:
        stripped = line.strip()
        if (stripped and not stripped.startswith("#") and "=" in stripped
                and stripped.split("=", 1)[0].strip() == key):
            out.append(f"{key}={value}{nl}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out.append(nl)
        out.append(f"{key}={value}{nl}")

    return _atomic_write_text(ENV_FILE, "".join(out), prefix=".env.")


def _atomic_write_text(path: str, text: str, prefix: str) -> bool:
    tmp = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=prefix, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.error(f"atomic write to {path} failed: {exc}")
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


# ─── Store ────────────────────────────────────────────────────────────────────

_lock = threading.RLock()
_STAT_INTERVAL = 1.0
_NO_FILE_CACHE = os.getenv("CSB_NO_FILE_CACHE", "false").strip().lower() in _TRUE
_FROM_ENV      = os.getenv("CSB_SETTINGS_FROM_ENV", "false").strip().lower() in _TRUE

_cache = {"key": None, "checked": 0.0, "raw": {}, "typed": {}}
_warned_invalid: set = set()


def _file_key(path):
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _shell_overrides() -> dict:
    """Keys in os.environ whose value did not come from .env."""
    if _FROM_ENV:
        return {k: os.environ[k] for k in KEYS if k in os.environ}
    env_file = _read_env_file()
    out = {}
    for k in KEYS:
        v = os.environ.get(k)
        if v is not None and env_file.get(k) != v:
            out[k] = v
    return out


_overrides = _shell_overrides()
if _overrides:
    log.info(f"Settings overridden from the shell: {sorted(_overrides)}")


def _coerce(key: str, raw, source: str):
    ok, v, err = validate(key, raw)
    if ok:
        return v
    if key not in _warned_invalid:
        _warned_invalid.add(key)
        log.warning(f"{err} (in {source}) — using default "
                    f"{SPEC_BY_KEY[key]['default']!r}")
    return SPEC_BY_KEY[key]["default"]


def _read_file_raw() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error(f"settings.json unreadable ({exc}) — falling back to .env/defaults")
        return {}


def _build_typed(raw: dict, use_overrides: bool = True) -> dict:
    env_file = None
    typed = {}
    for key in KEYS:
        if use_overrides and key in _overrides:
            typed[key] = _coerce(key, _overrides[key], "shell env")
        elif key in raw:
            typed[key] = _coerce(key, raw[key], "settings.json")
        else:
            if env_file is None:
                env_file = _read_env_file()
            if key in env_file:
                typed[key] = _coerce(key, env_file[key], ".env")
            else:
                typed[key] = SPEC_BY_KEY[key]["default"]
    return typed


def _load() -> dict:
    """Typed values for every key, re-read only when the file changed."""
    now = time.monotonic()
    with _lock:
        if (not _NO_FILE_CACHE and _cache["typed"]
                and now - _cache["checked"] < _STAT_INTERVAL):
            return _cache["typed"]
        key = _file_key(SETTINGS_FILE)
        if (not _NO_FILE_CACHE and _cache["typed"]
                and key is not None and key == _cache["key"]):
            _cache["checked"] = now
            return _cache["typed"]
        raw = _read_file_raw()
        _cache.update(key=key, checked=now, raw=raw, typed=_build_typed(raw))
        return _cache["typed"]


def get(key: str):
    """Typed current value. Unknown key raises — a typo must not be silent."""
    if key not in SPEC_BY_KEY:
        raise KeyError(f"{key} is not a known setting")
    return _load()[key]


def get_str(key: str) -> str:
    return to_str(key, get(key))


def get_all() -> dict:
    return dict(_load())


def is_hot(key: str) -> bool:
    return bool(SPEC_BY_KEY.get(key, {}).get("hot", False))


def _write_raw(raw: dict) -> bool:
    body = {
        "_comment": "Runtime settings for csb. Edited by the dashboard, "
                    "Telegram and Discord bots; hand edits are picked up "
                    "within a second. Secrets and LIVE_ENABLED stay in .env.",
        "_updated": datetime.now(timezone.utc).isoformat(),
    }
    for k in KEYS:
        if k in raw:
            body[k] = raw[k]
    ok = _atomic_write_text(SETTINGS_FILE, json.dumps(body, indent=2) + "\n",
                            prefix=".settings.")
    if ok:
        with _lock:
            _cache.update(key=_file_key(SETTINGS_FILE), checked=time.monotonic(),
                          raw=body, typed=_build_typed(body))
    return ok


def update(changes: dict) -> tuple:
    """
    Validate and persist several keys at once.

    Returns (ok, applied, errors):
      applied : [{"key", "from", "to"}] for keys whose value actually changed
      errors  : [str] — if non-empty NOTHING was written
    """
    with _lock:
        current = _load()
        raw = dict(_read_file_raw())        # fresh, so a concurrent writer's
        raw.pop("_comment", None)           # change is not thrown away
        raw.pop("_updated", None)
        # Always write the complete key set, so a file that was partial or
        # corrupt is repaired. Shell overrides are deliberately NOT persisted —
        # a backtest sweep's env must never leak into the live file.
        fallback = _build_typed(raw, use_overrides=False)
        for k in KEYS:
            raw.setdefault(k, fallback[k])
        applied, errors, new_vals = [], [], {}
        for key, val in changes.items():
            ok, typed, err = validate(key, val)
            if not ok:
                errors.append(err)
                continue
            if current.get(key) != typed:
                applied.append({"key": key, "from": to_str(key, current.get(key)),
                                "to": to_str(key, typed)})
            new_vals[key] = typed
        if errors:
            return False, [], errors
        if not applied:
            return True, [], []
        raw.update(new_vals)
        if not _write_raw(raw):
            return False, [], ["could not write settings.json"]
        for a in applied:
            log.info(f"[SETTINGS] {a['key']}: {a['from']!r} -> {a['to']!r}")
        return True, applied, []


def set_value(key: str, value) -> tuple:
    """Single-key convenience: (ok, error_message)."""
    ok, _applied, errors = update({key: value})
    return ok, ("; ".join(errors) if errors else "")


def migrate_from_env() -> bool:
    """
    Create data/settings.json on first run, seeded from .env so nothing
    changes behaviour on deploy. .env lines are left untouched; they are
    simply no longer read for these keys. Returns True if a file was written.
    """
    if os.path.exists(SETTINGS_FILE):
        return False
    env_file = _read_env_file()
    raw, seeded = {}, []
    for key in KEYS:
        if key in env_file:
            ok, typed, err = validate(key, env_file[key])
            if ok:
                raw[key] = typed
                seeded.append(key)
                continue
            log.warning(f"migrate: {err} — using default")
        raw[key] = SPEC_BY_KEY[key]["default"]
    if _write_raw(raw):
        log.info(f"Created {SETTINGS_FILE} — {len(seeded)} value(s) seeded from .env: "
                 f"{', '.join(seeded)}")
        return True
    return False
