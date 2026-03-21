"""
GreymatterAI — Alert Manager (Telegram)
Sends formatted alerts for signals, trade opens/closes, and system events.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


async def _send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping alert")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
            if resp.status_code != 200:
                logger.warning("Telegram error %d: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


async def send_signal_alert(strategy: str, direction: str, conviction: float, entry: float, sl: float, tp: float, notes: str = "") -> None:
    emoji = "🟢" if direction == "long" else "🔴"
    msg = (
        f"{emoji} <b>SIGNAL — XAUUSD</b>\n"
        f"Strategy: <code>{strategy}</code>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Conviction: <b>{conviction:.0f}/100</b>\n"
        f"Entry: <code>{entry:.2f}</code>\n"
        f"SL: <code>{sl:.2f}</code>\n"
        f"TP: <code>{tp:.2f}</code>\n"
        + (f"Notes: {notes}" if notes else "")
    )
    await _send(msg)


async def send_trade_alert(trade: Any, sig: Any, lot_size: float, risk_usd: float) -> None:
    direction = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)
    emoji = "🟢" if "long" in direction else "🔴"
    msg = (
        f"{emoji} <b>TRADE OPENED — XAUUSD</b>\n"
        f"Strategy: <code>{sig.strategy.value if hasattr(sig.strategy, 'value') else sig.strategy}</code>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Entry: <code>{sig.entry_price:.2f}</code>\n"
        f"SL: <code>{sig.stop_loss:.2f}</code>\n"
        f"TP: <code>{sig.take_profit:.2f}</code>\n"
        f"Lot: <code>{lot_size:.2f} oz</code>  Risk: <code>${risk_usd:.0f}</code>\n"
        f"Conviction: <b>{sig.conviction:.0f}/100</b>"
    )
    await _send(msg)


async def send_close_alert(strategy: str, direction: str, pnl_usd: float, pnl_r: float) -> None:
    emoji = "✅" if pnl_usd >= 0 else "❌"
    sign = "+" if pnl_usd >= 0 else ""
    msg = (
        f"{emoji} <b>TRADE CLOSED — XAUUSD</b>\n"
        f"Strategy: <code>{strategy}</code>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"P&amp;L: <b>{sign}{pnl_usd:.2f} USD</b>  ({sign}{pnl_r:.2f}R)"
    )
    await _send(msg)


async def send_kill_switch_alert(reason: str) -> None:
    msg = (
        f"🚨 <b>KILL-SWITCH FIRED</b>\n"
        f"Reason: {reason}\n"
        f"System is now HALTED. Manual resume required."
    )
    await _send(msg)


async def send_system_event(event_type: str, detail: str) -> None:
    msg = f"ℹ️ <b>SYSTEM EVENT</b>: {event_type}\n{detail}"
    await _send(msg)
