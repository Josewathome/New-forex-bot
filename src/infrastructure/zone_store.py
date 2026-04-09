"""
zone_store.py — Per-symbol timestamped zone snapshot files.

File layout:
    zones/
        EURUSD/
            zones_2026-04-09T08-30-00.json
            zones_2026-04-08T08-30-00.json
        GBPUSD/
            zones_2026-04-09T08-30-00.json

Old files beyond ZONE_RETENTION_DAYS are auto-pruned after each write.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import src.config as config
from src.domain.entities import Direction, LiquidityPool, Timeframe, Zone, ZoneType

logger = logging.getLogger(__name__)


class ZoneFileStore:
    """
    Writes and manages zone snapshot files.
    This is supplementary to InMemoryZoneRepository — it provides
    an auditable on-disk history of zone evolution per pair.
    """

    def __init__(
        self,
        zones_dir:      str | None = None,
        retention_days: int | None = None,
    ) -> None:
        self.root = Path(zones_dir or getattr(config, "ZONES_DIR", "zones"))
        self.retention_days = retention_days or getattr(config, "ZONE_RETENTION_DAYS", 5)

    # ── Public ──────────────────────────────────────────────────────

    def save_snapshot(
        self,
        symbol: str,
        zones:  List[Zone],
        pool:   Optional[LiquidityPool] = None,
    ) -> Path:
        """Write a zone snapshot file and prune old ones."""
        sym_dir = self.root / symbol
        sym_dir.mkdir(parents=True, exist_ok=True)

        ts   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        path = sym_dir / f"zones_{ts}.json"

        payload: Dict[str, Any] = {
            "symbol":        symbol,
            "timestamp":     datetime.now(tz=timezone.utc).isoformat(),
            "zone_count":    len(zones),
            "zones":         [self._zone_to_dict(z) for z in zones],
            "liquidity_pool": self._pool_to_dict(pool) if pool else None,
        }

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

        logger.debug(
            "ZoneFileStore: saved %d zones for %s → %s",
            len(zones), symbol, path.name,
        )
        self._prune(sym_dir)
        return path

    def list_snapshots(self, symbol: str) -> List[Path]:
        """Return snapshot files for a symbol, newest first."""
        sym_dir = self.root / symbol
        if not sym_dir.exists():
            return []
        return sorted(sym_dir.glob("zones_*.json"), reverse=True)

    def prune_all(self) -> None:
        """Prune old files for every symbol directory."""
        if not self.root.exists():
            return
        for sym_dir in self.root.iterdir():
            if sym_dir.is_dir():
                self._prune(sym_dir)

    # ── Internal ────────────────────────────────────────────────────

    def _prune(self, sym_dir: Path) -> None:
        cutoff  = datetime.now(tz=timezone.utc) - timedelta(days=self.retention_days)
        removed = 0
        for f in sym_dir.glob("zones_*.json"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("ZoneFileStore prune: %s — %s", f, exc)
        if removed:
            logger.debug("ZoneFileStore: pruned %d old file(s) in %s", removed, sym_dir.name)

    @staticmethod
    def _zone_to_dict(z: Zone) -> Dict[str, Any]:
        return {
            "id":           z.id,
            "symbol":       z.symbol,
            "zone_type":    z.zone_type.value,
            "direction":    z.direction.value,
            "high":         z.high,
            "low":          z.low,
            "created_at":   z.created_at.isoformat(),
            "timeframe":    z.timeframe.value,
            "is_active":    z.is_active,
            "touch_count":  z.touch_count,
        }

    @staticmethod
    def _pool_to_dict(p: LiquidityPool) -> Dict[str, Any]:
        return {
            "symbol":          p.symbol,
            "high":            p.high,
            "low":             p.low,
            "lookback_candles": p.lookback_candles,
            "created_at":      p.created_at.isoformat(),
            "timeframe":       p.timeframe.value,
        }