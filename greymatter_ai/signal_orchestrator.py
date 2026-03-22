"""
GreymatterAI — Signal Orchestrator (Central Brain)
Runs every 30 minutes. Collects signals from all 7 strategies,
ranks by conviction, applies risk rules, and decides whether to fire a trade.

Risk rules enforced here:
- Max 1 open position (XAUUSD only)
- Max 2 % daily drawdown on $200k
- Max 1 R per trade
- Correlation kill-switch: discard if >2 strategies agree (over-fit risk)
- Stores every decision in DB
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, func

import ema_momentum
import ema_pullbacks
import liquidity_sweeps
import macd_fib
import order_flow
import orb_breakout
import vp_ivb
from alert_manager import send_signal_alert, send_trade_alert
from mt5_executor import executor as mt5_executor
from config import (
    ACCOUNT_SIZE_USD, MAX_DAILY_DRAWDOWN_PCT, MAX_RISK_PER_TRADE_PCT,
    STRATEGY_CONFIGS,
)
from data_fetcher import DataFetcher
from database import (
    AsyncSessionLocal, EquitySnapshot, Signal, StrategyName, Trade,
    TradeDirection, TradeStatus,
)
from risk_manager import RiskManager

logger = logging.getLogger(__name__)


@dataclass
class CandidateSignal:
    strategy: StrategyName
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    atr: float
    bar_close_time: datetime
    notes: str = ""


class SignalOrchestrator:
    def __init__(self, fetcher: DataFetcher, risk: RiskManager) -> None:
        self._fetcher = fetcher
        self._risk = risk

    # ------------------------------------------------------------------
    # Main heartbeat
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Called every 30 min by APScheduler."""
        logger.info("Orchestrator heartbeat")

        if not self._risk.system_active:
            logger.warning("System halted — skipping signal scan")
            return

        # 1. Refresh data
        data = await self._fetcher.refresh()
        m15 = data.get("15min")
        h1 = data.get("1h")
        h4 = data.get("4h")
        d1 = data.get("1day")

        if any(df is None for df in (m15, h1, h4, d1)):
            logger.error("Missing timeframe data — skipping")
            return

        # 2. Collect raw signals from all strategies
        candidates: List[CandidateSignal] = []
        candidates += self._collect_liquidity_sweeps(m15, d1)
        candidates += self._collect_ema_pullbacks(m15, h1)
        candidates += self._collect_orb_breakouts(m15)
        candidates += self._collect_ema_momentum(m15, h4, d1)
        candidates += self._collect_volume_profile(m15, h1)
        candidates += self._collect_order_flow(m15, d1)
        candidates += self._collect_macd_fib(m15, h1, h4)

        # 3. Store all raw signals
        await self._persist_signals(candidates)

        if not candidates:
            logger.info("No signals this cycle")
            return

        # 4. Correlation kill-switch — if 3+ strategies agree, skip
        candidates = self._correlation_filter(candidates)
        if not candidates:
            logger.info("Correlation kill-switch fired — skipping")
            return

        # 5. Rank by conviction (descending)
        candidates.sort(key=lambda c: c.conviction, reverse=True)
        best = candidates[0]

        logger.info(
            "Top signal: %s %s conviction=%.0f",
            best.strategy, best.direction, best.conviction,
        )

        # 6. Check if already in a trade
        if await self._has_open_trade():
            logger.info("Position already open — skipping")
            return

        # 7. Risk checks
        equity = await self._current_equity()
        daily_pnl = await self._daily_pnl()
        max_loss = equity * MAX_DAILY_DRAWDOWN_PCT

        if daily_pnl <= -max_loss:
            logger.warning("Daily DD cap reached (%.0f USD) — skipping", max_loss)
            self._risk.trigger_dd_kill()
            return

        risk_usd = equity * MAX_RISK_PER_TRADE_PCT
        lot_size = self._calc_lot_size(best.entry_price, best.stop_loss, risk_usd)
        if lot_size <= 0:
            logger.warning("Lot size calculation failed — skipping")
            return

        # 8. Fire trade
        await self._open_trade(best, lot_size, risk_usd)

        # 9. Snapshot equity
        await self._snapshot_equity(equity, daily_pnl)

    # ------------------------------------------------------------------
    # Strategy collectors
    # ------------------------------------------------------------------
    def _collect_liquidity_sweeps(self, m15, d1) -> List[CandidateSignal]:
        try:
            sig = liquidity_sweeps.detect(m15, d1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.LIQUIDITY_SWEEP,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("LiquiditySweeps error: %s", exc)
        return []

    def _collect_ema_pullbacks(self, m15, h1) -> List[CandidateSignal]:
        try:
            sig = ema_pullbacks.detect(m15, h1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.EMA_PULLBACK,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("EMAPullback error: %s", exc)
        return []

    def _collect_orb_breakouts(self, m15) -> List[CandidateSignal]:
        try:
            sig = orb_breakout.detect(m15)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.ORB_BREAKOUT,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("ORBBreakout error: %s", exc)
        return []

    def _collect_ema_momentum(self, m15, h4, d1) -> List[CandidateSignal]:
        try:
            sig = ema_momentum.detect(m15, h4, d1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.EMA_MOMENTUM,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("EMAMomentum error: %s", exc)
        return []

    def _collect_volume_profile(self, m15, h1) -> List[CandidateSignal]:
        try:
            sig = vp_ivb.detect(m15, h1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.VOLUME_PROFILE,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("VolumeProfile error: %s", exc)
        return []

    def _collect_order_flow(self, m15, d1) -> List[CandidateSignal]:
        try:
            sig = order_flow.detect(m15, d1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.ORDER_FLOW,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("OrderFlow error: %s", exc)
        return []

    def _collect_macd_fib(self, m15, h1, h4) -> List[CandidateSignal]:
        try:
            sig = macd_fib.detect(m15, h1, h4)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.MACD_FIB,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("MACDFib error: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Filters & scoring
    # ------------------------------------------------------------------
    def _correlation_filter(self, candidates: List[CandidateSignal]) -> List[CandidateSignal]:
        """Remove all candidates if ≥3 different strategies signal the same direction."""
        from collections import Counter
        dir_counts = Counter(c.direction for c in candidates)
        for direction, count in dir_counts.items():
            if count >= 3:
                same_dir = [c for c in candidates if c.direction == direction]
                logger.warning(
                    "Correlation kill-switch: %d strategies agree on %s — skipping all",
                    count, direction,
                )
                return []
        return candidates

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    @staticmethod
    def _calc_lot_size(entry: float, sl: float, risk_usd: float) -> float:
        """
        XAUUSD: 1 oz move = $1 P&L per oz.
        lot_size (oz) = risk_usd / |entry - stop_loss|
        """
        distance = abs(entry - sl)
        if distance < 0.01:
            return 0.0
        return round(risk_usd / distance, 2)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    async def _persist_signals(self, candidates: List[CandidateSignal]) -> None:
        async with AsyncSessionLocal() as db:
            for c in candidates:
                sig = Signal(
                    strategy=c.strategy,
                    direction=c.direction,
                    conviction=c.conviction,
                    entry_price=c.entry_price,
                    stop_loss=c.stop_loss,
                    take_profit=c.take_profit,
                    atr=c.atr,
                    timeframe="15min",
                    bar_close_time=c.bar_close_time,
                    notes=c.notes,
                )
                db.add(sig)
            await db.commit()

    async def _has_open_trade(self) -> bool:
        async with AsyncSessionLocal() as db:
            count = (await db.execute(
                select(func.count()).select_from(Trade)
                .where(Trade.status == TradeStatus.OPEN)
            )).scalar_one()
        return count > 0

    async def _current_equity(self) -> float:
        async with AsyncSessionLocal() as db:
            snap = (await db.execute(
                select(EquitySnapshot).order_by(EquitySnapshot.recorded_at.desc()).limit(1)
            )).scalar_one_or_none()
        return snap.equity_usd if snap else ACCOUNT_SIZE_USD

    async def _daily_pnl(self) -> float:
        from datetime import date
        today = date.today()
        async with AsyncSessionLocal() as db:
            result = (await db.execute(
                select(func.coalesce(func.sum(Trade.pnl_usd), 0.0))
                .select_from(Trade)
                .where(
                    Trade.closed_at >= datetime(today.year, today.month, today.day, tzinfo=timezone.utc),
                    Trade.status.in_([TradeStatus.CLOSED_WIN, TradeStatus.CLOSED_LOSS]),
                )
            )).scalar_one()
        return float(result)

    async def _open_trade(self, sig: CandidateSignal, lot_size: float, risk_usd: float) -> None:
        direction = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)

        # Place the real order on MT5 first
        ticket = mt5_executor.place_order(
            direction=direction,
            lot_size_oz=lot_size,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            comment=f"GM-{sig.strategy.value if hasattr(sig.strategy, 'value') else sig.strategy}",
        )
        if ticket is None:
            logger.error("MT5 order failed — trade NOT recorded in DB")
            return

        # Only persist to DB after MT5 confirms the order
        async with AsyncSessionLocal() as db:
            trade = Trade(
                strategy=sig.strategy,
                direction=sig.direction,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                lot_size=lot_size,
                risk_usd=risk_usd,
                conviction=sig.conviction,
                status=TradeStatus.OPEN,
                opened_at=datetime.now(timezone.utc),
                mt5_ticket=ticket,
                notes=sig.notes,
            )
            db.add(trade)
            await db.commit()
            await db.refresh(trade)

        logger.info("Trade opened: %s %s lot=%.2f ticket=%d", sig.strategy, sig.direction, lot_size, ticket)
        await send_trade_alert(trade=None, sig=sig, lot_size=lot_size, risk_usd=risk_usd)

    async def _snapshot_equity(self, equity: float, daily_pnl: float) -> None:
        async with AsyncSessionLocal() as db:
            snap = EquitySnapshot(
                equity_usd=equity,
                daily_pnl_usd=daily_pnl,
                consecutive_losers=self._risk.consecutive_losers,
                system_active=self._risk.system_active,
            )
            db.add(snap)
            await db.commit()
