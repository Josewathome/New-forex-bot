"""
trade_repo.py — Windows-native MT5 Trade Execution.
Implements ITradeRepository by calling MT5Gateway directly.
No ZMQ bridge. Runs on Windows where MetaTrader5 is installed.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import List, Optional

from src.domain.entities import Direction, Trade, TradeStatus
from src.domain.repositories import ITradeRepository
from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway

logger = logging.getLogger(__name__)


class MT5TradeRepository(ITradeRepository):
    """Sends trade commands directly to MT5Gateway."""

    def __init__(self, gateway: MT5Gateway) -> None:
        self._gw = gateway

    # ── ITradeRepository ──────────────────────

    def place_trade(self, trade: Trade) -> Optional[int]:
        order = {
            "symbol":  trade.symbol,
            "type":    "BUY" if trade.direction == Direction.BULLISH else "SELL",
            "volume":  round(trade.lot_size, 2),
            "price":   trade.entry_price,
            "sl":      trade.stop_loss,
            "tp":      trade.take_profit,
            "comment": f"ZoneBot-{trade.id[:8]}",
        }
        result = self._gw.place_order(order)
        if result is None:
            logger.error("place_trade failed for %s", trade.id)
            return None
        logger.info(
            "Trade placed ✓ ticket=%s symbol=%s type=%s vol=%.2f",
            result["ticket"], trade.symbol, order["type"], order["volume"],
        )
        return result["ticket"]

    def close_trade(self, ticket: int) -> bool:
        result = self._gw.close_position(ticket)
        if result is None:
            logger.error("close_trade failed for ticket=%s", ticket)
            return False
        logger.info("Trade closed ✓ ticket=%s price=%s", ticket, result.get("price"))
        return True

    def get_open_positions(self) -> List[Trade]:
        raw_positions = self._gw.get_open_positions()
        trades = []
        for p in raw_positions:
            direction = Direction.BULLISH if p["type"] == "BUY" else Direction.BEARISH
            open_dt   = datetime.fromtimestamp(p["open_time"], tz=timezone.utc)
            trades.append(Trade(
                id=          str(p["ticket"]),
                symbol=      p["symbol"],
                direction=   direction,
                entry_price= p["open_price"],
                stop_loss=   p["sl"],
                take_profit= p["tp"],
                lot_size=    p["volume"],
                status=      TradeStatus.OPEN,
                created_at=  open_dt,
                mt5_ticket=  p["ticket"],
            ))
        return trades

    def get_account_balance(self) -> float:
        info = self._gw.get_account_info()
        return info["balance"] if info else 0.0