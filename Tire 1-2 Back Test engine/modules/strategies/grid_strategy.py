"""
strategies/grid_strategy.py
Neutral Grid Trading Strategy for RANGING regime.

Thesis:
  In ranging markets, price oscillates between support and resistance.
  A grid places buy orders below and sell orders above the current price
  at fixed intervals. Each fill targets the next grid level as TP.
  Profits from the chop that kills all directional strategies.

Grid structure (centered on price at grid creation):
  BUY levels  : center × (1 - step × N) for N = 1..GRID_LEVELS
  SELL levels : center × (1 + step × N) for N = 1..GRID_LEVELS
  TP per level: one step in the profitable direction
  SL per level: 2 steps in the adverse direction (1.4%)

Futures grid (different from spot):
  - BUY level  → open LONG position, TP = level + one step
  - SELL level → open SHORT position, TP = level - one step
  - Each position is independent — one per grid level

Key rules:
  - RANGING regime only. Grid dissolves on regime shift.
  - One grid per symbol. Bot runs grids on top GRID_SYMBOLS symbols.
  - Max GRID_LEVELS × 2 simultaneous positions per symbol.
  - Grid recenters if price moves beyond all levels (prevents runaway loss).
  - Risk per level = GRID_RISK_PCT (half of normal 1% directional risk).

Integration:
  scan() checks if last 1m bar crossed any unoccupied grid level.
  Returns one signal per call (strongest uncrossed level only).
  manage() checks all open grid positions for TP/SL hits.
  Regime change → all grid positions dissolved via _dissolve().
"""

import logging
from datetime import datetime, timezone

import pandas as pd

from modules.strategies.base_strategy import (
    BaseStrategy, no_exit, make_exit
)

log = logging.getLogger("StratGRID")

# ─── Constants ────────────────────────────────────────────────────────────────
GRID_LEVELS      = 5         # levels each side of center (5 buy + 5 sell = 10 total)
GRID_STEP_PCT    = 0.007     # 2026-04-20: tightened 1.0% → 0.7% because grid
                             # took zero trades — 1.0% steps were too wide for the
                             # observed RANGING oscillation amplitude. 0.7% sits
                             # between the 0.5% (too tight, boundary hits) and 1.0%
                             # (no fills) extremes. Smaller TP per level but more
                             # fills. Revisit after ≥ 20 grid trades.
GRID_RISK_PCT    = 0.005     # 0.5% equity risk per level (unchanged)
GRID_SYMBOLS     = 3         # max symbols to run grids on simultaneously
RECENTER_THRESH  = GRID_LEVELS + 1
PER_LEVEL_SL_STEPS = 3       # SL = 3 steps (2.1%) from entry
PER_LEVEL_TP_STEPS = 2       # 2026-06-09: TP widened 1→2 steps (1.4%) to match SL.
                             # Old TP=0.7% vs SL=1.4% needed 67% WR to break even.
                             # R:R was 0.50 — one loss erased two wins.
                             # New R:R=1.0 → need >50% WR. Actual WR=71% → profitable.

# Minimum RANGING regime age before grid activates.
# Live data showed BULL→RANGING→BULL in 46 seconds — grid opened 3 losing trades.
# Only activate grid if regime has been RANGING for at least this many minutes.
MIN_REGIME_AGE_MIN = 3       # 2026-04-20: lowered 5 → 3 min. 5 min caused grid to
                             # never activate through regime flickers. 3 min still
                             # filters the 46-sec flicker incident but lets genuine
                             # RANGING periods mature faster.




class GridLevel:
    """One level in the grid — a potential or active trade."""

    def __init__(
        self,
        price:     float,
        direction: str,    # 'LONG' (buy level) or 'SHORT' (sell level)
        tp_price:  float,
        sl_price:  float,
        level_id:  int,
    ):
        self.price     = price
        self.direction = direction
        self.tp_price  = tp_price
        self.sl_price  = sl_price
        self.level_id  = level_id
        self.triggered = False   # True once a position is opened at this level
        self.active    = False   # True while position is open


class SymbolGrid:
    """
    Full grid for one symbol. Created when regime = RANGING.
    Dissolved when regime changes.
    """

    def __init__(self, symbol: str, center_price: float):
        self.symbol       = symbol
        self.center_price = center_price
        self.created_at   = datetime.now(timezone.utc)
        self.levels: list[GridLevel] = []
        self._build(center_price)

    def _build(self, center: float) -> None:
        """Create all grid levels around center price with per-level SL."""
        self.levels = []

        for i in range(1, GRID_LEVELS + 1):
            # BUY levels below center — TP & SL both 2 steps (R:R=1.0)
            price    = center * (1 - GRID_STEP_PCT * i)
            tp_price = price * (1 + GRID_STEP_PCT * PER_LEVEL_TP_STEPS)
            sl_price = price * (1 - GRID_STEP_PCT * PER_LEVEL_SL_STEPS)
            self.levels.append(GridLevel(price, "LONG",  tp_price, sl_price, -i))

            # SELL levels above center — TP & SL both 2 steps (R:R=1.0)
            price    = center * (1 + GRID_STEP_PCT * i)
            tp_price = price * (1 - GRID_STEP_PCT * PER_LEVEL_TP_STEPS)
            sl_price = price * (1 + GRID_STEP_PCT * PER_LEVEL_SL_STEPS)
            self.levels.append(GridLevel(price, "SHORT", tp_price, sl_price, +i))

        log.info(
            f"[GRID] {self.symbol} grid built | Center: {center:.4f} | "
            f"Levels: {GRID_LEVELS}×2 | Step: {GRID_STEP_PCT*100:.1f}% | "
            f"Range: ±{GRID_STEP_PCT*GRID_LEVELS*100:.1f}%"
        )

    def recenter(self, new_price: float) -> None:
        """Reset grid around new price. Called when price escapes all levels."""
        log.info(
            f"[GRID] {self.symbol} recentering: {self.center_price:.4f} → {new_price:.4f}"
        )
        self.center_price = new_price
        self._build(new_price)

    def needs_recenter(self, current_price: float) -> bool:
        """True if price has moved beyond all grid levels."""
        upper = self.center_price * (1 + GRID_STEP_PCT * GRID_LEVELS)
        lower = self.center_price * (1 - GRID_STEP_PCT * GRID_LEVELS)
        return current_price > upper * 1.002 or current_price < lower * 0.998

    def get_crossed_level(self, bar_low: float, bar_high: float) -> GridLevel | None:
        """
        Find the closest untriggered level crossed by the current 1m bar.
        Returns at most one level per call.
        """
        candidates = []
        for lvl in self.levels:
            if lvl.triggered:
                continue
            if lvl.direction == "LONG" and bar_low <= lvl.price:
                candidates.append(lvl)
            elif lvl.direction == "SHORT" and bar_high >= lvl.price:
                candidates.append(lvl)

        if not candidates:
            return None

        # Take the level closest to current mid-price
        mid = (bar_low + bar_high) / 2
        return min(candidates, key=lambda l: abs(l.price - mid))


class GridStrategy(BaseStrategy):
    """
    Neutral grid strategy. Maintains one SymbolGrid per active symbol.
    All grids dissolve on regime change.
    """

    STRATEGY_ID = "GRID"

    def __init__(self):
        self._grids: dict[str, SymbolGrid] = {}
        self._regime_since: datetime | None = None   # when RANGING started

    # ─── Regime change dissolution ────────────────────────────────────────────

    def dissolve_all(self) -> list[str]:
        symbols = list(self._grids.keys())
        self._grids.clear()
        self._regime_since = None   # reset — next RANGING must wait MIN_REGIME_AGE
        if symbols:
            log.info(f"[GRID] All grids dissolved on regime change: {symbols}")
        return symbols

    def dissolve_symbol(self, symbol: str) -> None:
        if symbol in self._grids:
            del self._grids[symbol]
            log.info(f"[GRID] Grid dissolved for {symbol}")

    # ─── Regime age tracker ───────────────────────────────────────────────────

    def _regime_age_ok(self, regime_name: str) -> bool:
        """
        Returns True only if RANGING regime has been active for at least
        MIN_REGIME_AGE_MIN minutes. Prevents opening grids on transient
        regime flickers (e.g. BULL→RANGING→BULL in 46 seconds — live data).
        """
        if regime_name != "RANGING":
            self._regime_since = None
            return False
        now = datetime.now(timezone.utc)
        if self._regime_since is None:
            self._regime_since = now
            log.info("[GRID] RANGING regime started — waiting minimum age before activating")
            return False
        age_min = (now - self._regime_since).total_seconds() / 60
        if age_min < MIN_REGIME_AGE_MIN:
            log.debug(f"[GRID] Regime age {age_min:.1f}m < {MIN_REGIME_AGE_MIN}m minimum — waiting")
            return False
        return True

    # ─── scan ─────────────────────────────────────────────────────────────────

    def scan(
        self,
        symbol:  str,
        df_1m:   pd.DataFrame,
        df_15m:  pd.DataFrame,
        df_1h:   pd.DataFrame,
        regime:  dict,
    ) -> dict | None:
        """
        Check if the latest 1m bar crossed any grid level.
        Creates a new grid for the symbol if none exists.
        Returns one signal dict or None.
        """
        if regime.get("regime") != "RANGING":
            # Dissolve this symbol's grid if regime changed
            if symbol in self._grids:
                self.dissolve_symbol(symbol)
            return None

        # Minimum regime age gate — prevents transient flickers from opening grids
        if not self._regime_age_ok("RANGING"):
            return None

        if df_1m is None or df_1m.empty:
            return None

        # ── Per-Symbol Trend Filter (ADX) ────────────────────────────────────
        # Prevent running Grid on coins that are individually in a strong trend
        if df_1h is not None and not df_1h.empty:
            try:
                from modules.regime_engine import _compute_adx
                adx_1h = _compute_adx(df_1h)
                if adx_1h > 25.0:
                    log.debug(f"[GRID] {symbol} ADX={adx_1h:.1f} > 25.0 — strongly trending, skipping Grid")
                    if symbol in self._grids:
                        self.dissolve_symbol(symbol)
                    return None
            except Exception as e:
                log.debug(f"[GRID] Failed to compute ADX for {symbol}: {e}")

        latest   = df_1m.iloc[-1]
        c_high   = float(latest["high"])
        c_low    = float(latest["low"])
        c_close  = float(latest["close"])

        # ── Create grid if not exists ─────────────────────────────────────────
        if symbol not in self._grids:
            # Only create grids for top GRID_SYMBOLS active grids
            if len(self._grids) >= GRID_SYMBOLS:
                return None   # Already running max grids
            self._grids[symbol] = SymbolGrid(symbol, c_close)
            return None   # Don't trade on same bar as grid creation

        grid = self._grids[symbol]

        # ── Recenter if price escaped all levels ──────────────────────────────
        if grid.needs_recenter(c_close):
            grid.recenter(c_close)
            return None   # Don't trade on recenter bar

        # ── Check for crossed level ───────────────────────────────────────────
        lvl = grid.get_crossed_level(c_low, c_high)
        if lvl is None:
            return None

        # Mark level triggered — won't re-fire until position closes
        lvl.triggered = True
        lvl.active    = True

        strength = 1.0 - (abs(lvl.level_id) / (GRID_LEVELS + 1))  # closer levels = stronger

        # Per-level SL: 2 steps (1.4%) from entry. No cap needed —
        # 1.4% × lev 3 = 4.2% leveraged, well under 10% cap.
        effective_sl = lvl.sl_price

        # ── ATR context for ML features ──────────────────────────────────────
        atr = 0.0
        if df_1m is not None and len(df_1m) >= 15:
            h  = df_1m["high"].astype(float)
            lw = df_1m["low"].astype(float)
            cl = df_1m["close"].astype(float)
            tr = pd.concat(
                [h - lw, (h - cl.shift(1)).abs(), (lw - cl.shift(1)).abs()],
                axis=1,
            ).max(axis=1)
            atr = float(tr.rolling(14).mean().iloc[-1])

        # ── ATR% volatility gate ─────────────────────────────────────────
        if atr > 0 and lvl.price > 0:
            atr_pct = atr / lvl.price
            if atr_pct > 0.010:  # 1.0% max — GRID has tight 0.7% steps
                log.debug(f"[GRID] {symbol} ATR%={atr_pct*100:.2f}% > 1.0% — too volatile")
                return None

        vol_ratio = 1.0
        if df_1m is not None and len(df_1m) >= 21:
            v = df_1m["volume"].astype(float)
            v_avg = float(v.rolling(20).mean().iloc[-2])
            if v_avg > 0:
                vol_ratio = float(v.iloc[-1]) / v_avg

        log.info(
            f"[GRID] {symbol} {lvl.direction} | Level: {lvl.price:.4f} | "
            f"TP: {lvl.tp_price:.4f} | SL(decl): {effective_sl:.4f} | "
            f"Grid level: {lvl.level_id:+d}"
        )

        return {
            "symbol":      symbol,
            "strategy":    self.STRATEGY_ID,
            "direction":   lvl.direction,
            "entry_price": lvl.price,
            "sl_price":    effective_sl,
            "tp_price":    lvl.tp_price,
            "atr":         atr,
            "strength":    strength,
            "reason":      (
                f"Grid level {lvl.level_id:+d} | "
                f"{lvl.direction} @ {lvl.price:.4f} | "
                f"Step {GRID_STEP_PCT*100:.1f}%"
            ),
            "ml_features": {
                "adx":         0.0,                 # GRID is regime-agnostic in ranging
                "vol_ratio":   round(vol_ratio, 3),
                "atr_pct":     round(atr / lvl.price, 6) if lvl.price else 0.0,
                "sl_dist_pct": round(abs(lvl.price - effective_sl) / lvl.price, 6),
                "donchian_pos": round((abs(lvl.level_id) / (GRID_LEVELS + 1)), 4),
            },
            "_grid_level": lvl,   # internal ref for manage() to reset on close
        }

    # ─── manage ───────────────────────────────────────────────────────────────

    def manage(
        self,
        position:    dict,
        df_1m:       pd.DataFrame,
        session_pnl: float,
    ) -> dict:
        """
        Grid position exit: fixed TP and SL only. No trailing.
        Grid profits from the step — let it close cleanly.
        """
        if df_1m is None or df_1m.empty:
            return no_exit()

        entry     = position["entry_price"]
        direction = position["direction"]
        tp        = position.get("tp_price")
        sl        = position.get("sl_price")

        latest = df_1m.iloc[-1]
        c_high = float(latest["high"])
        c_low  = float(latest["low"])

        # NOTE: Hard stop removed (2026-05-29). Grid boundary SL is the
        # correct exit for grid strategies. Hard stop at 2.5% was firing
        # before boundary, causing -7.5% leveraged losses that wiped out
        # multiple TP wins. Let boundary SL + recenter handle risk.

        # ── TP hit ────────────────────────────────────────────────────────────
        if tp is not None:
            if direction == "LONG" and c_high >= tp:
                self._reset_level(position)
                return make_exit(tp, "GRID_TP")
            if direction == "SHORT" and c_low <= tp:
                self._reset_level(position)
                return make_exit(tp, "GRID_TP")

        # ── SL hit (per-level, 2 steps from entry) ─────────────────────────
        if sl is not None:
            if direction == "LONG" and c_low <= sl:
                self._reset_level(position)
                return make_exit(sl, "GRID_SL")
            if direction == "SHORT" and c_high >= sl:
                self._reset_level(position)
                return make_exit(sl, "GRID_SL")

        return no_exit()

    def _reset_level(self, position: dict) -> None:
        """Reset a grid level so it can fire again."""
        lvl = position.get("_grid_level")
        if lvl is not None:
            lvl.triggered = False
            lvl.active    = False






