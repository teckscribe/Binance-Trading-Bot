# ⚡ Binance Futures AI Trading Terminal — System Brief (CSB)

*Last updated: 2026-08-04 — revised after a full code audit and a 30-day backtest.*

> **Read this first.** An earlier version of this brief described the system as
> designed rather than as built. Several of its claims did not match the code.
> Those gaps are corrected inline below and collected in
> [§7 Design vs. Implementation](#7-design-vs-implementation). Where a number is
> quoted it comes from a measured backtest, with its limitations stated.

---

## 📑 1. What This Is

A modular multi-strategy trading engine for **USDT-margined perpetual futures on
Binance**. It scans a watchlist of high-volume symbols on a 24/7 loop,
classifies the market regime, runs seven independent strategies, gates every
signal through a risk engine, and executes (or dry-runs) the trade.

It runs headlessly on an Ubuntu VPS as five `systemd` services, with real-time
control via Telegram and Discord bots and a FastAPI web dashboard.

**Current operating mode:** `LIVE_ENABLED=false` — dry run. No real orders are
placed. There is **no paper-trading mode** in this project; paper trading lives
in the separate `cs` project. Dry run is not the same thing: it skips order
placement but still simulates fills internally at market price.

---

## 🧠 2. Execution Pipeline

A continuous loop; fast cycles (5s) manage open positions, full cycles (60s)
also scan for new entries.

### Stage 1 — Heartbeat · `live_scanner.py`
Main event loop. Refreshes the top-volume symbol list hourly, decides fast vs.
full cycle, reconciles with Binance (adopts orphaned positions, detects manual
closes), and handles `SIGTERM` by closing all open positions at market before
exit.

### Stage 2 — Regime Classification · `modules/regime_engine.py`
Fetches 1h data for BTC (primary), ETH (confirmation) and SOL (divergence
detector), computes ADX, SMA20 slope and live funding, and classifies into
`BULL_TREND`, `BEAR_TREND`, `RANGING`, `OVERHEATED` or `OVERSOLD`. Hysteresis
(2-bar confirmation, `MIN_HOLD_MINUTES=15`, 0.5% band) prevents flip-flopping.

> ⚠️ **This is not currently a master switch.**
> `REGIME_STRATEGY_PERMISSIONS` is `True` for every strategy in every regime, so
> regime gates nothing at the factory level. Of the seven production
> strategies, **only TP reads the regime at all**; the other six ignore it.
> See §7.

### Stage 3 — Strategy Factory · `modules/strategies/strategy_factory.py`
Instantiates the seven permitted strategies and calls
`scan(symbol, df_1m, df_15m, df_1h, regime)` on each. Returns a signal dict or
`None`. Per-strategy runtime disable is available via
`modules/strategy_overrides.py`.

### Stage 4 — Risk Gate · `modules/risk_engine.py` + `modules/ml_engine.py`
ATR-based position sizing, concurrency cap, per-symbol loss cooldown, and three
persistent loss caps. Immediately before execution the ML hook
(`get_ml_adjustments`) snapshots 27 market features and — depending on
`ML_PHASE` — can veto the trade or cut its size. Currently `ML_PHASE=1`.

### Stage 5 — Execution · `modules/order_engine.py` + `modules/llm_advisor.py`
Sets isolated margin and per-symbol leverage, then places a market order in
hedge mode (`positionSide=LONG/SHORT`), rounding quantity to `stepSize`. With
`LIVE_ENABLED=false` this is a dry run. If `LLM_ADVISOR_ENABLED=true`, a
DeepSeek-backed advisor polls every 15 minutes and can force an early exit.
Currently disabled.

---

## 🏗️ 3. The Strategies

Seven run in production. **The Elite / Good tier labels below are historical and
are not supported by measured performance** — see §4.

| ID | Name | What it actually does |
|---|---|---|
| `CSM` | Cross-Sectional Momentum | Enters LONG when a symbol has moved **>3 × ATR(1h) in 24h**. Despite the name it does **not** rank across the universe and has **no short leg** — it is a volatility-normalised breakout. |
| `WKD` | Weekend Anomaly | LONG at Friday 20:00 UTC, exit Sunday 23:00 UTC or on trailing stop. |
| `LIQ` | Liquidation Cascades | Volume >3× average plus a wick >60% of candle range; fades the wick. Trades both directions. |
| `VRP` | Volatility Risk Premium | Fades implied-vs-realised volatility dislocations. Both directions. |
| `TP` | Trend Pullback | EMA20/50 trend + pullback to EMA20 + RSI + MACD + ADX + volume. **Seven filters, and the only strategy that requires a regime** (`BULL_TREND` or `BEAR_TREND`). LONG in bull, SHORT in bear. |
| `OIB` | Open Interest Breakouts | Open-interest surge with volume and price breakout. LONG only. |
| `FF_V2` | Funding Rate Fade V2 | **Does not read funding rates.** Uses a trend-stretch proxy: fades price when it deviates >4% from the 200-EMA. Both directions. |

Archived and bypassed by the factory: Donchian Breakout, Grid, Bollinger Band
Reversion, Betting Against Beta, Overnight Seasonality (v1/v2), Pairs Trading,
Short-Term Reversal, Time Series Momentum, Turn of Month, Funding Fade v1.

---

## 📊 4. Measured Performance

**30 days · top-20 by volume · net of 0.08% round-trip taker fee ·
KORUUSDT excluded (unadjusted 20:1 split) · regime classified per 1h bar.**

| Strategy | Tier (label) | Trades | Win% | E[net]/trade | PF | Sum | Positive weeks |
|---|---|---:|---:|---:|---:|---:|---:|
| **CSM** | Elite | 556 | 44.1% | **+0.535%** | 1.75 | **+297.5%** | **5 / 5** |
| **WKD** | Elite | 72 | 50.0% | **+0.393%** | 1.83 | +28.3% | 3 / 5 |
| OIB | Good | 615 | 34.8% | −0.099% | 0.87 | −61.1% | 2 / 5 |
| LIQ | Elite | 393 | 33.6% | −0.308% | 0.63 | −120.9% | **0 / 5** |
| FF_V2 | Good | 1,311 | 29.7% | −0.366% | 0.73 | −479.8% | 2 / 5 |
| TP | Good | 28 | 14.3% | −0.504% | 0.34 | −14.1% | 0 / 5 |
| VRP | Elite | 170 | 29.4% | −0.564% | 0.61 | −96.0% | 1 / 5 |

**Only CSM and WKD clear the fee.** The other five combined lose −0.307% per
trade over 2,517 trades. Two of the four "Elite" strategies (LIQ, VRP) are among
the three worst performers; LIQ lost money in every week measured.

### Portfolio results ($20, 5×, 3 slots, 30% margin)

| Configuration | Return | Max DD | Sharpe |
|---|---:|---:|---:|
| All 7 strategies | −41.32% | 48.0% | −6.50 |
| Elite tier only | −34.91% | 46.2% | −5.71 |
| CSM + WKD | −4.28% | 27.4% | 1.06 |
| **CSM + WKD, crypto-native, no daily cap** | **+274.56%** | 27.0% | 5.80 |

### The single largest lever: the daily loss cap

`DAILY_LOSS_CAP = -0.05` against 30% margin at 5× means ~1.5× equity notional
per position, so **under three average losses trips the cap** and locks out the
rest of the UTC day. CSM makes money in its right tail with a *negative* median
trade, so the cap ejects it precisely while it waits for the winners.

| CSM alone | Return |
|---|---:|
| live config (−5% daily) | −8.94% |
| daily cap −15% | +28.25% |
| no daily cap | +64.01% |
| no caps | +138.85% |

The cap did not even reduce drawdown (27.0% capped vs 22.4% uncapped).

### Capacity starvation

3,145 signals compete for 3 slots at a 1.8h median hold — only ~101 get taken.
Allocation is **first-come-first-served by arrival time**, so signal *volume*
decides slot share, not signal *quality*. FF_V2 generates **41.7%** of all
signals at −0.366%/trade and crowds out CSM (17.7%). WKD took **zero** trades in
the combined engine. A `strength` field already exists in the signal schema and
is populated — nothing ranks on it.

### ⚠️ How much to trust these numbers

- **One 30-day in-sample window.** Parameters that look best were chosen on the
  same data they are measured against.
- **68.6% of CSM's entire result came from a single week** (W31), which was also
  the market's strongest up-week. CSM is long-only, so it is convex to market
  direction.
- **Sharpe values above ~4 are artifacts** of annualising 30 days. Not
  sustainable figures.
- A first-half/second-half split is encouraging — CSM+WKD is profitable in both
  (+0.259% then +0.827% per trade) — but the halves are adjacent and share
  market conditions. **This is not a true out-of-sample test.**
- The market *fell* 4.2% equal-weight over the window, so CSM's long-only gain
  is genuine selection alpha rather than beta.

**Before sizing to any of this, fetch 90–180 days and re-run.**

---

## 🎛️ 5. Risk Model

| Parameter | Value | Notes |
|---|---|---|
| Risk per trade | 1% of equity | ATR-derived stop distance |
| Max concurrent | 3 positions | Hard cap; new entries queue |
| Max margin per trade | 30% of equity | At 5× = 1.5× equity notional |
| Max leverage | 50× | Hard ceiling; practical leverage far lower |
| Daily loss cap | −5% | Blocks new entries until next UTC day |
| Weekly loss cap | −10% | Until next UTC Monday |
| Session floor | −5% | Per-run, resets on restart |
| Symbol cooldown | 15 min | After any losing trade on that symbol |
| Margin mode | Isolated | Loss bounded by posted margin |

All three loss caps persist to disk and survive restarts. Open positions always
run to their own stop — loss caps only block *new* entries.

---

## 🖥️ 6. Control & Deployment

**Telegram** (`telegram_bot.py`) and **Discord** (`discord_bot.py`) — inline
keyboard / slash-command control: live P&L, trade summaries, blacklist toggles,
loss-cap reset, scanner stop/restart, dashboard URL. Push notifications on every
trade event.

**Web dashboard** (`web_server.py`) — FastAPI/Uvicorn on port **8107**, reading
`data/active_state.json`. Tunnelled publicly via **ngrok** (`ngrok_runner.py`).

### systemd services

| Unit | Runs |
|---|---|
| `csb.service` | `live_scanner.py` — the trading engine |
| `csb-bot.service` | `telegram_bot.py` |
| `csb-discord.service` | `discord_bot.py` |
| `csb-web.service` | `web_server.py` |
| `csb-ngrok.service` | `ngrok_runner.py` |

> All units use `Restart=on-failure` with `RestartSec=10`. **Killing a process
> makes systemd respawn it 10 seconds later** — only `systemctl stop` works.
> This matters for Telegram: only one instance may poll a token, and a second
> one gets `409 Conflict`.

Deployment steps, exclusions and pitfalls: **`DEPLOY.md`**.
Development and test tooling: **`Testing Codes/`** (never deployed).

---

## 🔍 7. Design vs. Implementation

Gaps found during the audit. Each is a real discrepancy between this brief's
earlier claims and the code.

| # | Claim | Reality |
|---|---|---|
| 1 | Regime acts as a "master switch" over strategy permissions | `REGIME_STRATEGY_PERMISSIONS` is `True` everywhere — it gates nothing. Only TP reads regime; six of seven ignore it. |
| 2 | CSM "ranks the watchlist … and shorts the weakest" | No ranking, no cross-sectional comparison, no short leg. It is a per-symbol >3×ATR breakout, LONG only. |
| 3 | FF_V2 fades extreme funding rates | It never reads funding. It fades >4% deviation from the 200-EMA. |
| 4 | Elite tier is the resilient, institutional-grade core | LIQ and VRP are two of the three worst performers; LIQ was negative in all five weeks. |
| 5 | WKD is an active production strategy | **It had never opened a single live position.** It read the bar time via `df.index[-1]`, an `int` on the live feed, and threw on every symbol on every scan. Fixed 2026-08-04. |
| 6 | Trade simulation is trustworthy | The backtester compounded without a liquidation floor and reported *−$1,157,620 on a $20 account*. Rewritten. |

### Open issues

- **Watchlist is ~45% tokenised equities.** SNDK, SKHYNIX, SKHY, MU, KORU,
  SPCX, SNXX, SOXS, EWY are stock/ETF tokens that trade only in US cash hours
  and have no perpetual funding. `EXCLUDED_BASES` in `watchlist.py` filters
  TSLA/AAPL/NVDA but not these. Restricting OIB and LIQ to crypto alone would
  remove **−178.5%** of loss with no logic change.
- **No corporate-action handling in the live path.** KORUUSDT redenominated
  ~20:1 on 2026-07-15. The backtester now quarantines this; the live scanner
  does not.
- **Regime instability.** Observed a `BULL_TREND → RANGING` flip in 15 minutes
  with BTC moving −0.12% and ADX flat, driven by ETH oscillating BULL/NEUTRAL
  between cycles. Combined with the 10-minute regime-age gate, the scanner spent
  its first ten cycles refusing to trade.
- **`ngrok_runner.py` has two dead paths on Windows** — the `ngrok.yml` and CLI
  fallbacks invoke a bare `ngrok` that is not on PATH. Only the pyngrok path
  works. Unaffected on Ubuntu where ngrok is installed system-wide.
- **Dashboard displays `ACCOUNT_EQUITY_USDT` from `.env`** rather than the real
  Binance futures balance (which currently reads 0.0000 USDT). The strategy
  panel itself was fixed on 2026-08-04 — it now reads `/api/strategies`, which
  is derived from `StrategyFactory`, so it shows all 7 and cannot drift.
- **`requirements.txt` leaves `pandas`/`numpy` unpinned** — they resolved to
  pandas 3.0.5 / numpy 2.2.6 locally, which may not match the VPS.

---

## 🧪 8. Backtesting

**`backtest_optimizer.py`** — the main harness. Two layers:

1. **Per-trade expectancy** — leverage-independent, net of fees. The real test
   of whether a strategy has edge.
2. **Portfolio simulation** — chronological across all symbols under the live
   risk model (3 slots, margin cap, loss caps, equity floored at zero).

Also does per-1h-bar regime classification with real historical funding,
corporate-action quarantine, crypto vs. equity splits, robustness testing
(does the edge survive removing the best trades?), and derives what
`REGIME_STRATEGY_PERMISSIONS` *should* contain.

```bash
python fetch_binance_data.py                      # 30d klines, top-20
python backtest_optimizer.py                      # full report
python backtest_optimizer.py --crypto-only --slots 5 --no-caps
python run_specific_backtest.py                   # quick check, BTC/ETH/SOL only
```

**`run_specific_backtest.py`** is a thin wrapper over the same engine, hardcoded
to the three majors — a fast sanity check after changing a strategy. Its numbers
are not comparable to the full run (fixed mock regime, three liquid symbols).

---

## 📌 9. Recommended Next Steps

1. **Do not deploy the current config live.** All seven strategies lost 41.32%
   with a 48.0% drawdown over the test month.
2. **Validate across 90–180 days** before acting on any tuning. Everything in §4
   is one in-sample window.
3. **Restrict OIB and LIQ to crypto-native symbols** — removes −178.5% of loss,
   no logic change, justified on first principles.
4. **Revisit the −5% daily cap.** It is the single largest destroyer of edge and
   provides no drawdown benefit at current sizing. Consider measuring it in R
   rather than equity percent.
5. **Rank signals by `strength` before allocating slots.** The field exists and
   is populated; 3 slots are currently handed out by arrival time.
6. **Reduce margin per trade from 30% to 10–15%.** Risk-adjusted return improves
   as size drops (Sharpe rose from 7.81 to 8.39 in testing).
7. **Re-examine LIQ first, then VRP.** Zero and one positive weeks respectively.
8. **Implement CSM's short leg**, or rename it — the current implementation is
   not cross-sectional and is structurally long-biased.

---

## 📝 Changelog — 2026-08-04

- **Fixed:** WKD never fired in production (`df.index[-1]` was an `int` on the
  live feed). Added `bar_time()` to `base_strategy.py`; applied the same fix to
  `overnight_seasonality.py`, `overnight_seasonality_v2.py`, `turn_of_month.py`.
  Regression test: `Testing Codes/test_wkd_fix.py`.
- **Rewritten:** `backtest_optimizer.py` — liquidation floor, chronological
  portfolio ordering, fees on notional, risk model enforced, corporate-action
  quarantine, per-bar regime classification with real funding, strategy × regime
  matrix, CLI flags.
- **Added:** `healthcheck.py`, `DEPLOY.md`, `Testing Codes/`, `run_all.bat`
  (rewritten with venv auto-detect, live-trading confirmation, safe/check/stop
  modes).
- **Fixed — stale strategy lists in THREE places.** All carried the pre-2026 set
  `["TP","FF","DB","GRID","BBR"]` and all failed silently:
  - `live_logger.py` — `_append_strategy_record()` rejects unlisted strategies,
    so per-strategy trade logs were being **discarded for six of the seven**
    strategies in production.
  - `risk_engine.py` — `STRATEGY_LEVERAGE` was missing six production IDs, so
    `get_leverage()` silently returned the default 3× for all of them.
  - `analyze_strategies.py` — `--strategy ALL` iterated the stale list and
    argparse rejected real strategy names, so the per-strategy performance
    report was blind to six of seven.

  All three now derive from `StrategyFactory.get_all()`.
  Regression test: `Testing Codes/test_wiring.py`.
- **Fixed:** dashboard strategy panel was hardcoded HTML showing 4 of 7. Added
  `/api/strategies` (reads the factory, reflects `/disable` overrides) and made
  the panel render from it.
- **Removed dead code:** 9 unused strategy imports from `strategy_factory.py`;
  `ml_analyzer.py`, `symbol_volume.py`, `overnight_seasonality.py` archived to
  `_removed/`; all unused imports cleared across 5 files.
- **Corrected a prior finding:** TP was earlier described as a "fee casualty"
  with positive gross edge. That came from the mock-regime harness. Under real
  per-bar regime its gross expectancy is **−0.424%/trade — negative before
  fees**. It is not fee-limited; the entry logic loses money outright on the
  28-trade sample.
- **Fixed — a FOURTH stale strategy list.** `strategy_overrides.py`
  `KNOWN_STRATEGIES` was also `["TP","FF","DB","GRID","BBR"]`, and
  `set_disabled()` rejects unlisted strategies — so the Telegram/Discord
  "disable strategy" control silently could not turn off six of the seven
  strategies actually trading. Now derived from the factory.
- **Removed TP (Trend Pullback) from production**, by decision, ahead of the
  1-week dry-run test. `trend_pullback.py` is retained unchanged; re-enable by
  re-adding the import and `TrendPullback()` to `_ALL_STRATEGIES`.
- **Removed FF_V2 (Funding Fade V2) from production**, by decision, after
  variant testing found no positive-expectancy configuration. It was the worst
  loss source in both windows (−0.366%/trade 30d, −0.573%/trade 7d), generated
  41–45% of all engine signals — 82% of them re-entries into a stretch that had
  already stopped it out — and its cap-tripping starved CSM of slots.
  Production set is now **5**: LIQ, WKD, VRP, CSM, OIB. Removing it alone
  improved the simulated 30d portfolio from −49.1% to −10.2% under the live
  risk config. `funding_fade_v2.py` retained unchanged.
- **Diagnosed the three losing strategies** (`Testing Codes/diagnose_losers.py`,
  `improve_losers.py`): all three are fade-the-move strategies; 15% of their
  trades were duplicated exposure (62 identical entries across strategies).
  LIQ's loss is entirely its LONG side (PF 0.34 long vs 0.93 short). No FF_V2
  or VRP variant reached positive expectancy; both VRP confirmation filters
  made it worse.
- **Verified:** web dashboard (now 6/6, no console errors), Discord bot,
  Telegram bot and Binance auth all healthy — all files compile, 0 failures.
