"""
trade_journal.py — Persistent trade record.

Every opened/closed trade is appended here so the dashboard can
show live analytics without querying MT5 history.

Storage: .cache/trade_journal.json
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(".cache") / "trade_journal.json"


class TradeJournal:

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path = Path(path) if path else _DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trades: List[Dict[str, Any]] = self._load()

    # ── Write ────────────────────────────────────────────────────────

    def record_open(
        self,
        ticket:     int,
        symbol:     str,
        direction:  str,
        entry:      float,
        sl:         float,
        tp:         float,
        lot_size:   float,
        grade:      str = "?",
    ) -> None:
        record = {
            "ticket":    ticket,
            "symbol":    symbol,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "lot_size":  lot_size,
            "grade":     grade,
            "pnl":       None,
            "outcome":   "OPEN",
            "opened_at": datetime.now(tz=timezone.utc).isoformat(),
            "closed_at": None,
        }
        self._trades.append(record)
        self._save()
        logger.debug("Journal: recorded OPEN ticket=%s %s %s", ticket, symbol, direction)

    def record_close(
        self,
        ticket:  int,
        pnl:     float,
        outcome: str,   # WIN_FULL | WIN_PARTIAL | LOSS | MONITOR_CLOSE
    ) -> None:
        for t in reversed(self._trades):
            if t["ticket"] == ticket and t["outcome"] == "OPEN":
                t["pnl"]       = round(pnl, 2)
                t["outcome"]   = outcome
                t["closed_at"] = datetime.now(tz=timezone.utc).isoformat()
                self._save()
                logger.debug("Journal: closed ticket=%s outcome=%s pnl=%.2f", ticket, outcome, pnl)
                return
        logger.warning("Journal: no open record found for ticket=%s", ticket)

    # ── Read ─────────────────────────────────────────────────────────

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self._trades)

    def get_recent(self, n: int = 50) -> List[Dict[str, Any]]:
        return self._trades[-n:]

    def get_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        return [t for t in self._trades if t.get("symbol") == symbol]

    def get_stats(self, trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        if trades is None:
            trades = [t for t in self._trades if t["outcome"] != "OPEN"]
        if not trades:
            return {
                "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
            }
        wins    = [t for t in trades if t["outcome"] in ("WIN_FULL", "WIN_PARTIAL")]
        losses  = [t for t in trades if t["outcome"] == "LOSS"]
        total_pnl = sum(t["pnl"] or 0 for t in trades)
        return {
            "total":     len(trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl":   round(total_pnl / len(trades), 2),
        }

    def get_stats_by_symbol(self) -> Dict[str, Dict[str, Any]]:
        closed  = [t for t in self._trades if t["outcome"] != "OPEN"]
        symbols = sorted({t["symbol"] for t in closed})
        return {
            sym: self.get_stats(self.get_by_symbol(sym)) for sym in symbols
        }

    # ── Persistence ──────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._trades, fh, indent=2, default=str)
            tmp.rename(self.path)
        except Exception as exc:
            logger.error("TradeJournal save failed: %s", exc)

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("TradeJournal load failed (starting fresh): %s", exc)
            return []