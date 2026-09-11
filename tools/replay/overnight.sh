#!/usr/bin/env bash
# Unattended Tier 2 control run.
#
#   1. wait for the 1m fetch to finish (100 symbols x 2 series)
#   2. sanity-check the three regime symbols are present
#   3. replay production's CURRENT code (pre-fix) over 30 days x 100 symbols
#   4. produce the diagnostic report
#
# Detached on purpose: nohup + setsid so it outlives the shell that starts it.
# Resumable: the driver checkpoints every simulated day, so re-running this
# script after an interruption continues rather than restarting.

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # relocation-proof
CTRL="$ROOT/_tier2_control"
PY="$ROOT/.venv/Scripts/python.exe"
DATA="$ROOT/data"
LOG="$ROOT/tools/replay/overnight.log"

say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

say "=== Tier 2 control run starting ==="

# ── 1. wait for the fetch ────────────────────────────────────────────────────
say "waiting for 1m fetch (need 100 trade + 100 mark series)..."
for i in $(seq 1 240); do            # up to 4h of patience
  n1=$(ls "$DATA"/*_1m_30d.csv 2>/dev/null | wc -l)
  n2=$(ls "$DATA"/*_mark1m_30d.csv 2>/dev/null | wc -l)
  [ "$n1" -ge 100 ] && [ "$n2" -ge 100 ] && break
  [ $((i % 5)) -eq 0 ] && say "  fetch progress: ${n1}/100 trade, ${n2}/100 mark"
  sleep 60
done
say "fetch done: $(ls "$DATA"/*_1m_30d.csv | wc -l) trade, $(ls "$DATA"/*_mark1m_30d.csv | wc -l) mark"

# ── 2. the regime symbols are mandatory ──────────────────────────────────────
missing=""
for s in BTCUSDT ETHUSDT SOLUSDT; do
  [ -f "$DATA/${s}_1m_30d.csv" ] || missing="$missing $s"
done
if [ -n "$missing" ]; then
  say "ABORT: missing regime symbols:$missing"
  say "classify_regime() needs all three or every bar falls back to RANGING."
  exit 1
fi
say "regime symbols present (BTC/ETH/SOL)"

# ── 3. replay ────────────────────────────────────────────────────────────────
say "starting replay — 30d x 100 symbols, PRE-FIX code (the control arm)"
cd "$CTRL" || { say "ABORT: control tree missing"; exit 1; }
"$PY" -u tools/replay/driver.py \
    --days 30 \
    --data-dir "$DATA" \
    --equity 100 \
    --resume \
    --progress-every 360 \
    --checkpoint-every 1440 \
    --out "$CTRL/replay_30d_control.json" >> "$LOG" 2>&1
rc=$?
say "replay exited rc=$rc"

# ── 4. report ────────────────────────────────────────────────────────────────
if [ -f "$CTRL/replay_30d_control.json" ]; then
  say "generating diagnostic report..."
  cd "$ROOT" || exit 1
  "$PY" -u tools/replay/analyze.py "$CTRL/replay_30d_control.json" \
        --capital 100 >> "$LOG" 2>&1
  say "report written"
else
  say "no trades file produced — check the log above"
fi

say "=== finished ==="
