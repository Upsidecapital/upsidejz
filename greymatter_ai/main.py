"""
GreymatterAI — NAS100 Autonomous Prop Desk
FastAPI entrypoint: starts scheduler, initialises DB, exposes REST + WebSocket + dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import os

from database import init_db
from scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------
class _WSManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, data: dict) -> None:
        dead = set()
        payload = json.dumps(data, default=str)
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)


ws_manager = _WSManager()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GreymatterAI starting…")
    await init_db()
    await start_scheduler()
    # Background broadcaster — pushes stats to all WS clients every 15 s
    task = asyncio.create_task(_broadcast_loop())
    yield
    task.cancel()
    await stop_scheduler()
    logger.info("GreymatterAI stopped.")


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(15)
        try:
            payload = await _build_stats_payload()
            await ws_manager.broadcast({"type": "stats", "data": payload})
        except Exception as exc:
            logger.debug("WS broadcast error: %s", exc)


async def _build_stats_payload() -> dict:
    from sqlalchemy import select, func
    from database import AsyncSessionLocal, Trade, TradeStatus, EquitySnapshot
    from config import ACCOUNT_SIZE_USD
    async with AsyncSessionLocal() as db:
        total = (await db.execute(
            select(func.count()).select_from(Trade)
            .where(Trade.status.in_([TradeStatus.CLOSED_WIN, TradeStatus.CLOSED_LOSS]))
        )).scalar_one()
        wins = (await db.execute(
            select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.CLOSED_WIN)
        )).scalar_one()
        total_pnl = (await db.execute(
            select(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).select_from(Trade)
        )).scalar_one()
        last_snap = (await db.execute(
            select(EquitySnapshot).order_by(EquitySnapshot.recorded_at.desc()).limit(1)
        )).scalar_one_or_none()
    win_rate = (wins / total * 100) if total else 0.0
    equity   = last_snap.equity_usd if last_snap else ACCOUNT_SIZE_USD
    return {
        "total_trades": total,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl_usd": round(float(total_pnl), 2),
        "equity_usd": equity,
        "system_active": last_snap.system_active if last_snap else True,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="GreymatterAI — NAS100 Prop Desk",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send current stats immediately on connect
        payload = await _build_stats_payload()
        await websocket.send_text(json.dumps({"type": "stats", "data": payload}, default=str))
        while True:
            await websocket.receive_text()   # keep alive; client can ping
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# API — Signals
# ---------------------------------------------------------------------------
@app.get("/api/signals")
async def list_signals(limit: int = 50):
    from sqlalchemy import select, desc
    from database import AsyncSessionLocal, Signal
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Signal).order_by(desc(Signal.created_at)).limit(limit))
        rows = result.scalars().all()
    return [
        {
            "id": r.id, "strategy": r.strategy, "direction": r.direction,
            "conviction": r.conviction, "raw_conviction": r.raw_conviction,
            "entry": r.entry_price, "sl": r.stop_loss, "tp": r.take_profit,
            "setup": r.setup, "executed": r.executed, "created_at": r.created_at,
            "notes": r.notes,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# API — Trades
# ---------------------------------------------------------------------------
@app.get("/api/trades")
async def list_trades(limit: int = 50):
    from sqlalchemy import select, desc
    from database import AsyncSessionLocal, Trade
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Trade).order_by(desc(Trade.created_at)).limit(limit))
        rows = result.scalars().all()
    return [
        {
            "id": r.id, "strategy": r.strategy, "setup": r.setup,
            "direction": r.direction, "status": r.status,
            "entry": r.entry_price, "sl": r.stop_loss, "tp": r.take_profit,
            "pnl_usd": r.pnl_usd, "pnl_r": r.pnl_r,
            "conviction": r.conviction, "opened_at": r.opened_at, "closed_at": r.closed_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# API — Equity curve
# ---------------------------------------------------------------------------
@app.get("/api/equity")
async def equity_curve(limit: int = 200):
    from sqlalchemy import select, desc
    from database import AsyncSessionLocal, EquitySnapshot
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EquitySnapshot).order_by(desc(EquitySnapshot.recorded_at)).limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
    return [
        {"ts": r.recorded_at.isoformat(), "equity": r.equity_usd, "active": r.system_active}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# API — Stats
# ---------------------------------------------------------------------------
@app.get("/api/stats")
async def stats():
    return await _build_stats_payload()


# ---------------------------------------------------------------------------
# API — Setup performance (adaptive weights)
# ---------------------------------------------------------------------------
@app.get("/api/setup-performance")
async def setup_performance():
    try:
        from outcome_tracker import outcome_tracker
        return await outcome_tracker.all_performance()
    except Exception as exc:
        logger.warning("setup-performance error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# API — System events
# ---------------------------------------------------------------------------
@app.get("/api/events")
async def list_events(limit: int = 20):
    from sqlalchemy import select, desc
    from database import AsyncSessionLocal, SystemEvent
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SystemEvent).order_by(desc(SystemEvent.event_at)).limit(limit)
        )
        rows = result.scalars().all()
    return [
        {"id": r.id, "type": r.event_type, "detail": r.detail, "ts": r.event_at}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Dashboard  (production SPA)
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GreymatterAI — NAS100 Prop Desk</title>
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2128;
  --border: #30363d; --text: #e6edf3; --muted: #8b949e;
  --gold: #f0b429; --green: #3fb950; --red: #f85149; --blue: #58a6ff;
  --purple: #bc8cff; --orange: #ffa657;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, -apple-system, sans-serif; min-height: 100vh; }

/* ── Top nav ── */
.topnav { background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 24px; height: 56px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
.topnav .brand { font-size: 1rem; font-weight: 700; color: var(--gold); display: flex; align-items: center; gap: 8px; }
.topnav .brand svg { fill: var(--gold); }
.status-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px; font-size: .78rem; font-weight: 600; letter-spacing: .04em; }
.status-chip.active  { background: #1a3a2a; color: var(--green); border: 1px solid #2d5a3d; }
.status-chip.halted  { background: #3a1a1a; color: var(--red);   border: 1px solid #5a2d2d; }
.status-chip.unknown { background: #2a2a1a; color: var(--gold);  border: 1px solid #4a4a2d; }
.status-chip .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.topnav .right { display: flex; align-items: center; gap: 12px; }
.topnav .ts { font-size: .72rem; color: var(--muted); }

/* ── Layout ── */
.page { padding: 20px 24px; max-width: 1600px; margin: 0 auto; }

/* ── Stats grid ── */
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
.stat-card .label { font-size: .70rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-bottom: 8px; }
.stat-card .value { font-size: 1.6rem; font-weight: 700; line-height: 1; }
.stat-card .sub { font-size: .72rem; color: var(--muted); margin-top: 4px; }
.positive { color: var(--green); }
.negative { color: var(--red); }
.neutral  { color: var(--gold); }
.info     { color: var(--blue); }

/* ── Two-column grid ── */
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }

/* ── Section cards ── */
.section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.section-header { padding: 14px 18px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
.section-header h2 { font-size: .85rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.section-header .badge-count { background: var(--surface2); color: var(--muted); font-size: .7rem; padding: 2px 8px; border-radius: 999px; }
.section-body { padding: 0; }

/* ── Equity chart ── */
#equityChart { height: 220px; }

/* ── Tables ── */
.tbl-wrap { overflow-x: auto; max-height: 320px; overflow-y: auto; }
table { width: 100%; border-collapse: collapse; font-size: .78rem; }
thead th { position: sticky; top: 0; background: var(--surface); padding: 10px 14px; text-align: left; font-size: .68rem; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
tbody td { padding: 9px 14px; border-bottom: 1px solid #1c2128; white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--surface2); }

/* ── Badges ── */
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: .68rem; font-weight: 600; }
.badge-win     { background: #1a3a2a; color: var(--green); }
.badge-loss    { background: #3a1a1a; color: var(--red); }
.badge-open    { background: #1a2a3a; color: var(--blue); }
.badge-pending { background: #2a2a1a; color: var(--gold); }
.badge-long    { background: #1a2a3a; color: var(--blue); }
.badge-short   { background: #3a1a2a; color: var(--purple); }

/* ── Conviction bar ── */
.conv-bar { display: flex; align-items: center; gap: 6px; }
.conv-bar .track { flex: 1; height: 4px; background: var(--border); border-radius: 2px; }
.conv-bar .fill  { height: 100%; border-radius: 2px; }

/* ── Setup performance ── */
.mult-high { color: var(--green); font-weight: 700; }
.mult-low  { color: var(--red);   font-weight: 700; }
.mult-mid  { color: var(--muted); }
.wr-high { color: var(--green); }
.wr-low  { color: var(--red); }
.wr-mid  { color: var(--gold); }

/* ── Events log ── */
.event-row { display: flex; align-items: flex-start; gap: 10px; padding: 10px 18px; border-bottom: 1px solid #1c2128; font-size: .78rem; }
.event-row:last-child { border-bottom: none; }
.event-type { font-weight: 600; min-width: 110px; }
.event-type.kill_switch { color: var(--red); }
.event-type.restart     { color: var(--blue); }
.event-type.optimise    { color: var(--purple); }
.event-detail { color: var(--muted); flex: 1; }
.event-ts     { color: var(--muted); font-size: .68rem; white-space: nowrap; }

/* ── Full-width section ── */
.full-width { margin-bottom: 20px; }
</style>
</head>
<body>

<!-- ── Top navigation ───────────────────────────────────────────────── -->
<nav class="topnav">
  <div class="brand">
    <svg width="18" height="18" viewBox="0 0 24 24"><path d="M3 3h18v18H3z" opacity=".3"/><path d="M7 17l5-10 5 10"/><path d="M8.5 14h7"/></svg>
    GreymatterAI — NAS100 Prop Desk
  </div>
  <div class="right">
    <span id="lastUpdate" class="ts">—</span>
    <span id="sysChip" class="status-chip unknown"><span class="dot"></span><span id="sysLabel">LOADING</span></span>
  </div>
</nav>

<!-- ── Main page ────────────────────────────────────────────────────── -->
<div class="page">

  <!-- Stats cards -->
  <div class="stats-grid" id="statsGrid">
    <div class="stat-card"><div class="label">Equity</div><div class="value neutral" id="sEquity">—</div><div class="sub">Account balance</div></div>
    <div class="stat-card"><div class="label">Total P&amp;L</div><div class="value" id="sPnl">—</div><div class="sub">All time</div></div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value" id="sWin">—</div><div class="sub" id="sTradesSub">—</div></div>
    <div class="stat-card"><div class="label">Total Trades</div><div class="value neutral" id="sTrades">—</div><div class="sub">Closed</div></div>
    <div class="stat-card"><div class="label">System</div><div class="value" id="sActive">—</div><div class="sub">Engine status</div></div>
  </div>

  <!-- Equity chart + Signals feed -->
  <div class="two-col">
    <div class="section full-width">
      <div class="section-header"><h2>Equity Curve</h2></div>
      <div id="equityChart"></div>
    </div>
    <div class="section">
      <div class="section-header"><h2>Recent Signals</h2><span class="badge-count" id="signalCount">0</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Strategy</th><th>Setup</th><th>Dir</th><th>Conviction</th><th>Time</th></tr></thead>
          <tbody id="signalsTbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Setup performance table (adaptive weights) -->
  <div class="section full-width" style="margin-bottom:20px">
    <div class="section-header">
      <h2>Setup Performance &amp; Adaptive Weights</h2>
      <span class="badge-count" id="setupCount">0</span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>Strategy</th><th>Setup</th><th>Trades</th><th>EWMA Win Rate</th><th>Avg R</th><th>Multiplier</th><th>Effect</th><th>Last Updated</th></tr></thead>
        <tbody id="setupTbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Trades + Events -->
  <div class="two-col">
    <div class="section">
      <div class="section-header"><h2>Recent Trades</h2><span class="badge-count" id="tradeCount">0</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>ID</th><th>Strategy</th><th>Setup</th><th>Dir</th><th>Entry</th><th>P&amp;L $</th><th>R</th><th>Conv</th><th>Status</th></tr></thead>
          <tbody id="tradesTbody"></tbody>
        </table>
      </div>
    </div>
    <div class="section">
      <div class="section-header"><h2>System Events</h2></div>
      <div id="eventsList"></div>
    </div>
  </div>

</div><!-- /.page -->

<script>
// ── Utilities ─────────────────────────────────────────────────────────
const fmt    = (n, d=2) => n == null ? '—' : Number(n).toFixed(d);
const fmtUSD = n => n == null ? '—' : (n >= 0 ? '+' : '') + '$' + Math.abs(n).toLocaleString('en-US', {maximumFractionDigits: 0});
const fmtTs  = ts => ts ? new Date(ts).toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'}) : '—';
const fmtDt  = ts => ts ? new Date(ts).toLocaleDateString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const clamp  = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ── Stats ─────────────────────────────────────────────────────────────
function applyStats(s) {
  document.getElementById('sEquity').textContent = '$' + Number(s.equity_usd).toLocaleString('en-US', {maximumFractionDigits: 0});
  const pnlEl = document.getElementById('sPnl');
  pnlEl.textContent   = fmtUSD(s.total_pnl_usd);
  pnlEl.className     = 'value ' + (s.total_pnl_usd >= 0 ? 'positive' : 'negative');
  document.getElementById('sWin').textContent       = fmt(s.win_rate_pct, 1) + '%';
  document.getElementById('sTrades').textContent    = s.total_trades;
  document.getElementById('sTradesSub').textContent = s.total_trades + ' closed';
  const aEl = document.getElementById('sActive');
  aEl.textContent  = s.system_active ? 'ACTIVE' : 'HALTED';
  aEl.className    = 'value ' + (s.system_active ? 'positive' : 'negative');
  const chip = document.getElementById('sysChip');
  chip.className   = 'status-chip ' + (s.system_active ? 'active' : 'halted');
  document.getElementById('sysLabel').textContent = s.system_active ? 'ACTIVE' : 'HALTED';
  if (s.ts) document.getElementById('lastUpdate').textContent = 'Updated ' + fmtTs(s.ts);
}

// ── Equity chart ──────────────────────────────────────────────────────
let _chart, _series;
async function initEquityChart() {
  const rows = await fetch('/api/equity').then(r => r.json()).catch(() => []);
  const el   = document.getElementById('equityChart');
  _chart = LightweightCharts.createChart(el, {
    layout:  { background: { color: '#161b22' }, textColor: '#8b949e' },
    grid:    { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    timeScale: { borderColor: '#30363d', timeVisible: true },
    rightPriceScale: { borderColor: '#30363d' },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  _series = _chart.addAreaSeries({
    lineColor: '#f0b429', topColor: 'rgba(240,180,41,.25)',
    bottomColor: 'rgba(240,180,41,0)', lineWidth: 2,
  });
  const data = rows.map(r => ({ time: Math.floor(new Date(r.ts).getTime() / 1000), value: r.equity }))
                   .filter(r => r.value > 0);
  if (data.length) { _series.setData(data); _chart.timeScale().fitContent(); }
}

// ── Signals ───────────────────────────────────────────────────────────
async function loadSignals() {
  const rows = await fetch('/api/signals?limit=15').then(r => r.json()).catch(() => []);
  document.getElementById('signalCount').textContent = rows.length;
  const tb = document.getElementById('signalsTbody');
  tb.innerHTML = rows.map(s => {
    const pct   = clamp(s.conviction || 0, 0, 100);
    const color = pct >= 70 ? '#3fb950' : pct >= 50 ? '#f0b429' : '#8b949e';
    return `<tr>
      <td>${s.strategy || '—'}</td>
      <td style="color:#8b949e;font-size:.72rem">${s.setup || '—'}</td>
      <td><span class="badge badge-${s.direction}">${(s.direction||'').toUpperCase()}</span></td>
      <td><div class="conv-bar">
        <div class="track"><div class="fill" style="width:${pct}%;background:${color}"></div></div>
        <span style="font-size:.72rem;color:${color};min-width:28px">${fmt(pct,0)}</span>
      </div></td>
      <td style="color:#8b949e">${fmtTs(s.created_at)}</td>
    </tr>`;
  }).join('');
}

// ── Setup performance ─────────────────────────────────────────────────
async function loadSetupPerformance() {
  const rows = await fetch('/api/setup-performance').then(r => r.json()).catch(() => []);
  document.getElementById('setupCount').textContent = rows.length;
  const tb = document.getElementById('setupTbody');
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="8" style="padding:20px;text-align:center;color:#8b949e">No setup history yet — performance data accumulates as trades close.</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const wr  = r.win_rate_pct;
    const m   = r.multiplier;
    const wrC = wr >= 60 ? 'wr-high' : wr <= 40 ? 'wr-low' : 'wr-mid';
    const mC  = m >= 1.1  ? 'mult-high' : m <= 0.9 ? 'mult-low' : 'mult-mid';
    const effect = m >= 1.1 ? '↑ Boosted' : m <= 0.9 ? '↓ Penalised' : '→ Neutral';
    const effC   = m >= 1.1 ? 'positive' : m <= 0.9 ? 'negative' : '';
    return `<tr>
      <td>${r.strategy}</td>
      <td style="color:#8b949e">${r.setup}</td>
      <td>${r.trades_total}</td>
      <td class="${wrC}">${wr}%</td>
      <td class="${r.avg_pnl_r >= 0 ? 'positive':'negative'}">${fmt(r.avg_pnl_r)}R</td>
      <td class="${mC}">${m}×</td>
      <td class="${effC}">${effect}</td>
      <td style="color:#8b949e;font-size:.7rem">${fmtTs(r.last_updated)}</td>
    </tr>`;
  }).join('');
}

// ── Trades ────────────────────────────────────────────────────────────
async function loadTrades() {
  const rows = await fetch('/api/trades?limit=25').then(r => r.json()).catch(() => []);
  document.getElementById('tradeCount').textContent = rows.length;
  const tb = document.getElementById('tradesTbody');
  tb.innerHTML = rows.map(t => {
    const stCls = { closed_win:'win', closed_loss:'loss', open:'open', pending:'pending' }[t.status] || '';
    const pnlC  = (t.pnl_usd || 0) >= 0 ? 'positive' : 'negative';
    const pnlRc = (t.pnl_r   || 0) >= 0 ? 'positive' : 'negative';
    return `<tr>
      <td style="color:#8b949e">#${t.id}</td>
      <td>${t.strategy}</td>
      <td style="color:#8b949e;font-size:.72rem">${t.setup || '—'}</td>
      <td><span class="badge badge-${t.direction}">${(t.direction||'').toUpperCase()}</span></td>
      <td>${fmt(t.entry, 1)}</td>
      <td class="${pnlC}">${fmtUSD(t.pnl_usd)}</td>
      <td class="${pnlRc}">${t.pnl_r != null ? fmt(t.pnl_r, 2)+'R' : '—'}</td>
      <td style="color:#8b949e">${fmt(t.conviction, 0)}</td>
      <td><span class="badge badge-${stCls}">${t.status}</span></td>
    </tr>`;
  }).join('');
}

// ── Events ────────────────────────────────────────────────────────────
async function loadEvents() {
  const rows = await fetch('/api/events?limit=8').then(r => r.json()).catch(() => []);
  const el = document.getElementById('eventsList');
  if (!rows.length) { el.innerHTML = '<div class="event-row"><span style="color:#8b949e">No system events yet.</span></div>'; return; }
  el.innerHTML = rows.map(e => `
    <div class="event-row">
      <span class="event-type ${(e.type||'').toLowerCase()}">${e.type || 'EVENT'}</span>
      <span class="event-detail">${e.detail || '—'}</span>
      <span class="event-ts">${fmtTs(e.ts)}</span>
    </div>`).join('');
}

// ── WebSocket for real-time stats ─────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'stats') applyStats(msg.data);
  };
  ws.onclose = () => setTimeout(connectWS, 5000);  // auto-reconnect
  ws.onerror = () => ws.close();
  // ping every 30s to keep alive
  setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 30000);
}

// ── Boot ──────────────────────────────────────────────────────────────
(async () => {
  // Initial data loads
  const [statsData] = await Promise.all([
    fetch('/api/stats').then(r => r.json()).catch(() => ({})),
    initEquityChart(),
    loadSignals(),
    loadSetupPerformance(),
    loadTrades(),
    loadEvents(),
  ]);
  if (statsData && statsData.equity_usd) applyStats(statsData);

  // WebSocket for real-time stats updates
  connectWS();

  // Periodic refresh of everything except stats (handled by WS)
  setInterval(() => Promise.all([loadSignals(), loadTrades(), loadSetupPerformance(), loadEvents()]), 30000);
})();
</script>
</body>
</html>
"""
