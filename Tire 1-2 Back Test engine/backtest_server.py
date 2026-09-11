"""
backtest_server.py
Web dashboard for viewing Tier 1 & Tier 2 backtest results.
Reads JSON output from replay runs and displays analytics.
"""

import os
import json
import glob
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from modules.regime_engine import REGIME_STRATEGY_PERMISSIONS

app = FastAPI(title="CSB Backtest Dashboard", version="1.0.0")

_ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(_ROOT, "results")
DATA_DIR = os.path.join(_ROOT, "data")


def _load_trades(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("trades", [])
    except Exception:
        pass
    return []


def _load_daily(path: str) -> list:
    base = os.path.dirname(path)
    daily_path = os.path.join(base, "replay_daily.json")
    if not os.path.exists(daily_path):
        stem = os.path.splitext(os.path.basename(path))[0]
        daily_path = os.path.join(base, f"{stem}_daily.json")
    if not os.path.exists(daily_path):
        for candidate in glob.glob(os.path.join(base, "*daily*.json")):
            daily_path = candidate
            break
    try:
        with open(daily_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _stats(trades: list) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net": 0.0,
                "profit_factor": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "expectancy": 0.0, "max_drawdown": 0.0}

    nets = [float(t.get("pnl_usdt_net", t.get("net_usdt", 0.0))) for t in trades]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))

    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for pnl in nets:
        running += pnl
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)

    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n * 100, 1),
        "net": round(sum(nets), 4),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else 0.0,
        "avg_win": round(gross_w / len(wins), 4) if wins else 0.0,
        "avg_loss": round(-gross_l / len(losses), 4) if losses else 0.0,
        "expectancy": round(sum(nets) / n, 4),
        "max_drawdown": round(max_dd, 4),
    }


@app.get("/api/results")
async def list_results():
    files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")), reverse=True)
    out = []
    for f in files:
        trades = _load_trades(f)
        st = _stats(trades)
        out.append({
            "file": os.path.basename(f),
            "path": f,
            "trades": st["trades"],
            "net": st["net"],
            "win_rate": st["win_rate"],
            "profit_factor": st["profit_factor"],
            "size_kb": round(os.path.getsize(f) / 1024, 1),
            "modified": datetime.fromtimestamp(os.path.getmtime(f)).isoformat(),
        })
    return {"results": out}


@app.get("/api/results/{filename}")
async def get_result_detail(filename: str):
    path = os.path.join(RESULTS_DIR, filename)
    if not os.path.exists(path):
        return {"error": "File not found"}

    trades = _load_trades(path)
    daily = _load_daily(path)

    by_strategy = {}
    by_symbol = {}
    by_exit = {}
    for t in trades:
        sid = t.get("strategy", "?")
        sym = t.get("symbol", "?")
        ex = t.get("exit_reason", "?")
        by_strategy.setdefault(sid, []).append(t)
        by_symbol.setdefault(sym, []).append(t)
        by_exit.setdefault(ex, []).append(t)

    strat_stats = [{"name": k, **_stats(v)} for k, v in by_strategy.items()]
    strat_stats.sort(key=lambda r: r["net"], reverse=True)

    sym_stats = [{"name": k, **_stats(v)} for k, v in by_symbol.items()]
    sym_stats.sort(key=lambda r: r["net"], reverse=True)

    exit_stats = []
    for k, v in by_exit.items():
        nets = [float(t.get("pnl_usdt_net", t.get("net_usdt", 0.0))) for t in v]
        exit_stats.append({
            "name": k, "trades": len(v), "net": round(sum(nets), 4),
            "share": round(len(v) / len(trades) * 100, 1) if trades else 0,
        })
    exit_stats.sort(key=lambda r: r["trades"], reverse=True)

    equity_curve = []
    running = 0.0
    for t in trades:
        pnl = float(t.get("pnl_usdt_net", t.get("net_usdt", 0.0)))
        running += pnl
        equity_curve.append({
            "time": t.get("time", t.get("exit_time", "")),
            "equity": round(running, 4),
            "symbol": t.get("symbol", ""),
            "strategy": t.get("strategy", ""),
        })

    return {
        "file": filename,
        "overall": _stats(trades),
        "by_strategy": strat_stats,
        "by_symbol": sym_stats,
        "by_exit": exit_stats,
        "equity_curve": equity_curve,
        "daily": daily,
        "regime_config": {k: list(v.keys()) for k, v in REGIME_STRATEGY_PERMISSIONS.items()},
    }


@app.get("/api/data-status")
async def data_status():
    trade_1m = glob.glob(os.path.join(DATA_DIR, "*_1m_*d.csv"))
    mark_1m = glob.glob(os.path.join(DATA_DIR, "*_mark1m_*d.csv"))
    bar_15m = glob.glob(os.path.join(DATA_DIR, "*_15m_*d.csv"))
    funding = glob.glob(os.path.join(DATA_DIR, "funding", "*.csv"))

    regime_ok = all(
        any(s in os.path.basename(f) for f in trade_1m)
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )

    return {
        "trade_1m": len(trade_1m),
        "mark_1m": len(mark_1m),
        "bar_15m": len(bar_15m),
        "funding": len(funding),
        "regime_symbols_ok": regime_ok,
    }


@app.get("/api/regime")
async def regime_config():
    return {
        "permissions": {k: list(v.keys()) for k, v in REGIME_STRATEGY_PERMISSIONS.items()},
    }


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSB Backtest Dashboard</title>
<style>
:root {
  --bg: #0f1219;
  --surface: #181d28;
  --surface-2: #1e2433;
  --border: #2a3040;
  --text: #d0d6e2;
  --text-muted: #6b7590;
  --accent: #5b8def;
  --green: #34d399;
  --red: #f87171;
  --amber: #fbbf24;
  --cyan: #22d3ee;
  --purple: #a78bfa;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); padding: 1.5rem; }
.container { max-width: 1200px; margin: 0 auto; }

header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
header h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: -0.02em; }
header h1 span { color: var(--accent); }
.data-badge { background: var(--surface-2); padding: 0.3rem 0.75rem; border-radius: 6px; font-size: 0.78rem; color: var(--text-muted); border: 1px solid var(--border); }
.data-badge.ok { border-color: var(--green); color: var(--green); }
.data-badge.warn { border-color: var(--amber); color: var(--amber); }

.grid { display: grid; gap: 1rem; }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-2 { grid-template-columns: 1fr 1fr; }
@media (max-width: 768px) { .grid-4, .grid-3, .grid-2 { grid-template-columns: 1fr; } }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; }
.card h3 { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 0.5rem; }
.card .value { font-size: 1.5rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.card .sub { font-size: 0.8rem; color: var(--text-muted); margin-top: 0.2rem; }
.positive { color: var(--green); }
.negative { color: var(--red); }

.results-list { margin-top: 1.5rem; }
.results-list h2 { font-size: 1rem; margin-bottom: 0.75rem; }
.result-row { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 1fr 120px; gap: 0.5rem; align-items: center; padding: 0.65rem 1rem; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 0.5rem; cursor: pointer; transition: border-color 0.15s; font-variant-numeric: tabular-nums; }
.result-row:hover { border-color: var(--accent); }
.result-row .name { font-weight: 600; font-size: 0.88rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.result-row .meta { font-size: 0.82rem; color: var(--text-muted); }

.detail-panel { margin-top: 1.5rem; display: none; }
.detail-panel.active { display: block; }
.detail-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.detail-header h2 { font-size: 1.1rem; }
.btn-back { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); padding: 0.35rem 0.75rem; border-radius: 6px; cursor: pointer; font-size: 0.82rem; }
.btn-back:hover { border-color: var(--accent); }

table { width: 100%; border-collapse: collapse; font-size: 0.85rem; font-variant-numeric: tabular-nums; }
table th { text-align: left; padding: 0.5rem 0.6rem; background: var(--surface-2); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); border-bottom: 1px solid var(--border); }
table td { padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--border); }

.chart-container { position: relative; height: 220px; margin-top: 0.5rem; }
canvas { width: 100% !important; height: 100% !important; }

.regime-grid { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
.regime-tag { padding: 0.25rem 0.6rem; border-radius: 4px; font-size: 0.78rem; font-weight: 600; background: var(--surface-2); border: 1px solid var(--border); }
.regime-tag .strats { color: var(--text-muted); font-weight: 400; margin-left: 0.3rem; }

.empty-state { text-align: center; padding: 3rem; color: var(--text-muted); }
.empty-state h3 { font-size: 1.1rem; margin-bottom: 0.5rem; color: var(--text); }
.loading { text-align: center; padding: 2rem; color: var(--text-muted); }

.section { margin-top: 1.5rem; }
.section h3 { font-size: 0.95rem; margin-bottom: 0.75rem; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1><span>CSB</span> Backtest Dashboard</h1>
    <div id="dataBadge" class="data-badge">checking data...</div>
  </header>

  <div id="listView">
    <div id="regimeCard" class="card" style="margin-bottom:1rem">
      <h3>Regime Strategy Permissions</h3>
      <div id="regimeGrid" class="regime-grid"></div>
    </div>

    <div class="results-list">
      <h2>Backtest Results</h2>
      <div id="resultsList" class="loading">Loading results...</div>
    </div>
  </div>

  <div id="detailView" class="detail-panel">
    <div class="detail-header">
      <h2 id="detailTitle">—</h2>
      <button class="btn-back" onclick="showList()">Back to list</button>
    </div>

    <div class="grid grid-4" id="summaryCards"></div>

    <div class="section">
      <h3>Equity Curve</h3>
      <div class="card">
        <div class="chart-container"><canvas id="equityChart"></canvas></div>
      </div>
    </div>

    <div class="grid grid-2 section">
      <div class="card">
        <h3>By Strategy</h3>
        <div style="overflow-x:auto"><table id="strategyTable"><thead><tr><th>Strategy</th><th>Trades</th><th>Net $</th><th>Win%</th><th>PF</th><th>Expect</th></tr></thead><tbody></tbody></table></div>
      </div>
      <div class="card">
        <h3>By Exit Type</h3>
        <div style="overflow-x:auto"><table id="exitTable"><thead><tr><th>Exit</th><th>Trades</th><th>Share%</th><th>Net $</th></tr></thead><tbody></tbody></table></div>
      </div>
    </div>

    <div class="section">
      <div class="card">
        <h3>By Symbol (top 20)</h3>
        <div style="overflow-x:auto"><table id="symbolTable"><thead><tr><th>Symbol</th><th>Trades</th><th>Net $</th><th>Win%</th><th>PF</th></tr></thead><tbody></tbody></table></div>
      </div>
    </div>

    <div class="section" id="dailySection" style="display:none">
      <div class="card">
        <h3>Daily Breakdown</h3>
        <div style="overflow-x:auto"><table id="dailyTable"><thead><tr><th>Date</th><th>Trades</th><th>Net $</th><th>Cum $</th><th>Win%</th></tr></thead><tbody></tbody></table></div>
      </div>
    </div>
  </div>
</div>

<script>
const API = '';

async function fetchJSON(url) {
  const r = await fetch(API + url);
  return r.json();
}

function colorVal(v, decimals=2) {
  const n = parseFloat(v);
  const cls = n > 0 ? 'positive' : n < 0 ? 'negative' : '';
  return `<span class="${cls}">${n >= 0 ? '+' : ''}${n.toFixed(decimals)}</span>`;
}

async function init() {
  const [dataStatus, regime, results] = await Promise.all([
    fetchJSON('/api/data-status'),
    fetchJSON('/api/regime'),
    fetchJSON('/api/results'),
  ]);

  const badge = document.getElementById('dataBadge');
  if (dataStatus.trade_1m > 0) {
    badge.textContent = `${dataStatus.trade_1m} symbols | regime ${dataStatus.regime_symbols_ok ? 'OK' : 'MISSING'}`;
    badge.className = `data-badge ${dataStatus.regime_symbols_ok ? 'ok' : 'warn'}`;
  } else {
    badge.textContent = 'No data fetched';
    badge.className = 'data-badge warn';
  }

  const rg = document.getElementById('regimeGrid');
  const colors = {BULL_TREND:'var(--green)',BEAR_TREND:'var(--red)',RANGING:'var(--amber)',OVERHEATED:'var(--red)',OVERSOLD:'var(--cyan)'};
  for (const [regime_name, strats] of Object.entries(regime.permissions)) {
    const tag = document.createElement('div');
    tag.className = 'regime-tag';
    tag.style.borderColor = colors[regime_name] || 'var(--border)';
    tag.innerHTML = `${regime_name}<span class="strats">${strats.length ? strats.join(', ') : 'none'}</span>`;
    rg.appendChild(tag);
  }

  const rl = document.getElementById('resultsList');
  if (!results.results.length) {
    rl.innerHTML = '<div class="empty-state"><h3>No results yet</h3><p>Run a backtest first: run_tier2.bat</p></div>';
    return;
  }

  rl.innerHTML = '<div class="result-row" style="background:transparent;border:none;cursor:default;font-weight:600;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.04em;color:var(--text-muted)"><div>File</div><div>Trades</div><div>Net $</div><div>Win%</div><div>PF</div><div>Date</div></div>';
  for (const r of results.results) {
    const row = document.createElement('div');
    row.className = 'result-row';
    row.onclick = () => showDetail(r.file);
    row.innerHTML = `
      <div class="name">${r.file}</div>
      <div class="meta">${r.trades}</div>
      <div>${colorVal(r.net)}</div>
      <div class="meta">${r.win_rate}%</div>
      <div class="meta">${r.profit_factor}</div>
      <div class="meta">${r.modified.split('T')[0]}</div>
    `;
    rl.appendChild(row);
  }
}

async function showDetail(filename) {
  document.getElementById('listView').style.display = 'none';
  document.getElementById('detailView').className = 'detail-panel active';
  document.getElementById('detailTitle').textContent = filename;

  const d = await fetchJSON(`/api/results/${filename}`);
  const o = d.overall;

  document.getElementById('summaryCards').innerHTML = `
    <div class="card"><h3>Total Trades</h3><div class="value">${o.trades}</div><div class="sub">${o.wins} wins</div></div>
    <div class="card"><h3>Net P&L</h3><div class="value ${o.net>=0?'positive':'negative'}">${o.net>=0?'+':''}$${o.net.toFixed(2)}</div><div class="sub">Expectancy: ${colorVal(o.expectancy,4)}</div></div>
    <div class="card"><h3>Win Rate</h3><div class="value">${o.win_rate}%</div><div class="sub">PF: ${o.profit_factor}</div></div>
    <div class="card"><h3>Max Drawdown</h3><div class="value negative">-$${o.max_drawdown.toFixed(2)}</div><div class="sub">Avg loss: $${Math.abs(o.avg_loss).toFixed(4)}</div></div>
  `;

  const stBody = document.querySelector('#strategyTable tbody');
  stBody.innerHTML = d.by_strategy.map(s => `<tr><td><strong>${s.name}</strong></td><td>${s.trades}</td><td>${colorVal(s.net)}</td><td>${s.win_rate}%</td><td>${s.profit_factor}</td><td>${colorVal(s.expectancy,4)}</td></tr>`).join('');

  const exBody = document.querySelector('#exitTable tbody');
  exBody.innerHTML = d.by_exit.map(e => `<tr><td>${e.name}</td><td>${e.trades}</td><td>${e.share}%</td><td>${colorVal(e.net)}</td></tr>`).join('');

  const syBody = document.querySelector('#symbolTable tbody');
  syBody.innerHTML = d.by_symbol.slice(0, 20).map(s => `<tr><td>${s.name}</td><td>${s.trades}</td><td>${colorVal(s.net)}</td><td>${s.win_rate}%</td><td>${s.profit_factor}</td></tr>`).join('');

  if (d.daily && d.daily.length) {
    document.getElementById('dailySection').style.display = 'block';
    const dlBody = document.querySelector('#dailyTable tbody');
    dlBody.innerHTML = d.daily.map(day => `<tr><td>${day.date}</td><td>${day.trades}</td><td>${colorVal(day.net_usdt)}</td><td>${colorVal(day.cum_net_usdt)}</td><td>${day.win_pct}%</td></tr>`).join('');
  } else {
    document.getElementById('dailySection').style.display = 'none';
  }

  drawEquityChart(d.equity_curve);
}

function showList() {
  document.getElementById('listView').style.display = 'block';
  document.getElementById('detailView').className = 'detail-panel';
}

function drawEquityChart(curve) {
  const canvas = document.getElementById('equityChart');
  const ctx = canvas.getContext('2d');
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * 2;
  canvas.height = rect.height * 2;
  ctx.scale(2, 2);
  const W = rect.width, H = rect.height;

  ctx.clearRect(0, 0, W, H);

  if (!curve.length) return;

  const vals = curve.map(c => c.equity);
  const mn = Math.min(0, ...vals);
  const mx = Math.max(...vals);
  const range = mx - mn || 1;
  const pad = { t: 20, r: 10, b: 25, l: 50 };
  const cW = W - pad.l - pad.r;
  const cH = H - pad.t - pad.b;

  ctx.strokeStyle = '#2a3040';
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (cH * i / 4);
    const val = mx - (range * i / 4);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = '#6b7590'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(val.toFixed(2), pad.l - 5, y + 3);
  }

  ctx.strokeStyle = '#2a3040';
  ctx.beginPath();
  const zeroY = pad.t + cH * (mx / range);
  ctx.moveTo(pad.l, zeroY); ctx.lineTo(W - pad.r, zeroY);
  ctx.stroke();

  const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
  const lastVal = vals[vals.length - 1];
  if (lastVal >= 0) {
    grad.addColorStop(0, 'rgba(52, 211, 153, 0.15)');
    grad.addColorStop(1, 'rgba(52, 211, 153, 0.0)');
    ctx.strokeStyle = '#34d399';
  } else {
    grad.addColorStop(0, 'rgba(248, 113, 113, 0.0)');
    grad.addColorStop(1, 'rgba(248, 113, 113, 0.15)');
    ctx.strokeStyle = '#f87171';
  }

  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < vals.length; i++) {
    const x = pad.l + (i / (vals.length - 1)) * cW;
    const y = pad.t + ((mx - vals[i]) / range) * cH;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();

  ctx.lineTo(pad.l + cW, pad.t + cH);
  ctx.lineTo(pad.l, pad.t + cH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
}

init();
</script>
</body>
</html>
""".strip()


if __name__ == "__main__":
    import uvicorn
    print("=" * 54)
    print("  CSB Backtest Dashboard")
    print("  http://localhost:8200")
    print("=" * 54)
    uvicorn.run(app, host="0.0.0.0", port=8200)
