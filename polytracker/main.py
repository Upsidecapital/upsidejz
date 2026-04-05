"""Entry point for Upside - Polytracker."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .bot import PolyTracker
from .config import BotConfig


def setup_logging(verbose: bool = False):
    """Configure logging to file and console."""
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_dir / "polytracker.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Reduce noise from third-party libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upside - Polytracker: Polymarket Latency Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run in paper trading mode (default)
  python -m polytracker

  # Run with verbose logging
  python -m polytracker --verbose

  # Run in LIVE mode (requires all three flags)
  python -m polytracker --live --confirm-live --accept-risk

  # Set initial portfolio value
  python -m polytracker --portfolio 1000
        """,
    )

    parser.add_argument(
        "--portfolio",
        type=float,
        default=500.0,
        help="Initial portfolio value in USDC (default: 500)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    # Three explicit flags required for live trading
    live_group = parser.add_argument_group(
        "Live Trading",
        "ALL THREE flags are required to enable live trading. "
        "Defaults to paper trading mode.",
    )
    live_group.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (flag 1 of 3)",
    )
    live_group.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm live trading (flag 2 of 3)",
    )
    live_group.add_argument(
        "--accept-risk",
        action="store_true",
        help="Accept trading risks (flag 3 of 3)",
    )

    # Strategy overrides
    strat_group = parser.add_argument_group("Strategy Parameters")
    strat_group.add_argument(
        "--min-edge",
        type=float,
        default=None,
        help="Minimum edge %% to execute (default: 5.0)",
    )
    strat_group.add_argument(
        "--max-position",
        type=float,
        default=None,
        help="Max position size as %% of portfolio (default: 8.0)",
    )
    strat_group.add_argument(
        "--kelly-fraction",
        type=float,
        default=None,
        help="Kelly fraction multiplier (default: 0.5 = half-Kelly)",
    )

    return parser.parse_args()


def main():
    # Load environment variables from .env
    env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(env_path)

    args = parse_args()
    setup_logging(args.verbose)

    logger = logging.getLogger(__name__)

    # Load config
    config = BotConfig.load()

    # Apply live trading flags
    config.trading.paper_mode = not args.live
    config.trading.live_flag_1 = args.live
    config.trading.live_flag_2 = args.confirm_live
    config.trading.live_flag_3 = args.accept_risk

    # Apply strategy overrides
    if args.min_edge is not None:
        config.trading.min_edge_pct = args.min_edge
    if args.max_position is not None:
        config.trading.max_position_pct = args.max_position
    if args.kelly_fraction is not None:
        config.trading.kelly_fraction = args.kelly_fraction

    # Safety check for live mode
    if config.trading.is_live:
        logger.warning("=" * 60)
        logger.warning("  LIVE TRADING MODE ENABLED")
        logger.warning("  Real funds will be used for trades!")
        logger.warning("  Portfolio: $%.2f USDC", args.portfolio)
        logger.warning("=" * 60)

        if not config.api.polymarket_private_key:
            logger.error(
                "Cannot run in live mode without POLYMARKET_PRIVATE_KEY"
            )
            sys.exit(1)
    else:
        logger.info("Running in PAPER TRADING mode")
        logger.info(
            "Use --live --confirm-live --accept-risk to enable live trading"
        )

    # Create and run bot
    bot = PolyTracker(config, initial_portfolio=args.portfolio)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
