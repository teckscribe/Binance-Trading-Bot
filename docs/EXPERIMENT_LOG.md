# CSB — Experiment Log

**Last updated:** 2026-09-12
**Purpose:** every experiment run, with results, so nothing gets re-tested from scratch.
Read this before proposing changes. Several ideas below look obviously good and were
measured to be actively harmful.

---

### 0. PRODUCTION RELEASE & HARDENING (2026-09-11)

### 0.1 Production Release Deployment (2026-09-11 18:00–21:30 IST)
- **Monotone High-Water Mark (HWM) & Dual-Stage Hybrid Ladder**:
  - Monotonically tracks peak favorable excursion since entry (`hwm = max(prev_hwm, peak_gain)`).
  - **Dual-Stage Hybrid Evaluation**:
    - Stage 1 (+1.0% trigger → +0.15% lock): Evaluated on **15m bar close** (`gain`). Prevents 1-second noise whipsaws from locking breakeven prematurely right after entry.
    - Stage 2 (+2.5% trigger → +1.50% lock) & Stage 3 (+4.0% trigger → +2.50% lock): Evaluated on **Peak HWM** (`hwm`). Captures rapid intra-bar wick spikes instantly.
  - Threshold: `LADDER_HWM_TRIGGER_THRESHOLD = 0.020`.
- **Breakout Volume Expansion Filter**:
  - Requires breakout volume confirmation against 24h rolling average hourly volume.
  - Filters out low-liquidity fake-outs on illiquid altcoins. Dynamic signal strength scaled from 0.5 to 2.5 based on volume ratio.
  - Config: `CSM_VOL_RATIO_MIN = 1.0`.
- **90-Day Regime Gating Sweep**:
  - RANGING: **+666.0% ~ +719.7% net return** (66.3% win rate, max loss streak 11).
  - BULL_TREND: **−130.5% loss** (short wicks squeezed, late longs top-ticked).
  - BEAR_TREND: Dip-buying longs trapped in systematic trend bleed.
  - Rule: CSM remains strictly gated to RANGING markets where it possesses proven alpha.
- **Latency & Architecture Optimizations**:
  - `preload_exchange_specs()` warms `LOT_SIZE` `stepSize` and `PRICE_FILTER` `tickSize` on startup in 1 bulk API call (0ms order path).
  - `modules/auth_manager.py` equipped with `HTTPAdapter(pool_connections=30, pool_maxsize=30)` and `get_session()`.
- **Tokenized Asset & ETF Exclusion Filter**:
  - Added SKHYNIX, MU, SNDK, SAMSUNG, QQQ, SPY, EWY, KORU, SOXS, XAUT, PAXG, USDE, EUR, GBP to `EXCLUDED_BASES` in `modules/symbol_filter.py`.
- **Audit Field Tagging**:
  - Added `exit_source: "bot"` on strategy automated closes and `"manual"` on exchange-reconciled closes in `order_engine.py` for ledger auditing.

### 0.2 Production Cross-Check Audit & Hardening (2026-09-11 23:20–23:45 IST)
Following an independent 4-agent parallel code audit, 13 specific vulnerabilities and stability points were resolved:
- **0.2.1 Hourly Volume Filter Time-of-Hour Gating Fix (`modules/strategies/cross_sectional_momentum.py`)**:
  - *Problem*: Comparing in-progress 1h candle volume (`iloc[-1]`) against 24 full-hour completed candles caused an artificial volume starvation gate blocking entries during the first 30–45 minutes of every hour.
  - *Fix*: Switched to the last completed 1h candle (`iloc[-2]`) compared against the preceding 24 completed candles (`iloc[-26:-2]`).
- **0.2.2 Binance Rate-Limit Headroom Calibration (`modules/data_hub.py`)**:
  - *Problem*: `MIN_REQ_GAP = 0.025s` (40 req/s) sat exactly on Binance's 2400 req/min limit, leaving zero buffer for concurrent order routing or account syncs.
  - *Fix*: Increased to `MIN_REQ_GAP = 0.030s` (33 req/s), establishing a ~17% safety margin to guarantee immunity from `429` rate-limit bans.
- **0.2.3 BTC Reference Cache TTL Alignment (`modules/data_hub.py`)**:
  - *Problem*: `_BTC_REF_TTL = 60s` exactly matched the 60s scan interval, causing cache expirations every loop and burning 5 API calls per scan.
  - *Fix*: Increased TTL to 300s (5 minutes). Saves 5 signed API calls per cycle while preserving regime accuracy.
- **0.2.4 HWM Entry-Candle Overlap Guard (`modules/strategies/cross_sectional_momentum.py`)**:
  - *Problem*: When managing a newly opened trade, `df.iloc[-1]` could contain pre-fill price extremes from the entry minute, prematurely ratcheting stops into phantom stop-outs.
  - *Fix*: Added timestamp overlap detection; if bar timestamp $\le$ `entry_time`, `_px_hi` and `_px_lo` are clamped to `current_price`.
- **0.2.5 Kronos Model Inference Mode (`kronos/scorer.py`)**:
  - *Problem*: PyTorch model and tokenizer defaulted to `training=True`, causing stochastic dropout during inference.
  - *Fix*: Added explicit `self._tok.eval()` and `self._mdl.eval()` calls upon initialization.
- **0.2.6 Kronos Safetensors Dependency (`kronos/requirements.txt`)**:
  - Added `safetensors>=0.4.0` required for HuggingFace Hub model loading.
- **0.2.7 Shadow Worker Truncation & History Robustness (`kronos/shadow_worker.py`)**:
  - Added file-size check (`if os.path.getsize(REQ) < off: off = 0`) to prevent queue deadlocks on log rotation.
  - Added `endTime` parameter to `_fetch_15m()` and normalized all candidate timestamps to naive UTC for historical scoring.
- **0.2.8 Candidate Shadow Logging Pipeline (`live_scanner.py`)**:
  - Wired `kronos.shadow_client.log_candidate()` into `live_scanner.py` candidate generation loop so CSM setups are recorded into `shadow_requests.jsonl` in real time.
- **0.2.9 Mid-Trade Restart Recovery & Persistence (`live_scanner.py`)**:
  - Validated and configured `CLOSE_ON_SHUTDOWN=false` in `.env`.
  - Open trades persist every cycle to `data/open_positions.json` (including trailed SL and peak HWM) and automatically rehydrate on startup via `_load_positions()` and `reconcile_with_exchange()`.
- **0.2.10 Cross-Platform Line Endings & Dependencies (`.gitattributes`, `requirements.txt`, `.gitignore`)**:
  - Added `.gitattributes` (`*.sh text eol=lf`) to protect Linux shell scripts from Windows CRLF corruption.
  - Replaced blanket `data/` ignore with targeted `data/*.csv` rules.
  - Removed unused `cryptography>=42.0.0` dependency.
- **0.2.11 Regime Permission Changes (`modules/regime_engine.py`)** — 2026-09-12, intentional:
  - `BEAR_TREND: {"CSM": False → True}` — CSM now permitted in BEAR_TREND as a live monitoring experiment. Context: §17.29 measured CSM in BEAR_TREND at 778 trades, −0.144%/trade, −111.9% cumulative (Config A, leak-free 90d). Decision is deliberate; live P&L will confirm or refute.
  - `BULL_TREND: {"ELLIOT_V8": True → False}` and `BEAR_TREND: {"ELLIOT_V8": True → False}` — ELLIOT_V8 benched in all regimes. Backtest showed PF ~1.01 (after-tax negative under 115BBH, §17.x). Reinstated when live sample reaches ≥100 trades.
  - Commit: `ad18872`.

---

### 0.3 Runtime Settings Migration — .env → data/settings.json (2026-09-12 11:20–12:20 IST)

**Problem.** Every tunable was read with `os.getenv()` at module import, so a change made from Telegram, Discord or the dashboard needed a scanner restart — and a restart with `CLOSE_ON_SHUTDOWN=false` still costs a reconcile pass and a gap in management. Three separate `.env` writers had grown up (`telegram_bot`, `discord_bot`, `web_server`); two used plain `open(path, "w")` and could truncate `.env` on a crash mid-write. Only `GLOBAL_LEVERAGE` was hot (risk_engine re-read `.env` on mtime).

**Design.** `modules/settings_manager.py`:
- `SPEC` — 46 keys, each with type (`int|float|bool|choice|csv_caps`), bounds, default, group, label, help, `hot` flag. The dashboard renders from it; every writer validates against it.
- `get(key)` — typed; re-reads `data/settings.json` only when `(mtime, size)` changes, and checks that at most once per second, so it is safe per-signal (CSM calls it ~10× per symbol per scan).
- `update({k: v}, source)` — validates the whole batch first (one bad key → nothing written), rewrites via temp file + `os.replace`, always writes the complete key set (repairs a partial/corrupt file), appends to `data/settings_history.jsonl`.
- Precedence: shell env value not from `.env` → `settings.json` → `.env` (only for a key absent from the file) → default. The first rule keeps `CSM_MOM_LO=3.5 python backtest_optimizer.py` sweeps working with no code, and such overrides are never persisted.
- `migrate_from_env()` — on first start creates the file seeded from `.env`; `.env` lines are left in place and ignored thereafter.
- `env_get/env_set` — the one sanctioned `.env` writer, atomic, used only for `LIVE_ENABLED`.

**Stays in `.env`:** `BINANCE_API_KEY/SECRET`, `TELEGRAM_BOT_TOKEN/CHAT_ID`, `DISCORD_BOT_TOKEN/GUILD_ID/CHANNEL_ID/WEBHOOK_URL`, `NGROK_AUTHTOKEN`, `DASHBOARD_TOTP_SECRET`, `LIVE_ENABLED`, plus infra knobs (`BINANCE_RECV_WINDOW_MS`, `*_CACHE_TTL_SEC`, `CSB_NO_FILE_CACHE`) and the `kronos/` subsystem's own vars.

**Consumers rewired (all read at call time now):**

| File | Change |
|---|---|
| `live_scanner.py` | `_refresh_settings()` at top of every cycle re-snapshots 15 module globals, logs each change, forces symbol rebuild on universe-shape keys, applies `LOG_LEVEL` live, resets paper ledger on `ACCOUNT_EQUITY_USDT` change (PAPER only; LIVE takes Binance balance). `_tg()` reads `TELEGRAM_ENABLED` per call. |
| `modules/risk_engine.py` | Constants → functions: `max_concurrent()`, `max_total_margin_pct()`, `max_leveraged_loss_pct()`, `min_sl_pct()`, `max_sl_pct()`, `max_trade_loss_pct()`, `daily_loss_cap()`, `weekly_loss_cap()`, `session_loss_floor()`, `max_per_strategy()`. `get_leverage()` reads `GLOBAL_LEVERAGE` (0 = per-strategy table). The old `.env`-mtime reader is gone. |
| `modules/strategies/cross_sectional_momentum.py` | All `CSM_*` read at the top of `scan()` / `_build_signal()` / `manage()`. |
| `modules/strategies/freqtrade_port_nasos.py` | `NASOS_SL_MODE/SL_ATR/SL_FLAT/TP_ATR`, `PORT_MAX_HOLD_MIN` read per call. |
| `modules/ml_engine.py` | `_phase()` / `_shadow()`. |
| `modules/watchlist.py` | `MIN_COIN_AGE_DAYS` read per exchange-info fetch. |
| `modules/auth_manager.py` | Preflight equity sanity check reads the setting. |
| `web_server.py` | Private `SETTINGS_SPEC` / `_read_env_values` / `_validate` / `_write_env_values` deleted (−300 lines); `GET/POST /api/settings` and `GET /api/settings/history` use the manager. `restart_required` only for cold keys (`NGROK_ENABLED`). |
| `telegram_bot.py`, `discord_bot.py` | `_read_env_value` / `_write_env_value` deleted; `_setting()` / `_set_setting()` / `_live_enabled()`. Messages: "applies on the scanner's next cycle — no restart needed". |
| `ngrok_runner.py` | `NGROK_ENABLED` from settings (read once; cold). |
| `backtest_optimizer.py`, `tools/llmvalue.py`, `tools/replay/analyze.py`, `tools/fetch_1m.py` | Snapshot the accessor functions once per run. |
| `static/app.js`, `static/index.html`, `static/style.css` | "restart" pill on cold keys; Recent Changes table. |

**Migration check against the production `.env` (2026-09-12 12:05 IST).** Simulated `migrate_from_env()` on the exact Ubuntu `.env` tunables: 36 keys seeded 1:1 (`MAX_CONCURRENT=3`, `MAX_PER_STRATEGY=CSM:3,NASOS_V4:2,ELLIOT_V8:1`, `FOCUSED_SIZE=150`, `MIN_STRENGTH=0.5`, all `CSM_*`, `ML_PHASE=2`, …). 10 keys absent from `.env` take the defaults the code already used: `MIN_SL_PCT=0.015`, `DAILY_LOSS_CAP=-0.10`, `WEEKLY_LOSS_CAP=-0.15`, `SESSION_LOSS_FLOOR=-0.10`, `NASOS_SL_MODE=flat`, `NASOS_SL_FLAT=0.08`, `NASOS_SL_ATR=6.0`, `NASOS_TP_ATR=3.0`, `PORT_MAX_HOLD_MIN=0`, `MANAGE_ON_BAR_CLOSE=false`. **Behaviour before and after deploy is identical.**

**Verification.**
- 42 unit checks (scratch dir): migration typing (`.50` → 0.5, out-of-range `.env` value → default), batch rejection writes nothing, csv_caps normalisation, `set_value` errors, cross-process hot reload seen within ~1s, hand-edited bad value → default with one warning, corrupt file → `.env`/defaults and repaired on next write, `env_set` preserves comments/non-ASCII/other lines, no temp files left.
- 51 integration checks: scanner import seeds file; `_refresh_settings()` returns exactly the changed keys and updates all globals; `get_leverage`, `max_per_strategy`, `is_strategy_cap_hit` live; `_tg` honours `TELEGRAM_ENABLED`; CSM `manage()` honours a live `CSM_MAX_HOLD_MIN`; `compute_position_size` rejects/accepts on a live `MAX_SL_PCT`; dashboard `GET` exposes exactly the spec (no secrets, no `LIVE_ENABLED`), `POST` refuses bad OTP, rejects out-of-range and non-spec keys, applies a TOTP-authenticated save and flags only `NGROK_ENABLED` as restart; bot helpers read settings.
- Static wiring audit: every SPEC key has ≥1 `settings_manager` consumer and zero `os.getenv` reads; every `.env`-only key is still read from env and is not in SPEC; no `cfg.get()` of an unknown key; no legacy env writers; all 15 scanner snapshots covered by the per-cycle refresh; no local shadowing of refreshed globals in `main()`.
- Dashboard rendered and checked in browser (Settings tab, restart pill, history table).

**Deploy.** `git pull` + `systemctl restart csb csb-bot csb-discord csb-web csb-ngrok`. Journal will show `Created data/settings.json — 36 value(s) seeded from .env`. Never rsync `data/settings.json` from a dev box over the production one. `.gitignore` excludes `data/settings.json` and `data/settings_history.jsonl`.

**Lesson (process, not code).** Do not run integration tests against the real project `data/` — the first run left test rows (`CSM_MOM_LO 3.0 → 3.3`, `MAX_CONCURRENT 5 → 4`) visible in the dashboard history and looked like a real config change. Tests now point `SETTINGS_FILE` / `HISTORY_FILE` at a scratch directory.

Commits: `928b8e2` (migration), `1ec0e58` (offline tools + stale wording), `6e0ab0e` (audit trail).

---

### 0.4 Kronos Shadow Gate — FIRST VERDICT at N=81 (2026-09-14 07:00 IST)

**Context.** The Kronos shadow worker (§0.2.7–0.2.8) has been scoring every live CSM candidate since 2026-09-11 with `would_gate = pred_fav >= 0.020` — the threshold validated in the original backtest and **fixed before any of these trades happened**. `enrich_ablation.py` defers until ≥80 executed CSM trades can be matched to a Kronos verdict. That bar was crossed today (dashboard progress panel showed 81/80). Production `.env`-era config: Config A (§17.29), `MAX_CONCURRENT=3`, `MAX_PER_STRATEGY=CSM:3,NASOS_V4:2,ELLIOT_V8:1`.

**Run 1 — `kronos/enrich_ablation.py` (5-fold CV logistic AUC):**
```
labelled (signal x outcome): 93
  CSM: 83   matched to Kronos: 81 (98% of CSM)
  matched win rate 44.4%   pred_fav med +0.0162
== ABLATION (N=81) ==
  pred_fav/dir/adv ALONE : AUC 0.697
  27 base features       : AUC 0.624
  base + Kronos          : AUC 0.646   dAUC +0.022
```
- dAUC +0.022 clears the script's own bar, but at N=81 it is inside the noise — "promising", not "proven".
- The stronger line: **Kronos alone (3 features, AUC 0.697) out-ranks the ML engine's 27 base features (0.624).**
- Combined < Kronos-alone is the small-N signature (30 features on 81 rows; the logistic fit overfits the base features and dilutes the Kronos signal). Modelling artefact, not evidence against Kronos.

**Run 2 — what the gate would have DONE (split of the same 81 trades by `would_gate`; pnl = `pnl_equity_pct`):**

| | N | Win | PF | Sum | Mean/trade |
|---|---|---|---|---|---|
| All 81 CSM trades | 81 | 44.4% | 0.92 | −0.88% | −0.011% |
| **Kronos PASS** (pred_fav ≥ 0.020) | 31 | 54.8% | **1.75** | **+2.48%** | +0.080% |
| **Kronos BLOCKED** | 50 | 38.0% | **0.55** | −3.36% | −0.067% |

Over this window CSM was net negative; the gate would have removed the 50 trades carrying essentially all of the loss and kept 31 that were net positive. Because the 0.020 cutoff was pre-registered, this is an honest out-of-sample reading, not a fit.

**Why NOT flipped live yet.** N=31 on the pass side — PF 1.75 can move a lot on three or four different outcomes. Direction convincing; magnitude not yet.

**Robustness check (same 81 trades, 2026-09-14 08:10 IST):**

| thr | PASS | BLOCKED |
|---|---|---|
| 0.010 | N=56 PF 1.15 | N=25 **PF 0.28** |
| 0.015 | N=44 PF 1.02 | N=37 PF 0.75 |
| 0.020 | N=31 PF 1.75 | N=50 PF 0.55 |
| 0.025 | N=19 PF 3.96 | N=62 PF 0.57 |
| 0.030 | N=16 PF 3.56 | N=65 PF 0.62 |

PASS without best trade: N=30 PF 1.15; without best 2: PF 1.03.

Reading: PASS > BLOCKED at **every** threshold, and the lowest-scored trades are the worst of all (bottom 25 by `pred_fav`: PF 0.28) — the loser-detection is robust. The PASS-side profit is NOT: +2.48% is mostly one trade (PF 1.75 → 1.15 without it). Honest statement: **Kronos is a better loser-detector than winner-picker.** Expect the gate to stop CSM bleeding (PF 0.92 → ~1.0–1.15 with ~40% fewer trades), not to make it strongly profitable.

**DECISION (operator, 2026-09-14): gate goes LIVE at threshold 0.025.** Rationale for not waiting: a gate can only *block* trades, never open one, so being wrong costs missed trades, not capital; the un-gated strategy was net negative over this window, so waiting 40 more trades has a real cost; the switch is hot-reloadable and reverses in one cycle. Caveat recorded: 0.025 was chosen after seeing this sample (0.020 was the pre-registered value), so its PF 3.96 (N=19) is optimistic — the next reading is the honest test of 0.025. Operator intent: "after checking the result we can reduce" toward 0.020.

**Implementation (commit below):** `KRONOS_GATE` = `off | shadow | live` (default live), `KRONOS_PF_THR` (default 0.025), `KRONOS_GATE_WAIT_SEC` (default 8) — all hot in `data/settings.json`. `live_scanner._kronos_gate()` runs in `_execute_entries()` after the risk gates for CSM signals only: looks up the shadow worker's score for that exact candidate (`kronos/shadow_client.find_score` / `wait_for_score`, keyed on the `ts` the scan queued), skips the entry when `pred_fav < thr` in `live`, logs only in `shadow`. **Fail-open:** no score within `WAIT_SEC` (worker down, model reloading, `insufficient_context`) → the entry proceeds as before, and no further waiting that cycle. One Telegram/Discord notification per gated symbol per hour. The Kronos worker process is unchanged. Dashboard progress panel shows the gate mode and threshold.

**Measurement consequence:** with the gate live, blocked candidates never execute, so BLOCKED-PF can no longer be measured from live trades. The next reading compares gated-CSM PF (all executed CSM trades from 2026-09-14 on) against this pre-gate baseline (PF 0.92, mean −0.011%/trade, N=81). The shadow log still scores every candidate, so the threshold sweep can be repeated on the growing candidate set.

**Pre-registered decision rule (do not move the goalposts later).**
1. ~~Robustness check~~ — done above; passed on loser-detection, failed on winner-size.
2. **Next reading after ≥ 40 gated CSM trades** (executed with the gate live). Keep the gate if their PF ≥ 1.1 and mean/trade > the pre-gate baseline (−0.011%). If gated-CSM PF < 0.9, the gate is not helping live: set `KRONOS_GATE=shadow` and record why.
3. Threshold changes only at a reading, never mid-sample; lowering toward 0.020 is the stated direction if 0.025 looks too tight (too few trades).
4. Repeat the threshold sweep on the full candidate set at each reading — it does not need executed trades.

Split command (re-run for the second reading):
```bash
kronos/venv/bin/python -c "
import json
rows=[json.loads(l) for l in open('kronos/logs/ml_kronos_enriched.jsonl')]
rows=[r for r in rows if r['strategy']=='CSM' and r.get('kronos')]
def stats(s):
    w=sum(r['pnl'] for r in s if r['pnl']>0); l=-sum(r['pnl'] for r in s if r['pnl']<=0)
    return f\"N={len(s):3d}  win={100*sum(r['win'] for r in s)/len(s):5.1f}%  PF={w/l if l else float('inf'):.2f}  sum={100*sum(r['pnl'] for r in s):+.2f}%  mean={100*sum(r['pnl'] for r in s)/len(s):+.3f}%\" if s else 'N=0'
g=[r for r in rows if r['kronos']['would_gate']]; b=[r for r in rows if not r['kronos']['would_gate']]
print('ALL          ', stats(rows)); print('GATE PASS    ', stats(g)); print('GATE BLOCKED ', stats(b))
"
```

---

### 0.5 Phantom Closes, Rate Limit, NASOS Shadow, ELLIOT Removal (2026-09-14 12:00–14:00 IST)

**0.5.1 Phantom "manual" closes — root cause and fix.** Two profitable positions (VETUSDT +0.30 USDT, EDGEUSDT +0.04 on Binance) were reported by the bot as `MANUAL_CLOSE` with exit == entry and P&L = −fee at 12:09:56 IST, then closed on Binance at 12:11:05 in the same second. Chain: `positionRisk` returned `-1003` → `_get_binance_open_positions()` returned `{}` → reconciler treated both as manually closed, found no fill, booked at entry price, dropped them → next good read found them untracked → orphan-close market order. Synced history: **108 of 289 `MANUAL_CLOSE` exits carry the signature (exit == entry), 38 events of 2–3 positions in the same minute, 37 on 2026-09-13.** Their real P&L was never recorded; the ledger, paper equity, ML outcomes and the Kronos N=81 sample all contain them (booked ≈0, so they dilute rather than bias direction — treat the 09-13 regime numbers and §0.4 magnitudes as provisional).
Fix (`94953f5`): fetch returns `None` on failure and reconcile skips the cycle; a missing position is booked only when a closing fill exists in `allOrders` (or `userTrades` as second witness); no entry-price fallback. Earlier (`52b2a1a`): exchange-stop fills classified `SL_HIT` / `LIQUIDATED` / `TP_HIT` instead of manual; `close_position()` no longer drops a position on a position-gone code.

**0.5.2 Rate limit — cause of the failed reads.** Journal: 13 × `-1003` on 09-13 15:58–19:27, 1 at 09-14 12:09:55 (the VET/EDGE event), plus read timeouts. Weight accounting (BULL/BEAR, 3 positions): scan 150 × 1m `limit=1500` (w10) ≈ 1500 + 15m ≈ 300; fast cycle every 1s: positionRisk (w5) ≈ 300 + 3 × (1m w2 + mark w1) ≈ 540 → **~2650–2700/min vs 2400**. The 30ms gap counted requests, not weight. Fix (`174b23a`, `modules/rate_budget.py`): `SESSION.request` gated on `X-MBX-USED-WEIGHT-1M`, pre-charged per documented weight, soft ceiling 1900 (klines/exchangeInfo), hard 2300 (orders, positionRisk, mark, fills), waits to the clock-minute rollover, `Retry-After` honoured in full on 429/418/-1003. Operator chose `SCAN_INTERVAL_SECONDS=120` (fast stays 1s) → ~1740/min worst case, ~27% headroom; entries checked every 2 min (cost: up to 60s later entry).

**0.5.3 Trade context capture** (`2532de6`, `d1f36f9`): see PROJECT_BRIEF item 35 for the field list. Motivation: the ledger carried no regime; the BEAR_TREND analysis earlier today had to be reconstructed from REGIME_CHANGE events (85/85 agreement with the ML stamp, so the reconstruction is trustworthy).

**0.5.4 CSM by regime (reconstructed, 543 exits 08-06 → 09-14, BEFORE phantom-close correction).** All: PF 1.14. RANGING N=386 PF 1.17. BEAR_TREND N=58 PF 0.74; since enabled 09-12: N=35, win 14.3%, PF 0.24, avg hold 21 min, 30/35 `MANUAL_CLOSE` — but a large share of those are §0.5.1 phantoms, so **no permission change made**. Re-read after ≥1 week of clean data. Kronos split by regime (same 81): at 0.025 RANGING PASS N=13 PF 3.85 / BLOCKED N=35 PF 0.68; BEAR PASS N=6 PF 5.71 / BLOCKED N=27 PF 0.26 — the gate finds losers inside each regime, not just a regime proxy.

**0.5.5 NASOS_V4 into the Kronos shadow queue** — `KRONOS_SHADOW_STRATEGIES = ("CSM","NASOS_V4")`, `KRONOS_GATE_STRATEGIES = ("CSM",)`. Queue rows carry `strategy`; `enrich_ablation.py` matches per strategy and ablates each with ≥80; progress panel shows NASOS matched. Zero cost (local model, public klines, ~5–10 weight/min). NASOS trades ~4× less often than CSM → expect 3–4 weeks to a verdict. Decision rule: same PASS/BLOCKED split at 0.020/0.025; gate only if BLOCKED PF ≤ 0.9 and PASS > BLOCKED at every threshold.

**0.5.6 Ablation instability noted.** On the freshly synced data (N=83 vs 81) the logistic ablation reads base AUC 0.697 / +Kronos 0.682 (dAUC −0.015) vs the box's 0.624 / 0.646 (+0.022) two hours earlier. Two extra rows flipped the sign: at this N the ablation is noise. The PASS/BLOCKED split (§0.4) is the decision-grade reading; ignore dAUC until N ≥ 150.

**0.5.7 ELLIOT_V8 removed.** Benched since 09-12 (PF ~1.01, after-tax negative), permitted nowhere, `MAX_PER_STRATEGY … ELLIOT_V8:0` on the box — it produced no trades, so keeping it only added dead branches and a strategy the Kronos tooling could never evaluate. Deleted `freqtrade_port_elliot.py`; removed from factory, regime matrix, leverage table, settings default, bots, notifiers, dashboard, backtesters, tools, replay sync list. Historical logs/docs untouched. On-box `settings.json` still lists `ELLIOT_V8:0` in caps and `strategy_overrides.json` has an entry — both ignored; the bots' cap validator will reject `ELLIOT_V8` on the next manual edit, which is the desired prompt to drop it.

**0.5.8 Whale positioning per strategy** (`6182797`, 2026-09-14 16:00 IST). `whale_analyze.py` `_analyze(sid)` and `progress.py` report CSM and NASOS_V4 separately against WHALE_MIN_N=120; dashboard card shows both. NASOS whale-matched count starts at 0 from this deploy; same ~4× slower accrual as its Kronos queue.

**0.5.9 Dead-code / wiring cross-check** (`9be4a50`, 2026-09-14 17:00 IST). Method: byte-compile all; pyflakes (undefined names → 0; unused/shadowed imports → 20 removed, 3 of them introduced by this week's changes); grep every `def`/`class` for references across `.py/.js/.html` → 15 unreferenced, 13 removed, `get_session()`/`bar_time()` kept as documented helpers; `freqtrade_port_sma.py` deleted. Deliberately untouched: unused locals in offline backtest scripts, `SMA_OFFSET` leverage-table/label entries (historical positions), `kronos/src/` (vendored upstream). Verification: settings wiring audit, every service/tool imports, factory `['CSM','NASOS_V4']`, 7 scratch suites green, all `/api/*` GET 200, no unreferenced JS. No behaviour change.

**0.5.10 Kronos win% split (81 matched CSM trades, `ml_kronos_enriched.jsonl`, 2026-09-14 17:30 IST).**

| Bucket | N | Win% | PF | Σ PnL |
|---|---|---|---|---|
| ALL (no gate) | 81 | 44.4 | 0.92 | −0.9% |
| PASS ≥ 0.025 (live) | 19 | **63.2** | 3.96 | +3.3% |
| BLOCK < 0.025 | 62 | 38.7 | 0.57 | −4.2% |
| PASS ≥ 0.020 | 31 | 54.8 | 1.75 | +2.5% |
| BLOCK < 0.020 | 50 | 38.0 | 0.55 | −3.4% |

24-point win% spread at the live threshold. Same caveats as §0.4: N=19 PASS, profit concentrated in a few trades, sample contains §0.5.1 phantoms. Win% is the more stable statistic; first decision-grade reading is the live-gated win%/PF after ≥40 gated CSM trades (dashboard Kronos panel).

**0.5.11 External audit response (2026-09-14 19:00 IST).** A 14-item audit was checked line by line against the code.
Confirmed and fixed (`hold_minutes` commit): (1) **HIGH** `NASOSv4Port.manage()` → `self.hold_minutes()` undefined — dormant only because `PORT_MAX_HOLD_MIN=0`; the setting is hot, so a dashboard edit would have raised `AttributeError` on every NASOS manage cycle and left the position unmanaged by the strategy (exchange stop still resting). Added `BaseStrategy.hold_minutes(position, df)`; tested both frame layouts, ISO-string and `pd.Timestamp` entry_time, and the armed guard. Missed by the §0.5.9 pyflakes sweep because attribute access on `self` is not statically checked. (2) dead `'leverage': 1` in the NASOS signal — never read; `_execute_entries` uses `get_leverage("NASOS_V4")` = 3×; key removed, table unchanged (backtests ran at 3×). (3) duplicated 5m resample → `_to_5m()`. (4–6) stale comments in `regime_engine.py`, `live_scanner.py`, `risk_engine.py`. (7–8) `ELLIOT_V8` in dev `settings.json` caps and `strategy_overrides.json` removed. **On the box, run once:** edit `MAX_PER_STRATEGY` via the dashboard/bot to `CSM:2,NASOS_V4:2`, and delete the `ELLIOT_V8` block from `data/strategy_overrides.json` (any command touching it is already rejected — `set_disabled()` validates against the factory, so the audit's "`/enable ELLIOT_V8` will succeed" claim was wrong).
Rejected: `get_oi_trend()` "dead" — used by `trend_pullback.py`, kept for `backtest_optimizer.py`; "fast cycle 1500-depth → 1800 weight/min" — fast cycles fetch `limit=400` (w2) + mark, ≈540/min (§0.5.2), and the throttle caps it regardless; SPEC default `MAX_PER_STRATEGY` 3:3 → 2:2 — the default is the historical `.env` default, the box's 2:2 is a per-deployment choice in `settings.json`; NASOS in BEAR_TREND — measured (§17.31/17.32), a decision not a bug; `MAX_TOTAL_MARGIN_PCT=0.9`, `FOCUSED 150/200`, `FAST_INTERVAL=1` — operator choices, not findings; "empty" stale `logs/strategies/{ELLIOT_V8,LIQ,OIB,VRP,WKD}` — not empty, they hold August daily logs (git-ignored) and stay as history.

**0.5.12 Legacy strategy files deleted (2026-09-14 21:15 IST).** `trend_pullback.py`, `funding_fade_v2.py`, `grid_strategy.py`, `freqtrade_port_ichi.py` removed. They were the "kept for backtest_optimizer" set from August; the box pulls the whole repo, so they were shipping to production as dead weight. Consequential removals: `StrategyFactory.get_grid()` and the scanner's grid-dissolve-on-regime-change block (checked `data/open_positions.json` — no GRID position exists); `get_oi_trend()` / `_oi_cache` / `_OI_CACHE_TTL` (TP was the sole caller — the §0.5.11 rejection of that audit item is now moot); `stop_exit_reason` no longer falls back to TP's `be_active` key (`live_logger`/`ml_engine` still read it for old records). `backtest_optimizer` `STRATEGY_CLASSES`/`TIER`/`MOCK_REGIME` reduced to CSM + NASOS_V4; `backtest_freqtrade_ports` NASOS only; replay sync list trimmed. Verified: pyflakes clean on touched files, compile-all, all entry points import, factory `['CSM','NASOS_V4']`, suites green, verify_deploy READY. Behaviour change: none for CSM/NASOS.

**0.5.13 Final sweep of standalone scripts (2026-09-14 21:45 IST).** Every tracked `.py` was checked for references from code, services, shell scripts and the dashboard. All unreferenced files are entry points by design; each was then judged on whether it still does anything for a CSM + NASOS_V4 bot. Removed: `list_optimizer.py` (repo root — stale duplicate of `modules/list_optimizer.py`, the one the bots import; flagged since §17.17); `backtest_freqtrade_strategies.py` (backtested EI3v2 / ichiV1 / NASMAv3 from a `CSB_Top_5_Strategies/` directory that no longer exists — NASOS is covered by `backtest_freqtrade_ports.py`); `tools/replay/_run/*` untracked and git-ignored (replay-harness checkpoint/progress/paper-equity state that had been committed by accident). Kept, deliberately: `analyze_strategies.py`, `bot_cleanup.py`, `dashboard_2fa_setup.py`, `fetch_binance_data.py`, `run_specific_backtest.py`, `backtest_freqtrade_ports.py`, `backtest_optimizer.py`, every `tools/*.py` (all still run against CSM/NASOS data and are indexed in the tools table, §8), the ngrok trio (in `commands.txt`). `tools/llmvalue.py`'s docstring still describes the August "CSM in all regimes" config — the measurement it makes is still valid, the prose is dated.

**0.5.14 Kronos threshold de-duplicated (2026-09-14 22:30 IST).** `kronos-shadow.service` carried `Environment=KRONOS_PF_THR=0.020`, used by the worker only to stamp `would_gate` on each score row; the live gate reads 0.025 from `settings.json`. Two copies of a hot setting. Options weighed: (a) worker reads `settings.json` — one more reader, couples the standalone venv process to `modules/`; (b) hard-code in the worker — same drift, different file; (c) **remove the threshold from the worker entirely** — chosen. The worker writes raw scores; `progress.py` computes `pred_fav >= cfg.get("KRONOS_PF_THR")` per request, so old rows are re-read at whatever the threshold is now. `enrich_ablation` never used the flag for decisions (PASS/BLOCK splits are computed from `pred_fav`). Rows before this change still carry `would_gate` at 0.020 — informational, unread. Also measured (§ response to operator, same day): worker response median 4.0 s first candidate (5 s poll + ~1.2 s per score), 7.5 s median for the last of a typical 4-candidate burst; gate adds 4–7 s to a CSM entry, capped at 8 s per cycle; NASOS unaffected. `KRONOS_POLL_SEC` is unset (default 5 s).
**On the box:** `sudo cp kronos/kronos-shadow.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart kronos-shadow csb-web`.

**0.5.15 `KRONOS_POLL_SEC` 5 → 2 s (2026-09-14 23:00 IST).** Simulation over the 2,702 scan bursts in `shadow_scores.jsonl` (09-11 → 09-14 01:35 UTC), using the scanner's real behaviour — highest-strength candidate executes first and waits `KRONOS_GATE_WAIT_SEC=8` for its score:

| Poll | Top-ranked score latency (median / p90) | Cycles where top-ranked times out → fail-open |
|---|---|---|
| 5 s (was) | 5.0 s / 9.0 s | **14.4 %** |
| 2 s (now) | 3.5 s / 7.5 s | **8.5 %** |
| 1 s | 3.0 s / 7.0 s | 7.2 % |

Poll is `os.path.getsize()` on the queue file — no network. Worker kline load is per candidate (3.65/min ≈ 7 weight/min) and unchanged. Which candidates pass/fail is unchanged; ~6 % more cycles get a verdict instead of falling open. The residual 8.5 % is per-candidate scoring time (~1.2 s) on bursts of 6+ where the top pick sits deep in a queue written in scan order; the fix would be to queue in rank order — deferred until journal `fail-open` counts over a week show it matters. Deploy: copy unit, `daemon-reload`, restart `kronos-shadow`.

**0.5.16 Kronos model revision pinned (2026-09-15 00:15 IST).** Operator asked whether an HF token was needed (no — public MIT weights, cached locally, no auth at score time) and whether a token would auto-update the model (no — but the unpinned `from_pretrained()` already *would*: it resolves `main` on every worker start). Pinned in `scorer.py`: model `901c26c1332695a2a8f243eb2f37243a37bea320`, tokenizer `0e0117387f39004a9016484a186a908917e22426` — the HF `sha` of both repos at time of pinning, last modified 2025-09-09, i.e. before shadow logging began on 2026-09-11, and identical to the snapshot directories in the dev machine's HF cache. So the entire shadow dataset, the N=81 verdict and the 0.025 threshold are on exactly these weights. Verified `HF_HUB_OFFLINE=1` load from cache: 5.9 s, no network. `KRONOS_REVISION` / `KRONOS_TOKENIZER_REVISION` env overrides exist for evaluating a future release in shadow before adopting it (which would mean re-running §0.4 and re-deriving the threshold). Worker prints `weights: model@… tokenizer@…` at start.

**0.5.17 `KRONOS_GATE_WAIT_SEC` 8 → 20 (operator, 2026-09-15 ~00:30 IST, via settings.json; hot).** Same 2,702-burst simulation with the 2 s poll: fail-open (top-ranked CSM candidate unscored) 8.5 % @8 s → 2.4 % @12 → 0.7 % @15 → **0.0 % @20**. Cost: the scan loop is single-threaded, so on the 8.5 % of cycles where the score takes >8 s the fast cycle (trail/BE/TP software checks) is paused for up to 20 s instead of 8; exchange `STOP_MARKET` protects throughout; median stall unchanged at 3.6 s. Accepted — a verdict on every CSM entry outweighs a late trail nudge on a minority of cycles. Note for the second gate reading (§0.4 rule): from this point every gated CSM trade has a score, so "gated" = "scored" in the journal counts. Also: Antigravity commit `0224fd2` carried the §0.5.16 scorer/worker pin (identical to the tested patch) and the `strategy_overrides.json` ELLIOT removal; `5fa182f` added the docs.

---

## 1. CURRENT STATE

*Last updated: 2026-09-14. Sections below this point may use earlier parameter values
recorded at the time of their experiment — see this section for the live production state.*

### Strategy configuration (as of 2026-09-12)

```
Production set (3):  CSM, NASOS_V4, ELLIOT_V8   — scan/display order in strategy_factory.py

REGIME_STRATEGY_PERMISSIONS (modules/regime_engine.py):
  BULL_TREND:  NASOS_V4                      # trend dip-buy; CSM and ELLIOT_V8 off
  BEAR_TREND:  CSM, NASOS_V4                 # CSM range-continuation + NASOS dip-buy
  RANGING:     CSM                            # CSM ONLY — proven +666%→+719% (§0.1)
  OVERHEATED:  {} (nothing trades)
  OVERSOLD:    {} (nothing trades)
```

**Regime permission rationale (§17.20, §17.29, 2026-08-29 → 2026-09-11):**
- CSM: positive E[net] only in RANGING (§17.21/§17.26/§17.29). BULL_TREND thin and below
  after-tax PF threshold. BEAR_TREND negative in large sample.
- NASOS_V4: profitable in BULL_TREND (+0.60%) and BEAR_TREND (+0.41%); loses in RANGING
  (−0.21%). Gated out of RANGING (§17.20).
- ELLIOT_V8: marginal overall; currently disabled pending more live data.

**Deleted (source files removed, all wiring cleaned):**
- 2026-08-11: `oi_breakouts.py` (OIB), `weekend_anomaly.py` (WKD), `llm_regime.py`,
  `llm_advisor.py`. See `docs/LLM_REMOVED.md` for restoration instructions.
- 2026-08-19: `freqtrade_port_ei3.py` (EI3_V2), `volatility_risk_premium.py` (VRP),
  `liquidation_cascades.py` (LIQ). All showed negative expectancy on the corrected harness.
- 2026-08-20: `freqtrade_port_sma.py` (SMA_OFFSET). Negative at every configuration after
  harness corrections.

### `.env` (Ubuntu, as deployed — 2026-09-12)

```
LIVE_ENABLED=true                # ⚠ LIVE mode — real money on Binance
ACCOUNT_EQUITY_USDT=100.00
SCAN_INTERVAL_SECONDS=60
FAST_INTERVAL_SECONDS=1
TOP_N_SYMBOLS=200
FOCUSED_MODE=true
FOCUSED_SIZE=150                 # was 100; enlarged to widen the opportunity set
MAX_ENTRIES_PER_CYCLE=1          # 1 entry/scan avoids correlated fills (§17.16)
MAX_CONCURRENT=5
MAX_TOTAL_MARGIN_PCT=0.9
MAX_PER_STRATEGY=CSM:2,NASOS_V4:2,ELLIOT_V8:1
GLOBAL_LEVERAGE=3                # was 5; reduced to lower per-trade ROI volatility
MAX_LEVERAGED_LOSS_PCT=0.10      # see §5.3 — do not raise without reading it
MAX_SL_PCT=0.10                  # rejects signals with SL > 10% at entry (§17.1)
MAX_TRADE_LOSS_PCT=0             # 0 = disabled; the 5% guard was reverted (§17.2)
CLOSE_ON_SHUTDOWN=false          # persists open trades across restarts (§17.4)
MAX_RESUME_AGE_HOURS=12          # refuses positions unmanaged > 12h on restart
MIN_COIN_AGE_DAYS=90             # excludes new listings (§17.23)
MIN_STRENGTH=0.50                # NASOS/ELLIOT gate; CSM pins strength=1.0 (§16.9)
LOG_LEVEL=INFO
ML_PHASE=2                       # features logged + shadow scoring active
ML_SHADOW=true

# ── CSM config ──────────────────────────────────────────────────────────────
CSM_ALLOW_LONG=true
CSM_ALLOW_SHORT=true
CSM_MOM_LO=3.0                   # Config A band: counter-trend 3.0–4.0x ATR (§17.29)
CSM_MOM_HI=4.0
CSM_SL_ATR_LONG=2.0
CSM_TP_ATR_LONG=4.0
CSM_SL_ATR_SHORT=2.0
CSM_TP_ATR_SHORT=4.0
CSM_MIN_SL_PCT=0.02              # was 0.03; lowered to avoid over-widening in Sep release
CSM_LEGACY_BE=false
CSM_PROFIT_LADDER=on             # Dual-Stage Hybrid Ladder (§0.1, §16.8)
CSM_MAX_HOLD_MIN=1440            # 24h; was 480/8h; extended in Sep release
CSM_VOL_RATIO_MIN=1.0            # breakout volume confirmation (§0.1, §0.2.1)
```

### CSM Dual-Stage Hybrid Ladder (as of 2026-09-11)

Replaces the legacy single-rung breakeven. Monotone HWM tracks peak favourable excursion.

| Stage | Trigger | Lock | Evaluated on |
|---|---|---|---|
| 1 | +1.0% gain | +0.15% (stop to entry+0.15%) | **15m bar close** — avoids 1s noise premature exits |
| 2 | +2.5% gain | +1.50% | **Peak HWM** — captures intra-bar wick spikes instantly |
| 3 | +4.0% gain | +2.50% | **Peak HWM** |

`LADDER_HWM_TRIGGER_THRESHOLD = 0.020`. Stage 1 tested on completed-candle `gain`;
Stages 2 & 3 tested on monotone `hwm`. Original profit ladder (§16.8) used 2%→1%/4%→2.5%,
revised to the three-stage version for the September release.

### Hardcoded (NOT in `.env`, as of 2026-09-12)

| Constant | Value | Where |
|---|---|---|
| `RISK_PCT_PER_TRADE` | 0.01 | `modules/risk_engine.py` |
| `MIN_SL_PCT` | 0.015 | `modules/risk_engine.py` (global floor; CSM uses its own 0.02) |
| `ROUND_TRIP_FEE` | 0.0008 | `order_engine.py`, `backtest_optimizer.py` |
| `MIN_REQ_GAP` | 0.030s | `modules/data_hub.py` (33 req/s, ~17% below Binance limit) |
| `_BTC_REF_TTL` | 300s | `modules/data_hub.py` (5-min cache, saves 5 API calls/loop) |
| `VWAP_PERIOD` | 20 bars | `modules/regime_engine.py` (rolling VWAP, window-invariant) |

### Environment

- **Ubuntu box** (`/home/psms/ubuntu/program_files/csb`), i3-6100 2c/4t, 8 GB, runs 24/7.
  venv is **`venv/`** (not `.venv/`). Service units: `csb`, `csb-bot`, `csb-discord`,
  `csb-web`, `kronos-shadow`, `whale-shadow`. **LIVE_ENABLED=true — real money.**
- **Windows dev box**, i3-1315U 6c/8t. venv is `.venv/`. **Not IP-whitelisted with
  Binance** — signed API calls fail with `-2015`. Public endpoints only.
- `data/strategy_overrides.json` is NOT synced between dev and Ubuntu. The Ubuntu file
  is authoritative. Always check it directly on the server before concluding "the bot
  isn't trading" — three strategies sat `disabled: true` for four days once (§17.12).
- Backtest data on the dev box stops at **2026-08-06**. Refresh from Ubuntu.

---

## 2. HEADLINE BACKTEST NUMBERS

⚠ **Read this first:** All numbers published before 2026-08-29 came from a harness with a
look-ahead leak (`run_backtest_1m` sliced `df_1h[df_1h.index <= now]`, admitting up to 59
minutes of future price). The leak flatters LONG-heavy configs by ~2x and penalises SHORT-heavy
ones. §17.25 covers the fix; §17.26 has the corrected CSM numbers. **Trust only numbers
marked "leak-free" or dated 2026-08-29+.**

---

### CSM — authoritative (leak-free harness, 2026-08-29+)

**Config A (deployed 2026-08-29, still live 2026-09-12):** CSM_MOM_LO=3.0/HI=4.0,
ladder=ON, 8h hold, 100 symbols, 90d, 0.08% round-trip + 0.03%/side slippage:

| N | WIN% | R:R | PF | E[net] | MEDIAN | SUM | HOLD | Worst streak |
|---|---|---|---|---|---|---|---|---|
| 1817 | 66.3% | 0.57 | 1.12 | +0.141% | +0.267% | ~+306pp* | — | 11 |

*SUM depends on regime gate. By regime (Config A, no gate):*

| Regime | N | WIN% | PF | E[net] | SUM |
|---|---|---|---|---|---|
| RANGING | 1648 | 66.6% | 1.17 | **+0.186%** | +306.6% |
| BULL_TREND | 301 | 67.8% | 1.14 | +0.165% | +49.6% |
| BEAR_TREND | 1243 | 60.7% | 0.70 | **−0.360%** | **−447.8%** |

**BEAR_TREND correctly blocked.** Without the gate, ungated Config A = −0.029% E[net]:
the regime filter is what makes it work.

**Config B (not deployed — reference):** CSM_MOM_LO=4.0/HI=5.0 (original band),
ladder=OFF, legacy-BE=OFF:

| N | WIN% | R:R | PF | E[net] | SUM |
|---|---|---|---|---|---|
| 644 | 46.1% | 1.68 | **1.44** | **+1.229%** | — |

Config B is the only configuration to clear the after-tax PF threshold of 1.429
(India 115BBH: 30% on gains, no loss set-off). Config A wins 66% and is after-tax NEGATIVE
because R:R 0.57 makes the win pile only 1.12× the loss pile. See §17.29 for full analysis.

---

### CSM Regime Gating Sweep (September 2026, 2026-09-11)

Definitive 90-day sweep confirming RANGING as the only viable CSM regime:

| Regime | Net Return | Win Rate | Notes |
|---|---|---|---|
| **RANGING** | **+666.0% → +719.7%** | 66.3% | Max losing streak 11. Proven alpha. |
| BULL_TREND | −130.5% | — | Short wicks squeezed, late longs top-ticked. |
| BEAR_TREND | negative | — | Dip-buying longs trapped in trend bleed. |

This is the backtest that locked CSM to RANGING-only in production.

---

### NASOS_V4 and ELLIOT_V8 (2026-08-29, corrected harness)

90d / 100 symbols, 0.03%/side slippage, gated (`BT_MIN_STRENGTH=0.50`):

| Strategy | N | E[net] | PF | Trend regimes | RANGING |
|---|---|---|---|---|---|
| NASOS_V4 | 875 | +0.350% gated | 1.16 | BULL +0.60%, BEAR +0.41% | −0.21% |
| ELLIOT_V8 | ~430 gated | marginal | ~1.01 | BULL +0.44%, BEAR −0.25% | −0.24% |

NASOS and CSM are **mirror images**: CSM earns in RANGING, loses in trends; NASOS does
the opposite. That is real diversification (§16.14).

---

### The 60× entry-cadence gap

Backtest uses `STEP=60` (one evaluation per 60-minute bar). Live scans every 60 **seconds**.
Live sees strictly more opportunities. This gap cannot be closed in the current harness — it
means per-trade expectancy from backtests understates what live will see in signal count,
while the price at entry may differ. Documented in §17.9; not fixed.

---

### Converting backtest % to account %

The per-trade figures are on **notional**, not equity. Mean notional on $100 ≈ 0.27× equity.
So `+0.186%/trade → ~+0.05% of equity → ~$0.05 per trade` at Config A parameters.

A "+666%" style figure is a **compounded portfolio return**, not a sum of per-trade percentages.

**Statistical floor: ≥128 trades before the edge outruns 2σ of noise** (per-trade σ ≈ $1.12).

**Sharpe is meaningless below ~30 days** (too few daily points). Ignore it on short windows.

---

## 3. THINGS THAT WERE TESTED AND FAILED — DO NOT RETRY

Every one improved per-trade quality and **lost money**, because with 3 slots against
11,363 slot-rejected signals, discarding candidates costs more than the weak ones lose.

### 3.1 Coin blacklisting — **FAILED** *(2026-08-12)*

Trained on days 90→30, traded days 30→0:

```
trade everything       n=3109   E +0.710%   +347.43%
blacklist the losers   n=2513   E +0.808%   +264.94%     ← 82pp WORSE
```

- **74% of banned coins were profitable in the very next period** (14 of 19, incl. BTC, DOGE).
- Only **1 of 99 coins** has a statistically real negative edge (SUI, t = −2.16).
- Worst 5 coins cost −66.9% against a total of +4442.3% — **1.5% of the pie**.
- 88 of 99 coins are net-positive. The edge is broad, not carried by outliers.

**Pattern worth keeping instead:** winners are small, volatile, recently-listed alts
(BEAT, VELVET, AKE, SYN, BLESS); losers are large caps (SUI, AVAX, TRX, LTC, DOT, ICP).
CSM needs a >3× ATR move in 24h, which a $50bn coin cannot produce. A **liquidity or
market-cap filter** has a mechanism behind it; a blacklist just memorises one window.

### 3.2 Time-of-day filtering — **FAILED** *(2026-08-12)* (and the intuition was backwards)

Hypothesis was IST 23:00–07:00 being most profitable. It is the **worse** half:

```
IN  window (23:00-07:00)   n=2550   win 61.3%   E +0.387%
OUT of window              n=6321   win 66.5%   E +0.731%
difference -0.3439%/trade   t = -3.51   SIGNIFICANT
```

Best hours are **IST 09:00–21:00** (European morning → US open). Worst single hour is
**02:00 IST** (−0.255%), the only negative hour on the clock.

Out-of-sample test:

```
all hours                   n=3109   +347.43%   maxDD 6.9%
learned best 8 hours        n=1117   +184.78%   maxDD 7.0%
IST 23:00-07:00 only         n=885   +103.20%   maxDD 9.5%
```

The learned-best-8 and the proposed window had **zero hours in common**.

**This is the one genuinely real effect found** (t = −3.51 with a plausible mechanism:
thin Asian-hours liquidity). It is not actionable at 3 slots. **It becomes actionable if
`MAX_CONCURRENT` is raised.**

### 3.3 LIQ / VRP in any regime — **FAILED** *(2026-08-12 → 2026-08-19)*

Four permission matrices, same 16,250-trade set:

| Config | 30-day | 90-day |
|---|---|---|
| **CSM only** | **+347.43%** dd 6.9% | **+6254.39%** dd 12.7% |
| CSM + LIQ in BEAR | +371.67% dd 6.9% | +4469.70% dd 17.3% |
| CSM + VRP in RANGING | +316.27% dd 11.8% | +4798.58% dd 13.2% |
| CSM + VRP:RANG + LIQ:BEAR | +329.12% dd 11.2% | +3454.74% dd 16.5% |

CSM-only wins on return **and** drawdown. A slot spent on LIQ (+0.023%) or VRP (−0.160%)
is a slot denied to CSM (+0.632%). Adding a breakeven strategy to a capacity-constrained
portfolio is **value-destroying, not diversifying**.

⚠️ The **15-day window said the opposite** and its matrix scored **worst of the four**.
Do not re-derive strategy permissions from a window under 30 days.

### 3.4 `BE_ATR_MULT` tuning — **NO CHANGE WARRANTED** *(2026-08-13)*

| MULT | 30d return | 30d maxDD | ret/dd | 90d return | BE% |
|---|---|---|---|---|---|
| **1.0** (current) | +347.43% | **6.9%** | **50.4** | +6254% | 22 |
| 1.5 | +332.43% | 9.4% | 35.4 | +7255% | 13 |
| 2.0 | +379.60% | 8.6% | 44.1 | +6630% | 6 |
| 2.5 | +239.81% | 8.0% | 30.0 | +2848% | 3 |
| 3.0 | +308.31% | 8.0% | 38.5 | +2555% | 3 |
| off | +288.32% | 8.0% | 36.0 | +2622% | 4 |

`1.0` has the lowest drawdown and best risk-adjusted return at 30d, and the highest
profit factor at both windows. The windows disagree on the alternative (30d → 2.0,
90d → 1.5), so the difference is noise. **Keep 1.0.**

Note the same trap: expectancy rises monotonically (+0.710% → +0.837%) while portfolio
return does not, because trade count falls 3109 → 2485 (unscratched trades hold their
slot longer).

### 3.5 LLM regime override — **ZERO VALUE, REMOVED** *(2026-08-11)*

- CSM is permitted in all three live regimes, so the regime label **cannot change which
  trades fire**. Contribution is exactly zero.
- Regime-conditional *sizing* (the only plausible use) flips sign by window:
  +23.7pp @30d, **−148.1pp @60d**, +633.9pp @90d — indistinguishable from noise.
- Only non-zero effect was a hazard: OVERHEATED/OVERSOLD map to `{}`, so a confident
  hallucination would **halt trading**.

See `docs/LLM_REMOVED.md` to restore.

---

## 4. BUGS FOUND AND FIXED

### 4.1 Regime `_coin_trend` was not window-invariant — **FIXED**

`vwap = (close*volume).cumsum() / volume.cumsum()` anchors to the **first bar of whatever
dataframe is passed**. Three callers passed three different lengths:

| Caller | Bars | BULL_TREND labelled |
|---|---|---|
| backtest (`build_regime_series`) | expanding, ≤2160 | **1.3%** |
| live (`data_hub.fetch_btc_reference`) | 50 | **18.2%** |
| `classify_regime` fallback | 35 | 21.2% |

Backtest↔live agreement was **75.7%**. Fixed with a rolling `VWAP_PERIOD=20` →
**95.5%**, and 0/170 disagreement across fetch sizes.

⚠️ This invalidated every pre-2026-08-11 per-regime number. CSM/BULL flipped from
−0.166% to +0.359% purely from relabelling.

### 4.2 Backtest sized positions 5.5× too large — **FIXED**

`simulate_portfolio` used `margin = equity × 0.30; notional = margin × 5` = **1.5× equity
per position** (4.5× across 3 slots). Live uses `notional = risk$ / SL%` = **0.27×**.
Now calls the real `compute_position_size`, which also brings leverage step-down,
`MIN_SL_PCT` rejection and minimum-notional handling.

### 4.3 Backtest had no intra-bar stops — **FIXED**

`manage()` saw only the bar close, so losers that recovered by the close were scored as
still open. Live SL_HIT was **43.5% vs backtest 24.2%**. Added `_intrabar_exit()` testing
each bar's high/low against the resting stop, filling **at** the stop. Tie-break: stop
before target (pessimistic — 15m OHLC can't order intra-bar events).

Sub-bug: it initially labelled every stop touch `SL_HIT`, producing a nonsensical
**62% SL_HIT alongside a 66% win rate**. Now uses `strategy.stop_exit_reason()`.

### 4.4 Trailed stops lived only in bot memory — **FIXED (Fix B)**

`_place_stop_order()` ran once at entry and was never updated, so Binance guarded the
**original** stop for the life of the trade. Breakeven/trailing existed only in the
process and were checked only when `manage()` ran.

Measured across **63 stop-based exits: every single one filled worse than its stop,
totalling −$1.82 ≈ 14% of gross P&L.** Worst was LITUSDT: stop resting *above* entry,
booked **−0.967%**.

Fix: `order_engine.update_stop_order()` pushes each move to Binance.
**Ordering is deliberate — place new, THEN cancel old**, so there is never a window
without a stop. Safe because `side=SELL + positionSide=LONG` can only reduce in hedge
mode; two live stops are harmless.

### 4.5 Paper tested the entry stop, not the current stop — **FIXED**

After 4.4, `_manage_on_bar_close` stage 1 still tested `initial_sl_price`, making paper
the only engine without intra-bar protection on a trailed stop. Now tests the current
`sl_price` and fills **at** the stop.

⚠️ **Only applies when `MANAGE_ON_BAR_CLOSE=true`.** With `false` (current), the tick
path books the observed price, so paper stop fills still land ~0.02–0.31% below the stop.
Paper is therefore **pessimistic relative to live**. Not yet fixed.

### 4.6 Dashboard reported strategies as ACTIVE that could never trade — **FIXED**

Status came from the manual-override flag alone. `/api/strategies` is now regime-aware
with four states: `DISABLED` (Telegram) / `OFF` (no regime permits it) / `IDLE`
(permitted, wrong regime) / `ACTIVE`. Poll dropped 60s → 15s.

### 4.7 Documented-but-nonexistent SMA slope filter — **NOT FIXED (docstring lies)**

`regime_engine.py`'s decision table claims *"BTC SMA20 slope flat → RANGING"*.
`SMA_SLOPE_MIN_PCT = 0.001` is defined and **never compared to anything**. `btc_slope` is
computed, passed to `_decide_regime`, and used only in a log string. Would have flipped
**7.4%** of bars to RANGING if implemented. The inline comment says ADX is trusted
instead — the module docstring is simply stale.

---

## 5. SETTINGS DECISIONS AND WHY

### 5.1 Three-coin regime (BTC+ETH+SOL) — no measurable value

BEAR_TREND set (the only cell that gates anything):

```
BTC only        541 bars
+ ETH           492 bars   (ETH removes 49, adds 0)
+ ETH + SOL     490 bars   (SOL removes 2, adds 0)
```

Account outcome with the permission matrix applied, **all three identical to 0.00pp** at
3-day, 7-day and 60-day windows. SOL changed 2 bars in 490 and costs an API call per scan.

Where the decision actually gets made (90d, 2135 bars): **ADX<20 gate → RANGING 24.9%**,
**BTC NEUTRAL → RANGING 23.9%**. So **48.8% of the time BTC alone decides** and ETH/SOL
are computed then discarded. ETH vetoes 168 bars (7.9%). SOL flipped **5 bars (0.23%)**.

**Drop SOL** whenever convenient. ETH is unproven but harmless.

### 5.2 `MANAGE_ON_BAR_CLOSE=false`

```
tick management (false)   85 trades   +$14.39   (+$7.69 stripping fake TP fills)
bar-close (true)          49 trades    -$1.56
```

The 84% BE_HIT problem it was meant to solve was **already fixed** by the ATR-relative
breakeven trigger (08-06 → 08-07, before bar-close was enabled).

⚠️ Trade-off: `true` matches the backtest's manage cadence; `false` does not. Evidence is
not conclusive (49 vs 85 trades) and one bug inside the bar-close period (4.4) is now
fixed, so `true` deserves a re-test eventually. Default in code is `false`.

### 5.3 `MAX_LEVERAGED_LOSS_PCT=0.10`

| | maxDD 30d | return 90d |
|---|---|---|
| 0.10 | 6.9% | +6254% |
| 0.50 | 9.7% | +7144% |

Chosen `0.10` because it **matches every backtest number in this document**, and for a
non-obvious second reason:

The breakeven trigger is `min(max(1×ATR, 0.8%), 5%)`. That **5% ceiling** only binds when
`SL = 2×ATR > 10%`. At `0.10`, trades with SL > 10% are rejected outright, so the ceiling
never binds and `BE/SL` stays a clean 0.50 on every trade. At `0.50`, trades with 10–20%
stops become eligible, their breakeven clamps at 5%, and it arms at **25–35%** of the stop
distance — *earlier*, producing more scratched trades.

**So 0.10 also suppresses BE_HIT.** Raising it has two costs, not one.

---

## 6. KNOWN GAPS — backtest vs paper vs live

Goal is not equality (impossible) but a **known, stable ratio**.

| Gap | Size | Status |
|---|---|---|
| **Trade rate**: backtest 18.3/day vs live ~11/day | largest single gap | not fixed — no symbol cooldowns or scan spacing in backtest |
| **Slippage** | −$1.82 / 63 exits ≈ 14% of gross P&L | not modelled anywhere |
| **ADX warm-up**: live 50 bars reads ~5 pts high | 12% of gate decisions disagree | not fixed — bump `fetch_btc_reference` 50 → 200 |
| **Funding cost** | −0.004%/trade (median hold 2.2h, 36% cross a stamp) | not modelled; negligible |
| **Survivorship** | unquantified | structural — cannot fix |
| **In-sample** | unquantified | structural — config was chosen on this data |

**Slippage sensitivity** (90d, CSM-only):

| slippage/side | 90d return | final from $100 |
|---|---|---|
| 0.00% (today) | +6254% | $6,354 |
| 0.02% | +5055% | $5,155 |
| 0.05% | +3609% | $3,709 |
| 0.10% | +1862% | $1,962 |

A 0.05%/side assumption cuts the headline **42%**. Expectancy falls 16% but final balance
falls 70% at 0.10% — small per-trade costs compound hard across ~1,650 trades.

---

## 7. LIVE / PAPER RESULTS TO DATE

⚠ **Mode change 2026-09-11:** `LIVE_ENABLED` switched from `false` to `true`.
All results below the divider are LIVE (real money on Binance). Do not compare
paper and live P&L figures directly — they use different fill paths.

---

All PAPER (`LIVE_ENABLED=false`) until 2026-09-11.

### Pre-§11 sessions (LONG-only CSM, old parameters): 134 trades, +$12.83

```
08-06  n=9    +1.463     08-09  n=12   +3.783
08-06  n=31   +2.182     08-10  n=23   -2.772
08-07  n=4    +0.645     08-11  n=5    -0.183
08-07  n=3    -3.314     08-11  n=9    -2.388
08-07  n=18   -2.003     08-11  n=5    +0.285   ← first post-deploy session
08-08  n=20  +15.414
```

### Post-§11 sessions (LONG+SHORT CSM): 18 trades, −$6.82

```
08-15  n=8    -4.68    ← session_20260815_054739, LONG-only (pre-SHORT deploy)
08-15  n=10   -2.14    ← session_20260815_120552, first session with SHORT trades
```

Loss causes analysed:
- JTOUSDT: 1.7% SL at 5x leverage → SL hit in minutes (led to §11.4 MIN_SL_PCT)
- VELVETUSDT: SL hit in 4 minutes (led to §11.6 BLITZ_STOP diagnostic)
- BICOUSDT: SHORT SL hit, wider SL needed (confirmed by §11.3)

### 14-day aggregate (trade analyzer, 217 trades):

```
CSM: 187t WR=50% PnL=+168.4% edge=+6pp
VRP:  23t WR=48% PnL=-2.6%   edge=-1pp
LIQ:   2t WR=100% PnL=+8.7%
OIB:   5t WR=60%  PnL=+1.9%
```

⚠️ The 08-08 **+$15.41 includes 3 TP fills inside a single 15m bar worth +$6.70**, two at
0.20 min capturing +16% — mark-price artifacts, not achievable fills.

**Do not over-read single sessions.** A 5-trade session read as "80% BE_HIT" and drove an
entire investigation; the real rate is 37%. Per-trade σ is $1.12, so a 5-trade session has
σ ≈ $2.5 — noise dwarfs the ~$1.00 expected edge.

---

### LIVE results (2026-09-11+, Config A + Dual-Stage Hybrid Ladder)

*Accumulating. Target: ≥100 live trades before any configuration changes.*

First live session (2026-08-28, paper — Config A first-day audit):
- 5 open, 2 closed, session +$1.00. ROI arithmetic verified trade-by-trade (§17.18).
- HYPEUSDT: SL_HIT −$1.03 (−9.11% ROI = 3.00% stop × 3x leverage — display artifact, not error).
- ENAUSDT: TP_HIT +$2.03 (+19.66% ROI). 1% equity risk confirmed on every position.

Bot switched to **LIVE_ENABLED=true** on 2026-09-11 with the production release.
Accumulating real-money trades. Check `logs/live/` and the web dashboard for current results.

---

## 8. TOOLING (`tools/`)

Run from the project root with the venv python.

| Script | Usage | What it does |
|---|---|---|
| `btwin.py` | `btwin.py <data_days> <window_days> [strats]` | Single-window report: per-trade + portfolio + by-strategy + by-regime + strategy×regime. **Caches trades to `data/bt_cache_<days>d_<strats>.json`** so subsequent windows take ~1s |
| `coinwise.py` | `coinwise.py <data_days> <window> [strat]` | Coin-wise, worst-first |
| `coincheck.py` | `coincheck.py <data_days> <window>` | BTC vs BTC+ETH vs BTC+ETH+SOL, account outcome |
| `hourly.py` | `hourly.py [strat]` | Hour-of-day (IST) expectancy + out-of-sample test |
| `blacklist.py` | `blacklist.py` | Coin significance + out-of-sample blacklist test |
| `costs.py` | `costs.py` | Slippage/funding sensitivity |
| `llmvalue.py` | `llmvalue.py` | Value of regime information (sizing scenarios) |
| `be_sweep.py` | `be_sweep.py` | `BE_ATR_MULT` sweep, 30d + 90d |
| `be_sweep30.py` | `be_sweep30.py` | Same, 30d only, ~6× faster (truncates to 38d) |
| `regime_check.py` / `regime_check2.py` | — | Regime window-invariance verification |
| `verify_deploy.py` | `verify_deploy.py` | **Pre-deploy check**: deleted-module imports, compile, import chain, engine state |

### Caches

`data/bt_cache_10d_CSM-LIQ-VRP.json` (1.1 MB) and `data/bt_cache_90d_CSM-LIQ-VRP.json`
(8.7 MB). **Delete these after changing any strategy logic** — they hold generated trades
and will silently serve stale results. `BE_ATR_MULT` sweeps bypass the cache for this
reason.

### Runtime

90-day generation is **O(bars²)** (`run_backtest` slices an expanding window), so 100
symbols × 3 strategies ≈ 60–80 min on the Windows box. 10-day is ~4 min. Known easy win:
replace `df_1h[df_1h.index <= now]` and `df_sec[df_sec.index <= now]` with
`searchsorted` + positional slice — those are full-frame scans per bar, pure overhead.

---

## 11. CHANGES 2026-08-15 / 2026-08-16

All changes below were motivated by analysing live production trades (sessions
`20260815_054739` and `20260815_120552`, 18 trades total). Each change is documented
with its before/after impact where measurable.

---

### 11.1 VRP restricted to BULL_TREND only — 2026-08-15 ~05:30 UTC

**Problem:** VRP was active in RANGING, BEAR_TREND, OVERSOLD. 90-day analysis:
47.3% WR, R:R 1.07, SHORT side bleeding. Per-trade E = −0.012% (§2).

**Change:** `REGIME_STRATEGY_PERMISSIONS` in `modules/regime_engine.py`:

```
Before:  VRP in BULL_TREND, BEAR_TREND, RANGING, OVERSOLD
After:   VRP in BULL_TREND only
```

CSM + LIQ remain in BEAR/RANGING/OVERSOLD. OVERHEATED stays empty.

**VRP parameters tuned (backtest engine only, NOT deployed to production):**

| Param | Before | After |
|---|---|---|
| `SL_ATR_MULT` | 1.5 | 2.5 |
| `TP_ATR_MULT` | 3.0 | 2.0 |
| `VOL_RATIO_THRESHOLD` | 2.0 | 2.5 |
| `BE_TRIGGER` | 0.008 | 0.010 |

**Impact:** VRP disabled via Telegram override (`data/strategy_overrides.json`).
Tuned VRP exists in backtest only — pending validation before production deploy.

**Files changed:**
- `modules/regime_engine.py` (production + backtest)
- `Tire 1-2 Back Test engine/modules/strategies/volatility_risk_premium.py` (backtest only)

---

### 11.2 CSM SHORT signals added — 2026-08-15 ~06:00 UTC

**Problem:** CSM was LONG-only. Missed SHORT momentum signals in dumps.

**Change:** Added SHORT signal for −4 to −5x ATR normalised momentum in
`modules/strategies/cross_sectional_momentum.py`. Direction-aware `manage()` with
inverted breakeven, trailing stop, TP, and SL logic.

```python
# scan() — new SHORT path
if -5.0 < normalized_mom <= -4.0:
    return self._build_signal(symbol, df_15m, "SHORT", normalized_mom)
```

**Impact (live, session 20260815_120552):**
10 trades (5 LONG, 5 SHORT), −$2.14, 40% WR. SHORT side contributed both wins and losses.

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`
- `modules/strategies/strategy_factory.py` (docstring update)
- `web_server.py` (description "long only" → "long + short")

---

### 11.3 CSM SHORT wider SL — 2026-08-15 ~08:00 UTC

**Problem:** SHORT trades using same SL (2x ATR) got bounce-stopped. Dead-cat bounces
in dumps are larger than pullbacks in pumps.

**Change:** Separate `SL_ATR_MULT_SHORT = 3.0` (vs `SL_ATR_MULT = 2.0` for LONG).

**Backtest impact (Tier 2, 10-day data):**

```
Before (SL 2x for SHORT): SHORT side = −$4.45
After  (SL 3x for SHORT): SHORT side = +$3.01
```

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`

---

### 11.4 MIN_SL_PCT = 3% SL floor — 2026-08-16 ~06:00 UTC

**Problem:** Low-ATR coins at high leverage produce tiny stops.
e.g. JTOUSDT: 1.7% SL at 5x leverage = 8.5% leveraged loss, hit SL in 4 minutes.

**Change:** Added `MIN_SL_PCT = 0.03` to `_build_signal()`:

```python
min_sl_dist = entry_price * MIN_SL_PCT
sl_dist = max(raw_sl_dist, min_sl_dist)
```

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`

---

### 11.5 Scaled TP/ATR when SL floor activates — 2026-08-16 ~07:00 UTC

**Problem:** When the 3% SL floor widened SL, TP stayed ATR-based. R:R dropped from
2:1 to 1.1:1 on floored trades. Trailing stop also used raw ATR (too tight).

**Change:** Compute scale factor when floor activates, apply to TP and stored ATR:

```python
scale = sl_dist / raw_sl_dist if raw_sl_dist > 0 else 1.0
effective_atr = atr_15 * scale          # scaled for trailing stop
tp_dist = effective_atr * TP_ATR_MULT   # TP scales with SL
```

**Impact:** R:R preserved at 2:1 (LONG) / 1.33:1 (SHORT, before §11.7) regardless
of whether the floor activates.

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`

---

### 11.6 Trade analyzer: structural diagnostics — 2026-08-16 ~10:00 UTC

**Problem:** Existing trade analyzer showed P&L/WR/exit-reason but missed
structural issues like tight SL, R:R distortion, leverage risk.

**Change:** Added 5 automated diagnostic checks to `modules/trade_analyzer.py`:

| Check | Detects | Threshold |
|---|---|---|
| `TIGHT_SL` | SL < 3% on losing trades | sl_pct < 3.0 |
| `RR_DISTORTION` | R:R below 1.5:1 | actual_rr < 1.5 |
| `BLITZ_STOP` | SL hit within 10 minutes | duration_min < 10 + SL_HIT |
| `LEVERAGE_RISK` | Leveraged SL loss > 10% | sl_pct * leverage > 10 |
| `SL_BUCKETS` | Profitability by SL width | <3% / 3-6% / >6% buckets |

Also added SL Bucket Analysis section to Telegram output.

**Validation (14-day live data, 217 trades):**

```
TIGHT_SL:       45 trades flagged
BLITZ_STOP:     10 trades flagged
LEVERAGE_RISK:  45 trades flagged
RR_DISTORTION:   3 trades flagged

SL Bucket Analysis:
  <3%:  123t avg=+0.72% WR=59%
  3-6%:  40t avg=+4.27% WR=55%
  >6%:   54t avg=-1.54% WR=28%
```

Auto-runs on every session end via `run_and_send(days=3)` in `live_scanner.py:1448`.
Also available via Telegram `/analyze` command.

**Files changed:**
- `modules/trade_analyzer.py`

---

### 11.7 BE_TRIGGER raised 0.8% → 1.5% — 2026-08-16 ~14:30 UTC

**Problem:** Backtest showed 9 BE_HIT trades averaging 15.4h hold time, total −0.52%.
BNBUSDT held 97.5 hours at breakeven, exited +0.04%. These "zombie" trades block
slots for days without producing meaningful P&L.

The 0.8% trigger is too sensitive — price moves +0.8%, BE activates, SL moves to
entry+0.1%, price retraces, exits at breakeven minus fees.

**Change:** `BE_TRIGGER = 0.015` in CSM (was hardcoded `0.008` in `manage()`).

**Impact:** Fewer premature BE activations. Trades need +1.5% move before SL moves
to breakeven — filters out noise.

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`

---

### 11.8 SHORT TP multiplier raised 4x → 6x ATR — 2026-08-16 ~14:30 UTC

**Problem:** SHORT R:R was only 1.33:1 (TP 4x / SL 3x). Needed 43% WR just to break
even. SHORT SL hits cost −3.0 to −3.2% but wins returned only +1.4 to +4.0%.

**Change:** Added `TP_ATR_MULT_SHORT = 6.0` (separate from LONG's `TP_ATR_MULT = 4.0`).
SHORT R:R now 6/3 = 2.0:1, matching LONG.

```python
tp_mult = TP_ATR_MULT if direction == "LONG" else TP_ATR_MULT_SHORT
tp_dist = effective_atr * tp_mult
```

**Backtest (7-day):** SHORT R:R in trade data confirmed at 2.0:1 for all
floored-SL SHORT trades (was 1.3:1 before).

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`

---

### 11.9 MAX_HOLD_MIN = 1440 (24h max hold) — 2026-08-16 ~14:30 UTC

**Problem:** Trades block all 3 slots for 24-97 hours. Only 43 full scans out of
10,080 cycles (0.4%). Bot can't scan for new opportunities.

**Change:** Added `MAX_HOLD_MIN = 1440` — force-close at market after 24 hours.
Duration computed from `entry_time` and current bar timestamp.

```python
if duration_min >= MAX_HOLD_MIN:
    return {"exit": True, "exit_price": current_price, "exit_reason": "MAX_HOLD"}
```

**Bug found and fixed (two iterations):**
1. First bug: `entry_time` is timezone-aware (`+00:00`) but `df.index[-1]` is
   timezone-naive. Subtraction raised `TypeError`. Fixed by normalising both to UTC.
2. Second bug (the real one): the 1m DataFrame returned by the replay store (and
   live data_hub) uses a RangeIndex with a `"timestamp"` column — NOT a DatetimeIndex.
   `df.index[-1]` returned an integer, `.to_pydatetime()` raised `AttributeError`.
   Both exceptions caught by `except Exception`, silently setting `duration_min=0`.
   Fixed by reading `df["timestamp"].iloc[-1]` when the column exists.

**Files changed:**
- `modules/strategies/cross_sectional_momentum.py`

---

### 11.10 Backtest scan starvation explained — 2026-08-16 ~13:00 UTC

**Not a bug.** Investigated why backtest from `CSB New/Tire 1-2 Back Test engine/`
produced only 43 full scans vs 426 from the old location.

**Root cause:** The wider SL (3x ATR SHORT + 3% floor) and wider TP make trades
hold much longer. With `MAX_CONCURRENT=3`, all slots stay occupied for hours/days.
The driver only scans when `slots_free = True`, so scans are blocked.

Both locations had identical code, identical data (113 symbols, same timestamps).
The difference was the old run used the pre-SHORT, pre-floor parameters where
trades resolved faster.

**After MAX_HOLD fix:** scans increased from 43 to 70 (63% improvement) and trades
from 27 to 44 (63% more). Slot starvation remains (70/10,080 = 0.7% scan rate)
but MAX_HOLD is a meaningful mitigation.

---

### Summary: CSM parameter state after all changes

```python
SL_ATR_MULT       = 2.0     # LONG stop-loss: 2x 15m ATR
SL_ATR_MULT_SHORT = 3.0     # SHORT stop-loss: 3x 15m ATR (wider for bounces)
TP_ATR_MULT       = 4.0     # LONG take-profit: 4x effective ATR → R:R 2.0:1
TP_ATR_MULT_SHORT = 6.0     # SHORT take-profit: 6x effective ATR → R:R 2.0:1
MIN_SL_PCT        = 0.03    # 3% minimum SL distance (prevents tiny stops)
BE_TRIGGER        = 0.015   # 1.5% move before breakeven activates
MAX_HOLD_MIN      = 720     # 12h max hold time (was 1440/24h, see §13.4)
```

### Backtest comparison (7-day, Tier 2 replay, $100 start)

| | Before all changes | After §11.3-11.5 | After §11.7-11.9 (MAX_HOLD fixed) |
|---|---|---|---|
| Trades | 105 | 27 | 44 |
| Net PnL | +$11.43 | −$8.62 | −$2.72 |
| Win rate | — | 22.2% | 36.4% |
| Full scans | 426 | 43 | 70 |
| SL_HIT | — | 44% | 34% |
| BE_HIT | — | 33% | 18% |
| TP_HIT | — | — | 14% |
| TRAIL_HIT | — | — | 14% |
| MAX_HOLD | — | 0% | 20% |

MAX_HOLD fix (§11.9) had two bugs: timezone mismatch and RangeIndex vs
DatetimeIndex. Once fixed, MAX_HOLD frees slots after 24h, enabling 70 scans
(vs 43) and 44 trades (vs 27). Net PnL improved from −$8.62 to −$2.72.

⚠️ **7-day window warning applies (§3, §10).** These numbers are directional only.
Do not derive production config from under 30 days. The 105-trade "before" run used
LONG-only CSM with tighter stops — a fundamentally different strategy, not a
controlled A/B test.

---

## 12. FILES TO SYNC TO UBUNTU (as of 2026-08-16) — SUPERSEDED by §14

After §11 changes + core audit fixes, the following production files need to be
copied to Ubuntu (see §14 for the current list):

```
modules/strategies/cross_sectional_momentum.py   — SHORT, SL floor, BE, TP, MAX_HOLD
modules/regime_engine.py                          — VRP BULL_TREND only, ADX fix, dead code cleanup
modules/trade_analyzer.py                         — structural diagnostics
modules/strategies/strategy_factory.py            — docstring update
modules/strategies/base_strategy.py               — (no change needed, verified correct)
modules/order_engine.py                           — stop retry re-sign, close retry, fee fix
modules/risk_engine.py                            — risk_usdt recalc, docstring fix
modules/data_hub.py                               — (no change needed)
live_scanner.py                                   — duration_min fix, stop-check logging, log format
web_server.py                                     — CSM description update
```

VRP in production still has OLD parameters (SL 1.5x, TP 3.0x). The tuned VRP
(§11.1) is in the backtest engine only — do NOT sync
`Tire 1-2 Back Test engine/modules/strategies/volatility_risk_premium.py` to
production until validated with a longer backtest window.

Also: remove VRP manual disable via Telegram or delete
`data/strategy_overrides.json` on Ubuntu to allow VRP in BULL_TREND.

---

## 13. CHANGES 2026-08-17 / 2026-08-19

### 13.0 30-day baseline backtest — 2026-08-17

First 30-day Tier 2 replay with all §11 changes (LONG+SHORT CSM, SL floor, BE 1.5%,
TP 6x SHORT, MAX_HOLD 24h). This was the baseline before any §13 changes.

| Metric | Value |
|---|---|
| Period | 2026-07-17 → 2026-08-16 (30 days) |
| Trades | 174 |
| Net PnL | -$0.30 (-0.30%) |
| Win rate | 40.2% |
| Equity | $99.70 |
| Scans | 3,971 |

Essentially breakeven. SHORT performance was the main drag: 72/92 SHORT trades
hit the 3% SL floor on low-ATR coins. This motivated the SHORT floor filter (§13.1).

---

### 13.1 SHORT floor filter — 2026-08-17

**Problem:** 78% of SHORT trades (72/92) in the 30-day backtest hit the 3% SL floor
on low-ATR coins. These coins have natural ATR-based SL distance below 3%, so the
floor inflates their SL while TP scales proportionally — but the coin's actual
volatility can't reach the TP. Net result: -$5.10 from floor-hitting SHORTs vs
+$0.29/trade from natural-ATR SHORTs (5-8% SL bucket).

**Change:** Skip SHORT signals when `raw_sl_dist < min_sl_dist` (the coin's natural
ATR is too low for SHORT). LONGs still use the floor because upside breakouts can
exceed the scaled TP.

```python
if direction == "SHORT" and raw_sl_dist < min_sl_dist:
    return None
```

**30-day backtest result (before vs after SHORT filter):**

| Metric | Before | After |
|---|---|---|
| Trades | 174 | 241 |
| Net PnL | -$0.30 (-0.30%) | +$9.39 (+9.39%) |
| Win rate | 40.2% | ~38% |
| Equity | $99.70 | $109.39 |

More trades because removing bad SHORTs freed slots for better entries.

---

### 13.2 .env configuration changes — 2026-08-17

User updated `.env`:
- `MAX_CONCURRENT`: 3 → 5 (solves slot starvation)
- `MAX_ENTRIES_PER_CYCLE`: 3 → 2 (reduces correlated entries per scan)
- `MAX_TOTAL_MARGIN_PCT`: 0.60 → 0.80 (aggregate margin ceiling)

**Analysis:** With $100 equity and 2x leverage, 5 positions at ~$11 margin each =
~$55 total margin (69% of equity), within the 80% cap. The margin ceiling at
`live_scanner.py:875` blocks further entries when total margin exceeds the cap.
`MAX_ENTRIES_PER_CYCLE=2` limits same-scan correlation risk while still capturing
time-sensitive CSM signals (the 4-5x ATR window is narrow).

---

### 13.3 Per-strategy concurrent caps — 2026-08-17

**Problem:** CSM dominated all 3 (now 5) slots, starving LIQ of entry opportunities.
In BEAR/RANGING, CSM fires frequently while LIQ waits for rare liquidation cascades.
By the time a LIQ signal appears, all slots are full of CSM trades.

**Change:** Added `MAX_PER_STRATEGY` — configurable via `.env` as
`CSM:3,LIQ:2,VRP:3`. Each strategy can hold at most N of the MAX_CONCURRENT slots.
The global cap check changed from `break` to allowing other strategies through when
one strategy is full.

```python
# risk_engine.py
MAX_PER_STRATEGY = _parse_per_strategy(
    os.getenv("MAX_PER_STRATEGY", "CSM:3,LIQ:2,VRP:3")
)

def is_strategy_cap_hit(strategy_id, active_positions):
    cap = MAX_PER_STRATEGY.get(strategy_id)
    if cap is None: return False
    count = sum(1 for p in active_positions if p.get("strategy") == strategy_id)
    return count >= cap
```

**Slot behavior with MAX_CONCURRENT=5:**
- CSM fills 3 slots → CSM blocked, LIQ/VRP still allowed
- LIQ fills 2 slots → LIQ blocked, CSM/VRP still allowed
- No strategy can starve another

Files: `risk_engine.py`, `live_scanner.py` (both live and backtest copies).

---

### 13.4 MAX_HOLD reduced 24h → 12h — 2026-08-17

**Problem:** 62 trades in the 12-24h bucket produced only +$3.24 total (+$0.05/trade
avg). These near-dead trades occupied slots for 12+ hours with negligible edge.

**Duration analysis (30-day backtest, 241 CSM trades):**

| Bucket | Trades | Total PnL | Avg PnL | Win% |
|---|---|---|---|---|
| 0-1h | 34 | -$14.10 | -$0.415 | 18% |
| 1-2h | 37 | -$17.73 | -$0.479 | 11% |
| 2-4h | 54 | +$16.55 | +$0.306 | 43% |
| **4-8h** | **42** | **+$14.00** | **+$0.333** | **62%** |
| 8-12h | 12 | +$7.43 | +$0.619 | 58% |
| 12-24h | 62 | +$3.24 | +$0.052 | 47% |

**Profit zone is 2-12h.** The 4-8h bucket has the highest win rate (62%).
Trades resolving under 2h are heavily negative (quick SL/BE hits). Trades over
12h are near-breakeven slot hogs.

**Change:** `MAX_HOLD_MIN = 720` (12h, was 1440/24h) in CSM only. LIQ and VRP
have no MAX_HOLD — insufficient data to set one (21 LIQ trades, 2 VRP trades
in 40-day backtest).

**40-day backtest result (MAX_HOLD=12h + SHORT filter + per-strategy caps):**

| Metric | Value |
|---|---|
| Trades | 413 |
| Net PnL | +$2.56 (+2.56%) |
| Equity | $102.56 |
| TP_HIT | 59 (+$119.67) |
| TRAIL_HIT | 70 (+$16.57) |
| MAX_HOLD | 82 (+$3.92) |
| BE_HIT | 85 (-$6.69) |
| SL_HIT | 117 (-$130.90) |

MAX_HOLD at 12h is slightly positive (+$3.92 across 82 trades, avg +$0.05),
confirming it frees slots without bleeding money. Note: this is a 40-day run
(not 30) due to data alignment.

---

### 13.5 Core module audit fixes — 2026-08-17

Five parallel audits across all core modules. Fixes ranked by severity:

**HIGH — order_engine.py:** Stop order retry now re-signs params with fresh
`sign_params()`. Stale signature caused Binance -1022 rejection on retries.

**MEDIUM — order_engine.py:** Close order retry increments `fail_count` only on
failure (was incrementing before retry). Fee calculation uses averaged
open+close notional.

**MEDIUM — regime_engine.py:** ADX computation failure returns -1.0 (was 0.0,
which bypassed the ADX gate). ADX gate updated: `if btc_adx < 0 or (btc_adx > 0
and btc_adx < ADX_REGIME_MIN)`. ETH data unavailability warning added.

**MEDIUM — risk_engine.py:** `risk_usdt` recalculated after margin-cap/min-notional
scaling (was returning the pre-scaling value).

**LOW — live_scanner.py:** Paper-mode tick stop-check logs on failure (was silent
`pass`). `_inject_duration_min` uses `pos["exit_time"]` (was `now()`). Removed
hardcoded "IST" from log format.

---

### 13.6 LIQ profitability investigation — 2026-08-17 (in progress)

**40-day backtest LIQ results:** 21 trades, -$4.77, 33% win rate.

- 71% of trades (15/21) close within 2 hours — mostly quick SL hits
- SL hits average -$1.10, TP hits average +$2.10
- Win rate too low at 33% to overcome losses with 2:1 R:R
- Only 1 trade held >12h — and it was a TP winner (+$2.43)
- Adding MAX_HOLD would make LIQ worse (long-held trades are profitable)

**LIQ parameters:** SL 1.5x ATR, TP 3.0x ATR, BE trigger 0.6%, trail 1.5x ATR.
Volume threshold 3x avg, wick threshold 60%.

**Status:** 90-day backtest data fetched. Full 90-day run needed for statistically
significant LIQ analysis (21 trades insufficient). Pending.

---

## 14. Tire 2 Backtest — Strategy Evaluation (73 volatile coins) — 2026-08-19

Ran Tire 2 backtests (1m intrabar SL/TP) across 73 coins filtered by 4% min 24h
volatility range. All strategies used `mock_regime_name="BULL_TREND"`.

### 14.1 ATR-based TP for freqtrade ports — 2026-08-19

**Problem:** Freqtrade strategies (NASOS, SMA, ELLIOT, EI3) had fixed TP at 5-10%
which was unreachable for most trades. Trades exited via manage() sell signals
(RSI/EMA crossovers) well before TP, making TP a dead safety ceiling.

**Change:** `tp_price = entry_price + atr * 3.0` using 14-period ATR on 1m data.
Same approach CSM uses (4x ATR on 15m).

**Result:** Identical to fixed TP — confirms these strategies exit via manage()
sell signals, not TP. The TP is just a safety ceiling that rarely triggers.

### 14.2 Strategy performance results — 2026-08-19

| Strategy | Expectancy | Verdict |
|---|---|---|
| CSM | +1.461% | Edge YES |
| SMA_OFFSET | +1.192% | Edge YES |
| NASOS_V4 | +1.083% | Edge YES |
| ELLIOT_V8 | +0.062% | Marginal |
| EI3_V2 | -0.136% | No edge |
| VRP | -0.498% | No edge |
| LIQ | -0.059% | No edge |

### 14.3 Removed EI3_V2, VRP, LIQ from codebase — 2026-08-19

**Decision:** All three showed negative expectancy in Tire 2 backtests. Removed
entirely — source files deleted, all wiring cleaned across 20+ files.

**Files deleted:**
- `modules/strategies/freqtrade_port_ei3.py` (EI3_V2)
- `modules/strategies/volatility_risk_premium.py` (VRP)
- `modules/strategies/liquidation_cascades.py` (LIQ)
- Same 3 files in `Tire 1-2 Back Test engine/modules/strategies/`

**Wiring cleaned in:** `strategy_factory.py`, `regime_engine.py`, `risk_engine.py`,
`web_server.py`, `live_scanner.py`, `discord_notifier.py`, `discord_bot.py`,
`telegram_notifier.py`, `telegram_bot.py`, `analyze_strategies.py`,
`strategy_overrides.py`, `binance_status.py`, `live_logger.py`,
`backtest_optimizer.py` (both copies), `backtest_freqtrade_ports.py`,
`run_freqtrade_backtest.py`, `tools/replay/sync.py` (both copies),
6 `tools/*.py` scripts.

**Also cleaned:** Stale FF_V2 and TP entries from `web_server.py` dashboard
metadata. CSM missing from BULL_TREND in REGIME_STRATEGY_PERMISSIONS — fixed.

**Production set is now 4 strategies:** CSM, NASOS_V4, SMA_OFFSET, ELLIOT_V8.
All permitted in every regime except OVERHEATED.

### 14.4 Square-off feature — 2026-08-19

Added manual position close ("Square Off") button to the web dashboard.

**Mechanism:** File-based signaling — web server writes to
`data/square_off_queue.json`, live scanner reads it at the top of each cycle
(~5s) and closes via normal `close_position()` path with exit reason "SQUARE_OFF".

**Files:** `web_server.py` (POST `/api/square_off`), `live_scanner.py` (queue
processor), `static/index.html` (Action column), `static/app.js` (button + handler),
`static/style.css` (button styling).

### 14.5 Grid strategy import fix — 2026-08-19

**Problem:** Ubuntu server crashed with `ModuleNotFoundError: No module named
'modules.strategies.grid_strategy'` — file wasn't deployed but was imported
unconditionally.

**Fix:** Made import optional with try/except in `strategy_factory.py`. Grid
strategy is not in `_ALL_STRATEGIES` and cannot open positions — only used by
`live_scanner.py` to dissolve legacy grid positions on regime change.

### 14.6 MAX_HOLD reduced 12h → 8h — 2026-08-19

**Rationale:** Duration analysis from 30-day backtest (241 CSM trades):

| Bucket | Trades | Total PnL | Avg PnL | Win% |
|---|---|---|---|---|
| 0-1h | 34 | -$14.10 | -$0.415 | 18% |
| 1-2h | 37 | -$17.73 | -$0.479 | 11% |
| 2-4h | 54 | +$16.55 | +$0.306 | 43% |
| **4-8h** | **42** | **+$14.00** | **+$0.333** | **62%** |
| 8-12h | 12 | +$7.43 | +$0.619 | 58% |
| 12-24h | 62 | +$3.24 | +$0.052 | 47% |

4-8h is the sweet spot (62% win rate). The 8-12h bucket is still profitable but
only 12 trades. Decision: cut at 8h to free slots faster.

**Change:** `MAX_HOLD_MIN = 480` (was 720) in `cross_sectional_momentum.py`.

### 14.7 Watchlist refresh — fixed 8h UTC windows — 2026-08-19

**Problem:** `REFRESH_HOURS = 24` was too infrequent for catching intraday
volatility shifts. Elapsed-time refresh drifted with bot restarts.

**Change:** Refresh at fixed UTC windows: 00:00, 08:00, 16:00 (05:30, 13:30,
21:30 IST). Data saved in one window becomes stale as soon as the next starts.

Added `_is_stale()` helper in `watchlist.py` that compares current vs saved
8-hour UTC window slot, replacing the elapsed-time check.

### 14.8 Per-strategy slot configuration — 2026-08-19

**Change:** `.env` updated with explicit per-strategy caps:
`MAX_PER_STRATEGY=CSM:1,NASOS_V4:2,SMA_OFFSET:2,ELLIOT_V8:2`

CSM limited to 1 slot (long + short makes each trade high-conviction).
Freqtrade dip-buy ports get 2 slots each (LONG-only, benefit from diversification).

---

## 15. FILES TO SYNC TO UBUNTU (as of 2026-08-19) — SUPERSEDED by §18

**New strategy files (copy to server):**
```
modules/strategies/freqtrade_port_nasos.py
modules/strategies/freqtrade_port_sma.py
modules/strategies/freqtrade_port_elliot.py
```

**Updated files:**
```
modules/strategies/cross_sectional_momentum.py   — MAX_HOLD 12h→8h
modules/strategies/strategy_factory.py            — removed EI3/VRP/LIQ, added freqtrade ports
modules/regime_engine.py                          — cleaned permissions, fixed CSM in BULL_TREND
modules/risk_engine.py                            — cleaned leverage/slots, updated MAX_PER_STRATEGY
modules/strategy_overrides.py                     — updated fallback list
modules/binance_status.py                         — updated fallback list
modules/watchlist.py                              — 8h fixed UTC refresh
live_scanner.py                                   — square-off queue, grid guard
live_logger.py                                    — updated fallback list
web_server.py                                     — square-off endpoint, cleaned strategy meta
analyze_strategies.py                             — updated labels
discord_notifier.py                               — cleaned emoji map
discord_bot.py                                    — cleaned emoji/fallback
telegram_notifier.py                              — cleaned emoji map
telegram_bot.py                                   — cleaned emoji/fallback
static/index.html                                 — Action column for square-off
static/app.js                                     — square-off button + handler
static/style.css                                  — button styling
.env                                              — MAX_PER_STRATEGY added
```

**Delete from server:**
```
modules/strategies/freqtrade_port_ei3.py
modules/strategies/volatility_risk_premium.py
modules/strategies/liquidation_cascades.py
```

---

## 16. HARNESS CORRECTNESS — three verified bugs — 2026-08-19

Found while designing a new strategy. Two adversarial review agents wrote and ran
their own replay code against the real CSVs rather than reasoning in prose, which
is how these surfaced. All three were verified directly before fixing.

### 16.1 The Tire 2 harness was never intrabar — THE BIG ONE

`run_backtest_1m` sets `STEP = 60` (backtest_optimizer.py:536) and then tested
exits with `_intrabar_exit(active, cur_1m.iloc[-1])` — a SINGLE 1m bar.
**59 of every 60 minutes were never tested against the stop.** The docstring on
line 510 claims it steps 5 bars; the code steps 60.

A stop breached at :07 and recovered by :59 was invisible. Live holds a real
STOP_MARKET order and exits the moment price trades through, so this is the same
class of divergence `_intrabar_exit` was itself written to fix — reintroduced by
how it was called.

Direction and size of the error: expectancy was INFLATED, and inflated most for
TIGHT stops (they are the ones ordinary noise reaches). An independent
measurement on one identical signal set put it at +0.160%/trade with the SL_HIT
rate understated by ~13 points. Because the bias scales with stop tightness, it
distorted the RANKING between strategies, not just the levels.

Every figure in §14.2 came through this path.

**Fix:** added `_intrabar_exit_scan(pos, seg)` which walks every 1m bar since the
previous step, behind a vectorised min/max precheck so the per-bar loop only runs
on segments where a level is actually reachable. It also returns the touching
bar's timestamp, so `exit_time` — and therefore every duration bucket — is now
honest rather than rounded to the step boundary.

### 16.2 `trail_reference_price()` returned None on every backtest call

It guarded on `"timestamp" not in df.columns`. Live builds a `timestamp` COLUMN
over a RangeIndex; `load_data()` returns a DatetimeIndex with no such column
(verified). So it returned None on every backtest call and breakeven/trailing
never armed there — the exact live/backtest divergence the method exists to
prevent, hiding inside its own guard.

**Fix:** accept both layouts, deriving an internal `_ts` from the column when
present and from the DatetimeIndex otherwise. Live behaviour is unchanged.

### 16.3 CSM's MAX_HOLD had never fired in a backtest

`manage()` called `datetime.fromisoformat(position["entry_time"])`, but
`run_backtest_1m` sets `entry_time` to a `pd.Timestamp`
(backtest_optimizer.py:594). That raises `TypeError`, the bare `except` swallowed
it, and `duration_min` stayed 0 forever.

This is the second time this exact pattern has cost real information — see §10's
note that `except Exception` hid the MAX_HOLD timezone and RangeIndex bugs in
§11.9. Same feature, same silencing mechanism, third occurrence.

**Fix:** `pd.to_datetime(..., utc=True)` for both timestamps; it accepts strings
and Timestamps alike, so live and backtest agree.

### 16.4 Consequences for prior conclusions

- **The EI3_V2 / VRP / LIQ removals (§14.3) get SAFER, not riskier.** They
  measured negative under a harness that flatters, so their true numbers are
  worse than published. No reason to revisit.
- **ELLIOT_V8 at +0.062% is the one to watch.** Close enough to zero that the
  correction could put it underwater.
- **§14.6's duration table predates the fix.** Durations were rounded to the
  60-minute step and MAX_HOLD never fired, so the bucket boundaries are
  approximate. The SHAPE (sub-2h is a killing field, 2-12h pays) came from a
  large sample and is unlikely to invert, but the exact figures should be
  re-derived.
- The main-tree `backtest_optimizer.py` is the older 15m harness and has no
  `run_backtest_1m`; it is unaffected.

**Files changed:** `Tire 1-2 Back Test engine/backtest_optimizer.py`,
`modules/strategies/base_strategy.py`,
`modules/strategies/cross_sectional_momentum.py`, and the two backtest-engine
copies of the latter pair.

### 16.6 CORRECTED BASELINES — 90d, 100 symbols, fixed harness — 2026-08-19

Re-ran all four production strategies after the §16.1-16.3 fixes. **These
supersede §14.2 entirely.** Same data, same strategies, same window — the only
change is that the stop is now tested against every 1m bar instead of one in
sixty.

| Strategy | §14.2 (buggy) | **Corrected** | Change | Trades | Win% | PF | Median hold |
|---|---|---|---|---|---|---|---|
| NASOS_V4 | +1.083% | **+0.548%** | −49% | 798 | 47.9% | 1.13 | 2.5h |
| CSM | +1.461% | **+0.309%** | −79% | 4,192 | 54.6% | 1.22 | 6.0h |
| SMA_OFFSET | +1.192% | **+0.028%** | −98% | 697 | 50.4% | **1.01** | 1.8h |
| ELLIOT_V8 | +0.062% | **+0.022%** | −65% | 1,018 | 62.3% | **1.01** | 1.4h |

**The ranking inverted.** NASOS_V4 is now the best per-trade strategy; CSM was
first and is now second. CSM remains the largest TOTAL contributor (+1,296% of
~+1,775%) purely on volume — 4,192 trades against NASOS's 798.

**SMA_OFFSET and ELLIOT_V8 are at profit factor 1.01** — not an edge, a coin
flip that clears zero by a rounding error. Together they produce 2.4% of total
P&L while holding 4 of the per-strategy slots.

Note the harness prints `Edge: YES` for anything above zero, so PF 1.01 carries
the same label as PF 1.22. Do not read that column as a verdict.

**These are still gross of slippage** (harness ran `SLIPPAGE_PCT=0`). §6 measured
live slippage at ~14% of gross P&L. At 0.03%/side both marginal strategies go
negative: SMA_OFFSET −0.032%, ELLIOT_V8 −0.038% — the same band as LIQ (−0.059%),
which was removed on exactly this evidence.

**ELLIOT_V8 has a second structural problem.** Win rate 62.3% but R:R 0.61
(avg win +4.43%, avg loss −7.26%), and its median return (+4.92%) EQUALS its best
trade — most winners exit at one capped level while losers run to the 8% stop.
Combined with a 1.4h median hold, it sits squarely in the sub-2h zone §14.6
identifies as where money dies.

**Why the damage scales the way it does:** the dip-buy ports buy capitulation.
Sometimes the capitulation continues, hits the 8% stop intrabar, and recovers
within the hour. The old harness never looked, so the trade "survived" and later
exited on the recovery signal for a profit. Live holds a real STOP_MARKET order
and would have taken the −8%. This is the most likely explanation for any
unexplained live-vs-backtest divergence recorded before 2026-08-19.

### 16.7 CSM signal strength is not predictive — 2026-08-19

Tested whether normalized momentum predicts expectancy INSIDE CSM's 4-5x band,
to decide between `strength = 1.0` and `strength = max(0, min(1, |mom|-4))`.
1,337 trades, 30 symbols, 90d, fixed harness:

| Momentum | N | E[net] | SE | Win% |
|---|---|---|---|---|
| 4.0-4.2x | 388 | +0.419% | 0.263 | 56.4% |
| 4.2-4.4x | 297 | +0.556% | 0.355 | 55.9% |
| **4.4-4.6x** | 238 | **+1.114%** | 0.361 | 58.0% |
| **4.6-4.8x** | 225 | **+1.107%** | 0.380 | 59.6% |
| 4.8-5.0x | 189 | **+0.387%** | 0.543 | 51.3% |

**The response is an inverted U, not a ramp** — edge peaks at 4.4-4.8x and falls
at 4.8-5.0x, confirming the long-standing comment that momentum above ~5x tends
to reverse. A monotonic `|mom|-4` score therefore ranks the WORST bucket highest.

The split that `MIN_STRENGTH=0.50` would drive is not significant:
kept (>=4.5x) +0.975% vs blocked (<4.5x) +0.496%, difference +0.479%, SE 0.344,
**t = +1.39**. Correlation(momentum, pnl) = **+0.0217**.

**RESOLVED IN §17.15** (4,277 trades): the inverted U replicated and the
4.8-5.0x bucket went NEGATIVE. The `strength = 1.0` decision below is
confirmed (the 4.5x split fell to t=0.08), but the bucket figures here are
superseded by the larger sample.

**Decision: CSM stays pinned at `strength = 1.0`.** The filter would discard 810
profitable trades (+401.5% summed) on t=1.39 evidence. Recorded at
live_scanner.py:211. This is "not resolved" rather than "no effect" — 30 symbols
gave 1,337 trades and the full 100 gives ~4,200, which could move it either way.

---

### 16.8 CSM profit ladder replaces breakeven — 2026-08-20

**Problem reported from paper trading:** trades reach +1-2% and never bank it,
then drift into a loss over hours.

**Two mechanisms, same root cause.** `breakeven_trigger()` arms at
`entry + max(1xATR, 1.5%)` and then parks the stop at `entry * 1.001`:
1. When it armed, the 2xATR trail still sat below entry, so `max()` pinned the
   stop at +0.1%. Median trade was **+0.020%**; 21.5% of trades exited between
   -0.5% and +0.25%.
2. When ATR was wide — normal on the 4%+-daily-range universe — it did not arm
   until +2-3%, so a +1-2% winner had NO protection and rode the full 3-8% stop
   down. 26.6% of trades lost more than 2%.

**Change:** `PROFIT_LADDER = [(0.020, 0.010), (0.040, 0.025)]` — clear +2% lock
+1%, clear +4% lock +2.5% — with the existing 2xATR trail layered on top. Gain
is measured on a COMPLETED bar via `trail_reference_price()`, never the tick.

Variant sweep, 887-941 trades / 20 symbols / 90d:

| variant | E[net] | median | trades >+4% |
|---|---|---|---|
| breakeven (old) | +0.788% | +0.020% | 177 |
| lock 1%→0.3, 2%→1, 3%→1.8 | +0.747% | +0.220% | 143 |
| lock 1.5%→0.5, 3%→1.5 | +0.725% | +0.420% | 153 |
| **lock 2%→1.0, 4%→2.5** | **+0.813%** | **+0.799%** | 158 |

Confirmed on the full 90d/100-symbol run WITH 0.03%/side slippage:
**+0.300%/trade, median +0.267%, PF 1.21, total +1284%** versus ~+1044%
for the old exit under the same slippage — **the ladder is worth ~+240pp.**

⚠️ DO NOT tighten the first rung to +1%. Tested: it cuts >+4% trades from 177 to
143 and lowers expectancy. Banking small wins earlier feels better and measures
worse — the right tail is where the edge lives.

### 16.9 MIN_STRENGTH is per-strategy, not global — 2026-08-20

An rsi_fast-derived strength score, gated at `MIN_STRENGTH=0.50` (rsi_fast <
17.5), was applied to all three freqtrade ports. **It helps one and badly hurts
another.** Measured 90d / 100 symbols, fixed harness, 0.03%/side slippage:

| Strategy | Ungated | Gated | Delta |
|---|---|---|---|
| NASOS_V4 | 798 trades, +389% | 421 trades, +147% | **−242pp** |
| ELLIOT_V8 | 1018 trades, −39% | 430 trades, +62% | **+101pp** |
| SMA_OFFSET | −22% | −22% | negative either way |

Why the asymmetry: NASOS takes ~8.9 trades/day across 100 symbols with a 0.7h
median hold, so its slots are NOT contested — discarding 47% of its signals buys
nothing and costs throughput. ELLIOT ungated is a net loser (72% win rate but
R:R 0.61, small wins in front of an 8% stop); the gate keeps only the deep
washouts where its edge actually is.

**Resolution — no scanner change needed.** Each strategy opts in by emitting a
varying strength, or opts out by pinning 1.0:

| Strategy | strength | gated? |
|---|---|---|
| CSM | 1.0 | no — momentum is non-predictive, see §16.7 |
| NASOS_V4 | 1.0 | no — gate costs 242pp |
| ELLIOT_V8 | `(35 - rsi_fast)/35` | yes — gate gains 101pp |

**Methodology note:** an earlier in-house replay script said the gate helped
NASOS (+0.059% → +0.322%). The harness disagreed. The script counted 875 NASOS
trades against the harness's 798 — a symbol-universe difference — and its
absolute levels are not trustworthy. The harness figures are authoritative here.
Evaluating on expectancy-per-trade alone was also wrong: total P&L matters when
slots are not the binding constraint.

### 16.10 DELETED SMA_OFFSET — 2026-08-20

Negative in every configuration on the corrected harness with realistic
slippage: **−0.072%/trade ungated, −0.109%/trade gated, total −21.5%** over 90d
/ 100 symbols. PF 0.97. The strength gate improved it but not past zero.

It was the #2 strategy (+1.192%) under the pre-§16.1 harness. That figure was an
artefact of the single-bar exit check.

Source files removed from both trees; wiring cleaned across ~25 files.
`STRATEGY_LEVERAGE["SMA_OFFSET"]` retained so historical positions still size
correctly.

**Production set is now THREE: CSM, NASOS_V4, ELLIOT_V8.**

### 16.11 `BT_MIN_STRENGTH` — harness now models the scanner gate — 2026-08-20

`run_backtest_1m` called `scan()` and acted on every signal, so it scored signals
the live scanner discards. A strategy whose edge comes from that gate measured as
worthless. Added `BT_MIN_STRENGTH` (env, default 0 so historical runs are
unchanged) mirroring `live_scanner.MIN_STRENGTH`.

Same class of defect as §16.1: the backtest not modelling what live actually
does.

### 16.12 MAX_MARGIN_PCT stays at 0.30 — 2026-08-20

Considered lowering to 0.15. Rejected. The cap SCALES positions down
(risk_engine.py:299), so at 0.15 trades with stops between 5.0% and 6.7% get
shrunk and risk LESS than the intended 1%, while all others risk the full 1% —
non-uniform sizing for no measured benefit.

At 0.30 the cap never binds ($19.61 max margin on $100 equity), so every trade
gets uniform 1% risk. The over-exposure incident the cap was written for (86% of
equity committed, $27-30/trade) was caused by sub-1% stops and is already fixed
at source by `MIN_SL_PCT=0.015`.

Portfolio exposure is bounded anyway: 3 x $19.61 = $58.83 against the $60
aggregate ceiling. Note that is TIGHT — raising `MAX_CONCURRENT` to 4 would make
the aggregate cap start rejecting entries.

To reduce risk, lower `RISK_PCT_PER_TRADE` (uniform) rather than
`MAX_MARGIN_PCT` (distorts one stop band).

### 16.13 The real-regime backtest path had never worked — 2026-08-20

Two more tz-naive/tz-aware collisions, same family as §16.2:

- `load_funding()` cached branch used `pd.read_csv(parse_dates=...)`, which
  yields a tz-NAIVE index while the fresh-fetch branch builds a tz-aware one.
  `build_regime_series` compared the two and raised. Cold cache worked; warm
  cache did not.
- `regime_at()` bisected tz-aware keys against the tz-naive `cur_1m.index[-1]`
  that `run_backtest_1m` passes.

**Consequence: `regime_series=` was unusable on the 1m harness, so EVERY Tire 2
result ever produced came from `mock_regime_name` — a CONSTANT regime.** All
three strategies are mocked as permanent `BULL_TREND`.

Measured wall-clock distribution over the 90d window:

| Regime | Share |
|---|---|
| RANGING | 60.2% |
| BEAR_TREND | 20.3% |
| BULL_TREND | 19.5% |

So the mock assumed 100% of a regime that held 19.5% of the time. Fixed at
`regime_at()` rather than per call site.

Note this does NOT invalidate the aggregate numbers in §16.6: none of the three
strategies reads `regime`, so the trade SET is identical either way — only the
LABEL differs. Verified: NASOS gives 875 trades / -0.001% under both mock and
real regimes. The mock mattered for per-regime attribution, not for totals.

### 16.14 Per-regime expectancy, all three strategies — 2026-08-20

90d, 100 symbols, real regime labels, 0.03%/side slippage, `BT_MIN_STRENGTH=0.50`.

| Strategy | Regime | N | E[net] | SE | t | SUM |
|---|---|---|---|---|---|---|
| CSM | **RANGING** | 2432 | **+0.589%** | 0.104 | **5.64** | +1431.4% |
| CSM | BULL_TREND | 1067 | −0.033% | 0.131 | −0.25 | −35.7% |
| CSM | BEAR_TREND | 778 | −0.144% | 0.170 | −0.85 | −111.9% |
| NASOS_V4 | RANGING | 549 | −0.266% | 0.251 | −1.06 | −146.0% |
| NASOS_V4 | BULL_TREND | 160 | +0.540% | 0.427 | 1.27 | +86.4% |
| NASOS_V4 | BEAR_TREND | 166 | +0.351% | 0.430 | 0.82 | +58.3% |
| ELLIOT_V8 | RANGING | 264 | +0.173% | 0.318 | 0.54 | +45.7% |
| ELLIOT_V8 | BULL_TREND | 83 | +0.440% | 0.498 | 0.88 | +36.5% |
| ELLIOT_V8 | BEAR_TREND | 83 | −0.245% | 0.591 | −0.41 | −20.3% |

**CSM and NASOS are mirror images** — CSM earns in RANGING and loses in trends;
NASOS does the opposite. That is real diversification, and an argument for
keeping both rather than concentrating on the higher-expectancy one.

**Only CSM's RANGING cell is statistically significant.** Every other cell has a
CI spanning zero. NASOS and ELLIOT cells (n = 83–549) are too thin to act on.

The old contaminated numbers had CSM's BEAR as its BEST cell (+1.218%); on the
fixed harness it is the WORST. The lookahead bug did not merely inflate levels,
it INVERTED the regime ranking — and the pre-2026-08-19 matrix was built on it.

### 16.15 CSM direction-within-regime — measured, NOT applied — 2026-08-20

| Regime | Side | Role | N | E[net] | t | 95% CI |
|---|---|---|---|---|---|---|
| RANGING | LONG | — | 1669 | +0.533% | 4.27 | [+0.289, +0.778] |
| RANGING | SHORT | — | 763 | +0.710% | 3.74 | [+0.338, +1.082] |
| BULL | LONG | with | 930 | −0.119% | −0.91 | [−0.377, +0.138] |
| BULL | SHORT | counter | 137 | +0.550% | 1.11 | [−0.423, +1.523] |
| BEAR | LONG | counter | 244 | +0.338% | 0.96 | [−0.352, +1.028] |
| BEAR | SHORT | with | 534 | −0.364% | −1.94 | [−0.732, +0.004] |

Pooled: counter-trend − with-trend = **+0.623%, SE 0.307, t = +2.03**.

Mechanism is plausible — CSM buys 4-5x ATR CONTINUATION, and inside a trend a
move that extreme is more often exhaustion. Exit mix supports it: BULL trades
are 48.4% MAX_HOLD at −0.038%, i.e. half enter and go nowhere.

**NOT IMPLEMENTED, deliberately.** Two reasons:
1. t = 2.03 after examining six cells. With six looks, P(one crossing p<0.05 by
   chance) ≈ 26%.
2. Both cells a filter would remove are UNSTABLE across 30-day thirds:
   BULL LONG −0.028 / +0.036 / −0.552; BEAR SHORT −0.577 / −0.224 / **+0.484**.
   BEAR SHORT is POSITIVE in the most recent third.

RANGING, by contrast, is +0.523 / +0.313 / +0.826 — positive in all three. That
is the one robust regime finding in this session.

Config totals for reference: all regimes +1283.9%; RANGING-only +1431.4%;
block-with-trend +1589.3%. The last is an in-sample fit to two unstable cells
and should not be trusted.

### 16.16 NASOS strength gate — RESTORED after a bad revert — 2026-08-20

§16.9 removed NASOS's rsi_fast strength on the claim that gating cost 242pp.
**That was wrong.** It rested on a stale harness figure (798 trades, +0.548%)
that does not reproduce. Re-running the identical harness gave:

| Config | Trades | E[net] | PF | Total |
|---|---|---|---|---|
| Ungated | **875** | −0.001% | 1.00 | −1.2% |
| Gated | 421 | +0.350% | 1.16 | **+147.2%** |

875 confirmed three independent ways: harness re-run, independent replay, and
mock-vs-real regime. Ungated the harness reports **"Edge: NO"**. Kept trades
average +0.350%, rejected ones −0.327% — a clean split.

Strength formula restored. **Do not remove it again without re-measuring**; the
history is recorded in the file itself.

Lesson: the 242pp figure came from mixing a no-slippage number with a
slippage-adjusted one across two measurement paths. Compare within one path.

### 16.17 Profit ladder on the freqtrade ports — REJECTED — 2026-08-20

The CSM ladder (§16.8) was worth +240pp, and ELLIOT's R:R of 0.61 looked like
the same disease. Tested five variants on both ports (25 symbols, 90d):

| Variant | NASOS E / SUM | ELLIOT E / SUM |
|---|---|---|
| baseline (shipped) | +0.239% / +75.9% | +0.285% / +99.8% |
| lock 3%→1.5, 6%→4 | +0.248% / +78.8% | +0.285% / +99.8% |
| lock 2%→1, 4%→2.5 | +0.212% / +67.5% | +0.234% / +82.1% |
| lock 4%→2, 8%→5 | +0.147% / +46.6% | +0.243% / +85.0% |

Best case is a wash; everything else is worse. **Do not add one.**

Why the hypothesis failed: the ports' baseline MEDIAN is **+2.3% to +2.5%**,
against CSM's **+0.020%** before its ladder. They do not have CSM's
breakeven-amputation problem — their sell signal already banks a healthy
profit. Their low R:R comes from the 8% stop sizing losses, not from winners
being cut short. ELLIOT's 3%/6% variant is byte-identical to baseline because
most trades exit BELOW the first rung, so the ladder never fires.

### 16.18 run_all.bat paper mode — 2026-08-20

`run_all.bat paper` starts the dashboard and scanner only, waits 6s for uvicorn
to bind before opening the browser, and REFUSES to run when `LIVE_ENABLED=true`
— a mode named "paper" must not be able to reach the exchange. Telegram/Discord
bots are skipped, since only one instance may poll a token and a local copy
fights the VPS.

### 16.5 Correction to a claim made during this session

It was asserted mid-session that the 1.5-2.0% stop band was an unexploited
capital-efficiency opening, because `MAX_LEVERAGED_LOSS_PCT=0.10` clips leverage
to `floor(0.10/sl_pct)` and no strategy sits in that band.

**This was wrong.** `risk_usdt` is a fixed 1% of equity at EVERY stop width; the
leverage step-down exactly cancels the position-size change. Measured through
`compute_position_size` directly:

| SL | Leverage | Notional | Margin | Risk $ | Margin/Risk |
|---|---|---|---|---|---|
| 1.6% | 5x | $625 | $125 | $10.00 | 12.50x |
| 1.8% | 5x | $556 | $111 | $10.00 | 11.11x |
| 2.0% | 5x | $500 | $100 | $10.00 | 10.00x |
| 3.0% | 3x | $333 | $111 | $10.00 | 11.11x |
| 5.0% | 2x | $200 | $100 | $10.00 | 10.00x |
| 8.0% | 1x | $125 | $125 | $10.00 | 12.50x |

Margin sits at 10-12.5x risk with no trend, and risk is constant. Only expectancy
in **R multiples** matters when comparing strategies. A tight stop buys nothing
by itself. Recorded here because the false version is superficially convincing
and would otherwise be rediscovered.

---

## 17. THE 5% SL EPISODE, THE REVERT, AND A FULL AUDIT — 2026-08-22 → 08-28

### 17.1 MAX_SL_PCT — an entry-side stop ceiling

A live trade exited at **-16.85%**. The cause was an unwritten `if sl_pct > 0.20`
guard in `compute_position_size` — a 16.8% stop passed it untouched.

Replaced with `MAX_SL_PCT` (env, default **0.10**). This rejects the *signal at
entry*; it never closes an open position. That distinction is the whole reason it
survived the revert below while the exit guard did not.

`0.03` was tried and costs ~92% of CSM's profit — CSM's 2xATR stops on volatile
symbols routinely exceed 3%, so the filter removes its trade population.

Measured cost at 0.10, from the 90d/100-symbol run in §17.13: **152 signals
rejected** out of 4,134 generated (3.7%), including stops of 46.6%, 31.7% and
30.6%. Cheap insurance against the tail that produced the -16.85% exit.

### 17.2 MAX_TRADE_LOSS_PCT — measured harmful, deployed anyway, then reverted

A hard per-trade ceiling on LEVERAGED loss (the "ROI" figure Binance shows),
enforced on exit. Set to 0.05 in response to the -16.85% trade.

**It was measured BEFORE deployment and the measurement said not to ship it.**
90d / 30 symbols:

| Strategy | Without guard | With 5% guard | |
|---|---|---|---|
| CSM | +911% | +399% | fired on 44.6% of trades |
| NASOS_V4 | +86% | **-56%** | went negative |
| ELLIOT_V8 | +76% | **-20%** | went negative |

Two mechanisms: it fires on positions that dip past the ceiling and then
*recover*, and it runs BEFORE the strategy's own exit logic, so breakeven,
trailing and the CSM ladder never got to act on a losing trade.

Live confirmed it exactly — user report: "it kill all the trades." Reverted to
`MAX_TRADE_LOSS_PCT=0`. The guard code remains in `live_scanner` but is skipped
entirely at 0.

### 17.3 Why 3% is worse than 5%, and both are traps at 5x leverage

Leverage compresses the price tolerance the ceiling represents:

| Ceiling | 5x | 3x | 1x |
|---|---|---|---|
| 5% | 1.00% price move | 1.67% | 5.00% |
| 3% | **0.60%** | 1.00% | 3.00% |

At `GLOBAL_LEVERAGE=5`, a 3% ROI ceiling closes on a 0.60% price move — inside
ordinary noise for every symbol traded. A ceiling expressed in ROI is not a
ceiling on the trade; it is a ceiling divided by leverage.

If a backstop is ever wanted again, `0.10` is the only defensible value: with
`MAX_LEVERAGED_LOSS_PCT=0.10` already capping `sl_pct x leverage` at 0.10, a 0.10
exit guard sits exactly on top of the stop and therefore only catches price
GAPPING through it, never an ordinary excursion.

### 17.4 Restart persistence

`CLOSE_ON_SHUTDOWN=false` persists open positions to `data/open_positions.json`
and resumes them on the next start; `MAX_RESUME_AGE_HOURS` refuses positions that
went unmanaged too long.

Setting the flag alone was not sufficient: positions must load BEFORE
`reconcile_with_exchange()`, which market-closes any Binance position the bot
does not know about. Reverted to `true` (flat on every restart) with the rest.

### 17.5 The revert — and the trap inside it

Applied: `MIN_STRENGTH=0`, `MAX_TRADE_LOSS_PCT=0`, `CLOSE_ON_SHUTDOWN=true`,
`CSM_MAX_HOLD_MIN=720`, `CSM_PROFIT_LADDER=off`. Kept: `MAX_SL_PCT=0.10`.

**`CSM_PROFIT_LADDER=off` alone was NOT a revert.** `be_hit` was only ever set
inside the ladder loop, and `be_hit` is what enables the ATR trail. Emptying the
ladder therefore produced a position with **no breakeven AND no trail** — running
to the hard stop or TP with no stop management at all. Strictly worse than either
design, while looking like a restoration.

Fixed by making the legacy path an explicit `else:` branch (`BE_TRIGGER = 0.015`,
`breakeven_trigger()` ATR-scaled, then the 2xATR trail). Verified on synthetic 1m
bars, both directions:

| Mode | arms at | stop after +4.5% |
|---|---|---|
| ladder=off (legacy) | +2.1% -> stop +0.60% | +2.00% |
| ladder=on | +3.0% -> stop +1.10% | +2.00% |

SHORT mirrors LONG exactly. The one-step lag in both is the completed-15m-bar
reference (`trail_reference_price`), which is intended.

A first attempt to test this reported BOTH modes broken. That was the *test*: it
fed 15m bars, and `trail_reference_price` selects closes where
`minute % 15 == 14`, which a 15-minute index never satisfies. It needs 1m bars.

Independent confirmation that the legacy path is genuinely live: §17.13 reports
`BE_HIT=715` and `TRAIL_HIT=425` — both non-zero, which is only possible if the
`else:` branch arms `be_hit`.

### 17.6 The `.env` encoding bug — fired on every entry

`_global_leverage_override()` did `open(_ENV_FILE)` with no encoding. On Windows
that is cp1252, and `.env` carries non-ASCII comment art, so it raised
`UnicodeDecodeError` — which is **not** an `OSError`, so the `except OSError`
below it did not catch it. The call is reached via `get_leverage()` ->
`compute_position_size()`, i.e. on **every single entry**.

Worse, the WRITE path in both bots was `open(ENV_FILE, "w")` with no encoding.
`"w"` truncates immediately, then the write raises `UnicodeEncodeError` — so
editing `MAX_CONCURRENT` or leverage from Telegram/Discord would **blank the
`.env` and then fail**. Fixed in 5 read sites and 2 write sites; round-trip now
verified byte-identical.

### 17.7 Codebase audit — 2026-08-27/28

Full pass: 104 files compiled, 27 modules imported, no undefined names, no bare
`except:`, no mutable default arguments.

**`_regime_changed_at` was a dead gate.** A module global read by
`_scan_for_signals` and `_execute_entries`, but assigned inside `main()`, which
declared only `global _shutdown, ACCOUNT_EQUITY`. Both assignments bound a
main()-local; the global stayed pinned at import time. Consequences: the
`MIN_REGIME_AGE_MINUTES=5` stability gate only ever fired in the first 5 minutes
of process life and **never after an actual regime change**, and `ml_engine` was
trained on process uptime under the feature name `regime_age_min`. Present in
both trees. An AST sweep for the same pattern across every file found no others.

**Loss-cap tracker could silently zero itself.** `_load_tracker()` did
`json.load(open(...))` inside `try/except: pass`; a corrupt file returned a zeroed
tracker, silently forgetting an active daily/weekly cap and letting the bot keep
trading. Now logs loudly.

**`datetime.utcnow().timestamp()` skew.** Naive UTC reinterpreted as local time —
measured **-5.50 hours** on the IST dev box, 0 on a UTC server. Shifted the fetch
window in the three backtest data scripts. Fixed.

**13 unmanaged file handles** (`json.load(open(...))`) across both trees, in code
called every cycle. **`aiohttp`** was a hard import in `ngrok_runner.py` but
undeclared — it only resolved transitively via `discord.py`.

### 17.8 THE HARNESS HAD NEVER RUN CSM — 2026-08-28

Attempting to reproduce the §16.6 CSM baseline returned **0 trades over 90 days /
100 symbols**. Not a data problem, and not `MAX_SL_PCT` (A/B'd at 0.10 vs 0.20 —
identical zero).

`_is_1m_strategy()` routed on `REQUIRES_1M_DEPTH > 200`. CSM's value is exactly
**200** — the default — so `200 > 200` is False and CSM fell through to
`run_backtest()`, the legacy 15m replay. `data/` holds **zero** `*_15m_*.csv`
files (343 are 1m), so `load_data(symbol, "15m", days)` returned `None` and the
runner returned `[]` for every symbol.

CSM itself was healthy the whole time — calling `scan()` directly on the same
cached data produced **2,798 signals across 5 symbols**.

The harness reported this as `CSM  0 trades`, which reads as "the strategy
produces nothing." Boundary corrected to `>=`; `run_backtest_1m` was always built
for CSM — it resamples 1m->15m and 1m->1h precisely to serve CSM's `df_15m` and
`df_1h` arguments.

### 17.9 Live and backtest disagree on entry cadence by 60x

| | entry evaluation |
|---|---|
| Live | every `SCAN_INTERVAL_SECONDS=60` — i.e. every 60 **seconds** |
| Backtest | `STEP = 60` over 1m bars — every 60 **minutes** |

There is no delay *between* individual live entries: `MAX_ENTRIES_PER_CYCLE=3`
opens up to three positions back-to-back in the same cycle with no stagger (the
only `time.sleep` is the main loop's). The only per-symbol spacing is
`LOSS_COOLDOWN_MINUTES=15` after a *losing* trade — which the harness does model.

This does not invalidate per-trade expectancy, but the backtest cannot see a
signal that appears and disappears inside an hour, and it enters at different
prices than live will. Live sees strictly more opportunities than any backtest
figure implies.

### 17.10 Volatility floor stays at 4% — measured, not assumed

`MIN_RANGE_PCT = 0.04` in both `symbol_filter.py` and `watchlist.py`. Live
Binance data: 521 symbols pass price+volume, **464 clear the 4% floor**, and the
watchlist caps at `FOCUSED_SIZE=150`. The floor is nowhere near binding —
relaxing it cannot add a single candidate.

It would also make the universe *quieter*. The pool is sorted by **volume**, not
volatility, then truncated. Dropping 4% -> 2% swaps only 6 of 150:

```
dropped: AERO, GRVT, HUMA, MUBARAK, ORDI, SEI    (volatile mid-caps)
added:   BTC, BNB, LTC, HBAR, DELL, SPCX         (quiet large-caps)
```

Median 24h range of the top 150 would fall 9.87% -> 9.51%. Wrong direction for
CSM, whose entry is extreme normalized momentum at 4-5x ATR.

The real lever is the **sort key**, not the floor. Sorting by volatility changes
73 of 150 symbols and lifts median range to **13.12%**, but drops median 24h
volume from $37M to $12M. A `sqrt(volume) x range` blend holds $37M while
reaching 12.43%. **Not applied** — it shifts every established baseline and must
be measured first.

### 17.11 Tree sync — main vs `Tire 1-2 Back Test engine`

35 shared files: 23 identical, 12 differ. Strategy files (`base_strategy`, CSM,
NASOS, ELLIOT) are in sync. Genuine staleness in the backtest tree:

- **`regime_engine.py`** still lists deleted `VRP`/`LIQ`, and CSM's gating is
  effectively inverted vs main: BT has `BULL_TREND: {"VRP": True}` (no CSM) and
  `BEAR_TREND: {"CSM": True}`, where main has CSM permitted in BULL and
  `CSM: False` in BEAR. Harmless for raw runs — the harness replays **ungated**
  and only tags `regime_at_entry` — but any per-regime breakdown reads against a
  stale map. **§17.13 independently vindicates main's version.**
- **`symbol_filter.py` / `watchlist.py`** lack the 4% volatility floor entirely,
  and `REFRESH_HOURS` is 24 vs main's 8.
- **`data_hub.py`** has no `depth_1m` parameter — it fetches 200 1m bars, but
  NASOS/ELLIOT declare `REQUIRES_1M_DEPTH=1500`.
- **`risk_engine.py`** hardcoded `sl_pct > 0.20`, so backtests accepted stops the
  live bot now rejects at 10%. **Ported** — both engines now reject the 16.8%
  case and accept the 8% port stop.

### 17.12 Runtime state: all three strategies were disabled

`data/strategy_overrides.json` had CSM, NASOS_V4 and ELLIOT_V8 all
`disabled: true` since 2026-08-24. `StrategyFactory.get_permitted()` filters on
it, so it returned **nothing in every regime** — no entry was possible regardless
of config, code or universe. Recorded because it is invisible in `.env` and in
the code, and it silently explains "the bot isn't trading."

### 17.13 CSM RE-BASELINE — baseline reproduced and exceeded — 2026-08-28

> **CORRECTED 2026-08-29 — numbers below are OVERSTATED.** Produced by
> `run_backtest_1m` before the look-ahead leak was fixed (see 17.25). The leak
> flatters long-biased configs; this one was long-heavy. See 17.26 for the
> re-measured figures.

90d / 100 symbols, corrected routing, current live config
(`CSM_PROFIT_LADDER=off`, `CSM_MAX_HOLD_MIN=720`, `MIN_STRENGTH=0`,
`MAX_SL_PCT=0.10`), 3 slots, 5x, 0.08% round-trip fee.

| N | WIN% | AVG W | AVG L | R:R | **E[net]** | MEDIAN | PF | SUM | HOLD | WORST STREAK |
|---|---|---|---|---|---|---|---|---|---|---|
| 3982 | 56.1% | +3.38% | -3.46% | 0.98 | **+0.377%** | +0.020% | 1.25 | +1502.4% | 5.8h | 23 |

**vs the §16.6 baseline of +0.300%/trade: reproduced and exceeded (+0.377%).**
Tier: Elite. Edge: YES.

Robustness — the edge is broad, not a few lucky trades:

| SUM | ex TOP5 | ex TOP10 | BEST single | verdict |
|---|---|---|---|---|
| 1502.4% | 1291.2% | 1115.5% | 47.47% | robust — broad edge |

Exit mix: SL 1157 (29.1%), TP 586 (14.7%), MAX_HOLD 1085 (27.2%), BE_HIT 715
(18.0%), TRAIL_HIT 425 (10.7%), END_OF_DATA 14.

The median of **+0.020%** is the signature of the legacy (non-ladder) exit — it
matches the pre-ladder median recorded in §16.8 exactly, confirming
`CSM_PROFIT_LADDER=off` is genuinely in force. The ladder's contribution was to
lift that median; the mean edge survives without it.

Weekly consistency: **11 of 14 ISO weeks positive** (worst W21 -101.9%, best W33
+310.5%).

Portfolio simulation, risk model enforced: 898 of 3982 trades taken (slot-limited
at 3), **+190.88% return, 13.0% max drawdown, Sharpe 5.72**. Capacity: 3982
signals in 90d against a theoretical max of ~1120 trades at 5.8h median hold —
CSM alone can saturate all three slots.

Per-regime, and this is the important part:

| Regime | N | WIN% | E[net] | SUM |
|---|---|---|---|---|
| RANGING | 2254 | 58.0% | **+0.700%** | +1578.7% |
| BULL_TREND | 990 | 50.7% | +0.104% | +102.7% |
| BEAR_TREND | 738 | 57.3% | **-0.243%** | -179.0% |

The harness's own suggested permissions, derived independently from this window:

```
"BEAR_TREND": {}
"BULL_TREND": {"CSM": True}
"RANGING":    {"CSM": True}
```

This **exactly matches the live `REGIME_STRATEGY_PERMISSIONS` in main**, including
the manually-applied `"CSM": False` in BEAR_TREND. The live config is vindicated
by an out-of-band measurement, and the backtest tree's stale map (§17.11) is
confirmed wrong. Note BEAR_TREND wins 57.3% of the time and still loses money —
win rate without R:R is meaningless.

Caveat: one in-sample 90-day window. Treat the per-regime cells as confirmation
of an existing config, not as licence to tune further.

**Superseded as the live config by §17.14** — the ladder was re-enabled on
2026-08-28. These figures remain the ladder-OFF reference point.
### 17.14 CSM ladder RESTORED — ladder=on + 8h hold — 2026-08-28

User re-enabled `CSM_PROFIT_LADDER=on` and `CSM_MAX_HOLD_MIN=480` on the server,
keeping the rest of the §17.5 revert. Same 90d / 100 symbols, same harness.

**Caveat: two variables moved at once** (ladder AND max-hold), so the deltas below
are the combined effect and cannot be attributed individually. See §10, "one
change at a time" — recorded rather than hidden.

| | ladder OFF, 720 | ladder ON, 480 |
|---|---|---|
| N | 3982 | 4277 |
| WIN% | 56.1% | 53.4% |
| AVG W | +3.38% | +3.33% |
| AVG L | -3.46% | **-3.04%** |
| R:R | 0.98 | **1.10** |
| E[net] | **+0.377%** | +0.360% |
| MEDIAN | +0.020% | **+0.327%** |
| PF | 1.25 | 1.25 |
| SUM | +1502.4% | **+1540.5%** |
| HOLD | 5.8h | 5.4h |
| worst streak | 23 | 28 |
| ex TOP5 / TOP10 | 1291% / 1116% | **1331% / 1175%** |

Portfolio simulation (3 slots, risk model enforced) — the figure that matters,
since expectancy per signal is academic when slots are the binding constraint:

| | TAKEN | RETURN | MAXDD | SHARPE |
|---|---|---|---|---|
| ladder OFF, 720 | 898 | +190.88% | 13.0% | 5.72 |
| ladder ON, 480 | **1041** | **+204.96%** | 17.6% | **6.79** |

**The ladder raises the typical trade, not the average one.** Median +0.020% ->
+0.327% (16x) while the mean edge falls slightly, +0.377% -> +0.360%: rungs bank
gains that would sometimes have run further. The compensation is a smaller average
loss (-3.46% -> -3.04%) and R:R crossing 1.0 for the first time. Higher return and
Sharpe, at the cost of drawdown (13.0% -> 17.6%).

Exit mix: `TRAIL_HIT` 425 -> **1072**, `MAX_HOLD` 1085 -> **1618** (the 8h clock
cuts far more trades than 12h), TP 14.7% -> 11.1% (rungs exit before target),
SL 29.1% -> 25.7%.

Per-regime, the ladder also limits BEAR_TREND damage:

| Regime | OFF | ON |
|---|---|---|
| RANGING | +0.700% (2254) | +0.649% (2432) |
| BULL_TREND | +0.104% (990) | +0.027% (1067) |
| BEAR_TREND | -0.243% (738) | **-0.084%** (778) |

Suggested permissions are **identical** under both configs — `BEAR_TREND: {}`,
CSM in BULL and RANGING — matching live for the second independent time (§17.13).

Config note: `MAX_ENTRIES_PER_CYCLE` was found at **1** in `.env` (was 3). At 1 the
scanner opens at most one position per 60s cycle, throttling exactly the slot-fill
that CSM's 4277 signals are meant to saturate. Backtest matched to 1 so the
portfolio figures above reflect it. Flagged for confirmation — if unintended, 3
restores the tested behaviour.

### 17.15 CSM momentum bucket re-test at 4,277 trades — §16.7 resolved — 2026-08-28

> **CORRECTED 2026-08-29 — numbers below are OVERSTATED.** Produced by
> `run_backtest_1m` before the look-ahead leak was fixed (see 17.25). The leak
> flatters long-biased configs; this one was long-heavy. See 17.26 for the
> re-measured figures.

§16.7 tested whether normalized momentum predicts expectancy inside CSM's 4-5x
band, found an inverted U on 1,337 trades (30 symbols), and closed with: *"This is
'not resolved' rather than 'no effect' — 30 symbols gave 1,337 trades and the full
100 gives ~4,200, which could move it either way."*

The §17.14 run produced exactly that sample. Re-tested on **4,277 trades**,
100 symbols, 90d, ladder=on, net of 0.08% round-trip.

| Momentum | N | E[net] | SE | Win% | SUM |
|---|---|---|---|---|---|
| 4.0-4.2x | 1233 | +0.372% | 0.127 | 53.9% | +458.2% |
| 4.2-4.4x | 995 | +0.400% | 0.148 | 54.7% | +398.3% |
| **4.4-4.6x** | 772 | **+0.647%** | 0.178 | 54.5% | +499.6% |
| 4.6-4.8x | 702 | +0.481% | 0.188 | 53.4% | +337.5% |
| **4.8-5.0x** | 575 | **-0.266%** | 0.241 | 48.2% | **-153.0%** |

**The inverted U replicated and sharpened.** The top bucket was +0.387% on 30
symbols; on 100 it is **negative**, and it is the only bucket that loses money.

`correlation(|mom|, net pnl) = -0.0230` — sign-flipped from §16.7's +0.0217 and
still indistinguishable from zero. The `>=4.5x` split that `MIN_STRENGTH=0.50`
would drive is now completely dead: +0.368% vs +0.355%, **t = +0.08** (§16.7 had
t=1.39). **The decision to pin CSM at `strength = 1.0` is confirmed, more strongly
than before.** There is no monotonic signal to rank on.

#### Ranking vs excluding — nested tests (top slice vs the trades it displaces)

| Rule | top slice | rest | diff | t |
|---|---|---|---|---|
| PEAK score, top 25% | +0.600% (1069) | +0.280% (3208) | +0.320% | **+1.81** |
| PEAK score, top 50% | +0.419% (2138) | +0.301% (2139) | +0.119% | +0.79 |
| MONOTONIC, top 25% | +0.099% (1069) | +0.447% (3208) | **-0.348%** | **-1.88** |
| MONOTONIC, top 50% | +0.337% (2138) | +0.383% (2139) | -0.046% | -0.31 |
| **band: >=4.8x vs <4.8x** | -0.266% (575) | +0.457% (3702) | **-0.724%** | **-2.86** |

PEAK score = `max(0, min(1, 1 - |mom - 4.6| / 0.6))`.

**A monotonic `|mom| - 4` ranker is actively harmful (t = -1.88)** — it
systematically selects the one losing bucket. This is the intuitive
"pick the strongest momentum" rule, and it is measurably wrong.

**The peak ranker is only suggestive (t = +1.81)**, and most of its benefit is
simply avoiding the >=4.8x region. Its centre (4.6) is fitted to this same sample,
so it carries overfitting risk that the band cut does not.

**Excluding >=4.8x is the robust result (t = -2.86)** and needs no tiebreaker:

| | current 4.0-5.0x | narrowed 4.0-4.8x |
|---|---|---|
| N | 4277 | 3702 (-575) |
| E[net] | +0.360% | **+0.457%** (+27%) |
| SUM | +1540.5% | **+1693.6%** (+153pp) |

Dropping 575 trades *raises* total return, because those trades collectively lost
153 percentage points. Note this is the rare case that survives the §10 slot-
scarcity rule — the removed candidates have negative expectancy, so losing them
costs nothing.

#### Stability — the shape holds in both halves of the window

| | 4.0-4.4x | 4.4-4.8x | 4.8-5.0x |
|---|---|---|---|
| first 45d | +0.235% (1139) | +0.281% (722) | **-0.206%** (277) |
| second 45d | +0.541% (1089) | +0.844% (752) | **-0.322%** (298) |

The >=4.8x bucket loses money in both sub-periods and 4.4-4.8x is the best bucket
in both. That is a stable shape, not a single-period artifact.

**Status: NOT APPLIED.** Two reasons to validate first: one in-sample 90-day
window, and several tests were run, so t=-2.86 deserves a multiple-comparisons
discount. The clean check is the 130-symbol / 40-day cache — a partly different
symbol set over a different period. If >=4.8x is negative there too, narrow the
band in `cross_sectional_momentum.py` (`4.0 <= normalized_mom < 5.0` -> `< 4.8`).

Caveat on the bucket itself: `>=4.8x` alone is only t = -1.11 against zero. The
significant claim is that it is **worse than the rest of the band** (t = -2.86),
which is the comparison that matters for an exclusion rule.

### 17.16 Candidate ranking is degenerate, and CSM crowds out the ports — 2026-08-28

Signals are ordered by a single key with no tiebreaker
(`live_scanner.py:948`):

```python
candidates.sort(key=lambda s: s.get("strength", 1.0), reverse=True)
```

Two consequences follow, and both were invisible until
`MAX_ENTRIES_PER_CYCLE` was set to 1.

**1. "Best coin" selection does not exist for CSM.** CSM emits a constant
`strength = 1.0` (§16.7, reconfirmed §17.15), so every CSM candidate ties at the
top. Python's sort is stable, so ties retain insertion order — which is watchlist
iteration order. With one entry per cycle the bot therefore takes *the first CSM
signal in watchlist order*, not the best one. The selection is arbitrary, not
selective.

This is harmless in expectancy terms **only because** §17.15 shows there is no
monotonic quality signal to rank on (correlation -0.023, split t=+0.08). Arbitrary
selection among indistinguishable candidates costs nothing. It would matter
immediately if a real ranking key were ever found — see §17.15's band-cut, which
sidesteps ranking entirely.

**2. CSM structurally crowds out NASOS_V4 and ELLIOT_V8.** The two ports emit
`strength = max(0, min(1, (35 - rsi) / 35))`, which is **0.0 whenever RSI > 35**.
Both buy dips at RSI thresholds well above that (`rsi_buy` 72 and 57), so a large
fraction of their signals score exactly zero, and none can exceed CSM's constant
1.0 unless RSI reaches 0. CSM therefore wins essentially every contested cycle.

At `MAX_ENTRIES_PER_CYCLE=3` the ports still filled the remaining slots behind
CSM. At **1**, CSM takes the single slot nearly every cycle, which quietly turns a
three-strategy bot into a CSM-only bot. Profitable — §17.14 measures CSM alone at
+204.96% — but it is not the configuration it appears to be, and the ports stop
contributing diversification.

**Derived from the formulas and the sort, NOT measured.** The port strength
*distribution* has not been sampled. Quantifying the actual slot share across all
three strategies at 1 vs 3 entries per cycle is open work — it is the only way to
price what the throttle costs in strategy mix.

`MIN_STRENGTH=0` (post-revert) does not mitigate this: it removes the *floor*, so
zero-strength port signals become eligible, but they still sort below CSM.

### 17.17 Audit: verified clean, dead code, and two harness traps — 2026-08-28

Recorded so none of it is re-investigated from scratch.

**Verified correct (false alarms, closed):**

- *Position management is not scan-gated.* `if do_scan:` is a one-line block
  (`last_scan = time.monotonic()`); loop sections 0-6 sit under the following
  `try:` and run every cycle. Fast cycles refetch open symbols via
  `fetch_active_positions_data`, so SL/TP always evaluate on fresh data.
- *The square-off queue is processed every cycle*, not only on full scans — the
  same indentation question, same answer.
- *`order_engine` P&L sign is applied.* Line 973 computes `abs(...)`, but 974-975
  negate it when `pnl_pct < 0`. Reading 973 alone suggests losses are booked as
  gains; they are not.
- *`base_strategy` imports `data_feed` safely* — a guarded lazy import with a
  `modules.data_feed` -> `data_feed` -> `"UNKNOWN"` fallback chain.
- *Telegram wiring is complete* — all 36 `callback_data` values resolve: `menu_*`
  dispatch through `data in _SUBMENU_KEYBOARD`, and all six `_confirm_keyboard`
  call sites have matching `do:`/`confirm_` handlers.
- *Web wiring is complete* — all five frontend endpoints match FastAPI routes, and
  square-off runs button -> confirm modal -> POST -> queue file -> scanner with
  matching path constants on both sides.
- *All three strategies conform* to the `BaseStrategy.scan/manage` signatures, and
  every `compute_position_size` caller uses the correct keys (`notional`,
  `margin_req`, `contracts`, `risk_usdt`).
- *Sizing rejects all edge cases without raising*: zero/negative equity, zero entry
  price, 0% / 0.1% / 50% stops, unknown strategy id.

**Dead code found, NOT deleted (awaiting decision):**

- `modules/strategies/freqtrade_port_sma.py` — zero importers; the factory
  docstring already claims it was removed (§16.10).
- `list_optimizer.py` (repo root) — stale duplicate of
  `modules/list_optimizer.py`; only the latter is imported.
- `modules/strategies/freqtrade_port_ichi.py` — reachable only from
  `backtest_freqtrade_ports.py`, and it computes `tenkan_sen`/`kijun_sen` then
  never uses them.
- Unused functions: `_back_keyboard` (Back buttons are built inline, so the UI is
  unaffected), `reset_day_start`, `reset_week_start`, `fetch_multi_timeframe`,
  `_query_order_status`, `get_btc_symbol`, `clear_symbol`, `clear_all`,
  `get_status`.

`trend_pullback.py`, `funding_fade_v2.py` and `grid_strategy.py` look dead but are
genuinely referenced by the harness and factory — leave them.

**Two harness traps that silently produce wrong output:**

1. **`--cache` is reused without validation.** After the §17.8 zero-trade run, the
   re-run loaded the empty cache and reported 0 trades again, having done no work.
   It does print `! cache has no trades` — easy to miss in a long log. Delete the
   cache file when re-running after any code change.
2. **stdout is block-buffered under `nohup`/redirect.** stderr appears immediately
   but results do not, so a healthy run looks hung at a few hundred bytes for
   minutes. Use `python -u`. A run was misdiagnosed as stalled before the process
   table showed it at 76s CPU and climbing.

**`.env` audit:** 34 keys, every one read via `os.getenv`/`environ` (checked
against real reads, not text matches) — no orphans, no malformed lines, no value
corrupted by an inline comment, all 16 numerics parse. Fixed: `ML_PHASE`,
`ML_SHADOW` and `LOG_LEVEL` were each defined twice (identical values), a stale
comment reading `0.50 = current` sat directly above `MIN_STRENGTH=0`, and ten
trailing blank lines.

**Unresolved:** the `2026-08-22` date stamps in `.env` and §17 headers are
probably wrong — file mtimes, `strategy_overrides.json` (08-24) and the loss
tracker all point to the revert landing on 08-27. Left as-is pending
confirmation rather than rewriting the record.

### 17.18 First live paper trades audited — ROI verified, two findings — 2026-08-28

First trades after the §17.12 re-enable. 5 open, 2 closed, session +$1.00
(+1.00%). Data pulled from the server (`data/`, `logs/`) and reconciled against
the Telegram notifications and the web dashboard.

#### ROI arithmetic is correct

Recomputed from entry/exit/notional/leverage:

| | HYPEUSDT (SL_HIT) | ENAUSDT (TP_HIT) |
|---|---|---|
| Entry -> Exit | 86.4380 -> 83.8430 | 0.1685 -> 0.1853 |
| Price move | -3.0022% | +9.9703% |
| x leverage | -9.006% (3x) | +19.941% (2x) |
| Net P&L | -1.0266 | +2.0279 |
| ROI = net / margin | -9.24% (shown -9.11%) | +19.77% (shown +19.66%) |
| Equity impact | -1.016% | +2.008% |

Residuals are display rounding of notional. **ROI follows the Binance convention:
net P&L / INITIAL MARGIN** — not the price move, and not the equity impact. Three
different percentages describe one trade and they are all correct:

```
HYPEUSDT:  -3.00% price   |   -9.11% ROI   |   -0.99% of account
```

Position sizing verified across all 5 open positions: `risk_usdt` is **exactly
$1.00 (1% of equity) on every trade**, and leverage matches
`min(GLOBAL_LEVERAGE=5, floor(MAX_LEVERAGED_LOSS_PCT=0.10 / sl_pct))` in every
case — 8.00% stops -> 1x, 4.87% -> 2x, 3.00% -> 3x.

#### CSM's private 3% stop floor drives the 3x / -9% pattern

Three CSM positions showed stops of *exactly* 3.00%. The cause is a floor inside
the strategy, distinct from `risk_engine.MIN_SL_PCT` (0.015):

```python
# cross_sectional_momentum.py:20
MIN_SL_PCT = 0.03
sl_dist = max(raw_sl_dist, min_sl_dist)    # line 134
```

Any CSM signal whose 2xATR stop is tighter than 3% is widened to exactly 3%. That
cascades deterministically: **3% stop -> floor(0.10/0.03) = 3x leverage -> a
stop-out always displays as ~-9% ROI.** This is the §11.4 floor still doing its
job, and it is the mechanism behind the "why is my 3% stop showing -9%" question.
The equity impact remains the intended 1%, so nothing is mis-sized — the -9% is a
leverage display artifact, not an account loss.

#### The dashboard's "0% win rate" is a period boundary, not performance

`data/paper_equity.json`: 2 trades, 1 win, `realized_pnl_usdt` +1.0046,
`period_start` `2026-08-28T01:28:17Z` (06:58:17 IST). ENAUSDT closed at
**06:58:15 IST — two seconds before that boundary** (the bot restarted at that
moment), so it falls outside the "current equity period" window while remaining in
the all-time total.

Result: the Performance tab showed 1 trade / 0% win / PF 0.00 / -$1.01 directly
above an all-time line reading 385 trades / +$16.63. Both were correct; the
framing invited the wrong reading.

**FIXED.** `_stats()` now returns `sample_ok = n >= MIN_SAMPLE_FOR_RATIOS` (5),
and the dashboard renders an em dash instead of a number for the three stats that
are ratios implying a distribution — **win rate, profit factor, top-trade share** —
in the metric cards and in both the per-strategy and per-symbol tables. Counts and
sums (trades, net, avg win/loss, expectancy) are literal facts about the sample and
always render.

The suppressed cells use a new `.text-muted` class, deliberately un-emphasised:
a red or bold dash still reads as a verdict, which is the misreading being fixed.
Each carries a `title` of "Not shown: N trades is too small a sample".

Verified in-browser against the real 1-trade period: cards and both tables render
`—` in muted slate with the tooltip, `trades = 1` and `net = -$1.01` still render
normally, the all-time scope line is unaffected, and the console is clean. At 6
synthetic trades `sample_ok` flips true and real numbers return.

Touched: `web_server.py` (`MIN_SAMPLE_FOR_RATIOS`, `sample_ok`), `static/app.js`
(`ratio`/`ratioPf`/`ratioCls`/`sampleHint` helpers, `set()` gained a tooltip
argument), `static/style.css` (`.text-muted`).

#### BUG FIXED: every exit notification reported "Duration: 0.0 min"

`notify_exit()` and `logger.log_exit()` both read `pos["duration_min"]`, but
`_inject_duration_min()` — the only thing that writes it — was called *after* them
on all six close paths (`live_scanner.py` lines 656-661, 743-747, 789-793, plus
the square-off and reconcile paths).

Symptom: Telegram/Discord reported `Duration: 0.0 min` on every exit while the web
ledger showed the true values (405.4m for HYPEUSDT, 375.4m for ENAUSDT), because
`live_logger.log_exit()` recomputes duration internally rather than reading the
field.

Fixed by moving `_inject_duration_min()` ahead of `log_exit`/`notify_exit` at all
six sites — it depends only on `entry_time`/`exit_time`, both written by
`close_position()` before any of them. Verified it reproduces 405.4 exactly, with
the malformed-input path still defaulting safely to 0.0.

Note this is the *same class* of bug as §11.9 and §17.7: a value that silently
stays at its default because the code that populates it runs too late, with an
`except Exception` nearby to absorb any complaint. ML outcome records were already
protected (`ml_log_outcome` ran after the injection); only the notifications and
the exit log were affected.

### 17.19 Port max-hold A/B sweep — no improvement — 2026-08-29

Tested `PORT_MAX_HOLD_MIN` = 0 (off), 240, 480, 720 for NASOS_V4 and ELLIOT_V8,
90d/100 symbols. The question: does force-closing flat trades to free slots improve
portfolio returns?

| Hold limit | Trades taken | Portfolio return | Max DD | Sharpe |
|---|---|---|---|---|
| OFF | 118 | -17.05% | 21.7% | -8.21 |
| 240m | 96 | -17.27% | 20.0% | -9.02 |
| 480m | 93 | -17.05% | 20.9% | -8.60 |

The time exit fired 236 times (134 ELLIOT + 102 NASOS) in the 240m arm but portfolio
return barely moved (-0.22pp). Both strategies went from near-zero to slightly
negative expectancy. The freed slots filled with equally mediocre replacement trades.

**Decision: leave PORT_MAX_HOLD_MIN=0 (off).** Code remains in place for future use.

### 17.20 Regime-gated ports — RANGING blocked — 2026-08-29

Per-regime breakdown from the baseline port run revealed the source of port losses:

| Strategy | BEAR_TREND | BULL_TREND | RANGING |
|---|---|---|---|
| NASOS_V4 | +0.411% (166) | +0.600% (160) | **-0.206% (549)** |
| ELLIOT_V8 | +0.395% (181) | +0.359% (194) | **-0.235% (669)** |

Both ports are profitable in trends but lose in RANGING, which has 3-4x more trades
and drags overall expectancy negative. Ran a confirmation backtest with
`BT_REGIME_GATE="RANGING:NASOS_V4,ELLIOT_V8"`:

| Metric | Baseline (all regimes) | Trend-only |
|---|---|---|
| NASOS_V4 E[net] | +0.059% (875) | **+0.763%** (160) |
| ELLIOT_V8 E[net] | -0.015% (1044) | **+0.525%** (189) |
| Portfolio return | -17.05% | **+38.54%** |
| Max DD | 21.7% | 8.5% |
| Sharpe | -8.21 | **4.98** |
| Weekly consistency | — | 11/14 and 12/14 positive |
| Robustness | fragile/no edge | **robust — broad edge** (both) |

**Decision: applied.** `REGIME_STRATEGY_PERMISSIONS` updated:

```
BULL_TREND:  CSM=False  NASOS=True   ELLIOT=True
BEAR_TREND:  CSM=False  NASOS=True   ELLIOT=True
RANGING:     CSM=True   NASOS=False  ELLIOT=False
OVERSOLD:    CSM=True   NASOS=False  ELLIOT=False
OVERHEATED:  (empty — nothing trades)
```

CSM also disabled in BULL_TREND: only +0.023% E[net] there (701 trades) vs ports
at +0.5-1.0%. Each strategy now trades only where it has measured edge.

### 17.21 CSM regime performance — confirmed range specialist — 2026-08-29

> **CORRECTED 2026-08-29 — numbers below are OVERSTATED.** Produced by
> `run_backtest_1m` before the look-ahead leak was fixed (see 17.25). The leak
> flatters long-biased configs; this one was long-heavy. See 17.26 for the
> re-measured figures.

90d/100 symbol CSM retest (ladder=on, 8h hold):

| Regime | N | E[net] | SUM |
|---|---|---|---|
| RANGING | 1379 | **+0.627%** | +864.6% |
| BEAR_TREND | 433 | +0.137% | +59.3% |
| BULL_TREND | 701 | +0.023% | +16.2% |

Overall: 2513 trades, +0.374% E[net], PF 1.29, portfolio +36.08%, Sharpe 2.34.
CSM is a range specialist — 92% of its SUM comes from RANGING. Confirmed the
BEAR_TREND=False and BULL_TREND=False decisions are correct.

### 17.22 Regime age gate reduced 5→1 min — 2026-08-29

`MIN_REGIME_AGE_MINUTES` reduced from 5 to 1. The backtest showed trades entering
within 60 minutes of a regime flip are the best-performing window (+0.745%, t=+2.47),
but the harness cannot resolve ages below ~44 minutes (STEP=60). The 5-minute gate
was blocking a window the data is silent on.

The regime engine already prevents false flips via TREND_CONFIRM_BARS=2 and
MIN_HOLD_MINUTES=15. The 1-minute gate absorbs scan-cycle timing jitter only.

### 17.23 New listing age filter — MIN_COIN_AGE_DAYS=90 — 2026-08-29

DOSUSDT (listed 2026-08-11, 18 days old) entered an open position despite having
chaotic new-listing price action. New listings have inflated ATR, unreliable
indicators, and no backtest coverage (the backtest used 90 days of data, so every
coin was 90+ days old).

Filter added in `watchlist.py` `_fetch_crypto_symbol_set()`: uses Binance
`exchangeInfo` `onboardDate` field to exclude coins younger than 90 days from
the watchlist entirely. No extra API calls needed — exchangeInfo was already fetched.

Configurable via `MIN_COIN_AGE_DAYS` env var (default 90). Verified: current
150-coin watchlist has zero coins under 90 days (youngest is OPGUSDT at 128 days).
DOSUSDT would be filtered on next watchlist refresh.

### 17.24 Dashboard ROI display aligned with Binance — 2026-08-29

Dashboard showed `pnl_equity_pct` (P&L / account equity) for open positions.
Binance shows ROI = P&L / initial margin. Example: DEXEUSDT at -$0.25 with $7.94
margin showed -0.39% on dashboard vs -3.25% on Binance.

Added `roi_pct = pnl_usdt / margin_req` in `_positions_view()`. Frontend updated
to display ROI. Column header changed to "PNL(ROI %)" to match Binance.

### 17.25 LOOK-AHEAD LEAK IN `run_backtest_1m` — FIXED — 2026-08-29

`run_backtest_1m` sliced `df_1h[df_1h.index <= now]` and the same for `df_15m`.
Both frames are resampled from the FULL 1m series, so a bar is LABELLED by its
open time but CONTAINS the whole interval. At `now = 10:00` that admits the
10:00 hourly bar — up to 59 minutes of future price. The 15m frame is worse:
CSM takes its ENTRY PRICE and its SL/TP ATR from that frame's last close.

`run_backtest()` (the 15m path) was fixed for exactly this on 2026-08-12 via
`_hourly_as_of()` and has a `BT_LOOKAHEAD_1H` switch. **The 1m path never was,
and had no switch.** It is the runner behind `run_freqtrade_backtest.py`, so it
produced most of the CSM figures in this log.

**The bias has a SIGN, and it follows trade direction.** The leaked entry price
sits slightly into the future of the move that triggered the signal. Momentum
continues on average, so that price is BETTER for a long and WORSE for a short.

```
BASELINE config (69% LONG)  -- leak FLATTERS
  leaked  1154 tr  WIN 54.1%  E +0.789%  PF 1.56
  strict  1156 tr  WIN 50.6%  E +0.367%  PF 1.22     2.1x overstated

CONFIG A (100% SHORT)       -- leak PENALISES
  leaked   847 tr  WIN 63.9%  E -0.161%  PF 0.87
  strict   851 tr  WIN 62.4%  E +0.033%  PF 1.03     understated
```

Trade counts barely move — it was not changing WHICH trades fired, only the
fill they got. `BT_LOOKAHEAD_1H=true` restores the old behaviour so historical
runs stay reproducible. It is not a tuning knob.

NASOS_V4 reads `df_1h` only via `.iloc[-4:-1]` and ELLIOT_V8 not at all, so the
port numbers in §14 and §17 are unaffected.

---

### 17.26 §17.21 CORRECTED — CSM had no standalone edge — 2026-08-29

§17.21 was measured on the leaked runner with a long-heavy config. Re-run on
the fixed harness, 100 symbols / 90d, same baseline config, no regime gate:

| Regime | §17.21 as recorded | Corrected |
|---|---|---|
| RANGING | 1379 · **+0.627%** · +864.6% | 2414 · **+0.107%** · +258.5% |
| BEAR_TREND | 433 · +0.137% · +59.3% | 812 · **−0.431%** · −350.2% |
| BULL_TREND | 701 · +0.023% · +16.2% | 1117 · −0.061% · −68.6% |
| **overall** | 2513 · **+0.374%** · PF 1.29 | 4343 · **−0.037%** · **PF 0.98** |

**The qualitative conclusion survives** — RANGING is still the best regime and
the only positive one, so "CSM is a range specialist" and the RANGING-only gate
are both correct. **The magnitude does not.** Baseline CSM had *no standalone
edge*: PF 0.98. Every claim in this log that treats pre-2026-08-29 CSM as a
working strategy rests on the leak. §17.13 and §17.15 have the same problem.

---

### 17.27 CSM: 2,744-configuration sweep for a 75% win rate — 2026-08-29

Goal: a 75% win rate. Constraint added at the outset, because win rate alone is
trivially reachable and loses money — at 75% WIN the break-even R:R is exactly
0.333, so the objective was **WIN >= 75% AND positive expectancy**.

Method: a fast simulator over cached 1m arrays (~1.1s per 100-symbol config vs
~370s for the harness), validated against the strict harness on the baseline
config — 1150 vs 1156 trades, +0.394% vs +0.367%, and BTCUSDT reproduced
trade-for-trade. Train = first 45d, test = last 45d.

```
stage 1   200 configs   TP x SL x max-hold        ->  0 hits
stage 2   600 configs   10 ladder designs         ->  0 hits
stage 3  1944 configs   band x dir x regime       -> 83 hits, ALL short+RANGING
```

**Stage 1** reached 87.3% WIN at -0.108%/trade (TP 0.5 / SL 4.0). Win rate and
expectancy were strictly anti-correlated across the whole grid.

**Stage 2** is the useful one: the ladder prices win rate almost exactly fairly.
At 74.5% WIN the break-even R:R is 0.342 and the best config delivered **0.34**;
at 73.3% it needed 0.364 and delivered 0.37. Every point of win rate is paid for
in R:R to two decimal places. There is no free lunch in the exit logic.

**Stage 3** — only changing WHICH TRADES EXIST moved the frontier. Not one
long-side or trend-regime configuration qualified at any parameter setting.

---

### 17.28 THE SIMULATOR WAS RIGHT UNTIL IT WASN'T — 2026-08-29

The winning config (A) was validated in the sweep at **78.4% WIN / +0.247% /
PF 1.22**, stable across train/test, three sub-periods, and the parameter
neighbourhood. Verified against the real `CrossSectionalMomentum` class on the
leak-free harness it came back **66.3% WIN / +0.141% / PF 1.12**.

Cause: the simulator manufactured 863 sub-0.4% ladder locks the production
ladder does not produce (989 ladder exits vs 109; 52 MAX_HOLD vs 404). The
production ladder itself is correct — unit-tested, it arms and locks exactly as
designed in both frame layouts.

**The validation was the failure.** The simulator was checked against the
BASELINE config, where it matched to 0.5% — and then used to search configs
whose exit mechanics it had changed. Config B, which has no ratchet at all,
reproduced to three decimals (645 -> 644 trades, +1.230% -> +1.229%). The
simulator diverged *only* where ladder/trail mechanics were involved, which is
precisely where the search was looking.

**Lesson: a simulator validated on config X is not validated for config Y when
Y changes the mechanics X exercised. Re-confirm every candidate against the
real class before believing it.**

---

### 17.29 CONFIG A vs CONFIG B, AND THE 30% TAX — 2026-08-29

Four arms, production code, leak-free, 100 symbols / 90d, 0.08% fees +
0.03%/side slippage:

```
arm               N     WIN%   R:R    PF   E[net]   after-tax   streak
BASELINE       2698    50.5%  1.03  1.05  +0.091%    -0.433%       13
CONFIG_A       1817    66.3%  0.57  1.12  +0.141%    -0.239%       11   <- deployed
CONFIG_B        644    46.1%  1.68  1.44  +1.229%    +0.026%       21
CONFIG_B_trail  691    37.8%  2.26  1.37  +0.739%    -0.078%       12
```

**`after-tax` assumes India 115BBH: 30% on gains, NO loss set-off.** Under that
rule the break-even condition is:

```
0.7 * p * W > (1-p) * L     <=>     PF > 1/0.7 = 1.429
```

**Win rate does not appear in that equation.** Config A wins 66% of its trades
and is after-tax NEGATIVE, because R:R 0.57 makes its win pile only 1.12x its
loss pile. Per 100 trades: gross wins +126.6%, gross losses -112.6%, pre-tax
+14.1% — and the tax takes 38.0 points out of the win pile. Config B wins less
than half the time and survives because its win pile is 1.44x its losses.

**Of all 2,744 configurations tested, exactly 2 clear PF 1.429.** Config B is
one of them, at 1.44 — a 1.8% margin.

`CONFIG_B_trail` is Config B with the legacy breakeven + 2xATR trail switched
on. It costs **0.49%/trade**. The edge is a fat right tail; anything that cuts
winners short removes what pays for a 54% loss rate.

**Config A is deployed anyway**, chosen for tolerance not performance: max
losing streak 11 vs 21, WIN 66% vs 46%. Config B dominates it under BOTH tax
readings and is reachable entirely via `.env` with no code change:
`CSM_MOM_LO=3.0 CSM_MOM_HI=4.0 CSM_SL_ATR_SHORT=2.25 CSM_PROFIT_LADDER=off
CSM_LEGACY_BE=false`.

**Open question that outranks any further tuning:** whether crypto perps fall
under 115BBH at all, or under speculative-business treatment where losses CAN
be set off. Unsettled in Indian law, no CBDT guidance. The two answers point in
opposite directions — one demands PF > 1.43, the other just wants expectancy.
(Confirmed separately: 194S 1% TDS does NOT apply to futures — no VDA transfer
occurs. Verify all of this with a CA; it is not tax advice.)

Config A by regime, gate removed (leak-free, production code):

| Regime | N | WIN% | PF | E[net] | SUM |
|---|---|---|---|---|---|
| RANGING | 1648 | 66.6% | 1.17 | **+0.186%** | +306.6% |
| BULL_TREND | 301 | 67.8% | 1.14 | +0.165% | +49.6% |
| BEAR_TREND | 1243 | 60.7% | 0.70 | **-0.360%** | -447.8% |

BEAR_TREND is correctly blocked — leaving it open costs -447.8%. BULL_TREND is
positive and currently blocked; n=301 is thin and PF 1.14 is below the
after-tax threshold, so it adds pre-tax return and nothing after tax. Ungated
Config A is -0.029%: **the regime gate is what makes it work.**

---

### 17.30 LIVE ENGINE BUGS FOUND IN THE SAME AUDIT — 2026-08-29

Four defects in the live path, all fixed and committed.

**1. The freqtrade ports had no stop-loss.** Neither `freqtrade_port_nasos.py`
nor `freqtrade_port_elliot.py` reads `sl_price` or `tp_price` anywhere in
`manage()`. Their 8% stop and ATR target existed only as numbers on the
position dict. A position 40% under water — five times past its stop —
returned `{'exit': False}` from both. In LIVE the exchange STOP_MARKET capped
the loss; in PAPER there was no stop at all, and the TARGET was unenforced in
both modes. Measured, same data, stop enforcement the only variable:

```
NASOS_V4   with stop  147 trades   3.0h median   worst  -8.08%
           without     17 trades   414h median   worst -87.96%   100% END_OF_DATA
ELLIOT_V8  worst trade  -8.08%  ->  -48.17%
```

NASOS stopped being a strategy and became buy-and-hold. ELLIOT kept trading,
which hid the defect while its loss tail grew six-fold. Fixed by
`_check_hard_levels()` in `live_scanner.py`, applied to EVERY strategy before
its own `manage()` — a strategy can no longer forget to check a stop it owns.
Fills at the level and lets the stop win a tie, matching the exchange
STOP_MARKET, `_manage_on_bar_close()` and the harness `_intrabar_exit()`.

**2. Port positions were unmanaged on fast cycles.**
`fetch_active_positions_data` fetched `limit=100` 1m bars; both ports need
>=275 (they bail on `len(df_5m) < 55`). At `FAST_INTERVAL_SECONDS=1` that is
thousands of no-op calls per hour. Also failed on full cycles in RANGING, where
`_depth_1m` came from *permitted* strategies only (CSM, depth 200). Fixed:
depth 400, and depth/1h derived from strategies HOLDING a position too.

**3. Scan/manage deadlock.** `do_scan = due_for_scan and slots_free` gated BOTH
"skip scanning" and "fetch shallow data". Once every slot filled, only fast
cycles ran, so the ports could never produce an exit, so nothing closed, so no
slot freed. Fixed by keying `is_fast` on the scan clock alone; when slots are
full the full-depth fetch narrows to open symbols instead of being skipped.

**4. Weekly loss cap bypassed after midnight UTC.** `is_loss_cap_hit()`
early-returned `False` whenever the stored day was not today — *before* the
weekly test. Reproduced: `week_pnl = -12.00%` against a -10% cap returned
`(False, '')` when breached on any prior day. The weekly cap only held on the
calendar day it tripped.

Also fixed: CSM's `position.get("atr", trail_px * 0.01)` returned 0 rather than
the fallback when the stored ATR was 0, collapsing the trail onto current price.

**Stale fixture found:** the backtest tree's `regime_engine.py` still had
`BEAR_TREND: {"CSM": True, "LIQ": True}` and listed strategies deleted
2026-08-19 (§9 item 7b). It let 1,243 BEAR_TREND shorts into a RANGING-only
verification and turned +0.247% into -0.048% — which reads as the strategy
failing rather than the fixture being wrong. Synced.

**A git repository now exists** (`e5571dc` = pre-Config-A baseline). Market data
is gitignored; the repo is source only, ~1.2MB.

---

### 17.31 NASOS_V4 Parameter Sweep — 2026-08-29 / 2026-09-11

Goal: find a NASOS_V4 config with PF > 1.429 (India 115BBH after-tax break-even, §17.29), mirroring the CSM sweep in §17.27.

**Method:** 2,880 gated configurations over cached 1m/15m arrays. `risk_gates=True` — every candidate trade checked against `MAX_SL_PCT=0.10` before counting, so numbers reflect what the live engine would actually take (39% of 8×ATR signals would be rejected ungated; gating is required for honest counts). Train/test split at midpoint. Minimum 120 trades total, 40 per half.

Grid:
- SL: flat 3/4/5/6/8/10%, ATR 1.5/2/3/4/6/8×
- TP: 1.5/2/3/4/6× ATR
- MIN_STRENGTH: 0 / 0.3 / 0.4 / 0.5 / 0.6 / 0.7
- MAX_HOLD: off / 240 / 480 / 1440 min
- Regime: all / BULL+BEAR / RANGING / RANGING+BULL

Current live config (flat 8% SL / TP 3× ATR / no max-hold / BULL+BEAR / MINSTR 0.50):
```
162 tr  WIN 75.9%  R:R 0.41  PF 1.30  E +0.587%  aftax -0.177%  sum +95.2%  teE +0.849%
```

**Result: no configuration cleared PF > 1.429 with positive expectancy in both halves.** Mean PF across surviving configs ≈1.09, range 0.79–1.45. The top single-measurement entry (8×ATR SL, PF 1.46) was phase-dependent — it reversed at a different scan-phase anchor (see §17.32). Current live config (PF 1.30, after-tax −0.177%) is the most interpretable baseline and stays unchanged.

**Conclusion:** NASOS has no demonstrated edge under 115BBH at any configuration tested. Phase sensitivity makes any single-measurement improvement unreliable. Do not tune further until ≥200 live trades accumulate; monitor live P&L.

---

### 17.32 Scan-Phase Sensitivity — CSM robust, NASOS noise-dominated — 2026-08-29 / 2026-09-11

**Background:** The harness `range(1500, len(raw), 60)` anchors scan minutes to the data-file start, not wall-clock time. Shifting the start offset by 0–60 minutes gives 7 independent phase anchors (every ~8 min) with identical data. If a strategy's PF is phase-stable, the edge is real; if it swings widely, the single-phase measurement is dominated by noise.

**CSM Config A** (band 3.0–4.0 / LONG+SHORT / RANGING / Hybrid Ladder / 90d / 100 symbols):

| Phase offset | PF   |
|-------------|------|
| +0 min      | 1.57 |
| +8 min      | 1.50 |
| +16 min     | 1.46 |
| +24 min     | 1.42 |
| +32 min     | 1.48 |
| +40 min     | 1.44 |
| +48 min     | 1.39 |

Range **1.39–1.57**, mean **1.50**. All 7 phases positive; 4/7 clear the 1.429 after-tax threshold. **CSM's edge is real, not a scan-phase artifact.**

**NASOS_V4** (flat 8% SL / TP 3× ATR / BULL+BEAR / MINSTR 0.50, same dataset):
Range **0.79–1.45**, mean **1.09**. Two phases go below PF 1.0 (net loser). The 6×ATR stop variant that appeared best (PF ~1.46) was measured only at the most favourable phase anchor; at other offsets it reverted to near or below 1.0. Reverted to flat 8%: same data, same period, different file-start minute → contradictory results. The recommendation to deploy 6×ATR was reversed.

**Rule going forward:** before deploying any new NASOS (or thinly-sampled) config, run across ≥5 scan-phase offsets. A single measurement that looks better than the flat baseline is more likely a phase artifact than a genuine improvement.

---

## 18. FILES TO SYNC TO UBUNTU (as of 2026-08-29) — supersedes §15, superseded by §19

### Production — 2026-08-29 session (highest priority)

| File | Why | Marker |
|---|---|---|
| `modules/regime_engine.py` | Regime permissions: CSM range-only, ports trend-only (§17.20) | `"NASOS_V4": False` in RANGING |
| `modules/watchlist.py` | New listing age filter 90d (§17.23) | `MIN_COIN_AGE_DAYS` |
| `live_scanner.py` | Regime age 5→1 (§17.22), ROI field (§17.24), plus all prior fixes | `MIN_REGIME_AGE_MINUTES = 1` |
| `static/app.js` | ROI display matching Binance (§17.24), prior fixes | `roi_pct` |
| `static/index.html` | Column header PNL(ROI %) (§17.24) | `PNL(ROI %)` |

### Production — prior session (still required if not yet deployed)

| File | Why | Marker |
|---|---|---|
| `modules/risk_engine.py` | `MAX_SL_PCT` (§17.1), loss-tracker logging (§17.7), encoding (§17.6) | `MAX_SL_PCT = max(MIN_SL_PCT` |
| `modules/strategies/cross_sectional_momentum.py` | legacy breakeven `else:` branch (§17.5) | `if PROFIT_LADDER:` |
| `telegram_bot.py` | `.env` read+write encoding (§17.6) | `encoding="utf-8"` |
| `discord_bot.py` | same | `encoding="utf-8"` |
| `requirements.txt` | `aiohttp`, `pyotp`, `qrcode` | `aiohttp` |
| `web_server.py` | settings tab, TOTP, small-sample suppression | `MIN_SAMPLE_FOR_RATIOS` |
| `static/style.css` | `.text-muted` and settings styles | `.text-muted` |
| `dashboard_2fa_setup.py` | TOTP provisioning (root level) | `_ROOT = os.path.dirname` |

### Production — hygiene (file-handle leaks, §17.7)

`modules/blacklist.py`, `modules/whitelist.py`, `modules/strategy_overrides.py`,
`modules/symbol_blacklist.py`, `modules/binance_status.py`,
`modules/trade_analyzer.py` — marker `as _jf:` in each. Low priority.

### Backtest tree (`Tire 1-2 Back Test engine/`) — only if backtests run there

| File | Why |
|---|---|
| `backtest_optimizer.py` | **CSM routing fix (§17.8)** — without it CSM reports 0 trades |
| `modules/risk_engine.py` | `MAX_SL_PCT` port + encoding + leak (§17.11) |
| `live_scanner.py` | same `_regime_changed_at` bug + leak |
| `modules/strategies/cross_sectional_momentum.py` | synced to main |
| `modules/{blacklist,strategy_overrides,symbol_blacklist}.py` | leaks |
| `.env` | kept identical to main |

### Dev machine only — no server need

`backtest_freqtrade_ports.py`, `backtest_freqtrade_strategies.py`,
`fetch_binance_data.py` — the naive-UTC fetch-window fix (§17.7). Zero effect on
a UTC server; matters only where the local clock is not UTC.

### Still stale in the backtest tree, deliberately NOT synced

`modules/regime_engine.py` (deleted VRP/LIQ, CSM gating inverted),
`modules/symbol_filter.py` and `modules/watchlist.py` (no 4% floor,
`REFRESH_HOURS=24`), `modules/data_hub.py` (no `depth_1m`, so NASOS/ELLIOT get
200 bars instead of 1500). See §17.11 — left alone during the §17.13/§17.14
comparison to avoid confounding it. Worth syncing before the next port backtest.

---

## 19. PRODUCTION RELEASE — FILES DEPLOYED TO UBUNTU (2026-09-11) — supersedes §18, superseded by §20

Full production release deployed. All files below were synced to the Ubuntu VPS.

| File | Change | §Ref |
|---|---|---|
| `modules/strategies/cross_sectional_momentum.py` | Hybrid Ladder (bar-close Stage 1 + HWM Stages 2/3), Volume Expansion Filter, HWM entry-candle guard | §0.1, §0.2.1, §0.2.4 |
| `live_scanner.py` | Monotone HWM tracking, `preload_exchange_specs()`, Kronos `log_candidate()`, `_load_positions()` / `reconcile_with_exchange()` | §0.1, §0.2.8, §0.2.9 |
| `modules/auth_manager.py` | `HTTPAdapter(pool_connections=30, pool_maxsize=30)`, `get_session()` | §0.1 |
| `modules/data_hub.py` | `MIN_REQ_GAP=0.030s`, `_BTC_REF_TTL=300s` | §0.2.2, §0.2.3 |
| `modules/order_engine.py` | `exit_source` field stamping (`"bot"` / `"manual"`) | §0.1 |
| `modules/regime_engine.py` | `BEAR_TREND: CSM=True`, `ELLIOT_V8=False` all regimes (§0.2.11 intentional) | §0.2.11 |
| `modules/symbol_filter.py` | Expanded `EXCLUDED_BASES` (tokenized equities, leveraged ETFs, commodity tokens) | §0.1 |
| `modules/ml_engine.py` | Monotone HWM snapshot integration | §0.1 |
| `modules/watchlist.py` | `MIN_COIN_AGE_DAYS=90` default | §17.23 |
| `kronos/scorer.py` | `self._tok.eval()`, `self._mdl.eval()` | §0.2.5 |
| `kronos/requirements.txt` | `safetensors>=0.4.0` added | §0.2.6 |
| `kronos/shadow_worker.py` | Truncation guard, `endTime`, naive UTC timestamps | §0.2.7 |
| `data/strategy_overrides.json` | All three strategies `disabled: false` | §0.2 (item 8 in §17.12) |
| `.env` | `MIN_COIN_AGE_DAYS=90`, `MAX_SL_PCT=0.10`, `CLOSE_ON_SHUTDOWN=false`, `CSM_PROFIT_LADDER=on`, `CSM_ALLOW_LONG=true`, `CSM_ALLOW_SHORT=true` | various |
| `.gitattributes` | `*.sh text eol=lf` | §0.2.10 |
| `requirements.txt` | Removed `cryptography`, organized by group | §0.2.10 |
| `tools/*.py` (12 files) | Relocation-proof: `PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` | §17.30 |
| `tools/replay/overnight.sh` | `$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)` | §17.30 |

**Not deployed (backtest tree only):**
`Tire 1-2 Back Test engine/` — excluded from git per `.gitignore`. Manual sync before next backtest run.

---

## 20. FILES TO DEPLOY TO UBUNTU (2026-09-12) — supersedes §19, superseded by §21

Settings migration (§0.3). Pull the whole tree; the files that matter:

| File | Change | §Ref |
|---|---|---|
| `modules/settings_manager.py` | **NEW** — SPEC, typed hot-reload reader, validated atomic writer, migration, history | §0.3 |
| `live_scanner.py` | `_refresh_settings()` per cycle; config block reads settings; `_tg()` live toggle | §0.3 |
| `modules/risk_engine.py` | Limits are functions; `get_leverage()` from settings; old `.env` reader removed | §0.3 |
| `modules/strategies/cross_sectional_momentum.py` | `CSM_*` read per call | §0.3 |
| `modules/strategies/freqtrade_port_nasos.py` | `NASOS_*`, `PORT_MAX_HOLD_MIN` read per call | §0.3 |
| `modules/ml_engine.py` | `_phase()` / `_shadow()` | §0.3 |
| `modules/watchlist.py` | `MIN_COIN_AGE_DAYS` per fetch | §0.3 |
| `modules/auth_manager.py` | Preflight equity check from settings | §0.3 |
| `web_server.py` | Settings API from the manager; `/api/settings/history` | §0.3 |
| `telegram_bot.py`, `discord_bot.py` | Shared helpers; no `.env` writers; new messages | §0.3 |
| `ngrok_runner.py` | `NGROK_ENABLED` from settings | §0.3 |
| `static/app.js`, `static/index.html`, `static/style.css` | restart pill; Recent Changes table | §0.3 |
| `backtest_optimizer.py`, `tools/llmvalue.py`, `tools/replay/analyze.py`, `tools/fetch_1m.py` | Accessor snapshots | §0.3 |
| `.env.example`, `.gitignore`, `DEPLOY.md`, `PROJECT_BRIEF.md` | Docs; ignore `data/settings.json`, `data/settings_history.jsonl` | §0.3 |

**Generated on first start (do not copy from dev):** `data/settings.json`, `data/settings_history.jsonl`.

**`.env` on the box:** unchanged. Migrated keys may be deleted from it later (cosmetic).

---

## 21. FILES TO DEPLOY TO UBUNTU (2026-09-14) — supersedes §20

Commits `52b2a1a` → `9be4a50` (§0.4, §0.5). Pull the whole tree and **restart all four services** — the strategy factory, notifiers and Kronos queue are not hot-reloaded:

```
cd /home/psms/ubuntu/program_files/csb && git pull && sudo systemctl restart csb csb-bot csb-discord csb-web
```

| File | Change | §Ref |
|---|---|---|
| `modules/order_engine.py` | Reconcile: no close without a fill; `None` on failed read; exchange-stop classification | §0.5.1 |
| `modules/rate_budget.py` (**NEW**), `modules/auth_manager.py` | Weight-aware throttle on `SESSION.request` | §0.5.2 |
| `live_scanner.py` | `_kronos_gate()`, entry/exit context capture, MFE/MAE, `_current_regime`, NASOS shadow queue | §0.4, §0.5.3, §0.5.5 |
| `modules/settings_manager.py` | `KRONOS_GATE`, `KRONOS_PF_THR`, `KRONOS_GATE_WAIT_SEC`; default caps `CSM:3,NASOS_V4:3` | §0.4, §0.5.7 |
| `kronos/shadow_client.py`, `kronos/progress.py`, `kronos/enrich_ablation.py`, `kronos/whale_analyze.py` | Per-strategy queue/progress/ablation/whale | §0.5.5, §0.5.8 |
| `live_logger.py`, `modules/ml_engine.py` | Context keys on EXIT records and outcome rows | §0.5.3 |
| `telegram_notifier.py`, `discord_notifier.py`, `telegram_bot.py`, `discord_bot.py` | Kronos/regime lines; ELLIOT removed; dead handlers removed | §0.5.3, §0.5.7, §0.5.9 |
| `web_server.py`, `static/*` | `/api/kronos/progress`, Kronos/Whale panel, Kronos + Regime columns | §0.4, §0.5.8 |
| `kronos/shadow_worker.py`, `kronos/progress.py`, `kronos/kronos-shadow.service`, `kronos/scorer.py` | Threshold removed from the worker; would-pass derived at the live threshold; poll 2 s; weights pinned; **copy the unit + daemon-reload + restart kronos-shadow** | §0.5.14–16 |
| `modules/regime_engine.py`, `modules/risk_engine.py`, `modules/strategies/strategy_factory.py` | ELLIOT removed; SMA port deleted | §0.5.7, §0.5.9 |
| `modules/strategies/base_strategy.py`, `modules/strategies/freqtrade_port_nasos.py` | `hold_minutes()` added (crash fix behind `PORT_MAX_HOLD_MIN`); dead leverage key; `_to_5m()` | §0.5.11 |
| **Deleted:** `modules/strategies/freqtrade_port_elliot.py`, `modules/strategies/freqtrade_port_sma.py` | | §0.5.7, §0.5.9 |

**Post-restart checks:** journal shows strategies `['CSM', 'NASOS_V4']`, gate `live / 0.025 / 8s`, scan 120s / fast 1s; `curl localhost:8102/api/kronos/progress` returns CSM and NASOS_V4 blocks. On-box `settings.json` may still carry `ELLIOT_V8:0` in `MAX_PER_STRATEGY` — ignored; drop it on the next cap edit.

---

## 9. OPEN WORK, ranked

*Updated 2026-09-12.*

### Critical — do before any tuning

1. ~~**Run 30-day backtest with §11 changes.**~~ Done (§13.1, §13.4).
2. ~~**Strategy evaluation via Tire 2 backtest.**~~ Done (§14.2). EI3/VRP/LIQ removed.
3. ~~**BLOCKER: clear `data/strategy_overrides.json`.**~~ Done 2026-09-11 (§0.2 audit).
4. **Accumulate ≥100 live trades with no config changes.** Bot is LIVE as of 2026-09-11.
   CSM (RANGING only) and NASOS_V4 (trends only) are the active producers. ELLIOT_V8 is
   benched. No parameter changes until this completes.
5. **Resolve crypto-F&O tax classification with a CA** (§17.29). India 115BBH (30% on gains,
   no loss set-off) gives PF > 1.429 as break-even. Config A (deployed, PF 1.12) is
   after-tax NEGATIVE under that reading. Config B (PF 1.44) is the only backtested config
   that clears it. Outranks every other item. Note: 194S 1% TDS does NOT apply to USDM
   futures — no VDA transfer occurs. Still need a CA on 115BBH vs speculative-business.

### Strategy decisions (after live baseline)

6. **Decide on Config B vs Config A** (§17.29). Config B is measurably better under both tax
   readings. Reachable via .env only: CSM_MOM_LO=4.0 CSM_MOM_HI=5.0 CSM_PROFIT_LADDER=off
   CSM_LEGACY_BE=false. Config A chosen for lower losing streak (11 vs 21). Switch after
   tax question resolved and >= 100 live trades logged.
7. **Validate CSM >=4.8x band cut out-of-sample** (§17.15). t=-2.86 in-sample, negative
   in both sub-periods, worth +153pp. Test on 130-symbol 40-day cache first.
8. **Decide on CSM in BULL_TREND** (§17.29). Positive at +0.165% / PF 1.14 but n=301 and
   below after-tax PF threshold. Currently blocked. One line in regime_engine.py.
9. **ELLIOT_V8 reinstatement** (§0.2). Currently benched in all regimes. Re-enable in
   BULL_TREND + BEAR_TREND after >= 100 live trades available for independent validation.
   Backtest baseline: PF ~1.01, after-tax negative under 115BBH.
10. **Measure slot share across strategies** (§17.16). CSM strength=1.0 outranks ports'
    RSI-derived score at MAX_ENTRIES_PER_CYCLE=1. Never measured — quantify before
    changing MAX_ENTRIES_PER_CYCLE.
11. **NASOS_V4 — accumulate >= 200 live trades** before any parameter changes.

### Backtest / harness

12. **Re-run anything from run_backtest_1m before 2026-08-29** (§17.25). §17.13, §17.15,
    §17.21 carry correction markers. Any pre-2026-08-29 port figure predates MAX_SL_PCT
    port into the BT engine (§17.11).
13. ~~**Add symbol cooldowns to backtest.**~~ Done (§17.9).
14. ~~**Sync backtest tree regime_engine.py.**~~ Done 2026-08-29 (§17.30).
15. **Backtest the universe sort key** (§17.10). sqrt(volume) x range reaches 12.43% median
    range at unchanged liquidity. Hypothesis — must be measured.
16. **Close or quantify the 60x entry-cadence gap** (§17.9). Live: every 60s. Harness: every
    60 min. Cannot be fixed in the current harness.
17. **Add slippage parameter** to backtest from one shared .env value.

### Infrastructure / hygiene

18. **Bump fetch_btc_reference 50 → 200 bars** so ADX warm-up converges.
19. ~~**ML_PHASE=2 active**~~ (was: Phase 2 unlocks at 50 trades). Phase 3 target: 500 live trades.
20. ~~**Decide on dead code** (§17.17)~~ — done 2026-09-14 (§0.5.9, §0.5.12, §0.5.13): all listed files and functions deleted.

## 10. HARD-WON LESSONS

**A simulator validated on one config is not validated for another.** The fast
sweep harness matched the real class to 0.5% on the BASELINE config and
reproduced BTCUSDT trade-for-trade — then over-stated Config A by 12 points of
win rate, because the search had changed the very exit mechanics the validation
exercised (17.28). Config B, which removes those mechanics entirely, reproduced
to three decimals. Re-confirm every candidate against the real class.

**Win rate is not a target, it is a consequence.** Across 2,744 configurations
every route to a higher win rate was paid for in R:R at close to the fair rate:
at 74.5% WIN the break-even R:R is 0.342 and the best config delivered 0.34
(17.27). Chasing win rate produced a config that wins two trades in three and
is negative after tax.

**Under a no-loss-offset tax, only profit factor matters.** 30% on gains with no
set-off makes the break-even condition PF > 1/0.7 = 1.429 — win rate does not
appear in the equation at all (17.29).

**A measurement that says "don't" only counts if you don't.** The 5% exit guard
(§17.2) was backtested BEFORE deployment. The backtest said it turned two of three
strategies negative. It shipped anyway, killed live trades exactly as predicted,
and was reverted days later. The measurement was not the failure; acting against
it was.

**"0 trades" is a claim about the harness, not the strategy.** CSM reported zero
trades across 90 days and 100 symbols while its `scan()` produced 2,798 signals on
the same data (§17.8). One `>` that should have been `>=` routed it to a replay
path whose data files do not exist. Before concluding a strategy is dead, call it
directly and count signals.

**Reverting a feature is not the same as disabling it.** Emptying the CSM profit
ladder removed the breakeven AND the trail, because `be_hit` was only ever set
inside the ladder loop (§17.5). The "revert" would have left every position with
no stop management at all. When removing a feature, check what else reads the
state it wrote.

**A Python global assigned in a function without `global` is a silent no-op.**
`_regime_changed_at` (§17.7) bound a local for months; the regime-age gate never
fired after a regime change and ML trained on process uptime. Nothing raised. An
AST sweep for the pattern takes seconds and should be routine.

**Encoding defaults are a live trading bug, not a nit.** An unencoded `open()` on
a `.env` containing one emoji raised `UnicodeDecodeError` on every entry, and the
matching write path would have truncated the file to zero bytes (§17.6).
`except OSError` does not catch it.

**Config can be ahead of code, and one combination fails unsafely.** The server's
`.env` carried `CSM_PROFIT_LADDER=off` before the code that gives `off` a legacy
path existed. Most stale keys are simply ignored; this one would have silently
removed all stop management.

**Check runtime state, not just config and code.** Three strategies sat
`disabled: true` in a JSON file for four days (§17.12). `.env` was correct, the
code was correct, and the bot could not open a position in any regime.

**Win rate without R:R is meaningless.** CSM wins 57.3% of BEAR_TREND trades and
still loses 0.243% per trade there (§17.13).

**A constant score is not a neutral score.** CSM's `strength = 1.0` was chosen
because momentum is not predictive (§16.7). But `strength` is also the *sort* key,
so a constant maximum silently outranks every other strategy's varying score, and
ties among CSM's own signals fall back to watchlist order (§17.16). A value picked
for one purpose became a ranking decision nobody made.

**Delete the cache before re-running a backtest.** A stale `--cache` file made a
fixed harness report the same zero-trade result as the broken one, having done no
work (§17.17). The warning is printed and easy to miss.

**Three percentages describe one trade; say which one you mean.** A 3% stop at 3x
shows -9% ROI and costs 1% of the account (§17.18). Every one of those numbers is
correct. Most confusion about "why did I lose 9% when my stop was 3%" is this,
not a bug.

**Slot scarcity dominates.** 3 slots against 11,363 slot-rejected signals means *any*
filter that removes candidates loses money, even when it raises per-trade quality. This
killed blacklisting, hour-filtering, and adding LIQ/VRP. Check trade count before
celebrating an expectancy improvement.

**Short windows lie.** The 15-day window produced a permission matrix that scored worst of
four. Do not derive configuration from under 30 days; prefer 60–90 for strategy decisions.

**Distinguish measurement changes from behaviour changes.** Most of 2026-08-11 changed
what the *reports* said, not what the bot did. The backtest never "got worse" — it stopped
describing a bot that didn't exist.

**Verify from data, not memory.** Errors made and caught this session: quoting a
regime-cell value as an overall expectancy (inverted the LIQ/VRP ranking); claiming a fix
would make paper honest when it made it optimistic; diagnosing a correct config value as
stale; over-reading a 5-trade sample as an 80% pattern. Every one was caught by running a
command. Re-derive numbers rather than restating them.

**One change at a time.** Repeatedly this session, two changes landed together and the
result became unattributable.

**`except Exception` hides bugs.** The MAX_HOLD feature (§11.9) silently failed TWICE:
first a timezone mismatch, then `df.index[-1]` returning a RangeIndex integer instead of
a timestamp. Both exceptions were swallowed. `duration_min` stayed 0, MAX_HOLD never
fired, and 97-hour zombie trades persisted undetected. Always test new exit logic with
a debug print before trusting `except Exception` blocks.

**Wider stops reduce scan count.** The wider SL (§11.3) and SL floor (§11.4) made trades
hold longer, starving the scanner of slots. 43 scans vs 426 looked like a data bug but
was a direct consequence of parameter changes. Check scan count alongside trade count
when evaluating parameter changes.

**Automate what you diagnose manually.** The tight-SL and R:R distortion issues (§11.4,
§11.5) were found by eyeballing trade logs. The structural diagnostics (§11.6) now catch
these automatically every session. Build the check when you find the pattern.
