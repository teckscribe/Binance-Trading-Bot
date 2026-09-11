"""
backtest_freqtrade_strategies.py
Standalone backtester for the 5 Freqtrade strategies in CSB_Top_5_Strategies/.

Re-implements strategy logic using pandas-ta (no TA-Lib/freqtrade dependency).
Fetches 5m OHLCV data from Binance Futures, generates buy/sell signals,
simulates trades, and reports profitability.

Usage:
    python backtest_freqtrade_strategies.py                     # all 5 strategies
    python backtest_freqtrade_strategies.py --strategy ichiV1   # one strategy
    python backtest_freqtrade_strategies.py --days 60           # 60 days of data
    python backtest_freqtrade_strategies.py --symbols BTCUSDT,ETHUSDT
"""

import os
import sys
import time
import argparse
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import reduce

import numpy as np
import pandas as pd
import pandas_ta as pta
import requests

warnings.filterwarnings("ignore", category=FutureWarning)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
DATA_DIR         = os.path.join("data", "freqtrade_bt")
INITIAL_CAPITAL  = 1000.0
TAKER_FEE        = 0.001       # 0.1% per side (spot-like simulation)
ROUND_TRIP_FEE   = TAKER_FEE * 2
DEFAULT_SYMBOLS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
DEFAULT_DAYS     = 30


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_klines(symbol, interval, days):
    cache = os.path.join(DATA_DIR, f"{symbol}_{interval}_{days}d.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["open_time"]).set_index("open_time")
        print(f"  Loaded {symbol} {interval} from cache ({len(df)} bars)")
        return df

    os.makedirs(DATA_DIR, exist_ok=True)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    rows, cur = [], start_ms
    print(f"  Fetching {symbol} {interval} from Binance...")
    while cur < end_ms:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval,
                        "startTime": cur, "endTime": end_ms, "limit": 1500},
                timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"  ! fetch error for {symbol}: {e}")
            break
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        if len(batch) < 1500:
            break
        time.sleep(0.3)

    if not rows:
        return None

    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df.to_csv(cache)
    print(f"  Cached {len(df)} bars -> {cache}")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# INDICATOR HELPERS (replacing TA-Lib / qtpylib / technical)
# ═══════════════════════════════════════════════════════════════════════════════

def EMA(series, period):
    return series.ewm(span=period, adjust=False).mean()

def SMA(series, period):
    return series.rolling(period).mean()

def RSI(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def HMA(series, period):
    half = int(period / 2)
    sqrt_p = int(np.sqrt(period))
    wma_half = series.rolling(half).mean()
    wma_full = series.rolling(period).mean()
    diff = 2 * wma_half - wma_full
    return diff.rolling(sqrt_p).mean()

def STOCHF(df, fastk_period=5, fastd_period=3):
    lowest_low = df['low'].rolling(fastk_period).min()
    highest_high = df['high'].rolling(fastk_period).max()
    fastk = 100 * (df['close'] - lowest_low) / (highest_high - lowest_low)
    fastd = fastk.rolling(fastd_period).mean()
    return fastk, fastd

def ADX(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx

def EWO(df, fast=50, slow=200):
    ema1 = EMA(df['close'], fast)
    ema2 = EMA(df['close'], slow)
    return (ema1 - ema2) / df['close'] * 100

def EWO_low(df, fast=50, slow=200):
    ema1 = EMA(df['close'], fast)
    ema2 = EMA(df['close'], slow)
    return (ema1 - ema2) / df['low'] * 100

def ZEMA(series, period):
    ema1 = EMA(series, period)
    ema2 = EMA(ema1, period)
    d = ema1 - ema2
    return ema1 + d

def crossed_above(s1, s2):
    return (s1 > s2) & (s1.shift(1) <= s2.shift(1))

def crossed_below(s1, s2):
    return (s1 < s2) & (s1.shift(1) >= s2.shift(1))

def heikinashi(df):
    ha = df.copy()
    ha['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
    ha['open'] = (df['open'].shift(1) + df['close'].shift(1)) / 2
    ha.iloc[0, ha.columns.get_loc('open')] = df['open'].iloc[0]
    ha['high'] = pd.concat([df['high'], ha['open'], ha['close']], axis=1).max(axis=1)
    ha['low'] = pd.concat([df['low'], ha['open'], ha['close']], axis=1).min(axis=1)
    return ha

def ichimoku(df, conversion=20, base=60, lagging=120, displacement=30):
    high, low = df['high'], df['low']
    tenkan = (high.rolling(conversion).max() + low.rolling(conversion).min()) / 2
    kijun = (high.rolling(base).max() + low.rolling(base).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(displacement)
    senkou_b = ((high.rolling(lagging).max() + low.rolling(lagging).min()) / 2).shift(displacement)
    return tenkan, kijun, senkou_a, senkou_b


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def run_EI3v2(df):
    """EI3v2_tag_cofi_green — EWO + RSI + Cofi buy conditions, trailing stop."""
    d = df.copy()

    # Params
    base_nb_candles_buy = 12
    base_nb_candles_sell = 22
    low_offset = 0.987
    high_offset = 1.014
    high_offset_2 = 1.01
    ewo_high = 3.001
    ewo_low = -10.289
    rsi_buy = 58
    lambo2_ema_14_factor = 0.981
    lambo2_rsi_4_limit = 44
    lambo2_rsi_14_limit = 39
    buy_ema_cofi = 0.98
    buy_fastk = 22
    buy_fastd = 20
    buy_adx = 20
    buy_ewo_high = 4.179

    # Indicators
    d['ma_buy'] = EMA(d['close'], base_nb_candles_buy)
    d['ma_sell'] = EMA(d['close'], base_nb_candles_sell)
    d['hma_50'] = HMA(d['close'], 50)
    d['EWO'] = EWO(d, 50, 200)
    d['rsi'] = RSI(d['close'], 14)
    d['rsi_fast'] = RSI(d['close'], 4)
    d['rsi_slow'] = RSI(d['close'], 20)
    d['ema_14'] = EMA(d['close'], 14)
    d['rsi_4'] = RSI(d['close'], 4)
    d['rsi_14'] = RSI(d['close'], 14)
    d['zema_30'] = ZEMA(d['close'], 30)
    d['zema_200'] = ZEMA(d['close'], 200)
    d['pump_strength'] = (d['zema_30'] - d['zema_200']) / d['zema_30']
    fastk, fastd_val = STOCHF(d, 5, 3)
    d['fastk'] = fastk
    d['fastd'] = fastd_val
    d['adx'] = ADX(d)
    d['ema_8'] = EMA(d['close'], 8)
    d['volume_mean_short'] = d['volume'].rolling(4).mean()
    d['volume_mean_long'] = d['volume'].shift(288).rolling(48).mean()
    d['volume_mean_base'] = d['volume'].shift(432).rolling(288).mean()
    d['pnd_volume_warn'] = np.where(
        (d['volume_mean_short'] / d['volume_mean_long'] > 5.0), -1, 0)

    # Buy conditions
    lambo2 = (
        (d['close'] < (d['ema_14'] * lambo2_ema_14_factor)) &
        (d['rsi_4'] < lambo2_rsi_4_limit) &
        (d['rsi_14'] < lambo2_rsi_14_limit)
    )
    buy1ewo = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] > ewo_high) &
        (d['rsi'] < rsi_buy) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )
    buy2ewo = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] < ewo_low) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )
    is_cofi = (
        (d['open'] < d['ema_8'] * buy_ema_cofi) &
        crossed_above(d['fastk'], d['fastd']) &
        (d['fastk'] < buy_fastk) &
        (d['fastd'] < buy_fastd) &
        (d['adx'] > buy_adx) &
        (d['EWO'] > buy_ewo_high)
    )

    d['buy'] = (lambo2 | buy1ewo | buy2ewo | is_cofi).astype(int)
    d.loc[d['pnd_volume_warn'] < 0, 'buy'] = 0

    # Sell conditions
    sell1 = (
        (d['close'] > d['hma_50']) &
        (d['close'] > (d['ma_sell'] * high_offset_2)) &
        (d['rsi'] > 50) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    sell2 = (
        (d['close'] < d['hma_50']) &
        (d['close'] > (d['ma_sell'] * high_offset)) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    d['sell'] = (sell1 | sell2).astype(int)

    return d[['close', 'buy', 'sell']], {
        'trailing_stop': True,
        'trailing_stop_positive': 0.001,
        'trailing_stop_positive_offset': 0.012,
        'stoploss': -0.99,
        'sell_profit_only': True,
        'sell_profit_offset': 0.01,
    }


def run_ElliotV8(df):
    """ElliotV8_original_ichiv2 — EWO dip-buy with trailing stop."""
    d = df.copy()

    base_nb_candles_buy = 12
    base_nb_candles_sell = 22
    low_offset = 0.987
    high_offset = 1.008
    high_offset_2 = 1.016
    ewo_high = 3.147
    ewo_low = -17.145
    rsi_buy = 57

    d['ma_buy'] = EMA(d['close'], base_nb_candles_buy)
    d['ma_sell'] = EMA(d['close'], base_nb_candles_sell)
    d['hma_50'] = HMA(d['close'], 50)
    d['EWO'] = EWO(d, 50, 200)
    d['rsi'] = RSI(d['close'], 14)
    d['rsi_fast'] = RSI(d['close'], 4)
    d['rsi_slow'] = RSI(d['close'], 20)

    buy1 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] > ewo_high) &
        (d['rsi'] < rsi_buy) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )
    buy2 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] < ewo_low) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )
    d['buy'] = (buy1 | buy2).astype(int)

    sell1 = (
        (d['close'] > d['hma_50']) &
        (d['close'] > (d['ma_sell'] * high_offset_2)) &
        (d['rsi'] > 50) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    sell2 = (
        (d['close'] < d['hma_50']) &
        (d['close'] > (d['ma_sell'] * high_offset)) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    d['sell'] = (sell1 | sell2).astype(int)

    return d[['close', 'buy', 'sell']], {
        'trailing_stop': True,
        'trailing_stop_positive': 0.001,
        'trailing_stop_positive_offset': 0.02,
        'stoploss': -0.20,
        'sell_profit_only': True,
        'sell_profit_offset': 0.01,
    }


def run_ichiV1(df):
    """ichiV1 — Ichimoku cloud + multi-TF trend fan magnitude."""
    d = df.copy()

    buy_trend_above_senkou_level = 1
    buy_trend_bullish_level = 6
    buy_fan_magnitude_shift_value = 3
    buy_min_fan_magnitude_gain = 1.002

    ha = heikinashi(d)
    d['open'] = ha['open']
    d['high'] = ha['high']
    d['low'] = ha['low']

    d['trend_close_5m'] = d['close']
    d['trend_close_15m'] = EMA(d['close'], 3)
    d['trend_close_30m'] = EMA(d['close'], 6)
    d['trend_close_1h'] = EMA(d['close'], 12)
    d['trend_close_2h'] = EMA(d['close'], 24)
    d['trend_close_4h'] = EMA(d['close'], 48)
    d['trend_close_6h'] = EMA(d['close'], 72)
    d['trend_close_8h'] = EMA(d['close'], 96)

    d['trend_open_5m'] = d['open']
    d['trend_open_15m'] = EMA(d['open'], 3)
    d['trend_open_30m'] = EMA(d['open'], 6)
    d['trend_open_1h'] = EMA(d['open'], 12)
    d['trend_open_2h'] = EMA(d['open'], 24)
    d['trend_open_4h'] = EMA(d['open'], 48)
    d['trend_open_6h'] = EMA(d['open'], 72)
    d['trend_open_8h'] = EMA(d['open'], 96)

    d['fan_magnitude'] = d['trend_close_1h'] / d['trend_close_8h']
    d['fan_magnitude_gain'] = d['fan_magnitude'] / d['fan_magnitude'].shift(1)

    tenkan, kijun, senkou_a, senkou_b = ichimoku(d, 20, 60, 120, 30)
    d['senkou_a'] = senkou_a
    d['senkou_b'] = senkou_b

    conditions = []

    # Above senkou
    if buy_trend_above_senkou_level >= 1:
        conditions.append(d['trend_close_5m'] > d['senkou_a'])
        conditions.append(d['trend_close_5m'] > d['senkou_b'])

    # Bullish trends
    levels = [
        ('5m', 1), ('15m', 2), ('30m', 3), ('1h', 4), ('2h', 5), ('4h', 6)
    ]
    for tf, lvl in levels:
        if buy_trend_bullish_level >= lvl:
            conditions.append(d[f'trend_close_{tf}'] > d[f'trend_open_{tf}'])

    conditions.append(d['fan_magnitude_gain'] >= buy_min_fan_magnitude_gain)
    conditions.append(d['fan_magnitude'] > 1)
    for x in range(buy_fan_magnitude_shift_value):
        conditions.append(d['fan_magnitude'].shift(x + 1) < d['fan_magnitude'])

    if conditions:
        d['buy'] = reduce(lambda x, y: x & y, conditions).astype(int)
    else:
        d['buy'] = 0

    # Sell: 5m trend crosses below 2h EMA
    d['sell'] = crossed_below(d['trend_close_5m'], d['trend_close_2h']).astype(int)

    return d[['close', 'buy', 'sell']], {
        'trailing_stop': False,
        'stoploss': -0.275,
        'roi': {"0": 0.059, "10": 0.037, "41": 0.012, "114": 0},
        'sell_profit_only': False,
    }


def run_NASOSv4(df):
    """NASOSv4 — EWO + RSI dip-buy with custom trailing stoploss."""
    d = df.copy()

    base_nb_candles_buy = 8
    base_nb_candles_sell = 16
    low_offset = 0.984
    low_offset_2 = 0.942
    high_offset = 1.084
    high_offset_2 = 1.401
    ewo_high = 2.403
    ewo_high_2 = -5.585
    ewo_low = -14.378
    rsi_buy = 72
    lookback_candles = 3
    profit_threshold = 1.008

    d['ma_buy'] = EMA(d['close'], base_nb_candles_buy)
    d['ma_sell'] = EMA(d['close'], base_nb_candles_sell)
    d['hma_50'] = HMA(d['close'], 50)
    d['ema_100'] = EMA(d['close'], 100)
    d['sma_9'] = SMA(d['close'], 9)
    d['EWO'] = EWO_low(d, 50, 200)
    d['rsi'] = RSI(d['close'], 14)
    d['rsi_fast'] = RSI(d['close'], 4)
    d['rsi_slow'] = RSI(d['close'], 20)

    # Don't buy if no profit opportunity
    dont_buy = d['close'].rolling(lookback_candles).max() < (d['close'] * profit_threshold)

    buy1 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] > ewo_high) &
        (d['rsi'] < rsi_buy) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )
    buy2 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset_2)) &
        (d['EWO'] > ewo_high_2) &
        (d['rsi'] < rsi_buy) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset)) &
        (d['rsi'] < 25)
    )
    buy3 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] < ewo_low) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )

    d['buy'] = (buy1 | buy2 | buy3).astype(int)
    d.loc[dont_buy, 'buy'] = 0

    sell1 = (
        (d['close'] > d['sma_9']) &
        (d['close'] > (d['ma_sell'] * high_offset_2)) &
        (d['rsi'] > 50) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    sell2 = (
        (d['close'] < d['hma_50']) &
        (d['close'] > (d['ma_sell'] * high_offset)) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    d['sell'] = (sell1 | sell2).astype(int)

    return d[['close', 'buy', 'sell']], {
        'trailing_stop': True,
        'trailing_stop_positive': 0.001,
        'trailing_stop_positive_offset': 0.016,
        'stoploss': -0.15,
        'sell_profit_only': False,
    }


def run_NASMAv3(df):
    """NotAnotherSMAOffsetStrategyHOv3 — EWO + RSI offset buy."""
    d = df.copy()

    base_nb_candles_buy = 8
    base_nb_candles_sell = 16
    low_offset = 0.986
    low_offset_2 = 0.944
    high_offset = 1.054
    high_offset_2 = 1.018
    ewo_high = 4.179
    ewo_high_2 = -2.609
    ewo_low = -16.917
    rsi_buy = 58

    d['ma_buy'] = EMA(d['close'], base_nb_candles_buy)
    d['ma_sell'] = EMA(d['close'], base_nb_candles_sell)
    d['hma_50'] = HMA(d['close'], 50)
    d['ema_100'] = EMA(d['close'], 100)
    d['sma_9'] = SMA(d['close'], 9)
    d['EWO'] = EWO_low(d, 50, 200)
    d['rsi'] = RSI(d['close'], 14)
    d['rsi_fast'] = RSI(d['close'], 4)
    d['rsi_slow'] = RSI(d['close'], 20)

    buy1 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] > ewo_high) &
        (d['rsi'] < rsi_buy) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )
    buy2 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset_2)) &
        (d['EWO'] > ewo_high_2) &
        (d['rsi'] < rsi_buy) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset)) &
        (d['rsi'] < 25)
    )
    buy3 = (
        (d['rsi_fast'] < 35) &
        (d['close'] < (d['ma_buy'] * low_offset)) &
        (d['EWO'] < ewo_low) &
        (d['volume'] > 0) &
        (d['close'] < (d['ma_sell'] * high_offset))
    )

    d['buy'] = (buy1 | buy2 | buy3).astype(int)

    sell1 = (
        (d['close'] > d['sma_9']) &
        (d['close'] > (d['ma_sell'] * high_offset_2)) &
        (d['rsi'] > 50) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    sell2 = (
        (d['close'] < d['hma_50']) &
        (d['close'] > (d['ma_sell'] * high_offset)) &
        (d['volume'] > 0) &
        (d['rsi_fast'] > d['rsi_slow'])
    )
    d['sell'] = (sell1 | sell2).astype(int)

    return d[['close', 'buy', 'sell']], {
        'trailing_stop': True,
        'trailing_stop_positive': 0.005,
        'trailing_stop_positive_offset': 0.025,
        'stoploss': -0.3,
        'sell_profit_only': False,
    }


STRATEGIES = {
    "EI3v2_tag_cofi_green":              run_EI3v2,
    "ElliotV8_original_ichiv2":          run_ElliotV8,
    "ichiV1":                            run_ichiV1,
    "NASOSv4":                           run_NASOSv4,
    "NotAnotherSMAOffsetStrategyHOv3":   run_NASMAv3,
}


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_trades(signals_df, config, symbol=""):
    """
    Walk through the signals DataFrame and simulate trades.
    Handles: stoploss, trailing stop, ROI-based exit, sell signals.
    Returns list of trade dicts.
    """
    df = signals_df.dropna(subset=['close']).copy()
    if df.empty:
        return []

    stoploss = config.get('stoploss', -0.99)
    trailing = config.get('trailing_stop', False)
    trail_pos = config.get('trailing_stop_positive', 0.001)
    trail_offset = config.get('trailing_stop_positive_offset', 0.01)
    sell_profit_only = config.get('sell_profit_only', False)
    sell_profit_offset = config.get('sell_profit_offset', 0.0)
    roi = config.get('roi', {})

    trades = []
    in_trade = False
    entry_price = 0
    entry_time = None
    highest_since_entry = 0
    current_stoploss = stoploss
    candles_in_trade = 0

    for i in range(len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        price = row['close']

        if in_trade:
            candles_in_trade += 1
            profit = (price - entry_price) / entry_price
            highest_since_entry = max(highest_since_entry, price)
            max_profit = (highest_since_entry - entry_price) / entry_price

            # Trailing stop logic
            if trailing and max_profit >= trail_offset:
                trail_sl = max_profit - trail_pos
                current_stoploss = max(current_stoploss, -1 + (1 + trail_sl))
                sl_price = entry_price * (1 + trail_sl)
                if price <= sl_price:
                    trades.append({
                        'symbol': symbol, 'entry_time': entry_time,
                        'exit_time': ts, 'entry_price': entry_price,
                        'exit_price': price, 'pnl_pct': profit,
                        'exit_reason': 'trailing_stop',
                        'candles': candles_in_trade,
                    })
                    in_trade = False
                    continue

            # Hard stoploss
            if profit <= stoploss:
                exit_price = entry_price * (1 + stoploss)
                trades.append({
                    'symbol': symbol, 'entry_time': entry_time,
                    'exit_time': ts, 'entry_price': entry_price,
                    'exit_price': exit_price, 'pnl_pct': stoploss,
                    'exit_reason': 'stoploss',
                    'candles': candles_in_trade,
                })
                in_trade = False
                continue

            # ROI exit
            for candle_str, roi_pct in sorted(roi.items(), key=lambda x: int(x[0])):
                if candles_in_trade >= int(candle_str) and profit >= roi_pct:
                    trades.append({
                        'symbol': symbol, 'entry_time': entry_time,
                        'exit_time': ts, 'entry_price': entry_price,
                        'exit_price': price, 'pnl_pct': profit,
                        'exit_reason': f'roi_{candle_str}',
                        'candles': candles_in_trade,
                    })
                    in_trade = False
                    break

            if not in_trade:
                continue

            # Sell signal
            if row.get('sell', 0) == 1:
                if sell_profit_only and profit < sell_profit_offset:
                    continue
                trades.append({
                    'symbol': symbol, 'entry_time': entry_time,
                    'exit_time': ts, 'entry_price': entry_price,
                    'exit_price': price, 'pnl_pct': profit,
                    'exit_reason': 'sell_signal',
                    'candles': candles_in_trade,
                })
                in_trade = False
                continue

            # Unclog: sell at loss after 4 days (EI3v2 specific)
            if candles_in_trade >= 4 * 288 and profit < -0.04:
                trades.append({
                    'symbol': symbol, 'entry_time': entry_time,
                    'exit_time': ts, 'entry_price': entry_price,
                    'exit_price': price, 'pnl_pct': profit,
                    'exit_reason': 'unclog',
                    'candles': candles_in_trade,
                })
                in_trade = False
                continue

        else:
            # Check for buy signal
            if row.get('buy', 0) == 1:
                in_trade = True
                entry_price = price
                entry_time = ts
                highest_since_entry = price
                current_stoploss = stoploss
                candles_in_trade = 0

    # Force close open trades
    if in_trade:
        price = df.iloc[-1]['close']
        profit = (price - entry_price) / entry_price
        trades.append({
            'symbol': symbol, 'entry_time': entry_time,
            'exit_time': df.index[-1], 'entry_price': entry_price,
            'exit_price': price, 'pnl_pct': profit,
            'exit_reason': 'end_of_data',
            'candles': candles_in_trade,
        })

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# METRICS & REPORTING
# ═══════════════════════════════════════════════════════════════════════════════

def compute_metrics(trades, capital=INITIAL_CAPITAL):
    if not trades:
        return None

    pnls = np.array([t['pnl_pct'] - ROUND_TRIP_FEE for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    # Max consecutive losses
    streak = mx = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        mx = max(mx, streak)

    # Portfolio equity curve
    equity = [capital]
    for p in pnls:
        equity.append(equity[-1] * (1 + p))
    equity = np.array(equity)
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak
    max_dd = drawdown.max() * 100

    final_capital = equity[-1]
    total_return = (final_capital - capital) / capital * 100

    hold_hours = [(t['exit_time'] - t['entry_time']).total_seconds() / 3600
                  for t in trades]

    return {
        'n': len(pnls),
        'win_rate': len(wins) / len(pnls) * 100 if len(pnls) else 0,
        'avg_win': wins.mean() * 100 if len(wins) else 0,
        'avg_loss': losses.mean() * 100 if len(losses) else 0,
        'rr': abs(wins.mean() / losses.mean()) if len(wins) and len(losses) and losses.mean() != 0 else 0,
        'expectancy': pnls.mean() * 100,
        'profit_factor': wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float('inf'),
        'total_return': total_return,
        'max_drawdown': max_dd,
        'final_capital': final_capital,
        'worst_trade': pnls.min() * 100,
        'best_trade': pnls.max() * 100,
        'max_losing_streak': mx,
        'median_hold_hours': np.median(hold_hours) if hold_hours else 0,
        'sum_pnl': pnls.sum() * 100,
    }


def print_strategy_report(name, metrics, trades, capital=INITIAL_CAPITAL):
    if metrics is None:
        print(f"\n{'='*70}")
        print(f"  {name}")
        print(f"{'='*70}")
        print("  No trades generated.\n")
        return

    m = metrics
    edge = "YES" if m['expectancy'] > 0 else "NO"
    verdict = "PROFITABLE" if m['total_return'] > 0 else "UNPROFITABLE"

    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"{'='*70}")
    print(f"  Trades            : {m['n']}")
    print(f"  Win Rate          : {m['win_rate']:.1f}%")
    print(f"  Avg Win           : {m['avg_win']:+.2f}%")
    print(f"  Avg Loss          : {m['avg_loss']:+.2f}%")
    print(f"  Risk:Reward       : {m['rr']:.2f}")
    print(f"  Expectancy/trade  : {m['expectancy']:+.3f}%  (net of {ROUND_TRIP_FEE*100:.2f}% fees)")
    print(f"  Profit Factor     : {m['profit_factor']:.2f}")
    print(f"  Sum of Returns    : {m['sum_pnl']:+.1f}%")
    print(f"  Best Trade        : {m['best_trade']:+.2f}%")
    print(f"  Worst Trade       : {m['worst_trade']:+.2f}%")
    print(f"  Max Losing Streak : {m['max_losing_streak']}")
    print(f"  Median Hold       : {m['median_hold_hours']:.1f}h")
    print(f"  ---")
    print(f"  Initial Capital   : ${capital:.2f}")
    print(f"  Final Capital     : ${m['final_capital']:.2f}")
    print(f"  Total Return      : {m['total_return']:+.2f}%")
    print(f"  Max Drawdown      : {m['max_drawdown']:.1f}%")
    print(f"  ---")
    print(f"  EDGE              : {edge}")
    print(f"  VERDICT           : {verdict}")

    # Per-symbol breakdown
    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t['symbol']].append(t)
    if len(by_sym) > 1:
        print(f"\n  Per-Symbol Breakdown:")
        print(f"  {'SYMBOL':<12}{'TRADES':>8}{'WIN%':>8}{'E[net]':>10}{'SUM':>10}")
        for sym in sorted(by_sym):
            sm = compute_metrics(by_sym[sym])
            if sm:
                print(f"  {sym:<12}{sm['n']:>8}{sm['win_rate']:>7.1f}%"
                      f"{sm['expectancy']:>9.3f}%{sm['sum_pnl']:>9.1f}%")

    # Exit reasons
    from collections import Counter
    exits = Counter(t['exit_reason'] for t in trades)
    print(f"\n  Exit Reasons:")
    for reason, count in exits.most_common():
        print(f"    {reason:<20} {count:>5}  ({count/len(trades)*100:.1f}%)")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Backtest Freqtrade strategies")
    ap.add_argument("--strategy", default=None,
                    help="Run only this strategy (by class name)")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated symbols (default: BTC,ETH,SOL,BNB,XRP)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="Days of historical data (default: 30)")
    ap.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    args = ap.parse_args()

    capital = args.capital

    symbols = [s.strip().upper() for s in args.symbols.split(",")] \
        if args.symbols else DEFAULT_SYMBOLS

    strategies = STRATEGIES
    if args.strategy:
        matches = {k: v for k, v in STRATEGIES.items()
                   if args.strategy.lower() in k.lower()}
        if not matches:
            sys.exit(f"Unknown strategy '{args.strategy}'. "
                     f"Available: {list(STRATEGIES.keys())}")
        strategies = matches

    print("=" * 70)
    print(f"FREQTRADE STRATEGY BACKTEST — {args.days} DAYS")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Capital: ${capital:.2f} | Fee: {ROUND_TRIP_FEE*100:.2f}% round-trip")
    print("=" * 70)

    # Fetch data
    print("\nFetching 5m OHLCV data...")
    data = {}
    for sym in symbols:
        df = fetch_klines(sym, "5m", args.days)
        if df is not None and len(df) > 500:
            data[sym] = df
        else:
            print(f"  ! {sym}: insufficient data, skipping")

    if not data:
        sys.exit("No data available. Check your internet connection.")

    # Run each strategy
    all_results = {}
    for name, func in strategies.items():
        print(f"\n{'─'*70}")
        print(f"Running {name}...")

        all_trades = []
        for sym, df in data.items():
            try:
                signals, config = func(df)
                trades = simulate_trades(signals, config, symbol=sym)
                all_trades.extend(trades)
                print(f"  {sym}: {len(trades)} trades")
            except Exception as e:
                print(f"  {sym}: ERROR — {e}")

        metrics = compute_metrics(all_trades, capital)
        print_strategy_report(name, metrics, all_trades, capital)
        all_results[name] = {'metrics': metrics, 'trades': all_trades}

    # Summary comparison
    if len(all_results) > 1:
        print(f"\n\n{'='*90}")
        print("COMPARISON SUMMARY")
        print(f"{'='*90}")
        print(f"{'STRATEGY':<38}{'TRADES':>7}{'WIN%':>7}{'E[net]':>9}"
              f"{'PF':>7}{'RETURN':>9}{'MAXDD':>8}{'VERDICT':>12}")
        print(f"{'─'*90}")

        for name in strategies:
            r = all_results[name]
            m = r['metrics']
            if m is None:
                print(f"{name:<38}{'—':>7}{'—':>7}{'—':>9}{'—':>7}{'—':>9}{'—':>8}{'NO TRADES':>12}")
                continue
            v = "PROFIT" if m['total_return'] > 0 else "LOSS"
            print(f"{name:<38}{m['n']:>7}{m['win_rate']:>6.1f}%"
                  f"{m['expectancy']:>8.3f}%{m['profit_factor']:>7.2f}"
                  f"{m['total_return']:>8.1f}%{m['max_drawdown']:>7.1f}%"
                  f"{'  ' + v:>12}")
        print(f"{'='*90}")


if __name__ == "__main__":
    main()
