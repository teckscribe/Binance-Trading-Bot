"""
Port of Freqtrade strategy: NotAnotherSMAOffsetStrategyHOv3.py
Adapted for BaseStrategy architecture using 5m resampled candles.
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
import math
from .base_strategy import BaseStrategy, make_exit, no_exit

def EWO(df, ema_length=5, ema2_length=35):
    ema1 = ta.ema(df['close'], length=ema_length)
    ema2 = ta.ema(df['close'], length=ema2_length)
    return (ema1 - ema2) / df['low'] * 100

def hull_moving_average(close: pd.Series, window: int) -> pd.Series:
    half_length = int(window / 2)
    sqrt_length = int(math.sqrt(window))
    wmaf = ta.wma(close, length=half_length)
    wmas = ta.wma(close, length=window)
    return ta.wma(wmaf * 2 - wmas, length=sqrt_length)

class SMAOffsetPort(BaseStrategy):
    STRATEGY_ID = "SMA_OFFSET"
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

        base_nb_candles_buy = 8
        base_nb_candles_sell = 16
        low_offset = 0.986
        low_offset_2 = 0.944
        high_offset = 1.054
        ewo_high = 4.179
        ewo_high_2 = -2.609
        ewo_low = -16.917
        rsi_buy = 58

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
                (close.iloc[i] < (ma_buy.iloc[i] * low_offset_2)) and \
                (ewo.iloc[i] > ewo_high_2) and \
                (rsi.iloc[i] < rsi_buy) and \
                (volume.iloc[i] > 0) and \
                (close.iloc[i] < (ma_sell.iloc[i] * high_offset)) and \
                (rsi.iloc[i] < 25)

        cond3 = (rsi_fast.iloc[i] < 35) and \
                (close.iloc[i] < (ma_buy.iloc[i] * low_offset)) and \
                (ewo.iloc[i] < ewo_low) and \
                (volume.iloc[i] > 0) and \
                (close.iloc[i] < (ma_sell.iloc[i] * high_offset))

        if cond1 or cond2 or cond3:
            entry_price = float(df_1m['close'].iloc[-1])
            sl_price = entry_price * (1 - 0.08)
            atr = float(ta.atr(df_1m['high'], df_1m['low'], df_1m['close'], length=14).iloc[-1])
            tp_price = entry_price + atr * 3.0

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
                'reason': 'SMA_OFFSET_Entry',
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
        sma_9 = ta.sma(close, length=9)
        ma_sell = ta.ema(close, length=16)
        rsi = ta.rsi(close, length=14)
        rsi_fast = ta.rsi(close, length=4)
        rsi_slow = ta.rsi(close, length=20)

        i = -2
        
        high_offset_2 = 1.018
        high_offset = 1.054

        cond1 = (close.iloc[i] > sma_9.iloc[i]) and \
                (close.iloc[i] > (ma_sell.iloc[i] * high_offset_2)) and \
                (rsi.iloc[i] > 50) and \
                (volume.iloc[i] > 0) and \
                (rsi_fast.iloc[i] > rsi_slow.iloc[i])

        cond2 = (close.iloc[i] < hma_50.iloc[i]) and \
                (close.iloc[i] > (ma_sell.iloc[i] * high_offset)) and \
                (volume.iloc[i] > 0) and \
                (rsi_fast.iloc[i] > rsi_slow.iloc[i])

        if cond1 or cond2:
            return make_exit(float(df_1m['close'].iloc[-1]), "SMA_OFFSET_SELL")

        return no_exit()

