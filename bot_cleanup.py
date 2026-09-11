"""
bot_cleanup.py
Standalone diagnostic and cleanup utility for CSB bot.

Run manually from Ubuntu (bot can be running):
  python3 bot_cleanup.py           # show diagnostics only
  python3 bot_cleanup.py --clean   # diagnose + clean up old files
  python3 bot_cleanup.py --reset-overrides   # re-enable all strategies
  python3 bot_cleanup.py --reset-blacklist   # clear all blacklisted symbols
  python3 bot_cleanup.py --reset-all         # reset overrides + blacklist

Safe to run while bot is live — only reads/archives files, never deletes
active trading state (loss_tracker, cooldowns, open positions).
"""

import json
import shutil
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA              = ROOT / "data"
LOGS_LIVE         = ROOT / "logs" / "live"
LOGS_STRATEGIES   = ROOT / "logs" / "strategies"
ML_DIR            = DATA / "ml"
SIGNALS_JSONL     = ML_DIR / "signals.jsonl"
OUTCOMES_JSONL    = ML_DIR / "outcomes.jsonl"
LOSS_TRACKER      = DATA / "loss_tracker.json"
LOSS_COOLDOWN     = DATA / "loss_cooldown.json"
STRATEGY_OVERRIDES = DATA / "strategy_overrides.json"
SYMBOL_BLACKLIST  = DATA / "symbol_blacklist.json"
HARDSTOP_HISTORY  = DATA / "symbol_hardstop_history.json"
SYMBOL_CACHE      = DATA / "symbols" / "top200.json"

# Archive destination
ARCHIVE_DIR       = ROOT / "data" / "archive"

# Cleanup thresholds
SESSION_LOG_KEEP_DAYS  = 14     # keep last 14 days of session logs
STRATEGY_LOG_KEEP_DAYS = 14
ML_JSONL_KEEP_RECORDS  = 500    # keep last 500 records in each JSONL


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        return {"_error": str(e)}


def _file_size_kb(path: Path) -> str:
    if not path.exists():
        return "missing"
    size = path.stat().st_size
    if size < 1024:
        return f"{size}B"
    return f"{size/1024:.1f}KB"


def _file_age(path: Path) -> str:
    if not path.exists():
        return "missing"
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age   = datetime.now(timezone.utc) - mtime
    mins  = int(age.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    return f"{int(mins/60)}h {mins%60}m ago"


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with open(path) as f:
        for line in f:
            if line.strip():
                count += 1
    return count


# ─── Diagnostics ──────────────────────────────────────────────────────────────

def diagnose():
    print("\n" + "="*60)
    print("  CSB Bot Diagnostics")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)

    # ── 1. Loss caps (trade blocker #1) ───────────────────────────────────────
    print("\n[1] LOSS CAPS")
    tracker = _load_json(LOSS_TRACKER)
    if "_error" not in tracker:
        day_pnl  = tracker.get("day_pnl",  0.0) * 100
        week_pnl = tracker.get("week_pnl", 0.0) * 100
        day_cap  = -5.0
        week_cap = -10.0
        day_blocked  = day_pnl  <= day_cap
        week_blocked = week_pnl <= week_cap
        print(f"  Day  P&L : {day_pnl:+.2f}% (cap {day_cap:.0f}%)  "
              f"{'🚫 BLOCKED' if day_blocked else '✅ OK'}")
        print(f"  Week P&L : {week_pnl:+.2f}% (cap {week_cap:.0f}%)  "
              f"{'🚫 BLOCKED' if week_blocked else '✅ OK'}")
    else:
        print(f"  {LOSS_TRACKER} missing or corrupt")

    # ── 2. Strategy overrides (trade blocker #2) ──────────────────────────────
    print("\n[2] STRATEGY OVERRIDES  (Telegram toggles)")
    overrides = _load_json(STRATEGY_OVERRIDES)
    if not overrides or "_error" in overrides:
        print("  All strategies at default (no overrides file)")
    else:
        any_disabled = False
        for strat, state in overrides.items():
            if state.get("disabled"):
                print(f"  🚫 {strat} DISABLED  (since {state.get('set_at','?')[:10]}, "
                      f"reason: {state.get('reason','?')})")
                any_disabled = True
        if not any_disabled:
            print("  All strategies enabled")

    # ── 3. Symbol blacklist (trade blocker #3) ────────────────────────────────
    print("\n[3] SYMBOL BLACKLIST")
    blacklist = _load_json(SYMBOL_BLACKLIST)
    now = datetime.now(timezone.utc)
    if not blacklist or "_error" in blacklist:
        print("  No symbols blacklisted")
    else:
        permanent = []
        temp      = []
        for sym, entry in blacklist.items():
            exp = entry.get("expires_at", "")
            if exp == "PERMANENT":
                permanent.append((sym, entry))
            elif exp and datetime.fromisoformat(exp) > now:
                temp.append((sym, entry, exp[:10]))
        if permanent:
            print(f"  PERMANENT ({len(permanent)}): "
                  f"{', '.join(s for s,_ in permanent)}")
        if temp:
            for sym, entry, exp in temp:
                print(f"  TEMP: {sym} until {exp} — {entry.get('reason','')}")
        if not permanent and not temp:
            print("  No active bans")

    # ── 4. Loss cooldowns (temporary symbol blocks) ───────────────────────────
    print("\n[4] LOSS COOLDOWNS  (30-min symbol blocks)")
    cooldown = _load_json(LOSS_COOLDOWN)
    if not cooldown or "_error" in cooldown:
        print("  No active cooldowns")
    else:
        active = []
        for sym, ts_str in cooldown.items():
            try:
                ts      = datetime.fromisoformat(ts_str)
                elapsed = (now - ts).total_seconds() / 60
                if elapsed < 30:
                    remaining = 30 - elapsed
                    active.append(f"{sym} ({remaining:.0f}m left)")
            except Exception:
                pass
        if active:
            print(f"  Active ({len(active)}): {', '.join(active)}")
        else:
            print("  None active (all expired)")

    # ── 5. ML data size (slows signal scoring) ────────────────────────────────
    print("\n[5] ML DATA FILES")
    sig_count = _count_jsonl(SIGNALS_JSONL)
    out_count = _count_jsonl(OUTCOMES_JSONL)
    print(f"  signals.jsonl  : {sig_count} records  ({_file_size_kb(SIGNALS_JSONL)})")
    print(f"  outcomes.jsonl : {out_count} records  ({_file_size_kb(OUTCOMES_JSONL)})")
    if sig_count > ML_JSONL_KEEP_RECORDS:
        print(f"  ⚠️  signals.jsonl has {sig_count} records "
              f"(run --clean to trim to {ML_JSONL_KEEP_RECORDS})")

    # ── 6. Session logs (disk accumulation) ───────────────────────────────────
    print("\n[6] SESSION LOGS")
    if LOGS_LIVE.exists():
        session_files = sorted(LOGS_LIVE.glob("*.json"))
        total_kb = sum(f.stat().st_size for f in session_files) / 1024
        print(f"  {len(session_files)} session files  ({total_kb:.0f}KB total)")
        old = [f for f in session_files
               if (now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc))
               > timedelta(days=SESSION_LOG_KEEP_DAYS)]
        if old:
            print(f"  ⚠️  {len(old)} files older than {SESSION_LOG_KEEP_DAYS}d "
                  f"(run --clean to archive)")
    else:
        print("  No session logs directory")

    # ── 7. Strategy logs ──────────────────────────────────────────────────────
    print("\n[7] STRATEGY LOGS")
    if LOGS_STRATEGIES.exists():
        for strat_dir in sorted(LOGS_STRATEGIES.iterdir()):
            if not strat_dir.is_dir():
                continue
            files    = list(strat_dir.glob("*"))
            total_kb = sum(f.stat().st_size for f in files if f.is_file()) / 1024
            print(f"  {strat_dir.name}: {len(files)} files  ({total_kb:.0f}KB)")

    # ── 8. Symbol cache ───────────────────────────────────────────────────────
    print("\n[8] SYMBOL CACHE")
    cache = _load_json(SYMBOL_CACHE)
    if "_error" not in cache and cache:
        count   = cache.get("count", len(cache.get("symbols", [])))
        updated = cache.get("updated", "?")[:16]
        print(f"  {count} symbols, last updated: {updated} UTC")
        print(f"  File: {_file_size_kb(SYMBOL_CACHE)}  ({_file_age(SYMBOL_CACHE)})")
    else:
        print("  Cache missing")

    # ── 9. Hardstop history ───────────────────────────────────────────────────
    print("\n[9] HARDSTOP HISTORY")
    hs_hist = _load_json(HARDSTOP_HISTORY)
    if not hs_hist or "_error" in hs_hist:
        print("  No history")
    else:
        print(f"  {len(hs_hist)} symbols tracked  ({_file_size_kb(HARDSTOP_HISTORY)})")
        for sym, entry in hs_hist.items():
            if isinstance(entry, dict):
                hits      = len(entry.get("hits", []))
                ban_count = entry.get("ban_count", 0)
                print(f"  {sym}: {hits} hits in window, {ban_count} prior ban(s)")

    print("\n" + "="*60)


# ─── Cleanup ──────────────────────────────────────────────────────────────────

def clean():
    print("\n[CLEAN] Starting cleanup...")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)

    # ── Archive old session logs ───────────────────────────────────────────────
    if LOGS_LIVE.exists():
        archive_session = ARCHIVE_DIR / "sessions"
        archive_session.mkdir(exist_ok=True)
        moved = 0
        for f in sorted(LOGS_LIVE.glob("*.json")):
            age = now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if age > timedelta(days=SESSION_LOG_KEEP_DAYS):
                shutil.move(str(f), archive_session / f.name)
                moved += 1
        print(f"  Archived {moved} session log(s) older than {SESSION_LOG_KEEP_DAYS}d")

    # ── Archive old strategy logs ──────────────────────────────────────────────
    if LOGS_STRATEGIES.exists():
        moved = 0
        for strat_dir in LOGS_STRATEGIES.iterdir():
            if not strat_dir.is_dir():
                continue
            archive_strat = ARCHIVE_DIR / "strategies" / strat_dir.name
            archive_strat.mkdir(parents=True, exist_ok=True)
            for f in strat_dir.glob("*"):
                if not f.is_file():
                    continue
                age = now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if age > timedelta(days=STRATEGY_LOG_KEEP_DAYS):
                    shutil.move(str(f), archive_strat / f.name)
                    moved += 1
        print(f"  Archived {moved} strategy log(s) older than {STRATEGY_LOG_KEEP_DAYS}d")

    # ── Trim ML JSONL files (keep last N records) ─────────────────────────────
    for jsonl_path, label in [
        (SIGNALS_JSONL,  "signals.jsonl"),
        (OUTCOMES_JSONL, "outcomes.jsonl"),
    ]:
        if not jsonl_path.exists():
            continue
        lines = [l for l in jsonl_path.read_text().splitlines() if l.strip()]
        if len(lines) <= ML_JSONL_KEEP_RECORDS:
            print(f"  {label}: {len(lines)} records — no trim needed")
            continue
        # Archive full copy first
        archive_ml = ARCHIVE_DIR / "ml"
        archive_ml.mkdir(exist_ok=True)
        ts_tag = now.strftime("%Y%m%d_%H%M")
        shutil.copy2(jsonl_path, archive_ml / f"{jsonl_path.stem}_{ts_tag}.jsonl")
        # Keep only last N records
        kept = lines[-ML_JSONL_KEEP_RECORDS:]
        jsonl_path.write_text("\n".join(kept) + "\n")
        print(f"  {label}: trimmed {len(lines)} → {len(kept)} records "
              f"(full copy archived)")

    # ── Prune expired loss cooldowns from disk ─────────────────────────────────
    cooldown = _load_json(LOSS_COOLDOWN)
    if cooldown and "_error" not in cooldown:
        before = len(cooldown)
        active = {}
        for sym, ts_str in cooldown.items():
            try:
                ts = datetime.fromisoformat(ts_str)
                if (now - ts).total_seconds() < 30 * 60:
                    active[sym] = ts_str
            except Exception:
                pass
        if len(active) < before:
            LOSS_COOLDOWN.write_text(json.dumps(active, indent=2))
            print(f"  loss_cooldown.json: pruned {before - len(active)} expired entries")
        else:
            print(f"  loss_cooldown.json: {before} entries, none expired")

    # ── Prune hardstop history to window ──────────────────────────────────────
    hs_hist = _load_json(HARDSTOP_HISTORY)
    if hs_hist and "_error" not in hs_hist:
        from modules.symbol_blacklist import HARD_STOP_WINDOW_HOURS
        cutoff = (now - timedelta(hours=HARD_STOP_WINDOW_HOURS)).isoformat()
        changed = False
        for sym in list(hs_hist.keys()):
            entry = hs_hist[sym]
            if isinstance(entry, dict):
                before = len(entry.get("hits", []))
                entry["hits"] = [h for h in entry["hits"] if h["ts"] >= cutoff]
                if len(entry["hits"]) < before:
                    changed = True
        if changed:
            HARDSTOP_HISTORY.write_text(json.dumps(hs_hist, indent=2))
            print("  hardstop_history.json: pruned old hit entries")

    print("[CLEAN] Done.\n")


# ─── Resets ───────────────────────────────────────────────────────────────────

def reset_overrides():
    """Re-enable all strategies — clears strategy_overrides.json."""
    if STRATEGY_OVERRIDES.exists():
        STRATEGY_OVERRIDES.write_text(json.dumps({}, indent=2))
        print("✅ Strategy overrides cleared — all strategies re-enabled")
        print("   Bot reads this file hot — no restart needed")
    else:
        print("   No overrides file found (already clear)")


def reset_blacklist():
    """Clear all blacklisted symbols and their history."""
    cleared = []
    if SYMBOL_BLACKLIST.exists():
        data = _load_json(SYMBOL_BLACKLIST)
        cleared = list(data.keys()) if isinstance(data, dict) else []
        SYMBOL_BLACKLIST.write_text(json.dumps({}, indent=2))
    if HARDSTOP_HISTORY.exists():
        HARDSTOP_HISTORY.write_text(json.dumps({}, indent=2))
    if cleared:
        print(f"✅ Blacklist cleared: {', '.join(cleared)}")
    else:
        print("   Blacklist was already empty")
    print("   Bot reads this file hot — no restart needed")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CSB bot diagnostics and cleanup")
    parser.add_argument("--clean",            action="store_true",
                        help="Archive old logs, trim ML JSONL files")
    parser.add_argument("--reset-overrides",  action="store_true",
                        help="Re-enable all Telegram-disabled strategies")
    parser.add_argument("--reset-blacklist",  action="store_true",
                        help="Clear all blacklisted symbols")
    parser.add_argument("--reset-all",        action="store_true",
                        help="--reset-overrides + --reset-blacklist")
    args = parser.parse_args()

    diagnose()

    if args.clean:
        clean()
    if args.reset_overrides or args.reset_all:
        reset_overrides()
    if args.reset_blacklist or args.reset_all:
        reset_blacklist()

    if not any(vars(args).values()):
        print("\nRun with --clean to archive logs and trim ML files.")
        print("Run with --reset-overrides to re-enable disabled strategies.")
        print("Run with --reset-blacklist to unban all symbols.")

