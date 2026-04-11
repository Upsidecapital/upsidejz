"""
Upside - Polytracker Desktop Launcher

Launches the Polytracker bot + dashboard as a native desktop application.
The dashboard appears in a standalone OS window (not a browser tab) via
pywebview, so clients see a real application, not a Python script.

Falls back to auto-opening the user's default browser if pywebview is
not installed.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
import time
from pathlib import Path

# Allow running from project root without install
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

from polytracker.bot import PolyTracker
from polytracker.config import BotConfig
from polytracker.main import parse_args, setup_logging

# Optional: pywebview for native window wrapper
try:
    import webview  # type: ignore

    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False


logger = logging.getLogger("polytracker.launcher")


class BotRunner:
    """Runs the PolyTracker bot in a background asyncio loop."""

    def __init__(self, config: BotConfig, portfolio: float, host: str, port: int):
        self.config = config
        self.portfolio = portfolio
        self.host = host
        self.port = port
        self.bot: PolyTracker | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the bot in a background thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bot-thread"
        )
        self._thread.start()

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.bot = PolyTracker(
                self.config,
                initial_portfolio=self.portfolio,
                web_host=self.host,
                web_port=self.port,
            )
            self.loop.run_until_complete(self.bot.start())
        except Exception as e:
            logger.error("Bot runner error: %s", e, exc_info=True)

    def stop(self):
        """Request bot shutdown from any thread."""
        if self.bot and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.bot.stop(), self.loop)
            # Give the bot a moment to clean up
            time.sleep(1.5)


def wait_for_server(url: str, timeout: float = 20.0) -> bool:
    """Poll the server until it responds or timeout expires."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            pass
        time.sleep(0.4)
    return False


def launch_native_window(url: str, runner: BotRunner):
    """Open the dashboard in a native OS window via pywebview."""
    logger.info("Opening native desktop window at %s", url)

    window = webview.create_window(
        "Upside - Polytracker",
        url,
        width=1600,
        height=1000,
        min_size=(1100, 700),
        resizable=True,
        text_select=True,
        background_color="#0a0b0d",
    )

    def on_closed():
        logger.info("Window closed - stopping bot")
        runner.stop()

    window.events.closed += on_closed

    # webview.start() blocks until the window is closed
    try:
        webview.start()
    except Exception as e:
        logger.error("pywebview error: %s - falling back to browser", e)
        open_in_browser(url)


def open_in_browser(url: str):
    """Fallback: open dashboard in the default browser and wait for Ctrl+C."""
    import webbrowser

    logger.info("Opening dashboard in your default browser: %s", url)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("  UPSIDE - POLYTRACKER is running")
    print(f"  Dashboard: {url}")
    print("  Press Ctrl+C in this window to stop.")
    print("=" * 60 + "\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")


def main():
    # Load environment variables
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)

    args = parse_args()
    setup_logging(args.verbose)

    # Build config
    config = BotConfig.load()
    config.trading.paper_mode = not args.live
    config.trading.live_flag_1 = args.live
    config.trading.live_flag_2 = args.confirm_live
    config.trading.live_flag_3 = args.accept_risk

    if args.min_edge is not None:
        config.trading.min_edge_pct = args.min_edge
    if args.max_position is not None:
        config.trading.max_position_pct = args.max_position
    if args.kelly_fraction is not None:
        config.trading.kelly_fraction = args.kelly_fraction

    # Live mode safety check
    if config.trading.is_live:
        logger.warning("=" * 60)
        logger.warning("  LIVE TRADING MODE - REAL FUNDS WILL BE USED")
        logger.warning("=" * 60)
        if not config.api.polymarket_private_key:
            logger.error("LIVE mode requires POLYMARKET_PRIVATE_KEY in .env")
            sys.exit(1)
    else:
        logger.info("PAPER TRADING mode (safe - no real funds)")

    url = f"http://{args.host}:{args.port}"

    # Start bot in background
    runner = BotRunner(config, args.portfolio, args.host, args.port)
    runner.start()

    # Wait for server to be ready
    logger.info("Waiting for dashboard server to start...")
    if not wait_for_server(url):
        logger.error("Dashboard server failed to start within 20s")
        runner.stop()
        sys.exit(1)

    logger.info("Dashboard ready!")

    # Launch native window or browser fallback
    if HAS_WEBVIEW and not args.no_browser:
        try:
            launch_native_window(url, runner)
        except Exception as e:
            logger.error("Native window failed: %s - using browser", e)
            open_in_browser(url)
    elif not args.no_browser:
        logger.info("pywebview not installed - using browser fallback")
        logger.info("(Install pywebview for a native desktop window)")
        open_in_browser(url)
    else:
        # --no-browser: just run headless, block on the bot thread
        print("\n" + "=" * 60)
        print("  UPSIDE - POLYTRACKER is running (headless)")
        print(f"  Dashboard: {url}")
        print("  Press Ctrl+C to stop.")
        print("=" * 60 + "\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    # Clean shutdown
    runner.stop()
    logger.info("Goodbye.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(0)
