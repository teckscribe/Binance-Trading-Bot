"""
kronos/shadow_client.py  --  STDLIB ONLY.  Safe to import from the bot's venv.

This is the ONLY part of kronos/ the live bot may import.  It contains no torch,
no pandas, no Kronos -- just a best-effort append to a queue file.  The Kronos
worker (kronos/shadow_worker.py, in kronos/venv) consumes that queue out of
process, so the bot never loads the model and a scorer problem can never touch
the order loop.

Usage in live_scanner.py, right after a CSM candidate is accepted:

    if _sid == "CSM":
        try:
            from kronos.shadow_client import log_candidate
            log_candidate(sig.get("symbol"), sig.get("direction"),
                          sig.get("entry_price"), sig.get("strength"))
        except Exception:
            pass

Every failure is swallowed: shadow logging must NEVER affect trading.
"""
import os, json, time
from datetime import datetime, timezone

_LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOG = os.path.join(_LOGDIR, "shadow_requests.jsonl")
_SCORES = os.path.join(_LOGDIR, "shadow_scores.jsonl")


def log_candidate(symbol, direction, entry_price=None, strength=None, ts=None):
    """Append one CSM candidate to the shadow queue.  Best-effort, never raises.

    Returns the candidate's ts string (the key find_score() matches on), or
    None if the append failed.
    """
    try:
        os.makedirs(_LOGDIR, exist_ok=True)
        rec = {
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "strength": strength,
        }
        with open(_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec["ts"]
    except Exception:
        return None  # shadow logging must NEVER affect trading


def find_score(symbol, ts, tail_bytes=262144):
    """The worker's score row for one candidate, or None if not scored yet.

    The worker copies the request fields into its output row, so (symbol, ts)
    is an exact key. Only the tail of the scores file is read — a few hundred
    rows — which is far more than can accumulate between a candidate being
    queued and the entry step asking about it.
    """
    try:
        size = os.path.getsize(_SCORES)
        with open(_SCORES, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None
    for line in reversed(data.split(b"\n")):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("symbol") == symbol and r.get("ts") == ts:
            return r
    return None


def wait_for_score(symbol, ts, timeout_sec, poll_sec=0.5):
    """Poll find_score() until a row appears or timeout_sec elapses."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        r = find_score(symbol, ts)
        if r is not None or time.monotonic() >= deadline:
            return r
        time.sleep(poll_sec)

