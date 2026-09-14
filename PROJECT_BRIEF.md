# ⚡ Binance Futures AI Trading Terminal — System Brief (CSB)

*Last updated: 2026-09-12 — Production Release + Audit Hardening (CSM Hybrid Ladder, Monotone HWM Peak Tracking, Regime Permission Updates, Relocation-Proof Tools)*

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
| **CSM** | Cross-Sectional Momentum | 24h move in 3.0–4.0 ATR(1h) band + **Completed-Candle Volume Expansion Filter** (`vol_ratio >= 1.0x` 24h avg). | **Dual-Stage Hybrid Ladder**: Stage 1 (+1.0% → +0.15% lock) on 15m bar close; Stages 2 & 3 (+2.5% → +1.50%, +4.0% → +2.50%) on Peak HWM; ATR trailing stop; HWM entry-candle guard. |
| **NASOS_V4** | Freqtrade NASOS Port | Multi-timeframe trend & momentum confirmation with ATR breakout. | Dynamic SL + ATR trailing profit targets. |

*Retired/Archived: ELLIOT_V8 (removed 2026-09-14), WKD, OIB, LIQ, VRP, FF_V2, TP, LLM_ADVISOR.*

---

## 📊 4. Backtest Evidence & Edge Verification

Across extensive 90-day leak-free backtests (100 symbols, 0.08% taker fees, 0.03%/side slippage, compounding risk model):

- **CSM Hybrid Model in RANGING**: Delivered **+666.0% ~ +719.7% net return** with a win rate of **66.3%** and max losing streak of only 11 trades.
- **Volume Filter Impact**: Requiring VOL_RATIO_MIN = 1.0 on the last completed 1h candle eliminates illiquid false breakouts and increases trade expectancy.
- **Bar-Close vs HWM Hybrid**:
  - Evaluating Stage 1 (+1.0%) on completed 15m bar close eliminates 1-second noise premature breakeven exits.
  - Evaluating Stages 2 (+2.5%) & 3 (+4.0%) on Peak HWM instantly captures rapid intra-bar wick expansions.
- **Regime Gating**: In confirmed BULL_TREND squeezes, CSM was found to lose −130.5% (short wicks squeezed, late longs top-ticked). In RANGING, CSM delivers the +666%–+719% result. In BEAR_TREND, the 90-day backtest measured −0.144%/trade (−111.9% cumulative, 778 trades). CSM is gated **RANGING + BEAR_TREND** from 2026-09-12; BEAR_TREND is a live monitoring experiment with the measured backtest baseline above as the decision threshold (EXPERIMENT_LOG §0.2.11).

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
32. **Kronos gate switched on** (`KRONOS_GATE=live`, `KRONOS_PF_THR=0.025`, `KRONOS_GATE_WAIT_SEC=8`, all hot in `settings.json`). Robustness sweep on the 81-trade sample: PASS > BLOCKED at every threshold 0.010–0.030, bottom-25 candidates PF 0.28 — loser-detection robust; PASS-side profit concentrated in one trade. CSM entries with `pred_fav` below threshold are skipped; no score within 8s → fail-open. Kronos worker unchanged. Next reading after ≥40 gated trades. EXPERIMENT_LOG §0.4.

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

