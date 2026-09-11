"""
tools/replay/sandbox.py — make a replay incapable of touching production.

The replay drives the REAL live modules, which means it inherits their real
side effects. Left unpatched, a replay would:

  * write simulated losses into data/loss_tracker.json, which is the persistent
    daily/weekly loss cap the live bot reads on startup
  * write data/loss_cooldown.json, blocking real symbols
  * write data/active_state.json, so the dashboard renders replay positions
  * overwrite data/paper_equity.json, corrupting the running paper balance
  * append to logs/strategies/<ID>/, destroying the ONLY independent
    out-of-sample record of how the live engine actually performed
  * fire Telegram and Discord notifications for every simulated trade
  * call Binance for exchangeInfo, open positions and balance

That last log point is the one worth stressing: those trade logs are evidence,
not output. They cannot be regenerated.

Everything here is monkeypatching applied at the replay boundary. No file on
the live trading path is modified, so the running bot is unaffected whether it
is up or down.

Usage:
    with Sandbox(store, start_ts, scratch_dir) as sb:
        ...                      # drive live_scanner's functions
        sb.set_now(ts)           # advance simulated time
"""

import os
import shutil
import datetime as _dt

import pandas as pd


class SimDatetime(_dt.datetime):
    """
    Drop-in for datetime.datetime whose now()/utcnow() return simulated time.

    live_scanner, order_engine and risk_engine all do
    `from datetime import datetime`, binding the CLASS into their module
    namespace. Rebinding that name per module is enough — and is far safer
    than patching datetime globally, which would break pandas.
    """
    _now = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        n = cls._now
        return n.astimezone(tz) if tz else n.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls._now.replace(tzinfo=None)


class Sandbox:
    def __init__(self, store, start, scratch_dir, symbols=None, equity=100.0,
                 funding_csv="data/funding/BTCUSDT.csv"):
        self.store = store
        self.scratch = os.path.abspath(scratch_dir)
        self.symbols = symbols or store.symbols
        self.equity = equity
        self._start = start
        self._undo = []
        self._now = start

        # Historical BTC funding, loaded BEFORE any patching so the fetch (if
        # any) happens outside the no-network guard. classify_regime() reads
        # this to decide OVERHEATED / OVERSOLD.
        self._fund_ts, self._fund_v = None, None
        if os.path.exists(funding_csv):
            f = pd.read_csv(funding_csv, parse_dates=["fundingTime"])
            f = f.sort_values("fundingTime")
            self._fund_ts = f["fundingTime"].values.astype("datetime64[ns]").astype("int64")
            self._fund_v = f["fundingRate"].astype(float).to_numpy()
        else:
            print(f"  ! no funding history at {funding_csv} — funding will read "
                  f"0.0, so OVERHEATED/OVERSOLD can never trigger")

    def _funding_as_of(self, symbol=None):
        """Most recent funding stamp at or before sim-now. Never the live rate."""
        if self._fund_ts is None:
            return 0.0
        import numpy as _np
        i = _np.searchsorted(self._fund_ts, self.now_ns, "right") - 1
        return float(self._fund_v[i]) if i >= 0 else 0.0

    # ── time ────────────────────────────────────────────────────────────────
    def set_now(self, ts: pd.Timestamp):
        self._now = ts
        SimDatetime._now = ts.to_pydatetime()

    @property
    def now_ns(self) -> int:
        return int(self._now.value)

    # ── patching helpers ────────────────────────────────────────────────────
    def _set(self, mod, name, value):
        had = hasattr(mod, name)
        old = getattr(mod, name, None)
        setattr(mod, name, value)
        self._undo.append((mod, name, old, had))

    def __enter__(self):
        if os.path.exists(self.scratch):
            shutil.rmtree(self.scratch)
        os.makedirs(os.path.join(self.scratch, "data"), exist_ok=True)
        os.makedirs(os.path.join(self.scratch, "logs", "live"), exist_ok=True)

        import live_scanner as LS
        import live_logger as LL
        import modules.risk_engine as RE
        import modules.order_engine as OE
        import modules.paper_equity as PE
        import modules.data_hub as DH
        import modules.data_feed as DF
        import modules.regime_engine as RG
        import telegram_notifier as TG
        import discord_notifier as DC

        d = lambda *p: os.path.join(self.scratch, "data", *p)

        # ── 1. clock ────────────────────────────────────────────────────────
        for mod in (LS, OE, LL, RG):
            self._set(mod, "datetime", SimDatetime)
        self.set_now(self._start)

        # risk_engine builds its day/week bucket keys from datetime.now().
        # It imports datetime INSIDE the module body (line ~398), so patch the
        # two helpers directly rather than the class.
        self._set(RE, "_utc_today", lambda: SimDatetime._now.strftime("%Y-%m-%d"))
        self._set(RE, "_utc_week", lambda: (
            f"{SimDatetime._now.isocalendar()[0]}-"
            f"W{SimDatetime._now.isocalendar()[1]:02d}"))

        # ── 2. state files -> scratch ───────────────────────────────────────
        self._set(RE, "_LOSS_FILE", d("loss_tracker.json"))
        self._set(LS, "_COOLDOWN_FILE", d("loss_cooldown.json"))
        self._set(PE, "_STATE_FILE", d("paper_equity.json"))
        self._set(LL, "LOG_DIR", os.path.join(self.scratch, "logs", "live"))
        self._set(LL, "STRATEGY_DIR", os.path.join(self.scratch, "logs", "strategies"))

        # ── 3. notifications off ────────────────────────────────────────────
        noop = lambda *a, **k: None
        for mod in (TG, DC):
            for fn in [n for n in dir(mod) if n.startswith("notify_")]:
                self._set(mod, fn, noop)
        for fn in [n for n in dir(LS) if n.startswith("notify_")]:
            self._set(LS, fn, noop)

        # ── 4. unguarded Binance calls ──────────────────────────────────────
        # These three have no LIVE_ENABLED guard and would hit the network.
        self._set(OE, "_get_binance_open_positions", lambda: {})
        self._set(OE, "_get_account_equity", lambda: self.equity)
        self._set(OE, "get_account_equity", lambda: self.equity)
        # exchangeInfo is effectively static; serve a permissive spec so
        # quantity rounding never rejects a simulated order for lot size.
        self._set(OE, "_get_contract_spec",
                  lambda symbol: {"step": 1e-8, "tick": 1e-8, "min_qty": 0.0})

        # ── 5. data feed -> the historical store ────────────────────────────
        st = self.store

        def fetch_candles(symbol, timeframe="1m", limit=200):
            return st.candles(symbol, timeframe, limit, self.now_ns)

        def fetch_mark_price(symbol):
            return st.mark(symbol, self.now_ns)

        self._set(DF, "fetch_candles", fetch_candles)
        self._set(DF, "fetch_mark_price", fetch_mark_price)
        self._set(DF, "fetch_funding_rate", lambda s: 0.0)
        self._set(DF, "fetch_open_interest", lambda s: None)
        self._set(DH, "fetch_candles", fetch_candles)
        self._set(DH, "fetch_mark_price", fetch_mark_price)

        # data_hub's own fetchers, matching the limits live actually requests:
        # 1m=200, 15m=200 (fetch_all_symbols) and 1h=50 (_get_1h).
        def fetch_all_symbols(symbols, max_workers=None, regime="", need_1h=None):
            out = {}
            for s in symbols:
                df1 = fetch_candles(s, "1m", 200)
                if df1 is None or df1.empty:
                    continue
                out[s] = (df1,
                          fetch_candles(s, "15m", 200),
                          fetch_candles(s, "1h", 50) if need_1h else pd.DataFrame())
            return out

        def fetch_active_positions_data(symbols):
            out = {}
            for s in symbols:
                df1 = fetch_candles(s, "1m", 200)
                if df1 is None or df1.empty:
                    continue
                mark = fetch_mark_price(s)
                if mark:
                    # Mirrors data_hub: a synthetic volume-0 tick row carrying
                    # the mark price, with high/low widened so an intra-minute
                    # touch of SL/TP is visible. Uses the MARK bar's own
                    # extremes, which is the series the exchange triggers on.
                    rng = st.mark_range(s, self.now_ns)
                    last = df1.iloc[-1]
                    hi, lo = (rng if rng else (mark, mark))
                    row = {"timestamp": pd.Timestamp(self._now),
                           "open": mark,
                           "high": max(float(last["high"]), hi),
                           "low": min(float(last["low"]), lo),
                           "close": mark, "volume": 0.0}
                    df1 = pd.concat([df1, pd.DataFrame([row])], ignore_index=True)
                out[s] = (df1, pd.DataFrame(), pd.DataFrame())
            return out

        def fetch_btc_reference():
            return {"btc_1h": fetch_candles("BTCUSDT", "1h", 50),
                    "eth_1h": fetch_candles("ETHUSDT", "1h", 50),
                    "sol_1h": fetch_candles("SOLUSDT", "1h", 50),
                    "funding": 0.0, "oi": None}

        self._set(DH, "fetch_all_symbols", fetch_all_symbols)
        self._set(DH, "fetch_active_positions_data", fetch_active_positions_data)
        self._set(DH, "fetch_btc_reference", fetch_btc_reference)
        for name, fn in (("fetch_all_symbols", fetch_all_symbols),
                         ("fetch_active_positions_data", fetch_active_positions_data),
                         ("fetch_btc_reference", fetch_btc_reference)):
            self._set(LS, name, fn)

        # ── 5b. regime_engine's OWN bindings ────────────────────────────────
        # regime_engine.py:60 does `from modules.data_feed import fetch_candles,
        # fetch_funding_rate`, which copies those names into its namespace at
        # import time. Patching data_feed therefore never reached it, and
        # classify_regime() calls fetch_funding_rate(BTC_SYM) unconditionally —
        # so the replay was fetching TODAY'S funding rate over the network and
        # feeding it into a historical regime decision. A look-ahead of exactly
        # the kind this harness exists to eliminate, and 80% of its runtime.
        self._set(RG, "fetch_candles", fetch_candles)
        self._set(RG, "fetch_funding_rate", self._funding_as_of)
        # Module-level hysteresis state persists across runs; reset it so a
        # replay never inherits a regime from a previous one.
        self._set(RG, "_current_regime", "")
        self._set(RG, "_regime_set_at", None)

        # ── 5c. no network, at all ──────────────────────────────────────────
        # The funding-rate leak above was silent: it returned a plausible
        # number, so nothing failed and the result would have looked fine.
        # Any remaining unpatched call must therefore be LOUD. Every legitimate
        # data path is served from the store by this point, so reaching the
        # network can only mean a live value is contaminating the replay.
        import requests as _rq

        def _blocked(*a, **k):
            url = a[0] if a else k.get("url", "?")
            raise RuntimeError(
                f"Replay attempted a live network call to {url}. Something is "
                f"not patched — a live value would have contaminated this run. "
                f"Patch the caller's own module binding (see 5b for why "
                f"patching data_feed alone is not enough).")

        for fn in ("get", "post", "put", "delete", "request", "head", "patch"):
            self._set(_rq, fn, _blocked)
        self._set(_rq.Session, "request", _blocked)

        # ── 6. ML side effects off (Phase 1 only logs, but it writes JSONL) ──
        try:
            import modules.ml_engine as ML
            self._set(ML, "commit_ml_signal", noop)
            self._set(ML, "log_outcome", noop)
            self._set(LS, "commit_ml_signal", noop)
            self._set(LS, "ml_log_outcome", noop)
            self._set(LS, "check_milestone_alert", lambda: None)
        except Exception:
            pass

        return self

    def __exit__(self, *exc):
        for mod, name, old, had in reversed(self._undo):
            if had:
                setattr(mod, name, old)
            else:
                try:
                    delattr(mod, name)
                except AttributeError:
                    pass
        self._undo.clear()
        return False


def assert_production_untouched(paths=None):
    """
    Belt-and-braces: snapshot mtimes of the real state files, to be compared
    after a replay. Returns a dict for a later verify() call.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = paths or [
        os.path.join(root, "data", "loss_tracker.json"),
        os.path.join(root, "data", "loss_cooldown.json"),
        os.path.join(root, "data", "active_state.json"),
        os.path.join(root, "data", "paper_equity.json"),
        os.path.join(root, "logs", "strategies"),
    ]
    snap = {}
    for p in paths:
        try:
            snap[p] = os.path.getmtime(p)
        except OSError:
            snap[p] = None
    return snap


def verify(snap) -> list[str]:
    """Return the list of production paths whose mtime changed. Should be []."""
    bad = []
    for p, t in snap.items():
        try:
            cur = os.path.getmtime(p)
        except OSError:
            cur = None
        if cur != t:
            bad.append(p)
    return bad
