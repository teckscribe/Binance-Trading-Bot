# ⚡ Binance Futures AI Trading Terminal — System Brief (CSB)

*Last updated: 2026-09-24 — four strategies, one regime each; all backtest evidence re-measured after two harness defects were fixed (EXPERIMENT_LOG §0.5.29–§0.5.32)*

---

## 📑 1. What This Is

A modular multi-strategy algorithmic trading engine for **USDT-margined perpetual futures on Binance**. It scans high-volume USDM perpetual contracts on a 24/7 loop, classifies market regimes across BTC/ETH/SOL, evaluates production strategies, gates every signal through a dynamic risk & ML engine, and executes live hedge-mode orders with sub-second latency.

It runs headlessly on an Ubuntu VPS as dedicated systemd services, with real-time interactive control via Telegram and Discord bots, a FastAPI web dashboard with 2FA, and an isolated background Kronos neural forecasting observer.

**Current Operating Mode:** LIVE_ENABLED in .env selects real-money execution on Binance (true) or internal simulated execution (false). Every other tunable lives in `data/settings.json` (`modules/settings_manager.py`), hot-reloaded by the scanner each cycle, so edits from the dashboard, Telegram or Discord apply without a restart.

---

## 🧠 2. Execution Pipeline & Architecture

The bot runs a continuous cycle: **Fast cycles (2s-10s)** manage open positions via high-frequency stop ratcheting, while **Full cycles (60s)** scan the symbol universe for high-conviction breakout entries.

### Stage 1 — Heartbeat & Warmup · live_scanner.py
- Pre-warms contract specifications (LOT_SIZE stepSize and PRICE_FILTER 	ickSize) in a single bulk exchangeInfo call on startup via preload_exchange_specs() for **0ms local RAM order execution**.
- Refreshes the top-volume symbol universe hourly with EXCLUDED_BASES filtering.
- Reconciles live state against Binance USDM Futures (
econcile_with_exchange): detects manual closes and instantly closes unmanaged orphans.

### Stage 2 — Regime Classification · modules/regime_engine.py
- Fetches 1h candles for BTC (primary), ETH (confirmation), and SOL (divergence detector).
- Computes ADX, SMA20 slope, and live funding rates to classify into BULL_TREND, BEAR_TREND, RANGING, OVERHEATED, or OVERSOLD.
- Applies hysteresis (2-bar confirmation, MIN_HOLD_MINUTES=15, 0.5% buffer band) to prevent regime flip-flopping.

### Stage 3 — Strategy Suite & Factory · modules/strategies/strategy_factory.py
- Evaluates permitted strategies based on the current regime.
- **Active Production Strategies**:
  1. **CSM (Cross-Sectional Momentum)**: Volatility-normalized 24h momentum breakout with **Dual-Stage Hybrid Profit Ladder**, **Monotone High-Water Mark (HWM) peak tracking**, and **Volume Expansion Filter**. Proven in RANGING (+666%–+719% net). Also active in BEAR_TREND from 2026-09-12 as a live monitoring experiment (backtest baseline: −0.144%/trade — see §0.2.11 in EXPERIMENT_LOG).
  2. **NASOS_V4**: Freqtrade-ported multi-indicator volatility strategy. Active in BULL_TREND and BEAR_TREND.
  3. ~~ELLIOT_V8~~ — **removed from the codebase 2026-09-14** (benched since 09-12, backtest PF ~1.01 after-tax negative; §0.5 in EXPERIMENT_LOG).

### Stage 4 — Risk Engine & Telemetry · modules/risk_engine.py + modules/ml_engine.py
- Dynamic position sizing based on account equity, ATR stop distance, and leverage cap.
- Multi-layer loss protection: DAILY_LOSS_CAP, WEEKLY_LOSS_CAP, and SESSION_LOSS_FLOOR (persisted to disk to survive restarts).
- ML feature snapshotting (27 dimensionless market indicators) and milestone-based progression (Phases 1–4).
- Automatic exit source stamping (exit_source: 'bot' vs 'manual').

#### Stage 5 — Execution & Transport · modules/order_engine.py + modules/auth_manager.py
- Uses a shared pooled `requests.Session` with `HTTPAdapter(pool_connections=30, pool_maxsize=30)` for zero TCP/TLS handshake latency.
- Rate-gate calibrated to `MIN_REQ_GAP = 0.030s` (33 req/s, ~17% below Binance 2400/min limit).
- Reference cache `_BTC_REF_TTL = 300s` (5 min) avoids re-fetching across scan cycles.
- Enforces ISOLATED margin, sets leverage per symbol, and places market orders in Binance Hedge Mode (positionSide=LONG/SHORT).
- Real-time trailing stop ratcheting via `update_stop_order()`.
- Trade persistence on shutdown via `CLOSE_ON_SHUTDOWN=false` with automatic resume on restart via `_load_positions()`.

---

## 🏗️ 3. Active Strategy Configuration

| Strategy ID | Strategy Name | Entry Mechanism | Exit & Stop Management |
|---|---|---|---|
| Strategy | Regime (from 2026-09-24) | Entry | Exit |
|---|---|---|---|
| **TSMOM_4H** | **BULL_TREND + BEAR_TREND** | 72h return over 18 closed 4h bars ≥ `TSMOM_MIN_MOM_PCT` (5%); inverse-vol strength | SL 2×/TP 4× ATR(4h), 24h time stop |
| **NASOS_V4** | **BULL_TREND only** | Freqtrade port: EWO + RSI dip-buy on 5m | flat 8% SL + 3× ATR TP |
| **REBALANCING_PREMIUM** | **BEAR_TREND only** | 23:00–00:59 UTC, long each of a fixed 10-major basket, no entry condition | SL = TP = 3× ATR(1h), 24h cycle |
| **CSM** | **RANGING only** | 24h move in 3.0–4.0 ATR(1h) band + completed-candle volume filter (`vol_ratio ≥ 1.0×`) | Dual-stage hybrid ladder (bar-close stage 1, peak-HWM stages 2–3), ATR trail |

**One regime per strategy** — each is permitted only where it measured best (§0.5.32), with one exception: TSMOM_4H also runs in BEAR_TREND (§0.5.34), the only assignment both Binance and Delta agree on (PF 1.50 / 1.56) and the fix for BEAR otherwise having a 2-hour daily entry window. OVERSOLD and OVERHEATED: nothing trades.

Coverage: RANGING 60.6 % of hours (CSM), BULL 23.2 % (NASOS + TSMOM), BEAR 16.2 % (TSMOM + REBALANCING).

*Retired/Archived: ELLIOT_V8 (removed 2026-09-14), WKD, OIB, LIQ, VRP, FF_V2, TP, LLM_ADVISOR.*

---

## 📊 4. Backtest Evidence & Edge Verification

**All prior figures in this section were withdrawn on 2026-09-24.** Two harness
defects made every earlier number unusable (EXPERIMENT_LOG §0.5.29):

1. `build_regime_series()` compared a tz-aware funding index against a tz-naive
   bar index, threw, and `main()` silently fell back to a **mock BULL_TREND**
   regime. Every CSM figure ever recorded scored a RANGING/BEAR strategy as
   though the market were permanently bullish.
2. The harness never enforced `REGIME_STRATEGY_PERMISSIONS` — the gate is an
   opt-in env var that no recorded run ever set — so reported figures blended
   in regimes the bot does not allow.

Both fixed. Current method for every figure below: real regime classification
(2,135 hourly bars: RANGING 60.6%, BULL 23.2%, BEAR 16.2%), `BT_FORMING_1H=true`
so the harness hands strategies the **forming** hourly bar exactly as the live
feed does, 90 days, 0.08% round-trip fees, and the live `MAX_SL_PCT=10%`
rejection active.

| Strategy | Universe | N | Win | E[net]/trade | **PF in its enabled regime** |
|---|---|---|---|---|---|
| **TSMOM_4H** — BULL | 22 majors | 201 | 56.2% | +1.62% | **1.99** |
| **NASOS_V4** — BULL | 84 symbols | 117 | 82.9% | +1.32% | **1.96** |
| **REBALANCING_PREMIUM** — BEAR | 22 majors | 124 | 67.7% | +0.85% | **2.47** |
| **CSM** — RANGING | 22 majors | 422 | 56.9% | +0.03% | **1.03** (long-only: 1.20) |

**The regime is the whole story.** Measured across all regimes instead, the same
strategies read 1.74 / 1.14 / 1.18 / 1.08 — NASOS and REBALANCING_PREMIUM look
mediocre only because the blended figure includes regimes they never trade
(NASOS in RANGING: PF 0.86 over 241 trades; REBALANCING in RANGING: 0.94 over
447). CSM is the reverse: its all-regime 1.08 is flattered by BULL_TREND
(PF 1.39), which it is forbidden from trading; in its own regimes it is 0.94,
and **live over 314 real trades it is 0.96** — the strategy matches its
measurement exactly once the measurement is done properly.

**CSM's loss is specific:** RANGING+LONG is PF 1.20, RANGING+SHORT is **0.26**,
and all of BEAR_TREND is negative. DCB's independent Delta sweep reached the
same conclusion (long-only best, shorts contributed nothing) — two venues, two
datasets.

**Caveats that apply to every number above:** one 90-day in-sample window, one
scan-phase alignment; the §17.32 rule (≥5 offsets) has **not** been run on these
regime cuts. TSMOM_4H is calendar-driven — July was negative on both Binance and
Delta — so judge it by month, not by regime. NASOS_V4's payoff is lopsided
(avg win +3.6%, avg loss −8.0%): one loss erases roughly two wins, and its 80%
win rate is not safety.

---

## 🎛️ 5. Risk Model & Safety Parameters

| Parameter | Value | Description |
|---|---|---|
| **Risk per Trade** | 1.0% of equity | Scaled dynamically by ATR stop distance |
| **Max Concurrent Positions** | 3 (default) | `MAX_CONCURRENT` in `data/settings.json` — hot-reloaded |
| **Max Margin per Trade** | 30% of equity | Limits maximum capital at risk per trade |
| **Max Leverage** | 50× ceiling | Realized leverage governed by stop width |
| **Daily Loss Cap** | −10% (configurable) | Blocks new entries until next UTC day |
| **Weekly Loss Cap** | −15% (configurable) | Blocks new entries until next UTC Monday |
| **Session Loss Floor** | −10% (configurable) | Per-session safety threshold |
| **Symbol Cooldown** | 15 minutes | Enforced after any losing trade on a symbol |
| **Excluded Assets** | All tokenized equities, ETFs, precious metals | Filtered in modules/symbol_filter.py (EXCLUDED_BASES) |
| **Mid-Trade Restart Survival** | `CLOSE_ON_SHUTDOWN=false` | Persists stops & HWM to disk; rehydrates seamlessly |

All parameters marked configurable live in `data/settings.json` (schema: `modules/settings_manager.py` `SPEC`, 46 keys) and are re-read by the scanner every cycle. Edits from the dashboard Settings tab, Telegram or Discord apply within one cycle with no restart. `.env` holds only secrets and `LIVE_ENABLED`.

---

## 🖥️ 6. Deployment & Services

The system runs on a 24×7 Ubuntu 24.04 desktop (operated remotely via AnyDesk) as modular systemd units:

| Unit | Process | Role |
|---|---|---|
| **csb.service** | live_scanner.py | Core trading engine & position manager |
| **csb-bot.service** | telegram_bot.py | Telegram alerts & interactive control |
| **csb-discord.service** | discord_bot.py | Discord slash commands & controls |
| **csb-web.service** | web_server.py | FastAPI real-time dashboard on port 8107 |
| **kronos-shadow.service** | kronos/shadow_worker.py | Observe-only Kronos neural scoring |
| **whale-shadow.service** | kronos/whale_worker.py | Observe-only Binance whale positioning collector |

---

## 📝 7. Changelog

### Regime re-measurement & one-regime-per-strategy — 2026-09-24
1. **Two harness defects fixed** (§0.5.29): regime classification had been silently dead (tz-aware/naive mismatch → mock BULL_TREND fallback); the harness never enforced live regime permissions. Missing `{BTC,ETH,SOL}USDT_1h_90d.csv` fetched. New `BT_FORMING_1H` reproduces the live forming hourly bar; `CSM_CLOSED_BAR_MOM` added (default off) after the repaint hypothesis was measured and **withdrawn** — the forming bar is slightly better than closed bars (1.08 vs 1.05).
2. **Every strategy re-measured** (§0.5.30, §0.5.31). 62 more symbols fetched (84 total) so NASOS could be judged on 441 trades instead of 34.
3. **My two wrong calls, corrected on the record:** recommending REBALANCING_PREMIUM be retired (it is PF 1.50 where it actually runs, not 1.08), and claiming NASOS loses in BEAR from a 12-trade live sample (83 backtest trades say PF 1.51).
4. **CSM audited against the four "LLM backtest" failure modes** (§0.5.28): look-ahead — no, the harness is stricter than live; slippage — measured at +0.0002% entry, fees exactly 0.08% as modelled; repainting — real (28% of mid-hour signals vanish by the hour close) but harmless; overfitting — the remaining live explanation, and CSM matches its permitted-regime backtest anyway.
5. **One regime per strategy** (§0.5.32, operator decision): TSMOM_4H → BULL, NASOS_V4 → BULL, REBALANCING_PREMIUM → BEAR, CSM → RANGING.
6. **TSMOM_4H added to BEAR** (§0.5.34) — the only cross-venue-agreed assignment (Binance 1.50 / Delta 1.56), and BEAR otherwise had only REBALANCING's 2-hour nightly window.
7. **Dashboard fix** (§0.5.33): `/api/strategies` ignored `MAX_PER_STRATEGY`, so strategies capped at 0 slots displayed ACTIVE. New CAPPED state.



### Production Release — 2026-09-11 20:30 IST
1. **Hybrid CSM Stop Ratchet**: Implemented 2-stage hybrid ladder (Stage 1 bar-close stability + Stages 2/3 Peak HWM wick capture).
2. **Monotone HWM Tracking**: Integrated continuous Peak Favourable Excursion measurement into manage(), live_logger.py, and modules/ml_engine.py.
3. **Volume Expansion Breakout Filter**: Added VOL_RATIO_MIN=1.0 in cross_sectional_momentum.py with dynamic volume-based strength scaling (0.5–2.5).
4. **Preloaded Exchange Specs**: Added preload_exchange_specs() to warm contract step sizes and price tick filters on startup for 0ms order latency.
5. **Connection Pooling**: Upgraded auth_manager.py with HTTPAdapter(pool_connections=30, pool_maxsize=30) and get_session().
6. **Tokenized Asset Exclusions**: Expanded EXCLUDED_BASES with all US/Asian tokenized equities, leveraged ETFs, and commodity tokens.
7. **Dedicated Kronos Virtualenv**: Created kronos/requirements.txt and kronos/setup_venv.sh for isolated neural model execution.

### Production Cross-Check Audit & Hardening — 2026-09-11 23:45 IST
8. **Strategy Overrides Re-Enabled**: Cleared manual block in `data/strategy_overrides.json`, restoring permissions for `CSM`, `NASOS_V4`, and `ELLIOT_V8`.
9. **CSM Hourly Volume Gating Bug Fixed**: Switched volume filter to last completed candle (`iloc[-2]`), resolving the 30–45 min hourly entry freeze.
10. **Rate-Limit Headroom**: Increased `MIN_REQ_GAP` to 0.030s (33 req/s) providing ~17% safety margin under Binance's 40 req/s ceiling.
11. **BTC Reference Cache**: Extended `_BTC_REF_TTL` to 300s, preventing scan cycle cache misses and saving 5 API calls per loop.
12. **HWM Entry Candle Guard**: Clamped price extremes on the entry bar to prevent phantom stop-outs from pre-fill wick leakage.
13. **Kronos Inference Mode (`.eval()`)**: Added explicit `.eval()` mode calls on Kronos model and tokenizer to turn off stochastic dropout during inference.
14. **Kronos Dependencies & Pipeline**: Added `safetensors>=0.4.0` and wired `log_candidate()` into `live_scanner.py` candidate loop.
15. **Mid-Trade Restart Survival**: Configured `CLOSE_ON_SHUTDOWN=false` (settings.json) with validated state persistence and rehydration.
16. **Repository Hardening**: Added `.gitattributes` (`*.sh text eol=lf`), cleaned `.gitignore` (targeted `data/*.csv`), removed unused `cryptography`.

### Post-Release Audit — 2026-09-12 08:30 IST
17. **Regime Permission Changes (intentional)**: `BEAR_TREND: CSM=True` (monitoring experiment — backtest baseline −0.144%/trade); `ELLIOT_V8=False` in all regimes (benched — PF ~1.01 after-tax negative). See EXPERIMENT_LOG §0.2.11.
18. **NASOS Parameter Sweep**: 2,880 gated configs tested; no config cleared PF > 1.429 in a phase-robust measurement. Current flat 8% config retained (PF 1.30, after-tax −0.177%). See EXPERIMENT_LOG §17.31.
19. **Scan-Phase Sensitivity Analysis**: CSM Config A is phase-robust (PF 1.39–1.57 across 7 phases, mean 1.50). NASOS is phase-sensitive (PF 0.79–1.45, mean 1.09). Single-phase NASOS measurements are unreliable. See EXPERIMENT_LOG §17.32.
20. **Relocation-Proof Tools**: All 12 `tools/*.py` scripts + `overnight.sh` use `os.path.dirname(os.path.abspath(__file__))` rather than hardcoded paths. Safe after project folder rename.
21. **194S TDS confirmed inapplicable**: 1% TDS under Section 194S does NOT apply to USDM futures derivatives (no VDA transfer occurs). Tax question still open on 115BBH vs speculative-business classification — resolve with a CA.

### Runtime Settings Migration (.env → settings.json) — 2026-09-12 12:20 IST
22. **Hot-reloaded settings store**: New `modules/settings_manager.py` — single `SPEC` (46 keys: type, bounds, default, help, hot flag), typed `get()` cached on `(mtime, size)` and stat-throttled to 1/s, validated atomic `update()` (temp file + `os.replace`). Store is `data/settings.json` (gitignored). Commits `928b8e2`, `1ec0e58`, `6e0ab0e`.
23. **`.env` reduced to secrets + `LIVE_ENABLED`**: Binance keys, bot tokens/IDs, webhook, ngrok authtoken, TOTP secret and `LIVE_ENABLED` stay in `.env`. Every tunable (equity, intervals, risk caps, leverage, per-strategy caps, loss caps, all `CSM_*` / `NASOS_*`, ML phase/shadow, Telegram toggle, log level) moved to `settings.json`. Only `NGROK_ENABLED` still needs its service restarted.
24. **Zero-touch migration**: first service start after deploy seeds `settings.json` 1:1 from that machine's `.env` (verified against the production `.env`: 36 values seeded, 10 absent keys take the code's existing defaults). `.env` lines are left in place and ignored. Behaviour before/after is identical.
25. **Scanner per-cycle refresh**: `live_scanner._refresh_settings()` re-snapshots all hot keys at the top of every loop cycle, logs each change (`[SETTINGS] KEY changed -> value (applied live)`), forces a symbol-universe rebuild when `FOCUSED_MODE` / `FOCUSED_SIZE` / `TOP_N_SYMBOLS` / `MIN_COIN_AGE_DAYS` change, applies `LOG_LEVEL` live, and resets the paper ledger when `ACCOUNT_EQUITY_USDT` changes in PAPER mode.
26. **Call-time reads everywhere else**: `risk_engine` (`max_concurrent()`, `max_per_strategy()`, `get_leverage()`, loss caps, SL bounds), `ml_engine` (`_phase()`, `_shadow()`), `watchlist` (`MIN_COIN_AGE_DAYS`), CSM and NASOS strategies — no more import-time constants.
27. **One writer path**: dashboard `POST /api/settings`, Telegram and Discord bots all go through `settings_manager.update()`; the three divergent `.env` writers (two not crash-safe) are removed. Bot messages now say "applies on the next cycle" instead of "restart the scanner". `LIVE_ENABLED` flips still use an atomic `.env` write and restart the scanner.
28. **Backtest sweeps preserved**: a shell env value that did not come from `.env` still overrides the file (`CSM_MOM_LO=3.5 python backtest_optimizer.py`), and such overrides are never persisted.
29. **Audit trail**: every applied change appends `{ts, source, key, from, to}` to `data/settings_history.jsonl` (source = dashboard / telegram / discord / migrate_from_env); dashboard Settings tab shows the last 30 via `GET /api/settings/history`.
30. **Verification**: 42 unit checks (migration, typing, validation, cross-process hot reload ≤1s, corrupt-file recovery, `.env` preservation) + 51 integration checks (scanner refresh, live sizing gates, CSM max-hold, TOTP-authenticated dashboard save, bot helpers) + static wiring audit (every SPEC key read via `settings_manager`, zero `os.getenv` reads of migrated keys, no dangling keys, no legacy writers) — all green. Dashboard verified in browser.

### Kronos Shadow Gate — First Verdict — 2026-09-14 07:00 IST
31. **Kronos reached its verdict threshold (81/80 matched CSM trades)**. Ablation: Kronos-alone AUC 0.697 vs ML 27-feature AUC 0.624; base+Kronos dAUC +0.022. Gate split of the same 81 live trades (pre-registered 0.020 threshold): PASS N=31 PF 1.75 (+2.48%), BLOCKED N=50 PF 0.55 (−3.36%), ALL PF 0.92. **Not flipped live** — second reading required at N≈120 (PASS PF ≥ 1.3 and BLOCKED PF ≤ 0.9) before a hot-reloadable `KRONOS_GATE` setting is built. Full numbers, robustness checks and decision rule: EXPERIMENT_LOG §0.4.

### Kronos Gate LIVE — 2026-09-14 08:30 IST
32. **Kronos gate switched on** (`KRONOS_GATE=live`, `KRONOS_PF_THR=0.025`, `KRONOS_GATE_WAIT_SEC=8` — 20 on 2026-09-15 (§0.5.17), then 10 on 2026-09-17 once rank-order queueing landed (§0.5.18); all hot in `settings.json`). Robustness sweep on the 81-trade sample: PASS > BLOCKED at every threshold 0.010–0.030, bottom-25 candidates PF 0.28 — loser-detection robust; PASS-side profit concentrated in one trade. CSM entries with `pred_fav` below threshold are skipped; no score within 8s → fail-open. Kronos worker unchanged. Next reading after ≥40 gated trades. EXPERIMENT_LOG §0.4.

### Phantom-Close Fix, Rate Throttle, NASOS Shadow, ELLIOT Removal — 2026-09-14 14:00 IST
33. **Phantom "manual" closes fixed** (`52b2a1a`, `94953f5`): a failed `positionRisk` read returned `{}` and the reconciler treated every tracked position as manually closed — booked at entry price, dropped from tracking, then market-closed as an orphan one cycle later with its real P&L never recorded. 108 such trades in the synced history (37 on 09-13). Now: a failed read skips the cycle, a missing position is only booked when a real closing fill exists, and exchange-stop fills are labelled `SL_HIT`, not `MANUAL_CLOSE`.
34. **Weight-aware rate throttle** (`174b23a`, `modules/rate_budget.py`): the `-1003` rejections behind the failed reads came from running ~2700 request-weight/min against Binance's 2400 cap (30ms request gap counted requests, not weight). Every `SESSION` call is now gated on `X-MBX-USED-WEIGHT-1M` with soft (1900, scan) / hard (2300, position management) ceilings and full `Retry-After` backoff on 429/418. `SCAN_INTERVAL_SECONDS` set to 120 on the box → ~1740/min worst case.
35. **Full trade context captured** (`2532de6`): regime at entry/exit, BTC/ETH/SOL trend, funding, signal price + fill slippage, Kronos score, ML outputs, settings snapshot, MFE/MAE, stop moves — on every ledger EXIT record and ML outcome row. Kronos score + regime shown in Telegram/Discord alerts and dashboard tables (`d1f36f9`).
36. **NASOS_V4 queued for Kronos shadow scoring** — shadow only, gate remains CSM-only (`KRONOS_GATE_STRATEGIES`). Ablation and progress tooling are per-strategy; the dashboard Kronos card shows the NASOS matched count. Verdict when its matched count reaches 80.
37. **ELLIOT_V8 removed from the codebase**: `modules/strategies/freqtrade_port_elliot.py` deleted; factory, regime matrix, leverage table, settings default, bots, notifiers, dashboard, backtesters and tools updated. Existing `ELLIOT_V8` entries in on-box `data/settings.json` (`MAX_PER_STRATEGY`) and `strategy_overrides.json` are ignored harmlessly.

### Whale Per-Strategy, Dead-Code Sweep, Kronos Win% — 2026-09-14 17:30 IST
38. **Whale positioning analysis per strategy** (`6182797`): `kronos/whale_analyze.py` and `kronos/progress.py` now evaluate CSM and NASOS_V4 separately (WHALE_MIN_N=120 each); dashboard Kronos/Whale card shows the NASOS whale-matched count alongside CSM.
39. **Last ELLIOT_V8 traces removed** (`bec84cd`): comments, display labels and the backtest cache filename (`data/bt_cache_90d_CSM-NASOS.json`). Only historical docs/logs still mention it.
40. **Dead-code and wiring cross-check** (`9be4a50`): pyflakes clean over 74 modules (20 unused/shadowed imports removed, zero undefined names); 13 unreferenced definitions removed (`BlacklistAddModal`, `_send_text`, `reset_day_start`/`reset_week_start`, `fetch_multi_timeframe`, `_query_order_status`, `current_equity`, `max_trade_loss_pct`, `clear_symbol`/`clear_all`, `get_btc_symbol`, `_back_keyboard`); `modules/strategies/freqtrade_port_sma.py` deleted (registered nowhere; `SMA_OFFSET` leverage/label entries kept so old positions still render). Settings wiring audit consistent, all services import, factory = `['CSM','NASOS_V4']`, 7 scratch suites pass, every dashboard GET 200. No behaviour change.
41. **Kronos win% on the 81-trade shadow sample** (for reference; live-gated reading pending ≥40 gated trades): ALL 44.4% / PF 0.92; **PASS ≥ 0.025 (live threshold) N=19 win 63.2% PF 3.96 (+3.3%)**; BLOCKED N=62 win 38.7% PF 0.57 (−4.2%). At 0.020: PASS N=31 win 54.8% PF 1.75 / BLOCKED N=50 win 38.0% PF 0.55. EXPERIMENT_LOG §0.5.10.

### External Audit Response — 2026-09-14 19:00 IST
42. **`hold_minutes()` was undefined** — NASOS `manage()` called `self.hold_minutes()` behind the `PORT_MAX_HOLD_MIN > 0` guard, and no such method existed; any dashboard edit setting the (hot) max-hold above 0 would have crashed every NASOS manage cycle. Added `BaseStrategy.hold_minutes()` (same `pd.to_datetime` logic CSM uses inline). Tested with the guard armed: 90-min hold → `MAX_HOLD` exit, 10-min → no exit.
43. **Audit clean-ups**: dead `'leverage': 1` removed from the NASOS signal dict (sizing always used `STRATEGY_LEVERAGE` → 3×; nothing read the field); NASOS 1m→5m resample deduplicated into `_to_5m()`; stale comments corrected (`regime_engine.py` LIQ/VRP "fully wired" → deleted; `live_scanner.py` `KRONOS_*_STRATEGIES` are module constants, restart required, deliberately not hot; `compute_position_size` docstring strategy IDs). Dev `data/settings.json` / `strategy_overrides.json` cleared of `ELLIOT_V8` — **do the same on the box** (see EXPERIMENT_LOG §0.5.11). Audit items rejected with reasons: §0.5.11.
44. **Legacy strategy files deleted** — `trend_pullback.py` (TP), `funding_fade_v2.py` (FF_V2), `grid_strategy.py` (GRID), `freqtrade_port_ichi.py` (ICHI_V1). All out of production since August, kept only for `backtest_optimizer.py` / a GRID-dissolve path in the scanner; no GRID position has existed for weeks. Removed with them: `StrategyFactory.get_grid()`, the regime-change grid-dissolve block in `live_scanner.py`, `get_oi_trend()` + its OI cache in `base_strategy.py` (TP was its only caller), TP/FF_V2/ICHI entries in the backtesters and replay sync list. `modules/strategies/` is now exactly `base_strategy`, `strategy_factory`, `cross_sectional_momentum`, `freqtrade_port_nasos`.
45. **Standalone-script sweep**: removed root `list_optimizer.py` (stale duplicate of `modules/list_optimizer.py`) and `backtest_freqtrade_strategies.py` (backtested three deleted strategies from a directory that no longer exists); `tools/replay/_run/` runtime state untracked and git-ignored. Every remaining script was checked and still serves CSM/NASOS. EXPERIMENT_LOG §0.5.13.
46. **Kronos threshold has one copy** — the shadow worker no longer holds `KRONOS_PF_THR` (it was `0.020` in the service unit while the live gate ran `0.025` from `settings.json`). The worker records raw `pred_fav`/`pred_adv`/`dir_ret` only; `kronos/progress.py` derives the dashboard's "would-pass" count from `pred_fav` at the live threshold on every request (2,673 / 12,111 = 22.1% at 0.025 vs 3,564 at the stale 0.020). `Environment=KRONOS_PF_THR` removed from `kronos-shadow.service`. Worker stays standalone (no `settings.json` read). Dashboard label "would-gate" → "would-pass".
47. **`KRONOS_POLL_SEC=2`** in `kronos-shadow.service` (was unset → 5 s). Measured on 2,702 real scan bursts: the top-ranked candidate's score arrived after the 8 s gate wait in 14.4 % of cycles at 5 s (trade taken unscored, fail-open) vs 8.5 % at 2 s. No API weight change (fetches are per candidate, not per poll). Remaining fail-opens are queue-order on busy scans — revisit after a week of journal counts. EXPERIMENT_LOG §0.5.15.
48. **Kronos weights pinned** (`kronos/scorer.py`): `NeoQuasar/Kronos-small@901c26c1` and `NeoQuasar/Kronos-Tokenizer-base@0e011738` — the upstream commits (both 2025-09-09) every shadow score and the §0.4 verdict were produced with; verified against the local HF cache snapshots. Unpinned, `from_pretrained()` resolved `main` on each start and would have adopted a new upstream release silently, invalidating `KRONOS_PF_THR`. Override with `KRONOS_REVISION` / `KRONOS_TOKENIZER_REVISION` to shadow-test a new release. Worker logs the revisions at start. No HF token needed (public weights; loads offline from cache once downloaded). EXPERIMENT_LOG §0.5.16.

### Kronos Queue Order, Dashboard, Shutdown, Gate Reading — 2026-09-16 → 2026-09-18
49. **A change shipped without reading the log, then reverted** (`090f9b2` → `1656856`): a `CSM_BEAR_LONG_MAX_AGE_MIN` gate with no backtest basis (duplicating what the Kronos gate already measures — BEAR PASS PF 5.71 vs BLOCKED 0.26, §0.5.4) and `NASOS_TP_ATR` 3→4 against §17.31's "do not tune until ≥200 live trades". Both reverted the same day. Standing rule, now in the assistant's memory as well: **read `docs/EXPERIMENT_LOG.md` before touching any strategy, gate or setting.**
50. **Kronos candidates queued in rank order** (`ed4b446`, §0.5.18): `log_candidate()` ran inside the per-symbol scan loop (scan order) while `_execute_entries()` walks candidates strongest-first, so the one that trades was routinely scored behind weaker ones. A real gate bypass followed — FLOCKUSDT entered fail-open 0.03 s before its score landed at `pred_fav 0.0064`. Queueing now happens after the strength sort. Measured on 13,694 shadow rows: top-ranked fail-open **12.7 % → 0.4 % at 8 s, 5.7 % → 0.2 % at 10 s**. Zero fail-opens since deploy. Operator set `KRONOS_GATE_WAIT_SEC=10` on that basis.
51. **"Share the scanner's klines with the worker" ruled out** (§0.5.19): `scorer._LB_DEFAULT=256` context bars vs `data_hub` fetching 15m at `limit=200` — passing the scanner's frame would `insufficient_context` on every candidate and fail open 100 %. Deferred, not rejected; revisit only if the gate survives the N=80 reading.
52. **Enricher was 2 days stale; NASOS 0 % Kronos-matched was a date artefact** (§0.5.19): all NASOS rows in `ml_kronos_enriched.jsonl` predated 09-14 when NASOS shadow scoring began. Re-run on the box: CSM 121/123 matched, NASOS 13/29. CSM ablation N=121: Kronos alone AUC 0.618, base 0.630, base+Kronos 0.621 (dAUC −0.009 — redundant on top of the ML features, with ~24 rows/fold and ML at Phase 2 gating nothing). **`enrich_ablation.py` is manual — re-run it at every gate reading.**
53. **Notifier fixes** (`dd7a7d2`, `f4d78fd`, `bd7285e`, §0.5.20): startup message listed every strategy as active when *none* were (`[] or [...]` fallback) — now `is not None`; `notify_error` posted unescaped text under `parse_mode=HTML`, so any `<` (every "Kronos gated … < 0.025" alert) returned 400 and was silently dropped — now `html.escape`d at the sink; the per-block gate ping itself removed (100+ "Bot Error" pings/day for the gate's normal outcome — journal line kept).
54. **Dashboard: trade history and P&L broke under restarts** (`8bb7600`): `/api/trades` read the last **three** session files by name, and six restarts in a day left them holding 0/0/1 trades — now walks newest-first to 50. LIVE startup set `PAPER_STARTING_EQUITY = live balance` on **every** start, so total P&L measured from the last restart — now persisted in `data/live_equity.json` (pinned once from Binance, delete to start a new period). `/api/performance` was scoping by `paper_equity.json`, frozen 08-29 in paper mode at $100 — live baseline takes precedence.
55. **Shutdown honoured within 1 s** (`40006a7`): the SIGTERM handler set a flag the loop only checked after `time.sleep(SCAN_INTERVAL)`; with no positions open a stop took ~70–110 s — slowest exactly when there was nothing to protect. Sleep now runs in 1 s slices. Both bots' `_systemctl` raised 70→120 s and, on timeout, ask `is-active` before reporting — a 71 s clean stop had been reported as failed, and the operator restarted a scanner that had already stopped. Stop text no longer claims "closing all positions" (`CLOSE_ON_SHUTDOWN=false`).
56. **No watchlist sweep when nothing can scan** (`dd6775e`): `do_scan = due_for_scan and slots_free` never asked whether a strategy was permitted. With both `/disabled`, the cycle still fetched 150 symbols at NASOS depth (~1,920 of 2,400 weight, 35 s) for a scan that returned "No strategies permitted" a line later. Symbol set now narrows to open positions when `_permitted` is empty; depth/1h derivation and the `is_fast` deadlock guard untouched. Steady-state in BULL/BEAR with NASOS on remains ~1,900 — that is the strategy's 1,500-bar 1m fetch × 150 symbols and is legitimate.
57. **§0.4 second reading — N=45, rule says demote, operator keeps** (`8a220ee`, §0.5.21): all-scored PF 0.84 / mean −0.041 % fail both criteria; 16 of 45 were restart closes (−$6.78) and all 29 the market decided won (+$5.66). Recorded unchanged, override stated. Also recorded that "the account went positive after the gate" is **not** supported — CSM all-trade P&L went +$1.21 → −$0.99 under the gate; the swing came from ungated NASOS (regime). Gate's actual contribution: CSM organic 94 % → 100 %. **Third reading pre-registered: N≥80, valid only if `MANUAL_CLOSE` share < 20 % (else deferred), thresholds as written, 0.025 unchanged.**
58. **Whale worker status**: 16k+ records, 95 % match to gated CSM exits, healthy — and untestable on both strategies for the same reason as everything above: 48 organic exits since 09-14, **48 wins, 0 losses**, every loss on the book a restart. Every open measurement (Kronos N=80, NASOS ablation, whale ×2) is blocked on restart discipline, not on code.

