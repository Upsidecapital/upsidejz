"""Terminal dashboard showing P&L, win rate, open positions, last 10 trades."""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from .risk_manager import RiskManager
from .trade_logger import TradeLogger

logger = logging.getLogger(__name__)


class Dashboard:
    """Rich terminal dashboard for monitoring the bot."""

    def __init__(
        self,
        risk_manager: RiskManager,
        trade_logger: TradeLogger,
        refresh_rate: float = 2.0,
        is_paper: bool = True,
    ):
        self.risk = risk_manager
        self.logger = trade_logger
        self.refresh_rate = refresh_rate
        self.is_paper = is_paper
        self._running = False
        self._binance_prices: dict[str, float] = {}
        self._start_time = time.time()

    def update_prices(self, prices: dict[str, float]):
        self._binance_prices = prices

    async def run(self):
        """Run the dashboard update loop."""
        self._running = True
        while self._running:
            try:
                self._render()
            except Exception as e:
                logger.debug("Dashboard render error: %s", e)
            await asyncio.sleep(self.refresh_rate)

    def stop(self):
        self._running = False

    def _render(self):
        """Render the dashboard to terminal."""
        os.system("clear" if os.name != "nt" else "cls")

        state = self.risk.state
        stats = self.logger.get_stats()
        recent = self.logger.get_recent_trades(10)
        open_pos = self.logger.get_open_positions()

        mode = "PAPER" if self.is_paper else "LIVE"
        uptime = time.time() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)

        lines = []
        lines.append("=" * 72)
        lines.append(
            f"  UPSIDE - POLYTRACKER  [{mode} MODE]"
            f"  Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}"
        )
        lines.append("=" * 72)

        # Portfolio summary
        lines.append("")
        lines.append("  PORTFOLIO")
        lines.append(f"  {'─' * 40}")
        lines.append(
            f"  Value:        ${state.current_portfolio_value:>12,.2f}"
        )
        lines.append(f"  Daily P&L:    ${state.daily_pnl:>+12,.2f}")
        lines.append(f"  Total P&L:    ${state.total_pnl:>+12,.2f}")
        lines.append(
            f"  Peak Value:   ${state.peak_portfolio_value:>12,.2f}"
        )

        # Risk metrics
        lines.append("")
        lines.append("  RISK METRICS")
        lines.append(f"  {'─' * 40}")
        daily_dd = self.risk.daily_drawdown_pct
        total_dd = self.risk.total_drawdown_pct
        lines.append(
            f"  Daily Drawdown:  {daily_dd:>+8.2f}%"
            f"  (limit: -{self.risk.config.daily_drawdown_limit_pct}%)"
        )
        lines.append(
            f"  Total Drawdown:  {total_dd:>+8.2f}%"
            f"  (kill:  -{self.risk.config.total_drawdown_kill_pct}%)"
        )
        halted = "YES - " + state.halt_reason if state.trading_halted else "No"
        lines.append(f"  Trading Halted:  {halted}")

        # Performance
        lines.append("")
        lines.append("  PERFORMANCE")
        lines.append(f"  {'─' * 40}")
        win_rate = stats.get("win_rate", 0)
        total_trades = stats.get("total_trades", 0)
        lines.append(f"  Win Rate:     {win_rate:>8.1f}%")
        lines.append(f"  Total Trades: {total_trades:>8d}")
        lines.append(f"  Trades Today: {state.trades_today:>8d}")
        lines.append(
            f"  Today W/L:    {state.wins_today}/{state.losses_today}"
        )
        lines.append(
            f"  Avg P&L:      ${stats.get('avg_pnl', 0):>+8.2f}"
        )
        lines.append(
            f"  Best Trade:   ${stats.get('best_trade', 0):>+8.2f}"
        )
        lines.append(
            f"  Worst Trade:  ${stats.get('worst_trade', 0):>+8.2f}"
        )

        # Live prices
        lines.append("")
        lines.append("  CEX PRICES (Binance)")
        lines.append(f"  {'─' * 40}")
        for asset in ["BTC", "ETH"]:
            price = self._binance_prices.get(asset, 0)
            lines.append(f"  {asset}:  ${price:>12,.2f}")

        # Open positions
        lines.append("")
        lines.append(f"  OPEN POSITIONS ({len(open_pos)})")
        lines.append(f"  {'─' * 68}")
        if open_pos:
            lines.append(
                f"  {'Asset':<6} {'TF':<4} {'Dir':<5} {'Side':<4} "
                f"{'Size':>8} {'Entry':>8} {'Edge':>6}"
            )
            for pos in open_pos[:5]:
                lines.append(
                    f"  {pos['asset']:<6} {pos['timeframe']:<4} "
                    f"{pos['direction']:<5} {pos['side']:<4} "
                    f"${pos['size_usdc']:>7.2f} "
                    f"{pos['entry_price']:>8.4f} "
                    f"{pos['edge_pct']:>5.1f}%"
                )
        else:
            lines.append("  No open positions")

        # Last 10 trades
        lines.append("")
        lines.append("  LAST 10 TRADES")
        lines.append(f"  {'─' * 68}")
        if recent:
            lines.append(
                f"  {'Time':<20} {'Asset':<5} {'Side':<4} "
                f"{'Size':>8} {'P&L':>10} {'Status':<8}"
            )
            for trade in recent:
                ts = datetime.fromtimestamp(
                    trade["timestamp"], tz=timezone.utc
                ).strftime("%m-%d %H:%M:%S")
                pnl_str = (
                    f"${trade['pnl']:>+8.2f}" if trade["pnl"] != 0 else "    --"
                )
                lines.append(
                    f"  {ts:<20} {trade['asset']:<5} {trade['side']:<4} "
                    f"${trade['size_usdc']:>7.2f} {pnl_str:>10} "
                    f"{trade['status']:<8}"
                )
        else:
            lines.append("  No trades yet")

        lines.append("")
        lines.append("=" * 72)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"  Last updated: {now}")
        lines.append("")

        print("\n".join(lines))
