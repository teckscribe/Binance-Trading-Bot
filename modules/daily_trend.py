"""Causal daily features shared by the EMA100 and Donchian adapters."""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Rules:
    slow: int = 100
    channel: int = 55
    exit_channel: int = 20
    atr: int = 20
    stop_atr: float = 3.0


def features(d: pd.DataFrame, rules: Rules = Rules()) -> pd.DataFrame:
    """Build daily features from completed UTC daily OHLC bars."""
    if d is None or d.empty:
        return pd.DataFrame()
    frame = d.copy()
    frame.index = pd.to_datetime(frame.index, utc=True)
    frame = frame.sort_index()[["open", "high", "low", "close"]].astype(float)
    previous = frame.close.shift(1)
    tr = pd.concat([
        frame.high - frame.low,
        (frame.high - previous).abs(),
        (frame.low - previous).abs(),
    ], axis=1).max(axis=1)
    frame["atr"] = tr.rolling(rules.atr, min_periods=rules.atr).mean()
    frame["ema_slow"] = frame.close.ewm(
        span=rules.slow, adjust=False, min_periods=rules.slow
    ).mean()
    frame["ema_target"] = np.sign(frame.close - frame.ema_slow)
    upper = frame.high.shift(1).rolling(rules.channel, min_periods=rules.channel).max()
    lower = frame.low.shift(1).rolling(rules.channel, min_periods=rules.channel).min()
    exit_high = frame.high.shift(1).rolling(rules.exit_channel, min_periods=rules.exit_channel).max()
    exit_low = frame.low.shift(1).rolling(rules.exit_channel, min_periods=rules.exit_channel).min()
    state = 0
    donchian = []
    for ts, row in frame.iterrows():
        close = float(row.close)
        if state == 1 and pd.notna(exit_low.loc[ts]) and close < exit_low.loc[ts]:
            state = 0
        elif state == -1 and pd.notna(exit_high.loc[ts]) and close > exit_high.loc[ts]:
            state = 0
        if pd.notna(upper.loc[ts]) and close > upper.loc[ts]:
            state = 1
        elif pd.notna(lower.loc[ts]) and close < lower.loc[ts]:
            state = -1
        donchian.append(state)
    frame["donchian_target"] = donchian
    frame["ready"] = frame.ema_slow.notna() & frame.atr.notna()
    return frame
