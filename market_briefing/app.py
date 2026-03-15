"""
Market Briefing Bot — FastAPI server
  • Serves the web UI
  • Schedules weekday 08:00 MYT briefings via APScheduler
  • Streams live generation over Server-Sent Events (SSE)
  • Stores every briefing in SQLite

Usage:
    python app.py          # or:  uvicorn app:app --host 0.0.0.0 --port 8080
"""

import asyncio
import json
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

from briefing import generate_briefing_stream
from database import BriefingDB

load_dotenv()

MYT = ZoneInfo("Asia/Kuala_Lumpur")
db = BriefingDB()
scheduler = AsyncIOScheduler(timezone=MYT)

# ── In-memory generation state ─────────────────────────────────────────────────

class GenerationState:
    """Buffers all events from an active generation so multiple SSE
    subscribers (e.g. multiple browser tabs) can read from the start."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.complete: bool = False
        self._signal = asyncio.Event()

    def push(self, event: dict) -> None:
        self.events.append(event)
        self._signal.set()

    def finish(self) -> None:
        self.complete = True
        self._signal.set()

    async def subscribe(self):
        """Async generator — yields events from the beginning of the buffer."""
        cursor = 0
        while True:
            # Yield all buffered events the subscriber hasn't seen yet
            while cursor < len(self.events):
                yield self.events[cursor]
                cursor += 1

            if self.complete and cursor >= len(self.events):
                return

            # Wait for new events (clear signal AFTER re-checking buffer
            # to avoid a race where an event arrives between the while
            # condition check and the wait call)
            self._signal.clear()
            if cursor < len(self.events):
                continue  # more arrived while we were clearing
            await self._signal.wait()


active_generations: dict[int, GenerationState] = {}

# ── Core generation runner ─────────────────────────────────────────────────────

async def run_generation(briefing_id: int, date_str: str) -> None:
    state = GenerationState()
    active_generations[briefing_id] = state
    full_content: list[str] = []

    try:
        async for event in generate_briefing_stream(date_str):
            state.push(event)
            if event["type"] == "delta":
                full_content.append(event["text"])

        db.complete_briefing(briefing_id, "".join(full_content))
        state.push({"type": "done", "briefing_id": briefing_id})

    except Exception as exc:
        err = str(exc)
        db.fail_briefing(briefing_id, err)
        state.push({"type": "error", "message": err})

    finally:
        state.finish()
        # Keep state alive briefly for late-connecting SSE clients
        await asyncio.sleep(15)
        active_generations.pop(briefing_id, None)


# ── Scheduled job ──────────────────────────────────────────────────────────────

async def scheduled_briefing() -> None:
    now = datetime.now(MYT)
    date_str = now.strftime("%A, %d %B %Y")
    briefing_id = db.create_briefing(now.strftime("%Y-%m-%d"))
    asyncio.create_task(run_generation(briefing_id, date_str))


# ── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        scheduled_briefing,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=MYT),
        id="daily_briefing",
        replace_existing=True,
        misfire_grace_time=3600,  # fire within 1 h of missed trigger
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Market Briefing Bot", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.post("/api/generate")
async def trigger_generation():
    """Manually trigger a briefing generation. Returns the new briefing_id."""
    now = datetime.now(MYT)
    date_str = now.strftime("%A, %d %B %Y")
    briefing_id = db.create_briefing(now.strftime("%Y-%m-%d"))
    asyncio.create_task(run_generation(briefing_id, date_str))
    return {"briefing_id": briefing_id}


@app.get("/api/stream/{briefing_id}")
async def stream_briefing(briefing_id: int, request: Request):
    """
    Server-Sent Events endpoint.
    • If generation is active: streams live events from the buffer.
    • If generation is complete: serves the full content in one shot.
    """
    async def event_gen():
        # ── Active generation ──────────────────────────────────────────
        if briefing_id in active_generations:
            state = active_generations[briefing_id]
            async for event in state.subscribe():
                if await request.is_disconnected():
                    return
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] in ("done", "error"):
                    return
            return

        # ── Already complete — serve from DB ───────────────────────────
        briefing = db.get_briefing(briefing_id)
        if not briefing:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Not found'})}\n\n"
            return

        if briefing["status"] == "complete":
            yield f"data: {json.dumps({'type': 'complete', 'content': briefing['content']})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': briefing.get('error', 'Unknown error')})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/briefings")
async def list_briefings():
    return db.list_briefings()


@app.get("/api/briefings/{briefing_id}")
async def get_briefing(briefing_id: int):
    briefing = db.get_briefing(briefing_id)
    if not briefing:
        raise HTTPException(status_code=404, detail="Briefing not found")
    return briefing


@app.get("/api/status")
async def status():
    """Returns scheduler next run time and active generation count."""
    job = scheduler.get_job("daily_briefing")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return {
        "scheduler_running": scheduler.running,
        "next_scheduled_run_myt": next_run,
        "active_generations": list(active_generations.keys()),
        "current_time_myt": datetime.now(MYT).isoformat(),
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
