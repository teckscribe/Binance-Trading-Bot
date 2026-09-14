// app.js - CSB Web Server Frontend Logic

// Kronos score for a CSM trade/position. null = entered with no score
// (fail-open); absent = non-CSM or gate off.
function kronosCell(r) {
    if (r.strategy !== "CSM" || !("kronos_pred_fav" in r)) return '<span class="dim">—</span>';
    const pf = r.kronos_pred_fav;
    if (pf === null || pf === undefined) return '<span class="text-amber" title="entered without a score (fail-open)">n/a</span>';
    const thr = Number(r.settings_entry?.KRONOS_PF_THR);
    const ok = isFinite(thr) ? pf >= thr : null;
    const cls = ok === null ? "" : ok ? "text-green" : "text-amber";
    return `<span class="${cls}" title="${isFinite(thr) ? "threshold " + thr : ""}">${(pf >= 0 ? "+" : "") + Number(pf).toFixed(4)}</span>`;
}

function regimeCell(r) {
    if (!r.regime_entry) return '<span class="dim">—</span>';
    const short = (x) => String(x).replace("_TREND", "");
    if (r.regime_exit && r.regime_exit !== r.regime_entry) return `${short(r.regime_entry)} → ${short(r.regime_exit)}`;
    return short(r.regime_entry);
}

async function fetchStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        const statusBadge = document.getElementById('server-status-badge');
        const botStatus = document.getElementById('bot-status');

        // Check if the scanner process has stopped updating the state file
        const now = new Date();
        const lastUpdate = new Date(data.timestamp);
        const diffSeconds = (now - lastUpdate) / 1000;
        // `running:false` is written by the scanner's shutdown handler, so a
        // clean stop is known immediately rather than waiting out the staleness
        // timeout. The timestamp check still covers crashes and SIGKILL, where
        // the handler never runs and the file is simply abandoned.
        const isOffline = data.running === false || diffSeconds > 120;

        if (statusBadge) {
            if (isOffline) {
                statusBadge.textContent = "OFFLINE 🔴";
                statusBadge.style.color = "#E11D48";
                statusBadge.style.borderColor = "rgba(225, 29, 72, 0.30)";
            } else {
                statusBadge.textContent = "ONLINE 🟢";
                statusBadge.style.color = "#059669";
                statusBadge.style.borderColor = "rgba(5, 150, 105, 0.30)";
            }
        }
        
        if (botStatus) {
            if (isOffline) {
                botStatus.textContent = "STOPPED";
                botStatus.style.color = "#E11D48";
            } else {
                botStatus.textContent = "SCANNING";
                botStatus.style.color = "var(--text-main)";
            }
        }

        // Update Trade Mode badge (PAPER vs LIVE).
        // Paper positions are simulated — the dashboard must say so plainly,
        // or a simulated P&L reads as real money.
        const modeEl = document.getElementById('trade-mode-badge');
        if (modeEl) {
            const mode = data.mode || "UNKNOWN";
            if (mode === "LIVE") {
                modeEl.textContent = "⚡ LIVE — REAL FUNDS";
                modeEl.style.color = "#DC2626";
                modeEl.style.borderColor = "rgba(220, 38, 38, 0.35)";
                modeEl.style.background = "rgba(220, 38, 38, 0.08)";
            } else if (mode === "PAPER") {
                modeEl.textContent = "📝 PAPER — SIMULATED";
                modeEl.style.color = "#B45309";
                modeEl.style.borderColor = "rgba(180, 83, 9, 0.35)";
                modeEl.style.background = "rgba(180, 83, 9, 0.08)";
            } else {
                modeEl.textContent = "MODE UNKNOWN";
                modeEl.style.color = "#64748B";
                modeEl.style.borderColor = "rgba(100, 116, 139, 0.30)";
                modeEl.style.background = "rgba(100, 116, 139, 0.08)";
            }
        }

        // Update Regime
        const regimeEl = document.getElementById('ai-regime');
        if(regimeEl) {
            regimeEl.textContent = data.regime + " 🛡️";
            if(data.regime.includes("BULL")) regimeEl.style.color = "#047857";
            else if(data.regime.includes("BEAR")) regimeEl.style.color = "#DC2626";
            else regimeEl.style.color = "#2563EB";
        }

        // Update PNL — use total_pnl_pct (cumulative since period start) so
        // the Live tab agrees with the Performance tab and wallet balance.
        // Falls back to session_pnl_pct for older active_state.json files
        // that don't have the new field yet.
        const pnlEl = document.getElementById('net-pnl');
        if(pnlEl) {
            const rawPnl  = data.total_pnl_pct ?? data.session_pnl_pct ?? 0;
            const pnlPct  = Number(rawPnl) * 100;
            const start   = Number(data.starting_equity) || Number(data.account_equity) || 0;
            const pnlUsd  = Number(data.account_equity) - start;
            const sign    = pnlUsd >= 0 ? "+$" : "-$";
            pnlEl.textContent = pnlPct.toFixed(2) + "%  " + sign + Math.abs(pnlUsd).toFixed(2);
            pnlEl.className = "metric-value " + (pnlPct >= 0 ? "text-green" : "text-red");
        }

        // Update Active Count
        if(document.getElementById('active-count')) {
            document.getElementById('active-count').textContent = data.n_open || 0;
        }

        // Update Last Update Time
        if(document.getElementById('last-update')) {
            const date = new Date(data.timestamp);
            document.getElementById('last-update').textContent = date.toLocaleTimeString();
        }

        // Update Wallet Balance
        if(document.getElementById('wallet-balance')) {
            const balance = data.account_equity || 0;
            document.getElementById('wallet-balance').textContent = "$" + balance.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
        }

        // Update Positions Table
        const tbody = document.getElementById('positions-tbody');
        if(tbody && data.active_positions) {
            if(data.active_positions.length === 0) {
                tbody.innerHTML = isOffline
                    ? '<tr><td colspan="12" class="empty-msg">Scanner stopped — no open positions.</td></tr>'
                    : '<tr><td colspan="12" class="empty-msg">No active open positions. Auto-trader scanning market...</td></tr>';
            } else {
                tbody.innerHTML = '';
                // If the scanner is down these rows are a frozen snapshot, not
                // live state — the positions may already have been closed.
                // Label them rather than letting them read as current.
                if (isOffline) {
                    const warn = document.createElement('tr');
                    warn.innerHTML = `<td colspan="12" class="empty-msg" style="color:var(--warn);">
                        ⚠️ Scanner is stopped — these are the last known positions, not live data.
                        Check Telegram for actual close confirmations.</td>`;
                    tbody.appendChild(warn);
                }
                data.active_positions.forEach(pos => {
                    const tr = document.createElement('tr');
                    
                    const roiNum   = (Number(pos.roi_pct) || 0) * 100;
                    const pnlUsdt  = Number(pos.pnl_usdt) || 0;
                    const pnlClass = roiNum >= 0 ? "text-green" : "text-red";
                    const sideClass = pos.direction === "LONG" ? "text-green" : "text-red";
                    const usd = (v) => (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);

                    tr.innerHTML = `
                        <td><strong>${pos.symbol}</strong></td>
                        <td class="${sideClass}">${pos.direction}</td>
                        <td>${pos.strategy}</td>
                        <td>$${pos.entry_price.toFixed(4)}</td>
                        <td>${pos.leverage}x</td>
                        <td>$${(Number(pos.notional) || 0).toFixed(2)}</td>
                        <td>$${(Number(pos.margin_req) || 0).toFixed(2)}</td>
                        <td class="${pnlClass}">${roiNum.toFixed(2)}% <span class="pnl-usd">${usd(pnlUsdt)}</span></td>
                        <td>$${pos.sl_price ? pos.sl_price.toFixed(4) : '--'}</td>
                        <td>${pos.duration_min || 0}</td>
                        <td>${kronosCell(pos)}</td>
                        <td><button class="btn-square-off" onclick="squareOff('${pos.symbol}','${pos.direction}',this,${pos.pnl_equity_pct ?? 'null'},${pnlUsdt ?? 'null'})">Square Off</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }
    } catch (error) {
        console.error("Error fetching status:", error);
        const statusBadge = document.getElementById('server-status-badge');
        const botStatus = document.getElementById('bot-status');
        if (statusBadge) {
            statusBadge.textContent = "OFFLINE 🔴";
            statusBadge.style.color = "#E11D48";
            statusBadge.style.borderColor = "rgba(225, 29, 72, 0.30)";
        }
        if (botStatus) {
            botStatus.textContent = "OFFLINE";
            botStatus.style.color = "#E11D48";
        }
    }
}

async function fetchTrades() {
    try {
        const response = await fetch('/api/trades');
        const data = await response.json();
        
        const tbody = document.getElementById('decisions-tbody');
        if(tbody && data.trades) {
            if(data.trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="13" class="empty-msg">No closed trades yet.</td></tr>';
            } else {
                tbody.innerHTML = '';
                data.trades.forEach(trade => {
                    const tr = document.createElement('tr');
                    // live_logger already stores pnl_equity_pct as a percentage
                    // (round(pnl * 100, 4)) — do NOT multiply by 100 again.
                    const pnlNum = Number(trade.pnl_equity_pct) || 0;
                    const pnl = pnlNum.toFixed(2);
                    const pnlClass = pnlNum >= 0 ? "text-green" : "text-red";
                    const sideClass = trade.direction === "LONG" ? "text-green" : "text-red";

                    // Field names follow live_logger's record: "time" and
                    // "exit_reason" (not "timestamp"/"reason").
                    const ts = trade.time || trade.timestamp || "";
                    // live_logger stamps trades in UTC. Splitting the raw ISO
                    // string showed UTC while journalctl and every other
                    // surface show IST, so a trade opened at 00:36 IST
                    // appeared as 19:06 — six hours adrift and easy to read
                    // as wrong data. Render in the bot's timezone instead.
                    let timeCell = "--";
                    if (ts) {
                        const d = new Date(ts);
                        timeCell = isNaN(d)
                            ? (ts.includes('T') ? ts.split('T')[1].split('.')[0] : ts)
                            : d.toLocaleTimeString('en-GB', {
                                  timeZone: 'Asia/Kolkata', hour12: false });
                    }

                    const mode = trade.mode || "?";
                    const modeClass = mode === "LIVE" ? "text-red" : "text-amber";

                    const num = (v) => (typeof v === "number" ? v : Number(v) || 0);
                    const usd = (v) => (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
                    // Prefer the net figure — it has the round-trip fee deducted.
                    const pnlUsdt = num(trade.pnl_usdt_net ?? trade.pnl_usdt);

                    tr.innerHTML = `
                        <td>${timeCell}</td>
                        <td class="${modeClass}"><strong>${mode}</strong></td>
                        <td><strong>${trade.symbol}</strong></td>
                        <td class="${sideClass}">${trade.direction}</td>
                        <td>${trade.strategy}</td>
                        <td>$${num(trade.notional).toFixed(2)}</td>
                        <td>$${num(trade.entry_price).toFixed(4)}</td>
                        <td>$${num(trade.exit_price).toFixed(4)}</td>
                        <td>${trade.exit_reason || trade.reason || "--"}</td>
                        <td class="${pnlClass}">${pnl}% <span class="pnl-usd">${usd(pnlUsdt)}</span></td>
                        <td>${trade.duration_min || 0}m</td>
                        <td>${kronosCell(trade)}</td>
                        <td>${regimeCell(trade)}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }
    } catch (error) {
        console.error("Error fetching trades:", error);
    }
}

// Strategy panel — driven by /api/strategies, which reads StrategyFactory.
// Never hardcode the strategy list here: the panel previously showed 4 of the
// 7 strategies actually running because it was static HTML.
async function fetchStrategies() {
    const grid = document.getElementById('strategy-grid');
    if (!grid) return;
    try {
        const res  = await fetch('/api/strategies');
        const data = await res.json();
        const list = data.strategies || [];

        if (!list.length) {
            grid.innerHTML = `<div class="ai-card">
                <div class="ai-title">No strategies loaded</div>
                <div class="ai-val" style="color:#DC2626;">ERROR</div>
                <div class="ai-sub">${data.error || 'StrategyFactory returned nothing'}</div>
            </div>`;
            return;
        }

        grid.innerHTML = list.map(s => {
            const active = s.status === 'ACTIVE';
            const colour = active ? s.colour : '#64748B';
            // Say WHY a strategy is not trading. A greyed-out "OFF" alone
            // reads as a fault; these are deliberate config states.
            let why = `${s.tier} &middot; ${s.desc}`;
            if (s.status === 'OFF') {
                why = 'Not permitted in any regime &middot; disabled by config';
            } else if (s.status === 'IDLE') {
                why = `Waiting for ${(s.permitted_in || []).join(' / ')}`;
            } else if (s.status === 'DISABLED') {
                why = 'Manually disabled via Telegram/Discord';
            }
            return `<div class="ai-card">
                <div class="ai-title">${s.name} (${s.id})</div>
                <div class="ai-val" style="color:${colour};">${s.status}</div>
                <div class="ai-sub">${why}</div>
            </div>`;
        }).join('');

        const countEl = document.getElementById('strategy-count');
        if (countEl) {
            // Count only strategies that can actually fire right now. This
            // used to count everything not manually disabled, so it read
            // "3/3 active" while two of the three could never trade.
            const on = list.filter(s => s.status === 'ACTIVE').length;
            const rg = data.regime ? ` in ${data.regime}` : '';
            countEl.textContent = `— ${on}/${list.length} active${rg}`;
        }
        // The strategy IDs used to be appended to the header subtitle. That
        // span was removed — the same information is in the strategy panel
        // below, with per-strategy status rather than just names.
    } catch (error) {
        console.error("Error fetching strategies:", error);
        grid.innerHTML = `<div class="ai-card">
            <div class="ai-title">Strategy panel unavailable</div>
            <div class="ai-val" style="color:#DC2626;">OFFLINE</div>
            <div class="ai-sub">${error}</div>
        </div>`;
    }
}

// ─── Performance tab ─────────────────────────────────────────────────────────

const usd  = (v) => (v >= 0 ? "+$" : "-$") + Math.abs(Number(v) || 0).toFixed(2);
const cls  = (v) => (Number(v) >= 0 ? "text-green" : "text-red");
const pfTxt = (v) => (v === null || v === undefined ? "∞" : Number(v).toFixed(2));

// Ratio stats (win rate, profit factor, top-trade share) are noise on a tiny
// sample. The backend sets sample_ok=false below MIN_SAMPLE_FOR_RATIOS; we
// render an em dash rather than a number, because "0% / 0.00" on one losing
// trade reads as a broken strategy instead of an empty sample.
const DASH = "—";
const ratio = (v, ok, suffix = "") =>
    ok ? ((v ?? 0) + suffix) : DASH;
const ratioPf = (v, ok) => (ok ? pfTxt(v) : DASH);
// Never colour a suppressed value — a red dash still reads as a verdict.
const ratioCls = (ok, colour) => (ok ? colour : "text-muted");
const sampleHint = (n, ok) =>
    ok ? "" : `Not shown: ${n} trade${n === 1 ? "" : "s"} is too small a sample`;

async function fetchPerformance() {
    try {
        const d = await (await fetch('/api/performance')).json();
        const o = d.overall || {};

        const set = (id, txt, colour, title) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = txt;
            if (colour) el.className = "metric-value " + colour;
            if (title !== undefined) {
                if (title) el.title = title; else el.removeAttribute('title');
            }
        };
        set('pf-trades',  o.trades || 0);
        set('pf-net',     usd(o.net), cls(o.net));
        const ok = o.sample_ok === true;
        const hint = sampleHint(o.trades ?? 0, ok);
        set('pf-winrate', ratio(o.win_rate, ok, "%"), ratioCls(ok, ""), hint);
        set('pf-factor',  ratioPf(o.profit_factor, ok),
            ratioCls(ok, (o.profit_factor ?? 0) >= 1 ? "text-green" : "text-red"), hint);
        // A single trade carrying most of the profit means the edge is thin.
        set('pf-topshare', ratio(o.top_trade_share, ok, "%"),
            ratioCls(ok, (o.top_trade_share ?? 0) > 50 ? "text-amber" : ""), hint);

        // Say WHICH trades these totals cover. They are scoped to the current
        // equity period so Net P&L agrees with the wallet balance; without this
        // note the tab summed every session ever logged and could show a profit
        // sitting directly above a wallet that was down.
        const scopeEl = document.getElementById('perf-scope');
        if (scopeEl) {
            const p = d.period || {};
            const a = d.alltime || {};
            if (p.scope === 'period') {
                const since = p.period_start ? new Date(p.period_start)
                    .toLocaleString('en-GB', {timeZone: 'Asia/Kolkata',
                        day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'})
                    : '—';
                scopeEl.textContent =
                    `current equity period — ${p.trades_in_period} trades since ${since} IST`
                    + ` · all-time: ${p.trades_all_time} trades, ${usd(a.net)}`;
            } else {
                scopeEl.textContent = `all ${p.trades_all_time || 0} logged trades`;
            }
        }

        const rowsInto = (id, rows, cols) => {
            const tb = document.getElementById(id);
            if (!tb) return;
            if (!rows.length) {
                tb.innerHTML = `<tr><td colspan="${cols}" class="empty-msg">No trades yet.</td></tr>`;
                return;
            }
            tb.innerHTML = "";
            rows.forEach(r => { tb.appendChild(r); });
        };

        const tr = (cells) => {
            const el = document.createElement('tr');
            el.innerHTML = cells;
            return el;
        };

        rowsInto('perf-strategy-tbody', (d.by_strategy || []).map(s => tr(`
            <td><strong>${s.name}</strong></td>
            <td>${s.trades}</td>
            <td class="${ratioCls(s.sample_ok, '')}" title="${sampleHint(s.trades, s.sample_ok)}">${ratio(s.win_rate, s.sample_ok, "%")}</td>
            <td class="${ratioCls(s.sample_ok, (s.profit_factor ?? 0) >= 1 ? 'text-green' : 'text-red')}" title="${sampleHint(s.trades, s.sample_ok)}">${ratioPf(s.profit_factor, s.sample_ok)}</td>
            <td class="text-green">${usd(s.avg_win)}</td>
            <td class="text-red">${usd(s.avg_loss)}</td>
            <td class="${cls(s.expectancy)}">${usd(s.expectancy)}</td>
            <td class="${ratioCls(s.sample_ok, s.top_trade_share > 50 ? 'text-amber' : '')}" title="${sampleHint(s.trades, s.sample_ok)}">${ratio(s.top_trade_share, s.sample_ok, "%")}</td>
            <td class="${cls(s.net)}"><strong>${usd(s.net)}</strong></td>`)), 9);

        rowsInto('perf-symbol-tbody', (d.by_symbol || []).map(s => tr(`
            <td><strong>${s.name}</strong></td>
            <td>${s.trades}</td>
            <td class="${ratioCls(s.sample_ok, '')}" title="${sampleHint(s.trades, s.sample_ok)}">${ratio(s.win_rate, s.sample_ok, "%")}</td>
            <td class="${ratioCls(s.sample_ok, (s.profit_factor ?? 0) >= 1 ? 'text-green' : 'text-red')}" title="${sampleHint(s.trades, s.sample_ok)}">${ratioPf(s.profit_factor, s.sample_ok)}</td>
            <td class="text-green">${usd(s.avg_win)}</td>
            <td class="text-red">${usd(s.avg_loss)}</td>
            <td class="${cls(s.expectancy)}">${usd(s.expectancy)}</td>
            <td class="${cls(s.net)}"><strong>${usd(s.net)}</strong></td>`)), 8);

        rowsInto('perf-exit-tbody', (d.by_exit || []).map(e => tr(`
            <td><strong>${e.name}</strong></td>
            <td>${e.trades}</td>
            <td>${e.share}%</td>
            <td class="${cls(e.net)}">${usd(e.net)}</td>`)), 4);

        drawCurve(d.equity_curve || []);
    } catch (err) {
        const tb = document.getElementById('perf-strategy-tbody');
        if (tb) tb.innerHTML = `<tr><td colspan="9" class="empty-msg">Performance unavailable: ${err}</td></tr>`;
    }
}

function drawCurve(points) {
    const svg = document.getElementById('perf-curve');
    if (!svg) return;
    if (points.length < 2) {
        svg.innerHTML = `<text x="500" y="110" fill="#64748B" font-size="16"
                          text-anchor="middle">Not enough trades to plot</text>`;
        return;
    }
    const W = 1000, H = 220, PAD = 10;
    const vals = points.map(p => p.equity);
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const span = (hi - lo) || 1;
    const x = i => PAD + (i / (points.length - 1)) * (W - 2 * PAD);
    const y = v => H - PAD - ((v - lo) / span) * (H - 2 * PAD);

    const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ');
    const area = `${line} L${x(points.length - 1).toFixed(1)},${y(0).toFixed(1)} L${x(0).toFixed(1)},${y(0).toFixed(1)} Z`;
    const up = vals[vals.length - 1] >= 0;
    const col = up ? '#047857' : '#DC2626';

    svg.innerHTML = `
        <path d="${area}" fill="${col}" opacity="0.12"/>
        <line x1="${PAD}" y1="${y(0)}" x2="${W - PAD}" y2="${y(0)}"
              stroke="#64748B" stroke-width="1" stroke-dasharray="4 4" opacity="0.5"/>
        <path d="${line}" fill="none" stroke="${col}" stroke-width="2"
              vector-effect="non-scaling-stroke"/>
        <text x="${PAD + 4}" y="16" fill="#64748B" font-size="12">
            ${points.length} trades · final ${usd(vals[vals.length - 1])}
        </text>`;
}

// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const want = btn.dataset.tab;
        document.getElementById('tab-live').style.display = want === 'live' ? '' : 'none';
        document.getElementById('tab-perf').style.display = want === 'perf' ? '' : 'none';
        const st = document.getElementById('tab-settings');
        if (st) st.style.display = want === 'settings' ? '' : 'none';
        if (want === 'perf') fetchPerformance();
        // Re-read on entry so the form always reflects .env, including edits
        // made from Telegram/Discord since the page was loaded.
        if (want === 'settings') fetchSettings();
    });
});

// Initial load
fetchStatus();
fetchTrades();
fetchStrategies();
fetchPerformance();

// Poll every 5 seconds
setInterval(fetchStatus, 5000);
setInterval(fetchTrades, 10000);
// Strategy set changes rarely (only via /disable overrides) — poll slowly.
// 15s, was 60s. Strategy status used to change only when someone toggled a
// strategy from Telegram/Discord, so a slow poll was fine. It now also flips
// ACTIVE <-> IDLE with the REGIME, and the regime badge refreshes every 5s
// from /api/status — at 60s the two panels could disagree for a full minute
// after a regime change. The endpoint is cheap (two small JSON reads).
setInterval(fetchStrategies, 15000);
// Performance reads every session log on disk, so keep it infrequent.
setInterval(() => {
    if (document.getElementById('tab-perf').style.display !== 'none') fetchPerformance();
}, 30000);

// In-page confirmation for Square Off.
//
// Replaces window.confirm(), which browsers let users permanently suppress
// ("prevent this page from creating additional dialogs") — after which a single
// stray tap would close a live position with no prompt at all. This modal also
// shows WHAT is being closed and its current P&L, so the decision is made with
// the numbers in view rather than from memory.
function confirmSquareOff(symbol, direction, pnlPct, pnlUsd) {
    return new Promise(resolve => {
        const prev = document.getElementById('sq-modal');
        if (prev) prev.remove();

        const cls = (pnlPct >= 0) ? 'text-green' : 'text-red';
        const pnl = (pnlPct === null || pnlPct === undefined)
            ? '<span class="sq-muted">unavailable</span>'
            : `<span class="${cls}">${(pnlPct * 100).toFixed(2)}%` +
              (pnlUsd === null || pnlUsd === undefined ? '' :
               ` (${pnlUsd >= 0 ? '+' : ''}${Number(pnlUsd).toFixed(2)} USDT)`) + '</span>';

        const wrap = document.createElement('div');
        wrap.id = 'sq-modal';
        wrap.className = 'sq-overlay';
        wrap.innerHTML = `
          <div class="sq-box" role="dialog" aria-modal="true" aria-labelledby="sq-title">
            <h3 id="sq-title">Square off this position?</h3>
            <dl class="sq-rows">
              <dt>Symbol</dt><dd><strong>${symbol}</strong></dd>
              <dt>Direction</dt><dd><strong>${direction}</strong></dd>
              <dt>Unrealized P&amp;L</dt><dd>${pnl}</dd>
            </dl>
            <p class="sq-note">Closes at <strong>market price</strong> on the next
               scanner cycle (~5s). This cannot be undone.</p>
            <div class="sq-actions">
              <button class="sq-cancel" id="sq-no">Cancel</button>
              <button class="sq-confirm" id="sq-yes">Confirm Square Off</button>
            </div>
          </div>`;
        document.body.appendChild(wrap);

        const done = (v) => {
            document.removeEventListener('keydown', onKey);
            wrap.remove();
            resolve(v);
        };
        const onKey = (e) => {
            if (e.key === 'Escape') done(false);
            if (e.key === 'Enter')  done(true);
        };
        document.addEventListener('keydown', onKey);
        wrap.querySelector('#sq-no').onclick  = () => done(false);
        wrap.querySelector('#sq-yes').onclick = () => done(true);
        // Clicking the backdrop cancels; clicking inside the box must not.
        wrap.onclick = (e) => { if (e.target === wrap) done(false); };
        wrap.querySelector('#sq-yes').focus();
    });
}

async function squareOff(symbol, direction, btn, pnlPct, pnlUsd) {
    if (!await confirmSquareOff(symbol, direction, pnlPct, pnlUsd)) {
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Closing...';
    btn.classList.add('btn-square-off-pending');
    try {
        const res = await fetch('/api/square_off', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({symbol, direction}),
        });
        const data = await res.json();
        if (data.ok) {
            btn.textContent = 'Queued';
        } else {
            btn.textContent = 'Failed';
            btn.disabled = false;
            btn.classList.remove('btn-square-off-pending');
            alert('Square off failed: ' + (data.error || 'unknown error'));
        }
    } catch (err) {
        btn.textContent = 'Error';
        btn.disabled = false;
        btn.classList.remove('btn-square-off-pending');
        alert('Network error: ' + err.message);
    }
}

// ─── Settings tab ────────────────────────────────────────────────────────────
//
// Values are edited locally and only sent on Save, so a mistyped digit never
// reaches .env mid-keystroke. The server re-validates everything regardless —
// these input types are an operator convenience, not a trust boundary.

let SET_FIELDS = [];     // spec from the server
let SET_EDITABLE = false;

const setEsc = (v) => String(v ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function setControl(f) {
    const id = `set-f-${f.key}`;
    const dis = SET_EDITABLE ? "" : "disabled";
    if (f.type === "bool") {
        const on = String(f.value).toLowerCase() === "true";
        return `<label class="set-switch">
                  <input type="checkbox" id="${id}" data-key="${f.key}" ${on ? "checked" : ""} ${dis}>
                  <span>${on ? "On" : "Off"}</span>
                </label>`;
    }
    if (f.type === "choice") {
        const opts = (f.options || []).map(o =>
            `<option value="${setEsc(o)}"${String(f.value) === o ? " selected" : ""}>${setEsc(o)}</option>`
        ).join("");
        return `<select id="${id}" data-key="${f.key}" ${dis}>${opts}</select>`;
    }
    if (f.type === "int" || f.type === "float") {
        const step = f.step || (f.type === "int" ? "1" : "any");
        const mn = f.min !== undefined ? `min="${f.min}"` : "";
        const mx = f.max !== undefined ? `max="${f.max}"` : "";
        return `<input type="number" id="${id}" data-key="${f.key}"
                       value="${setEsc(f.value)}" step="${step}" ${mn} ${mx} ${dis}>`;
    }
    return `<input type="text" id="${id}" data-key="${f.key}"
                   value="${setEsc(f.value)}" ${dis}>`;
}

function renderSettings(d) {
    SET_FIELDS = d.fields || [];
    SET_EDITABLE = d.editable === true;

    const lock = document.getElementById('set-lock');
    if (lock) {
        lock.textContent = SET_EDITABLE ? "Unlocked with 2FA" : "Read-only";
        lock.className = "pill " + (SET_EDITABLE ? "pill-green" : "pill-amber");
    }
    const reason = document.getElementById('set-reason');
    if (reason) {
        reason.textContent = d.reason || "";
        reason.style.display = d.reason ? "" : "none";
    }
    const excl = document.getElementById('set-excluded');
    if (excl) excl.textContent = d.excluded_note || "";

    const groups = {};
    SET_FIELDS.forEach(f => (groups[f.group] = groups[f.group] || []).push(f));

    const host = document.getElementById('set-groups');
    if (!host) return;
    host.innerHTML = Object.entries(groups).map(([name, fields]) => `
        <div class="set-group">
          <h3>${setEsc(name)}</h3>
          ${fields.map(f => `
            <div class="set-row" data-row="${f.key}">
              <div class="set-label">
                <label for="set-f-${f.key}">${setEsc(f.label || f.key)}</label>
                <code>${setEsc(f.key)}</code>
                ${f.restart ? `<span class="pill pill-amber set-pill">restart</span>` : ""}
                ${f.help ? `<span class="set-help">${setEsc(f.help)}</span>` : ""}
              </div>
              <div class="set-control">${setControl(f)}</div>
            </div>`).join("")}
        </div>`).join("");

    // Reflect the on/off word next to a toggle, and mark rows that differ from
    // what is currently saved so "what am I about to change" is visible.
    host.querySelectorAll('[data-key]').forEach(el => {
        el.addEventListener('input', markDirty);
        el.addEventListener('change', () => {
            if (el.type === "checkbox") {
                const span = el.parentElement.querySelector('span');
                if (span) span.textContent = el.checked ? "On" : "Off";
            }
            markDirty();
        });
    });

    const otp = document.getElementById('set-otp-input');
    const save = document.getElementById('set-save');
    if (otp) otp.disabled = !SET_EDITABLE;
    if (save) save.disabled = !SET_EDITABLE;
    markDirty();
}

function currentValue(f) {
    const el = document.getElementById(`set-f-${f.key}`);
    if (!el) return null;
    if (el.type === "checkbox") return el.checked ? "true" : "false";
    return el.value;
}

function collectChanges() {
    const out = {};
    SET_FIELDS.forEach(f => {
        const v = currentValue(f);
        if (v === null) return;
        if (String(v) !== String(f.value)) out[f.key] = v;
    });
    return out;
}

function markDirty() {
    const changes = collectChanges();
    SET_FIELDS.forEach(f => {
        const row = document.querySelector(`[data-row="${f.key}"]`);
        if (row) row.classList.toggle('set-dirty', f.key in changes);
    });
    const save = document.getElementById('set-save');
    const n = Object.keys(changes).length;
    if (save) {
        save.textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save changes";
        save.disabled = !SET_EDITABLE || n === 0;
    }
}

function setStatus(msg, kind) {
    const el = document.getElementById('set-status');
    if (!el) return;
    el.textContent = msg || "";
    el.className = "set-status" + (kind ? " " + kind : "");
}

async function fetchSettings() {
    try {
        const d = await (await fetch('/api/settings')).json();
        renderSettings(d);
    } catch (err) {
        setStatus("Could not load settings: " + err.message, "text-red");
    }
    fetchSettingsHistory();
}

async function fetchSettingsHistory() {
    const body = document.getElementById('set-history-body');
    if (!body) return;
    try {
        const d = await (await fetch('/api/settings/history?limit=30')).json();
        const rows = d.changes || [];
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="5" class="set-note-dim">No changes recorded yet.</td></tr>`;
            return;
        }
        body.innerHTML = rows.map(r => `
            <tr>
              <td>${setEsc(String(r.ts || "").replace("T", " ").replace("+00:00", ""))}</td>
              <td>${setEsc(r.source)}</td>
              <td><code>${setEsc(r.key)}</code></td>
              <td>${setEsc(r.from)}</td>
              <td><b>${setEsc(r.to)}</b></td>
            </tr>`).join("");
    } catch (err) {
        body.innerHTML = `<tr><td colspan="5" class="text-red">Could not load history: ${setEsc(err.message)}</td></tr>`;
    }
}

async function saveSettings() {
    const changes = collectChanges();
    if (!Object.keys(changes).length) { setStatus("Nothing to save."); return; }

    const otpEl = document.getElementById('set-otp-input');
    const otp = (otpEl?.value || "").trim();
    if (!/^\d{6}$/.test(otp)) {
        setStatus("Enter the 6-digit code from Google Authenticator.", "text-amber");
        otpEl?.focus();
        return;
    }

    const btn = document.getElementById('set-save');
    if (btn) { btn.disabled = true; btn.textContent = "Saving..."; }
    setStatus("Saving...");
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ changes, otp }),
        });
        const d = await res.json();
        if (d.ok) {
            setStatus(d.message || "Saved.", "text-green");
            if (otpEl) otpEl.value = "";     // codes are single-use in practice
            await fetchSettings();           // re-read so "saved" reflects .env
        } else {
            setStatus(d.error || "Save failed.", "text-red");
        }
    } catch (err) {
        setStatus("Network error: " + err.message, "text-red");
    } finally {
        if (btn) btn.disabled = false;
        markDirty();
    }
}

document.getElementById('set-save')?.addEventListener('click', saveSettings);
document.getElementById('set-reset')?.addEventListener('click', () => {
    fetchSettings();
    setStatus("Changes discarded.");
});


// ─── Kronos / whale shadow-experiment progress ───────────────────────────────
//
// Both experiments are observe-only and only deliver a verdict once enough
// executed CSM trades can be matched to their shadow records (Kronos: 80,
// whale: 120). The panel shows how close each is, plus worker health.

let KP_TIMER = null;

function kpFmtTs(s) {
    if (!s) return "—";
    const d = new Date(s);
    return isNaN(d) ? String(s) : d.toLocaleString();
}

function kpFill(prefix, b) {
    const set = (id, v) => { const el = document.getElementById(`kp-${prefix}-${id}`); if (el) el.textContent = v; };
    const fill = document.getElementById(`kp-${prefix}-fill`);
    if (fill) {
        fill.style.width = `${b.pct}%`;
        fill.classList.toggle("done", b.state === "READY");
    }
    const st = document.getElementById(`kp-${prefix}-state`);
    if (st) {
        st.textContent = b.state === "READY" ? "READY — run verdict" : "COLLECTING";
        st.className = "pill " + (b.state === "READY" ? "pill-green" : "pill-amber");
    }
    set("matched", b.matched); set("target", b.target); set("pct", b.pct);
    set("remaining", b.remaining); set("scored", b.scored); set("err", b.errors);
    set("backlog", b.backlog); set("last", kpFmtTs(b.last_activity));
    if (b.would_gate !== undefined) { set("gate", b.would_gate); set("gaterate", b.gate_rate_pct); }
}

async function fetchKronosProgress() {
    const upd = document.getElementById("kp-updated");
    try {
        const r = await fetch("/api/kronos/progress");
        const d = await r.json();
        if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
        kpFill("k", d.kronos);
        kpFill("w", d.whale);
        const g = document.getElementById("kp-gate");
        if (g && d.gate) {
            const m = d.gate.mode || "unknown";
            g.textContent = m === "live" ? `LIVE · thr ${d.gate.threshold}`
                          : m === "shadow" ? `SHADOW · thr ${d.gate.threshold}` : m.toUpperCase();
            g.className = "pill " + (m === "live" ? "pill-green" : "pill-amber");
        }
        const q = document.getElementById("kp-q"); if (q) q.textContent = d.queue.candidates;
        const t = document.getElementById("kp-trades"); if (t) t.textContent = d.csm_trades_with_outcome;
        if (upd) upd.textContent = `updated ${kpFmtTs(d.generated)}`;
    } catch (err) {
        if (upd) upd.textContent = `Could not load: ${err.message}`;
    }
}

document.getElementById("kp-toggle")?.addEventListener("click", () => {
    const panel = document.getElementById("kronos-panel");
    if (!panel) return;
    const open = panel.style.display === "none";
    panel.style.display = open ? "" : "none";
    if (open) {
        fetchKronosProgress();
        KP_TIMER = setInterval(fetchKronosProgress, 60000);
    } else if (KP_TIMER) {
        clearInterval(KP_TIMER); KP_TIMER = null;
    }
});
document.getElementById("kp-refresh")?.addEventListener("click", fetchKronosProgress);
