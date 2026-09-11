"""
Port of Freqtrade strategy: ElliotV8_original_ichiv2.py
Adapted for BaseStrategy architecture using 5m resampled candles.
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
import math
from .base_strategy import BaseStrategy, make_exit, no_exit

def EWO(df, ema_length=5, ema2_length=3):
    ema1 = ta.ema(df['close'], length=ema_length)
    ema2 = ta.ema(df['close'], length=ema2_length)
    return (ema1 - ema2) / df['close'] * 100

def hull_moving_average(close: pd.Series, window: int) -> pd.Series:
    half_length = int(window / 2)
    sqrt_length = int(math.sqrt(window))
    wmaf = ta.wma(close, length=half_length)
    wmas = ta.wma(close, length=window)
    return ta.wma(wmaf * 2 - wmas, length=sqrt_length)

class ElliotV8Port(BaseStrategy):
    STRATEGY_ID = "ELLIOT_V8"
    REQUIRES_1H = False
    REQUIRES_1M_DEPTH = 1500

    def scan(self, symbol: str, df_1m: pd.DataFrame, df_15m: pd.DataFrame, df_1h: pd.DataFrame, regime: dict) -> dict | None:
        if df_1m is None or len(df_1m) < 1000:
            return None

        # Resample to 5m
        df_5m = df_1m.resample('5min', on='timestamp').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna() if 'timestamp' in df_1m.columns else df_1m.resample('5min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        if len(df_5m) < 200:
            return None

        close = df_5m['close']
        volume = df_5m['volume']

        base_nb_candles_buy = 12
        base_nb_candles_sell = 22
        low_offset = 0.987
        high_offset = 1.008
        ewo_high = 3.147
        ewo_low = -17.145
        rsi_buy = 57

        ma_buy = ta.ema(close, length=base_nb_candles_buy)
        ma_sell = ta.ema(close, length=base_nb_candles_sell)
        ewo = EWO(df_5m, 50, 200)
        rsi = ta.rsi(close, length=14)
        rsi_fast = ta.rsi(close, length=4)

        i = -2

        cond1 = (rsi_fast.iloc[i] < 35) and \
                (close.iloc[i] < (ma_buy.iloc[i] * low_offset)) and \
                (ewo.iloc[i] > ewo_high) and \
                (rsi.iloc[i] < rsi_buy) and \
                (volume.iloc[i] > 0) and \
                (close.iloc[i] < (ma_sell.iloc[i] * high_offset))

        cond2 = (rsi_fast.iloc[i] < 35) and \
                (close.iloc[i] < (ma_buy.iloc[i] * low_offset)) and \
                (ewo.iloc[i] < ewo_low) and \
                (volume.iloc[i] > 0) and \
                (close.iloc[i] < (ma_sell.iloc[i] * high_offset))

        if cond1 or cond2:
            entry_price = float(df_1m['close'].iloc[-1])
            # Flat 8% stop, as ported. Briefly cut to 3% while risk_engine's
            # MAX_SL_PCT was set to 0.03 — which would otherwise have REJECTED
            # every signal from this port and silently stopped it trading.
            # MAX_SL_PCT is now 0.10, so the original stop is restored.
            #
            # Do not tighten this without measuring: the strategy is built
            # around a wide stop (~72% win rate at R:R ~0.45), and its edge
            # depends on giving trades room. It is well inside the 10% ceiling.
            sl_price = entry_price * (1 - 0.08)
            atr = float(ta.atr(df_1m['high'], df_1m['low'], df_1m['close'], length=14).iloc[-1])
            tp_price = entry_price + atr * 3.0

            # Signal strength = how deep the 5m fast-RSI washout is. Entry
            # already requires rsi_fast < 35, so this maps [35 -> 0] onto
            # [0.0 -> 1.0]. live_scanner drops anything below MIN_STRENGTH
            # (0.50, i.e. rsi_fast < 17.5) and ranks the rest.
            #
            # ELLIOT_V8 is the ONLY strategy that opts into this gate, because
            # it is the only one the gate helps. Measured 90d / 100 symbols on
            # the fixed harness with 0.03%/side slippage:
            #
            #   ungated  1018 trades  E=-0.038%  total  -39%
            #   gated     430 trades  E=+0.144%  total  +62%   (+101pp)
            #
            # Ungated, this strategy is a net loser: 72% win rate but R:R 0.61,
            # picking up small wins in front of an 8% stop. The gate keeps only
            # the deep washouts, which is where its edge actually lives.
            #
            # The SAME gate COSTS NASOS_V4 242pp (see freqtrade_port_nasos.py),
            # so do not generalise this to the other ports.
            rsi_val = float(rsi_fast.iloc[i])
            strength_val = max(0.0, min(1.0, (35.0 - rsi_val) / 35.0))

            return {
                'symbol': symbol,
                'strategy': self.STRATEGY_ID,
                'direction': 'LONG',
                'entry_price': entry_price,
                'sl_price': sl_price,
                'tp_price': tp_price,
                'atr': atr,
                'strength': strength_val,
                'reason': 'ElliotV8_Entry',
                'leverage': 1
            }

        return None

    def manage(self, position: dict, df_1m: pd.DataFrame, session_pnl: float) -> dict:
        if df_1m is None or len(df_1m) < 150:
            return no_exit()

        df_5m = df_1m.resample('5min', on='timestamp').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna() if 'timestamp' in df_1m.columns else df_1m.resample('5min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()

        if len(df_5m) < 55:
            return no_exit()

        close = df_5m['close']
        volume = df_5m['volume']
        hma_50 = hull_moving_average(close, window=50)
        ma_sell = ta.ema(close, length=22)
        rsi = ta.rsi(close, length=14)
        rsi_fast = ta.rsi(close, length=4)
        rsi_slow = ta.rsi(close, length=20)

        i = -2
        
        high_offset_2 = 1.016
        high_offset = 1.008

        cond1 = (close.iloc[i] > hma_50.iloc[i]) and \
                (close.iloc[i] > (ma_sell.iloc[i] * high_offset_2)) and \
                (rsi.iloc[i] > 50) and \
                (volume.iloc[i] > 0) and \
                (rsi_fast.iloc[i] > rsi_slow.iloc[i])

        cond2 = (close.iloc[i] < hma_50.iloc[i]) and \
                (close.iloc[i] > (ma_sell.iloc[i] * high_offset)) and \
                (volume.iloc[i] > 0) and \
                (rsi_fast.iloc[i] > rsi_slow.iloc[i])

        if cond1 or cond2:
            return make_exit(float(df_1m['close'].iloc[-1]), "ELLIOT_SELL")

        return no_exit()


