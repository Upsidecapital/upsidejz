"""
GreymatterAI — Adaptive Outcome Tracker

Tracks per-setup win rates using an Exponential Weighted Moving Average (EWMA)
and translates that into a conviction multiplier applied to future signals.

How it works:
  1. Every time a trade closes, `record_outcome()` is called with pnl_r.
  2. The EWMA win rate for that strategy+setup is updated.
  3. A conviction_multiplier (0.6–1.4) is derived from the EWMA:
       - EWMA  ≥ 0.60  → multiplier up to 1.40  (boost well-performing setups)
       - EWMA  ≤ 0.40  → multiplier down to 0.60 (suppress underperforming setups)
       - < MIN_TRADES  → multiplier = 1.0          (neutral; learning phase)
  4. Signal orchestrator applies the multiplier before ranking candidates.

The EWMA alpha is calibrated to a 20-trade window:
    α = 2 / (WINDOW + 1) = 2/21 ≈ 0.095

A multiplier of 0.6 on a 40-conviction signal → 24 (below the 40 gate = effectively filtered).
A multiplier of 1.4 on a 60-conviction signal → 84 (high-conviction entry).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from database import AsyncSessionLocal, SetupPerformance

logger = logging.getLogger(__name__)

WINDOW = 20        # EWMA half-life trades
MIN_TRADES = 5     # trades before multiplier departs from 1.0
ALPHA = 2.0 / (WINDOW + 1)   # ≈ 0.095


class OutcomeTracker:

    # ------------------------------------------------------------------
    # Write path (called from scheduler on trade close)
    # ------------------------------------------------------------------
    async def record_outcome(self, strategy: str, setup: str, pnl_r: float) -> None:
        """
        Update the EWMA and multiplier for a given strategy+setup after a trade closes.
        Creates the row if this is the first trade for that setup.
        """
        win = 1.0 if pnl_r > 0 else 0.0

        async with AsyncSessionLocal() as db:
            row = await self._get_or_create(db, strategy, setup)

            row.trades_total += 1
            if win:
                row.trades_win += 1

            # EWMA win rate update
            row.ewma_win_rate = ALPHA * win + (1.0 - ALPHA) * row.ewma_win_rate

            # Running avg R (simple cumulative average)
            n = row.trades_total
            row.avg_pnl_r = row.avg_pnl_r + (pnl_r - row.avg_pnl_r) / n

            # Recompute multiplier
            row.conviction_multiplier = self._compute_multiplier(
                row.ewma_win_rate, row.trades_total
            )
            row.last_updated = datetime.now(timezone.utc)

            await db.commit()

        logger.info(
            "OutcomeTracker [%s/%s] trades=%d EWMA_WR=%.2f multiplier=%.2f",
            strategy, setup,
            row.trades_total, row.ewma_win_rate, row.conviction_multiplier,
        )

    # ------------------------------------------------------------------
    # Read path (called from orchestrator before ranking)
    # ------------------------------------------------------------------
    async def get_multiplier(self, strategy: str, setup: str) -> float:
        """
        Returns the current conviction multiplier for the given setup.
        Returns 1.0 (neutral) if the setup has never been seen.
        """
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SetupPerformance)
                .where(SetupPerformance.strategy == strategy)
                .where(SetupPerformance.setup == setup)
            )).scalar_one_or_none()
        if row is None:
            return 1.0
        return row.conviction_multiplier

    async def all_performance(self) -> list[dict]:
        """Returns all setup rows for the dashboard API."""
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SetupPerformance).order_by(SetupPerformance.strategy, SetupPerformance.setup)
            )).scalars().all()
        return [
            {
                "strategy": r.strategy,
                "setup": r.setup,
                "trades_total": r.trades_total,
                "trades_win": r.trades_win,
                "win_rate_pct": round(r.ewma_win_rate * 100, 1),
                "avg_pnl_r": round(r.avg_pnl_r, 2),
                "multiplier": round(r.conviction_multiplier, 2),
                "last_updated": r.last_updated.isoformat() if r.last_updated else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _get_or_create(db, strategy: str, setup: str) -> SetupPerformance:
        row = (await db.execute(
            select(SetupPerformance)
            .where(SetupPerformance.strategy == strategy)
            .where(SetupPerformance.setup == setup)
        )).scalar_one_or_none()
        if row is None:
            row = SetupPerformance(
                strategy=strategy,
                setup=setup,
                ewma_win_rate=0.5,   # start neutral
                conviction_multiplier=1.0,
            )
            db.add(row)
            await db.flush()
        return row

    @staticmethod
    def _compute_multiplier(ewma_win_rate: float, trades_total: int) -> float:
        """
        Linear interpolation between 0.6 and 1.4 based on EWMA win rate.
        Clamped to the neutral value of 1.0 until MIN_TRADES seen.
        """
        if trades_total < MIN_TRADES:
            return 1.0
        # Linear map: WR 0.4→multiplier 0.6, WR 0.5→1.0, WR 0.6→1.4
        raw = 1.0 + (ewma_win_rate - 0.5) * 4.0
        return round(max(0.6, min(1.4, raw)), 3)


# Module-level singleton used by scheduler and orchestrator
outcome_tracker = OutcomeTracker()
