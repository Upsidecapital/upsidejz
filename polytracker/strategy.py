"""Arbitrage strategy engine - detects edge between Polymarket and CEX prices."""

import logging
import math
import time
from dataclasses import dataclass

from .binance_feed import BinanceFeed
from .config import BotConfig
from .kelly import KellySizer
from .polymarket_client import Contract, PolymarketClient, TradeOrder

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """An arbitrage signal identified by the strategy."""

    contract: Contract
    side: str  # "YES" or "NO"
    edge_pct: float
    confidence: float
    cex_price: float
    cex_implied_prob: float
    polymarket_price: float
    position_size: float
    timestamp: float


class ArbitrageStrategy:
    """
    Identifies opportunities when Polymarket odds lag CEX prices
    by more than 3 percentage points.

    Monitors BTC/ETH 5-minute and 15-minute up/down contracts.
    """

    def __init__(
        self,
        config: BotConfig,
        binance: BinanceFeed,
        polymarket: PolymarketClient,
        kelly: KellySizer,
        portfolio_value: float,
    ):
        self.config = config
        self.binance = binance
        self.polymarket = polymarket
        self.kelly = kelly
        self.portfolio_value = portfolio_value
        self._last_prices: dict[str, list[float]] = {"BTC": [], "ETH": []}
        self._price_window = 60  # Track 60 price points for momentum

    def update_portfolio_value(self, value: float):
        self.portfolio_value = value

    async def scan_for_signals(self) -> list[Signal]:
        """Scan all monitored contracts for arbitrage signals."""
        signals = []
        contracts = await self.polymarket.fetch_crypto_contracts()

        if not contracts:
            logger.debug("No crypto contracts found")
            return signals

        for contract in contracts:
            # Skip low liquidity markets
            if contract.liquidity < self.config.trading.min_market_liquidity:
                continue

            signal = await self._evaluate_contract(contract)
            if signal:
                signals.append(signal)

        if signals:
            signals.sort(key=lambda s: s.edge_pct, reverse=True)
            logger.info("Found %d actionable signals", len(signals))

        return signals

    async def _evaluate_contract(self, contract: Contract) -> Signal | None:
        """Evaluate a single contract for arbitrage opportunity."""
        cex_price = self.binance.get_price(contract.asset)
        if cex_price is None:
            return None

        # Track price history for momentum calculation
        history = self._last_prices[contract.asset]
        history.append(cex_price)
        if len(history) > self._price_window:
            history.pop(0)

        # Calculate CEX-implied probability for this contract
        cex_implied_prob = self._calculate_cex_implied_probability(
            contract, cex_price
        )

        # Get current Polymarket price
        poly_price = contract.yes_price
        if poly_price <= 0 or poly_price >= 1:
            return None

        # Calculate edge: difference between CEX-implied and Polymarket price
        if contract.direction == "up":
            edge_pct = (cex_implied_prob - poly_price) * 100
            side = "YES" if edge_pct > 0 else "NO"
        else:
            edge_pct = (poly_price - cex_implied_prob) * 100
            side = "NO" if edge_pct > 0 else "YES"

        edge_pct = abs(edge_pct)

        # Check if edge exceeds the lag threshold
        lag = abs(cex_implied_prob - poly_price) * 100
        if lag < self.config.trading.lag_threshold_pct:
            return None

        # Calculate confidence score
        confidence = self._calculate_confidence(
            contract, cex_price, edge_pct, lag
        )

        # Apply thresholds
        if edge_pct < self.config.trading.min_edge_pct:
            return None
        if confidence < self.config.trading.confidence_threshold:
            return None

        # Calculate position size via Kelly
        odds = self.kelly.calculate_odds_from_price(poly_price)
        win_prob = self.kelly.estimate_win_probability(
            cex_implied_prob, poly_price, edge_pct
        )
        position_size = self.kelly.calculate_position_size(
            self.portfolio_value, win_prob, odds, edge_pct
        )

        if position_size <= 0:
            return None

        logger.info(
            "Signal: %s %s %s | Edge: %.2f%% | Conf: %.1f%% | "
            "Size: $%.2f | CEX: $%.2f | Poly: %.4f",
            contract.asset,
            contract.timeframe,
            contract.direction,
            edge_pct,
            confidence * 100,
            position_size,
            cex_price,
            poly_price,
        )

        return Signal(
            contract=contract,
            side=side,
            edge_pct=edge_pct,
            confidence=confidence,
            cex_price=cex_price,
            cex_implied_prob=cex_implied_prob,
            polymarket_price=poly_price,
            position_size=position_size,
            timestamp=time.time(),
        )

    def _calculate_cex_implied_probability(
        self, contract: Contract, cex_price: float
    ) -> float:
        """
        Estimate the probability of the contract outcome based on CEX price.

        For a "BTC up in 5 minutes" contract, we use recent price momentum
        and volatility to estimate the probability that price will be higher
        at contract expiry.
        """
        history = self._last_prices.get(contract.asset, [])

        if len(history) < 5:
            return 0.5  # Not enough data

        # Calculate short-term momentum
        recent = history[-5:]
        momentum = (recent[-1] - recent[0]) / recent[0]

        # Calculate recent volatility
        if len(history) >= 10:
            returns = [
                (history[i] - history[i - 1]) / history[i - 1]
                for i in range(1, len(history))
            ]
            volatility = (
                sum(r ** 2 for r in returns) / len(returns)
            ) ** 0.5
        else:
            volatility = 0.001

        # Time factor: shorter timeframes have less drift
        time_factor = 1.0 if contract.timeframe == "5m" else 1.5

        # Z-score of momentum relative to volatility
        if volatility > 0:
            z_score = momentum / (volatility * math.sqrt(time_factor))
        else:
            z_score = 0

        # Convert z-score to probability using sigmoid approximation
        prob_up = 1.0 / (1.0 + math.exp(-z_score * 2.5))

        if contract.direction == "up":
            return max(0.01, min(0.99, prob_up))
        else:
            return max(0.01, min(0.99, 1 - prob_up))

    def _calculate_confidence(
        self,
        contract: Contract,
        cex_price: float,
        edge_pct: float,
        lag_pct: float,
    ) -> float:
        """
        Calculate a confidence score (0-1) for a signal.

        Factors:
        - Price history depth (more data = more confident)
        - Edge magnitude (larger edge = more confident)
        - Market liquidity
        - Freshness of price data
        """
        score = 0.0

        # Data depth: 0-0.3
        history = self._last_prices.get(contract.asset, [])
        depth_score = min(len(history) / self._price_window, 1.0) * 0.3
        score += depth_score

        # Edge magnitude: 0-0.3
        edge_score = min(edge_pct / 15.0, 1.0) * 0.3
        score += edge_score

        # Liquidity: 0-0.2
        liq_ratio = min(
            contract.liquidity / (self.config.trading.min_market_liquidity * 4),
            1.0,
        )
        score += liq_ratio * 0.2

        # Lag magnitude: 0-0.2 (bigger lag = more confident the market is stale)
        lag_score = min(lag_pct / 10.0, 1.0) * 0.2
        score += lag_score

        return min(score, 1.0)

    def create_order(self, signal: Signal) -> TradeOrder:
        """Create a trade order from a signal."""
        return TradeOrder(
            contract=signal.contract,
            side=signal.side,
            size=signal.position_size,
            price=signal.polymarket_price,
        )
