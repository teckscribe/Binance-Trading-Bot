"""
kronos/progress.py  --  read-only progress report for the two shadow experiments.

Answers "how far are Kronos and the whale collector from a verdict?" for the
dashboard. Stdlib only, touches nothing, safe to call from the web server.

The verdict thresholds and the candidate<->trade matching rules are the SAME
ones enrich_ablation.py (Kronos) and whale_analyze.py (whale) use, so the
"matched" figure here is exactly the N those tools will report.
"""
import os
import sys
import json
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
LOGDIR = os.path.join(_HERE, "logs")
REQ = os.path.join(LOGDIR, "shadow_requests.jsonl")
KRONOS_OUT = os.path.join(LOGDIR, "shadow_scores.jsonl")
KRONOS_OFF = os.path.join(LOGDIR, ".shadow_offset")
WHALE_OUT = os.path.join(LOGDIR, "whale_scores.jsonl")
WHALE_OFF = os.path.join(LOGDIR, ".whale_offset")
SIGNALS = os.path.join(_ROOT, "data", "ml", "signals.jsonl")
OUTCOMES = os.path.join(_ROOT, "data", "ml", "outcomes.jsonl")

KRONOS_TARGET = int(os.getenv("MIN_ABLATE", "80"))     # enrich_ablation.MIN_ABLATE
WHALE_TARGET = int(os.getenv("WHALE_MIN_N", "120"))    # whale_analyze.MIN_N
PRICE_TOL = 0.005
TIME_TOL_S = 600


def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    return out


def _ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _backlog(offset_file, queue_file):
    """Queue lines the worker has not consumed yet (byte offset -> line count)."""
    try:
        off = int(open(offset_file).read().strip() or 0)
    except Exception:
        off = 0
    try:
        size = os.path.getsize(queue_file)
    except OSError:
        return 0
    if off >= size:
        return 0
    n = 0
    with open(queue_file, "rb") as f:
        f.seek(off)
        for ln in f:
            if ln.strip():
                n += 1
    return n


SHADOW_STRATEGIES = ("CSM", "NASOS_V4", "TSMOM_4H", "REBALANCING_PREMIUM")


def _trades(strategy):
    sigs = {s["signal_id"]: s for s in _load_jsonl(SIGNALS) if "signal_id" in s}
    outs = {o["signal_id"]: o for o in _load_jsonl(OUTCOMES) if "signal_id" in o}
    rows = []
    for sid, s in sigs.items():
        if s.get("strategy") != strategy:
            continue
        o = outs.get(sid)
        if not o or o.get("pnl_equity_pct", o.get("pnl_pct")) is None:
            continue
        rows.append(s)
    return rows


def _match_kronos(trades, scores):
    """enrich_ablation.build(): nearest by (price diff, time diff), both within tolerance."""
    by_sym = {}
    for r in scores:
        by_sym.setdefault(r.get("symbol"), []).append(r)
    n = 0
    for s in trades:
        et, ep = _ts(s.get("timestamp")), s.get("entry_price")
        if et is None or not ep:
            continue
        for c in by_sym.get(s.get("symbol"), []):
            ct = _ts(c.get("ts"))
            if ct is None:
                continue
            try:
                dt = abs((ct - et).total_seconds())
                dp = abs(float(c["entry_price"]) - float(ep)) / float(ep)
            except (TypeError, ValueError, KeyError):
                continue
            if dt <= TIME_TOL_S and dp <= PRICE_TOL:
                n += 1
                break
    return n


def _match_whale(trades, rows):
    """whale_analyze.main(): nearest by (price diff, time diff); price within 0.5%."""
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r.get("symbol"), []).append(r)
    n = 0
    for s in trades:
        et, ep = _ts(s.get("timestamp")), s.get("entry_price")
        cands = by_sym.get(s.get("symbol"), [])
        if et is None or not ep or not cands:
            continue
        def key(r):
            ct = _ts(r.get("ts"))
            dt = abs((ct - et).total_seconds()) if ct else float("inf")
            return (abs(float(r.get("entry_price") or 0) - float(ep)) / float(ep), dt)
        best = min(cands, key=key)
        if key(best)[0] <= PRICE_TOL:
            n += 1
    return n


def _last(rows, field):
    vals = [r.get(field) for r in rows if r.get(field)]
    return max(vals) if vals else None


def progress() -> dict:
    queue = _load_jsonl(REQ)
    kscores = _load_jsonl(KRONOS_OUT)
    wrows = _load_jsonl(WHALE_OUT)
    trades = _trades("CSM")

    k_ok = [r for r in kscores if "err" not in r and r.get("entry_price")]
    k_err = len(kscores) - len(k_ok)
    # "Would block" is derived here from pred_fav at the LIVE threshold, not
    # read from the rows: the worker stamps no threshold, so this count always
    # agrees with what the gate actually does (and re-reads old rows the same
    # way when the threshold changes).
    gate = {"mode": "unknown", "threshold": None, "wait_sec": None}
    try:
        sys.path.insert(0, _ROOT)
        from modules import settings_manager as cfg
        gate = {"mode": cfg.get("KRONOS_GATE"), "threshold": cfg.get("KRONOS_PF_THR"),
                "wait_sec": cfg.get("KRONOS_GATE_WAIT_SEC")}
    except Exception:
        pass
    thr = gate["threshold"]
    k_gate = (sum(1 for r in k_ok
                  if r.get("pred_fav") is not None and float(r["pred_fav"]) >= thr)
              if thr is not None else 0)
    # rows without a strategy tag predate NASOS queueing and are CSM
    k_by = {s: [r for r in k_ok if r.get("strategy", "CSM") == s] for s in SHADOW_STRATEGIES}
    k_matched = _match_kronos(trades, k_by["CSM"])
    by_strategy = {s: {"scored": len(k_by[s]),
                       "matched": _match_kronos(_trades(s), k_by[s]),
                       "trades_with_outcome": len(_trades(s))}
                   for s in SHADOW_STRATEGIES}

    w_ok = [r for r in wrows if "err" not in r and r.get("top_ls") is not None]
    w_err = len(wrows) - len(w_ok)
    w_by = {s: [r for r in w_ok if r.get("strategy", "CSM") == s] for s in SHADOW_STRATEGIES}
    w_matched = _match_whale(trades, w_by["CSM"])
    w_by_strategy = {s: {"collected": len(w_by[s]),
                         "matched": _match_whale(_trades(s), w_by[s]),
                         "trades_with_outcome": len(_trades(s))}
                     for s in SHADOW_STRATEGIES}

    def block(matched, target, scored, errors, backlog, last, extra=None):
        pct = min(100.0, round(100.0 * matched / target, 1)) if target else 0.0
        d = {
            "matched": matched, "target": target, "pct": pct,
            "remaining": max(0, target - matched),
            "state": "READY" if matched >= target else "COLLECTING",
            "scored": scored, "errors": errors, "backlog": backlog,
            "last_activity": last,
        }
        if extra:
            d.update(extra)
        return d

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate": gate,
        "queue": {"candidates": len(queue), "last_ts": _last(queue, "ts")},
        "csm_trades_with_outcome": len(trades),
        "kronos": block(k_matched, KRONOS_TARGET, len(k_ok), k_err,
                        _backlog(KRONOS_OFF, REQ), _last(kscores, "scored_at"),
                        {"would_gate": k_gate,
                         "gate_rate_pct": round(100.0 * k_gate / len(k_ok), 1) if k_ok else 0.0,
                         "by_strategy": by_strategy,
                         "verdict_tool": "kronos/enrich_ablation.py"}),
        "whale": block(w_matched, WHALE_TARGET, len(w_ok), w_err,
                       _backlog(WHALE_OFF, REQ), _last(wrows, "collected_at") or _last(wrows, "ts"),
                       {"by_strategy": w_by_strategy,
                        "verdict_tool": "kronos/whale_analyze.py"}),
    }


if __name__ == "__main__":
    print(json.dumps(progress(), indent=2))
