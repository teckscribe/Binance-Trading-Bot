"""
kronos/shadow_worker.py  --  runs in kronos/venv, OUT of the bot's process.

Consumes the shadow queue (logs/shadow_requests.jsonl) that the bot appends CSM
candidates to, scores each with Kronos-small, and records what it WOULD have
gated -- without ever affecting a trade (observe-only).

For each queued candidate it fetches that symbol's recent 15m klines from
Binance USDM public API (no auth), builds the LEAK-FREE context (bars whose
close <= the candidate's timestamp), scores, and appends to
logs/shadow_scores.jsonl:

    {...request..., dir_ret, pred_fav, pred_adv, would_gate, scored_at}

would_gate = pred_fav >= KRONOS_PF_THR (default 0.020, the validated gate).

Robust by construction: every candidate is scored in its own try/except; a bad
symbol or a network blip is logged as an error row and the loop continues.  It
tails the queue by byte offset (state file) so a restart resumes, not repeats.

Run as a systemd service (see kronos/kronos-shadow.service) or manually:
    KRONOS_THREADS=2 kronos/venv/bin/python kronos/shadow_worker.py
"""
import os, sys, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from scorer import KronosScorer

LOGDIR = os.path.join(_HERE, "logs")
REQ = os.path.join(LOGDIR, "shadow_requests.jsonl")
OUT = os.path.join(LOGDIR, "shadow_scores.jsonl")
STATE = os.path.join(LOGDIR, ".shadow_offset")
THR = float(os.getenv("KRONOS_PF_THR", "0.020"))
POLL = float(os.getenv("KRONOS_POLL_SEC", "5"))
KLINES = "https://fapi.binance.com/fapi/v1/klines"
_Q = pd.Timedelta(minutes=15)


def _fetch_15m(symbol, limit=400):
    q = urllib.parse.urlencode({"symbol": symbol.upper(), "interval": "15m", "limit": limit})
    with urllib.request.urlopen(f"{KLINES}?{q}", timeout=10) as r:
        raw = json.load(r)
    rows = []
    for k in raw:
        rows.append({
            "open_time": pd.to_datetime(int(k[0]), unit="ms"),
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
            "close_time": pd.to_datetime(int(k[6]), unit="ms"),
            "amount": float(k[7]),          # quote asset volume
        })
    df = pd.DataFrame(rows).set_index("open_time")
    return df


def _read_offset():
    try:
        return int(open(STATE).read().strip())
    except Exception:
        return 0


def _write_offset(n):
    try:
        with open(STATE, "w") as f:
            f.write(str(n))
    except Exception:
        pass


def _append(rec):
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")


def score_request(sc, req):
    df = _fetch_15m(req["symbol"])
    # leak-free: keep only bars fully closed at/before the candidate timestamp
    ts = pd.Timestamp(req["ts"]).tz_localize(None) if pd.Timestamp(req["ts"]).tz else pd.Timestamp(req["ts"])
    ctx = df[df["close_time"] <= ts]
    ctx = ctx[["open", "high", "low", "close", "volume", "amount"]]
    s = sc.score_window(ctx, req["direction"])
    if s is None:
        return {**req, "err": "insufficient_context", "scored_at": datetime.now(timezone.utc).isoformat()}
    return {**req, **s, "would_gate": bool(s["pred_fav"] >= THR),
            "scored_at": datetime.now(timezone.utc).isoformat()}


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    print(f"[shadow] loading Kronos-small (THR={THR}) ...", flush=True)
    sc = KronosScorer()
    print(f"[shadow] ready. polling {REQ} every {POLL}s", flush=True)
    off = _read_offset()
    while True:
        try:
            if os.path.exists(REQ):
                with open(REQ) as f:
                    f.seek(off)
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                req = json.loads(line)
                                rec = score_request(sc, req)
                            except Exception as exc:
                                rec = {"raw": line[:200], "err": str(exc)[:160],
                                       "scored_at": datetime.now(timezone.utc).isoformat()}
                            _append(rec)
                            tag = rec.get("would_gate")
                            print(f"[shadow] {rec.get('symbol','?')} {rec.get('direction','?')} "
                                  f"pred_fav={rec.get('pred_fav')} gate={tag} err={rec.get('err','')}", flush=True)
                    off = f.tell()
                _write_offset(off)
        except Exception as exc:
            print(f"[shadow] loop error: {exc}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()

