"""
MT5 Gateway — Windows Side ONLY.
Pure MT5 API wrapper. Returns plain dicts. Zero business logic.
"""
from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 not available (expected on WSL — ignore).")

TIMEFRAME_MAP: Dict[str, int] = {}
if MT5_AVAILABLE:
    TIMEFRAME_MAP = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
    }


class MT5Gateway:
    """
    Thin, stateless wrapper over the MetaTrader5 Python package.
    All methods return plain dicts / lists / primitives (JSON-safe).
    Includes auto-reconnect on every public call.
    """

    def __init__(
        self,
        login:    int,
        password: str,
        server:   str,
        mt5_path: str = "",
    ) -> None:
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 package not installed. Run on Windows.")
        self.login    = login
        self.password = password
        self.server   = server
        self.mt5_path = mt5_path
        self._ready   = False

    # ── Lifecycle ─────────────────────────────────────────────────

    def connect(self, retries: int = 3, delay: float = 5.0) -> bool:
        """
        Uses the exact same pattern as known-working MT5 Python code:
            mt5.initialize(login=, password=, server=, timeout=)
        No path= unless explicitly set — passing path= to a running
        terminal causes -6 Authorization errors.
        """
        import time as _time
        for attempt in range(1, retries + 1):
            logger.info("MT5 connect attempt %d/%d …", attempt, retries)
            kwargs: dict = {
                "login":    self.login,
                "password": self.password,
                "server":   self.server,
                "timeout":  60000,
            }
            if self.mt5_path:
                kwargs["path"] = self.mt5_path

            if not mt5.initialize(**kwargs):
                code, msg = mt5.last_error()
                logger.error("mt5.initialize() failed: (%s, %s)", code, msg)
                if code == -10005:
                    logger.error("Terminal not running. Open MT5, log in, wait for charts.")
                elif code == -6:
                    logger.error("Auth failed. Run as same user/admin level as MT5.")
                elif code == -2:
                    logger.error("Bad server name '%s'. Check File→Open Account in MT5.", self.server)
                if attempt < retries:
                    logger.info("Retrying in %.0fs …", delay)
                    _time.sleep(delay)
                continue

            acc = mt5.account_info()
            if acc is None:
                logger.error("initialize() OK but account_info() None: %s", mt5.last_error())
                mt5.shutdown()
                if attempt < retries:
                    _time.sleep(delay)
                continue

            self._ready = True
            logger.info(
                "MT5 connected ✓  account=%s  name=%s  balance=%.2f %s  server=%s",
                acc.login, acc.name, acc.balance, acc.currency, acc.server,
            )
            return True

        logger.critical("All %d MT5 connection attempts failed.", retries)
        return False

    def disconnect(self) -> None:
        mt5.shutdown()
        self._ready = False
        logger.info("MT5 disconnected.")

    def is_ready(self) -> bool:
        if not self._ready:
            return False
        return mt5.account_info() is not None

    def _ensure_ready(self) -> bool:
        """Auto-reconnect if MT5 dropped. Called before every data request."""
        if self._ready and mt5.account_info() is not None:
            return True
        logger.warning("MT5 not ready — attempting reconnect …")
        self._ready = False
        return self.connect(retries=2, delay=2.0)

    # ── Account ───────────────────────────────────────────────────

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "login":       int(info.login),
            "balance":     float(info.balance),
            "equity":      float(info.equity),
            "margin":      float(info.margin),
            "free_margin": float(info.margin_free),
            "currency":    str(info.currency),
            "leverage":    int(info.leverage),
            "server":      str(info.server),
            "name":        str(info.name),
        }

    # ── Market Data ───────────────────────────────────────────────

    def get_candles(
        self, symbol: str, timeframe: str, count: int
    ) -> Optional[List[Dict[str, Any]]]:
        if not self._ensure_ready():
            return None
        tf = TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error("Unknown timeframe: %s", timeframe)
            return None
        # Ensure symbol is in Market Watch
        if not mt5.symbol_select(symbol, True):
            logger.error("Failed to select symbol %s", symbol)
            return None
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.error("copy_rates_from_pos empty %s/%s: %s", symbol, timeframe, mt5.last_error())
            return None
        result = []
        for r in rates:
            # prefer real_volume; fall back to tick_volume
            vol = float(r["real_volume"]) if r["real_volume"] > 0 else float(r["tick_volume"])
            result.append({
                "time":   int(r["time"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": vol,
            })
        return result

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("symbol_info(%s) None: %s", symbol, mt5.last_error())
            return None
        return {
            "symbol":              str(info.name),
            "digits":              int(info.digits),
            "point":               float(info.point),
            "trade_tick_value":    float(info.trade_tick_value),
            "trade_tick_size":     float(info.trade_tick_size),
            "trade_contract_size": float(info.trade_contract_size),
            "spread":              int(info.spread),
            "volume_min":          float(info.volume_min),
            "volume_max":          float(info.volume_max),
            "volume_step":         float(info.volume_step),
        }

    def get_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "bid":  float(tick.bid),
            "ask":  float(tick.ask),
            "last": float(tick.last),
            "time": int(tick.time),
        }

    def get_server_time(self) -> Optional[int]:
        if not self._ensure_ready():
            return None
        tick = mt5.symbol_info_tick("EURUSD")
        return int(tick.time) if tick else None

    # ── Order Execution ───────────────────────────────────────────

    def place_order(self, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        order_type = mt5.ORDER_TYPE_BUY if order["type"] == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       str(order["symbol"]),
            "volume":       float(order["volume"]),
            "type":         order_type,
            "price":        float(order["price"]),
            "sl":           float(order["sl"]),
            "tp":           float(order["tp"]),
            "deviation":    int(order.get("deviation", 20)),
            "magic":        int(order.get("magic", 202400)),
            "comment":      str(order.get("comment", "ZoneBot")),
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("order_send failed: retcode=%s request=%s", 
                         result.retcode if result else "None", request)
            return None
        return {
            "ticket":  int(result.order),
            "retcode": int(result.retcode),
            "price":   float(result.price),
            "volume":  float(result.volume),
        }

    def close_position(self, ticket: int) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.error("No open position with ticket %s", ticket)
            return None
        pos = positions[0]
        close_type  = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick        = mt5.symbol_info_tick(pos.symbol)
        close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    20,
            "magic":        pos.magic,
            "comment":      "ZoneBot Close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("close_position failed ticket=%s: %s", ticket, result)
            return None
        return {"ticket": int(ticket), "retcode": int(result.retcode), "price": float(close_price)}

    def modify_position(self, ticket: int, sl: float, tp: float) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        result = mt5.order_send({
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       float(sl),
            "tp":       float(tp),
        })
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("modify_position failed ticket=%s: %s", ticket, result)
            return None
        return {"ticket": int(ticket), "retcode": int(result.retcode)}

    # ── Positions ─────────────────────────────────────────────────

    def get_open_positions(self) -> List[Dict[str, Any]]:
        if not self._ensure_ready():
            return []
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":     int(p.ticket),
                "symbol":     str(p.symbol),
                "type":       "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume":     float(p.volume),
                "open_price": float(p.price_open),
                "sl":         float(p.sl),
                "tp":         float(p.tp),
                "profit":     float(p.profit),
                "swap":       float(p.swap),
                "open_time":  int(p.time),
                "magic":      int(p.magic),
                "comment":    str(p.comment),
            }
            for p in positions
        ]

    def get_position_by_ticket(self, ticket: int) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return None
        p = positions[0]
        return {
            "ticket":     int(p.ticket),
            "symbol":     str(p.symbol),
            "type":       "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
            "volume":     float(p.volume),
            "open_price": float(p.price_open),
            "sl":         float(p.sl),
            "tp":         float(p.tp),
            "profit":     float(p.profit),
            "open_time":  int(p.time),
        }
    
    def get_candles_range(
        self,
        symbol:    str,
        timeframe: str,
        start_dt:  datetime,
        end_dt:    datetime,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch all candles between two UTC datetimes. No bar-count cap."""
        if not self._ensure_ready():
            return None
        tf = TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error("Unknown timeframe: %s", timeframe)
            return None
        if not mt5.symbol_select(symbol, True):
            logger.error("Failed to select symbol %s", symbol)
            return None

        rates = mt5.copy_rates_range(symbol, tf, start_dt, end_dt)
        if rates is None or len(rates) == 0:
            logger.error("copy_rates_range empty %s/%s: %s", symbol, timeframe, mt5.last_error())
            return None

        result = []
        for r in rates:
            vol = float(r["real_volume"]) if r["real_volume"] > 0 else float(r["tick_volume"])
            result.append({
                "time":   int(r["time"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": vol,
            })
        return result