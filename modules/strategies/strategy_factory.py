"""
strategies/strategy_factory.py
Instantiate and return the production strategy objects.

Production set (3), listed in scan-priority order:
    EMA100_TREND - Daily EMA100 trend flip (LONG-only, 3x Daily ATR trail)
    TSMOM_4H     - 4H Time-Series Momentum on 22 Majors (72h >= 5%, 24h hold)
    NASOS_V4     - 5m Fast-RSI / EWO Washout Dip-Buy (BULL_TREND + BEAR_TREND)

DELETED / RETIRED:
    2026-11-24: CSM, REBALANCING_PREMIUM, DONCHIAN_55_20, FUNDING_SHORT.
      Retired after 180d + 90d 1m + 1,284-trade live/paper audit kept only
      the top 3 highest-expectancy strategies.
    2026-09-14: TP, FF_V2, GRID, ICHI_V1.
    2026-08-20: SMA_OFFSET.
    2026-08-19: EI3_V2, VRP, LIQ.
    2026-08-11: OIB, WKD.
"""

from modules.strategies.base_strategy            import BaseStrategy
from modules.strategies.ema100_trend             import EMA100Trend
from modules.strategies.tsmom_4h                 import TSMOM4HStrategy
from modules.strategies.freqtrade_port_nasos     import NASOSv4Port

_ALL_STRATEGIES = [
    EMA100Trend(),
    TSMOM4HStrategy(),
    NASOSv4Port(),
]

_STRATEGY_MAP = {s.STRATEGY_ID: s for s in _ALL_STRATEGIES}


class StrategyFactory:

    @staticmethod
    def get_all() -> list[BaseStrategy]:
        return _ALL_STRATEGIES

    @staticmethod
    def get(strategy_id: str) -> BaseStrategy | None:
        return _STRATEGY_MAP.get(strategy_id)


    @staticmethod
    def get_permitted(regime: dict, regime_permissions: dict) -> list[BaseStrategy]:
        regime_name = regime.get("regime", "RANGING")
        perms       = regime_permissions.get(regime_name, {})

        try:
            from modules.strategy_overrides import is_disabled
            return [
                s for s in _ALL_STRATEGIES
                if perms.get(s.STRATEGY_ID, False)
                and not is_disabled(s.STRATEGY_ID)
            ]
        except Exception:
            pass
        return [s for s in _ALL_STRATEGIES if perms.get(s.STRATEGY_ID, False)]
        


