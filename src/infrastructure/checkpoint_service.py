"""
checkpoint_service.py — Bot state persistence.

Saves a JSON snapshot of critical runtime state so the bot can
resume after a restart without losing context.

Layout:
    .checkpoints/
        checkpoint_latest.json    ← always the most recent
        checkpoint_2026-04-09.json ← daily backup
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CheckpointService:
    def __init__(self, checkpoint_dir: str = ".checkpoints") -> None:
        self.dir    = Path(checkpoint_dir)
        self.latest = self.dir / "checkpoint_latest.json"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── Public ──────────────────────────────────────────────────────

    def save(self, state: Dict[str, Any]) -> bool:
        """
        Persist bot state atomically (write-then-rename).
        Always updates checkpoint_latest.json.
        Also writes a dated backup once per day.
        """
        state["saved_at"] = datetime.now(tz=timezone.utc).isoformat()
        try:
            # Atomic write — never leaves a half-written file
            tmp = self.latest.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, default=str)
            # replace() is atomic on all platforms including Windows.
            # rename() raises FileExistsError on Windows if the target already exists,
            # which means checkpoints silently stop updating after the first save.
            tmp.replace(self.latest)

            # Daily dated backup
            date_str  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            dated     = self.dir / f"checkpoint_{date_str}.json"
            if not dated.exists():
                with open(dated, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2, default=str)

            logger.debug("Checkpoint saved (%d keys).", len(state))
            return True
        except Exception as exc:
            logger.error("Checkpoint save failed: %s", exc)
            return False

    def load(self) -> Optional[Dict[str, Any]]:
        """Return the latest checkpoint dict, or None if none exists."""
        if not self.latest.exists():
            return None
        try:
            with open(self.latest, encoding="utf-8") as fh:
                state = json.load(fh)
            logger.info(
                "Checkpoint loaded (saved_at=%s, keys=%s).",
                state.get("saved_at", "?"), list(state.keys()),
            )
            return state
        except Exception as exc:
            logger.error("Checkpoint load failed: %s", exc)
            return None

    def exists(self) -> bool:
        return self.latest.exists()