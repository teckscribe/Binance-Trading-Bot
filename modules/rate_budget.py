"""
modules/rate_budget.py
Weight-aware throttle for the Binance USDM REST API.

Binance limits REQUEST WEIGHT, not request count: 2400 per IP per clock
minute. A klines call costs 1-10 depending on `limit`, positionRisk costs 5.
The old throttle (a 30ms gap between requests) counted requests, so the bot
ran at ~2700 weight/min in BULL/BEAR regimes with positions open and Binance
answered with -1003. A failed positionRisk read is what produced the phantom
"manual" closes (see order_engine.reconcile_with_exchange), and repeated
-1003 escalates to a 418 IP ban — minutes to hours with no API at all,
stops included.

This module makes a breach impossible from inside the process:

  * Every response's X-MBX-USED-WEIGHT-1M header is the authoritative
    reading of what Binance has counted this minute. Between headers, each
    outgoing call is pre-charged with its estimated weight so a burst cannot
    overshoot before the next header arrives.
  * Two ceilings. LOW-priority calls (klines, exchangeInfo — the scan) wait
    once usage passes SOFT; HIGH-priority calls (orders, positionRisk, mark
    price, fills — everything that manages an open position) only wait at
    HARD. So a heavy scan can never starve position management.
  * A 429 / 418 / -1003 response puts every caller into backoff for
    Retry-After (or until the minute rolls over). The reconciler already
    treats a failed read as "unknown, skip"; this stops the read failing.

Waiting means sleeping until the clock minute rolls over — Binance resets
the window on the minute, not on a rolling basis.

Wired in by auth_manager: SESSION.request is wrapped so every SESSION.get /
post / delete passes through acquire() before and observe() after. Callers
need no changes. Bare `requests.get` calls elsewhere (exchangeInfo in
watchlist/symbol_filter, a few per hour) are not gated; their weight still
shows up in the header, so they are accounted for, just not throttled.
"""
import logging
import re
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("RateBudget")

LIMIT = 2400
SOFT_CEILING = 1900     # scan-class calls wait above this
HARD_CEILING = 2300     # management-class calls wait above this
MAX_WAIT_SEC = 65.0     # never block a caller longer than one window + slack

_HIGH_PRIORITY = (
    "/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v2/positionRisk",
    "/fapi/v1/premiumIndex", "/fapi/v1/userTrades", "/fapi/v1/allOrders",
    "/fapi/v2/account", "/fapi/v2/balance", "/fapi/v1/leverage",
    "/fapi/v1/marginType", "/fapi/v1/positionSide", "/fapi/v1/openOrders",
    "/fapi/v1/time", "/fapi/v1/ping",
)


def _estimate_weight(path: str, params) -> int:
    """Binance's documented weight for the endpoints the bot uses."""
    p = params or {}
    if path.endswith("/klines"):
        try:
            limit = int(p.get("limit", 500))
        except (TypeError, ValueError):
            limit = 500
        if limit < 100:  return 1
        if limit < 500:  return 2
        if limit < 1000: return 5
        return 10
    if path.endswith(("/positionRisk", "/account", "/balance", "/allOrders",
                      "/userTrades", "/openOrders")):
        return 5
    if path.endswith("/ticker/24hr"):
        return 40 if not p.get("symbol") else 1
    return 1


def _is_high(path: str) -> bool:
    return any(path.endswith(h) for h in _HIGH_PRIORITY)


def _window_reset_in(now: float) -> float:
    """Seconds until the next clock minute plus a little slack."""
    return 60.0 - (now % 60.0) + 0.5


class WeightBudget:
    def __init__(self, clock=time.time, sleep=time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._used = 0            # last header reading
        self._pending = 0         # estimated weight sent since that header
        self._window = None       # int(minute) the readings belong to
        self._backoff_until = 0.0
        self.stats = {"throttled": 0, "waited_sec": 0.0, "backoffs": 0, "peak": 0}
        self._last_summary = 0.0

    # ── accounting ──────────────────────────────────────────────────────────
    def _roll(self, now: float) -> None:
        minute = int(now // 60)
        if self._window != minute:
            self._window = minute
            self._used = 0
            self._pending = 0

    def projected(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        with self._lock:
            self._roll(now)
            return self._used + self._pending

    # ── gate ────────────────────────────────────────────────────────────────
    def acquire(self, url: str, params=None) -> None:
        path = urlparse(url).path
        weight = _estimate_weight(path, params)
        high = _is_high(path)
        ceiling = HARD_CEILING if high else SOFT_CEILING
        start = self._clock()
        warned = False
        while True:
            now = self._clock()
            with self._lock:
                self._roll(now)
                wait = 0.0
                in_backoff = now < self._backoff_until
                if in_backoff:
                    # Explicit instruction from Binance (Retry-After / ban):
                    # honoured in full. Retrying inside a 418 ban extends it.
                    wait = self._backoff_until - now
                elif self._used + self._pending + weight > ceiling:
                    wait = _window_reset_in(now)
                if wait <= 0 or (not in_backoff and now - start >= MAX_WAIT_SEC):
                    self._pending += weight
                    return
                if not warned:
                    warned = True
                    self.stats["throttled"] += 1
                    kind = "backoff" if now < self._backoff_until else f"used~{self._used + self._pending}"
                    log.warning(
                        f"[RateBudget] holding {'HIGH' if high else 'low'}-priority "
                        f"{path} (w{weight}) for {wait:.1f}s — {kind}/{LIMIT}"
                    )
            self.stats["waited_sec"] += wait
            self._sleep(min(wait, MAX_WAIT_SEC))

    # ── feedback ────────────────────────────────────────────────────────────
    def observe(self, resp) -> None:
        now = self._clock()
        hdr = None
        try:
            hdr = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        except Exception:
            pass
        status = getattr(resp, "status_code", 0)
        body_code = None
        if status in (400, 418, 429):
            try:
                body_code = resp.json().get("code")
            except Exception:
                body_code = None
        with self._lock:
            self._roll(now)
            if hdr is not None:
                try:
                    self._used = int(hdr)
                    self._pending = 0        # header supersedes our estimate
                    self.stats["peak"] = max(self.stats["peak"], self._used)
                except ValueError:
                    pass
            if status in (418, 429) or body_code == -1003:
                retry = 0.0
                try:
                    retry = float(resp.headers.get("Retry-After") or 0)
                except Exception:
                    retry = 0.0
                until = now + (retry if retry > 0 else _window_reset_in(now))
                if until > self._backoff_until:
                    self._backoff_until = until
                self.stats["backoffs"] += 1
                log.error(
                    f"[RateBudget] Binance rate limit hit (HTTP {status}, code "
                    f"{body_code}) — backing off {until - now:.0f}s"
                    + (" — IP BAN, do not retry" if status == 418 else "")
                )
        if now - self._last_summary >= 60 and (self.stats["throttled"] or self.stats["backoffs"]):
            self._last_summary = now
            log.info(
                f"[RateBudget] last window: peak used {self.stats['peak']}/{LIMIT}, "
                f"throttled {self.stats['throttled']} call(s), "
                f"waited {self.stats['waited_sec']:.0f}s total, "
                f"backoffs {self.stats['backoffs']}"
            )


BUDGET = WeightBudget()


def install(session) -> None:
    """Wrap session.request so every call is gated and observed."""
    if getattr(session, "_rate_budget_installed", False):
        return
    orig = session.request

    def gated(method, url, **kw):
        BUDGET.acquire(url, kw.get("params"))
        resp = orig(method, url, **kw)
        BUDGET.observe(resp)
        return resp

    session.request = gated
    session._rate_budget_installed = True
