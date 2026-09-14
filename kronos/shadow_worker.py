"""
kronos/shadow_worker.py  --  runs in kronos/venv, OUT of the bot's process.

Consumes the shadow queue (logs/shadow_requests.jsonl) that the bot appends
candidates to, scores each with Kronos-small, and records the raw scores --
without ever affecting a trade (observe-only).

For each queued candidate it fetches that symbol's recent 15m klines from
Binance USDM public API (no auth), builds the LEAK-FREE context (bars whose
close <= the candidate's timestamp), scores, and appends to
logs/shadow_scores.jsonl:

    {...request..., dir_ret, pred_fav, pred_adv, scored_at}

The worker holds NO threshold. The live gate compares pred_fav against the
hot KRONOS_PF_THR setting in the scanner, and kronos/progress.py derives the
"would block" count from pred_fav at that same setting, so there is exactly
one copy of the number. (Rows written before 2026-09-14 also carry a
would_gate flag stamped at the then-fixed 0.020; nothing reads it any more.)

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
POLL = float(os.getenv("KRONOS_POLL_SEC", "5"))
KLINES = "https://fapi.binance.com/fapi/v1/klines"
_Q = pd.Timedelta(minutes=15)


def _fetch_15m(symbol, limit=400, end_time_ms=None):
    params = {"symbol": symbol.upper(), "interval": "15m", "limit": limit}
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    q = urllib.parse.urlencode(params)
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
    # Normalize timestamp to naive UTC (Binance kline close_time is UTC)
    _raw_ts = pd.Timestamp(req["ts"])
    if _raw_ts.tz is not None:
        ts = _raw_ts.tz_convert("UTC").tz_localize(None)
    else:
        ts = _raw_ts
    end_ms = int(ts.timestamp() * 1000)
    df = _fetch_15m(req["symbol"], end_time_ms=end_ms)
    # leak-free: keep only bars fully closed at/before the candidate timestamp
    ctx = df[df["close_time"] <= ts]
    ctx = ctx[["open", "high", "low", "close", "volume", "amount"]]
    s = sc.score_window(ctx, req["direction"])
    if s is None:
        return {**req, "err": "insufficient_context", "scored_at": datetime.now(timezone.utc).isoformat()}
    return {**req, **s, "scored_at": datetime.now(timezone.utc).isoformat()}


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    print("[shadow] loading Kronos-small ...", flush=True)
    sc = KronosScorer()
    print(f"[shadow] ready. polling {REQ} every {POLL}s", flush=True)
    off = _read_offset()
    while True:
        try:
            if os.path.exists(REQ):
                # Guard against file truncation/rotation: if file shrunk,
                # reset offset to re-read from the beginning
                fsize = os.path.getsize(REQ)
                if fsize < off:
                    print(f"[shadow] file truncated ({off} > {fsize}), resetting offset", flush=True)
                    off = 0
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
                            print(f"[shadow] {rec.get('symbol','?')} {rec.get('direction','?')} "
                                  f"pred_fav={rec.get('pred_fav')} err={rec.get('err','')}", flush=True)
                    off = f.tell()
                _write_offset(off)
        except Exception as exc:
            print(f"[shadow] loop error: {exc}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()

