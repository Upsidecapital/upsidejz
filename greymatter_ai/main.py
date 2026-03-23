"""
GreymatterAI — NAS100 Autonomous Prop Desk (Fabio Valentini ORB / IVB / Order Flow)
FastAPI entrypoint — starts scheduler, initialises DB, exposes REST + dashboard.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from database import init_db
from scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GreymatterAI starting…")
    await init_db()
    await start_scheduler()
    yield
    await stop_scheduler()
    logger.info("GreymatterAI stopped.")


app = FastAPI(
    title="GreymatterAI — NAS100 Prop Desk",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API — Signals & Trades (imported lazily to avoid circular imports)
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
            "id": r.id,
            "strategy": r.strategy,
            "direction": r.direction,
            "conviction": r.conviction,
            "entry": r.entry_price,
            "sl": r.stop_loss,
            "tp": r.take_profit,
            "executed": r.executed,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@app.get("/api/trades")
async def list_trades(limit: int = 50):
    from sqlalchemy import select, desc
    from database import AsyncSessionLocal, Trade
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Trade).order_by(desc(Trade.created_at)).limit(limit))
        rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "strategy": r.strategy,
            "direction": r.direction,
            "status": r.status,
            "entry": r.entry_price,
            "sl": r.stop_loss,
            "tp": r.take_profit,
            "pnl_usd": r.pnl_usd,
            "pnl_r": r.pnl_r,
            "conviction": r.conviction,
            "opened_at": r.opened_at,
            "closed_at": r.closed_at,
        }
        for r in rows
    ]


@app.get("/api/equity")
async def equity_curve(limit: int = 200):
    from sqlalchemy import select, desc
    from database import AsyncSessionLocal, EquitySnapshot
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EquitySnapshot).order_by(desc(EquitySnapshot.recorded_at)).limit(limit)
        )
        rows = result.scalars().all()
    rows = list(reversed(rows))
    return [
        {"ts": r.recorded_at.isoformat(), "equity": r.equity_usd, "active": r.system_active}
        for r in rows
    ]


@app.get("/api/stats")
async def stats():
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
    equity = last_snap.equity_usd if last_snap else ACCOUNT_SIZE_USD
    return {
        "total_trades": total,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl_usd": round(float(total_pnl), 2),
        "equity_usd": equity,
        "system_active": last_snap.system_active if last_snap else True,
    }


# ---------------------------------------------------------------------------
# Dashboard (TradingView Lightweight Charts)
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GreymatterAI — NAS100 Prop Desk</title>
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--gold:#f0b429;--green:#3fb950;--red:#f85149}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh;padding:24px}
  h1{font-size:1.4rem;font-weight:700;color:var(--gold);margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
  .card .label{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin-bottom:6px}
  .card .value{font-size:1.5rem;font-weight:700}
  .positive{color:var(--green)}.negative{color:var(--red)}.neutral{color:var(--gold)}
  #equityChart{background:var(--surface);border:1px solid var(--border);border-radius:8px;height:260px;margin-bottom:24px}
  table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:8px;overflow:hidden}
  th,td{padding:10px 14px;text-align:left;font-size:.82rem;border-bottom:1px solid var(--border)}
  th{color:#8b949e;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:600}
  .badge-win{background:#1a3a2a;color:var(--green)}.badge-loss{background:#3a1a1a;color:var(--red)}
  .badge-open{background:#1a2a3a;color:#58a6ff}.badge-pending{background:#2a2a1a;color:#e3b341}
</style>
</head>
<body>
<h1>GreymatterAI — NAS100 Autonomous Prop Desk</h1>

<div class="grid" id="statsGrid">
  <div class="card"><div class="label">Equity</div><div class="value neutral" id="sEquity">—</div></div>
  <div class="card"><div class="label">Total P&amp;L</div><div class="value" id="sPnl">—</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value" id="sWin">—</div></div>
  <div class="card"><div class="label">Total Trades</div><div class="value neutral" id="sTrades">—</div></div>
  <div class="card"><div class="label">System</div><div class="value" id="sActive">—</div></div>
</div>

<div id="equityChart"></div>

<h2 style="font-size:1rem;margin-bottom:12px;color:#8b949e">Recent Trades</h2>
<table>
  <thead><tr><th>Strategy</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>P&amp;L</th><th>R</th><th>Status</th></tr></thead>
  <tbody id="tradesTbody"></tbody>
</table>

<script>
const fmt = (n,d=2) => n==null?'—':Number(n).toFixed(d);
const fmtUSD = n => n==null?'—':(n>=0?'+':'')+fmt(n,0)+'$';

async function loadStats(){
  const s = await fetch('/api/stats').then(r=>r.json());
  document.getElementById('sEquity').textContent = '$'+fmt(s.equity_usd,0);
  const pnlEl = document.getElementById('sPnl');
  pnlEl.textContent = fmtUSD(s.total_pnl_usd);
  pnlEl.className = 'value '+(s.total_pnl_usd>=0?'positive':'negative');
  document.getElementById('sWin').textContent = fmt(s.win_rate_pct,1)+'%';
  document.getElementById('sTrades').textContent = s.total_trades;
  const aEl = document.getElementById('sActive');
  aEl.textContent = s.system_active?'ACTIVE':'HALTED';
  aEl.className = 'value '+(s.system_active?'positive':'negative');
}

async function loadEquityChart(){
  const rows = await fetch('/api/equity').then(r=>r.json());
  const el = document.getElementById('equityChart');
  const chart = LightweightCharts.createChart(el,{
    layout:{background:{color:'#161b22'},textColor:'#8b949e'},
    grid:{vertLines:{color:'#21262d'},horzLines:{color:'#21262d'}},
    timeScale:{borderColor:'#30363d'},
    rightPriceScale:{borderColor:'#30363d'},
  });
  const series = chart.addAreaSeries({
    lineColor:'#f0b429',topColor:'rgba(240,180,41,.3)',
    bottomColor:'rgba(240,180,41,.0)',lineWidth:2,
  });
  series.setData(rows.map(r=>({time:r.ts.slice(0,10)||r.ts,value:r.equity})));
  chart.timeScale().fitContent();
}

async function loadTrades(){
  const trades = await fetch('/api/trades?limit=30').then(r=>r.json());
  const tbody = document.getElementById('tradesTbody');
  tbody.innerHTML = trades.map(t=>{
    const statusClass = {closed_win:'win',closed_loss:'loss',open:'open',pending:'pending'}[t.status]||'';
    return `<tr>
      <td>${t.strategy}</td><td>${t.direction}</td>
      <td>${fmt(t.entry,2)}</td><td>${fmt(t.sl,2)}</td><td>${fmt(t.tp,2)}</td>
      <td class="${t.pnl_usd>=0?'positive':'negative'}">${fmtUSD(t.pnl_usd)}</td>
      <td>${fmt(t.pnl_r,2)}</td>
      <td><span class="badge badge-${statusClass}">${t.status}</span></td>
    </tr>`;
  }).join('');
}

Promise.all([loadStats(),loadEquityChart(),loadTrades()]);
setInterval(()=>Promise.all([loadStats(),loadTrades()]),60000);
</script>
</body>
</html>
"""
