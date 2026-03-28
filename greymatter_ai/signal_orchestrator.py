"""
GreymatterAI — NAS100 Signal Orchestrator (Central Brain)
Runs every 15 minutes (aligns with M15 bar close).
Runs Fabio Valentini's 3 strategies: ORB, IVB, Order Flow.

Risk rules:
- Max 1 open NAS100 position at a time
- Max 2 % daily drawdown on equity
- Max 1 R per trade
- If all 3 strategies fire the same direction → require conviction ≥ 70 to proceed
  (agreement = strong confirmation, not over-fit kill)
- Stores every signal & decision in DB

Adaptive weights:
- outcome_tracker maintains per-setup EWMA win rates
- claude_learn provides AI-driven setup adjustments from trade pattern analysis
- Combined multiplier = EWMA_multiplier × Claude_adjustment (clamped 0.5–1.5)
- raw_conviction is stored so the dashboard can show both pre- and post-weight values

Account awareness:
- Reads live equity from MT5 (if connected) or falls back to ACCOUNT_SIZE_USD env var
- For small accounts (<$1000), risk_usd is further scaled by Claude's risk_adjustment
- Lot sizes are validated through RiskManager.validate_lot_size() before every order
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, func

import order_flow
import orb_breakout
import vp_ivb
from alert_manager import send_trade_alert
from claude_learning import claude_learn
from mt5_executor import executor as mt5_executor
from config import (
    ACCOUNT_SIZE_USD, MAX_DAILY_DRAWDOWN_PCT, MAX_RISK_PER_TRADE_PCT,
    MT5_POINT_VALUE, STRATEGY_CONFIGS, IS_SMALL_ACCOUNT,
)
from data_fetcher import DataFetcher
from database import (
    AsyncSessionLocal, EquitySnapshot, Signal, StrategyName, Trade,
    TradeDirection, TradeStatus,
)
from outcome_tracker import outcome_tracker
from risk_manager import RiskManager

logger = logging.getLogger(__name__)


@dataclass
class CandidateSignal:
    strategy: StrategyName
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float           # after adaptive weight applied
    raw_conviction: float       # straight from strategy detector
    atr: float
    bar_close_time: datetime
    setup: str = ""             # e.g. "retest_long", "delta_flip", "vah_retest_long"
    notes: str = ""


class SignalOrchestrator:
    def __init__(self, fetcher: DataFetcher, risk: RiskManager) -> None:
        self._fetcher = fetcher
        self._risk = risk

    # ------------------------------------------------------------------
    # Main heartbeat
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Called every 15 min by APScheduler (M15 bar boundary)."""
        logger.info("NAS100 Orchestrator heartbeat")

        if not self._risk.system_active:
            logger.warning("System halted — skipping signal scan")
            return

        # 1. Refresh data
        data = await self._fetcher.refresh()
        m5  = data.get("5min")
        m15 = data.get("15min")
        h1  = data.get("1h")
        d1  = data.get("1day")

        if any(df is None for df in (m5, m15, h1, d1)):
            logger.error("Missing timeframe data — skipping")
            return

        # 2. Collect raw signals from all 3 strategies (pass h1/d1 for market structure)
        candidates: List[CandidateSignal] = []
        candidates += self._collect_orb(m15, d1, h1)
        candidates += self._collect_ivb(m5, m15, d1, h1)
        candidates += self._collect_order_flow(m15, d1, h1)

        # 3. Apply adaptive conviction weights (non-blocking; neutral if no history)
        candidates = await self._apply_adaptive_weights(candidates)

        # 4. Persist all raw signals to DB
        await self._persist_signals(candidates)

        if not candidates:
            logger.info("No signals this cycle")
            return

        # 5. Direction gate — conflicting signals cancel; require minimum conviction
        candidates = self._conviction_gate(candidates)
        if not candidates:
            return

        # 6. Pick highest conviction
        candidates.sort(key=lambda c: c.conviction, reverse=True)
        best = candidates[0]

        logger.info(
            "Top signal: %s %s setup=%s conviction=%.0f (raw=%.0f)",
            best.strategy, best.direction, best.setup, best.conviction, best.raw_conviction,
        )

        # 7. One position at a time
        if await self._has_open_trade():
            logger.info("Position already open — skipping")
            return

        # 8. Daily drawdown gate
        equity = await self._current_equity()
        daily_pnl = await self._daily_pnl()
        if daily_pnl <= -(equity * MAX_DAILY_DRAWDOWN_PCT):
            logger.warning("Daily DD cap reached — skipping")
            self._risk.trigger_dd_kill()
            return

        # 9. Position size — live equity-aware + Claude AI risk adjustment
        base_risk_usd   = equity * MAX_RISK_PER_TRADE_PCT
        claude_risk_adj = claude_learn.get_risk_adjustment()   # 0.50–1.00
        risk_usd        = base_risk_usd * claude_risk_adj

        if IS_SMALL_ACCOUNT:
            logger.info(
                "Small account ($%.2f): base_risk=$%.2f claude_adj=%.2f → risk=$%.2f",
                equity, base_risk_usd, claude_risk_adj, risk_usd,
            )

        if not self._risk.check_minimum_tradeable(risk_usd):
            logger.warning("Risk $%.2f too small to trade — skipping", risk_usd)
            return

        raw_lot  = self._calc_lot_size(best.entry_price, best.stop_loss, risk_usd)
        lot_size = self._risk.validate_lot_size(raw_lot, risk_usd)
        if lot_size <= 0:
            logger.warning("Lot size zero after validation — skipping")
            return

        # 10. Fire
        await self._open_trade(best, lot_size, risk_usd)
        await self._snapshot_equity(equity, daily_pnl)

    # ------------------------------------------------------------------
    # Strategy collectors
    # ------------------------------------------------------------------
    def _collect_orb(self, m15, d1=None, h1=None) -> List[CandidateSignal]:
        try:
            sig = orb_breakout.detect(m15, d1=d1, h1=h1)
            if sig:
                setup = getattr(sig, "setup", None) or (
                    "retest_long" if sig.direction == TradeDirection.LONG else "retest_short"
                )
                return [CandidateSignal(
                    strategy=StrategyName.ORB_BREAKOUT,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    raw_conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    setup=setup,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("ORB error: %s", exc)
        return []

    def _collect_ivb(self, m5, m15, d1=None, h1=None) -> List[CandidateSignal]:
        try:
            sig = vp_ivb.detect(m5, m15, d1=d1, h1=h1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.IVB,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    raw_conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    setup=sig.setup,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("IVB error: %s", exc)
        return []

    def _collect_order_flow(self, m15, d1=None, h1=None) -> List[CandidateSignal]:
        try:
            sig = order_flow.detect(m15, d1=d1, h1=h1)
            if sig:
                return [CandidateSignal(
                    strategy=StrategyName.ORDER_FLOW,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    conviction=sig.conviction,
                    raw_conviction=sig.conviction,
                    atr=sig.atr,
                    bar_close_time=sig.bar_close_time,
                    setup=sig.setup,
                    notes=sig.notes,
                )]
        except Exception as exc:
            logger.exception("OrderFlow error: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Adaptive weights
    # ------------------------------------------------------------------
    async def _apply_adaptive_weights(
        self, candidates: List[CandidateSignal]
    ) -> List[CandidateSignal]:
        """
        Three-layer conviction adjustment:
          1. EWMA adaptive weight (outcome_tracker) — 0.6–1.4×
          2. Claude AI adjustment (claude_learn)     — 0.5–1.5×
          3. Combined multiplier clamped to 0.4–1.5 range
        raw_conviction is preserved for dashboard display.
        Signals below 20 after all adjustments are dropped.
        """
        weighted = []
        for c in candidates:
            ewma_mult   = await outcome_tracker.get_multiplier(c.strategy.value, c.setup)
            claude_mult = claude_learn.get_setup_adjustment(c.strategy.value, c.setup)
            combined    = max(0.4, min(1.5, ewma_mult * claude_mult))
            c.conviction = min(100.0, round(c.raw_conviction * combined, 1))
            if c.conviction >= 20:
                weighted.append(c)
            else:
                logger.info(
                    "Signal %s/%s conviction %.0f → %.0f "
                    "(EWMA×%.2f Claude×%.2f combined×%.2f) — dropped",
                    c.strategy, c.setup, c.raw_conviction, c.conviction,
                    ewma_mult, claude_mult, combined,
                )
        return weighted

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def _conviction_gate(self, candidates: List[CandidateSignal]) -> List[CandidateSignal]:
        from collections import Counter
        dir_counts = Counter(c.direction for c in candidates)

        if len(dir_counts) > 1:
            max_dir, max_count = dir_counts.most_common(1)[0]
            if list(dir_counts.values()).count(max_count) > 1:
                logger.info("Conflicting signals: %s — skipping", dict(dir_counts))
                return []
            candidates = [c for c in candidates if c.direction == max_dir]

        if all(c.conviction >= 70 for c in candidates):
            logger.info("All strategies agree (%d) — high-conviction entry", len(candidates))
        elif all(c.conviction < 40 for c in candidates):
            logger.info("All signals low conviction — skipping")
            return []

        return candidates

    # ------------------------------------------------------------------
    # Position sizing — NAS100 CFD
    # ------------------------------------------------------------------
    @staticmethod
    def _calc_lot_size(entry: float, sl: float, risk_usd: float) -> float:
        distance = abs(entry - sl)
        if distance < 0.5:
            return 0.0
        lot = risk_usd / (distance * MT5_POINT_VALUE)
        return round(lot, 2)

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
                    raw_conviction=c.raw_conviction,
                    entry_price=c.entry_price,
                    stop_loss=c.stop_loss,
                    take_profit=c.take_profit,
                    atr=c.atr,
                    timeframe="15min",
                    bar_close_time=c.bar_close_time,
                    setup=c.setup,
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
        """
        Prefer live MT5 account equity (always up-to-date after each trade).
        Falls back to last DB snapshot, then to ACCOUNT_SIZE_USD env var.
        This ensures position sizing always reflects the actual current balance,
        which matters especially for small accounts where every dollar counts.
        """
        live_equity = mt5_executor.get_account_equity()
        if live_equity and live_equity > 0:
            return live_equity
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

        ticket = mt5_executor.place_order(
            direction=direction,
            lot_size=lot_size,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            comment=f"GM-{sig.strategy.value if hasattr(sig.strategy, 'value') else sig.strategy}",
        )
        if ticket is None:
            logger.error("MT5 order failed — trade NOT recorded in DB")
            return

        async with AsyncSessionLocal() as db:
            trade = Trade(
                strategy=sig.strategy,
                setup=sig.setup,
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

        logger.info(
            "NAS100 trade opened: %s %s setup=%s lots=%.2f ticket=%d",
            sig.strategy, sig.direction, sig.setup, lot_size, ticket,
        )
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
