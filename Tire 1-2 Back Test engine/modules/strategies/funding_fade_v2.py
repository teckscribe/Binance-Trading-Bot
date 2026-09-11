"""
strategies/funding_fade_v2.py
Implementation of Funding Rate Fade (V2).
Academic Concept: Fading extreme sentiment when the perpetual swap funding rate gets too high or too low.
Logic (Proxy): 
- Without historical funding rates, we use a Trend-Stretch proxy. 
- Funding rates explode when price moves parabolically in one direction. 
- When deviation from 200 EMA exceeds 4%, we fade the move.
"""

import pandas as pd
from modules.strategies.base_strategy import BaseStrategy

class FundingFadeV2(BaseStrategy):
    STRATEGY_ID = "FF_V2"

    def scan(self, symbol: str, df_1m: pd.DataFrame, df_15m: pd.DataFrame, df_1h: pd.DataFrame, regime: dict) -> dict:
        if df_15m is None or len(df_15m) < 200:
            return None
            
        c15 = df_15m['close'].astype(float)
        ema_200 = c15.ewm(span=200, adjust=False).mean()
        
        current_price = float(c15.iloc[-1])
        current_ema = float(ema_200.iloc[-1])
        
        if current_ema == 0:
            return None
            
        deviation = (current_price - current_ema) / current_ema
        
        # Entry Logic: Deviated > 4% from 200 EMA
        direction = None
        if deviation > 0.04:
            # Overextended up -> Short
            direction = "SHORT"
        elif deviation < -0.04:
            # Overextended down -> Long
            direction = "LONG"
                
        if direction:
            # Calculate ATR for SL/TP
            tr1 = df_15m['high'] - df_15m['low']
            tr2 = (df_15m['high'] - df_15m['close'].shift()).abs()
            tr3 = (df_15m['low'] - df_15m['close'].shift()).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = float(tr.rolling(14).mean().iloc[-1])
            
            if direction == "LONG":
                sl = current_price - (atr * 1.5)
                tp = current_price + (atr * 3.0)
            else:
                sl = current_price + (atr * 1.5)
                tp = current_price - (atr * 3.0)
                
            return {
                "symbol": symbol,
                "strategy": self.STRATEGY_ID,
                "direction": direction,
                "entry_price": current_price,
                "sl_price": sl,
                "tp_price": tp,
                "atr": atr,
                "qty": 0.0,
                "reason": f"FF_V2 (Stretch: {deviation*100:.2f}%)"
            }
        return None

    def manage(self, position: dict, df_1m: pd.DataFrame, session_pnl: float) -> dict:
        df = df_1m
        if df is None or df.empty:
            return {"exit": False, "exit_price": 0.0, "exit_reason": ""}
            
        current_price = float(df['close'].iloc[-1])
        entry_price = float(position['entry_price'])
        direction = position['direction']
        
        # Breakeven/trailing evaluate on COMPLETED bars so intra-bar noise
        # cannot arm them; hard SL/TP below still use current_price. Returns
        # None on 15m input (the backtest), falling back to current_price.
        trail_px = self.trail_reference_price(df, position)
        if trail_px is None:
            trail_px = current_price

        # --- Advanced Trailing Stop Logic ---
        if direction == "LONG":
            breakeven_price = self.breakeven_trigger(position, 0.006)
            if trail_px > breakeven_price and not position.get("be_hit", False):
                position["be_hit"] = True
                position["sl_price"] = max(position["sl_price"], entry_price * 1.001)

            if position.get("be_hit", False):
                trail_price = trail_px - (1.5 * position.get("atr", trail_px * 0.01))
                if trail_price > position["sl_price"]:
                    position["sl_price"] = trail_price
                    
            if current_price >= position['tp_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": "TP_HIT"}
            elif current_price <= position['sl_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": "SL_HIT"}
        else:
            breakeven_price = self.breakeven_trigger(position, 0.006)
            if trail_px < breakeven_price and not position.get("be_hit", False):
                position["be_hit"] = True
                position["sl_price"] = min(position["sl_price"], entry_price * 0.999)

            if position.get("be_hit", False):
                trail_price = trail_px + (1.5 * position.get("atr", trail_px * 0.01))
                if trail_price < position["sl_price"]:
                    position["sl_price"] = trail_price
                    
            if current_price <= position['tp_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": "TP_HIT"}
            elif current_price >= position['sl_price']:
                return {"exit": True, "exit_price": current_price, "exit_reason": "SL_HIT"}
                
        return {"exit": False, "exit_price": 0.0, "exit_reason": ""}

