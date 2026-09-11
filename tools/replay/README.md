# Tier 2 replay harness

Runs **production's own live_scanner code** over historical 1-minute data, in a
tree that cannot touch production state.

## Why it exists

`backtest_optimizer.py` and `live_scanner.py` were two separate implementations
of the same logic. They drifted for months, and the drift was invisible until
measured: the backtest sliced its 1h frame on the 15m bar's *open* time, handing
strategies an hourly bar that had not closed yet. On 90 days / 9,562 trades that
one defect was the difference between

```
+0.632%/trade  PF 1.74  ->  +6254% account     (as the harness ran)
-0.046%/trade  PF 0.96  ->    -57% account     (look-ahead removed)
```

Tier 1 fixed that inside `backtest_optimizer.py`. Tier 2 removes the duplication
itself: the replay calls `_scan_for_signals()`, `_execute_entries()` and
`_manage_positions()` directly, so entries, sizing, stops, cooldowns and loss
caps cannot diverge from live — they *are* live.

## Layout on the Ubuntu box

```
/home/psms/ubuntu/program_files/csb          production — never written to
/home/psms/ubuntu/program_files/test   this tree
    ├── modules/ live_scanner.py ...         synced, hash-verified vs production
    ├── tools/replay/                        replay-only
    ├── data/  logs/  .env                   entirely its own
    └── venv/
```

## Two independent safety layers

**1. `sandbox.py`** monkeypatches at the replay boundary — clock, data source,
state-file paths, notifications, and the three Binance calls that have no
`LIVE_ENABLED` guard (`_get_contract_spec`, `_get_binance_open_positions`,
`_get_account_equity`). It also **blocks `requests` entirely**, so any unpatched
path fails loudly instead of silently returning a live value.

That guard exists because of a real bug found during the build:
`regime_engine.py:60` does `from modules.data_feed import fetch_funding_rate`,
binding the name into its own namespace. Patching `data_feed` never reached it,
so `classify_regime()` was fetching **today's** funding rate and feeding it into
historical regime decisions — silently, because the value looked plausible.

**2. systemd + `sync.py`.** `ReadOnlyPaths` on the production tree means the
kernel denies a write even if the sandbox has a hole. `sync.py --verify` runs as
`ExecStartPre` and aborts the job if any of the 24 tracked decision-making files
differs from production.

## Setup

The tree is a full copy of production at
`/home/psms/ubuntu/program_files/test`, so the code is already in place. What a
copy does NOT give you is a clean slate: it also copied production's
`data/loss_tracker.json`, `data/loss_cooldown.json`, `data/paper_equity.json`
and `logs/strategies/`. `sandbox.py` redirects every read and write to a scratch
directory so none of them are used, but leaving them there invites a later
reader to mistake production's paper history for replay output. Clear them:

```bash
cd /home/psms/ubuntu/program_files/test
rm -f data/loss_tracker.json data/loss_cooldown.json \
      data/paper_equity.json data/active_state.json
rm -rf logs/live logs/strategies
mkdir -p data logs

# funding history is NEEDED, so keep/copy that one
cp -r ../csb/data/funding data/ 2>/dev/null

# a copied venv has absolute paths baked in — rebuild it
rm -rf venv && python3 -m venv venv
./venv/bin/pip install -r requirements.txt

sed -i 's/^LIVE_ENABLED=.*/LIVE_ENABLED=false/' .env

# prove the copy still matches production byte-for-byte
./venv/bin/python tools/replay/sync.py --verify
```

Fetch the data (~30 min, 5,800 requests, rate-gated to 200/min so it will not
disturb the live scanner sharing the IP):

```bash
./venv/bin/python tools/fetch_1m.py --days 30
```

**BTCUSDT, ETHUSDT and SOLUSDT are mandatory** — `classify_regime()` needs all
three, and without them every bar falls back to `RANGING`.

## Run

```bash
sudo cp tools/replay/csb-replay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start csb-replay          # oneshot, ~4-5 h for 30d x 100 symbols
journalctl -u csb-replay -f
```

Or directly, for a smoke test:

```bash
./venv/bin/python tools/replay/driver.py --days 30 --limit-hours 12
```

## Reading the output

Runtime is ~4-5 h for 30 days x 100 symbols on the i3-6100. `Nice=15` and
`CPUWeight=20` keep the live scanner ahead of it.

Two traps this project has already fallen into — check both before believing a
result:

- **Symbol count changes the answer.** 3 slots against ~1,300 slot-rejected
  signals means which trades get taken depends on the whole queue. The same
  30 days scored −0.9% at 20 symbols and −26.1% at 100. Never report a reduced
  universe.
- **Short windows lie.** A 10-day sweep said widening slots turned −5.9% into
  +31.6%; at 30 and 90 days every such configuration was negative and the
  current one was the least-bad of sixteen. Do not derive configuration from
  under 30 days; prefer 60-90.

## Files

| file | role |
|---|---|
| `store.py` | historical data served as-of sim-time, with a genuinely **forming** last bar. 15m/1h derived from 1m — validated identical to Binance's own klines (0.00e+00 rel. diff) |
| `sandbox.py` | clock, data, state-path and network interception |
| `sync.py` | hash-verified code parity with production |
| `driver.py` | the cycle loop; mirrors `live_scanner.main()`'s fast/full decision |
| `csb-replay.service` | oneshot unit, resource-capped, production tree read-only |

## Known limits

- **1-minute management resolution.** Live manages every `FAST_INTERVAL` second
  against a mark tick; the replay manages once per minute against that minute's
  mark bar. Enough to reproduce the breakeven leak (which depends on *which
  minute* the clock is in), not enough to model sub-minute stop sequencing.
- **Slippage is not modelled** here — set `SLIPPAGE_PCT` in `.env`, which
  Tier 1's `backtest_optimizer.py` already reads, and wire it in before quoting
  absolute returns.
- **Survivorship.** The watchlist is today's top-volume symbols, not the
  historical one. Structural; cannot be fixed from cached klines.
- **`live_logger.py` writes TXT without `encoding="utf-8"`**, so its `−`
  (U+2212) throws `charmap` errors on Windows. Harmless under Ubuntu's UTF-8
  locale; the JSON logs are unaffected either way.
