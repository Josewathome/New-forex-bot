"""
cleanup_service.py — Automated retention policy enforcement.

Retention rules (all configurable in config.py):
    Logs   : 3 days
    Cache  : 2 days
    Zones  : 5 days
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import src.config as config

logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(self) -> None:
        self.log_dir   = Path("logs")
        self.cache_dir = Path(".cache")
        self.zones_dir = Path(getattr(config, "ZONES_DIR", "zones"))

        self.log_days   = getattr(config, "LOG_RETENTION_DAYS",   3)
        self.cache_days = getattr(config, "CACHE_RETENTION_DAYS", 2)
        self.zone_days  = getattr(config, "ZONE_RETENTION_DAYS",  5)

    # ── Public ──────────────────────────────────────────────────────

    def run_all(self) -> None:
        """Run all cleanup passes. Called daily by the scheduler."""
        logger.info("CleanupService: starting daily retention pass.")
        self._purge(self.log_dir,   self.log_days,   "logs")
        self._purge(self.cache_dir, self.cache_days, "cache")
        self._purge(self.zones_dir, self.zone_days,  "zones")
        logger.info("CleanupService: retention pass complete.")

    def purge_zones(self, symbol: str | None = None) -> None:
        """Purge old zone files for one symbol or all symbols."""
        target = (self.zones_dir / symbol) if symbol else self.zones_dir
        self._purge(target, self.zone_days, f"zones/{symbol or '*'}")

    # ── Internal ────────────────────────────────────────────────────

    def _purge(self, directory: Path, days: int, label: str) -> None:
        if not directory.exists():
            return
        cutoff  = datetime.now(tz=timezone.utc) - timedelta(days=days)
        removed = 0
        errors  = 0
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Cleanup: could not delete %s — %s", f, exc)
                    errors += 1
        if removed or errors:
            logger.info(
                "Cleanup [%-8s]: removed %d file(s) older than %d days  (errors=%d)",
                label, removed, days, errors,
            )