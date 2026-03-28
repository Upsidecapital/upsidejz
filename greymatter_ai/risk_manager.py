"""
GreymatterAI — Risk Manager
Tracks consecutive losers, daily drawdown, Sharpe drop and fires kill-switches.
Includes small-account ($250) safety limits.
"""
from __future__ import annotations

import logging

from config import (
    MAX_CONSECUTIVE_LOSERS, MIN_SHARPE_THRESHOLD,
    ACCOUNT_SIZE_USD, IS_SMALL_ACCOUNT, MAX_LOT_SIZE, MIN_LOT_SIZE,
    MAX_RISK_PER_TRADE_PCT,
)

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self) -> None:
        self.consecutive_losers: int  = 0
        self.system_active:      bool = True
        self._halt_reason:       str  = ""

    # ------------------------------------------------------------------
    # Called after each trade close
    # ------------------------------------------------------------------
    def on_trade_win(self) -> None:
        self.consecutive_losers = 0

    def on_trade_loss(self) -> None:
        self.consecutive_losers += 1
        logger.warning(
            "Consecutive losers: %d / %d%s",
            self.consecutive_losers, MAX_CONSECUTIVE_LOSERS,
            " [SMALL ACCOUNT — tight limit]" if IS_SMALL_ACCOUNT else "",
        )
        if self.consecutive_losers >= MAX_CONSECUTIVE_LOSERS:
            self._halt(f"{MAX_CONSECUTIVE_LOSERS} consecutive losers")

    def trigger_dd_kill(self) -> None:
        self._halt("Daily drawdown cap reached")

    def trigger_sharpe_kill(self, sharpe: float) -> None:
        if sharpe < MIN_SHARPE_THRESHOLD:
            self._halt(f"Sharpe dropped to {sharpe:.2f} (< {MIN_SHARPE_THRESHOLD})")

    def trigger_dead_market_kill(self) -> None:
        self._halt("Dead market detected (ATR collapse)")

    # ------------------------------------------------------------------
    # Lot-size safety (small account protection)
    # ------------------------------------------------------------------
    def validate_lot_size(self, raw_lot: float, risk_usd: float) -> float:
        """
        Clamp lot size to safe bounds.
        - Never go below MIN_LOT_SIZE (broker floor)
        - Never go above MAX_LOT_SIZE (small account hard cap)
        - If calculated risk_usd exceeds 5% of account → halve the lot (extra safety)
        """
        lot = max(MIN_LOT_SIZE, raw_lot)
        lot = min(MAX_LOT_SIZE, lot)

        max_acceptable_risk = ACCOUNT_SIZE_USD * 0.05   # absolute max 5% of account
        if risk_usd > max_acceptable_risk:
            logger.warning(
                "Risk $%.2f exceeds 5%% of $%.2f account — capping lot to %.2f",
                risk_usd, ACCOUNT_SIZE_USD, lot * (max_acceptable_risk / risk_usd),
            )
            lot = lot * (max_acceptable_risk / risk_usd)
            lot = round(max(MIN_LOT_SIZE, lot), 2)

        if IS_SMALL_ACCOUNT and lot > MAX_LOT_SIZE:
            logger.warning(
                "Small account lot cap applied: %.2f → %.2f", lot, MAX_LOT_SIZE
            )
            lot = MAX_LOT_SIZE

        return round(lot, 2)

    def check_minimum_tradeable(self, risk_usd: float) -> bool:
        """
        Returns False if risk_usd is so small that no valid lot can be calculated.
        At $250 with 1% risk = $2.50, this should always be fine with 0.01 lots.
        """
        if risk_usd < 0.50:
            logger.warning(
                "Risk $%.2f too small to place a valid order (min $0.50). "
                "Increase account size or check ACCOUNT_SIZE_USD env var.", risk_usd
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Manual controls
    # ------------------------------------------------------------------
    def resume(self) -> None:
        if not self.system_active:
            logger.info("System resuming. Resetting consecutive losers.")
        self.consecutive_losers = 0
        self.system_active      = True
        self._halt_reason       = ""

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _halt(self, reason: str) -> None:
        self.system_active = False
        self._halt_reason  = reason
        logger.critical("KILL-SWITCH FIRED: %s", reason)
