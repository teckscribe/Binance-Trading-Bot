"""Focused offline checks for the CSB daily strategy adapters."""
import unittest
from unittest.mock import patch

import pandas as pd

from modules.daily_trend import features
from modules.strategies.donchian_55_20 import Donchian5520
from modules.strategies.ema100_trend import EMA100Trend


def bars(n=140):
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    close = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    return pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
    }, index=idx)


class DailyTrendTests(unittest.TestCase):
    def test_features_have_warmup_and_targets(self):
        result = features(bars())
        self.assertTrue(result.ready.iloc[-1])
        self.assertIn("ema_target", result)
        self.assertIn("donchian_target", result)

    def test_factory_contains_only_csm_and_daily_rules(self):
        from modules.strategies.strategy_factory import StrategyFactory
        self.assertEqual(
            [s.STRATEGY_ID for s in StrategyFactory.get_all()],
            ["CSM", "EMA100_TREND", "DONCHIAN_55_20"],
        )

    def test_daily_adapters_are_disabled_by_default(self):
        from modules import settings_manager as cfg
        self.assertFalse(cfg.SPEC_BY_KEY["EMA100_TREND_ENABLED"]["default"])
        self.assertFalse(cfg.SPEC_BY_KEY["DONCHIAN_55_20_ENABLED"]["default"])
        self.assertEqual(Donchian5520().TARGET, "donchian_target")

    def test_regime_matrix_contains_only_production_ids(self):
        from modules.regime_engine import REGIME_STRATEGY_PERMISSIONS
        expected = {"CSM", "EMA100_TREND", "DONCHIAN_55_20"}
        for permissions in REGIME_STRATEGY_PERMISSIONS.values():
            self.assertTrue(set(permissions) <= expected)

    def test_daily_cache_drops_forming_candle(self):
        from modules.strategies import ema100_trend as trend
        raw = bars().reset_index(names="timestamp")
        raw = pd.concat([raw, raw.iloc[[-1]].assign(timestamp=pd.Timestamp("2026-05-21", tz="UTC"))])
        with patch.object(trend, "fetch_candles", return_value=raw):
            out = trend._daily("BTCUSDT", pd.Timestamp("2026-05-21T00:02Z"))
        self.assertLess(out.index[-1], pd.Timestamp("2026-05-21", tz="UTC"))


if __name__ == "__main__":
    unittest.main()
