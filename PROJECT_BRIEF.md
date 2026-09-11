# ⚡ Binance Futures AI Trading Terminal — System Brief (CSB)

*Last updated: 2026-09-11 — Production Release (CSM Hybrid Ladder, Monotone HWM Peak Tracking, Freqtrade Port Integrations, Zero-Handshake Session Pooling, Preloaded Exchange Specs)*

---

## 📑 1. What This Is

A modular multi-strategy algorithmic trading engine for **USDT-margined perpetual futures on Binance**. It scans high-volume USDM perpetual contracts on a 24/7 loop, classifies market regimes across BTC/ETH/SOL, evaluates production strategies, gates every signal through a dynamic risk & ML engine, and executes live hedge-mode orders with sub-second latency.

It runs headlessly on an Ubuntu VPS as dedicated systemd services, with real-time interactive control via Telegram and Discord bots, a FastAPI web dashboard with 2FA, and an isolated background Kronos neural forecasting observer.

**Current Operating Mode:** Fully configurable via .env (LIVE_ENABLED=true for live real-money execution on Binance; LIVE_ENABLED=false for internal simulated execution).

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
  1. **CSM (Cross-Sectional Momentum)**: Volatility-normalized 24h momentum breakout with **Dual-Stage Hybrid Profit Ladder**, **Monotone High-Water Mark (HWM) peak tracking**, and **Volume Expansion Filter**. Gated to RANGING where backtests prove $+666\% \sim +719\%$ net profit.
  2. **NASOS_V4**: Freqtrade-ported multi-indicator volatility strategy.
  3. **ELLIOT_V8**: Freqtrade-ported Elliot Wave impulse breakout strategy.

### Stage 4 — Risk Engine & Telemetry · modules/risk_engine.py + modules/ml_engine.py
- Dynamic position sizing based on account equity, ATR stop distance, and leverage cap.
- Multi-layer loss protection: DAILY_LOSS_CAP, WEEKLY_LOSS_CAP, and SESSION_LOSS_FLOOR (persisted to disk to survive restarts).
- ML feature snapshotting (27 dimensionless market indicators) and milestone-based progression (Phases 1–4).
- Automatic exit source stamping (exit_source: 'bot' vs 'manual').

### Stage 5 — Execution & Transport · modules/order_engine.py + modules/auth_manager.py
- Uses a shared pooled 
equests.Session with HTTPAdapter(pool_connections=30, pool_maxsize=30) for zero TCP/TLS handshake latency.
- Enforces ISOLATED margin, sets leverage per symbol, and places market orders in Binance Hedge Mode (positionSide=LONG/SHORT).
- Real-time trailing stop ratcheting via update_stop_order().

---

## 🏗️ 3. Active Strategy Configuration

| Strategy ID | Strategy Name | Entry Mechanism | Exit & Stop Management |
|---|---|---|---|
| **CSM** | Cross-Sectional Momentum | 24h move in 3.0–4.0 ATR(1h) band + **Volume Expansion Filter** (ol_ratio >= 1.0x 24h avg). | **Dual-Stage Hybrid Ladder**: Stage 1 (+1.0% → +0.15% lock) on 15m bar close; Stages 2 & 3 (+2.5% → +1.50%, +4.0% → +2.50%) on Peak HWM; ATR trailing stop. |
| **NASOS_V4** | Freqtrade NASOS Port | Multi-timeframe trend & momentum confirmation with ATR breakout. | Dynamic SL + ATR trailing profit targets. |
| **ELLIOT_V8** | Freqtrade Elliot Wave Port | Wave impulse expansion and wave-3 continuation detection. | Dynamic SL + Wave exhaustion exit. |

*Retired/Archived: WKD, OIB, LIQ, VRP, FF_V2, TP, LLM_ADVISOR.*

---

## 📊 4. Backtest Evidence & Edge Verification

Across extensive 90-day leak-free backtests (100 symbols, 0.08% taker fees, 0.03%/side slippage, compounding risk model):

- **CSM Hybrid Model in RANGING**: Delivered **+666.0% ~ +719.7% net return** with a win rate of **66.3%** and max losing streak of only 11 trades.
- **Volume Filter Impact**: Requiring VOL_RATIO_MIN = 1.0 eliminates illiquid false breakouts and increases trade expectancy.
- **Bar-Close vs HWM Hybrid**:
  - Evaluating Stage 1 (+1.0%) on completed 15m bar close eliminates 1-second noise premature breakeven exits.
  - Evaluating Stages 2 (+2.5%) & 3 (+4.0%) on Peak HWM instantly captures rapid intra-bar wick expansions.
- **Regime Gating**: In confirmed BULL_TREND squeezes, CSM was found to lose -130.5% (short wicks squeezed, late longs top-ticked). Locking CSM exclusively to RANGING converts it into an engine with massive alpha.

---

## 🎛️ 5. Risk Model & Safety Parameters

| Parameter | Value | Description |
|---|---|---|
| **Risk per Trade** | 1.0% of equity | Scaled dynamically by ATR stop distance |
| **Max Concurrent Positions** | 3 (default) | Configurable via MAX_CONCURRENT |
| **Max Margin per Trade** | 30% of equity | Limits maximum capital at risk per trade |
| **Max Leverage** | 50× ceiling | Realized leverage governed by stop width |
| **Daily Loss Cap** | −10% (configurable) | Blocks new entries until next UTC day |
| **Weekly Loss Cap** | −15% (configurable) | Blocks new entries until next UTC Monday |
| **Session Loss Floor** | −10% (configurable) | Per-session safety threshold |
| **Symbol Cooldown** | 15 minutes | Enforced after any losing trade on a symbol |
| **Excluded Assets** | All tokenized equities, ETFs, precious metals | Filtered in modules/symbol_filter.py (EXCLUDED_BASES) |

---

## 🖥️ 6. Deployment & Services

The system is deployed on an Ubuntu 24.04 VPS as modular systemd units:

| Unit | Process | Role |
|---|---|---|
| **csb.service** | live_scanner.py | Core trading engine & position manager |
| **csb-bot.service** | 	elegram_bot.py | Telegram alerts & interactive control |
| **csb-discord.service** | discord_bot.py | Discord slash commands & controls |
| **csb-web.service** | web_server.py | FastAPI real-time dashboard on port 8107 |
| **kronos-shadow.service** | kronos/shadow_worker.py | Observe-only Kronos neural scoring |
| **whale-shadow.service** | kronos/whale_worker.py | Observe-only Binance whale positioning collector |

---

## 📝 7. Changelog — September 2026 Release

1. **Hybrid CSM Stop Ratchet**: Implemented 2-stage hybrid ladder (Stage 1 bar-close stability + Stages 2/3 Peak HWM wick capture).
2. **Monotone HWM Tracking**: Integrated continuous Peak Favourable Excursion measurement into manage(), live_logger.py, and modules/ml_engine.py.
3. **Volume Expansion Breakout Filter**: Added VOL_RATIO_MIN=1.0 in cross_sectional_momentum.py with dynamic volume-based strength scaling (0.5–2.5).
4. **Preloaded Exchange Specs**: Added preload_exchange_specs() to warm contract step sizes and price tick filters on startup for 0ms order latency.
5. **Connection Pooling**: Upgraded uth_manager.py with HTTPAdapter(pool_connections=30, pool_maxsize=30) and get_session().
6. **Tokenized Asset Exclusions**: Expanded EXCLUDED_BASES with all US/Asian tokenized equities, leveraged ETFs, and commodity tokens.
7. **Dedicated Kronos Virtualenv**: Created kronos/requirements.txt and kronos/setup_venv.sh for isolated neural model execution.
8. **Codebase Cleanup & Verification**: Fixed null-byte corruption in root utilities, verified 100% compilation across all 145 Python files, and verified deploy readiness.
