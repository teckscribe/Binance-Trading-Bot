"""
tools/replay/driver.py — replay the live scanner over historical data.

Steps simulated time one minute at a time and calls live_scanner's OWN
functions at each step:

    classify_regime()          modules/regime_engine.py
    _scan_for_signals()        live_scanner.py
    _execute_entries()         live_scanner.py  (-> risk_engine, order_engine)
    _manage_positions()        live_scanner.py  (-> strategy.manage())

Nothing about entries, sizing, stops, cooldowns or loss caps is
re-implemented here. Only the loop scaffolding is — the fast/full cycle
decision and the sleep timing, which in live is wall-clock arithmetic and here
is an index over 1m bars. That scaffolding mirrors live_scanner.py's main loop
(the `do_scan = due_for_scan and slots_free` block); everything else comes
from the live modules unchanged.

Resolution: 1 minute. Live manages every FAST_INTERVAL seconds against a mark
tick; the replay manages once per minute against that minute's mark bar. Going
finer is 60x the work and cannot change what it is measuring — the breakeven
leak depends on WHICH MINUTE the clock is in, which 1m data resolves exactly.

    python tools/replay/driver.py --days 30
    python tools/replay/driver.py --days 30 --symbols BTCUSDT,ETHUSDT --limit-hours 6
"""

import os
import sys
import json
import time
import argparse
import logging
from collections import Counter, defaultdict

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from tools.replay.store import ReplayStore
from tools.replay.sandbox import Sandbox, assert_production_untouched, verify


def _atomic_write(path, obj):
    """Write JSON via a temp file + rename, so a reader never sees half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, default=str, indent=1)
    os.replace(tmp, path)


def _fmt(sec):
    sec = int(max(0, sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def daily_rows(closed, starting_equity):
    """
    Per simulated day: trades, P&L, running equity, exit mix.

    A 30-day replay is hours long, so a run that is only 60% done still has to
    be worth reading. Bucketing by exit date means an interrupted run yields a
    complete, honest record of the days it did finish rather than one
    all-or-nothing number at the end.
    """
    by = defaultdict(list)
    for t in closed:
        et = t.get("exit_time")
        if not et:
            continue
        by[str(pd.Timestamp(et).date())].append(t)

    rows, cum = [], 0.0
    for day in sorted(by):
        v = by[day]
        net = sum(float(x.get("pnl_usdt_net", 0.0)) for x in v)
        cum += net
        wins = sum(1 for x in v if float(x.get("pnl_usdt_net", 0.0)) > 0)
        rows.append({
            "date": day,
            "trades": len(v),
            "net_usdt": round(net, 4),
            "cum_net_usdt": round(cum, 4),
            "equity": round(starting_equity + cum, 4),
            "win_pct": round(wins / len(v) * 100, 1),
            "exit_mix": dict(Counter(x.get("exit_reason", "?") for x in v)),
        })
    return rows


class Progress:
    """
    Status the operator can read at any moment, without tailing a log.

    A 30-day / 100-symbol run takes ~4-5 hours, so 'is it alive and how far in'
    has to be answerable with a single cat. Written atomically because it will
    be read while the run is mid-write.
    """

    def __init__(self, path, start, end, total, symbols, equity):
        self.path, self.total = path, total
        self.t0 = time.monotonic()
        self.base = {
            "status": "running",
            "started_utc": pd.Timestamp.now('UTC').isoformat(),
            "sim_from": str(start), "sim_to": str(end),
            "total_sim_minutes": total,
            "symbols": len(symbols),
            "starting_equity": equity,
        }
        self.write(0, start, 0, 0, equity, {})

    def write(self, step, now, closed, n_open, equity, mix, status="running",
              daily=None):
        el = time.monotonic() - self.t0
        pct = step / self.total * 100 if self.total else 0.0
        eta = (el / step * (self.total - step)) if step else None
        rec = dict(self.base)
        rec.update({
            "status": status,
            "updated_utc": pd.Timestamp.now('UTC').isoformat(),
            "sim_now": str(now),
            "step": step, "pct_complete": round(pct, 2),
            "elapsed": _fmt(el), "elapsed_sec": round(el),
            "eta": _fmt(eta) if eta is not None else None,
            "eta_sec": round(eta) if eta is not None else None,
            "trades_closed": closed, "open_positions": n_open,
            "equity": round(equity, 4),
            "net_pnl": round(equity - self.base["starting_equity"], 4),
            "exit_mix": mix,
            "daily": daily or [],
        })
        try:
            _atomic_write(self.path, rec)
        except Exception:
            pass          # never let status writing kill the run
        return rec

    def finish(self, **kw):
        self.write(status="finished", **kw)


def main():
    ap = argparse.ArgumentParser(description="Replay the live scanner over history")
    ap.add_argument("--days", type=int, default=30, help="which 1m dataset to load")
    ap.add_argument("--symbols", default=None, help="comma-separated; default = all with data")
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--limit-hours", type=float, default=None,
                    help="replay only the first N hours (smoke test)")
    ap.add_argument("--last-days", type=int, default=None,
                    help="replay only the last N days of available data")
    ap.add_argument("--scratch", default=os.path.join(_HERE, "_run"))
    ap.add_argument("--data-dir", default="data",
                    help="where the 1m/mark CSVs live (default: ./data)")
    ap.add_argument("--out", default=None, help="write trades to this JSON")
    ap.add_argument("--skip-drift-check", action="store_true",
                    help="allow running from a tree that differs from production")
    ap.add_argument("--progress-every", type=int, default=360,
                    help="log + write progress every N simulated minutes "
                         "(default 360 = every 6 simulated hours)")
    ap.add_argument("--progress-file", default=None,
                    help="JSON status file, rewritten each progress tick "
                         "(default: <scratch>/progress.json)")
    ap.add_argument("--checkpoint-every", type=int, default=1440,
                    help="save resumable state every N simulated minutes; "
                         "0 disables")
    ap.add_argument("--resume", action="store_true",
                    help="continue from the last checkpoint if one exists")
    args = ap.parse_args()

    # ── Refuse to measure stale code ────────────────────────────────────────
    if not args.skip_drift_check:
        try:
            from tools.replay import sync
            if os.path.isdir(sync.PROD) and os.path.abspath(sync.PROD) != \
                    os.path.abspath(os.path.dirname(os.path.dirname(_HERE))):
                sync.require_clean()
        except SystemExit:
            raise
        except Exception as exc:
            print(f"  (drift check skipped: {exc})")

    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, force=True)
    logging.disable(logging.INFO)

    # ── Symbols ─────────────────────────────────────────────────────────────
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        import glob
        symbols = sorted(
            os.path.basename(p).split(f"_1m_{args.days}d")[0]
            for p in glob.glob(os.path.join(args.data_dir, f"*_1m_{args.days}d.csv")))
    if not symbols:
        sys.exit(f"No 1m data in {args.data_dir}. Run: python tools/fetch_1m.py --days {args.days}")

    print(f"Loading {len(symbols)} symbols x {args.days}d of 1m data...")
    t0 = time.monotonic()
    store = ReplayStore(symbols, args.days, data_dir=args.data_dir)
    print(f"  loaded in {time.monotonic() - t0:.1f}s")

    start = pd.Timestamp(store.first_ns, tz="UTC")
    end = pd.Timestamp(store.last_ns, tz="UTC")
    # Warm-up: strategies need ~25 hourly bars, and the regime classifier needs
    # its own history, so the first entry cannot be at bar zero.
    start = start + pd.Timedelta(hours=30)
    if args.last_days:
        start = max(start, end - pd.Timedelta(days=args.last_days))
    if args.limit_hours:
        end = min(end, start + pd.Timedelta(hours=args.limit_hours))
    total_min = int((end - start).total_seconds() // 60)
    print(f"Replaying {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC "
          f"({total_min} simulated minutes)\n")

    snap = assert_production_untouched()

    with Sandbox(store, start, args.scratch, symbols, args.equity) as sb:
        import live_scanner as LS
        from modules.regime_engine import REGIME_STRATEGY_PERMISSIONS
        from modules.order_engine import OrderEngine
        from live_logger import LiveLogger
        from modules.strategies.strategy_factory import StrategyFactory

        LS.ACCOUNT_EQUITY = args.equity
        LS.PAPER_STARTING_EQUITY = args.equity
        LS._symbol_loss_cooldown.clear()

        live = OrderEngine()
        logger = LiveLogger()

        regime = {"regime": "RANGING", "funding": 0.0}
        current_regime = "RANGING"
        LS._regime_changed_at = start

        last_scan = -10 ** 9          # force a scan on the first cycle
        SCAN_INTERVAL = LS.SCAN_INTERVAL
        t_run = time.monotonic()
        cycles = scans = 0
        step0 = 0

        prog_path = args.progress_file or os.path.join(args.scratch, "progress.json")
        ckpt_path = os.path.join(args.scratch, "checkpoint.json")
        out_path  = args.out or os.path.join(args.scratch, f"replay_{args.days}d.json")
        daily_path = os.path.join(os.path.dirname(out_path) or ".",
                                  "replay_daily.json")

        # ── Resume ──────────────────────────────────────────────────────────
        # A 4-5 hour run losing everything to an OOM at hour 4 is the failure
        # mode worth protecting against. State restored here is exactly the
        # state the loop mutates: open/closed positions, session P&L, equity,
        # per-symbol cooldowns and the regime clock. Anything outside that list
        # (the loss tracker, strategy overrides) already persists to the
        # scratch dir on its own.
        if args.resume and os.path.exists(ckpt_path):
            try:
                ck = json.load(open(ckpt_path))
                step0 = int(ck["step"]) + 1
                live.active = ck["active"]
                live.closed = ck["closed"]
                live.session_pnl_pct = float(ck["session_pnl_pct"])
                LS.ACCOUNT_EQUITY = float(ck["equity"])
                LS._symbol_loss_cooldown.clear()
                for s, t in ck.get("cooldown", {}).items():
                    LS._symbol_loss_cooldown[s] = pd.Timestamp(t).to_pydatetime()
                current_regime = ck.get("regime", "RANGING")
                LS._regime_changed_at = pd.Timestamp(ck["regime_changed_at"]).to_pydatetime()
                last_scan = int(ck.get("last_scan", -10 ** 9))
                print(f"RESUMED from checkpoint at step {step0}/{total_min} "
                      f"({step0/total_min*100:.1f}%), {len(live.closed)} trades, "
                      f"equity ${LS.ACCOUNT_EQUITY:.2f}\n")
            except Exception as exc:
                print(f"  ! checkpoint unreadable ({exc}) — starting fresh\n")
                step0 = 0

        def save_checkpoint(step, now):
            try:
                _atomic_write(ckpt_path, {
                    "step": step, "sim_now": str(now),
                    "active": live.active, "closed": live.closed,
                    "session_pnl_pct": live.session_pnl_pct,
                    "equity": LS.ACCOUNT_EQUITY,
                    "cooldown": {s: t.isoformat()
                                 for s, t in LS._symbol_loss_cooldown.items()},
                    "regime": current_regime,
                    "regime_changed_at": str(LS._regime_changed_at),
                    "last_scan": last_scan,
                })
            except Exception as exc:
                print(f"  ! checkpoint save failed: {exc}")

        prog = Progress(prog_path, start, end, total_min, symbols, args.equity)
        print(f"progress   -> {prog_path}")
        print(f"daily      -> {daily_path}")
        print(f"trades     -> {out_path}  (rewritten each simulated day)")
        print(f"checkpoint -> {ckpt_path}"
              + ("" if args.checkpoint_every else "  (disabled)") + "\n")

        prev_day = (start + pd.Timedelta(minutes=max(0, step0 - 1))).strftime("%Y-%m-%d")
        for step in range(step0, total_min):
            now = start + pd.Timedelta(minutes=step)
            sb.set_now(now)
            cycles += 1

            # ── day-boundary reset ─────────────────────────────────────────
            # In production the bot restarts at least daily, which zeroes
            # session_pnl_pct. Without this reset the SESSION_LOSS_FLOOR
            # (-5%) accumulates across the entire replay and permanently
            # locks out entries after the first bad streak.
            cur_day = now.strftime("%Y-%m-%d")
            if cur_day != prev_day:
                live.session_pnl_pct = 0.0
                prev_day = cur_day

            # ── regime, on the full-cycle cadence (live: once per full cycle)
            slots_free = live.n_open() < LS.MAX_CONCURRENT
            due = (step - last_scan) >= (SCAN_INTERVAL // 60)
            do_scan = due and slots_free
            if do_scan:
                last_scan = step

            open_syms = [p["symbol"] for p in live.active]

            if do_scan:
                btc = LS.fetch_btc_reference()
                regime = LS.classify_regime(btc_1h=btc.get("btc_1h"),
                                            eth_1h=btc.get("eth_1h"),
                                            sol_1h=btc.get("sol_1h"))
                new_regime = regime.get("regime", "RANGING")
                if new_regime != current_regime:
                    current_regime = new_regime
                    LS._regime_changed_at = now

                permitted = StrategyFactory.get_permitted(
                    regime, REGIME_STRATEGY_PERMISSIONS)
                need_1h = any(getattr(s, "REQUIRES_1H", False) for s in permitted)
                scan_syms = list(symbols)
                symbol_data = LS.fetch_all_symbols(
                    scan_syms, regime=current_regime, need_1h=need_1h)
            else:
                symbol_data = LS.fetch_active_positions_data(open_syms)

            # ── manage, every cycle (live does this at FAST_INTERVAL) ───────
            LS._manage_positions(live, symbol_data, logger)

            # ── scan + enter, full cycles only ──────────────────────────────
            if do_scan:
                cap_hit, _ = LS.is_loss_cap_hit()
                if not cap_hit and not LS.is_daily_floor_hit(live.session_pnl_pct):
                    signals = LS._scan_for_signals(symbols, symbol_data, regime, live)
                    if signals:
                        LS._execute_entries(signals, live, logger, regime,
                                            symbol_data=symbol_data)
                scans += 1

            if args.progress_every and step and step % args.progress_every == 0:
                mix = dict(Counter(t.get("exit_reason", "?") for t in live.closed))
                r = prog.write(step, now, len(live.closed), live.n_open(),
                               LS.ACCOUNT_EQUITY, mix,
                               daily=daily_rows(live.closed, args.equity))
                print(f"  [{r['pct_complete']:>5.1f}%] {now:%m-%d %H:%M} | "
                      f"closed={len(live.closed):>4} open={live.n_open()} | "
                      f"equity ${LS.ACCOUNT_EQUITY:>7.2f} "
                      f"({r['net_pnl']:+.2f}) | {r['elapsed']} elapsed, "
                      f"{r['eta']} left")

            if args.checkpoint_every and step and step % args.checkpoint_every == 0:
                save_checkpoint(step, now)
                # Dump what has completed so far. An interrupted run is then
                # still fully analysable with tools/replay/analyze.py — the
                # days that finished are real results, not a lost run.
                try:
                    _atomic_write(out_path, live.closed)
                    rows = daily_rows(live.closed, args.equity)
                    _atomic_write(daily_path, rows)
                    if rows:
                        d = rows[-1]
                        print(f"    day {d['date']}: {d['trades']:>3} trades  "
                              f"{d['net_usdt']:+7.2f} USDT  "
                              f"(cum {d['cum_net_usdt']:+7.2f}, "
                              f"equity ${d['equity']:.2f})")
                except Exception as exc:
                    print(f"  ! partial dump failed: {exc}")

        # ── results ─────────────────────────────────────────────────────────
        closed = list(live.closed)
        el = time.monotonic() - t_run
        prog.write(total_min, end, len(closed), live.n_open(), LS.ACCOUNT_EQUITY,
                   dict(Counter(t.get("exit_reason", "?") for t in closed)),
                   status="finished", daily=daily_rows(closed, args.equity))
        _atomic_write(daily_path, daily_rows(closed, args.equity))
        print(f"\nReplay finished in {el/60:.1f} min "
              f"({cycles} cycles, {scans} full scans)")

    bad = verify(snap)
    print(f"production files modified: {bad if bad else 'NONE'}")

    if not closed:
        print("\nNo trades taken.")
        return

    net = [t.get("pnl_usdt_net", 0.0) for t in closed]
    wins = [x for x in net if x > 0]
    mix = Counter(t.get("exit_reason", "?") for t in closed)
    print(f"\n{'='*70}\nREPLAY RESULT — {len(closed)} trades\n{'='*70}")
    print(f"  net P&L        {sum(net):+.2f} USDT on ${args.equity:.0f} "
          f"({sum(net)/args.equity*100:+.2f}%)")
    print(f"  win rate       {len(wins)/len(closed)*100:.1f}%")
    print(f"  mean/trade     {sum(net)/len(net):+.4f} USDT")
    print(f"  exit mix       " + "  ".join(
        f"{k}={v/len(closed)*100:.0f}%" for k, v in mix.most_common()))

    out = args.out or os.path.join(args.scratch, f"replay_{args.days}d.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(closed, f, default=str, indent=1)
    print(f"\n  trades -> {out}")


if __name__ == "__main__":
    main()
