"""Main bot orchestrator - ties all components together."""

import asyncio
import logging
import signal
import time

from .binance_feed import BinanceFeed, PriceUpdate
from .config import BotConfig
from .dashboard import Dashboard
from .kelly import KellySizer
from .polymarket_client import PolymarketClient
from .risk_manager import RiskManager
from .strategy import ArbitrageStrategy
from .telegram_alerts import TelegramAlerts
from .trade_logger import TradeLogger, TradeRecord

logger = logging.getLogger(__name__)


class PolyTracker:
    """
    Upside - Polytracker: Polymarket latency arbitrage bot.

    Monitors BTC/ETH short-duration contracts on Polymarket,
    compares odds to real-time Binance prices, and executes
    trades when a significant edge is detected.
    """

    def __init__(self, config: BotConfig, initial_portfolio: float = 500.0):
        self.config = config
        self.initial_portfolio = initial_portfolio

        # Core components
        self.binance = BinanceFeed(config)
        self.polymarket = PolymarketClient(config)
        self.kelly = KellySizer(config.trading)
        self.risk = RiskManager(config.trading, initial_portfolio)
        self.trade_logger = TradeLogger(config.db_path)
        self.telegram = TelegramAlerts(config)
        self.strategy = ArbitrageStrategy(
            config, self.binance, self.polymarket, self.kelly, initial_portfolio
        )
        self.dashboard = Dashboard(
            self.risk,
            self.trade_logger,
            config.dashboard_refresh,
            is_paper=not config.trading.is_live,
        )

        # State
        self._running = False
        self._scan_interval = 5.0  # Seconds between strategy scans
        self._snapshot_interval = 60.0  # Seconds between portfolio snapshots
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        """Initialize and start all bot components."""
        logger.info("=" * 60)
        logger.info("  UPSIDE - POLYTRACKER STARTING")
        logger.info(
            "  Mode: %s", "PAPER" if not self.config.trading.is_live else "LIVE"
        )
        logger.info("  Initial Portfolio: $%.2f", self.initial_portfolio)
        logger.info("  Assets: %s", ", ".join(self.config.assets))
        logger.info("  Timeframes: %s", ", ".join(self.config.timeframes))
        logger.info("  Min Edge: %.1f%%", self.config.trading.min_edge_pct)
        logger.info(
            "  Kelly Fraction: %.1f%%",
            self.config.trading.kelly_fraction * 100,
        )
        logger.info(
            "  Daily Drawdown Limit: -%.1f%%",
            self.config.trading.daily_drawdown_limit_pct,
        )
        logger.info("=" * 60)

        # Initialize components
        self.trade_logger.initialize()
        await self.polymarket.initialize()
        await self.telegram.start()

        # Register risk alert handler
        self.risk.on_alert(
            lambda level, msg: asyncio.create_task(
                self.telegram.alert_drawdown(level, msg)
            )
        )

        # Register price update handler for dashboard
        self.binance.on_price_update(self._on_price_update)

        # Set up signal handlers for graceful shutdown
        self._running = True
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Launch concurrent tasks
        self._tasks = [
            asyncio.create_task(self.binance.start(), name="binance_feed"),
            asyncio.create_task(self._trading_loop(), name="trading_loop"),
            asyncio.create_task(self._snapshot_loop(), name="snapshot_loop"),
            asyncio.create_task(self.dashboard.run(), name="dashboard"),
        ]

        logger.info("All components started - bot is running")

        # Wait for all tasks
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Gracefully shut down all components."""
        if not self._running:
            return

        logger.info("Shutting down Polytracker...")
        self._running = False
        self.dashboard.stop()

        # Cancel open orders if live
        if self.config.trading.is_live:
            await self.polymarket.cancel_all_orders()

        # Stop components
        await self.binance.stop()
        await self.telegram.stop()
        self.trade_logger.close()

        # Cancel tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        logger.info("Polytracker stopped")

    async def _on_price_update(self, update: PriceUpdate):
        """Handle a new price update from Binance."""
        self.dashboard.update_prices(
            {k: v.price for k, v in self.binance.prices.items()}
        )

    async def _trading_loop(self):
        """Main trading loop - scan for signals and execute trades."""
        # Wait for Binance prices to populate
        logger.info("Waiting for Binance price data...")
        while self._running and not self.binance.prices:
            await asyncio.sleep(1)

        logger.info("Price data received - starting trading loop")

        while self._running:
            try:
                # Check if trading is allowed
                can_trade, reason = self.risk.can_trade()
                if not can_trade:
                    logger.debug("Trading paused: %s", reason)
                    await asyncio.sleep(self._scan_interval)
                    continue

                # Scan for arbitrage signals
                signals = await self.strategy.scan_for_signals()

                for sig in signals:
                    if not self._running:
                        break

                    # Pre-trade risk check
                    ok, check_reason = self.risk.pre_trade_check(
                        sig.position_size
                    )
                    if not ok:
                        logger.debug("Trade rejected: %s", check_reason)
                        continue

                    # Execute trade
                    await self._execute_signal(sig)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Trading loop error: %s", e, exc_info=True)
                await self.telegram.alert_error(str(e))

            await asyncio.sleep(self._scan_interval)

    async def _execute_signal(self, sig):
        """Execute a single trading signal."""
        order = self.strategy.create_order(sig)
        result = await self.polymarket.place_order(order)

        if result.success:
            # Log the trade
            is_paper = not self.config.trading.is_live
            record = TradeRecord(
                trade_id=result.order_id,
                timestamp=time.time(),
                asset=sig.contract.asset,
                timeframe=sig.contract.timeframe,
                direction=sig.contract.direction,
                side=sig.side,
                entry_price=result.filled_price,
                size_usdc=result.filled_size,
                edge_pct=sig.edge_pct,
                confidence=sig.confidence,
                cex_price=sig.cex_price,
                polymarket_price=sig.polymarket_price,
                is_paper=is_paper,
            )
            self.trade_logger.log_trade(record)

            # Update risk manager (paper P&L simulation)
            # In paper mode, simulate P&L based on edge
            if is_paper:
                simulated_pnl = self._simulate_paper_pnl(sig)
                self.risk.record_trade(simulated_pnl)
                self.strategy.update_portfolio_value(
                    self.risk.state.current_portfolio_value
                )
                self.trade_logger.update_trade(
                    result.order_id, simulated_pnl, sig.polymarket_price
                )
            else:
                self.risk.record_trade(0)  # Actual P&L tracked on close

            # Send Telegram alert
            await self.telegram.alert_trade(
                asset=sig.contract.asset,
                timeframe=sig.contract.timeframe,
                direction=sig.contract.direction,
                side=sig.side,
                size=result.filled_size,
                price=result.filled_price,
                edge_pct=sig.edge_pct,
                confidence=sig.confidence,
                cex_price=sig.cex_price,
                is_paper=is_paper,
            )

    def _simulate_paper_pnl(self, sig) -> float:
        """
        Simulate P&L for paper trades based on edge.

        Uses the edge percentage to probabilistically determine
        win/loss, with the expected value matching the calculated edge.
        """
        import random

        # Win probability is the CEX-implied probability
        win_prob = sig.cex_implied_prob
        won = random.random() < win_prob

        if won:
            # Profit = size * (1/price - 1) for YES bets
            if sig.side == "YES":
                payout = sig.position_size / sig.polymarket_price
                pnl = payout - sig.position_size
            else:
                payout = sig.position_size / (1 - sig.polymarket_price)
                pnl = payout - sig.position_size
        else:
            pnl = -sig.position_size

        return round(pnl, 2)

    async def _snapshot_loop(self):
        """Periodically log portfolio snapshots."""
        while self._running:
            try:
                self.trade_logger.log_portfolio_snapshot(
                    portfolio_value=self.risk.state.current_portfolio_value,
                    daily_pnl=self.risk.state.daily_pnl,
                    total_pnl=self.risk.state.total_pnl,
                    open_positions=self.risk.state.open_position_count,
                    win_rate=self.risk.win_rate,
                )
            except Exception as e:
                logger.debug("Snapshot error: %s", e)

            await asyncio.sleep(self._snapshot_interval)
