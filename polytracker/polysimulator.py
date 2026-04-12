"""
PolySimulator — synthetic Polymarket data source for paper trading.

Generates realistic BTC/ETH 5m and 15m up/down contracts priced from
the live Binance CEX feed, with an intentional LAG that creates the same
arbitrage opportunities a real Polymarket market would. Lets the bot
paper trade without needing any real Polymarket API credentials.

How the edge is simulated:
- Each round (5m or 15m) tracks the reference CEX price at round start.
- The contract price for "UP" drifts toward the CEX-implied probability
  as time passes, but with a LAG (uses a stale CEX price from N seconds
  ago).
- When a round expires, it resolves based on whether the CEX price
  at expiry is above or below the reference price.
- This creates natural, realistic arbitrage signals for the bot to act on.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .binance_feed import BinanceFeed

logger = logging.getLogger(__name__)


# Timeframe lengths in seconds
TIMEFRAME_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
}

# How stale the simulated Polymarket price is, in seconds.
# This is the arbitrage window — bot reacts to CEX moves faster than sim.
# Higher = more frequent and larger edge opportunities.
SIM_LAG_SECONDS = 15.0

# Extra noise on contract prices (stddev of a random walk).
SIM_NOISE = 0.02

# Min / max clamp for contract prices
MIN_PRICE = 0.02
MAX_PRICE = 0.98


@dataclass
class SimContract:
    """A single simulated Polymarket contract (one round of UP or DOWN)."""

    asset: str
    timeframe: str
    direction: str  # "up" or "down"
    round_id: int
    round_start: float
    round_end: float
    reference_price: float  # CEX price at round start
    token_id: str
    condition_id: str
    question: str
    yes_price: float = 0.5
    liquidity: float = 250_000.0


@dataclass
class SimRound:
    """A single round across both UP/DOWN for an asset/timeframe pair."""

    asset: str
    timeframe: str
    round_id: int
    start: float
    end: float
    reference_price: float
    up_contract: SimContract
    down_contract: SimContract


class PolySimulator:
    """
    Deterministic(ish) synthetic Polymarket feed keyed off live Binance prices.

    Usage:
        sim = PolySimulator(binance_feed)
        contracts = await sim.fetch_contracts()   # dict-like markets
        sim.resolve_trade(token_id, side, size, price) -> pnl  (on round expiry)
    """

    def __init__(self, binance_feed: "BinanceFeed"):
        self.binance = binance_feed
        self._rounds: dict[str, SimRound] = {}  # key: "BTC_5m" etc.
        self._price_history: dict[str, deque] = {
            "BTC": deque(maxlen=300),
            "ETH": deque(maxlen=300),
        }
        self._next_round_id = 1

    # --------------------------------------------------------------
    # Price history tracking
    # --------------------------------------------------------------
    def _record_price(self, asset: str, price: float, ts: float):
        hist = self._price_history.setdefault(asset, deque(maxlen=300))
        hist.append((ts, price))

    def _price_at(self, asset: str, lookback_seconds: float) -> float | None:
        """Return the CEX price from ~N seconds ago (stale/lagged price)."""
        hist = self._price_history.get(asset)
        if not hist:
            return None
        target = time.time() - lookback_seconds
        best = None
        for ts, price in hist:
            if ts <= target:
                best = price
            else:
                break
        return best if best is not None else hist[0][1]

    # --------------------------------------------------------------
    # Round management
    # --------------------------------------------------------------
    def _round_key(self, asset: str, timeframe: str) -> str:
        return f"{asset}_{timeframe}"

    def _ensure_round(
        self, asset: str, timeframe: str, now: float, current_price: float
    ) -> SimRound | None:
        """Create or roll a round for the given asset/timeframe."""
        key = self._round_key(asset, timeframe)
        duration = TIMEFRAME_SECONDS[timeframe]
        existing = self._rounds.get(key)

        # Expire if the round has ended
        if existing and now >= existing.end:
            self._resolve_round(existing, current_price)
            existing = None

        if existing is None:
            round_id = self._next_round_id
            self._next_round_id += 1
            start = now
            end = now + duration
            ref = current_price

            up_q = f"Will {asset} be above ${ref:,.2f} in {timeframe}?"
            dn_q = f"Will {asset} be below ${ref:,.2f} in {timeframe}?"

            up = SimContract(
                asset=asset,
                timeframe=timeframe,
                direction="up",
                round_id=round_id,
                round_start=start,
                round_end=end,
                reference_price=ref,
                token_id=f"SIM-{asset}-{timeframe}-UP-{round_id}",
                condition_id=f"SIM-COND-{asset}-{timeframe}-{round_id}",
                question=up_q,
            )
            dn = SimContract(
                asset=asset,
                timeframe=timeframe,
                direction="down",
                round_id=round_id,
                round_start=start,
                round_end=end,
                reference_price=ref,
                token_id=f"SIM-{asset}-{timeframe}-DN-{round_id}",
                condition_id=f"SIM-COND-{asset}-{timeframe}-{round_id}",
                question=dn_q,
            )
            new_round = SimRound(
                asset=asset,
                timeframe=timeframe,
                round_id=round_id,
                start=start,
                end=end,
                reference_price=ref,
                up_contract=up,
                down_contract=dn,
            )
            self._rounds[key] = new_round
            logger.debug(
                "Simulator opened round %d: %s %s ref=$%.2f",
                round_id, asset, timeframe, ref,
            )
            return new_round

        return existing

    def _resolve_round(self, rnd: SimRound, current_price: float):
        """A round has expired — log its outcome (bot already got paid via resolve_trade)."""
        won_up = current_price > rnd.reference_price
        logger.debug(
            "Simulator closed round %d (%s %s): %s "
            "(ref=$%.2f → close=$%.2f)",
            rnd.round_id, rnd.asset, rnd.timeframe,
            "UP" if won_up else "DOWN",
            rnd.reference_price, current_price,
        )

    # --------------------------------------------------------------
    # Pricing logic
    # --------------------------------------------------------------
    def _cex_implied_probability(
        self, asset: str, timeframe: str, reference_price: float, now: float
    ) -> float:
        """
        Estimate the probability of ending above the reference price, given
        recent CEX momentum and remaining time in the round.
        """
        hist = self._price_history.get(asset)
        if not hist or len(hist) < 3:
            return 0.5

        # Compute recent log-return volatility from last ~60s of prices
        recent = [p for (ts, p) in hist if ts >= now - 60]
        if len(recent) < 3:
            recent = [p for _, p in list(hist)[-10:]]

        rets = [
            math.log(recent[i] / recent[i - 1])
            for i in range(1, len(recent))
            if recent[i - 1] > 0
        ]
        if not rets:
            return 0.5

        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets), 1)
        sigma = math.sqrt(var) if var > 0 else 0.0005

        # Current price vs reference
        current = hist[-1][1] if hist else reference_price
        drift = math.log(current / reference_price) if reference_price > 0 else 0

        # Scale by sqrt(time remaining) for Brownian motion
        duration = TIMEFRAME_SECONDS[timeframe]
        t_remaining_ratio = 1.0  # We're computing at any point in the round
        effective_sigma = sigma * math.sqrt(duration * t_remaining_ratio)
        if effective_sigma <= 0:
            return 0.5

        # P(close > ref) using normal CDF approximation
        z = drift / effective_sigma
        prob_up = _normal_cdf(z)
        return max(0.02, min(0.98, prob_up))

    def _price_contract(
        self, contract: SimContract, now: float
    ) -> float:
        """
        Set the current price for a contract based on LAGGED CEX data
        plus noise. This is what creates the arbitrage edge — the sim
        trails the real CEX price by ~SIM_LAG_SECONDS.
        """
        # Use STALE price from N seconds ago → creates lag arbitrage
        lagged_cex = self._price_at(contract.asset, SIM_LAG_SECONDS)
        if lagged_cex is None:
            return contract.yes_price

        # Temporarily swap in the lagged price to compute implied prob
        hist = self._price_history[contract.asset]
        saved = hist[-1] if hist else None
        try:
            # Fake a "current" price using the stale one for pricing
            hist.append((now, lagged_cex))
            prob_up = self._cex_implied_probability(
                contract.asset, contract.timeframe,
                contract.reference_price, now,
            )
        finally:
            # Remove our fake point
            if saved is not None and hist and hist[-1] == (now, lagged_cex):
                hist.pop()

        if contract.direction == "up":
            base = prob_up
        else:
            base = 1.0 - prob_up

        # Add small noise
        noise = random.gauss(0, SIM_NOISE)
        price = max(MIN_PRICE, min(MAX_PRICE, base + noise))

        # Smooth update — heavier weight on the old (stale) price
        # keeps the sim "sticky" so edges persist longer
        if contract.yes_price > 0:
            price = 0.4 * price + 0.6 * contract.yes_price
        return round(price, 4)

    # --------------------------------------------------------------
    # Public API (matches PolymarketClient interface)
    # --------------------------------------------------------------
    async def fetch_contracts(self) -> list[dict]:
        """
        Return all active simulated markets in the same format as
        PolymarketClient.fetch_crypto_contracts().
        """
        now = time.time()

        # Record current CEX prices
        for asset in ("BTC", "ETH"):
            price = self.binance.get_price(asset)
            if price and price > 0:
                self._record_price(asset, price, now)

        result: list[dict] = []

        for asset in ("BTC", "ETH"):
            cex = self.binance.get_price(asset)
            if not cex or cex <= 0:
                continue
            for tf in ("5m", "15m"):
                rnd = self._ensure_round(asset, tf, now, cex)
                if rnd is None:
                    continue

                # Price both contracts
                up_price = self._price_contract(rnd.up_contract, now)
                dn_price = self._price_contract(rnd.down_contract, now)

                # Keep them approximately complementary
                avg_total = (up_price + dn_price) / 2
                if avg_total > 0:
                    adj = 1.0 / (up_price + dn_price)
                    up_price = max(MIN_PRICE, min(MAX_PRICE, up_price * adj))
                    dn_price = max(MIN_PRICE, min(MAX_PRICE, dn_price * adj))

                rnd.up_contract.yes_price = up_price
                rnd.down_contract.yes_price = dn_price

                for c in (rnd.up_contract, rnd.down_contract):
                    result.append(
                        self._to_market_dict(c)
                    )

        return result

    def _to_market_dict(self, c: SimContract) -> dict:
        """Format a SimContract as a Polymarket-like market payload."""
        return {
            "question": c.question,
            "condition_id": c.condition_id,
            "volume": c.liquidity,
            "tokens": [
                {
                    "outcome": "Yes",
                    "token_id": c.token_id,
                    "price": c.yes_price,
                },
                {
                    "outcome": "No",
                    "token_id": c.token_id + "-NO",
                    "price": round(1 - c.yes_price, 4),
                },
            ],
            "_sim_contract": c,  # back-reference for internal use
        }

    # --------------------------------------------------------------
    # Trade simulation
    # --------------------------------------------------------------
    def simulate_trade_outcome(
        self,
        asset: str,
        timeframe: str,
        direction: str,
        side: str,
        entry_price: float,
        size: float,
    ) -> tuple[float, float]:
        """
        Resolve a paper trade using spread-based P&L.

        Returns (pnl, exit_price).

        How it works (mirrors real Polymarket trading):
        ─────────────────────────────────────────────────
        The bot bought a contract at the LAGGED sim price (entry_price).
        The "true" fair value is computed from the CURRENT CEX spot
        price directly — NOT through the deque history used by the sim
        pricer. This ensures the edge is real: the sim is stale, the
        bot is fast.

        P&L = (exit - entry) * shares  for YES side
        """
        current = self.binance.get_price(asset)
        if not current:
            return 0.0, entry_price

        rnd = self._rounds.get(self._round_key(asset, timeframe))
        if rnd is None:
            return 0.0, entry_price

        # Direct fair value: how far is the current price from the
        # round's reference price?  Use a simple logistic model.
        ref = rnd.reference_price
        if ref <= 0:
            return 0.0, entry_price

        # Normalised move: positive = above reference, negative = below
        move = (current - ref) / ref

        # Scale by a sensitivity factor tuned to the timeframe
        # (shorter timeframes → sharper response)
        sensitivity = 8.0 if timeframe == "5m" else 5.0
        z = move * sensitivity * 100  # convert fractional to bps scale

        # Logistic → probability that price ends above reference
        import math
        prob_up = 1.0 / (1.0 + math.exp(-z))

        if direction == "up":
            fair_yes = prob_up
        else:
            fair_yes = 1.0 - prob_up

        fair_yes = max(MIN_PRICE, min(MAX_PRICE, fair_yes))

        # Execution noise
        noise = random.gauss(0, 0.005)
        exit_price = max(MIN_PRICE, min(MAX_PRICE, fair_yes + noise))

        # Spread-based P&L
        if side == "YES":
            shares = size / entry_price if entry_price > 0 else 0
            pnl = (exit_price - entry_price) * shares
        else:
            no_entry = 1 - entry_price
            no_exit = 1 - exit_price
            shares = size / no_entry if no_entry > 0 else 0
            pnl = (no_exit - no_entry) * shares

        return round(pnl, 2), round(exit_price, 4)


# ----------------------------------------------------------------------
# Math helpers
# ----------------------------------------------------------------------
def _normal_cdf(x: float) -> float:
    """Approximation of standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))
