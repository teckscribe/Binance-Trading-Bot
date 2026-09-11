"""
Port of Freqtrade strategy: ichiV1.py
Adapted for BaseStrategy architecture using 5m resampled candles.
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
from .base_strategy import BaseStrategy, make_exit, no_exit

class IchiV1Port(BaseStrategy):
    STRATEGY_ID = "ICHI_V1"
    REQUIRES_1H = False

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
        
        if len(df_5m) < 150:
            return None

        close = df_5m['close']

        # Calculate Ichimoku (9, 26, 52)
        ichi = ta.ichimoku(df_5m['high'], df_5m['low'], df_5m['close'], tenkan=20, kijun=60, senkou=120)
        if ichi is None or len(ichi) < 2:
            return None
        
        ichi_df = ichi[0]  # First element is the DataFrame containing span A and B
        
        # In pandas_ta, Senkou A and B are usually named ISA_9_26_52 and ISB_9_26_52 or similar based on length.
        # We'll just grab by column index to be safe if pandas_ta version differs:
        # ISA is index 0, ISB is index 1, ITS is 2, IKS is 3...
        cols = ichi_df.columns
        if len(cols) < 2:
            return None
            
        senkou_a = ichi_df[cols[0]]
        senkou_b = ichi_df[cols[1]]
        tenkan_sen = ichi_df[cols[2]] if len(cols) > 2 else None
        kijun_sen = ichi_df[cols[3]] if len(cols) > 3 else None

        # Fan magnitude (EMA variations)
        ema1 = ta.ema(close, length=9)
        ema2 = ta.ema(close, length=50)

        i = -2

        if pd.isna(senkou_a.iloc[i]) or pd.isna(senkou_b.iloc[i]) or pd.isna(ema1.iloc[i]):
            return None

        cloud_top = max(senkou_a.iloc[i], senkou_b.iloc[i])
        
        # Basic Ichi buy condition (Trend above cloud + fan)
        cond_ichi = (close.iloc[i] > cloud_top) and \
                    (ema1.iloc[i] > ema2.iloc[i]) and \
                    (close.iloc[i] > close.iloc[i-1])

        if cond_ichi:
            entry_price = float(df_1m['close'].iloc[-1])
            sl_price = entry_price * (1 - 0.275) # as per original stoploss
            tp_price = entry_price * (1 + 0.059) # minimum ROI
            atr = float(ta.atr(df_1m['high'], df_1m['low'], df_1m['close'], length=14).iloc[-1])

            return {
                'symbol': symbol,
                'strategy': self.STRATEGY_ID,
                'direction': 'LONG',
                'entry_price': entry_price,
                'sl_price': sl_price,
                'tp_price': tp_price,
                'atr': atr,
                'strength': 1.0,
                'reason': 'IchiV1_Entry',
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
        trend_close_2h = ta.ema(close, length=24) # approx 2h close using 5m candles

        i = -2
        
        # Original sell_trend_indicator: "trend_close_2h" crosses something or condition
        # We'll just exit if close dips below trend_close_2h
        if close.iloc[i] < trend_close_2h.iloc[i] and close.iloc[i-1] >= trend_close_2h.iloc[i-1]:
            return make_exit(float(df_1m['close'].iloc[-1]), "ICHI_SELL")

        return no_exit()
