"""
kronos/enrich_ablation.py  --  offline measurement rig (safe; touches no live code).

Builds the Kronos-enriched ML training set and answers ONE question:
does Kronos's pred_fav add predictive value ON TOP of the ML engine's 27
features?  Runs anywhere (numpy + pandas only -- no sklearn/scipy), so it works
in kronos/venv on the server or any plain python.

Pipeline:
  1. join data/ml/signals.jsonl x data/ml/outcomes.jsonl on signal_id  -> labelled
  2. match each CSM row to its Kronos verdict in kronos/logs/shadow_scores.jsonl
     by symbol + nearest timestamp + entry-price closeness
  3. write kronos/logs/ml_kronos_enriched.jsonl (the growing enriched dataset)
  4. if enough matched rows: cross-validated logistic-regression ABLATION
     AUC(base 27 feats) vs AUC(base + pred_fav/dir_ret/pred_adv); else DEFER.

Re-run as data accumulates -- the moment coverage is large enough it prints the
verdict instead of deferring, closing the "wait for data" gap to one command.

NOTE: linear (logistic) ablation is a conservative proxy; the production model
is a RandomForest.  A positive linear signal is strong evidence; a null linear
result should be re-checked with the RF (run in the bot venv) before concluding.
"""
import os, sys, json, math
from datetime import datetime
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SIGNALS = os.path.join(_ROOT, "data", "ml", "signals.jsonl")
OUTCOMES = os.path.join(_ROOT, "data", "ml", "outcomes.jsonl")
SCORES = os.path.join(_HERE, "logs", "shadow_scores.jsonl")
ENRICHED = os.path.join(_HERE, "logs", "ml_kronos_enriched.jsonl")
KFEATS = ["pred_fav", "dir_ret", "pred_adv"]
MIN_ABLATE = int(os.getenv("MIN_ABLATE", "80"))     # min matched rows to attempt ablation
PRICE_TOL = 0.005                                    # 0.5% entry-price match tolerance
TIME_TOL_S = 600                                     # 10 min timestamp match window


def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try: out.append(json.loads(ln))
            except Exception: pass
    return out


def _ts(s):
    try: return datetime.fromisoformat(s)
    except Exception: return None


def build():
    sigs = {s["signal_id"]: s for s in _load_jsonl(SIGNALS) if "signal_id" in s}
    outs = {o["signal_id"]: o for o in _load_jsonl(OUTCOMES) if "signal_id" in o}
    scores = [r for r in _load_jsonl(SCORES) if "err" not in r and r.get("entry_price")]
    byS = {}
    for r in scores:
        byS.setdefault(r["symbol"], []).append(r)

    labelled = []
    for sid, s in sigs.items():
        o = outs.get(sid)
        if o is None:
            continue
        pnl = o.get("pnl_equity_pct", o.get("pnl_pct"))
        if pnl is None:
            continue
        row = {"signal_id": sid, "symbol": s.get("symbol"), "strategy": s.get("strategy"),
               "direction": s.get("direction"), "entry_price": s.get("entry_price"),
               "ts": s.get("timestamp"), "features": s.get("features", {}),
               "pnl": float(pnl), "win": int(float(pnl) > 0)}
        labelled.append(row)

    # match Kronos verdict. Candidates are keyed by symbol; rows carry a
    # `strategy` tag since 2026-09-14 (NASOS_V4 queued alongside CSM) and
    # older rows without one are CSM. A trade only matches a candidate of the
    # same strategy so a NASOS entry cannot borrow a CSM score.
    matched = 0
    for row in labelled:
        row["kronos"] = None
        cands = [c for c in byS.get(row["symbol"], [])
                 if c.get("strategy", "CSM") == row["strategy"]]
        et = _ts(row["ts"]); ep = row["entry_price"]
        if not cands or et is None or ep is None:
            continue
        best, bestkey = None, None
        for c in cands:
            ct = _ts(c["ts"])
            if ct is None:
                continue
            dt = abs((ct - et).total_seconds())
            dp = abs(float(c["entry_price"]) - float(ep)) / float(ep)
            if dt <= TIME_TOL_S and dp <= PRICE_TOL:
                key = (dp, dt)
                if bestkey is None or key < bestkey:
                    best, bestkey = c, key
        if best is not None:
            row["kronos"] = {k: best.get(k) for k in KFEATS + ["would_gate"]}
            matched += 1
    return labelled, matched


def _standardize(X):
    mu = X.mean(0); sd = X.std(0); sd[sd == 0] = 1.0
    return (X - mu) / sd


def _auc(y, s):
    # rank-based AUC (Mann-Whitney); handles ties
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), float); ranks[order] = np.arange(1, len(s) + 1)
    # average ranks for ties
    s_sorted = s[order]; i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    n1 = y.sum(); n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _fit_logit(X, y, l2=1.0, iters=400, lr=0.1):
    X = np.hstack([np.ones((len(X), 1)), _standardize(X)])
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) / len(y) + l2 * np.r_[0, w[1:]] / len(y)
        w -= lr * g
    return w


def _predict(Xtr, ytr, Xte):
    mu = Xtr.mean(0); sd = Xtr.std(0); sd[sd == 0] = 1.0
    w = _fit_logit(Xtr, ytr)
    Xte_s = np.hstack([np.ones((len(Xte), 1)), (Xte - mu) / sd])
    return 1 / (1 + np.exp(-Xte_s @ w))


def _cv_auc(X, y, folds=5, seed=0):
    rng = np.random.RandomState(seed); idx = rng.permutation(len(y))
    aucs = []
    for f in range(folds):
        te = idx[f::folds]; tr = np.setdiff1d(idx, te)
        if y[tr].sum() in (0, len(tr)) or y[te].sum() in (0, len(te)):
            continue
        p = _predict(X[tr], y[tr].astype(float), X[te])
        a = _auc(y[te], p)
        if not math.isnan(a):
            aucs.append(a)
    return (float(np.mean(aucs)), len(aucs)) if aucs else (float("nan"), 0)


def main():
    labelled, matched = build()
    os.makedirs(os.path.dirname(ENRICHED), exist_ok=True)
    with open(ENRICHED, "w") as f:
        for r in labelled:
            f.write(json.dumps(r) + "\n")
    print(f"labelled (signal x outcome): {len(labelled)}")
    print(f"enriched dataset -> {ENRICHED}")
    strategies = sorted({r["strategy"] for r in labelled if r.get("strategy")})
    for sid in strategies:
        rows = [r for r in labelled if r["strategy"] == sid]
        km = [r for r in rows if r.get("kronos")]
        line = f"  {sid:10} trades {len(rows):4d}   matched to Kronos: {len(km):4d}"
        if km:
            wins = sum(r["win"] for r in km)
            line += (f"   win {100*wins/len(km):.1f}%   pred_fav med "
                     f"{np.median([r['kronos']['pred_fav'] for r in km]):+.4f}")
        print(line)
    for sid in strategies:
        km = [r for r in labelled if r["strategy"] == sid and r.get("kronos")]
        if not km:
            continue
        if len(km) < MIN_ABLATE:
            print(f"\n{sid}: ABLATION DEFERRED — need >= {MIN_ABLATE} matched rows, have {len(km)} "
                  f"(~{MIN_ABLATE - len(km)} more).")
            continue
        _ablate(sid, km)


def _ablate(sid, km):
    # assemble matrices
    fnames = list(km[0]["features"].keys())
    Xb = np.array([[float(r["features"].get(k, 0.0)) for k in fnames] for r in km])
    Xk = np.array([[float(r["kronos"][k]) for k in KFEATS] for r in km])
    y = np.array([r["win"] for r in km])
    Xp = np.hstack([Xb, Xk])

    print(f"\n== {sid} ABLATION (5-fold CV logistic AUC, N={len(km)}) ==")
    a_uni, _ = _cv_auc(Xk, y)
    a_base, nb = _cv_auc(Xb, y)
    a_plus, npl = _cv_auc(Xp, y)
    print(f"  pred_fav/dir/adv ALONE : AUC {a_uni:.3f}")
    print(f"  27 base features       : AUC {a_base:.3f}")
    print(f"  base + Kronos          : AUC {a_plus:.3f}   dAUC {a_plus-a_base:+.3f}")
    print("  (dAUC > ~+0.02 and stable => Kronos adds value on top of ML; "
          "~0 => redundant)")


if __name__ == "__main__":
    main()

