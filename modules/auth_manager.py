"""
auth_manager.py
Binance USDM Futures API authentication — HMAC-SHA256 signed requests.

Binance signature scheme:
  signature = HMAC-SHA256( secret, query_string )
  where query_string includes all params INCLUDING timestamp.

  For GET  requests: signature appended to URL query string.
  For POST requests: signature appended to URL-encoded body.

Headers required on every private endpoint:
  X-MBX-APIKEY  — API key string

Credentials loaded from .env:
  BINANCE_API_KEY
  BINANCE_API_SECRET

No passphrase. No OAuth. No token refresh. Stateless per-request signing.
"""

import os
import hmac
import hashlib
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlencode

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("AuthManager")

# ─── Shared HTTP session (connection pooling / keep-alive) ────────────────────
# One pooled Session for every call to fapi.binance.com, imported and reused by
# data_feed and order_engine.  Plain requests.get/post opens a fresh TCP+TLS
# connection per call; on the scan hot path (100s of klines/cycle through a
# 10-worker pool) and on the order path (where handshake latency = slippage)
# that per-call handshake is pure waste.  A shared pool reuses the connection.
#
# Thread-safe: requests.Session / urllib3's pool are safe for concurrent
# requests.  pool_maxsize (20) is set >= data_hub's MAX_CONCURRENT (10) so the
# thread pool never forces a new connection or logs "connection pool is full".
# Signing is unaffected — it is per-request (timestamp + HMAC); the session only
# manages the transport, so there is no shared auth state to race.
SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

API_KEY    = os.getenv("BINANCE_API_KEY",    "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

BASE_URL = "https://fapi.binance.com"

# Latency budget for signed requests, in ms. Binance's 5000ms default rejects
# any request whose round trip is slower than that (-1021) even with a perfect
# clock. Capped at Binance's own 60000ms maximum. Works together with the
# server-time sync below: this covers arriving LATE, that covers being EARLY.
# Override with BINANCE_RECV_WINDOW_MS in .env if the link is unusually slow.
RECV_WINDOW_MS = min(60000, max(1000, int(os.getenv("BINANCE_RECV_WINDOW_MS", "10000"))))


# ─── Validation ───────────────────────────────────────────────────────────────

def validate_credentials() -> bool:
    """Return True if both credential fields are present."""
    if not all([API_KEY, API_SECRET]):
        log.error(
            "Missing Binance credentials. Set BINANCE_API_KEY and "
            "BINANCE_API_SECRET in .env"
        )
        return False
    return True


def test_binance_connection() -> dict:
    """
    Run a pre-flight check of the Binance connection and account config.
    Call this at startup before any trading begins.

    Checks (in order):
      1. Network reachability  — GET /fapi/v1/ping
      2. API key + Futures permission — GET /fapi/v2/account
      3. Position mode (must be Hedge/dual) — GET /fapi/v1/positionSide/dual
      4. LIVE_ENABLED env flag
      5. ACCOUNT_EQUITY_USDT env setting

    Returns:
        {
          "ok"     : bool   — False if any blocking issue found
          "issues" : list[str]   — human-readable problem descriptions
          "info"   : dict   — balance, hedge_mode, live_enabled, equity
        }
    """
    issues: list[str] = []
    info:   dict      = {}

    # ── 1. Network ping ───────────────────────────────────────────────────────
    try:
        resp = SESSION.get(BASE_URL + "/fapi/v1/ping", timeout=5)
        if resp.status_code != 200:
            issues.append(f"Binance FAPI unreachable — ping returned {resp.status_code}")
            return {"ok": False, "issues": issues, "info": info}
    except Exception as exc:
        issues.append(f"Network error reaching Binance ({exc}) — check internet/firewall")
        return {"ok": False, "issues": issues, "info": info}

    # ── 2. API key + Futures permissions ──────────────────────────────────────
    try:
        params = sign_params({})
        resp   = SESSION.get(
            BASE_URL + "/fapi/v2/account",
            params  = params,
            headers = api_headers(),
            timeout = 10,
        )
        data = resp.json()
        if resp.status_code != 200:
            code = data.get("code", resp.status_code)
            msg  = data.get("msg", "unknown")
            if code == -2015:
                issues.append(
                    "API key lacks Futures trading permission — "
                    "go to Binance → API Management → edit key → enable USD-M Futures"
                )
            elif code == -1022:
                issues.append(
                    "Signature invalid — check BINANCE_API_SECRET in .env "
                    "(no extra spaces or quotes)"
                )
            else:
                issues.append(f"Account API error: code={code} msg={msg}")
        else:
            bal = float(data.get("totalWalletBalance", "0") or "0")
            info["balance_usdt"]  = round(bal, 4)
            info["can_trade"]     = data.get("canTrade", False)
            info["can_withdraw"]  = data.get("canWithdraw", False)
            if not info["can_trade"]:
                issues.append(
                    "Account canTrade=False — your account may be restricted; "
                    "check Binance account health"
                )
            log.info(f"[Preflight] Futures account OK | Balance: {bal:.4f} USDT")
    except Exception as exc:
        issues.append(f"Account check exception: {exc}")

    # ── 3. Position mode: must be Hedge (dual side) ───────────────────────────
    try:
        params = sign_params({})
        resp   = SESSION.get(
            BASE_URL + "/fapi/v1/positionSide/dual",
            params  = params,
            headers = api_headers(),
            timeout = 10,
        )
        data = resp.json()
        if resp.status_code == 200:
            dual = data.get("dualSidePosition", False)
            info["hedge_mode"] = dual
            if not dual:
                issues.append(
                    "Hedge mode (dual position) is OFF — bot uses positionSide=LONG/SHORT "
                    "which requires Hedge Mode. Enable it: Binance Futures → ⚙ → "
                    "Position Mode → Hedge Mode → confirm. Then restart the bot."
                )
            else:
                log.info("[Preflight] Hedge mode: ON ✓")
        else:
            issues.append(
                f"Could not verify position mode: {data.get('msg', resp.status_code)}"
            )
    except Exception as exc:
        issues.append(f"Position mode check failed: {exc}")

    # ── 4. LIVE_ENABLED flag ──────────────────────────────────────────────────
    live_enabled = os.getenv("LIVE_ENABLED", "false").lower() == "true"
    info["live_enabled"] = live_enabled
    if not live_enabled:
        issues.append(
            "LIVE_ENABLED is not 'true' in .env — bot is in DRY RUN mode. "
            "Set LIVE_ENABLED=true to enable real order placement."
        )

    # ── 5. ACCOUNT_EQUITY_USDT ────────────────────────────────────────────────
    from modules import settings_manager as cfg
    equity = cfg.get("ACCOUNT_EQUITY_USDT")
    info["configured_equity"] = equity
    if equity > 1000 and info.get("balance_usdt", 0) < equity * 0.5:
        issues.append(
            f"ACCOUNT_EQUITY_USDT={equity} looks wrong "
            f"(actual balance ≈ {info.get('balance_usdt', '?')} USDT) — "
            f"correct it in the dashboard Settings tab"
        )

    # Blocking issues: everything except LIVE_ENABLED and equity warnings
    blocking = [
        i for i in issues
        if "DRY RUN" not in i and "ACCOUNT_EQUITY_USDT" not in i
    ]

    return {"ok": len(blocking) == 0, "issues": issues, "info": info}


# ─── Signing ──────────────────────────────────────────────────────────────────

def _sign(query_string: str) -> str:
    """
    Compute HMAC-SHA256 signature over a query string.

    Args:
        query_string : URL-encoded params string including timestamp,
                       e.g. 'symbol=BTCUSDT&side=BUY&timestamp=1711500000000'

    Returns:
        Hex-encoded signature string.
    """
    return hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


# ─── Server-time synchronisation ─────────────────────────────────────────────
# Binance validates every signed request as:
#     timestamp < serverTime + 1000  AND  serverTime - timestamp <= recvWindow
#
# The second half is a latency budget that recvWindow widens. The FIRST half is
# not: a local clock running more than 1s AHEAD of Binance is rejected with
# -1021 no matter how large recvWindow is. NTP keeps the host accurate against
# UTC, but Binance's own clock is the only reference that matters here.
#
# So we measure the offset against Binance directly and apply it to every
# timestamp. Re-measured periodically to track drift.

_TIME_OFFSET_MS      = 0        # server clock minus local clock
_LAST_TIME_SYNC      = 0.0      # monotonic seconds
_TIME_SYNC_INTERVAL  = 1800     # re-measure every 30 min

# Deliberately aim slightly behind server time. Being late is covered by
# recvWindow (10s); being early by >1s is an outright rejection.
_TIME_SAFETY_BIAS_MS = 1000


def sync_server_time(force: bool = False) -> int:
    """
    Measure and cache the offset between Binance's clock and ours.
    Uses a public endpoint, so it never signs anything (no recursion).
    Returns the offset in ms; on failure keeps the last known value.
    """
    global _TIME_OFFSET_MS, _LAST_TIME_SYNC

    now = time.monotonic()
    if not force and (now - _LAST_TIME_SYNC) < _TIME_SYNC_INTERVAL:
        return _TIME_OFFSET_MS

    try:
        t0   = time.time() * 1000
        resp = SESSION.get(BASE_URL + "/fapi/v1/time", timeout=10)
        t1   = time.time() * 1000
        server = int(resp.json()["serverTime"])
        # Midpoint of the round trip approximates our clock at reply time.
        offset = int(server - (t0 + t1) / 2)

        if abs(offset - _TIME_OFFSET_MS) > 500 or _LAST_TIME_SYNC == 0.0:
            log.info(
                f"Binance server-time offset: {offset:+d} ms "
                f"(local clock is {'behind' if offset > 0 else 'ahead'})"
            )
        if abs(offset) > 5000:
            log.warning(
                f"Large clock offset vs Binance: {offset:+d} ms — check NTP on "
                f"this host. Requests are being corrected, but investigate."
            )

        _TIME_OFFSET_MS = offset
        _LAST_TIME_SYNC = now
    except Exception as exc:
        log.warning(f"Could not sync Binance server time: {exc}")

    return _TIME_OFFSET_MS


def get_timestamp() -> int:
    """
    Current Unix ms, corrected to Binance's clock and biased slightly early-safe.
    """
    return int(time.time() * 1000) + sync_server_time() - _TIME_SAFETY_BIAS_MS


def sign_params(params: dict) -> dict:
    """
    Add timestamp and signature to a params dict (in-place).

    Works for both GET (params go in URL query string) and POST
    (params go in URL-encoded body). Binance treats both identically
    for signature purposes.

    Usage (GET):
        p = sign_params({"symbol": "BTCUSDT"})
        resp = SESSION.get(BASE_URL + path, params=p, headers=api_headers())

    Usage (POST):
        p = sign_params({"symbol": "BTCUSDT", "leverage": "5"})
        resp = SESSION.post(BASE_URL + path, params=p, headers=api_headers())

    Args:
        params : Dict of request parameters (without timestamp or signature).

    Returns:
        Same dict with 'timestamp' and 'signature' added.
    """
    params["timestamp"] = get_timestamp()
    params.setdefault("recvWindow", RECV_WINDOW_MS)
    # Use urlencode so special chars (e.g. '&', '=', '+', spaces) are escaped
    # identically to the way requests will send them. Must match exactly, or
    # Binance returns -1022 Signature for this request is not valid.
    query_string = urlencode(params, doseq=True)
    params["signature"] = _sign(query_string)
    return params


def api_headers() -> dict:
    """Headers for authenticated (private) endpoints."""
    return {
        "X-MBX-APIKEY": API_KEY,
        "Content-Type":  "application/x-www-form-urlencoded",
    }


def public_headers() -> dict:
    """Headers for public (unauthenticated) endpoints."""
    return {"Content-Type": "application/json"}


def get_session() -> requests.Session:
    """Return the shared pooled requests.Session instance."""
    return SESSION




