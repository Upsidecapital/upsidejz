"""
GreymatterAI — APScheduler (NAS100)
Wires up the 15-min heartbeat (M15 bar close), trade-monitor, and 4-hour optimisation cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import HEARTBEAT_SECONDS, OPTIMIZATION_INTERVAL_HOURS
from data_fetcher import DataFetcher
from mt5_executor import executor as mt5_executor
from risk_manager import RiskManager
from signal_orchestrator import SignalOrchestrator

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_fetcher: DataFetcher | None = None
_risk: RiskManager | None = None
_orchestrator: SignalOrchestrator | None = None


async def _heartbeat() -> None:
    assert _orchestrator is not None
    try:
        await _orchestrator.run()
    except Exception as exc:
        logger.exception("Heartbeat error: %s", exc)


async def _monitor_open_trades() -> None:
    """
    Check open trades against MT5 positions.
    If MT5 has closed a position (SL/TP hit), sync the result to the DB.
    Falls back to M15 price simulation if the trade has no MT5 ticket.
    """
    assert _fetcher is not None
    assert _risk is not None

    from database import AsyncSessionLocal, Trade, TradeStatus
    from sqlalchemy import select
    from alert_manager import send_close_alert

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Trade).where(Trade.status == TradeStatus.OPEN)
        )
        open_trades = result.scalars().all()

    if not open_trades:
        return

    for trade in open_trades:
        closed = False
        win = False
        close_price = None
        pnl_usd = None

        if trade.mt5_ticket:
            # --- MT5 path: check if the position is still open ---
            still_open = mt5_executor.is_position_open(trade.mt5_ticket)
            if not still_open:
                # Position closed in MT5 (SL/TP hit or manual close)
                deal_info = mt5_executor.get_closed_deal_info(trade.mt5_ticket)
                if deal_info:
                    close_price, pnl_usd = deal_info
                else:
                    # Fallback: estimate from M15 close
                    m15 = _fetcher.get("15min")
                    close_price = float(m15.iloc[-1]["close"]) if m15 is not None and len(m15) > 0 else trade.entry_price
                    pnl_usd = (close_price - trade.entry_price) * trade.lot_size
                    if trade.direction.value == "short":
                        pnl_usd = -pnl_usd
                win = pnl_usd >= 0
                closed = True
        else:
            # --- Simulation fallback for trades without an MT5 ticket ---
            m15 = _fetcher.get("15min")
            if m15 is None or len(m15) == 0:
                continue
            current_price = float(m15.iloc[-1]["close"])
            from config import MT5_POINT_VALUE
            if trade.direction.value == "long":
                if current_price <= trade.stop_loss:
                    closed, win = True, False
                elif current_price >= trade.take_profit:
                    closed, win = True, True
            else:
                if current_price >= trade.stop_loss:
                    closed, win = True, False
                elif current_price <= trade.take_profit:
                    closed, win = True, True
            if closed:
                close_price = current_price
                dist = close_price - trade.entry_price
                if trade.direction.value == "short":
                    dist = -dist
                pnl_usd = dist * trade.lot_size * MT5_POINT_VALUE

        if closed and close_price is not None and pnl_usd is not None:
            pnl_r = pnl_usd / trade.risk_usd if trade.risk_usd else 0.0
            status = TradeStatus.CLOSED_WIN if win else TradeStatus.CLOSED_LOSS

            async with AsyncSessionLocal() as db:
                t = await db.get(Trade, trade.id)
                if t:
                    t.status = status
                    t.close_price = close_price
                    t.pnl_usd = pnl_usd
                    t.pnl_r = pnl_r
                    t.closed_at = datetime.now(timezone.utc)
                    await db.commit()

            if win:
                _risk.on_trade_win()
            else:
                _risk.on_trade_loss()

            await send_close_alert(
                strategy=trade.strategy.value,
                direction=trade.direction.value,
                pnl_usd=pnl_usd,
                pnl_r=pnl_r,
            )
            logger.info("Trade %d closed %s pnl=%.2f", trade.id, status.value, pnl_usd)

            # Dead-market detection: ATR < 20% of 50-bar average
            atr = _fetcher.compute_atr("15min")
            baseline = _fetcher.compute_atr("1h")
            if atr and baseline and atr < baseline * 0.20:
                _risk.trigger_dead_market_kill()
                from alert_manager import send_kill_switch_alert
                import asyncio
                asyncio.create_task(send_kill_switch_alert("Dead market detected"))


async def _run_optimisation() -> None:
    """Walk-forward optimisation — refreshes strategy params every 4h."""
    try:
        from optimiser import run_walk_forward
        await run_walk_forward()
    except Exception as exc:
        logger.exception("Optimisation error: %s", exc)


async def start_scheduler() -> None:
    global _scheduler, _fetcher, _risk, _orchestrator
    _fetcher = DataFetcher()
    _risk = RiskManager()
    _orchestrator = SignalOrchestrator(_fetcher, _risk)

    connected = mt5_executor.connect()
    if connected:
        logger.info("MT5 executor connected and ready")
    else:
        logger.warning("MT5 executor NOT connected — signals will be generated but orders will NOT be placed")

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _heartbeat,
        IntervalTrigger(seconds=HEARTBEAT_SECONDS),
        id="heartbeat",
        next_run_time=datetime.now(timezone.utc),  # run immediately on start
    )
    _scheduler.add_job(
        _monitor_open_trades,
        IntervalTrigger(minutes=5),
        id="trade_monitor",
    )
    _scheduler.add_job(
        _run_optimisation,
        IntervalTrigger(hours=OPTIMIZATION_INTERVAL_HOURS),
        id="optimisation",
    )
    _scheduler.start()
    logger.info("Scheduler started")


async def stop_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _fetcher:
        await _fetcher.close()
    mt5_executor.disconnect()
    logger.info("Scheduler stopped")
