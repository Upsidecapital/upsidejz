"""Risk management with kill switch and drawdown tracking."""

import logging
import time
from dataclasses import dataclass, field

from .config import TradingConfig

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Current risk management state."""

    trading_halted: bool = False
    halt_reason: str = ""
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    peak_portfolio_value: float = 0.0
    current_portfolio_value: float = 0.0
    open_position_count: int = 0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    daily_start_value: float = 0.0
    day_start_timestamp: float = field(default_factory=time.time)


class RiskManager:
    """Manages risk limits, drawdown tracking, and kill switch."""

    def __init__(self, config: TradingConfig, initial_portfolio: float):
        self.config = config
        self.state = RiskState(
            peak_portfolio_value=initial_portfolio,
            current_portfolio_value=initial_portfolio,
            daily_start_value=initial_portfolio,
        )
        self._alert_callbacks: list = []

    def on_alert(self, callback):
        """Register a callback for risk alerts: callback(level, message)."""
        self._alert_callbacks.append(callback)

    def _emit_alert(self, level: str, message: str):
        for cb in self._alert_callbacks:
            try:
                cb(level, message)
            except Exception as e:
                logger.error("Alert callback error: %s", e)

    def check_new_day(self):
        """Reset daily counters if a new day has started."""
        now = time.time()
        # Check if 24 hours have passed since day start
        if now - self.state.day_start_timestamp >= 86400:
            logger.info(
                "New trading day - resetting daily counters. "
                "Yesterday P&L: $%.2f",
                self.state.daily_pnl,
            )
            self.state.daily_pnl = 0.0
            self.state.trades_today = 0
            self.state.wins_today = 0
            self.state.losses_today = 0
            self.state.daily_start_value = self.state.current_portfolio_value
            self.state.day_start_timestamp = now

            # Un-halt if only daily limit was hit (not total drawdown)
            if (
                self.state.trading_halted
                and "daily" in self.state.halt_reason.lower()
            ):
                self.state.trading_halted = False
                self.state.halt_reason = ""
                logger.info("Daily halt lifted for new trading day")

    def can_trade(self) -> tuple[bool, str]:
        """Check if trading is allowed under current risk parameters."""
        self.check_new_day()

        if self.state.trading_halted:
            return False, f"Trading halted: {self.state.halt_reason}"

        if self.state.open_position_count >= self.config.max_open_positions:
            return False, (
                f"Max open positions reached "
                f"({self.config.max_open_positions})"
            )

        return True, "OK"

    def pre_trade_check(self, position_size: float) -> tuple[bool, str]:
        """Validate a trade before execution."""
        can, reason = self.can_trade()
        if not can:
            return False, reason

        # Check position size limit
        max_size = (
            self.state.current_portfolio_value
            * self.config.max_position_pct
            / 100
        )
        if position_size > max_size:
            return False, (
                f"Position ${position_size:.2f} exceeds max "
                f"${max_size:.2f} ({self.config.max_position_pct}%)"
            )

        return True, "OK"

    def record_trade(self, pnl: float, is_open: bool = True):
        """Record a trade result and update risk state."""
        self.state.daily_pnl += pnl
        self.state.total_pnl += pnl
        self.state.current_portfolio_value += pnl
        self.state.trades_today += 1

        if pnl >= 0:
            self.state.wins_today += 1
        else:
            self.state.losses_today += 1

        if is_open:
            self.state.open_position_count += 1

        # Update peak
        if self.state.current_portfolio_value > self.state.peak_portfolio_value:
            self.state.peak_portfolio_value = (
                self.state.current_portfolio_value
            )

        # Check drawdown limits
        self._check_daily_drawdown()
        self._check_total_drawdown()

    def record_position_close(self, pnl: float):
        """Record a position being closed."""
        self.state.open_position_count = max(
            0, self.state.open_position_count - 1
        )
        self.record_trade(pnl, is_open=False)

    def _check_daily_drawdown(self):
        """Check if daily drawdown limit has been breached."""
        if self.state.daily_start_value <= 0:
            return

        daily_drawdown_pct = (
            abs(min(0, self.state.daily_pnl))
            / self.state.daily_start_value
            * 100
        )

        if daily_drawdown_pct >= self.config.daily_drawdown_limit_pct:
            self.state.trading_halted = True
            self.state.halt_reason = (
                f"Daily drawdown limit hit: "
                f"-{daily_drawdown_pct:.1f}% "
                f"(limit: -{self.config.daily_drawdown_limit_pct}%)"
            )
            msg = (
                f"KILL SWITCH ACTIVATED - {self.state.halt_reason}\n"
                f"Daily P&L: ${self.state.daily_pnl:.2f}\n"
                f"Portfolio: ${self.state.current_portfolio_value:.2f}"
            )
            logger.critical(msg)
            self._emit_alert("CRITICAL", msg)

        elif daily_drawdown_pct >= self.config.daily_drawdown_limit_pct * 0.75:
            msg = (
                f"WARNING: Daily drawdown at -{daily_drawdown_pct:.1f}% "
                f"(limit: -{self.config.daily_drawdown_limit_pct}%)"
            )
            logger.warning(msg)
            self._emit_alert("WARNING", msg)

    def _check_total_drawdown(self):
        """Check if total drawdown kill switch should trigger."""
        if self.state.peak_portfolio_value <= 0:
            return

        total_drawdown_pct = (
            (self.state.peak_portfolio_value - self.state.current_portfolio_value)
            / self.state.peak_portfolio_value
            * 100
        )

        if total_drawdown_pct >= self.config.total_drawdown_kill_pct:
            self.state.trading_halted = True
            self.state.halt_reason = (
                f"Total drawdown kill switch: "
                f"-{total_drawdown_pct:.1f}% from peak "
                f"(limit: -{self.config.total_drawdown_kill_pct}%)"
            )
            msg = (
                f"KILL SWITCH ACTIVATED - {self.state.halt_reason}\n"
                f"Peak: ${self.state.peak_portfolio_value:.2f}\n"
                f"Current: ${self.state.current_portfolio_value:.2f}"
            )
            logger.critical(msg)
            self._emit_alert("CRITICAL", msg)

    @property
    def daily_drawdown_pct(self) -> float:
        if self.state.daily_start_value <= 0:
            return 0.0
        return (
            min(0, self.state.daily_pnl)
            / self.state.daily_start_value
            * 100
        )

    @property
    def total_drawdown_pct(self) -> float:
        if self.state.peak_portfolio_value <= 0:
            return 0.0
        return (
            (self.state.peak_portfolio_value - self.state.current_portfolio_value)
            / self.state.peak_portfolio_value
            * 100
        )

    @property
    def win_rate(self) -> float:
        total = self.state.wins_today + self.state.losses_today
        if total == 0:
            return 0.0
        return self.state.wins_today / total * 100
