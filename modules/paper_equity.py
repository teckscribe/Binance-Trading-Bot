"""
paper_equity.py
Persistent simulated balance for PAPER mode.

In LIVE mode the account balance comes from Binance, so it already compounds:
every realized win or loss is reflected the next time get_account_equity()
runs. PAPER mode has no exchange to ask, so without this the equity stayed
pinned at ACCOUNT_EQUITY_USDT forever — position sizing never scaled with
performance and the dashboard's wallet balance never moved, which makes it
impossible to read cumulative performance off a paper run.

This module keeps a small JSON file holding the realized P&L accumulated
across paper trades. Paper equity is therefore:

    equity = starting_equity (ACCOUNT_EQUITY_USDT setting)  +  realized_pnl_usdt

It persists across restarts, so a multi-day paper run accumulates rather than
resetting to the configured figure every time the service bounces.

If ACCOUNT_EQUITY_USDT is changed (settings.json), the tracker resets to the new
starting figure — a different starting balance means a different experiment.
"""

import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("PaperEquity")

_MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_MODULE_DIR)
_STATE_FILE  = os.path.join(_PROJECT_DIR, "data", "paper_equity.json")


def _blank(starting: float) -> dict:
    return {
        "starting_equity":  round(starting, 4),
        "realized_pnl_usdt": 0.0,
        "trades":            0,
        "wins":              0,
        # When this equity period began. The dashboard's Performance tab scopes
        # its totals to this, so its Net P&L agrees with the wallet balance.
        # Without it the tab summed EVERY session log ever written — 152 trades
        # showing +8.57 while the wallet, tracking the 18 trades since the last
        # reset, showed -4.25. Both were correct and they contradicted visibly.
        "period_start":      datetime.now(timezone.utc).isoformat(),
        "updated":           datetime.now(timezone.utc).isoformat(),
    }


def _load(starting: float) -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _blank(starting)

    if not isinstance(data, dict):
        return _blank(starting)

    # A changed starting balance is a new experiment — don't carry P&L over.
    prev_start = float(data.get("starting_equity", 0.0) or 0.0)
    if abs(prev_start - starting) > 1e-9:
        log.info(
            f"Starting equity changed ${prev_start:.2f} → ${starting:.2f} "
            f"— resetting paper equity tracker"
        )
        return _blank(starting)

    data.setdefault("realized_pnl_usdt", 0.0)
    data.setdefault("trades", 0)
    data.setdefault("wins", 0)
    return data


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        data["updated"] = datetime.now(timezone.utc).isoformat()
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        log.warning(f"Could not persist paper equity: {exc}")


def record(starting: float, pnl_usdt_net: float) -> float:
    """
    Book a closed paper trade's net P&L and return the new simulated balance.
    Pass the NET figure (after round-trip fees) so the balance reflects what
    the account would actually hold.
    """
    data = _load(starting)
    data["realized_pnl_usdt"] = round(
        float(data.get("realized_pnl_usdt", 0.0)) + float(pnl_usdt_net), 4
    )
    data["trades"] = int(data.get("trades", 0)) + 1
    if pnl_usdt_net >= 0:
        data["wins"] = int(data.get("wins", 0)) + 1
    _save(data)

    equity = max(0.0, starting + data["realized_pnl_usdt"])
    log.info(
        f"Paper equity: ${equity:.2f} "
        f"(realized {data['realized_pnl_usdt']:+.2f} over "
        f"{data['trades']} trade{'s' if data['trades'] != 1 else ''})"
    )
    return equity


def summary(starting: float) -> dict:
    """Stats for the dashboard / notifications."""
    data = _load(starting)
    realized = float(data.get("realized_pnl_usdt", 0.0))
    trades   = int(data.get("trades", 0))
    return {
        "starting_equity":   starting,
        "realized_pnl_usdt": realized,
        "equity":            max(0.0, starting + realized),
        "return_pct":        (realized / starting) if starting > 0 else 0.0,
        "trades":            trades,
        "wins":              int(data.get("wins", 0)),
        "period_start":      data.get("period_start"),
    }


def reset(starting: float) -> None:
    """Wipe accumulated paper P&L and start again from `starting`."""
    _save(_blank(starting))
    log.info(f"Paper equity tracker reset to ${starting:.2f}")


