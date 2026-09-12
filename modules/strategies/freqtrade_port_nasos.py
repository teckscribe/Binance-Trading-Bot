"""
Port of Freqtrade strategy: NASOSv4.py
Adapted for BaseStrategy architecture using 5m resampled candles.
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
import math
from modules import settings_manager as cfg
from .base_strategy import BaseStrategy, make_exit, no_exit

# Tunables (PORT_MAX_HOLD_MIN, NASOS_SL_MODE, NASOS_SL_ATR, NASOS_SL_FLAT,
# NASOS_TP_ATR) are read from settings_manager at call time so an edit from
# the dashboard or a bot applies on the next scan without a restart.
#
# PORT_MAX_HOLD_MIN — time-based exit for the freqtrade ports. 0 = OFF, which
# is the historical behaviour: these strategies had NO time exit of any kind,
# so a flat position could hold a slot indefinitely (observed live at 10 hours
# and ~0.0%). CSM has had one since 2026-08-16; the ports never did, an
# asymmetry nobody chose. Off by default so enabling it is a measured
# decision, not a silent change.

# ── Stop construction (NASOS_SL_MODE) ───────────────────────────────────────
# "flat" = the ported 8%. "atr" = 6 x ATR(1m).
#
# REVERTED to "flat" 2026-08-30. The 6xATR default was set on a 90d/100-symbol
# sweep where it measured PF 1.49 vs 1.30 and looked robust (positive in both
# train/test halves, better under truncation, confirmed against this class).
#
# An independent Tier-2 run on 40d/130-symbol data over an OVERLAPPING period
# contradicted it outright:
#     flat 8%   PF 0.91   E -0.244%
#     6xATR     PF 0.54   E -1.289%
# and the gap is NOT explained by MAX_SL_PCT (loosening 0.08 -> 0.10 made
# 6xATR worse, not better) nor by the newly-listed symbols in that set (NASOS
# collapsed on the 100 ESTABLISHED symbols, PF 0.47).
#
# Two measurement paths disagree on the same strategy, symbols and period.
# Until that is understood, the ported original is the defensible default.
# NASOS_SL_MODE=atr re-enables 6xATR.

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

class NASOSv4Port(BaseStrategy):
    STRATEGY_ID = "NASOS_V4"
    REQUIRES_1H = True
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

        # Parameters
        base_nb_candles_buy = 8
        base_nb_candles_sell = 16
        low_offset = 0.984
        low_offset_2 = 0.942
        high_offset = 1.084
        
        ewo_high = 2.403
        ewo_high_2 = -5.585
        ewo_low = -14.378
        rsi_buy = 72
        profit_threshold = 1.008

        # Indicators
        ma_buy = ta.ema(close, length=base_nb_candles_buy)
        ma_sell = ta.ema(close, length=base_nb_candles_sell)
        ewo = EWO(df_5m, 50, 200)
        rsi = ta.rsi(close, length=14)
        rsi_fast = ta.rsi(close, length=4)

        i = -2

        if df_1h is not None and not df_1h.empty and len(df_1h) >= 3:
            close_1h_max = df_1h['close'].iloc[-4:-1].max() # lookback 3
            if close_1h_max < (close.iloc[i] * profit_threshold):
                return None
        
        cond_ewo1 = (rsi_fast.iloc[i] < 35) and \
                    (close.iloc[i] < (ma_buy.iloc[i] * low_offset)) and \
                    (ewo.iloc[i] > ewo_high) and \
                    (rsi.iloc[i] < rsi_buy) and \
                    (volume.iloc[i] > 0) and \
                    (close.iloc[i] < (ma_sell.iloc[i] * high_offset))

        cond_ewo2 = (rsi_fast.iloc[i] < 35) and \
                    (close.iloc[i] < (ma_buy.iloc[i] * low_offset_2)) and \
                    (ewo.iloc[i] > ewo_high_2) and \
                    (rsi.iloc[i] < rsi_buy) and \
                    (volume.iloc[i] > 0) and \
                    (close.iloc[i] < (ma_sell.iloc[i] * high_offset)) and \
                    (rsi.iloc[i] < 25)

        cond_ewolow = (rsi_fast.iloc[i] < 35) and \
                      (close.iloc[i] < (ma_buy.iloc[i] * low_offset)) and \
                      (ewo.iloc[i] < ewo_low) and \
                      (volume.iloc[i] > 0) and \
                      (close.iloc[i] < (ma_sell.iloc[i] * high_offset))

        if cond_ewo1 or cond_ewo2 or cond_ewolow:
            entry_price = float(df_1m['close'].iloc[-1])
            atr = float(ta.atr(df_1m['high'], df_1m['low'], df_1m['close'], length=14).iloc[-1])

            # ── Stop construction ────────────────────────────────────────────
            # Set by NASOS_SL_MODE (see the top of this file). Currently "flat"
            # (the ported 8%). The measurements for both options, and why the
            # 6xATR default was reverted, are recorded there -- they are
            # contested, so read them before changing this.
            if cfg.get("NASOS_SL_MODE") == "atr" and atr > 0:
                sl_price = entry_price - cfg.get("NASOS_SL_ATR") * atr
            else:
                sl_price = entry_price * (1 - cfg.get("NASOS_SL_FLAT"))
            tp_price = entry_price + atr * cfg.get("NASOS_TP_ATR")

            # Signal strength = how deep the 5m fast-RSI washout is. Entry
            # already requires rsi_fast < 35, so this maps [35 -> 0] onto
            # [0.0 -> 1.0]. live_scanner drops anything below MIN_STRENGTH
            # (0.50, i.e. rsi_fast < 17.5) and ranks the rest.
            #
            # WITHOUT THIS GATE NASOS HAS NO EDGE. Measured 90d / 100 symbols,
            # fixed harness, 0.03%/side slippage, both runs from
            # run_freqtrade_backtest:
            #
            #   ungated   875 trades  E=-0.001%  PF 1.00  total   -1.2%
            #   gated     421 trades  E=+0.350%  PF 1.16  total +147.2%
            #
            # The split is clean: kept trades average +0.350%, rejected ones
            # -0.327%. Ungated the harness reports "Edge: NO".
            #
            # HISTORY, so this is not reverted a third time: an earlier run
            # reported 798 trades at +0.488% ungated, which made the gate look
            # like it cost 242pp, and the formula was removed on that basis.
            # That figure did not reproduce — re-running the same harness gave
            # 875 / -0.001%, matching an independent replay exactly. The 798 was
            # stale output from superseded code. Do not restore a "gate hurts
            # NASOS" claim without re-measuring first.
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
                'reason': 'NASOS_V4_Entry',
                'leverage': 1
            }

        return None

    def manage(self, position: dict, df_1m: pd.DataFrame, session_pnl: float) -> dict:
        if df_1m is None or len(df_1m) < 150:
            return no_exit()

        # Max hold: free the slot when a trade has gone nowhere. Checked BEFORE
        # the indicator work below, so it costs nothing when it fires.
        max_hold = cfg.get("PORT_MAX_HOLD_MIN")
        if max_hold > 0:
            if self.hold_minutes(position, df_1m) >= max_hold:
                return make_exit(float(df_1m["close"].iloc[-1]), "MAX_HOLD")

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
        ma_sell_16 = ta.ema(close, length=16)
        rsi = ta.rsi(close, length=14)
        rsi_fast = ta.rsi(close, length=4)
        rsi_slow = ta.rsi(close, length=20)

        i = -2
        
        high_offset_2 = 1.401
        high_offset = 1.084

        cond1 = (close.iloc[i] > sma_9.iloc[i]) and \
                (close.iloc[i] > (ma_sell_16.iloc[i] * high_offset_2)) and \
                (rsi.iloc[i] > 50) and \
                (volume.iloc[i] > 0) and \
                (rsi_fast.iloc[i] > rsi_slow.iloc[i])

        cond2 = (close.iloc[i] < hma_50.iloc[i]) and \
                (close.iloc[i] > (ma_sell_16.iloc[i] * high_offset)) and \
                (volume.iloc[i] > 0) and \
                (rsi_fast.iloc[i] > rsi_slow.iloc[i])

        if cond1 or cond2:
            return make_exit(float(df_1m['close'].iloc[-1]), "NASOS_SELL_SIGNAL")

        return no_exit()


