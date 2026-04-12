"""Main bot orchestrator - ties all components together."""

import asyncio
import logging
import signal
import time

from .binance_feed import BinanceFeed, PriceUpdate
from .config import BotConfig
from .kelly import KellySizer
from .polymarket_client import PolymarketClient
from .polysimulator import PolySimulator
from .risk_manager import RiskManager
from .strategy import ArbitrageStrategy
from .telegram_alerts import TelegramAlerts
from .trade_logger import TradeLogger, TradeRecord
from .web.server import run_server
from .web.state import store

logger = logging.getLogger(__name__)


class PolyTracker:
    """
    Upside - Polytracker: Polymarket latency arbitrage bot.

    Monitors BTC/ETH short-duration contracts on Polymarket,
    compares odds to real-time Binance prices, and executes
    trades when a significant edge is detected.
    """

    def __init__(
        self,
        config: BotConfig,
        initial_portfolio: float = 500.0,
        web_host: str = "127.0.0.1",
        web_port: int = 8787,
    ):
        self.config = config
        self.initial_portfolio = initial_portfolio
        self.web_host = web_host
        self.web_port = web_port

        # Core components
        self.binance = BinanceFeed(config)
        # In paper mode, use the PolySimulator so we don't need real API creds
        self.simulator: PolySimulator | None = None
        if not config.trading.is_live:
            self.simulator = PolySimulator(self.binance)
            logger.info("PolySimulator enabled for paper trading")
        self.polymarket = PolymarketClient(config, simulator=self.simulator)
        self.kelly = KellySizer(config.trading)
        self.risk = RiskManager(config.trading, initial_portfolio)
        self.trade_logger = TradeLogger(config.db_path)
        self.telegram = TelegramAlerts(config)
        self.strategy = ArbitrageStrategy(
            config, self.binance, self.polymarket, self.kelly, initial_portfolio
        )

        # State
        self._running = False
        self._scan_interval = 10.0  # Seconds between strategy scans
        self._snapshot_interval = 30.0  # Seconds between portfolio snapshots
        self._state_refresh = 2.0  # Seconds between state publishes
        self._tasks: list[asyncio.Task] = []
        self._asset_stats: dict[str, dict] = {}
        self._last_prices: dict[str, float] = {}

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
        logger.info(
            "  Dashboard: http://%s:%d", self.web_host, self.web_port
        )
        logger.info("=" * 60)

        # Prime the state store with initial values
        await store.update(
            is_paper=not self.config.trading.is_live,
            initial_portfolio=self.initial_portfolio,
            portfolio_value=self.initial_portfolio,
            peak_portfolio_value=self.initial_portfolio,
            daily_drawdown_limit=self.config.trading.daily_drawdown_limit_pct,
            total_drawdown_limit=self.config.trading.total_drawdown_kill_pct,
            bot_status="INITIALIZING",
        )
        await store.record_equity_point(self.initial_portfolio)
        await store.log_activity("INFO", "Polytracker starting up")

        # Initialize components
        self.trade_logger.initialize()
        await self._safe_init_polymarket()
        await self.telegram.start()

        # Register risk alert handler
        self.risk.on_alert(self._on_risk_alert)

        # Register price update handler
        self.binance.on_price_update(self._on_price_update)

        # Set up signal handlers for graceful shutdown
        self._running = True
        try:
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig, lambda: asyncio.create_task(self.stop())
                )
        except NotImplementedError:
            pass  # Windows or restricted env

        await store.update(bot_status="RUNNING")
        await store.log_activity("INFO", "Bot status: RUNNING")

        # Launch concurrent tasks
        self._tasks = [
            asyncio.create_task(self.binance.start(), name="binance_feed"),
            asyncio.create_task(self._trading_loop(), name="trading_loop"),
            asyncio.create_task(self._resolution_loop(), name="resolution_loop"),
            asyncio.create_task(self._snapshot_loop(), name="snapshot_loop"),
            asyncio.create_task(self._state_loop(), name="state_publisher"),
            asyncio.create_task(
                run_server(self.web_host, self.web_port), name="web_server"
            ),
        ]

        logger.info("All components started - bot is running")

        # Wait for all tasks
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def _safe_init_polymarket(self):
        """Initialize Polymarket client - tolerate missing credentials in paper mode."""
        try:
            await self.polymarket.initialize()
        except Exception as e:
            if self.config.trading.is_live:
                raise
            logger.warning(
                "Polymarket init failed in paper mode (%s) - continuing", e
            )
            await store.log_activity(
                "WARN", f"Polymarket init failed: {str(e)[:80]}"
            )

    async def stop(self):
        """Gracefully shut down all components."""
        if not self._running:
            return

        logger.info("Shutting down Polytracker...")
        self._running = False
        await store.update(bot_status="STOPPING")
        await store.log_activity("INFO", "Shutting down")

        # Cancel open orders if live
        if self.config.trading.is_live:
            try:
                await self.polymarket.cancel_all_orders()
            except Exception as e:
                logger.error("Error cancelling orders: %s", e)

        # Stop components
        try:
            await self.binance.stop()
        except Exception:
            pass
        try:
            await self.telegram.stop()
        except Exception:
            pass
        self.trade_logger.close()

        await store.update(bot_status="STOPPED")

        # Cancel tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        logger.info("Polytracker stopped")

    async def _on_price_update(self, update: PriceUpdate):
        """Handle a new price update from Binance."""
        prev = self._last_prices.get(update.symbol, update.price)
        change = (
            (update.price - prev) / prev * 100 if prev > 0 else 0.0
        )
        self._last_prices[update.symbol] = update.price

        new_prices = {
            sym: round(p.price, 2)
            for sym, p in self.binance.prices.items()
        }
        new_changes = dict(store.state.price_changes or {})
        new_changes[update.symbol] = round(change, 3)

        await store.update(prices=new_prices, price_changes=new_changes)

    def _on_risk_alert(self, level: str, message: str):
        """Fan out risk alerts to Telegram and activity feed."""
        asyncio.create_task(self.telegram.alert_drawdown(level, message))
        asyncio.create_task(store.log_activity(level, message))

    async def _trading_loop(self):
        """Main trading loop - scan for signals and execute trades."""
        # Wait for Binance prices to populate
        logger.info("Waiting for Binance price data...")
        await store.log_activity("INFO", "Waiting for Binance price feed")

        wait_start = time.time()
        while self._running and not self.binance.prices:
            await asyncio.sleep(1)
            if time.time() - wait_start > 30:
                await store.log_activity(
                    "WARN", "Binance price feed still not ready"
                )
                wait_start = time.time()

        if self._running:
            logger.info("Price data received - starting trading loop")
            await store.log_activity("INFO", "Price feed live - scanning")

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

                    ok, check_reason = self.risk.pre_trade_check(
                        sig.position_size
                    )
                    if not ok:
                        logger.debug("Trade rejected: %s", check_reason)
                        continue

                    await self._execute_signal(sig)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Trading loop error: %s", e, exc_info=True)
                await store.log_activity("ERROR", f"Loop: {str(e)[:80]}")
                try:
                    await self.telegram.alert_error(str(e))
                except Exception:
                    pass

            await asyncio.sleep(self._scan_interval)

    async def _execute_signal(self, sig):
        """Execute a single trading signal — position stays OPEN until expiry."""
        order = self.strategy.create_order(sig)
        result = await self.polymarket.place_order(order)

        if not result.success:
            await store.log_activity(
                "WARN",
                f"Order failed: {result.error[:80] if result.error else 'unknown'}",
            )
            return

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
            status="OPEN",
        )
        self.trade_logger.log_trade(record)

        # Track as open position in risk manager
        self.risk.record_trade(0)

        await store.log_activity(
            "TRADE",
            f"OPENED {sig.contract.asset} {sig.contract.timeframe} "
            f"{sig.side} ${result.filled_size:.2f} "
            f"edge {sig.edge_pct:.1f}% — waiting for round expiry",
        )

        # Telegram alert
        try:
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
        except Exception as e:
            logger.debug("Telegram alert failed: %s", e)

    async def _resolution_loop(self):
        """Check for resolved positions and record their P&L.

        In paper mode, the PolySimulator holds positions until the
        round expires, then resolves them to $1 (win) or $0 (loss).
        This loop picks up those resolutions and updates everything.
        """
        while self._running:
            try:
                if self.simulator is not None:
                    resolved = self.simulator.check_resolutions()
                    for res in resolved:
                        # Update trade log: mark CLOSED with final P&L
                        self.trade_logger.update_trade(
                            res.trade_id,
                            res.pnl,
                            res.exit_price,
                        )

                        # Record P&L in risk manager
                        self.risk.record_trade(res.pnl)
                        self.strategy.update_portfolio_value(
                            self.risk.state.current_portfolio_value
                        )

                        # Track per-asset stats
                        self._update_asset_stats(
                            res.asset,
                            res.timeframe,
                            res.pnl,
                            0.0,  # edge not stored in ResolvedPosition
                        )

                        # Activity feed
                        outcome = "WON" if res.won else "LOST"
                        await store.log_activity(
                            "RESOLVED",
                            f"{res.asset} {res.timeframe} {res.direction} "
                            f"{res.side} → {outcome} "
                            f"{'+'if res.pnl >= 0 else ''}{res.pnl:.2f} "
                            f"(ref=${res.reference_price:,.2f} "
                            f"close=${res.close_price:,.2f})",
                        )

                        # Telegram notification
                        try:
                            await self.telegram.alert_trade(
                                asset=res.asset,
                                timeframe=res.timeframe,
                                direction=res.direction,
                                side=res.side,
                                size=res.size,
                                price=res.exit_price,
                                edge_pct=0.0,
                                confidence=0.0,
                                cex_price=res.close_price,
                                is_paper=True,
                            )
                        except Exception:
                            pass

                        logger.info(
                            "Position %s resolved: %s %+.2f",
                            res.trade_id, outcome, res.pnl,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Resolution loop error: %s", e, exc_info=True)

            await asyncio.sleep(2.0)  # Check every 2 seconds

    def _update_asset_stats(
        self, asset: str, timeframe: str, pnl: float, edge: float
    ):
        """Track per-asset-timeframe performance."""
        key = f"{asset}_{timeframe}"
        stat = self._asset_stats.get(
            key,
            {"pnl": 0.0, "trades": 0, "wins": 0, "edge": 0.0, "total_edge": 0.0},
        )
        stat["pnl"] = round(stat["pnl"] + pnl, 2)
        stat["trades"] += 1
        if pnl >= 0:
            stat["wins"] += 1
        stat["total_edge"] = stat.get("total_edge", 0) + edge
        stat["edge"] = round(stat["total_edge"] / stat["trades"], 2)
        self._asset_stats[key] = stat

    async def _snapshot_loop(self):
        """Periodically log portfolio snapshots to DB + equity curve."""
        while self._running:
            try:
                self.trade_logger.log_portfolio_snapshot(
                    portfolio_value=self.risk.state.current_portfolio_value,
                    daily_pnl=self.risk.state.daily_pnl,
                    total_pnl=self.risk.state.total_pnl,
                    open_positions=self.risk.state.open_position_count,
                    win_rate=self.risk.win_rate,
                )
                await store.record_equity_point(
                    self.risk.state.current_portfolio_value
                )
            except Exception as e:
                logger.debug("Snapshot error: %s", e)

            await asyncio.sleep(self._snapshot_interval)

    async def _state_loop(self):
        """Publish live bot state to the dashboard store."""
        while self._running:
            try:
                stats = self.trade_logger.get_stats() or {}
                recent = self.trade_logger.get_recent_trades(10)
                open_pos = self.trade_logger.get_open_positions()
                rs = self.risk.state

                total_pnl_pct = (
                    rs.total_pnl / self.initial_portfolio * 100
                    if self.initial_portfolio > 0
                    else 0.0
                )

                await store.update(
                    bot_status=(
                        "HALTED" if rs.trading_halted else "RUNNING"
                    ),
                    trading_halted=rs.trading_halted,
                    halt_reason=rs.halt_reason,
                    portfolio_value=rs.current_portfolio_value,
                    peak_portfolio_value=rs.peak_portfolio_value,
                    daily_pnl=rs.daily_pnl,
                    total_pnl=rs.total_pnl,
                    total_pnl_pct=total_pnl_pct,
                    daily_drawdown_pct=abs(self.risk.daily_drawdown_pct),
                    total_drawdown_pct=self.risk.total_drawdown_pct,
                    total_trades=stats.get("total_trades", 0),
                    wins=stats.get("wins", 0),
                    losses=stats.get("losses", 0),
                    win_rate=stats.get("win_rate", 0.0),
                    trades_today=rs.trades_today,
                    wins_today=rs.wins_today,
                    losses_today=rs.losses_today,
                    avg_edge=stats.get("avg_edge", 0.0),
                    avg_pnl=stats.get("avg_pnl", 0.0),
                    best_trade=stats.get("best_trade", 0.0),
                    worst_trade=stats.get("worst_trade", 0.0),
                    open_positions_count=rs.open_position_count,
                    asset_stats=self._asset_stats,
                    recent_trades=recent,
                    open_positions=open_pos,
                    price_source=self.binance._active_source,
                )
            except Exception as e:
                logger.debug("State publish error: %s", e)

            await asyncio.sleep(self._state_refresh)
