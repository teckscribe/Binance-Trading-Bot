"""
tools/replay/sync.py — keep the replay tree byte-identical to production.

Why this exists
---------------
The whole reason live and backtest diverged is that they were two separate
implementations of the same logic, drifting independently for months. Copying
the code into a second tree so the replay cannot touch production state
reintroduces that risk at the FILE level: edit a strategy in production, forget
the replay tree, and the replay faithfully measures a bot that no longer exists.

So the replay refuses to run unless every decision-making file hashes identical
to production. Isolation of STATE, zero drift in CODE.

    python tools/replay/sync.py --status     # what differs, change nothing
    python tools/replay/sync.py --pull       # copy production -> replay
    python tools/replay/sync.py --verify     # exit 1 if anything differs

Layout on the Ubuntu box:

    /home/psms/ubuntu/program_files/csb          production — never written to
    /home/psms/ubuntu/program_files/csb-replay   this tree
        data/   logs/   .env                     entirely its own

Override the pair with CSB_PROD_DIR / CSB_REPLAY_DIR.
"""

import os
import sys
import shutil
import hashlib
import argparse

PROD = os.getenv("CSB_PROD_DIR", "/home/psms/ubuntu/program_files/csb")
REPLAY = os.getenv("CSB_REPLAY_DIR", "/home/psms/ubuntu/program_files/test")

# Everything that can change a trading decision. If a file here differs, the
# replay is measuring something other than production and must not run.
TRACKED = [
    "live_scanner.py",
    "live_logger.py",
    "backtest_optimizer.py",
    "modules/risk_engine.py",
    "modules/regime_engine.py",
    "modules/order_engine.py",
    "modules/data_hub.py",
    "modules/data_feed.py",
    "modules/ml_engine.py",
    "modules/strategy_overrides.py",
    "modules/paper_equity.py",
    "modules/watchlist.py",
    "modules/symbol_filter.py",
    "modules/symbol_blacklist.py",
    "modules/blacklist.py",
    "modules/auth_manager.py",
    "modules/strategies/base_strategy.py",
    "modules/strategies/strategy_factory.py",
    "modules/strategies/cross_sectional_momentum.py",
    "modules/strategies/freqtrade_port_nasos.py",
    "modules/strategies/grid_strategy.py",
    "modules/strategies/trend_pullback.py",
    "modules/strategies/funding_fade_v2.py",
]

# Deliberately NOT tracked, because the replay must own them:
#   .env            replay needs its own (LIVE_ENABLED=false, its own equity)
#   data/ logs/     replay state must never mix with production's
#   tools/          replay-only code, does not exist in production


def _sha(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compare():
    """Returns (same, differing, missing_in_replay, missing_in_prod)."""
    same, diff, miss_r, miss_p = [], [], [], []
    for rel in TRACKED:
        a, b = _sha(os.path.join(PROD, rel)), _sha(os.path.join(REPLAY, rel))
        if a is None and b is None:
            continue
        if a is None:
            miss_p.append(rel)
        elif b is None:
            miss_r.append(rel)
        elif a == b:
            same.append(rel)
        else:
            diff.append(rel)
    return same, diff, miss_r, miss_p


def status(quiet=False):
    same, diff, miss_r, miss_p = compare()
    if not quiet:
        print(f"production : {PROD}")
        print(f"replay     : {REPLAY}\n")
        print(f"  identical         {len(same)}")
        for rel in diff:
            print(f"  DIFFERS           {rel}")
        for rel in miss_r:
            print(f"  MISSING in replay {rel}")
        for rel in miss_p:
            print(f"  MISSING in prod   {rel}  <- replay-only, investigate")
    return diff + miss_r + miss_p


def pull():
    if not os.path.isdir(PROD):
        sys.exit(f"Production tree not found: {PROD}")
    n = 0
    for rel in TRACKED:
        src = os.path.join(PROD, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(REPLAY, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if _sha(src) != _sha(dst):
            shutil.copy2(src, dst)
            print(f"  updated {rel}")
            n += 1
    # Package markers, so imports resolve inside the replay tree.
    for pkg in ("modules", "modules/strategies", "tools", "tools/replay"):
        os.makedirs(os.path.join(REPLAY, pkg), exist_ok=True)
        init = os.path.join(REPLAY, pkg, "__init__.py")
        if not os.path.exists(init):
            open(init, "a").close()
    for sub in ("data", "logs/live", "logs/strategies"):
        os.makedirs(os.path.join(REPLAY, sub), exist_ok=True)
    print(f"\n{n} file(s) updated; {len(TRACKED) - n} already identical.")


def require_clean():
    """
    Call at replay start. Aborts if the replay tree has drifted from
    production — a replay of stale code is worse than no replay, because it
    produces numbers that look authoritative and describe nothing.
    """
    bad = status(quiet=True)
    if bad:
        print("REPLAY ABORTED — code drift vs production:", file=sys.stderr)
        for rel in bad:
            print(f"  {rel}", file=sys.stderr)
        print("\nRun:  python tools/replay/sync.py --pull", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sync/verify the replay tree")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--pull", action="store_true")
    g.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    if a.pull:
        pull()
    elif a.verify:
        bad = status(quiet=True)
        if bad:
            print("DRIFT:", ", ".join(bad))
            sys.exit(1)
        print(f"clean — all {len(TRACKED)} tracked files identical to production")
    else:
        status()
