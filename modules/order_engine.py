"""
order_engine.py
Live order execution engine for Binance USDM Futures.

Mirrors paper_engine.py in interface — callers use the same method names.

Binance USDM Futures order endpoint:
  POST /fapi/v1/order
  Params (URL-encoded body):
    symbol       : e.g. 'BTCUSDT'
    side         : BUY | SELL
    positionSide : LONG | SHORT  (hedge mode — must be enabled on account)
    type         : MARKET
    quantity     : contract quantity (string, rounded to stepSize)
    timestamp    : Unix ms
    signature    : HMAC-SHA256

Hedge mode mapping:
  Open  LONG  : side=BUY,  positionSide=LONG
  Close LONG  : side=SELL, positionSide=LONG
  Open  SHORT : side=SELL, positionSide=SHORT
  Close SHORT : side=BUY,  positionSide=SHORT

Safety guards:
  1. All orders use ISOLATED margin
  2. Leverage set via /fapi/v1/leverage before first order per symbol
  3. Margin type set via /fapi/v1/marginType before first order per symbol
  4. LIVE_ENABLED=true in .env required for real orders (dry-run otherwise)
  5. Contract quantity rounded to symbol stepSize (avoids LOT_SIZE rejection)

Error codes handled as "position gone" (no alert, silent cleanup):
  -2011 : Order does not exist
  -4061 : Position side does not match (position already closed)

Fix (2026-04-14):
  _place_stop_order was using closePosition=true together with
  positionSide=LONG/SHORT (hedge mode). Binance API rejects this with
  -4120: closePosition=true only works in one-way mode (positionSide=BOTH).
  Fixed: removed closePosition=true, added explicit quantity parameter.
  Both call sites (open_position + reconcile_with_exchange) updated.
"""

import os
import json
import math
import logging
import requests
from datetime import datetime, timezone

from modules.auth_manager import SESSION, BASE_URL, api_headers, public_headers, sign_params

log = logging.getLogger("OrderEngine")

# ── Safety gate ────────────────────────────────────────────────────────────────
LIVE_ENABLED = os.getenv("LIVE_ENABLED", "false").lower() == "true"

# Tag stamped onto positions and summaries. Must reflect reality: a paper
# position recorded as "LIVE" is indistinguishable from real money downstream
# (logs, dashboard, notifications).
TRADE_MODE = "LIVE" if LIVE_ENABLED else "PAPER"

# ── Binance USDM Futures fee rates ────────────────────────────────────────────
TAKER_FEE_RATE = 0.0004        # 0.04% per side (market orders, VIP0)
ROUND_TRIP_FEE = TAKER_FEE_RATE * 2   # 0.08% total per trade

# ── Error codes that mean "position/order no longer exists" ───────────────────
# These should NOT trigger a failure alert — the position is simply gone.
_POSITION_GONE_CODES = {
    -2011,   # Unknown order — order doesn't exist
    -4061,   # Position side does not match user's setting
    -1121,   # Invalid symbol (delisted)
}


# ─── Contract spec (stepSize) ─────────────────────────────────────────────────

# In-process cache: {symbol: stepSize float}
_contract_spec_cache: dict[str, float] = {}
_price_tick_cache:    dict[str, float] = {}   # {symbol: PRICE_FILTER tickSize}

def _get_contract_spec(symbol: str) -> dict:
    """
    Fetch LOT_SIZE stepSize for a symbol from Binance exchangeInfo.
    Also caches PRICE_FILTER tickSize (used by _round_price).
    Cached in-process for the session lifetime.

    Binance requires quantity to be a multiple of stepSize.
    e.g. BTCUSDT stepSize=0.001 → valid: 0.001, 0.002 ... invalid: 0.0015

    Returns:
        dict with key 'step' (float). Returns {'step': 1.0} on failure
        (safe fallback — rounds to nearest whole number).
    """
    if symbol in _contract_spec_cache:
        return {"step": _contract_spec_cache[symbol]}

    url = BASE_URL + "/fapi/v1/exchangeInfo"
    try:
        resp = SESSION.get(url, headers=public_headers(), timeout=10)
        resp.raise_for_status()
        info = resp.json()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                step_found = False
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = float(f["stepSize"])
                        _contract_spec_cache[symbol] = step
                        step_found = True
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                        _price_tick_cache[symbol] = tick
                if step_found:
                    log.info(
                        f"[ContractSpec] {symbol}: stepSize="
                        f"{_contract_spec_cache[symbol]} "
                        f"tickSize={_price_tick_cache.get(symbol, '?')}"
                    )
                    return {"step": _contract_spec_cache[symbol]}
    except Exception as exc:
        log.warning(f"[ContractSpec] Failed for {symbol}: {exc}")

    log.warning(f"[ContractSpec] {symbol}: stepSize not found — defaulting to 1.0")
    _contract_spec_cache[symbol] = 1.0
    return {"step": 1.0}


def preload_exchange_specs(symbols=None) -> int:
    """
    Warm _contract_spec_cache / _price_tick_cache in ONE exchangeInfo call at
    startup, so the first order on each symbol does a 0ms RAM lookup instead of
    a ~500ms per-symbol exchangeInfo round trip mid-entry.

    Reconstructed 2026-09-05: the server ran a version with this function; the
    local repo never had it, so a deploy of the local order_engine broke
    live_scanner's `from modules.order_engine import (... preload_exchange_specs ...)`.

    Args:
        symbols : optional iterable to restrict the warm-up. None = cache every
                  TRADING USDT perpetual returned by exchangeInfo.

    Returns:
        Number of symbols whose stepSize was cached. Never raises — on failure
        it logs and returns 0, and per-symbol _get_contract_spec() still works
        as the lazy fallback.
    """
    want = {s.upper() for s in symbols} if symbols else None
    url  = BASE_URL + "/fapi/v1/exchangeInfo"
    n = 0
    try:
        resp = SESSION.get(url, headers=public_headers(), timeout=15)
        resp.raise_for_status()
        info = resp.json()
        for s in info.get("symbols", []):
            sym = s.get("symbol", "")
            if want is not None and sym.upper() not in want:
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    _contract_spec_cache[sym] = float(f["stepSize"]); n += 1
                elif f.get("filterType") == "PRICE_FILTER":
                    _price_tick_cache[sym] = float(f["tickSize"])
        log.info(f"[ContractSpec] preloaded {n} symbol specs in one call")
    except Exception as exc:
        # Non-fatal: lazy _get_contract_spec() covers any symbol not preloaded.
        log.warning(f"[ContractSpec] preload failed ({exc}) — falling back to lazy per-symbol fetch")
        return 0
    return n


def _round_price(price: float, symbol: str) -> str:
    """Round price to symbol's PRICE_FILTER tickSize, return as string."""
    tick = _price_tick_cache.get(symbol, 0.00001)
    if tick <= 0:
        tick = 0.00001
    decimals = max(0, -int(math.floor(math.log10(tick))))
    rounded  = round(round(price / tick) * tick, decimals)
    return f"{rounded:.{decimals}f}"


def _round_to_step(qty: float, step: float) -> float:
    """
    Floor qty to the nearest valid multiple of step.

    e.g. qty=0.0171, step=0.01 → 0.01
         qty=1.567,  step=0.001 → 1.567

    Uses floor (not round) to avoid accidentally exceeding available margin.
    """
    if step <= 0:
        return qty
    decimals = max(0, -int(math.floor(math.log10(step))))
    floored  = math.floor(qty / step) * step
    return round(floored, decimals)


# ─── Leverage and margin setup ─────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> bool:
    """
    Set leverage for a symbol.
    Single call covers both LONG and SHORT sides on Binance.

    Returns True on success, False on failure.
    """
    if not LIVE_ENABLED:
        log.info(f"[LIVE DISABLED] Would set leverage {leverage}× for {symbol}")
        return True

    params = sign_params({
        "symbol":   symbol,
        "leverage": leverage,
    })
    try:
        resp = SESSION.post(
            BASE_URL + "/fapi/v1/leverage",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200:
            log.info(f"Leverage set: {symbol} → {leverage}×")
            return True
        log.error(f"Set leverage failed [{symbol}]: {data}")
        return False
    except Exception as exc:
        log.error(f"Set leverage exception [{symbol}]: {exc}")
        return False


def _set_margin_type(symbol: str) -> bool:
    """
    Set margin type to ISOLATED for a symbol.
    Binance returns -4046 if already ISOLATED — treat as success.

    Returns True on success or already-isolated, False on real failure.
    """
    if not LIVE_ENABLED:
        return True

    params = sign_params({
        "symbol":     symbol,
        "marginType": "ISOLATED",
    })
    try:
        resp = SESSION.post(
            BASE_URL + "/fapi/v1/marginType",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        data = resp.json()
        code = data.get("code", 200)
        if resp.status_code == 200 or code == -4046:
            # 200 = changed, -4046 = already ISOLATED
            log.info(f"Margin type ISOLATED confirmed: {symbol}")
            return True
        log.error(f"Set margin type failed [{symbol}]: {data}")
        return False
    except Exception as exc:
        log.error(f"Set margin type exception [{symbol}]: {exc}")
        return False


# ─── Place order ──────────────────────────────────────────────────────────────

def _place_order(
    symbol:        str,
    side:          str,    # 'BUY' | 'SELL'
    position_side: str,    # 'LONG' | 'SHORT'  (hedge mode)
    quantity:      str,    # contract quantity as string
    order_type:    str = "MARKET",
) -> dict | None:
    """
    Place a single futures order on Binance USDM.
    Returns the API response dict or None on exception.
    """
    params = sign_params({
        "symbol":       symbol,
        "side":         side,
        "positionSide": position_side,
        "type":         order_type,
        "quantity":     quantity,
    })

    if not LIVE_ENABLED:
        log.info(
            f"[LIVE DISABLED] Would place order: {symbol} {side} {position_side} "
            f"qty={quantity} type={order_type}"
        )
        return {"orderId": 0, "status": "FILLED", "_simulated": True}

    try:
        resp = SESSION.post(
            BASE_URL + "/fapi/v1/order",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200:
            log.info(
                f"Order placed: {symbol} {side}/{position_side} "
                f"qty={quantity} orderId={data.get('orderId', '?')}"
            )
        else:
            log.error(
                f"Order rejected [{symbol}]: code={data.get('code')} "
                f"msg={data.get('msg')} | side={side} qty={quantity}"
            )
        return data
    except Exception as exc:
        log.error(f"Order exception [{symbol}]: {exc}")
        return None


# ─── Query order status ──────────────────────────────────────────────────────

def _query_order_status(symbol: str, order_id: int) -> str | None:
    """Query Binance for the current status of an order. Returns status string or None."""
    try:
        params = sign_params({"symbol": symbol, "orderId": str(order_id)})
        resp = SESSION.get(
            BASE_URL + "/fapi/v1/order",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("status")
    except Exception as exc:
        log.warning(f"[LIVE] order status query failed [{symbol}] orderId={order_id}: {exc}")
    return None


# ─── Fetch fill price ─────────────────────────────────────────────────────────

def _get_fill_price(order_id: int, symbol: str) -> float | None:
    """
    Fetch the actual average fill price from a completed order.
    Used to recompute SL/TP from the real entry rather than signal price.

    Args:
        order_id : orderId from _place_order response
        symbol   : e.g. 'BTCUSDT'

    Returns:
        Float fill price, or None on failure.
    """
    import time as _time
    _time.sleep(0.5)   # brief delay — order needs time to be filled

    params = sign_params({
        "symbol":  symbol,
        "orderId": order_id,
    })
    try:
        resp = SESSION.get(
            BASE_URL + "/fapi/v1/order",
            params=params,
            headers=api_headers(),
            timeout=8,
        )
        data = resp.json()
        if resp.status_code == 200:
            avg_price = data.get("avgPrice") or data.get("price")
            if avg_price and float(avg_price) > 0:
                return float(avg_price)
    except Exception as exc:
        log.warning(f"Fill price fetch failed [{symbol} #{order_id}]: {exc}")
    return None


# ─── Server-side stop orders (safety net) ─────────────────────────────────────

def _place_stop_order(
    symbol:     str,
    direction:  str,      # 'LONG' | 'SHORT' — the POSITION direction
    stop_price: float,
    contracts:  float,    # FIX: explicit qty required in hedge mode
) -> int | None:
    """
    Place a STOP_MARKET order on Binance as a server-side SL.

    This fires on Binance's server even if the bot process is dead.

    FIX (2026-04-14): Removed closePosition=true — this parameter only
    works in one-way mode (positionSide=BOTH). In hedge mode
    (positionSide=LONG/SHORT), Binance returns -4120 when closePosition=true
    is used. Fixed by passing explicit quantity instead.

    Returns the orderId (int) on success, None on failure.
    """
    if not LIVE_ENABLED:
        log.info(
            f"[LIVE DISABLED] Would place STOP_MARKET: "
            f"{symbol} {direction} @ {stop_price} qty={contracts}"
        )
        return None

    # Close LONG → SELL, Close SHORT → BUY
    side          = "SELL" if direction == "LONG" else "BUY"
    position_side = "LONG" if direction == "LONG" else "SHORT"
    price_str     = _round_price(stop_price, symbol)

    # Round contracts to stepSize before placing stop order
    spec     = _get_contract_spec(symbol)
    step     = spec["step"]
    qty_adj  = _round_to_step(contracts, step)
    if qty_adj <= 0:
        log.warning(
            f"[STOP] SL order skipped [{symbol}]: quantity after rounding is 0 "
            f"(raw={contracts}, step={step})"
        )
        return None

    qty_str  = str(qty_adj)
    # NOTE: reduceOnly is incompatible with positionSide in hedge mode
    # (Binance error -1106). positionSide already implies reduce direction.
    params = sign_params({
        "symbol":        symbol,
        "side":          side,
        "positionSide":  position_side,
        "algoType":      "CONDITIONAL",
        "type":          "STOP_MARKET",
        "triggerPrice":  price_str,
        "quantity":      qty_str,
    })

    def _send_stop_request():
        resp = SESSION.post(
            BASE_URL + "/fapi/v1/algoOrder",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        return resp, resp.json()

    try:
        resp, data = _send_stop_request()
        if resp.status_code == 200:
            oid = data.get("orderId") or data.get("algoId")
            log.info(
                f"[STOP] Server-side SL placed: {symbol} {direction} "
                f"@ {price_str} qty={qty_str} orderId={oid}"
            )
            return oid

        log.warning(
            f"[STOP] SL order rejected [{symbol}]: "
            f"code={data.get('code')} msg={data.get('msg')} "
            f"qty={qty_str} stopPrice={price_str} positionSide={position_side}"
        )

        if resp.status_code in (429, 418) or data.get("code") in (-1003, -4120, -2010, -2015):
            import time as _time
            _time.sleep(0.5)
            actual_contracts = _get_live_contracts(symbol, direction)
            if actual_contracts is not None:
                actual_qty_adj = _round_to_step(actual_contracts, step)
                if actual_qty_adj > 0 and actual_qty_adj != qty_adj:
                    qty_adj = actual_qty_adj
                    qty_str = str(qty_adj)
                    log.info(
                        f"[STOP] Retrying SL with live contract qty: {symbol} "
                        f"qty={qty_str}"
                    )
            params = sign_params({
                "symbol":        symbol,
                "side":          side,
                "positionSide":  position_side,
                "algoType":      "CONDITIONAL",
        "type":          "STOP_MARKET",
                "triggerPrice":  price_str,
                "quantity":      qty_str,
            })
            resp, data = _send_stop_request()
            if resp.status_code == 200:
                oid = data.get("orderId") or data.get("algoId")
                log.info(
                    f"[STOP] Server-side SL placed on retry: {symbol} {direction} "
                    f"@ {price_str} qty={qty_str} orderId={oid}"
                )
                return oid
            log.warning(
                f"[STOP] SL retry rejected [{symbol}]: "
                f"code={data.get('code')} msg={data.get('msg')} "
                f"qty={qty_str} stopPrice={price_str} positionSide={position_side}"
            )
        return None
    except Exception as exc:
        log.warning(f"[STOP] SL order exception [{symbol}]: {exc}")
        return None


def update_stop_order(pos: dict, new_stop: float) -> bool:
    """
    Move the server-side STOP_MARKET to a new level as breakeven/trailing arms.

    WHY THIS EXISTS
    ---------------
    _place_stop_order() ran ONCE at entry and was never updated, so Binance
    kept guarding the ORIGINAL stop for the life of the trade. Breakeven and
    trailing levels lived only in bot memory and were checked only when
    manage() ran — at 15m bar close under MANAGE_ON_BAR_CLOSE. Price could
    break through a trailed stop mid-bar and keep falling; the bot noticed at
    the close and exited THERE, not at the stop.

    Measured over 63 stop-based exits in the live logs: every single one
    filled worse than its stop, totalling -1.82 USDT — about 14% of gross
    P&L. Worst case LITUSDT gave back 1.138% (-0.63 USDT) on a stop that was
    sitting ABOVE entry and should have closed in profit.

    ORDERING IS DELIBERATE — place first, cancel second
    --------------------------------------------------
    The obvious implementation (cancel old, place new) leaves a window where
    the position has NO stop on the exchange. If the placement then fails
    (network, rate limit, -1021 clock, -2015 IP) the position is naked until
    something notices, and one gap could cost far more than every trailing
    improvement combined.

    Placing first is safe here because the order is
    side=SELL + positionSide=LONG (or BUY/SHORT), which in hedge mode can
    only ever REDUCE that position — it cannot open the opposite side. So
    two live stops are harmless: the nearer one fires, closing the position,
    and the other is left as a stale reduce-only order.

    Consequently:
      placement fails -> old stop still active, position protected, retry later
      cancel fails    -> two stops, tighter one wins, stale one is inert

    Never widens the stop. A trail must only ever move in the favourable
    direction; a "new" level that is worse than the current one is ignored.

    Returns True if the exchange stop was actually moved.
    """
    if not LIVE_ENABLED:
        return False                      # paper: nothing server-side to move

    try:
        symbol    = pos["symbol"]
        direction = pos["direction"]
        cur       = float(pos.get("sl_price") or 0.0)
        new_stop  = float(new_stop)
    except (KeyError, TypeError, ValueError):
        return False

    if new_stop <= 0 or cur <= 0:
        return False

    # Refuse to loosen protection.
    if direction == "LONG" and new_stop <= cur:
        return False
    if direction == "SHORT" and new_stop >= cur:
        return False

    # Ignore sub-tick noise so a 15m bar cannot burn an API call per cycle.
    tick = _price_tick_cache.get(symbol) or 0.0
    if tick and abs(new_stop - cur) < tick:
        return False

    contracts = float(pos.get("contracts") or 0.0)
    if contracts <= 0:
        return False

    old_oid = pos.get("stop_order_id")

    # 1. place the tighter stop FIRST — position stays protected throughout
    new_oid = _place_stop_order(symbol, direction, new_stop, contracts)
    if new_oid is None:
        log.warning(
            f"[STOP] Trail update FAILED [{symbol}]: could not place new stop "
            f"@ {new_stop} — keeping existing stop @ {cur} (orderId={old_oid})"
        )
        return False

    # 2. only now retire the looser one
    if old_oid is not None and old_oid != new_oid:
        _cancel_stop_order(symbol, old_oid)

    pos["stop_order_id"] = new_oid
    log.info(
        f"[STOP] Trail moved [{symbol}] {cur} -> {new_stop} "
        f"(orderId {old_oid} -> {new_oid})"
    )
    return True


def _cancel_stop_order(symbol: str, order_id: int | None) -> None:
    """Cancel a STOP_MARKET order on Binance.  Silent on already-gone orders."""
    if order_id is None or not LIVE_ENABLED:
        return

    params = sign_params({
        "symbol":  symbol,
        "algoId": order_id,
    })
    try:
        resp = requests.delete(BASE_URL + "/fapi/v1/algoOrder",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        data = resp.json()
        code = data.get("code")
        if resp.status_code == 200:
            log.info(f"[STOP] SL order cancelled: {symbol} orderId={order_id}")
        elif code in (-2011,):
            log.debug(f"[STOP] SL order already gone: {symbol} orderId={order_id}")
        else:
            log.warning(f"[STOP] Cancel failed [{symbol}]: {data}")
    except Exception as exc:
        log.warning(f"[STOP] Cancel exception [{symbol}]: {exc}")


# ─── Fetch open positions from Binance ────────────────────────────────────────

def _get_binance_open_positions() -> dict:
    """
    Fetch all open positions from Binance /fapi/v2/positionRisk.

    Returns:
        Dict keyed by (symbol, positionSide) → position info dict.
        Only includes positions with non-zero positionAmt.

    Example return:
        {
          ("BTCUSDT", "LONG"): {
              "symbol": "BTCUSDT",
              "positionSide": "LONG",
              "positionAmt": "0.010",
              "entryPrice": "71500.0",
              "leverage": "5",
          }
        }
    """
    params = sign_params({})
    try:
        resp = SESSION.get(
            BASE_URL + "/fapi/v2/positionRisk",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"positionRisk fetch failed: {resp.text[:200]}")
            return {}

        positions = {}
        for p in resp.json():
            amt = float(p.get("positionAmt", "0") or "0")
            if abs(amt) < 1e-9:
                continue   # zero position — skip
            sym  = p.get("symbol", "")
            side = p.get("positionSide", "BOTH")
            positions[(sym, side)] = p
        return positions
    except Exception as exc:
        log.warning(f"positionRisk exception: {exc}")
        return {}


def _get_manual_close_price(symbol: str, direction: str) -> float | None:
    """
    Attempt to find the close price of a position that was manually closed
    by scanning recent trades for the symbol.

    FIX 2026-06-09: Must filter by positionSide in hedge mode.
    Without this filter, a BUY trade from the LONG side was returned
    when looking for the SHORT close price — AAVE showed exit=73.96
    instead of the real SL fill ~62.0, creating a phantom -20% loss.

    Returns float price or None.
    """
    import time
    # Fetch trades from the last 2 hours to ensure we get recent fills.
    # Binance returns oldest trades first if startTime is omitted.
    start_time = int((time.time() - 2 * 3600) * 1000)
    params = sign_params({
        "symbol": symbol,
        "startTime": start_time,
        "limit":  1000,
    })
    try:
        resp = SESSION.get(
            BASE_URL + "/fapi/v1/userTrades",
            params=params,
            headers=api_headers(),
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        trades = resp.json()
        # Close LONG → SELL on LONG side, close SHORT → BUY on SHORT side
        close_side = "SELL" if direction == "LONG" else "BUY"
        pos_side   = "LONG" if direction == "LONG" else "SHORT"
        for trade in reversed(trades):
            if (trade.get("side") == close_side
                    and trade.get("positionSide") == pos_side):
                price = trade.get("price")
                if price:
                    return float(price)
    except Exception as exc:
        log.warning(f"Manual close price fetch failed [{symbol}]: {exc}")
    return None


def _classify_exchange_close(pos: dict) -> tuple[str, str, float | None]:
    """
    Work out WHY a tracked position is no longer on Binance, from the order
    history since entry. Returns (exit_reason, exit_source, fill_price).

    A position vanishing from positionRisk has three common causes, and only
    one of them is a human:
      STOP_MARKET filled  -> the bot's own server-side stop fired between two
                             fast cycles. This is an SL hit, not a manual close.
      LIQUIDATION         -> exchange force-close.
      MARKET / LIMIT      -> someone closed it from the Binance app.
    Before this, all three were stamped MANUAL_CLOSE, so every exchange-side
    stop fill showed up in the ledger as if the operator had intervened.
    """
    symbol    = pos["symbol"]
    direction = pos["direction"]
    close_side = "SELL" if direction == "LONG" else "BUY"
    pos_side   = "LONG" if direction == "LONG" else "SHORT"
    try:
        entry_ms = int(datetime.fromisoformat(str(pos["entry_time"])).timestamp() * 1000)
    except Exception:
        import time
        entry_ms = int((time.time() - 24 * 3600) * 1000)

    try:
        resp = SESSION.get(
            BASE_URL + "/fapi/v1/allOrders",
            params=sign_params({"symbol": symbol, "startTime": entry_ms - 60_000,
                                "limit": 200}),
            headers=api_headers(), timeout=8,
        )
        orders = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        log.warning(f"allOrders fetch failed [{symbol}]: {exc}")
        orders = []

    fills = [o for o in orders
             if o.get("status") == "FILLED"
             and o.get("side") == close_side
             and o.get("positionSide") == pos_side]
    if not fills:
        price = _get_manual_close_price(symbol, direction)
        return "MANUAL_CLOSE", "manual", price

    o = max(fills, key=lambda x: int(x.get("updateTime", 0) or 0))
    try:
        price = float(o.get("avgPrice") or 0) or None
    except (TypeError, ValueError):
        price = None
    otype = str(o.get("type", "")).upper()
    is_our_stop = pos.get("stop_order_id") is not None and \
                  str(o.get("orderId")) == str(pos.get("stop_order_id"))

    if otype in ("STOP_MARKET", "STOP") or is_our_stop:
        return "SL_HIT", "exchange", price
    if otype == "LIQUIDATION":
        return "LIQUIDATED", "exchange", price
    if otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
        return "TP_HIT", "exchange", price
    return "MANUAL_CLOSE", "manual", price


def _get_live_contracts(symbol: str, direction: str) -> float | None:
    """
    Get the current position size from Binance for a specific symbol/direction.
    Used to verify contracts match what the bot expects.

    Returns float contracts or None.
    """
    position_side = "LONG" if direction == "LONG" else "SHORT"
    positions     = _get_binance_open_positions()
    p = positions.get((symbol, position_side))
    if p is None:
        return None
    return abs(float(p.get("positionAmt", "0") or "0"))


# ─── Get account equity ───────────────────────────────────────────────────────

def _get_account_equity() -> float:
    """
    Fetch total wallet balance from Binance USDM account.

    Returns float USDT balance, or 0.0 on failure.
    """
    params = sign_params({})
    try:
        resp = SESSION.get(
            BASE_URL + "/fapi/v2/account",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return 0.0
        data = resp.json()
        bal  = data.get("totalWalletBalance") or data.get("totalMarginBalance")
        return float(bal) if bal else 0.0
    except Exception as exc:
        log.warning(f"Account equity fetch failed: {exc}")
        return 0.0


def get_account_equity() -> float:
    """
    Public wrapper — fetch live account balance from Binance.
    Returns 0.0 on failure (caller should keep last known good value).
    """
    return _get_account_equity()


# ─── Public interface ─────────────────────────────────────────────────────────

class OrderEngine:

    def __init__(self):
        self.active           : list[dict] = []
        self.closed           : list[dict] = []
        self.session_pnl_pct  = 0.0
        self._setup_done      : set[str] = set()   # symbols where lev+margin confirmed

    # ── Open position ──────────────────────────────────────────────────────────

    def open_position(self, signal: dict, size: dict) -> dict | None:
        """
        Place a market order to open a new live position.
        Sets leverage and margin type first if not done for this symbol.

        Returns the position dict on success, None on failure.
        """
        symbol    = signal["symbol"]
        direction = signal["direction"]
        leverage  = size["leverage"]
        contracts = size["contracts"]

        # ── Setup: leverage + isolated margin (once per symbol per session) ───
        if symbol not in self._setup_done:
            if not _set_margin_type(symbol):
                log.error(f"Aborting open — could not set margin type for {symbol}")
                return None
            if not set_leverage(symbol, leverage):
                log.error(f"Aborting open — could not set leverage for {symbol}")
                return None
            self._setup_done.add(symbol)

        # ── Round quantity to stepSize ────────────────────────────────────────
        spec      = _get_contract_spec(symbol)
        step      = spec["step"]
        contracts_adj = _round_to_step(contracts, step)
        if contracts_adj <= 0:
            log.error(
                f"[LIVE] {symbol}: quantity after rounding is 0 "
                f"(raw={contracts:.6f}, step={step}) — skipping"
            )
            return None
        if contracts_adj != contracts:
            log.info(
                f"[LIVE] {symbol}: qty adjusted {contracts:.6f} → {contracts_adj} "
                f"(step={step})"
            )

        # ── Hedge mode mapping ────────────────────────────────────────────────
        # Open LONG  : BUY  / LONG
        # Open SHORT : SELL / SHORT
        side          = "BUY"  if direction == "LONG"  else "SELL"
        position_side = "LONG" if direction == "LONG"  else "SHORT"

        resp = _place_order(
            symbol        = symbol,
            side          = side,
            position_side = position_side,
            quantity      = str(contracts_adj),
            order_type    = "MARKET",
        )

        if resp is None:
            log.error(f"open_position failed (no response) for {symbol}")
            return None

        # Check for error code
        resp_code = resp.get("code")
        if resp_code is not None and int(resp_code) != 200:
            log.error(f"open_position rejected [{symbol}]: {resp.get('msg')}")
            return None

        order_id = resp.get("orderId", 0)

        # Try to get actual fill price (falls back to signal price)
        fill_price = signal["entry_price"]
        if LIVE_ENABLED and order_id:
            actual = _get_fill_price(order_id, symbol)
            if actual:
                fill_price = actual
                log.info(f"[LIVE] {symbol}: fill price {fill_price:.4f} (signal was {signal['entry_price']:.4f})")

        # Recompute SL/TP from actual fill if price differs materially
        entry_signal = signal["entry_price"]
        sl_signal    = signal["sl_price"]
        if fill_price != entry_signal and entry_signal > 0:
            ratio = fill_price / entry_signal
            sl_price = sl_signal * ratio
            tp_raw   = signal.get("tp_price")
            tp_price = (tp_raw * ratio) if tp_raw else None
            log.info(
                f"[LIVE] {symbol}: SL adjusted "
                f"{sl_signal:.4f} → {sl_price:.4f} (fill ratio {ratio:.6f})"
            )
        else:
            sl_price = sl_signal
            tp_price = signal.get("tp_price")

        pos = {
            "id":             order_id,
            "mode":           TRADE_MODE,
            "symbol":         symbol,
            "strategy":       signal["strategy"],
            "direction":      direction,
            "entry_price":    fill_price,
            "sl_price":       sl_price,
            # The price the exchange STOP_MARKET sits at. sl_price is mutated
            # as the trail advances, but the order on Binance is never moved —
            # so this is the level the exchange actually enforces, at tick
            # resolution, regardless of how often the bot polls.
            "initial_sl_price": sl_price,
            "tp_price":       tp_price,
            "atr":            signal.get("atr", 0.0),
            "contracts":      contracts_adj,
            "notional":       size["notional"],
            "leverage":       leverage,
            "margin_req":     size["margin_req"],
            "risk_usdt":      size["risk_usdt"],
            "account_equity": size.get("account_equity", 0.0),
            "entry_time":     datetime.now(timezone.utc).isoformat(),
            "exit_time":      None,
            "exit_price":     None,
            "exit_reason":    None,
            "pnl_pct":        0.0,
            "pnl_usdt":       0.0,
            "pnl_equity_pct": 0.0,
            "be_active":      False,
            "hwm":            0.0,
            "status":         "OPEN",
            "stop_order_id":  None,
        }

        # ── Place server-side STOP_MARKET on Binance (safety net) ─────────────
        # FIX: pass contracts_adj so _place_stop_order uses explicit quantity
        # (closePosition=true was removed — invalid in hedge mode)
        stop_oid = _place_stop_order(symbol, direction, sl_price, contracts_adj)
        pos["stop_order_id"] = stop_oid

        self.active.append(pos)
        log.info(
            f"[LIVE] OPEN {symbol} {direction} @ {fill_price:.4f} | "
            f"orderId={order_id} | qty={contracts_adj} | Lev={leverage}× | "
            f"SL_order={'✅' if stop_oid else '⚠️ NONE'}"
        )
        return pos

    # ── Close position ─────────────────────────────────────────────────────────

    def close_position(
        self,
        pos:            dict,
        exit_price:     float,
        exit_reason:    str,
        account_equity: float = 0.0,
    ) -> bool | None:
        """
        Place a market order to close an existing live position.

        Returns:
            True  — closed successfully
            False — real failure (log + alert needed)
            None  — position no longer exists (liquidated or manual close — silent)
        """
        if pos not in self.active:
            log.warning(f"[LIVE] close_position: {pos.get('id')} not in active list")
            return None

        symbol    = pos["symbol"]
        direction = pos["direction"]
        contracts = pos["contracts"]

        # ── Round to stepSize ─────────────────────────────────────────────────
        spec      = _get_contract_spec(symbol)
        step      = spec["step"]
        contracts = _round_to_step(contracts, step)
        if contracts <= 0:
            log.error(f"[LIVE] {symbol}: close qty rounded to 0 — removing from active")
            self.active.remove(pos)
            return None

        # ── Hedge mode mapping ────────────────────────────────────────────────
        # Close LONG  : SELL / LONG
        # Close SHORT : BUY  / SHORT
        side          = "SELL" if direction == "LONG"  else "BUY"
        position_side = "LONG" if direction == "LONG"  else "SHORT"

        fail_count = pos.get("close_fail_count", 0)

        resp = _place_order(
            symbol        = symbol,
            side          = side,
            position_side = position_side,
            quantity      = str(contracts),
            order_type    = "MARKET",
        )

        if resp is None:
            fail_count += 1
            pos["close_fail_count"] = fail_count
            if fail_count < 3:
                import time as _time
                _time.sleep(0.3)
                log.warning(f"[LIVE] close_position retry #{fail_count} [{symbol}]")
                resp = _place_order(
                    symbol=symbol, side=side, position_side=position_side,
                    quantity=str(contracts), order_type="MARKET",
                )
            if resp is None:
                log.error(f"[LIVE] close_position FAILED (no response) [{symbol}] attempt #{fail_count}")
                return False

        # Check for "position gone" codes
        resp_code = resp.get("code")
        if resp_code is not None:
            try:
                code_int = int(resp_code)
            except (TypeError, ValueError):
                code_int = 0

            if code_int in _POSITION_GONE_CODES:
                # Leave it in `active`: reconcile_with_exchange() will find it
                # missing on Binance, classify the close from order history
                # (usually the exchange stop fired first) and book the P&L.
                # Removing it here skipped that entirely — the trade vanished
                # with no exit log, no notification and no P&L.
                log.info(
                    f"[LIVE] {symbol}: position no longer exists (code {code_int}) "
                    f"— exchange closed it first; reconciler will book the exit"
                )
                return None   # Not a failure

            if code_int != 200 and resp_code != 0:
                log.error(
                    f"[LIVE] close_position FAILED [{symbol}] attempt #{fail_count}: "
                    f"code={resp_code} msg={resp.get('msg')}"
                )
                return False

        # ── P&L calculation ───────────────────────────────────────────────────
        # Extract actual fill price if returned by Binance to match order exactly
        if isinstance(resp, dict):
            avg_price_str = resp.get("avgPrice") or resp.get("price")
            if avg_price_str:
                try:
                    avg_price_val = float(avg_price_str)
                    if avg_price_val > 0:
                        log.info(
                            f"[LIVE] {symbol} close filled avgPrice={avg_price_val:.4f} "
                            f"(planned={exit_price:.4f})"
                        )
                        exit_price = avg_price_val
                except (ValueError, TypeError):
                    pass

        entry = pos["entry_price"]
        if direction == "LONG":
            pnl_pct = (exit_price - entry) / entry
        else:
            pnl_pct = (entry - exit_price) / entry

        pnl_usdt = pos["contracts"] * abs(exit_price - entry)
        if pnl_pct < 0:
            pnl_usdt = -pnl_usdt

        notional_open  = pos.get("notional", abs(pos["contracts"] * entry))
        notional_close = abs(pos["contracts"] * exit_price)
        fee_usdt       = ((notional_open + notional_close) / 2) * ROUND_TRIP_FEE
        pnl_usdt_net = pnl_usdt - fee_usdt

        # Explicit > 0 check — Python 'or' treats 0.0 as falsy
        if account_equity > 0:
            equity = account_equity
        elif pos.get("account_equity", 0.0) > 0:
            equity = pos["account_equity"]
        else:
            equity = 10.0   # hard fallback

        pnl_equity_pct     = pnl_usdt     / equity
        pnl_equity_pct_net = pnl_usdt_net / equity

        pos.update({
            "exit_time":            datetime.now(timezone.utc).isoformat(),
            "exit_price":           exit_price,
            "exit_reason":          exit_reason,
            "exit_source":          "bot",
            "pnl_pct":              round(pnl_pct, 6),
            "pnl_usdt":             round(pnl_usdt, 4),
            "pnl_usdt_net":         round(pnl_usdt_net, 4),
            "fee_usdt":             round(fee_usdt, 4),
            "pnl_equity_pct":       round(pnl_equity_pct_net, 6),
            "pnl_equity_pct_gross": round(pnl_equity_pct, 6),
            "pnl_pct_100":          round(pnl_equity_pct_net * 100, 4),
            "status":               "CLOSED",
        })

        self.session_pnl_pct += pnl_equity_pct_net
        self.active.remove(pos)
        self.closed.append(pos)

        # Cancel server-side stop order (it's now orphaned)
        _cancel_stop_order(symbol, pos.get("stop_order_id"))

        # Blacklist tracking — record hard stops, ban repeat offenders
        if exit_reason in ("HARD_STOP", "GRID_HARD_STOP"):
            try:
                from modules.symbol_blacklist import record_hard_stop
                record_hard_stop(symbol, pnl_equity_pct_net, exit_reason)
            except Exception as _bl_exc:
                log.warning(f"[Blacklist] record failed: {_bl_exc}")

        emoji = "✅" if pnl_usdt_net >= 0 else "❌"
        log.info(
            f"[LIVE] {emoji} CLOSE {symbol} {direction} @ {exit_price:.4f} | "
            f"Gross: {pnl_usdt:+.2f} USDT | Fee: −{fee_usdt:.3f} USDT | "
            f"Net: {pnl_usdt_net:+.2f} USDT ({pnl_equity_pct_net*100:+.3f}% equity) | "
            f"{exit_reason}"
        )
        return True

    # ── Reconcile with exchange ────────────────────────────────────────────────

    def reconcile_with_exchange(
        self,
        account_equity: float,
        symbol_data:    dict,
    ) -> list[dict]:
        """
        Compare bot's active list against Binance positionRisk.

        1. ORPHAN CLOSE: Positions on Binance not in bot's active list
           are immediately market-closed. No adoption, no fake trades.

        2. MANUAL CLOSE DETECTION: Positions in active list not on Binance
           are treated as manually/liquidation closed and removed cleanly.

        Returns list of positions that were manually closed (for P&L recording).
        """
        # Both branches below are gated on LIVE_ENABLED, so in PAPER mode this
        # fetch was a signed API call every cycle — every 5s with a position
        # open — whose result was then discarded. It burned rate limit and was
        # the source of spurious -1021 recvWindow warnings. There is no
        # exchange state to reconcile against when no orders are ever placed.
        if not LIVE_ENABLED:
            return []

        binance_positions = _get_binance_open_positions()

        # ── ORPHAN CLOSE (replaces ORPHAN ADOPTION) ──────────────────────────
        # Orphans are positions on Binance not tracked by the bot (residuals
        # from partial fills, restarts, or manual opens). Previously these were
        # adopted with a tight SL — but 20 adopted trades produced 19 losses.
        # Now: detect orphan → market-close immediately → no tracking.
        if LIVE_ENABLED:
            active_keys = {
                (p["symbol"], "LONG" if p["direction"] == "LONG" else "SHORT")
                for p in self.active
            }
            for (sym, pos_side), bpos in binance_positions.items():
                if (sym, pos_side) not in active_keys:
                    try:
                        entry_price = float(bpos.get("entryPrice", "0") or "0")
                        contracts   = abs(float(bpos.get("positionAmt", "0") or "0"))
                        direction   = "LONG" if pos_side == "LONG" else "SHORT"
                    except (ValueError, TypeError):
                        continue

                    if entry_price <= 0 or contracts <= 0:
                        continue

                    close_side = "SELL" if direction == "LONG" else "BUY"

                    spec = _get_contract_spec(sym)
                    qty  = _round_to_step(contracts, spec["step"])
                    if qty <= 0:
                        continue

                    log.warning(
                        f"[Reconcile] Orphan found: {sym} {direction} | "
                        f"entry={entry_price:.4f} qty={contracts} — closing immediately"
                    )

                    resp = _place_order(
                        symbol        = sym,
                        side          = close_side,
                        position_side = pos_side,
                        quantity      = str(qty),
                        order_type    = "MARKET",
                    )

                    if resp and resp.get("code") is None:
                        log.info(
                            f"[Reconcile] Orphan closed: {sym} {direction} | "
                            f"orderId={resp.get('orderId')}"
                        )
                    else:
                        log.error(
                            f"[Reconcile] Orphan close FAILED: {sym} {direction} | "
                            f"resp={resp}"
                        )

        # ── MANUAL CLOSE DETECTION ────────────────────────────────────────────
        manually_closed = []
        for pos in list(self.active):
            sym       = pos["symbol"]
            direction = pos["direction"]
            pos_side  = "LONG" if direction == "LONG" else "SHORT"

            if LIVE_ENABLED and (sym, pos_side) not in binance_positions:
                # Position not on exchange. Find out why BEFORE cancelling the
                # stop — a filled stop is the usual reason, and it is an SL
                # hit, not a manual close.
                exit_reason, exit_source, close_price = _classify_exchange_close(pos)
                _cancel_stop_order(sym, pos.get("stop_order_id"))
                if close_price is None:
                    close_price = pos["entry_price"]   # fallback

                log.warning(
                    f"[Reconcile] {exit_reason} on exchange: {sym} {direction} | "
                    f"close price ~{close_price:.4f} (source={exit_source})"
                )

                entry       = pos["entry_price"]
                pnl_pct     = (close_price - entry) / entry if direction == "LONG" \
                              else (entry - close_price) / entry
                pnl_usdt    = pos["contracts"] * abs(close_price - entry) * (1 if pnl_pct >= 0 else -1)
                fee_usdt    = pos.get("notional", pos["contracts"] * entry) * ROUND_TRIP_FEE
                pnl_net     = pnl_usdt - fee_usdt

                if account_equity > 0:
                    pnl_eq = pnl_net / account_equity
                else:
                    pnl_eq = pnl_pct

                pos.update({
                    "exit_time":      datetime.now(timezone.utc).isoformat(),
                    "exit_price":     close_price,
                    "exit_reason":    exit_reason,
                    "exit_source":    exit_source,
                    "pnl_pct":        round(pnl_pct, 6),
                    "pnl_usdt":       round(pnl_usdt, 4),
                    "pnl_usdt_net":   round(pnl_net, 4),
                    "fee_usdt":       round(fee_usdt, 4),
                    "pnl_equity_pct": round(pnl_eq, 6),
                    "pnl_pct_100":    round(pnl_eq * 100, 4),
                    "status":         "CLOSED",
                })
                self.session_pnl_pct += pnl_eq
                self.active.remove(pos)
                self.closed.append(pos)
                manually_closed.append(pos)

        return manually_closed

    # ── Helpers ────────────────────────────────────────────────────────────────

    def is_symbol_active(self, symbol: str) -> bool:
        return any(p["symbol"] == symbol for p in self.active)

    def n_open(self) -> int:
        return len(self.active)

    def open_symbols(self) -> list[str]:
        """Symbols with an open position. Callers used this before it existed."""
        return [p["symbol"] for p in self.active]

    def session_summary(self) -> dict:
        total  = len(self.closed)
        wins   = sum(1 for p in self.closed if p.get("pnl_equity_pct", p["pnl_pct"]) >= 0)
        losses = total - wins
        wr     = (wins / total * 100) if total > 0 else 0.0

        by_strategy: dict[str, dict] = {}
        for p in self.closed:
            sid = p.get("strategy", "?")
            if sid not in by_strategy:
                by_strategy[sid] = {"trades": 0, "wins": 0, "pnl_pct_sum": 0.0}
            by_strategy[sid]["trades"]      += 1
            by_strategy[sid]["pnl_pct_sum"] += p.get("pnl_equity_pct", p.get("pnl_pct", 0.0))
            if p.get("pnl_equity_pct", p.get("pnl_pct", 0.0)) >= 0:
                by_strategy[sid]["wins"] += 1

        return {
            "mode":          TRADE_MODE,
            "total_trades":  total,
            "wins":          wins,
            "losses":        losses,
            "win_rate":      round(wr, 1),
            "session_pnl":   round(self.session_pnl_pct * 100, 3),
            "open_trades":   len(self.active),
            "by_strategy":   by_strategy,
        }



