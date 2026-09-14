"""
kronos/whale_worker.py  --  observe-only positioning/"whale" collector.

Reads the SAME CSM-candidate queue the Kronos worker uses
(logs/shadow_requests.jsonl) and, for each candidate, records Binance futures
positioning data AS OF the candidate time -- so we can forward-test whether
whale/positioning activity separates CSM winners from losers.  Binance only
serves 30 days of positioning history, so this can only be validated forward;
this collector is that forward data source.

Never affects trades (no bot change, no gate).  Own offset file, so it runs
alongside the Kronos worker on the same queue without conflict.  Stdlib only.

Per candidate it appends to logs/whale_scores.jsonl:
  top_ls    top-trader long/short ratio        (topLongShortPositionRatio)
  glob_ls   retail long/short ratio            (globalLongShortAccountRatio)
  taker_bs  aggressive taker buy/sell ratio    (takerlongshortRatio)
  oi        sum open interest                  (openInterestHist)
  oi_chg6   OI change vs 6 bars earlier
All leak-free: only positioning bars fully closed before the candidate time.

Run:  kronos/venv/bin/python kronos/whale_worker.py   (systemd: whale-shadow)
"""
import os, json, time, urllib.request
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(_HERE, "logs")
REQ = os.path.join(LOGDIR, "shadow_requests.jsonl")
OUT = os.path.join(LOGDIR, "whale_scores.jsonl")
STATE = os.path.join(LOGDIR, ".whale_offset")
POLL = float(os.getenv("WHALE_POLL_SEC", "15"))
PERIOD = os.getenv("WHALE_PERIOD", "1h")
PERIOD_MS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
             "2h": 7200, "4h": 14400}.get(PERIOD, 3600) * 1000
CACHE_TTL = float(os.getenv("WHALE_CACHE_SEC", "1800"))
BASE = "https://fapi.binance.com/futures/data/"
EPS = {"top": ("topLongShortPositionRatio", "longShortRatio"),
       "glob": ("globalLongShortAccountRatio", "longShortRatio"),
       "taker": ("takerlongshortRatio", "buySellRatio"),
       "oi": ("openInterestHist", "sumOpenInterest")}

_cache = {}


def _fetch_series(sym):
    out = {}
    for k, (ep, field) in EPS.items():
        try:
            u = f"{BASE}{ep}?symbol={sym.upper()}&period={PERIOD}&limit=1000"
            d = json.load(urllib.request.urlopen(u, timeout=15))
            out[k] = [(int(x["timestamp"]), float(x[field])) for x in d]  # ascending
        except Exception:
            out[k] = []
        time.sleep(0.1)
    return out


def _series(sym):
    now = time.time(); c = _cache.get(sym)
    if c and now - c[0] < CACHE_TTL:
        return c[1]
    s = _fetch_series(sym); _cache[sym] = (now, s)
    if len(_cache) > 150:
        for kk in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
            _cache.pop(kk, None)
    return s


def _asof(series, et_ms, back=0):
    cut = et_ms - PERIOD_MS               # last bar fully closed before entry
    vals = [v for ts, v in series if ts <= cut]
    return vals[-1 - back] if len(vals) >= 1 + back else None


def score(req):
    et_ms = int(datetime.fromisoformat(req["ts"]).timestamp() * 1000)
    S = _series(req["symbol"])
    top = _asof(S["top"], et_ms); glob = _asof(S["glob"], et_ms)
    tak = _asof(S["taker"], et_ms)
    oi = _asof(S["oi"], et_ms); oi6 = _asof(S["oi"], et_ms, back=6)
    if top is None and glob is None and tak is None and oi is None:
        return {**req, "err": "no_positioning_data", "collected_at": datetime.now(timezone.utc).isoformat()}
    return {**req, "top_ls": top, "glob_ls": glob, "taker_bs": tak, "oi": oi,
            "oi_chg6": ((oi - oi6) / oi6) if (oi and oi6 and oi6 > 0) else None,
            "period": PERIOD, "collected_at": datetime.now(timezone.utc).isoformat()}


def _read_off():
    try: return int(open(STATE).read().strip())
    except Exception: return 0


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    print(f"[whale] collector up. period={PERIOD} polling {REQ} every {POLL}s", flush=True)
    off = _read_off()
    n = 0
    while True:
        try:
            if os.path.exists(REQ):
                batch = 0
                with open(REQ) as f:
                    f.seek(off)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = score(json.loads(line))
                        except Exception as exc:
                            rec = {"raw": line[:200], "err": str(exc)[:160]}
                        with open(OUT, "a") as fo:
                            fo.write(json.dumps(rec) + "\n")
                        n += 1; batch += 1
                        # Throttle: heartbeat every 200 rows, but always show errors.
                        if rec.get("err") or n % 200 == 0:
                            print(f"[whale] #{n} {rec.get('symbol','?')} top_ls={rec.get('top_ls')} "
                                  f"oi_chg6={rec.get('oi_chg6')} err={rec.get('err','')}", flush=True)
                    off = f.tell()
                if batch:
                    print(f"[whale] processed {batch} candidates (total {n})", flush=True)
                try:
                    open(STATE, "w").write(str(off))
                except Exception:
                    pass
        except Exception as exc:
            print(f"[whale] loop error: {exc}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()

