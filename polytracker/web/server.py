"""FastAPI web server for the Polytracker dashboard."""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .state import store

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


def create_app() -> FastAPI:
    """Create and configure the FastAPI app."""
    app = FastAPI(
        title="Upside - Polytracker",
        description="Polymarket Latency Arbitrage Dashboard",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )

    app.mount(
        "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
    )

    @app.get("/", response_class=HTMLResponse)
    async def root():
        template = TEMPLATES_DIR / "dashboard.html"
        return HTMLResponse(template.read_text())

    @app.get("/api/state")
    async def get_state():
        return store.snapshot()

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "bot_status": store.state.bot_status}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        queue = await store.subscribe()
        logger.debug("WebSocket client connected")

        async def send_loop():
            try:
                while True:
                    snap = await queue.get()
                    await ws.send_text(json.dumps(snap))
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug("WebSocket send error: %s", e)

        async def keepalive_loop():
            try:
                while True:
                    await asyncio.sleep(15)
                    await ws.send_text(json.dumps({"_ping": True}))
            except Exception:
                pass

        try:
            await asyncio.gather(
                send_loop(), keepalive_loop(), return_exceptions=True
            )
        except WebSocketDisconnect:
            pass
        finally:
            await store.unsubscribe(queue)
            logger.debug("WebSocket client disconnected")

    return app


async def run_server(host: str = "127.0.0.1", port: int = 8787):
    """Run the Uvicorn server for the dashboard."""
    import uvicorn

    app = create_app()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("Dashboard available at http://%s:%d", host, port)
    await server.serve()
