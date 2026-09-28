"""
modules/strategies/funding_short.py
Funding crowding short (FUNDING_SHORT) -- PAPER-ONLY replication on Binance.
Delta evidence: DCB EXPERIMENT_LOG 1.39-1.43. Binance measurement: 0.5.38.

Evidence: EXPERIMENT_LOG 1.39. Over the 1-year point-in-time universe with
real costs, shorting coins whose funding rate was >= 0.10% for 24h earned
+1.51%/trade (PF 1.37, 506 trades) and beat random shorts at the same moments
in 7 of 9 months -- but FAILED the walk-forward bound narrowly, and 5 small
coins carried 87% of the profit. It was found after looking, so it is tested
PROSPECTIVELY: the rule below is frozen (constants, not settings) as of
2026-09-27, and only trades taken after that date count.

Rule (identical to tools/funding_study.py, variant thr0.10_SHORT_24h):
  * Decision times: the funding boundaries 00/08/16 UTC. Entries only in the
    first ENTRY_WINDOW_MIN after a boundary T.
  * Signal: the funding rate KNOWN at T -- Delta's FUNDING:<SYMBOL> 1h candle
    that opened at T-1h and closed at T (the same history series the study
    read; small coins fund every 4h, so the live ticker's current-window rate
    is not the same number). Short when it is >= RATE_THR.
  * Stop: entry + STOP_K x ATR(14) on closed 1h bars x sqrt(HOLD_H).
    No take-profit. Exit after HOLD_H hours.
  * Universe: the scanner's watchlist, i.e. the live selection rules the
    point-in-time universe was built from.

PAPER ONLY ON CSB -- HARD BLOCK, NOT A SETTING. See EXPERIMENT_LOG 0.5.38.
Measured on Binance with this exact rule over 365 days / 527 perps / 1,208
trades: -1.134%/trade, PF 0.83, and WORSE than random same-moment twin shorts
(-0.566%/trade edge). Funding income is real (+0.512%/trade) but the adverse
price move is 3x larger. Delta's five profit-carrying coins (LAB/ARC/BEAT/
AIO/PIPPIN) lose here too: PF 0.53, -5.288%/trade over 102 trades.
It is also unsizable on this account: stops are 26-66% of entry, so a
correctly-sized order is under Binance's $5 minimum and forcing the minimum
costs 2.3-5.8% of equity per trade, outside the 1-2% rule in AGENTS.md.
Carried here so the operator can watch it run on paper against live Binance
data. `PAPER_ONLY = True` is enforced by live_scanner._execute_entries and is
deliberately not exposed as a setting.
Every signal is also appended to data/funding_short_signals.jsonl before any
slot/cap check, so the forward test has an unbiased sample even when position
limits block the trade.

Pre-registered forward test: 100 signals. Pass = mean net return > 0 after
costs and better than random shorts at the same boundaries (evaluated with
tools/funding_study.py's twin method on the logged signals).
"""
from __future__ import annotations

import os
import json
import logging
import threading

import numpy as np
import pandas as pd

from modules.strategies.base_strategy import BaseStrategy

log = logging.getLogger("FundingShort")

RATE_THR = 0.0010            # 0.10% per funding payment (fraction)
HOLD_H = 24
STOP_K = 1.5
ENTRY_WINDOW_MIN = 30
BOUNDARY_H = 8               # decision grid 00/08/16 UTC (as studied)
FROZEN_ON = "2026-09-27"      # rule frozen on DCB; unchanged here

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIGNAL_LOG = os.path.join(_ROOT, "data", "funding_short_signals.jsonl")
# One line per completed funding check (after its entry window closes): every
# coin's known rate and the signals -- read by the dashboard, and sent to
# Telegram/Discord by live_scanner via pop_ready_checks().
CHECK_LOG = os.path.join(_ROOT, "data", "funding_short_checks.jsonl")


def _live_rate_at(symbol: str, boundary: pd.Timestamp) -> float | None:
    """
    The funding rate SETTLED at `boundary` -- i.e. known at T, no look-ahead.

    Binance publishes settled rates on /fapi/v1/fundingRate; the row whose
    fundingTime == T is the payment just made. (Delta had to be read from a
    FUNDING:<SYMBOL> candle because its small coins settle every 4h; Binance
    settles on the contract's own schedule and reports it directly.)
    """
    from modules.auth_manager import SESSION, BASE_URL, public_headers
    want = int(boundary.timestamp() * 1000)
    try:
        r = SESSION.get(BASE_URL + "/fapi/v1/fundingRate",
                        params={"symbol": symbol,
                                "startTime": want - 60_000,
                                "endTime": want + 60_000, "limit": 10},
                        headers=public_headers(), timeout=8)
        if r.status_code != 200:
            return None
        for row in r.json() or []:
            if abs(int(row.get("fundingTime", 0)) - want) <= 60_000:
                return float(row["fundingRate"])          # Binance reports a fraction
    except Exception as exc:
        log.debug(f"funding lookup failed [{symbol}]: {exc}")
    return None


# Swappable for backtests/tests: fn(symbol, boundary) -> rate fraction | None.
RATE_SOURCE = _live_rate_at


def _time_indexed(df):
    if df is None or len(df) == 0:
        return df
    if "timestamp" in df.columns:
        return df.set_index(pd.to_datetime(df["timestamp"], utc=True))
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    return df


class FundingShortStrategy(BaseStrategy):
    STRATEGY_ID = "FUNDING_SHORT"
    PAPER_ONLY = True        # enforced in live_scanner._execute_entries (0.5.38)
    REQUIRES_1H = True
    REQUIRES_1M_DEPTH = 60
    STRENGTH = 0.8           # fixed: the rule has no ranking, only a threshold

    def __init__(self):
        self._rate_cache: dict[tuple[str, pd.Timestamp], float | None] = {}
        self._logged: set[tuple[str, str]] = set()
        self._checks: dict[pd.Timestamp, dict] = {}
        self._lock = threading.Lock()

    def _note(self, boundary: pd.Timestamp, symbol: str, rate: float | None, signal: bool = False):
        with self._lock:
            c = self._checks.setdefault(boundary, {"rates": {}, "signals": []})
            if rate is not None:
                c["rates"][symbol] = rate
            if signal and symbol not in c["signals"]:
                c["signals"].append(symbol)

    def pop_ready_checks(self, now: pd.Timestamp | None = None) -> list[dict]:
        """
        Summaries of funding checks whose entry window has closed, oldest
        first; each is returned once and appended to CHECK_LOG. Called by the
        scanner every cycle. A check with no coin reaching the rate lookup
        (e.g. an empty watchlist) is still reported, with 0 coins.
        """
        now = now or pd.Timestamp.now(tz="UTC")
        out = []
        with self._lock:
            for b in sorted(self._checks):
                if now - b < pd.Timedelta(minutes=ENTRY_WINDOW_MIN):
                    continue
                c = self._checks.pop(b)
                top = sorted(c["rates"].items(), key=lambda kv: -kv[1])
                out.append({"boundary": b.isoformat(), "coins": len(c["rates"]),
                            "threshold": RATE_THR, "signals": list(c["signals"]),
                            "top": [[s, r] for s, r in top[:5]],
                            "above": [s for s, r in top if r >= RATE_THR]})
        for rec in out:
            try:
                os.makedirs(os.path.dirname(CHECK_LOG), exist_ok=True)
                with open(CHECK_LOG, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except Exception as exc:
                log.warning(f"check log write failed: {exc}")
        return out

    def _now(self, df_1m, df_15m):
        for frame, step in ((df_1m, "1min"), (df_15m, "15min")):
            f = _time_indexed(frame) if frame is not None and len(frame) else None
            if f is not None and len(f):
                return f.index[-1] + pd.Timedelta(step)
        return None

    def _rate(self, symbol: str, boundary: pd.Timestamp):
        k = (symbol, boundary)
        with self._lock:
            if k in self._rate_cache:
                return self._rate_cache[k]
        try:
            v = RATE_SOURCE(symbol, boundary)
        except Exception as exc:
            log.debug(f"funding lookup failed [{symbol}]: {exc}")
            return None                      # not cached: retried next cycle
        if v is None:
            return None                      # candle not published yet: retry next cycle
        with self._lock:
            self._rate_cache[k] = v
            if len(self._rate_cache) > 5000:
                self._rate_cache.clear()
        return v

    def _log_signal(self, sig: dict, boundary: pd.Timestamp) -> None:
        k = (sig["symbol"], boundary.isoformat())
        with self._lock:
            if k in self._logged:
                return
            self._logged.add(k)
        try:
            os.makedirs(os.path.dirname(SIGNAL_LOG), exist_ok=True)
            with open(SIGNAL_LOG, "a") as f:
                f.write(json.dumps({"boundary": boundary.isoformat(), "symbol": sig["symbol"],
                                    "rate": sig["funding_rate"], "entry_price": sig["entry_price"],
                                    "sl_price": sig["sl_price"], "atr": sig["atr"],
                                    "logged_at": pd.Timestamp.now(tz="UTC").isoformat()}) + "\n")
        except Exception as exc:
            log.warning(f"signal log write failed: {exc}")

    def scan(self, symbol, df_1m, df_15m, df_1h, regime):
        now = self._now(df_1m, df_15m)
        if now is None:
            return None
        boundary = now.floor(f"{BOUNDARY_H}h")
        if now - boundary >= pd.Timedelta(minutes=ENTRY_WINDOW_MIN):
            return None

        h1 = _time_indexed(df_1h)
        if h1 is None or len(h1) < 16:
            return None
        h1 = h1[h1.index + pd.Timedelta(hours=1) <= now]          # closed bars only
        if len(h1) < 15:
            return None
        tr = pd.concat([h1["high"] - h1["low"], (h1["high"] - h1["close"].shift()).abs(),
                        (h1["low"] - h1["close"].shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.iloc[-14:].mean())

        rate = self._rate(symbol, boundary)
        self._note(boundary, symbol, rate)
        if rate is None or rate < RATE_THR:
            return None

        f = df_1m if df_1m is not None and len(df_1m) else df_15m
        entry = float(f["close"].iloc[-1])
        if not (entry > 0 and atr > 0):
            return None
        sl = entry + STOP_K * atr * np.sqrt(HOLD_H)
        sig = {
            "symbol": symbol,
            "strategy": self.STRATEGY_ID,
            "direction": "SHORT",
            "entry_price": entry,
            "sl_price": sl,
            "tp_price": None,
            "atr": atr,
            "qty": 0.0,
            "strength": self.STRENGTH,
            "funding_rate": rate,
            "reason": (f"FUNDING_SHORT (funding {rate*100:.3f}% >= {RATE_THR*100:.2f}% at "
                       f"{boundary:%H:%M} UTC, stop {STOP_K}xATRx sqrt({HOLD_H}))"),
        }
        self._log_signal(sig, boundary)
        self._note(boundary, symbol, rate, signal=True)
        return sig

    def manage(self, position, df_1m, session_pnl):
        if df_1m is None or df_1m.empty:
            return {"exit": False, "exit_price": 0.0, "exit_reason": ""}
        price = float(df_1m["close"].iloc[-1])
        try:
            entry_t = pd.to_datetime(position["entry_time"], utc=True)
            bar_t = pd.to_datetime(
                df_1m["timestamp"].iloc[-1] if "timestamp" in df_1m.columns else df_1m.index[-1], utc=True)
            if (bar_t - entry_t) >= pd.Timedelta(hours=HOLD_H):
                return {"exit": True, "exit_price": price, "exit_reason": "HOLD_24H"}
        except Exception:
            pass
        sl = float(position.get("sl_price") or 0.0)
        if sl > 0 and price >= sl:
            return {"exit": True, "exit_price": price, "exit_reason": "SL_HIT"}
        return {"exit": False, "exit_price": 0.0, "exit_reason": ""}
