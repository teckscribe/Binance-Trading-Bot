"""
modules/ml_engine.py  v2
Machine Learning Engine - 4-phase adaptive system.

Combines best practices from CSB original + ML_CODES (Bitget bot):
  - JSONL append-only storage (crash-safe - partial writes are skipped)
  - 27 dimensionless multi-timeframe features
  - Phase-gated progressive unlock (50/100/200 trades)
  - Reduce-risk-don't-block for Phase 2 (safer for live trading)
  - RF/XGBoost + isotonic calibration for Phase 4
  - Shadow mode for signal gate (safe rollout)
  - Milestone tracking + Telegram integration

Phases:
  Phase 1 (ML_PHASE >= 1, always):
    Rich feature logger - 27 features per signal at entry, outcome on
    close.  Appends to data/ml/signals.jsonl + outcomes.jsonl.
    Zero impact on trading.

  Phase 2 (ML_PHASE >= 2, requires 50+ completed trades):
    Adaptive position sizing - buckets historical trades by ADX x vol_ratio
    quartiles, computes win rate per bucket.  Returns risk multiplier
    (0.3-1.0).  Weak setups get smaller positions.  Trade count unchanged.

  Phase 3 (ML_PHASE >= 3, requires 100+ completed trades):
    Dynamic SL/Trail - finds optimal SL and trail multipliers per
    volatility bucket from historical winners.  Returns override values.

  Phase 4 (ML_PHASE >= 4, requires 200+ completed trades):
    Signal quality gate - RandomForest (or XGBoost if installed) predicts
    P(win).  Low-confidence signals are blocked (or logged in shadow mode).
    Shadow mode (ML_SHADOW=true) is the default for safe rollout.

Config (via .env):
  ML_PHASE  = 1|2|3|4  (default: 1)
  ML_SHADOW = true|false (default: true - Phase 4 logs but doesn't block)
"""

import os
import json
import uuid
import time as _time
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("MLEngine")

# -"-"-"- Paths -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
_ROOT          = Path(__file__).resolve().parent.parent
_ML_DIR        = _ROOT / "data" / "ml"
_SIGNALS_PATH  = _ML_DIR / "signals.jsonl"
_OUTCOMES_PATH = _ML_DIR / "outcomes.jsonl"
_MODEL_PATH    = _ML_DIR / "model.pkl"
_MILESTONES_PATH = _ML_DIR / "milestones_notified.json"

# -"-"-"- Phase gating -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
ML_PHASE  = int(os.getenv("ML_PHASE", "1"))
ML_SHADOW = os.getenv("ML_SHADOW", "true").lower() == "true"

MIN_TRADES_PHASE2 = 50
MIN_TRADES_PHASE3 = 100
MIN_TRADES_PHASE4 = 200

CONFIDENCE_THRESHOLD = 0.55   # Phase 4: below this -> SKIP (unless shadow)

# -"-"-"- Feature names (ordered - model training column order) -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
FEATURE_NAMES = [
    # Strategy-computed (from signal.ml_features)
    "adx", "vol_ratio", "atr_pct", "sl_dist_pct", "strength",
    "rsi", "dist_from_ema_pct", "macd_hist_norm", "donchian_pos",
    # Market context
    "regime_bull", "regime_bear", "funding_rate",
    "regime_age_min", "hour_utc", "day_of_week", "direction_sign",
    # 1m OHLCV-derived
    "ret_1bar", "ret_5bar", "ret_15bar",
    "rsi_1m", "vwap_dist", "vol_spike", "body_ratio", "atr_expanding",
    # 15m OHLCV-derived
    "ret_15m", "trend_15m",
    # Cross-timeframe
    "mtf_alignment",
]


# -
# HELPERS
# -

def _safe_div(a, b, default=0.0):
    """a / b, returning default on zero / NaN / inf."""
    if b == 0 or pd.isna(b) or pd.isna(a):
        return default
    v = a / b
    return float(v) if np.isfinite(v) else default


def _clip(v, lo=-5.0, hi=5.0):
    """Clip to bounds; NaN / inf -> 0."""
    if pd.isna(v) or not np.isfinite(v):
        return 0.0
    return float(np.clip(v, lo, hi))


def _append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line.  Crash-safe: a partial write produces a broken
    JSON line that _load_jsonl silently skips on read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    """Load all records from JSONL, silently skipping broken lines."""
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# Validity key is (mtime, size); CSB_NO_FILE_CACHE=true bypasses the cache.
_outcomes_count_cache = {"key": None, "count": 0}
_NO_FILE_CACHE = os.getenv("CSB_NO_FILE_CACHE", "false").strip().lower() in ("1", "true", "yes", "on")

def _completed_count() -> int:
    """Outcome count with (mtime,size)-based caching - avoids re-scanning the
    entire JSONL on every signal evaluation (2-4 calls per signal at
    ML_PHASE>=4). The scan cost grows with the file, so caching matters as
    outcomes.jsonl fills."""
    if not _OUTCOMES_PATH.exists():
        return 0
    try:
        st = _OUTCOMES_PATH.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        return 0
    if not _NO_FILE_CACHE and _outcomes_count_cache["key"] == key:
        return _outcomes_count_cache["count"]
    count = 0
    with open(_OUTCOMES_PATH) as f:
        for line in f:
            if line.strip():
                count += 1
    _outcomes_count_cache["key"] = key
    _outcomes_count_cache["count"] = count
    return count


# -"-"-"- Data cache (avoids re-reading JSONL every signal) -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
_data_cache: list[dict] | None = None
_data_cache_ts: float = 0.0
_CACHE_TTL = 300   # 5 min


def _load_labelled(force: bool = False) -> list[dict]:
    """Merge signals + outcomes into labelled records.  Cached for 5 min."""
    global _data_cache, _data_cache_ts

    now = _time.monotonic()
    if not force and _data_cache is not None and (now - _data_cache_ts) < _CACHE_TTL:
        return _data_cache

    sig_map  = {r["signal_id"]: r for r in _load_jsonl(_SIGNALS_PATH)}
    outcomes = _load_jsonl(_OUTCOMES_PATH)

    labelled = []
    for out in outcomes:
        sid = out.get("signal_id")
        sig = sig_map.get(sid)
        if not sig:
            continue
        rec = {**sig}
        rec["pnl_pct"]        = float(out.get("pnl_pct", 0))
        rec["pnl_equity_pct"] = float(out.get("pnl_equity_pct", 0))
        rec["duration_min"]   = float(out.get("duration_min", 0))
        rec["exit_reason"]    = out.get("exit_reason", "")
        rec["be_triggered"]   = out.get("be_triggered", False)
        labelled.append(rec)

    _data_cache    = labelled
    _data_cache_ts = now
    return labelled


# -
# FEATURE COMPUTATION  (~27 dimensionless features)
# -

def _compute_15m_indicators(df_15m: pd.DataFrame | None) -> dict:
    """ADX/RSI/ATR%/MACD-hist/Donchian/EMA-dist/vol-ratio from 15m OHLCV.

    These were historically expected in signal['ml_features'], but no strategy
    populates that dict, so all eight were logged as 0.0 for every trade. Compute
    them centrally here so every strategy gets a full feature vector.  No
    lookahead — uses only bars up to the last completed one.  Returns zeros when
    df is missing or too short.
    """
    keys = ["adx", "rsi", "atr_pct", "macd_hist_norm",
            "donchian_pos", "dist_from_ema_pct", "vol_ratio"]
    out = {k: 0.0 for k in keys}
    if df_15m is None or len(df_15m) < 28:
        return out
    try:
        c  = df_15m["close"].astype(float)
        h  = df_15m["high"].astype(float)
        lo = df_15m["low"].astype(float)
        v  = df_15m["volume"].astype(float) if "volume" in df_15m.columns \
             else pd.Series([0.0] * len(df_15m), index=c.index)

        # Wilder ATR(14) + ADX(14)
        prev_c = c.shift(1)
        tr  = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        up  = h.diff(); dn = -lo.diff()
        plus_dm  = ((up > dn) & (up > 0)) * up.clip(lower=0)
        minus_dm = ((dn > up) & (dn > 0)) * dn.clip(lower=0)
        atr_safe = atr.replace(0, np.nan)
        plus_di  = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_safe
        minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_safe
        dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = float(dx.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
        out["adx"]     = _clip(adx if np.isfinite(adx) else 0.0, 0, 100)
        out["atr_pct"] = _clip(_safe_div(float(atr.iloc[-1]), float(c.iloc[-1])) * 100, 0, 50)

        # RSI(14)
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi_v = float((100 - 100 / (1 + rs)).iloc[-1])
        out["rsi"] = _clip(rsi_v if np.isfinite(rsi_v) else 50.0, 0, 100)

        # MACD(12,26,9) histogram, normalised by price
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        hist = float((macd - macd.ewm(span=9, adjust=False).mean()).iloc[-1])
        out["macd_hist_norm"] = _clip(_safe_div(hist, float(c.iloc[-1])) * 100, -50, 50)

        # Donchian(20) position (0 = channel low, 1 = channel high)
        hh = float(h.rolling(20).max().iloc[-1]); ll = float(lo.rolling(20).min().iloc[-1])
        out["donchian_pos"] = _clip(_safe_div(float(c.iloc[-1]) - ll, hh - ll), 0, 1)

        # EMA(20) distance and volume ratio
        ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
        out["dist_from_ema_pct"] = _clip(_safe_div(float(c.iloc[-1]) - ema20, ema20) * 100, -50, 50)
        vavg = float(v.rolling(20).mean().iloc[-1])
        out["vol_ratio"] = _clip(_safe_div(float(v.iloc[-1]), vavg) if vavg > 0 else 0.0, 0, 10)
    except Exception:
        pass
    return out


def _compute_features(
    signal:         dict,
    regime:         dict,
    regime_age_min: float,
    df_1m:          pd.DataFrame | None = None,
    df_15m:         pd.DataFrame | None = None,
) -> dict:
    """
    Compute ~27 dimensionless market features from signal + OHLCV data.

    Features are scale-invariant across symbols (ratios, z-scores, bounded
    indicators).  No lookahead - only uses data up to the current bar.
    """
    feat  = signal.get("ml_features", {})
    now   = datetime.now(timezone.utc)
    entry = float(signal.get("entry_price", 0))
    if entry <= 0:
        return {}

    direction  = signal.get("direction", "LONG")
    regime_str = regime.get("regime", "RANGING")

    # -"-"- Strategy-timeframe indicators -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
    # Computed here from 15m OHLCV because strategies do not populate
    # signal['ml_features'] (all eight were 0.0 for every trade otherwise).  A
    # strategy MAY still override any of these by supplying ml_features.
    sf = _compute_15m_indicators(df_15m)
    sl_px       = float(signal.get("sl_price", 0) or 0)
    sl_dist_pct = _clip(abs(entry - sl_px) / entry * 100, 0, 50) if sl_px > 0 else 0.0

    features = {
        "adx":               float(feat.get("adx", sf["adx"])),
        "vol_ratio":         float(feat.get("vol_ratio", sf["vol_ratio"])),
        "atr_pct":           float(feat.get("atr_pct", sf["atr_pct"])),
        "sl_dist_pct":       float(feat.get("sl_dist_pct", sl_dist_pct)),
        "strength":          float(signal.get("strength", 0)),
        "rsi":               float(feat.get("rsi", sf["rsi"])),
        "dist_from_ema_pct": float(feat.get("dist_from_ema_pct", sf["dist_from_ema_pct"])),
        "macd_hist_norm":    float(feat.get("macd_hist_norm", sf["macd_hist_norm"])),
        "donchian_pos":      float(feat.get("donchian_pos", sf["donchian_pos"])),
    }

    # -"-"- Market context -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
    features.update({
        "regime_bull":    1.0 if regime_str == "BULL_TREND" else 0.0,
        "regime_bear":    1.0 if regime_str == "BEAR_TREND" else 0.0,
        "funding_rate":   float(regime.get("funding", 0)),
        "regime_age_min": round(regime_age_min, 1),
        "hour_utc":       now.hour,
        "day_of_week":    now.weekday(),
        "direction_sign": 1.0 if direction == "LONG" else -1.0,
    })

    # -"-"- 1m OHLCV-derived -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
    has_1m = df_1m is not None and len(df_1m) >= 30
    if has_1m:
        c  = df_1m["close"].astype(float)
        h  = df_1m["high"].astype(float)
        lo = df_1m["low"].astype(float)
        v  = df_1m["volume"].astype(float) if "volume" in df_1m.columns \
             else pd.Series([0.0] * len(df_1m))

        # Momentum returns
        features["ret_1bar"] = _clip(
            _safe_div(c.iloc[-1] - c.iloc[-2], c.iloc[-2]) * 100
        )
        features["ret_5bar"] = _clip(
            _safe_div(c.iloc[-1] - c.iloc[-6], c.iloc[-6]) * 100
        ) if len(c) >= 7 else 0.0
        features["ret_15bar"] = _clip(
            _safe_div(c.iloc[-1] - c.iloc[-16], c.iloc[-16]) * 100
        ) if len(c) >= 17 else 0.0

        # RSI 1m
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi_v = float((100 - (100 / (1 + rs))).iloc[-1])
        features["rsi_1m"] = _clip(
            rsi_v if np.isfinite(rsi_v) else 50.0, 0, 100
        )

        # VWAP distance (price vs 30-bar volume-weighted average)
        typical = (h + lo + c) / 3
        cum_vol = v.rolling(30).sum().iloc[-1]
        vwap    = float(
            (typical * v).rolling(30).sum().iloc[-1] / cum_vol
        ) if cum_vol > 0 else entry
        features["vwap_dist"] = _clip(
            _safe_div(entry - vwap, vwap) * 100
        )

        # Volume spike (current bar vs 20-bar average)
        vol_avg = float(v.rolling(20).mean().iloc[-2]) if len(v) >= 21 else 0.0
        features["vol_spike"] = _clip(
            _safe_div(float(v.iloc[-1]), vol_avg) if vol_avg > 0 else 1.0,
            0, 10,
        )

        # Body ratio (1 = marubozu, 0 = doji)
        if "open" in df_1m.columns:
            body = abs(
                float(c.iloc[-1]) - float(df_1m["open"].astype(float).iloc[-1])
            )
            rng = float(h.iloc[-1] - lo.iloc[-1])
            features["body_ratio"] = _clip(_safe_div(body, rng), 0, 1)
        else:
            features["body_ratio"] = 0.5

        # ATR expanding (current vs previous, normalised by rolling std)
        tr = pd.concat(
            [h - lo, (h - c.shift(1)).abs(), (lo - c.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        atr_14  = tr.rolling(14).mean()
        atr_std = float(tr.rolling(14).std().iloc[-1]) if len(tr) >= 15 else 0.0
        if atr_std > 0 and len(atr_14) >= 2:
            features["atr_expanding"] = _clip(_safe_div(
                float(atr_14.iloc[-1]) - float(atr_14.iloc[-2]), atr_std
            ))
        else:
            features["atr_expanding"] = 0.0
    else:
        for k in ["ret_1bar", "ret_5bar", "ret_15bar", "rsi_1m",
                   "vwap_dist", "vol_spike", "body_ratio", "atr_expanding"]:
            features[k] = 0.0

    # -"-"- 15m OHLCV-derived -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
    has_15m = df_15m is not None and len(df_15m) >= 20
    if has_15m:
        c15 = df_15m["close"].astype(float)
        features["ret_15m"] = _clip(
            _safe_div(c15.iloc[-1] - c15.iloc[-4], c15.iloc[-4]) * 100
        ) if len(c15) >= 5 else 0.0
        ema20 = float(c15.ewm(span=20, adjust=False).mean().iloc[-1])
        features["trend_15m"] = 1.0 if float(c15.iloc[-1]) > ema20 else -1.0
    else:
        features["ret_15m"]   = 0.0
        features["trend_15m"] = 0.0

    # -"-"- Multi-timeframe alignment -"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-"-
    ds = features["direction_sign"]
    al = 0
    if ds > 0:   # LONG
        al += 1 if features.get("ret_5bar", 0) > 0 else -1
        al += 1 if features.get("ret_15m", 0)  > 0 else -1
        al += 1 if features.get("trend_15m", 0) > 0 else -1
        al += 1 if features.get("regime_bull", 0) > 0 else -1
    else:        # SHORT
        al += 1 if features.get("ret_5bar", 0) < 0 else -1
        al += 1 if features.get("ret_15m", 0)  < 0 else -1
        al += 1 if features.get("trend_15m", 0) < 0 else -1
        al += 1 if features.get("regime_bear", 0) > 0 else -1
    features["mtf_alignment"] = al / 4.0

    return features


# -
# PHASE 1 - Signal & Outcome Logger  (append-only JSONL)
# -

def _log_signal_entry(
    signal_id: str,
    signal:    dict,
    size:      dict,
    regime:    dict,
    features:  dict,
) -> None:
    """Append one signal record to signals.jsonl."""
    record = {
        "signal_id":   signal_id,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "symbol":      signal.get("symbol", ""),
        "strategy":    signal.get("strategy", ""),
        "direction":   signal.get("direction", ""),
        "entry_price": signal.get("entry_price", 0),
        "sl_price":    signal.get("sl_price", 0),
        "tp_price":    signal.get("tp_price"),
        "leverage":    size.get("leverage", 5),
        "notional":    size.get("notional", 0),
        "regime":      regime.get("regime", ""),
        "features":    features,
    }
    try:
        _append_jsonl(_SIGNALS_PATH, record)
        log.info(
            f"[ML] Signal logged: {signal_id} "
            f"{signal.get('symbol')} {signal.get('strategy')}"
        )
    except Exception as exc:
        log.warning(f"[ML] Signal log failed: {exc}")


def log_outcome(position: dict) -> None:
    """Append trade outcome to outcomes.jsonl.  Called on position close."""
    signal_id = position.get("ml_signal_id")
    if not signal_id:
        return

    record = {
        "signal_id":      signal_id,
        "exit_time":      datetime.now(timezone.utc).isoformat(),
        "exit_price":     position.get("exit_price", 0),
        "pnl_pct":        position.get("pnl_pct", 0),
        "pnl_equity_pct": position.get("pnl_equity_pct", 0),
        "duration_min":   position.get("duration_min", 0),
        "exit_reason":    position.get("exit_reason", ""),
        "be_triggered":   position.get("be_hit", position.get("be_active", False)),
        "hwm":            round(float(position.get("hwm", 0.0) or 0.0), 6),
    }
    try:
        _append_jsonl(_OUTCOMES_PATH, record)
        # Invalidate data cache so next scoring uses fresh data
        global _data_cache
        _data_cache = None
        log.info(
            f"[ML] Outcome logged: {signal_id} -> "
            f"{position.get('exit_reason')} "
            f"pnl={position.get('pnl_pct', 0):.4f}"
        )
    except Exception as exc:
        log.warning(f"[ML] Outcome log failed: {exc}")


# -
# PHASE 2 - Adaptive Position Sizing
# -

def _score_signal(signal: dict, features: dict) -> float:
    """
    Return confidence multiplier (0.3-1.0) from historical feature buckets.

    Approach: bucket trades by ADX quartile x vol_ratio quartile (same
    strategy).  Compute win rate per bucket.  Map to multiplier:
      WR >= 60% -> 1.0 (full risk)
      WR == 20% -> 0.3 (minimum risk)
      Linear interpolation between.
    Falls back to strategy-wide stats if bucket has < 5 trades.
    """
    if ML_PHASE < 2:
        return 1.0
    if _completed_count() < MIN_TRADES_PHASE2:
        return 1.0

    try:
        labelled = _load_labelled()
        strategy = signal.get("strategy", "")
        strat_trades = [r for r in labelled if r.get("strategy") == strategy]
        if len(strat_trades) < 20:
            return 1.0

        adx_vals, vol_vals, pnl_vals = [], [], []
        for t in strat_trades:
            f = t.get("features", {})
            a = f.get("adx", 0)
            v = f.get("vol_ratio", 0)
            if a and v:
                adx_vals.append(float(a))
                vol_vals.append(float(v))
                pnl_vals.append(float(t.get("pnl_pct", 0)))

        if len(adx_vals) < 20:
            return 1.0

        adx_arr = np.array(adx_vals)
        vol_arr = np.array(vol_vals)
        pnl_arr = np.array(pnl_vals)

        adx_qs = np.percentile(adx_arr, [25, 50, 75])
        vol_qs = np.percentile(vol_arr, [25, 50, 75])

        cur_adx = features.get("adx", 0)
        cur_vol = features.get("vol_ratio", 0)
        adx_q = int(np.searchsorted(adx_qs, cur_adx))
        vol_q = int(np.searchsorted(vol_qs, cur_vol))

        bucket_mask = np.array([
            int(np.searchsorted(adx_qs, adx_arr[i])) == adx_q
            and int(np.searchsorted(vol_qs, vol_arr[i])) == vol_q
            for i in range(len(adx_arr))
        ])

        bucket_pnl = pnl_arr[bucket_mask]
        if len(bucket_pnl) < 5:
            bucket_pnl = pnl_arr

        wr = float((bucket_pnl > 0).mean())
        confidence = float(np.clip(0.3 + (wr - 0.2) * (0.7 / 0.4), 0.3, 1.0))

        log.info(
            f"[ML P2] {signal.get('symbol')} {strategy} | "
            f"ADX_Q={adx_q} VOL_Q={vol_q} | "
            f"WR={wr:.1%} ({len(bucket_pnl)} trades) | "
            f"risk_mult={confidence:.2f}"
        )
        return confidence

    except Exception as exc:
        log.warning(f"[ML P2] Scoring failed: {exc}")
        return 1.0


# -
# PHASE 3 - Dynamic SL / Trail
# -

def _get_optimal_params(signal: dict, features: dict) -> dict:
    """
    Return optimal SL and trail ATR multipliers for this signal's
    volatility bucket.

    Approach: bucket by atr_pct tercile (same strategy).  For each bucket:
      SL    = 75th percentile of SL distances among winners -> ATR mult.
      Trail = based on median winner duration (short -> tight trail).
    Returns empty dict if Phase 3 not active or insufficient data.
    """
    if ML_PHASE < 3:
        return {}
    if _completed_count() < MIN_TRADES_PHASE3:
        return {}

    try:
        labelled = _load_labelled()
        strategy = signal.get("strategy", "")
        strat_trades = [r for r in labelled if r.get("strategy") == strategy]

        atr_pcts, sl_dists, pnl_vals, dur_vals = [], [], [], []
        for t in strat_trades:
            f    = t.get("features", {})
            a    = f.get("atr_pct", 0)
            sl_d = f.get("sl_dist_pct", 0)
            if a and sl_d:
                atr_pcts.append(float(a))
                sl_dists.append(float(sl_d))
                pnl_vals.append(float(t.get("pnl_pct", 0)))
                dur_vals.append(float(t.get("duration_min", 0)))

        if len(atr_pcts) < 30:
            return {}

        atr_arr = np.array(atr_pcts)
        sl_arr  = np.array(sl_dists)
        pnl_arr = np.array(pnl_vals)
        dur_arr = np.array(dur_vals)

        cur_atr = features.get("atr_pct", 0)
        q33 = float(np.percentile(atr_arr, 33))
        q66 = float(np.percentile(atr_arr, 66))

        if cur_atr <= q33:
            mask = atr_arr <= q33
        elif cur_atr <= q66:
            mask = (atr_arr > q33) & (atr_arr <= q66)
        else:
            mask = atr_arr > q66

        if mask.sum() < 10:
            return {}

        bucket_pnl = pnl_arr[mask]
        bucket_sl  = sl_arr[mask]
        bucket_dur = dur_arr[mask]
        winners    = bucket_pnl > 0

        if winners.sum() < 3:
            return {}

        optimal_sl_pct = float(np.percentile(bucket_sl[winners], 75))
        if cur_atr <= 0:
            return {}
        optimal_sl_mult = float(np.clip(optimal_sl_pct / cur_atr, 1.5, 4.0))

        median_dur = float(np.median(bucket_dur[winners]))
        if median_dur < 15:
            trail_mult = 1.5
        elif median_dur < 30:
            trail_mult = 2.0
        else:
            trail_mult = 2.5

        log.info(
            f"[ML P3] {signal.get('symbol')} {strategy} | "
            f"SL={optimal_sl_mult:.2f}xATR | Trail={trail_mult}xATR | "
            f"{mask.sum()} trades in bucket"
        )
        return {
            "sl_atr_mult":    round(optimal_sl_mult, 2),
            "trail_atr_mult": trail_mult,
        }

    except Exception as exc:
        log.warning(f"[ML P3] Params failed: {exc}")
        return {}


# -
# PHASE 4 - Signal Quality Gate  (RF/XGBoost + Shadow Mode)
# -

_model: dict | None = None
_model_loaded_at: datetime | None = None
_MODEL_RETRAIN_HOURS = 6


def _train_model() -> bool:
    """Train a probability-calibrated classifier on all labelled data."""
    global _model, _model_loaded_at

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.calibration import CalibratedClassifierCV
        import pickle
    except ImportError:
        log.warning("[ML P4] scikit-learn required. pip install scikit-learn")
        return False

    labelled = _load_labelled(force=True)
    if len(labelled) < MIN_TRADES_PHASE4:
        return False
        
    global FEATURE_NAMES
    # Dynamically build FEATURE_NAMES to capture all new pandas-ta indicators
    all_keys = set()
    for rec in labelled:
        all_keys.update(rec.get("features", {}).keys())
    FEATURE_NAMES = sorted(list(all_keys))

    rows, labels = [], []
    for rec in labelled:
        feat = rec.get("features", {})
        row  = [float(feat.get(k, 0)) for k in FEATURE_NAMES]
        rows.append(row)
        labels.append(1 if float(rec.get("pnl_pct", 0)) > 0 else 0)

    X = np.nan_to_num(np.array(rows, dtype=float))
    y = np.array(labels, dtype=int)

    # Try XGBoost (better on small tabular data), fall back to RF
    try:
        import xgboost as xgb
        base = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=42, n_jobs=-1,
        )
        log.info("[ML P4] Using XGBClassifier")
    except ImportError:
        base = RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=5,
            class_weight="balanced", random_state=42, n_jobs=-1,
        )
        log.info("[ML P4] Using RandomForestClassifier")

    # Isotonic calibration -> reliable probability output
    model = CalibratedClassifierCV(base, cv=3, method="isotonic")
    model.fit(X, y)

    _model = {"model": model, "features": FEATURE_NAMES, "n_trained": len(X)}
    _model_loaded_at = datetime.now(timezone.utc)

    try:
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_PATH, "wb") as f:
            pickle.dump(_model, f)
        try:
            base_est = model.calibrated_classifiers_[0].estimator
            if hasattr(base_est, "feature_importances_"):
                imps = sorted(
                    zip(FEATURE_NAMES, base_est.feature_importances_),
                    key=lambda x: -x[1],
                )
                top5 = ", ".join(f"{n}={v:.3f}" for n, v in imps[:5])
                log.info(f"[ML P4] Top features: {top5}")
        except Exception:
            pass
        log.info(f"[ML P4] Model trained on {len(X)} trades, saved")
    except Exception as exc:
        log.warning(f"[ML P4] Model save failed: {exc}")

    return True


def _load_cached_model() -> bool:
    """Load model from disk if not already in memory."""
    global _model, _model_loaded_at

    if _model is not None:
        return True
    if not _MODEL_PATH.exists():
        return False
    try:
        import pickle
        with open(_MODEL_PATH, "rb") as f:
            _model = pickle.load(f)
        _model_loaded_at = datetime.now(timezone.utc)
        log.info("[ML P4] Model loaded from disk")
        return True
    except Exception as exc:
        log.warning(f"[ML P4] Model load failed: {exc}")
        return False


def _predict_and_gate(signal: dict, features: dict) -> dict:
    """
    Phase 4: predict P(win) and return gate decision.

    Shadow mode (ML_SHADOW=true, default):
      Scores all signals and logs decisions, but ALWAYS returns PASS.
      Use for the first 2-3 weeks to verify the model helps.

    Live mode (ML_SHADOW=false):
      Signals with P(win) < CONFIDENCE_THRESHOLD are SKIPPED.
    """
    result = {"win_prob": -1.0, "gate_action": "PASS", "gate_shadow": True}

    if ML_PHASE < 4:
        return result
    if _completed_count() < MIN_TRADES_PHASE4:
        return result

    need_retrain = (
        _model is None
        or _model_loaded_at is None
        or (datetime.now(timezone.utc) - _model_loaded_at).total_seconds()
           > _MODEL_RETRAIN_HOURS * 3600
    )
    if need_retrain:
        if not _train_model():
            if not _load_cached_model():
                return result

    try:
        row = np.array(
            [[float(features.get(k, 0)) for k in FEATURE_NAMES]],
            dtype=float,
        )
        row  = np.nan_to_num(row)
        prob = float(_model["model"].predict_proba(row)[0, 1])
    except Exception as exc:
        log.warning(f"[ML P4] Prediction failed: {exc}")
        return result

    passes    = prob >= CONFIDENCE_THRESHOLD
    action    = "PASS" if passes else "SKIP"
    effective = "PASS" if ML_SHADOW else action

    emoji = "\u2705" if passes else "\u274c"
    tag   = "[SHADOW]" if ML_SHADOW else "[GATE]"
    log.info(
        f"{tag} {emoji} {signal.get('strategy')} {signal.get('symbol')} "
        f"P(win)={prob:.3f} -> {effective}"
    )

    return {
        "win_prob":    round(prob, 4),
        "gate_action": effective,
        "gate_shadow": ML_SHADOW,
    }


# -
# STATUS & MILESTONES
# -

_milestones_notified: set[int] = set()

_MILESTONES = {
    MIN_TRADES_PHASE2: "Phase 2 (Adaptive Position Sizing)",
    MIN_TRADES_PHASE3: "Phase 3 (Dynamic SL/Trail)",
    MIN_TRADES_PHASE4: "Phase 4 (Signal Quality Gate)",
}


def _load_milestones_notified() -> None:
    """Load already-notified milestone thresholds from disk on startup."""
    global _milestones_notified
    try:
        if _MILESTONES_PATH.exists():
            data = json.loads(_MILESTONES_PATH.read_text())
            _milestones_notified = set(int(x) for x in data.get("notified", []))
            if _milestones_notified:
                log.info(
                    f"[ML] Loaded notified milestones from disk: "
                    f"{sorted(_milestones_notified)}"
                )
    except Exception as exc:
        log.warning(f"[ML] Milestone state load failed: {exc}")


def _save_milestones_notified() -> None:
    """Persist notified milestone thresholds to disk."""
    try:
        _MILESTONES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MILESTONES_PATH.write_text(
            json.dumps({"notified": sorted(_milestones_notified)}, indent=2)
        )
    except Exception as exc:
        log.warning(f"[ML] Milestone state save failed: {exc}")


# Load on module import so the set is populated before the first call
_load_milestones_notified()


def get_ml_status() -> dict:
    """ML engine status for daily report and Telegram bot."""
    n = _completed_count()
    phases_ready = []
    if n >= MIN_TRADES_PHASE2:
        phases_ready.append(2)
    if n >= MIN_TRADES_PHASE3:
        phases_ready.append(3)
    if n >= MIN_TRADES_PHASE4:
        phases_ready.append(4)

    next_milestone = 0
    next_name      = ""
    for threshold, name in sorted(_MILESTONES.items()):
        if n < threshold:
            next_milestone = threshold
            next_name      = name
            break

    return {
        "ml_phase":         ML_PHASE,
        "completed_trades": n,
        "next_milestone":   next_milestone,
        "next_phase_name":  next_name,
        "phases_ready":     phases_ready,
        "shadow_mode":      ML_SHADOW,
    }


def check_milestone_alert() -> str | None:
    """Check if a new milestone was just reached.  Called after every close."""
    n = _completed_count()
    for threshold, name in _MILESTONES.items():
        if n >= threshold and threshold not in _milestones_notified:
            _milestones_notified.add(threshold)
            _save_milestones_notified()   # persist so restart won't re-alert
            phase_num = {
                MIN_TRADES_PHASE2: 2,
                MIN_TRADES_PHASE3: 3,
                MIN_TRADES_PHASE4: 4,
            }[threshold]
            return (
                f"\U0001f916 ML Milestone reached!\n"
                f"\U0001f4ca {n} trades logged\n"
                f"\U0001f513 {name} is now ready\n\n"
                f"To enable: set ML_PHASE={phase_num} in .env and restart"
            )
    return None


# -
# PUBLIC API - single entry point for live_scanner
# -

def get_ml_adjustments(
    signal:         dict,
    size:           dict,
    regime:         dict,
    regime_age_min: float,
    df_1m:          pd.DataFrame | None = None,
    df_15m:         pd.DataFrame | None = None,
) -> dict:
    """
    Master function - returns all ML adjustments in one call.

    Called by live_scanner._execute_entries() BEFORE opening a position.
    The scanner uses these values to:
      - Scale risk via risk_mult (Phase 2)
      - Override SL/trail if Phase 3 active
      - Block signal if Phase 4 gate says SKIP (and not shadow)
      - Log features (Phase 1, always)

    Args:
        signal         : Signal dict from strategy.scan()
        size           : {"leverage": N} dict
        regime         : Regime dict from regime_engine
        regime_age_min : Minutes since last regime change
        df_1m          : 1m OHLCV DataFrame for this symbol (optional)
        df_15m         : 15m OHLCV DataFrame for this symbol (optional)

    Returns:
        {
            "signal_id":      str,
            "risk_mult":      float,        # Phase 2: 0.3-1.0
            "confidence":     float,
            "sl_atr_mult":    float | None, # Phase 3 override
            "trail_atr_mult": float | None, # Phase 3 override
            "win_prob":       float,        # Phase 4: P(win) or -1.0
            "gate_action":    str,          # Phase 4: "PASS" or "SKIP"
            "gate_shadow":    bool,         # Phase 4: shadow mode on?
        }
    """
    # Phase 1: Compute rich features (NOT logged here - caller must call
    # commit_ml_signal() after order actually opens, to avoid orphan rows).
    features  = _compute_features(signal, regime, regime_age_min, df_1m, df_15m)
    signal_id = uuid.uuid4().hex[:12]

    # Phase 2: Confidence -> risk_mult
    confidence = _score_signal(signal, features)
    risk_mult  = confidence

    # Phase 3: Dynamic SL/Trail
    params         = _get_optimal_params(signal, features)
    sl_override    = params.get("sl_atr_mult")
    trail_override = params.get("trail_atr_mult")

    # Phase 4: Signal quality gate
    gate     = _predict_and_gate(signal, features)
    win_prob = gate["win_prob"]
    if win_prob >= 0:
        if win_prob < 0.35:
            risk_mult *= 0.5
        elif win_prob > 0.55:
            risk_mult = min(1.0, risk_mult * 1.2)

    return {
        "signal_id":      signal_id,
        "risk_mult":      round(risk_mult, 3),
        "confidence":     round(confidence, 3),
        "sl_atr_mult":    sl_override,
        "trail_atr_mult": trail_override,
        "win_prob":       round(win_prob, 4),
        "gate_action":    gate["gate_action"],
        "gate_shadow":    gate["gate_shadow"],
        "_features":      features,          # internal - used by commit_ml_signal
        "_committed":     False,             # internal - commit idempotency flag
    }


def commit_ml_signal(
    ml_adj: dict,
    signal: dict,
    size:   dict,
    regime: dict,
) -> None:
    """
    Append the signal record to signals.jsonl.

    MUST be called after the order actually opens - not from
    get_ml_adjustments() - so that signals.jsonl only contains rows that
    correspond to real trades (prevents orphan pollution of P2-P4 training).
    Idempotent: safe to call twice.
    """
    if not ml_adj or ml_adj.get("_committed"):
        return
    sid = ml_adj.get("signal_id")
    if not sid:
        return
    features = ml_adj.get("_features", {})
    _log_signal_entry(sid, signal, size, regime, features)
    ml_adj["_committed"] = True


