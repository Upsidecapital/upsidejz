"""
Entry point — starts the XBot web dashboard and scheduler.
Usage: python run.py
"""

import logging
import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    print(f"\n  XBot Geopolitical News Bot")
    print(f"  Dashboard: http://localhost:{port}")
    print(f"  Posting 10 tweets/day on schedule (Eastern Time)\n")

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
