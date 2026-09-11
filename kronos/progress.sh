#!/usr/bin/env bash
# kronos/progress.sh -- one-shot dashboard for every observe-only experiment.
# Run from the project root:  bash kronos/progress.sh
# Read-only: touches no trades, changes nothing.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
VENV=kronos/venv/bin/python
PY=python3
L=kronos/logs

echo "======== CSB experiment progress  ($(date -u +%Y-%m-%dT%H:%MZ)) ========"

echo
echo "-- services (want: active) --"
for s in csb kronos-shadow whale-shadow; do
  printf "  %-14s %s\n" "$s" "$(systemctl is-active "$s" 2>/dev/null)"
done

echo
echo "-- collection (queue vs consumers; equal = fully caught up) --"
for f in shadow_requests shadow_scores whale_scores; do
  if [ -f "$L/$f.jsonl" ]; then
    n=$(wc -l < "$L/$f.jsonl" 2>/dev/null || echo 0)
  else
    n=0
  fi
  printf "  %-18s %s rows\n" "$f" "$n"
done

echo
echo "-- ML clocks --"
$PY - <<'PYEOF'
import json, os
sig="data/ml/signals.jsonl"; out="data/ml/outcomes.jsonl"
no=sum(1 for _ in open(out)) if os.path.exists(out) else 0
print(f"  Phase 4 (model activates): {no}/200 outcomes")
sigs=[json.loads(l) for l in open(sig)] if os.path.exists(sig) else []
from collections import defaultdict
d=defaultdict(lambda:[0,0])
for s in sigs:
    st=s.get("strategy"); f=s.get("features",{})
    d[st][0]+=1
    if f.get("atr_pct",0)>0 and f.get("sl_dist_pct",0)>0: d[st][1]+=1
for st,(tot,u) in sorted(d.items()):
    print(f"  Phase 3 usable [{st}]: {u}/30  (of {tot} signals)")
PYEOF

echo
echo "-- Kronos-as-ML-feature ablation --"
$VENV kronos/enrich_ablation.py 2>/dev/null | tail -3 | sed 's/^/  /'

echo
echo "-- Whale/positioning --"
$VENV kronos/whale_analyze.py 2>/dev/null | tail -3 | sed 's/^/  /'

echo
echo "-- Kronos live-gate shadow (would_gate rate; forward OOS join is manual) --"
$PY - <<'PYEOF'
import json, os
f="kronos/logs/shadow_scores.jsonl"
if not os.path.exists(f): print("  (no scores yet)"); raise SystemExit
g=t=0
for l in open(f):
    try: r=json.loads(l)
    except: continue
    if "err" in r: continue
    t+=1; g+=1 if r.get("would_gate") else 0
print(f"  scored {t}   would_gate {100*g/t:.1f}%" if t else "  (none)")
PYEOF
echo "=================================================================="

