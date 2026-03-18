"""
Forex Edge Finder — FastAPI application server.

Routes:
  GET  /                        → Dashboard HTML
  POST /api/refresh/cot         → Refresh COT data from CFTC
  POST /api/refresh/rates       → Refresh forex rates
  GET  /api/cot                 → All currency COT analysis
  GET  /api/cot/{currency}      → Single currency COT detail
  GET  /api/edges               → All currency edge scores
  GET  /api/signals             → All pair trading signals
  GET  /api/signals/top         → Top actionable signals
  GET  /api/calendar            → Economic calendar events
  GET  /api/rates               → Current forex rates
  GET  /api/strength            → Currency strength scores
  GET  /api/interest-rates      → Central bank interest rates
  PUT  /api/interest-rates/{c}  → Update a central bank rate
  GET  /api/ai/analyze/{pair}   → SSE-streamed AI analysis
  GET  /api/ai/overview         → SSE-streamed market overview
  GET  /api/status              → System status

Usage:
    python app.py
    uvicorn app:app --host 0.0.0.0 --port 8081 --reload
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_analyzer import analyze_pair_streaming, get_market_overview_stream
from cot_fetcher import get_all_cot_analysis, get_cot_analysis, refresh_cot_data
from database import (
    get_interest_rates,
    get_latest_signals,
    get_upcoming_events,
    init_db,
    update_interest_rate,
    upsert_economic_events,
)
from edge_engine import (
    compute_all_currency_edges,
    generate_all_signals,
    get_top_signals,
)
from forex_data import (
    calculate_currency_strength,
    fetch_forex_rates,
    get_default_economic_calendar,
    get_rates_usd_base,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

MYT = ZoneInfo("Asia/Kuala_Lumpur")
scheduler = AsyncIOScheduler(timezone=MYT)
STATIC_DIR = Path(__file__).parent / "static"


# ── Background refresh tasks ──────────────────────────────────────────────────

async def _refresh_all():
    """Refresh COT data + forex rates (runs on schedule)."""
    logger.info("Scheduled refresh: COT + rates")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, refresh_cot_data)
    await loop.run_in_executor(None, fetch_forex_rates)
    # Seed/refresh economic calendar
    events = get_default_economic_calendar()
    upsert_economic_events(events)
    logger.info("Scheduled refresh complete")


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise DB
    init_db()
    logger.info("Database initialised")

    # Seed economic calendar
    events = get_default_economic_calendar()
    upsert_economic_events(events)

    # Initial data fetch in background
    asyncio.create_task(_refresh_all())

    # Schedule: refresh COT every Sunday 18:00 MYT (CFTC releases Friday afternoon ET)
    # and refresh forex rates every 30 min on weekdays
    scheduler.add_job(
        _refresh_all,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=MYT),
        id="weekly_cot_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(
            asyncio.get_event_loop().run_in_executor(None, fetch_forex_rates)
        ),
        CronTrigger(day_of_week="mon-fri", minute="*/30", timezone=MYT),
        id="intraday_rate_refresh",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="Forex Edge Finder", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── HTML ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


# ── COT ───────────────────────────────────────────────────────────────────────

@app.post("/api/refresh/cot")
async def trigger_cot_refresh():
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, refresh_cot_data)
    return {"results": results, "refreshed_at": datetime.utcnow().isoformat()}


@app.post("/api/refresh/rates")
async def trigger_rates_refresh():
    loop = asyncio.get_event_loop()
    rates = await loop.run_in_executor(None, fetch_forex_rates)
    return {"currencies": len(rates), "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/cot")
async def get_cot_all():
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, get_all_cot_analysis)
    return data


@app.get("/api/cot/{currency}")
async def get_cot_currency(currency: str):
    currency = currency.upper()
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, get_cot_analysis, currency)
    return data


# ── Edge scores ───────────────────────────────────────────────────────────────

@app.get("/api/edges")
async def get_edges():
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, compute_all_currency_edges)
    return data


# ── Signals ───────────────────────────────────────────────────────────────────

@app.get("/api/signals")
async def get_signals():
    loop = asyncio.get_event_loop()
    signals = await loop.run_in_executor(None, generate_all_signals, False)
    return signals


@app.get("/api/signals/top")
async def get_top():
    loop = asyncio.get_event_loop()
    signals = await loop.run_in_executor(None, get_top_signals, 8)
    return signals


@app.get("/api/signals/history")
async def get_signal_history(limit: int = 20):
    return get_latest_signals(limit)


# ── Calendar ──────────────────────────────────────────────────────────────────

@app.get("/api/calendar")
async def get_calendar(days_ahead: int = 7):
    events = get_upcoming_events(days_ahead)
    if not events:
        events = get_default_economic_calendar()
    return events


# ── Forex rates & strength ────────────────────────────────────────────────────

@app.get("/api/rates")
async def get_rates():
    loop = asyncio.get_event_loop()
    rates = await loop.run_in_executor(None, get_rates_usd_base)
    return {"base": "USD", "rates": rates, "fetched_at": datetime.utcnow().isoformat()}


@app.get("/api/strength")
async def get_strength():
    loop = asyncio.get_event_loop()
    rates = await loop.run_in_executor(None, get_rates_usd_base)
    strength = await loop.run_in_executor(None, calculate_currency_strength, rates)
    return strength


# ── Interest rates ────────────────────────────────────────────────────────────

@app.get("/api/interest-rates")
async def get_rates_list():
    return get_interest_rates()


class RateUpdate(BaseModel):
    rate: float


@app.put("/api/interest-rates/{currency}")
async def update_rate(currency: str, body: RateUpdate):
    currency = currency.upper()
    if body.rate < 0 or body.rate > 30:
        raise HTTPException(status_code=400, detail="Rate must be between 0 and 30")
    update_interest_rate(currency, body.rate)
    return {"currency": currency, "rate": body.rate, "updated": True}


# ── AI analysis (SSE streaming) ───────────────────────────────────────────────

@app.get("/api/ai/analyze/{pair}")
async def ai_analyze(pair: str, request: Request):
    pair = pair.upper()
    if len(pair) != 6:
        raise HTTPException(status_code=400, detail="Pair must be 6 characters (e.g. EURUSD)")

    # Gather data synchronously in executor
    loop = asyncio.get_event_loop()
    signal = await loop.run_in_executor(
        None,
        lambda: __import__("edge_engine").generate_pair_signal(pair)
    )
    cot_data = await loop.run_in_executor(None, get_all_cot_analysis)
    events = get_upcoming_events(7)

    async def sse_gen():
        async for chunk in analyze_pair_streaming(pair, signal, cot_data, events):
            if await request.is_disconnected():
                return
            yield f"data: {json.dumps({'text': chunk})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/ai/overview")
async def ai_overview(request: Request):
    loop = asyncio.get_event_loop()
    edges = await loop.run_in_executor(None, compute_all_currency_edges)
    top_sigs = await loop.run_in_executor(None, get_top_signals, 5)

    async def sse_gen():
        async for chunk in get_market_overview_stream(edges, top_sigs):
            if await request.is_disconnected():
                return
            yield f"data: {json.dumps({'text': chunk})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    job_cot = scheduler.get_job("weekly_cot_refresh")
    job_rates = scheduler.get_job("intraday_rate_refresh")
    return {
        "scheduler_running": scheduler.running,
        "next_cot_refresh": job_cot.next_run_time.isoformat() if job_cot and job_cot.next_run_time else None,
        "next_rates_refresh": job_rates.next_run_time.isoformat() if job_rates and job_rates.next_run_time else None,
        "current_time_utc": datetime.utcnow().isoformat(),
        "current_time_myt": datetime.now(MYT).isoformat(),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
