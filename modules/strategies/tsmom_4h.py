"""
modules/strategies/tsmom_4h.py
Time-Series Momentum on 4-Hour Timeframe (TSMOM_4H).

Academic Basis:
  Moskowitz, Ooi, Pedersen (2012) - "Time Series Momentum", Journal of Financial Economics.
  Adapted for 24/7 Crypto Perpetual Futures on Binance USDM (ported from DCB for a cross-venue backtest).

Key Mechanics:
  1. Timeframe: 4-Hour bars (resampled from 1h).
  2. Lookback: 18 bars (72 hours = 3 days).
  3. Signal: Excess return over 72 hours.
  4. Position Sizing / Strength: Sized inversely to volatility (1 / ATR(4h)),
     allocating larger capital to stable trenders (BTC/ETH) and smaller to high-beta alts.
  5. Low Turnover: 24h holding period (6 bars of 4h), reducing fee drag by >80%.
"""

import pandas as pd
import numpy as np
from modules import settings_manager as cfg
from modules.strategies.base_strategy import BaseStrategy


def _time_indexed(df):
    """
    The live data feed returns a RangeIndex with a `timestamp` column; the
    backtest harness returns a DatetimeIndex. resample() and .hour need the
    latter. Without this the strategy silently never fired live (resample
    raised -> None) while the harness measured it fine.
    """
    if df is None or len(df) == 0:
        return df
    if "timestamp" in df.columns:
        return df.set_index(pd.to_datetime(df["timestamp"], utc=True))
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    return df


class TSMOM4HStrategy(BaseStrategy):
    STRATEGY_ID = "TSMOM_4H"
    REQUIRES_1H = True
    REQUIRES_1M_DEPTH = 300

    LOOKBACK_4H_BARS = 18  # 72 hours
    HOLD_MINUTES = 1440    # 24 hours
    SL_ATR_MULT = 2.0
    TP_ATR_MULT = 4.0
    MIN_MOM_PCT = 0.05     # default; DCB 1.6.5: 1.5% admits ~850 fee-paying noise trades

    def scan(self, symbol: str, df_1m: pd.DataFrame, df_15m: pd.DataFrame, df_1h: pd.DataFrame, regime: dict) -> dict:
        if df_1h is None or len(df_1h) < (self.LOOKBACK_4H_BARS * 4 + 20):
            return None
        df_1h = _time_indexed(df_1h)
        if df_1h is None:
            return None

        # Resample 1h to 4h bars
        try:
            df_4h = df_1h.resample("4h").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna()
        except Exception:
            return None

        if len(df_4h) < (self.LOOKBACK_4H_BARS + 15):
            return None

        c4h = df_4h["close"].astype(float)
        # Momentum is measured on closed 4h bars; the ENTRY price is the live
        # 1m close. The last closed 4h bar can be up to 4h stale - using it as
        # the entry price both mis-sizes the live order and, in the strict
        # as-of backtest, fills at a price the market has already left.
        last_4h_close = float(c4h.iloc[-1])
        current_price = float(df_1m["close"].iloc[-1]) if df_1m is not None and len(df_1m) else last_4h_close
        prev_price = float(c4h.iloc[-self.LOOKBACK_4H_BARS - 1])

        # 72h momentum
        mom_pct = (last_4h_close - prev_price) / prev_price

        # ATR(14) on 4h
        tr1 = df_4h["high"] - df_4h["low"]
        tr2 = (df_4h["high"] - df_4h["close"].shift()).abs()
        tr3 = (df_4h["low"] - df_4h["close"].shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_4h = float(tr.rolling(14).mean().iloc[-1])

        if atr_4h <= 0 or current_price <= 0:
            return None

        vol_norm = atr_4h / current_price  # Normalized ATR volatility

        # Directional Filter: Positive momentum exceeding minimum threshold
        try:
            min_mom = float(cfg.get("TSMOM_MIN_MOM_PCT"))
        except Exception:
            min_mom = self.MIN_MOM_PCT
        if mom_pct < min_mom:
            return None

        # Inverse Volatility Weighting / Conviction Strength
        # Lower volatility coins get higher weight; normalized to 0.1 - 1.0
        inv_vol = 1.0 / max(0.01, vol_norm)
        strength = min(1.0, max(0.1, round(inv_vol / 40.0, 3)))

        sl_dist = atr_4h * self.SL_ATR_MULT
        tp_dist = atr_4h * self.TP_ATR_MULT

        sl_price = current_price - sl_dist
        tp_price = current_price + tp_dist

        return {
            "symbol": symbol,
            "strategy": self.STRATEGY_ID,
            "direction": "LONG",
            "entry_price": current_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "atr": atr_4h,
            "qty": 0.0,
            "strength": strength,
            "reason": f"TSMOM_4H (72h_mom={mom_pct*100:+.2f}%, vol={vol_norm*100:.2f}%, inv_vol={inv_vol:.1f})",
        }

    def manage(self, position: dict, df_1m: pd.DataFrame, session_pnl: float) -> dict:
        if df_1m is None or df_1m.empty:
            return {"exit": False, "exit_price": 0.0, "exit_reason": ""}

        current_price = float(df_1m["close"].iloc[-1])
        entry_price = float(position["entry_price"])
        direction = position.get("direction", "LONG")

        # 1. 24-Hour Max Hold (rebalance cycle)
        try:
            entry_t = pd.to_datetime(position["entry_time"], utc=True)
            bar_t = pd.to_datetime(
                df_1m["timestamp"].iloc[-1] if "timestamp" in df_1m.columns else df_1m.index[-1],
                utc=True
            )
            duration_min = (bar_t - entry_t).total_seconds() / 60.0
            if duration_min >= self.HOLD_MINUTES:
                return {"exit": True, "exit_price": current_price, "exit_reason": "MAX_HOLD_24H"}
        except Exception:
            pass

        # 2. Hard Stop Loss & Take Profit
        sl_price = float(position.get("sl_price", 0.0))
        tp_price = float(position.get("tp_price", 0.0))

        if direction == "LONG":
            if tp_price > 0 and current_price >= tp_price:
                return {"exit": True, "exit_price": current_price, "exit_reason": "TP_HIT"}
            if sl_price > 0 and current_price <= sl_price:
                return {"exit": True, "exit_price": current_price, "exit_reason": "SL_HIT"}
        else:
            if tp_price > 0 and current_price <= tp_price:
                return {"exit": True, "exit_price": current_price, "exit_reason": "TP_HIT"}
            if sl_price > 0 and current_price >= sl_price:
                return {"exit": True, "exit_price": current_price, "exit_reason": "SL_HIT"}

        return {"exit": False, "exit_price": 0.0, "exit_reason": ""}
