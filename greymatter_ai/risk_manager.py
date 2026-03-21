"""
GreymatterAI — Risk Manager
Tracks consecutive losers, daily drawdown, Sharpe drop and fires kill-switches.
"""
from __future__ import annotations

import logging

from config import MAX_CONSECUTIVE_LOSERS, MIN_SHARPE_THRESHOLD

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self) -> None:
        self.consecutive_losers: int = 0
        self.system_active: bool = True
        self._halt_reason: str = ""

    # ------------------------------------------------------------------
    # Called after each trade close
    # ------------------------------------------------------------------
    def on_trade_win(self) -> None:
        self.consecutive_losers = 0

    def on_trade_loss(self) -> None:
        self.consecutive_losers += 1
        logger.warning("Consecutive losers: %d / %d", self.consecutive_losers, MAX_CONSECUTIVE_LOSERS)
        if self.consecutive_losers >= MAX_CONSECUTIVE_LOSERS:
            self._halt("8 consecutive losers")

    def trigger_dd_kill(self) -> None:
        self._halt("Daily 2% drawdown cap reached")

    def trigger_sharpe_kill(self, sharpe: float) -> None:
        if sharpe < MIN_SHARPE_THRESHOLD:
            self._halt(f"Sharpe dropped to {sharpe:.2f} (< {MIN_SHARPE_THRESHOLD})")

    def trigger_dead_market_kill(self) -> None:
        self._halt("Dead market detected (ATR collapse)")

    # ------------------------------------------------------------------
    # Manual controls
    # ------------------------------------------------------------------
    def resume(self) -> None:
        if not self.system_active:
            logger.info("System resuming. Resetting consecutive losers.")
        self.consecutive_losers = 0
        self.system_active = True
        self._halt_reason = ""

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _halt(self, reason: str) -> None:
        self.system_active = False
        self._halt_reason = reason
        logger.critical("KILL-SWITCH FIRED: %s", reason)
