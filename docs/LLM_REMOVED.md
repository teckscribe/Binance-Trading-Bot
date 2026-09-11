# LLM subsystems — removed 2026-08-11, and how to restore them

Two modules were deleted: `modules/llm_regime.py` and `modules/llm_advisor.py`.

## Why

The goal was to make the **live engine match `backtest_optimizer.py` exactly**,
so that any live-vs-backtest divergence is attributable to the engine itself
rather than to an optional subsystem the backtest cannot model.

Both were already disabled in `.env` (`LLM_REGIME_ENABLED=false`,
`LLM_ADVISOR_ENABLED=false`), so removing them changed no trading behaviour.

Measured contribution before removal, 90-day backtest, CSM-only config:

| what | effect |
|---|---|
| LLM regime override | **exactly zero** — CSM is permitted in BULL/BEAR/RANGING, so the label cannot change which trades fire |
| regime-conditional sizing (the only plausible use) | sign flips by window: +23.7pp @30d, **−148.1pp @60d**, +633.9pp @90d — indistinguishable from noise |

The one non-zero effect was a hazard: `VALID_REGIMES` includes OVERHEATED and
OVERSOLD, and `REGIME_STRATEGY_PERMISSIONS` maps both to `{}`. An LLM returning
either at ≥70% confidence would have **halted trading entirely**. The rule
engine reaches those states only via `funding > 0.0005`, which never occurred
in 90 days (peak 0.0001, 5× below threshold).

## What was removed

| location | what |
|---|---|
| `modules/llm_regime.py` | whole file — DeepSeek regime classifier, 5-min cache, 70% confidence gate |
| `modules/llm_advisor.py` | whole file — background thread producing per-coin bias/risk_state |
| `live_scanner.py` import block (~line 157) | `from modules.llm_regime import classify as llm_classify, LLM_REGIME_ENABLED` |
| `live_scanner.py` manage loop (~line 511) | advisor post-trade exit: forced exits on `CHOPPY` risk state and on bias flip. Exit reasons `LLM_CHOPPY_EXIT`, `LLM_BIAS_FLIP` |
| `live_scanner.py` startup (~line 1001) | `start_advisor_thread(symbols[:TOP_N_SYMBOLS])` |
| `live_scanner.py` regime refresh (~line 1142) | the `if LLM_REGIME_ENABLED:` override block |

`.env` keys were left in place: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`,
`DEEPSEEK_BASE_URL`, `LLM_REGIME_ENABLED`, `LLM_ADVISOR_ENABLED`. They are now
inert. **Rotate `DEEPSEEK_API_KEY`** — it was exposed in a chat transcript on
2026-08-11.

## Restoring

Backups (this tree is not under version control — these are the only copies):

```
<scratchpad>/removed_llm/llm_regime.py
<scratchpad>/removed_llm/llm_advisor.py
<scratchpad>/removed_llm/live_scanner.py.before_llm_removal
```

Steps:

1. Copy both modules back into `modules/`.
2. Restore the import block in `live_scanner.py` (replaces the removal comment
   near the top of the imports).
3. Restore the three call sites listed above. The easiest reference is
   `live_scanner.py.before_llm_removal` — diff it against the current file;
   the LLM blocks are the only differences.
4. Set `LLM_REGIME_ENABLED=true` and/or `LLM_ADVISOR_ENABLED=true` in `.env`.

### Do this before re-enabling the regime override

Clamp OVERHEATED/OVERSOLD to RANGING in `llm_regime.py` before returning the
override, so a hallucination degrades to "trade normally" rather than "stop
trading". Without the clamp, one confident wrong answer silently halts the bot:

```python
# in classify(), after `regime = result.get("regime", "").upper()...`
if regime in ("OVERHEATED", "OVERSOLD"):
    log.warning(f"[LLM] clamping {regime} -> RANGING "
                f"(permission matrix has no strategies for it)")
    regime = "RANGING"
```

Alternatively, populate `REGIME_STRATEGY_PERMISSIONS["OVERHEATED"]` and
`["OVERSOLD"]` with real entries so those regimes stop being trading halts.

### If restoring the advisor

The advisor forced market exits the backtest has no equivalent for, so
re-enabling it reintroduces live/backtest divergence. If the purpose is
diagnosing that divergence, leave it off.

## What was NOT removed

`modules/ml_engine.py` is untouched and still imported by `live_scanner.py`. At
`ML_PHASE=1` it only logs features (27 per signal to `data/ml/signals.jsonl`)
and has zero effect on trading. Phase 2 (adaptive sizing) unlocks at 50
completed trades; there were 5 at time of writing.

`data/llm_regime_cache.jsonl`, if present, holds historical LLM classifications
and was left in place.
