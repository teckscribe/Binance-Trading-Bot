"""Daily close-confirmed Donchian 55/20 breakout adapter."""
from modules.strategies.ema100_trend import EMA100Trend


class Donchian5520(EMA100Trend):
    STRATEGY_ID = "DONCHIAN_55_20"
    TARGET = "donchian_target"
