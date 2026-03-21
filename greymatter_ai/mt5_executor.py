"""
GreymatterAI — MetaTrader 5 Executor
Handles MT5 connection, order placement, and position management.

Requirements:
  pip install MetaTrader5
  - Windows only (MT5 Python library is Windows-exclusive)
  - MetaTrader 5 terminal must be running and logged in
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not installed — MT5 execution disabled. Run: pip install MetaTrader5")

from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_SYMBOL, MT5_LOT_DIVISOR

_MAGIC = 20250101  # EA magic number to identify our trades


class MT5Executor:
    """Manages the MT5 terminal connection and all order operations."""

    def __init__(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """Initialize connection to the MT5 terminal. Returns True on success."""
        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 package not available — install it with: pip install MetaTrader5")
            return False

        if not MT5_LOGIN or not MT5_PASSWORD or not MT5_SERVER:
            logger.error("MT5 credentials missing — set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER in .env")
            return False

        if not mt5.initialize(
            login=int(MT5_LOGIN),
            password=MT5_PASSWORD,
            server=MT5_SERVER,
        ):
            logger.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        info = mt5.account_info()
        if info is None:
            logger.error("MT5 account_info() unavailable: %s", mt5.last_error())
            mt5.shutdown()
            return False

        self._connected = True
        logger.info(
            "MT5 connected — account=%d  server=%s  equity=%.2f  currency=%s",
            info.login, info.server, info.equity, info.currency,
        )
        return True

    def disconnect(self) -> None:
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected")

    def _ensure_connected(self) -> bool:
        if not self._connected:
            return self.connect()
        return True

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def place_order(
        self,
        direction: str,       # "long" or "short"
        lot_size_oz: float,   # oz calculated by orchestrator
        stop_loss: float,
        take_profit: float,
        comment: str = "GreymatterAI",
    ) -> Optional[int]:
        """
        Place a market order. Returns the MT5 ticket number, or None on failure.

        Lot conversion: MT5 lots = lot_size_oz / MT5_LOT_DIVISOR
        Default: 1 MT5 lot = 100 oz  →  MT5_LOT_DIVISOR=100
        Adjust MT5_LOT_DIVISOR in .env if your broker uses a different contract size.
        """
        if not self._ensure_connected():
            return None

        # Convert oz → MT5 lots, enforce broker minimum of 0.01
        mt5_lots = round(lot_size_oz / MT5_LOT_DIVISOR, 2)
        mt5_lots = max(mt5_lots, 0.01)

        # Make sure the symbol is visible in Market Watch
        symbol_info = mt5.symbol_info(MT5_SYMBOL)
        if symbol_info is None:
            logger.error("Symbol %s not found in MT5", MT5_SYMBOL)
            return None
        if not symbol_info.visible:
            if not mt5.symbol_select(MT5_SYMBOL, True):
                logger.error("Failed to select symbol %s in Market Watch", MT5_SYMBOL)
                return None

        # Get live bid/ask
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
        if tick is None:
            logger.error("Could not get tick data for %s", MT5_SYMBOL)
            return None

        if direction == "long":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      MT5_SYMBOL,
            "volume":      mt5_lots,
            "type":        order_type,
            "price":       price,
            "sl":          round(stop_loss, 2),
            "tp":          round(take_profit, 2),
            "deviation":   20,          # max slippage in points
            "magic":       _MAGIC,
            "comment":     comment[:31],  # MT5 limits comment to 31 chars
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = result.retcode if result else "None"
            comment_mt5 = result.comment if result else "no result"
            logger.error("MT5 order_send failed: retcode=%s  comment=%s", retcode, comment_mt5)
            return None

        logger.info(
            "MT5 order placed — ticket=%d  %s  %s  lots=%.2f  SL=%.2f  TP=%.2f",
            result.order, MT5_SYMBOL, direction.upper(), mt5_lots, stop_loss, take_profit,
        )
        return result.order

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def close_position(self, ticket: int) -> bool:
        """Force-close an open position by ticket. Used by the kill-switch."""
        if not self._ensure_connected():
            return False

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning("No open MT5 position for ticket %d", ticket)
            return False

        pos = positions[0]
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
        if tick is None:
            return False

        # Closing a BUY uses SELL, closing a SELL uses BUY
        if pos.type == mt5.POSITION_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      MT5_SYMBOL,
            "volume":      pos.volume,
            "type":        close_type,
            "position":    ticket,
            "price":       price,
            "deviation":   20,
            "magic":       _MAGIC,
            "comment":     "GreymatterAI close",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("MT5 close failed — ticket=%d  retcode=%s", ticket, result.retcode if result else "None")
            return False

        logger.info("MT5 position closed — ticket=%d", ticket)
        return True

    def is_position_open(self, ticket: int) -> bool:
        """Returns True if the MT5 position is still open."""
        if not self._ensure_connected():
            return True  # assume open if we can't check, to avoid double-close
        positions = mt5.positions_get(ticket=ticket)
        return bool(positions)

    def get_closed_deal_info(self, ticket: int) -> Optional[tuple[float, float]]:
        """
        Look up the realized close price and P&L for a position that was
        closed by MT5 (SL/TP hit). Returns (close_price, pnl_usd) or None.
        """
        if not self._ensure_connected():
            return None
        from datetime import datetime, timezone, timedelta
        # Search deals in the last 7 days to find the closing deal
        date_from = datetime.now(timezone.utc) - timedelta(days=7)
        date_to = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return None
        # The closing deal has position_id == ticket and entry == DEAL_ENTRY_OUT
        DEAL_ENTRY_OUT = 1
        for deal in deals:
            if deal.position_id == ticket and deal.entry == DEAL_ENTRY_OUT:
                return (deal.price, deal.profit)
        return None

    # ------------------------------------------------------------------
    # Account info
    # ------------------------------------------------------------------
    def get_account_equity(self) -> Optional[float]:
        """Return live account equity from MT5."""
        if not self._ensure_connected():
            return None
        info = mt5.account_info()
        return float(info.equity) if info else None

    def get_open_tickets(self) -> list[int]:
        """Return ticket numbers of all our open XAUUSD positions (by magic number)."""
        if not self._ensure_connected():
            return []
        positions = mt5.positions_get(symbol=MT5_SYMBOL)
        if positions is None:
            return []
        return [p.ticket for p in positions if p.magic == _MAGIC]


# Module-level singleton — import this everywhere
executor = MT5Executor()
