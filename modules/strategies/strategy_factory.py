"""
strategies/strategy_factory.py
Instantiate and return the production strategy objects.

Production set (3), listed in scan-priority order:
    CSM        - Cross-Sectional Momentum  (4-5x ATR(1h) 24h move, long + short)
    NASOS_V4   - NASOS V4 Dip-Buy         (EWO + RSI dip-buy on 5m)
    ELLIOT_V8  - Elliot V8 Dip-Buy        (EWO dip-buy with trailing stop)

DELETED 2026-08-20: SMA_OFFSET.
  Negative in every configuration on the corrected harness with 0.03%/side
  slippage: -0.072%/trade ungated, -0.109%/trade with MIN_STRENGTH=0.50.
  Total P&L -21.5% over 90d / 100 symbols. Source file removed.

DELETED 2026-08-19: EI3_V2, VRP, LIQ.
  Tire 2 backtest (73 volatile coins, 1m intrabar SL/TP) showed all three
  have negative expectancy: EI3_V2 -0.136%, VRP -0.498%, LIQ -0.059%.
  Source files removed and all wiring cleaned.

DELETED 2026-08-11: OIB, WKD. Source files removed.

Files still present but NOT in production:
  trend_pullback.py   - removed 2026-08-04. Kept for backtest_optimizer.
  funding_fade_v2.py  - removed 2026-08-04. Same reason.
  grid_strategy.py    - see GRID note below.

GRID (Neutral Grid Trading) is a special case: it is NOT in _ALL_STRATEGIES so
it can never open a position, but live_scanner.py still calls get_grid() to
dissolve any pre-existing grid positions on a regime change.
"""

from modules.strategies.base_strategy               import BaseStrategy
from modules.strategies.cross_sectional_momentum    import CrossSectionalMomentum
from modules.strategies.freqtrade_port_nasos        import NASOSv4Port
from modules.strategies.freqtrade_port_elliot       import ElliotV8Port

# Retained only so live_scanner can dissolve legacy grid positions on a regime
# change. GRID is not in _ALL_STRATEGIES and cannot open new positions.
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
        


