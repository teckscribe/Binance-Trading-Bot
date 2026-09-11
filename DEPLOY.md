# Deploying to the Ubuntu VPS

Target: `/home/psms/ubuntu/program_files/csb`

---

## Do NOT copy these

| Path | Why |
|---|---|
| `Testing Codes/` | **Dev tooling and test harnesses only.** Nothing the bot needs at runtime. See its README. |
| `.venv/` | **536 MB of Windows binaries.** Broken on Linux. Build a fresh venv on the VPS. |
| `_removed/` | Staging folder from cleanup — delete once you've checked it. |
| `__pycache__/` | Windows bytecode. Regenerates itself. |
| `run_all.bat` | Windows-only launcher; systemd runs the services on Ubuntu. |

## Two things that will bite on a straight copy

**1. The venv must be named `venv`, not `.venv`.** The systemd units hardcode it:

```
ExecStart=/home/psms/ubuntu/program_files/csb/venv/bin/python3 telegram_bot.py
```

The local one is `.venv` (with a dot) because that's the Python convention on Windows. On the VPS, create `venv`.

**2. `data/` runtime state was deliberately left out.** `loss_tracker.json`, `watchlist.json`, `active_state.json` and `binance_status_cache.json` were moved to `_removed/`. Copying the local copies onto the VPS would **reset your live daily/weekly loss caps** and overwrite the VPS watchlist. All four are recreated automatically on first run.

---

## Copy

From Windows (PowerShell), excluding what shouldn't travel:

```bash
scp -r $(Get-ChildItem -Exclude .venv,_removed,'Testing Codes',__pycache__,run_all.bat | % FullName) psms@VPS:/home/psms/ubuntu/program_files/csb/
```

Or from WSL / Git Bash with rsync, which is cleaner:

```bash
rsync -avz --exclude='.venv' --exclude='_removed' --exclude='Testing Codes' --exclude='__pycache__' --exclude='*.bat' ./ psms@VPS:/home/psms/ubuntu/program_files/csb/
```

What actually ships: the 5 service entry points, `modules/`, `static/`,
`requirements.txt`, the 5 `.service` units, `ngrok.yml`, `.env`,
`backtest_optimizer.py`, `fetch_binance_data.py` and the pre-existing helper
scripts. Empty `data/` and `logs/live/`.

---

## On the VPS

```bash
cd /home/psms/ubuntu/program_files/csb
python3 -m venv venv
venv/bin/python3 -m pip install --upgrade pip
venv/bin/python3 -m pip install -r requirements.txt
chmod 600 .env
venv/bin/python3 healthcheck.py
```

`healthcheck.py` byte-compiles every file, imports every module, validates the
env vars, and probes Binance / Telegram / Discord. Expect `0 fail` before
starting any service.

### Pin your dependencies first

`requirements.txt` leaves `pandas` and `numpy` unpinned. On this machine they
resolved to **pandas 3.0.5 / numpy 2.2.6**, which is almost certainly not what
the VPS currently runs. Either pin them to match production, or accept that the
VPS will now upgrade. Check what is installed there before you install:

```bash
venv/bin/python3 -c "import pandas, numpy; print(pandas.__version__, numpy.__version__)"
```

---

## Start services

```bash
sudo systemctl daemon-reload
sudo systemctl start csb-web csb-ngrok csb-bot csb-discord csb
systemctl is-active csb csb-bot csb-discord csb-web csb-ngrok
```

Stopping them — note `Restart=on-failure` in the unit files means **killing a
process makes systemd respawn it 10 seconds later**. Only a unit stop works:

```bash
sudo systemctl stop csb csb-bot csb-discord csb-web csb-ngrok
```

Only one Telegram bot may poll a token at a time. If a local instance is also
running you get `409 Conflict` and neither works reliably.

---

## Optional: backtest data

`data/` ships empty. To re-run backtests on the VPS:

```bash
venv/bin/python3 fetch_binance_data.py       # 30d klines for the top-20
venv/bin/python3 backtest_optimizer.py       # full report
```

---

## Changed in this pass

- **`modules/strategies/weekend_anomaly.py`** — WKD read the bar time via
  `df.index[-1]`, which is an `int` on the live feed (`data_feed` puts the
  datetime in a `timestamp` column and resets the index). It threw on every
  symbol on every scan and **had never opened a live position**. Now uses
  `bar_time()`. Same latent fix applied to `overnight_seasonality.py`,
  `overnight_seasonality_v2.py`, `turn_of_month.py`.
- **`modules/strategies/base_strategy.py`** — added `bar_time(df)`, which reads
  either frame layout.
- **`backtest_optimizer.py`** — rewritten. Fixes unbounded compounding (the old
  one reported −$1,157,620 on a $20 account), non-chronological ordering, fees
  charged against the leveraged return instead of notional, and an ignored risk
  model. Adds per-bar regime classification with real funding rates, corporate
  action quarantine, and a strategy × regime matrix.
- **`healthcheck.py`**, **`test_wkd_fix.py`** — new.
