"""Fractional Kelly Criterion position sizing."""

import logging

from .config import TradingConfig

logger = logging.getLogger(__name__)


class KellySizer:
    """Half-Kelly position sizing for conservative capital allocation."""

    def __init__(self, config: TradingConfig):
        self.config = config

    def calculate_position_size(
        self,
        portfolio_value: float,
        win_probability: float,
        odds: float,
        edge_pct: float,
    ) -> float:
        """
        Calculate position size using fractional Kelly Criterion.

        Args:
            portfolio_value: Total portfolio value in USDC.
            win_probability: Estimated probability of winning (0-1).
            odds: Decimal odds (e.g., 2.0 for even money).
            edge_pct: Calculated edge percentage.

        Returns:
            Position size in USDC, capped at max_position_pct of portfolio.
        """
        if win_probability <= 0 or win_probability >= 1:
            return 0.0
        if odds <= 1:
            return 0.0
        if edge_pct < self.config.min_edge_pct:
            return 0.0

        # Kelly formula: f* = (bp - q) / b
        # b = odds - 1 (net odds)
        # p = win probability
        # q = 1 - p (loss probability)
        b = odds - 1
        p = win_probability
        q = 1 - p

        kelly_fraction = (b * p - q) / b

        if kelly_fraction <= 0:
            logger.debug(
                "Negative Kelly fraction (%.4f) - no edge", kelly_fraction
            )
            return 0.0

        # Apply half-Kelly for conservative sizing
        adjusted = kelly_fraction * self.config.kelly_fraction

        # Convert to USDC amount
        position_size = portfolio_value * adjusted

        # Cap at maximum position size
        max_size = portfolio_value * (self.config.max_position_pct / 100)
        position_size = min(position_size, max_size)

        # Floor at $1 minimum for meaningful trades
        if position_size < 1.0:
            return 0.0

        logger.debug(
            "Kelly sizing: raw=%.4f, adjusted=%.4f, size=$%.2f (max=$%.2f)",
            kelly_fraction,
            adjusted,
            position_size,
            max_size,
        )

        return round(position_size, 2)

    def calculate_odds_from_price(self, contract_price: float) -> float:
        """
        Convert a contract price (0-1) to decimal odds.

        A contract priced at 0.40 implies 2.5x odds (1 / 0.40).
        """
        if contract_price <= 0 or contract_price >= 1:
            return 0.0
        return 1.0 / contract_price

    def estimate_win_probability(
        self,
        cex_implied_prob: float,
        polymarket_price: float,
        edge_pct: float,
    ) -> float:
        """
        Estimate win probability based on CEX-implied probability.

        When Polymarket lags the CEX price, the CEX-implied probability
        is our best estimate of the true probability.
        """
        # Use CEX-implied probability as our estimate, with a small
        # discount for execution risk and model uncertainty
        execution_discount = 0.02  # 2% discount for slippage/timing
        return max(0.0, min(1.0, cex_implied_prob - execution_discount))
