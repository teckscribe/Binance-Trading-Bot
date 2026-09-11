"""
strategies/strategy_factory.py  (Tire 1-2 Back Test engine copy)
Instantiate and return the production strategy objects.

Production set (3): CSM, NASOS_V4, ELLIOT_V8.
"""

from modules.strategies.base_strategy               import BaseStrategy
from modules.strategies.cross_sectional_momentum    import CrossSectionalMomentum
from modules.strategies.freqtrade_port_nasos        import NASOSv4Port
from modules.strategies.freqtrade_port_elliot       import ElliotV8Port

try:
    from modules.strategies.grid_strategy import GridStrategy
    _GRID_INSTANCE = GridStrategy()
except ImportError:
    _GRID_INSTANCE = None

_ALL_STRATEGIES = [
    CrossSectionalMomentum(),
    NASOSv4Port(),
    ElliotV8Port(),
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
    def get_grid():
        return _GRID_INSTANCE

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
