"""
strategies/strategy_factory.py
Instantiate and return the production strategy objects.

Production set (2), listed in scan-priority order:
    CSM        - Cross-Sectional Momentum  (4-5x ATR(1h) 24h move, long + short)
    NASOS_V4   - NASOS V4 Dip-Buy         (EWO + RSI dip-buy on 5m)

DELETED 2026-08-20: SMA_OFFSET.
  Negative in every configuration on the corrected harness with 0.03%/side
  slippage: -0.072%/trade ungated, -0.109%/trade with MIN_STRENGTH=0.50.
  Total P&L -21.5% over 90d / 100 symbols. Source file removed.

DELETED 2026-08-19: EI3_V2, VRP, LIQ.
  Tire 2 backtest (73 volatile coins, 1m intrabar SL/TP) showed all three
  have negative expectancy: EI3_V2 -0.136%, VRP -0.498%, LIQ -0.059%.
  Source files removed and all wiring cleaned.

DELETED 2026-08-11: OIB, WKD. Source files removed.

DELETED 2026-09-14: TP (trend_pullback), FF_V2 (funding_fade_v2), GRID,
  ICHI_V1. All four had been out of production since August and were kept
  only for backtest_optimizer / the legacy grid-dissolve path; no GRID
  position has existed for weeks, so the scaffolding went with them.
"""

from modules.strategies.base_strategy               import BaseStrategy
from modules.strategies.cross_sectional_momentum    import CrossSectionalMomentum
from modules.strategies.freqtrade_port_nasos        import NASOSv4Port
from modules.strategies.tsmom_4h                  import TSMOM4HStrategy
from modules.strategies.rebalancing_premium        import RebalancingPremiumStrategy

_ALL_STRATEGIES = [
    CrossSectionalMomentum(),
    NASOSv4Port(),
    TSMOM4HStrategy(),
    RebalancingPremiumStrategy(),
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
        


