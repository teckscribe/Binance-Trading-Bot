"""
modules/binance_status.py
Read-only Binance USDM Futures status fetcher for accurate Telegram display.

Independent of trading code — calls /fapi/v2/account directly to get the
exact wallet balance and unrealized PnL that Binance shows in its app.
Used by telegram_bot.py for /status display so the numbers match Binance
1:1 (instead of the bot's internal closed-trade tracker which can drift
on MANUAL_CLOSE / fee / funding events).

Public API:
    get_wallet_state()  -> dict with wallet_balance, unrealized_pnl,
                           margin_balance (single API call).
    get_today_pnl()     -> dict with day_realized_usdt, day_realized_pct,
                           day_total_usdt, day_total_pct (incl. unrealized).
    get_week_pnl()      -> same for current ISO week.

Caching:
    Day/week start equity snapshots persist in data/binance_status_cache.json.
    Wallet state is fetched fresh each call (no cache — small overhead, ~1 req).
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone, timedelta

from modules.auth_manager import BASE_URL, api_headers, sign_params

log = logging.getLogger("BinanceStatus")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE  = os.path.join(_PROJECT_DIR, "data", "binance_status_cache.json")


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_week() -> str:
    n = datetime.now(timezone.utc)
    return f"{n.isocalendar()[0]}-W{n.isocalendar()[1]:02d}"


def _load_cache() -> dict:
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, encoding="utf-8") as _jf:
                return json.load(_jf)
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning(f"Cache save failed: {exc}")


# ────────────────────────────────────────────────────────────────────────────
# Live wallet state (1 API call)
# ────────────────────────────────────────────────────────────────────────────

def get_wallet_state() -> dict | None:
    """
    Fetch current wallet balance + unrealized PnL from Binance USDM Futures.

    Returns:
        {
          "wallet_balance":   float,  # totalWalletBalance (realized only)
          "unrealized_pnl":   float,  # totalUnrealizedProfit (open positions)
          "margin_balance":   float,  # totalMarginBalance = wallet + unrealized
          "available":        float,  # availableBalance (free margin)
        }
        or None on API failure.
    """
    params = sign_params({})
    try:
        resp = requests.get(
            BASE_URL + "/fapi/v2/account",
            params=params,
            headers=api_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"account fetch failed: HTTP {resp.status_code}")
            return None
        data = resp.json()
        return {
            "wallet_balance":  float(data.get("totalWalletBalance",   0) or 0),
            "unrealized_pnl":  float(data.get("totalUnrealizedProfit", 0) or 0),
            "margin_balance":  float(data.get("totalMarginBalance",   0) or 0),
            "available":       float(data.get("availableBalance",     0) or 0),
        }
    except Exception as exc:
        log.warning(f"get_wallet_state exception: {exc}")
        return None


# ────────────────────────────────────────────────────────────────────────────
# Period-start snapshot (auto-rollover at UTC day/week boundary)
# ────────────────────────────────────────────────────────────────────────────

def _ensure_period_starts(margin_balance: float) -> dict:
    """
    Idempotent: snapshot day/week start margin balance on first call of the
    period. Returns {day_start, week_start, day, week}.
    """
    cache = _load_cache()
    today = _utc_today()
    week  = _utc_week()
    changed = False

    if cache.get("day") != today or cache.get("day_start", 0) <= 0:
        cache["day"]       = today
        cache["day_start"] = round(float(margin_balance), 4)
        log.info(f"[BinanceStatus] Day rollover {today} — start ${margin_balance:.4f}")
        changed = True

    if cache.get("week") != week or cache.get("week_start", 0) <= 0:
        cache["week"]       = week
        cache["week_start"] = round(float(margin_balance), 4)
        log.info(f"[BinanceStatus] Week rollover {week} — start ${margin_balance:.4f}")
        changed = True

    if changed:
        _save_cache(cache)
    return cache


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def get_pnl_summary() -> dict | None:
    """
    Returns Binance-matching day & week PnL, both in USDT and %.

    Day/Week start = margin balance snapshot at first UTC day/week start.
    Current = live margin balance (wallet + unrealized).
    Delta = current - start.

    This matches what the Binance app shows because it uses the SAME
    totalMarginBalance the app displays.

    Returns dict or None on API failure:
        {
          "wallet_balance":  float,
          "unrealized_pnl":  float,
          "margin_balance":  float,
          "day_start":       float,
          "week_start":      float,
          "day_pnl_usdt":    float,   # (margin_now - day_start)
          "day_pnl_pct":     float,   # %
          "week_pnl_usdt":   float,
          "week_pnl_pct":    float,
        }
    """
    state = get_wallet_state()
    if state is None:
        return None

    margin_now = state["margin_balance"]
    cache = _ensure_period_starts(margin_now)
    day_start  = float(cache.get("day_start",  0) or 0)
    week_start = float(cache.get("week_start", 0) or 0)

    day_usdt   = margin_now - day_start  if day_start  > 0 else 0.0
    week_usdt  = margin_now - week_start if week_start > 0 else 0.0
    day_pct    = (day_usdt  / day_start)  * 100 if day_start  > 0 else 0.0
    week_pct   = (week_usdt / week_start) * 100 if week_start > 0 else 0.0

    return {
        "wallet_balance":  round(state["wallet_balance"], 4),
        "unrealized_pnl":  round(state["unrealized_pnl"], 4),
        "margin_balance":  round(margin_now,              4),
        "day_start":       round(day_start,               4),
        "week_start":      round(week_start,              4),
        "day_pnl_usdt":    round(day_usdt,                4),
        "day_pnl_pct":     round(day_pct,                 3),
        "week_pnl_usdt":   round(week_usdt,               4),
        "week_pnl_pct":    round(week_pct,                3),
    }


# ────────────────────────────────────────────────────────────────────────────
# Per-strategy PnL (matches Binance fills back to bot's strategy logs)
# ────────────────────────────────────────────────────────────────────────────

# Bot's strategy log directory (logs/strategies/{STRAT}/{STRAT}_YYYY-MM-DD.json)
#
# 2026-08-04: was hardcoded to the pre-2026 set ["TP","FF","DB","GRID","BBR"] —
# the SIXTH stale copy of this list. Per-strategy Binance P&L matching read log
# folders for strategies that never trade and ignored every strategy that does,
# so the bots' "Binance live P&L per strategy" section was permanently empty.
def _production_strats() -> list[str]:
    try:
        from modules.strategies.strategy_factory import StrategyFactory
        ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
        if ids:
            return ids
    except Exception:
        pass
    return ["CSM", "NASOS_V4", "TSMOM_4H", "REBALANCING_PREMIUM"]


_STRATS = _production_strats()
_LOG_DIR = os.path.join(_PROJECT_DIR, "logs", "strategies")

# Match window: Binance income event time must be within this many seconds
# of the bot's logged exit_time to attribute the PnL to that strategy.
_MATCH_WINDOW_SEC = 90


def _fetch_income_events(
    start_ms:    int,
    end_ms:      int | None = None,
    income_type: str        = "REALIZED_PNL",
) -> list[dict]:
    """
    Fetch /fapi/v1/income filtered by income type and time range.
    Paginates if more than 1000 events. Returns list of {symbol, time, income}.
    """
    events = []
    cursor = start_ms
    while True:
        params_dict = {
            "incomeType": income_type,
            "startTime":  cursor,
            "limit":      1000,
        }
        if end_ms:
            params_dict["endTime"] = end_ms
        params = sign_params(params_dict)
        try:
            resp = requests.get(
                BASE_URL + "/fapi/v1/income",
                params=params,
                headers=api_headers(),
                timeout=15,
            )
            if resp.status_code != 200:
                log.warning(
                    f"income fetch failed: HTTP {resp.status_code} "
                    f"({income_type})"
                )
                break
            batch = resp.json() or []
        except Exception as exc:
            log.warning(f"income fetch exception ({income_type}): {exc}")
            break

        if not batch:
            break
        events.extend(batch)
        if len(batch) < 1000:
            break
        # Advance cursor past last event time +1ms
        cursor = int(batch[-1].get("time", cursor)) + 1
        if end_ms and cursor >= end_ms:
            break
    return events


def _read_strategy_exits(start_ms: int) -> list[dict]:
    """
    Read bot's per-strategy logs and return list of EXIT entries with
    (symbol, exit_time_ms, strategy). Only includes exits >= start_ms.
    """
    exits = []
    if not os.path.isdir(_LOG_DIR):
        return exits
    for strat in _STRATS:
        sdir = os.path.join(_LOG_DIR, strat)
        if not os.path.isdir(sdir):
            continue
        for fname in os.listdir(sdir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sdir, fname)
            try:
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if r.get("type") != "EXIT":
                            continue
                        ts_str = r.get("time")
                        if not ts_str:
                            continue
                        try:
                            dt = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            )
                            ts_ms = int(dt.timestamp() * 1000)
                        except Exception:
                            continue
                        if ts_ms < start_ms:
                            continue
                        exits.append({
                            "symbol":   r.get("symbol", ""),
                            "exit_ms":  ts_ms,
                            "strategy": strat,
                            "exit_reason": r.get("exit_reason", ""),
                        })
            except Exception:
                continue
    exits.sort(key=lambda x: x["exit_ms"])
    return exits


def _attribute_event_to_strategy(
    event:  dict,
    exits:  list[dict],
    used:   set,
) -> str | None:
    """
    Match a Binance income event to a bot exit by symbol + nearest time.
    Returns the strategy name or None if no match within window.
    `used` tracks already-matched exit indices to avoid double-attribution.
    """
    sym  = event.get("symbol", "")
    e_ms = int(event.get("time", 0))
    if not sym or e_ms == 0:
        return None
    best_idx = -1
    best_dt  = _MATCH_WINDOW_SEC * 1000 + 1
    for i, ex in enumerate(exits):
        if i in used:
            continue
        if ex["symbol"] != sym:
            continue
        dt = abs(ex["exit_ms"] - e_ms)
        if dt < best_dt:
            best_dt  = dt
            best_idx = i
    if best_idx < 0:
        return None
    used.add(best_idx)
    return exits[best_idx]["strategy"]


def get_per_strategy_pnl(period: str = "today") -> dict | None:
    """
    Returns per-strategy realized PnL exactly as Binance recorded it.

    Args:
        period : "today" | "week" | "all" — time window.

    Approach:
        1. Compute period start_ms (UTC day or week boundary).
        2. Fetch /fapi/v1/income (REALIZED_PNL + COMMISSION + FUNDING_FEE).
        3. Read bot's strategy logs (logs/strategies/{S}/*.json) for EXIT entries.
        4. Match each income event to a strategy by symbol + ±90s window.
        5. Aggregate per strategy.

    Returns dict or None on API failure:
        {
          "period_start_iso":   str,
          "TP":  {"trades": N, "wins": N, "realized_usdt": float,
                  "fees_usdt": float, "funding_usdt": float, "net_usdt": float},
          ...
          "TOTAL": {... aggregate ...},
          "unmatched_usdt": float,   # PnL events with no matching bot log
        }
    """
    # Determine start
    now = datetime.now(timezone.utc)
    if period == "today":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        # ISO week start (Monday 00:00 UTC)
        start_dt = (
            now - timedelta(days=now.weekday())
        ).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "all":
        # Hard cap at 90 days back — Binance income endpoint has limits
        start_dt = now - timedelta(days=90)
    else:
        log.warning(f"get_per_strategy_pnl: unknown period {period}")
        return None
    start_ms = int(start_dt.timestamp() * 1000)

    # Fetch all 3 income streams
    realized = _fetch_income_events(start_ms, income_type="REALIZED_PNL")
    fees     = _fetch_income_events(start_ms, income_type="COMMISSION")
    funding  = _fetch_income_events(start_ms, income_type="FUNDING_FEE")

    # Read bot strategy logs (with 60s pre-window for clock skew)
    exits = _read_strategy_exits(start_ms - 60_000)

    # Aggregate per-strategy
    agg: dict = {s: {
        "trades":        0,
        "wins":          0,
        "realized_usdt": 0.0,
        "fees_usdt":     0.0,
        "funding_usdt":  0.0,
    } for s in _STRATS}
    unmatched_usdt = 0.0

    # Match REALIZED_PNL events (these are the trade-level outcomes)
    used_realized: set = set()
    for ev in realized:
        amount = float(ev.get("income", 0) or 0)
        strat  = _attribute_event_to_strategy(ev, exits, used_realized)
        if strat and strat in agg:
            agg[strat]["trades"]        += 1
            agg[strat]["realized_usdt"] += amount
            if amount > 0:
                agg[strat]["wins"] += 1
        else:
            unmatched_usdt += amount

    # Match COMMISSION events
    used_fees: set = set()
    for ev in fees:
        amount = float(ev.get("income", 0) or 0)   # negative for fees paid
        strat  = _attribute_event_to_strategy(ev, exits, used_fees)
        if strat and strat in agg:
            agg[strat]["fees_usdt"] += amount

    # Match FUNDING_FEE events (apply to nearest strategy by open-window)
    used_funding: set = set()
    for ev in funding:
        amount = float(ev.get("income", 0) or 0)
        strat  = _attribute_event_to_strategy(ev, exits, used_funding)
        if strat and strat in agg:
            agg[strat]["funding_usdt"] += amount

    # Compute net + total
    total = {
        "trades":        0,
        "wins":          0,
        "realized_usdt": 0.0,
        "fees_usdt":     0.0,
        "funding_usdt":  0.0,
        "net_usdt":      0.0,
    }
    for s, d in agg.items():
        d["net_usdt"] = d["realized_usdt"] + d["fees_usdt"] + d["funding_usdt"]
        for k in ("trades", "wins", "realized_usdt", "fees_usdt",
                  "funding_usdt"):
            total[k] += d[k]
    total["net_usdt"] = (
        total["realized_usdt"] + total["fees_usdt"] + total["funding_usdt"]
    )

    # Round + drop empty strategies
    out: dict = {
        "period":           period,
        "period_start_iso": start_dt.isoformat(),
        "unmatched_usdt":   round(unmatched_usdt, 4),
    }
    for s, d in agg.items():
        if d["trades"] == 0 and abs(d["realized_usdt"]) < 1e-6:
            continue
        out[s] = {
            "trades":        d["trades"],
            "wins":          d["wins"],
            "realized_usdt": round(d["realized_usdt"], 4),
            "fees_usdt":     round(d["fees_usdt"],     4),
            "funding_usdt":  round(d["funding_usdt"],  4),
            "net_usdt":      round(d["net_usdt"],      4),
        }
    out["TOTAL"] = {
        "trades":        total["trades"],
        "wins":          total["wins"],
        "realized_usdt": round(total["realized_usdt"], 4),
        "fees_usdt":     round(total["fees_usdt"],     4),
        "funding_usdt":  round(total["funding_usdt"],  4),
        "net_usdt":      round(total["net_usdt"],      4),
    }
    return out



