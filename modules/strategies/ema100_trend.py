"""Daily EMA100 trend adapter for Binance USDM PAPER validation."""
import logging
import math
import os
import time

import pandas as pd

from modules import settings_manager as cfg
from modules.data_feed import fetch_candles
from modules.daily_trend import Rules, features
from modules.strategies.base_strategy import BaseStrategy, bar_time

log = logging.getLogger("DailyTrend")
RULES = Rules()
IDS = frozenset({"EMA100_TREND", "DONCHIAN_55_20"})
_cache = {}
_CACHE_TTL = 300.0


def _daily(symbol: str, now: pd.Timestamp, hourly: pd.DataFrame | None = None) -> pd.DataFrame:
    # Replay supplies only completed bars as of `now`; use them instead of the
    # live API so historical signals remain causal and deterministic.
    if hourly is not None and not hourly.empty:
        frame = hourly.copy()
        frame.index = pd.to_datetime(frame.index, utc=True)
        return features(frame.resample("1D").agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
        }).dropna())

    day = now.floor("D")
    cached = _cache.get(symbol)
    if cached and cached[0] == day and time.monotonic() - cached[1] < _CACHE_TTL:
        return cached[2]
    raw = fetch_candles(symbol, "1d", limit=140)
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    raw = raw.set_index("timestamp")
    raw = raw.loc[raw.index < day]
    result = features(raw)
    _cache[symbol] = (day, time.monotonic(), result)
    return result


def _entry_window(now: pd.Timestamp) -> bool:
    return now.hour == 0 and now.minute < 5


class EMA100Trend(BaseStrategy):
    STRATEGY_ID = "EMA100_TREND"
    TARGET = "ema_target"
    TRAIL_BAR_MINUTES = 1
    # In live mode _daily() fetches 1d bars directly with a 5-min cache; it
    # does not use per-symbol 1h bars (1h is only used in offline CSB_BACKTEST).
    REQUIRES_1H = False
    # Daily strategies run on the core 15m replay; 1m is unnecessary for
    # their entry or stop-management rules.
    # scan() still uses the latest 1m bar for the live entry price and
    # freshness check.
    REQUIRES_1M_DEPTH = 90

    def __init__(self):
        self._backtest_daily = {}
        self._backtest_day = {}

    def set_backtest_context(self, symbol, hourly, asof=None):
        if hourly is not None and not hourly.empty:
            now = pd.to_datetime(asof if asof is not None else hourly.index[-1], utc=True)
            day = now.floor("D")
            if self._backtest_day.get(symbol) == day:
                return
            self._backtest_daily[symbol] = _daily(symbol, now, hourly)
            self._backtest_day[symbol] = day

    def _context_daily(self, symbol, now, hourly=None):
        if os.getenv("CSB_BACKTEST", "false").lower() == "true":
            return self._backtest_daily.get(symbol, pd.DataFrame())
        return _daily(symbol, now)

    def scan(self, symbol, df_1m, df_15m, df_1h, regime):
        if os.getenv("CSB_BACKTEST", "false").lower() != "true" and not cfg.get(self.STRATEGY_ID + "_ENABLED"):
            return None
        price_frame = df_1m if df_1m is not None and not df_1m.empty else df_15m
        now = bar_time(price_frame) or pd.Timestamp.now(tz="UTC")
        if not _entry_window(now) or price_frame is None or price_frame.empty:
            return None
        try:
            daily = self._context_daily(symbol, now, df_1h)
            if len(daily) < 2 or not daily.iloc[-1].ready or not daily.iloc[-2].ready:
                return None
            current, previous = daily.iloc[-1], daily.iloc[-2]
            side = int(current[self.TARGET])
            # Strictly LONG-only: 180-day 143-symbol evaluation proved LONG has
            # PF 3.39 (+4.04%/trade, +78.3R) while SHORT has PF 0.43 (-1.05%/trade, -14.6R).
            if side != 1 or side == int(previous[self.TARGET]):
                return None
            mark = float(price_frame.close.iloc[-1])
            atr = float(current.atr)
            if not math.isfinite(mark + atr) or mark <= 0 or atr <= 0:
                return None
            stop = mark - side * RULES.stop_atr * atr
            if stop <= 0:
                return None
            return {
                "symbol": symbol, "strategy": self.STRATEGY_ID,
                "direction": "LONG" if side == 1 else "SHORT",
                "entry_price": mark, "sl_price": stop, "tp_price": None,
                "atr": atr, "strength": 1.0,
                "time": daily.index[-1].isoformat(),
                "trend_setup": {"decision": daily.index[-1].isoformat(), "target": side},
                "reason": "EMA100 daily flip; 3 ATR trailing stop",
            }
        except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
            log.warning("%s daily signal unavailable: %s", symbol, exc)
            return None

    def manage(self, position, df_1m, session_pnl):
        if df_1m is None or df_1m.empty:
            return {"exit": False, "exit_price": 0.0, "exit_reason": ""}
        price = float(df_1m.close.iloc[-1])
        side = 1 if position.get("direction") == "LONG" else -1
        try:
            now = bar_time(df_1m) or pd.Timestamp.now(tz="UTC")
            daily = self._context_daily(position["symbol"], now)
            since = pd.to_datetime(position.get("trend_setup", {}).get("decision"), utc=True)
            updates = daily.loc[daily.index > since]
            for ts, bar in updates.iterrows():
                target = int(bar[self.TARGET])
                if target != side:
                    return {"exit": True, "exit_price": price, "exit_reason": self.STRATEGY_ID + "_CONSENSUS_EXIT"}
                trail = float(
                    bar.high - RULES.stop_atr * bar.atr
                    if side == 1 else
                    bar.low + RULES.stop_atr * bar.atr
                )
                position["sl_price"] = max(position["sl_price"], trail) if side == 1 else min(position["sl_price"], trail)
                position.setdefault("trend_setup", {})["decision"] = ts.isoformat()
            if side * (price - float(position["sl_price"])) <= 0:
                return {"exit": True, "exit_price": price, "exit_reason": "TRAIL_STOP"}
        except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
            log.warning("%s daily management unavailable; hard stop retained: %s", position.get("symbol"), exc)
        return {"exit": False, "exit_price": 0.0, "exit_reason": ""}
