"""
kronos/scorer.py  --  isolated Kronos-small scorer for CSM entry gating.

This module runs in the DEDICATED kronos venv (kronos/venv), NOT the bot's
venv, and is intended to be driven as its own process (batch scoring now; a
fail-open sidecar later).  The live bot must NEVER import this file directly --
torch belongs on the far side of a process boundary from the order loop.

Given the leak-free pre-entry 15m OHLCV(+amount) window of a candidate CSM
entry, it forecasts the next PL bars with Kronos-small and returns:

  dir_ret   signed horizon return toward the trade's direction  (>0 = agrees)
  pred_fav  direction-aware max FAVORABLE excursion Kronos predicts
  pred_adv  direction-aware max ADVERSE  excursion Kronos predicts  (<=0)

The validated gate (walk-forward, both time halves + OOS) is pred_fav:
  pred_fav >= 0.020  ->  PF ~1.5   (clears the 1.4286 after-tax bar)
  pred_fav >= 0.015  ->  PF ~1.4   (higher volume, just under the bar)
dir_ret did NOT survive walk-forward (late-half flat) -- do not gate on it.

Env:
  KRONOS_SRC     path to the cloned shiyu-coder/Kronos repo (default ./src)
  KRONOS_THREADS torch CPU threads   (default 2 -- keep the bot's cores free)
  KRONOS_TOKENIZER / KRONOS_MODEL    HF ids (defaults below)
"""
import os, sys
import pandas as pd

_LB_DEFAULT = 256          # context bars (15m)
_PL_DEFAULT = 8            # forecast horizon bars (15m) = 2h
_Q = pd.Timedelta(minutes=15)

_HERE = os.path.dirname(os.path.abspath(__file__))


class KronosScorer:
    def __init__(self, lb=_LB_DEFAULT, pl=_PL_DEFAULT, device="cpu",
                 threads=None, src=None, tokenizer=None, model=None):
        import torch
        threads = int(os.getenv("KRONOS_THREADS", threads or 2))
        torch.set_num_threads(threads)          # keep the 5 bot services' cores free
        src = src or os.getenv("KRONOS_SRC", os.path.join(_HERE, "src"))
        if src not in sys.path:
            sys.path.insert(0, src)
        from model import Kronos, KronosTokenizer, KronosPredictor
        tok_id = tokenizer or os.getenv("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
        mdl_id = model or os.getenv("KRONOS_MODEL", "NeoQuasar/Kronos-small")
        self.lb, self.pl = lb, pl
        self._tok = KronosTokenizer.from_pretrained(tok_id)
        self._mdl = Kronos.from_pretrained(mdl_id)
        # Disable dropout layers for deterministic inference scoring
        self._tok.eval()
        self._mdl.eval()
        self._pred = KronosPredictor(self._mdl, self._tok, device=device, max_context=512)

    def score_window(self, ctx15, direction):
        """ctx15: DataFrame indexed by 15m timestamp with columns
        open,high,low,close,volume,amount -- already sliced LEAK-FREE (only bars
        whose close <= entry time).  direction: 'LONG' or 'SHORT'.
        Returns dict or None if insufficient context."""
        if ctx15 is None or len(ctx15) < self.lb:
            return None
        ctx = ctx15.iloc[-self.lb:]
        c = float(ctx["close"].iloc[-1])
        xdf = ctx[["open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
        xts = pd.Series(ctx.index)
        yts = pd.Series(pd.date_range(ctx.index[-1] + _Q, periods=self.pl, freq="15min"))
        out = self._pred.predict(df=xdf, x_timestamp=xts, y_timestamp=yts, pred_len=self.pl,
                                 T=1.0, top_p=0.9, sample_count=1, verbose=False)
        pc = out["close"].to_numpy(float); ph = out["high"].to_numpy(float); pl = out["low"].to_numpy(float)
        long = direction == "LONG"
        ret = (pc[-1] - c) / c
        return dict(
            dir_ret=float(ret if long else -ret),
            pred_fav=float(((ph - c) / c).max()) if long else float(((c - pl) / c).max()),
            pred_adv=float(((pl - c) / c).min()) if long else float(((c - ph) / c).min()),
        )

    @staticmethod
    def leakfree_ctx15(df1m, entry_time):
        """Resample a 1m OHLCV(+quote_asset_volume) frame to 15m and keep only
        bars whose close (t+15m) <= entry_time -- the live scanner's rule."""
        d = df1m.rename(columns={"quote_asset_volume": "amount"}) if "quote_asset_volume" in df1m.columns else df1m
        ag = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "amount": "sum"}
        r = d[["open", "high", "low", "close", "volume", "amount"]].resample("15min").agg(ag).dropna()
        et = pd.Timestamp(entry_time)
        return r[r.index + _Q <= et]


if __name__ == "__main__":
    # Smoke test: load model (downloads weights on first run) and score one
    # window both ways.  Uses a real 90d 1m CSV if present (dev box); otherwise
    # falls back to a synthetic random-walk window so the check runs anywhere
    # (e.g. the server, which does not keep the backtest CSVs).
    import glob
    data = os.getenv("KRONOS_DATA", os.path.join(os.path.dirname(_HERE), "data"))
    files = sorted(glob.glob(os.path.join(data, "*_1m_90d.csv")))
    print("loading Kronos-small ...", flush=True)
    sc = KronosScorer()
    if files:
        df = pd.read_csv(files[0], parse_dates=["open_time"]).set_index("open_time")
        ctx = KronosScorer.leakfree_ctx15(df, df.index[-1] + _Q)
        print("scoring real window from", os.path.basename(files[0]))
    else:
        import numpy as np
        n = 300; idx = pd.date_range("2026-01-01", periods=n, freq="15min")
        px = 100 * np.exp(np.cumsum(np.random.normal(0, 0.001, n)))
        ctx = pd.DataFrame({"open": px, "high": px * 1.001, "low": px * 0.999,
                            "close": px, "volume": 1.0, "amount": 1.0}, index=idx)
        print("no 90d CSV found -> scoring a synthetic window (stack check only)")
    for d in ("LONG", "SHORT"):
        print(d, sc.score_window(ctx, d))
    print("OK")

