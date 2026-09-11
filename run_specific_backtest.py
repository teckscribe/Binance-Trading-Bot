"""
run_specific_backtest.py
Quick sanity check: run the production strategies over the three majors only.

Faster than the full 20-coin sweep — use it after changing a strategy, before
committing to `python backtest_optimizer.py`.

Note its numbers are NOT comparable to the full run: it uses a fixed mock
regime per strategy and only three highly liquid symbols. LIQ, for example,
looks positive on BTC/ETH/SOL and clearly negative across the full watchlist.

Requires cached data — run fetch_binance_data.py first.
"""
from backtest_optimizer import (
    run_portfolio_backtest, STRATEGY_CLASSES, PRODUCTION_IDS, MOCK_REGIME,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"=== QUICK BACKTEST: {', '.join(SYMBOLS)} ===")
    print(f"=== {len(PRODUCTION_IDS)} production strategies: "
          f"{', '.join(PRODUCTION_IDS)} ===")
    print("=" * 60)

    # Driven by the factory, so this file never drifts from production.
    for i, sid in enumerate(PRODUCTION_IDS, 1):
        cls = STRATEGY_CLASSES.get(sid)
        if cls is None:
            print(f"\n--- {i}. {sid}: no class registered, skipping ---")
            continue
        print(f"\n--- {i}. {sid} ---")
        run_portfolio_backtest(cls, SYMBOLS, MOCK_REGIME.get(sid, "RANGING"))
