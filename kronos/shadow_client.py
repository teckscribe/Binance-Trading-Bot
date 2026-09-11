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
import os, json
from datetime import datetime, timezone

_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "shadow_requests.jsonl")


def log_candidate(symbol, direction, entry_price=None, strength=None, ts=None):
    """Append one CSM candidate to the shadow queue.  Best-effort, never raises."""
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        rec = {
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "strength": strength,
        }
        with open(_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # shadow logging must NEVER affect trading

