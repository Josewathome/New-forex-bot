"""
Zone Repository — In-memory implementation with optional JSON persistence.
Stores active Zones and LiquidityPools produced by the analysis engine.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.domain.entities import (
    Direction, LiquidityPool, Timeframe, Zone, ZoneType,
)
from src.domain.repositories import IZoneRepository

logger = logging.getLogger(__name__)

PERSIST_PATH = Path(__file__).parent.parent.parent / ".cache" / "zones.json"


class InMemoryZoneRepository(IZoneRepository):
    """
    Fast in-memory store; optionally persists to disk on save so zones
    survive bot restarts within the same trading session.
    """

    def __init__(self, persist: bool = True) -> None:
        self._zones:  Dict[str, Zone]         = {}
        self._pools:  Dict[str, LiquidityPool] = {}
        self._persist = persist
        if persist and PERSIST_PATH.exists():
            self._load_from_disk()

    # ── IZoneRepository ───────────────────────

    def save_zone(self, zone: Zone) -> None:
        self._zones[zone.id] = zone
        self._flush()

    def get_active_zones(self, symbol: str) -> List[Zone]:
        return [z for z in self._zones.values() if z.symbol == symbol and z.is_active]

    def get_all_zones(self) -> List[Zone]:
        return list(self._zones.values())

    def invalidate_zone(self, zone_id: str) -> None:
        if zone_id in self._zones:
            self._zones[zone_id].is_active = False
        self._flush()

    def save_liquidity_pool(self, pool: LiquidityPool) -> None:
        self._pools[pool.symbol] = pool
        self._flush()

    def get_liquidity_pool(self, symbol: str) -> Optional[LiquidityPool]:
        return self._pools.get(symbol)

    # ── Persistence helpers ───────────────────

    def _flush(self) -> None:
        if not self._persist:
            return
        try:
            PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "zones":  [self._zone_to_dict(z) for z in self._zones.values()],
                "pools":  [self._pool_to_dict(p) for p in self._pools.values()],
            }
            with open(PERSIST_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            logger.warning("Zone persist failed: %s", exc)

    def _load_from_disk(self) -> None:
        try:
            with open(PERSIST_PATH, "r") as f:
                data = json.load(f)
            for zd in data.get("zones", []):
                z = self._dict_to_zone(zd)
                self._zones[z.id] = z
            for pd in data.get("pools", []):
                p = self._dict_to_pool(pd)
                self._pools[p.symbol] = p
            logger.info("Loaded %d zones, %d pools from disk.", len(self._zones), len(self._pools))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load zones from disk: %s", exc)

    # ── Serialisation ─────────────────────────

    def _zone_to_dict(self, z: Zone) -> dict:
        return {
            "id":                  z.id,
            "symbol":              z.symbol,
            "zone_type":           z.zone_type.value,
            "direction":           z.direction.value,
            "high":                z.high,
            "low":                 z.low,
            "created_at":          z.created_at.isoformat(),
            "timeframe":           z.timeframe.value,
            "is_active":           z.is_active,
            "touch_count":         z.touch_count,
            "source_candle_index": z.source_candle_index,
        }

    def _dict_to_zone(self, d: dict) -> Zone:
        return Zone(
            id=                  d["id"],
            symbol=              d["symbol"],
            zone_type=           ZoneType(d["zone_type"]),
            direction=           Direction(d["direction"]),
            high=                d["high"],
            low=                 d["low"],
            created_at=          datetime.fromisoformat(d["created_at"]),
            timeframe=           Timeframe(d["timeframe"]),
            is_active=           d.get("is_active", True),
            touch_count=         d.get("touch_count", 0),
            source_candle_index= d.get("source_candle_index"),
        )

    def _pool_to_dict(self, p: LiquidityPool) -> dict:
        return {
            "symbol":           p.symbol,
            "high":             p.high,
            "low":              p.low,
            "lookback_candles": p.lookback_candles,
            "created_at":       p.created_at.isoformat(),
            "timeframe":        p.timeframe.value,
        }

    def _dict_to_pool(self, d: dict) -> LiquidityPool:
        return LiquidityPool(
            symbol=           d["symbol"],
            high=             d["high"],
            low=              d["low"],
            lookback_candles= d["lookback_candles"],
            created_at=       datetime.fromisoformat(d["created_at"]),
            timeframe=        Timeframe(d["timeframe"]),
        )