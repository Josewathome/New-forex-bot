"""
server.py — Flask dashboard API server.

Runs in a background daemon thread inside Wine Python.
Serves the dashboard HTML at http://localhost:8080
and exposes a REST API for live config/stats updates.

Endpoints:
    GET  /                      → dashboard HTML
    GET  /api/status            → bot status + config snapshot
    GET  /api/stats             → trade analytics (global + per symbol)
    GET  /api/trades            → recent trades list
    GET  /api/config            → current config values
    PATCH /api/config           → live config update
    GET  /api/symbols           → current symbol list
    POST /api/symbols           → add a symbol
    DELETE /api/symbols/<sym>   → remove a symbol
    GET  /api/logs              → last N lines of trading_bot.log
"""
from __future__ import annotations
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import src.config as config

logger = logging.getLogger(__name__)

_BOT_START_TIME = datetime.now(tz=timezone.utc)


class DashboardServer:

    def __init__(self, trade_journal, checkpoint_service) -> None:
        self.journal    = trade_journal
        self.checkpoint = checkpoint_service
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="Dashboard"
        )
        self._thread.start()
        port = getattr(config, "DASHBOARD_PORT", 8080)
        logger.info("Dashboard starting at http://0.0.0.0:%d", port)

    # ── Flask app ────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            from flask import Flask, jsonify, request, send_file
            from flask import Response
        except ImportError:
            logger.error(
                "Flask not installed — dashboard unavailable. "
                "Run: wine python -m pip install flask"
            )
            return

        app = Flask(__name__)
        app.config["JSON_SORT_KEYS"] = False

        # ── Static ──────────────────────────────────────────────────

        @app.route("/")
        def index():
            p = Path("src/dashboard/index.html")
            if p.exists():
                return send_file(str(p.resolve()))
            return "<h2>Dashboard HTML not found at src/dashboard/index.html</h2>", 404

        # ── Status ──────────────────────────────────────────────────

        @app.route("/api/status")
        def status():
            cp  = self.checkpoint.load() or {}
            uptime_secs = int(
                (datetime.now(tz=timezone.utc) - _BOT_START_TIME).total_seconds()
            )
            return jsonify({
                "running":          True,
                "uptime_seconds":   uptime_secs,
                "server_time":      datetime.now(tz=timezone.utc).isoformat(),
                "last_checkpoint":  cp.get("saved_at"),
                "symbols":          config.SYMBOLS,
                "risk_pct":         config.RISK_PERCENT,
                "min_rr":           config.MIN_RR,
            })

        # ── Analytics ───────────────────────────────────────────────

        @app.route("/api/stats")
        def stats():
            return jsonify({
                "global":     self.journal.get_stats(),
                "by_symbol":  self.journal.get_stats_by_symbol(),
            })

        @app.route("/api/trades")
        def trades():
            n = int(request.args.get("n", 50))
            return jsonify({"trades": self.journal.get_recent(n)})

        # ── Config ──────────────────────────────────────────────────

        @app.route("/api/config", methods=["GET"])
        def get_config():
            return jsonify({
                "symbols":             config.SYMBOLS,
                "risk_pct":            config.RISK_PERCENT,
                "min_rr":              config.MIN_RR,
                "max_open_trades":     config.MAX_OPEN_TRADES,
                "atr_cost_threshold":  config.ATR_COST_THRESHOLD,
                "loop_interval":       config.LOOP_INTERVAL_SECONDS,
                "tiered_tp_enabled":   config.TIERED_TP_ENABLED,
                "tiered_tp1_ratio":    config.TIERED_TP1_RATIO,
                "tiered_tp2_ratio":    config.TIERED_TP2_RATIO,
                "monitor_enabled":     config.MONITOR_ENABLED,
                "breakeven_rr":        config.BREAKEVEN_RR_TRIGGER,
            })

        @app.route("/api/config", methods=["PATCH"])
        def update_config():
            data    = request.get_json(silent=True) or {}
            changed = []
            _MAP = {
                "risk_pct":           ("RISK_PERCENT",         float),
                "min_rr":             ("MIN_RR",               float),
                "max_open_trades":    ("MAX_OPEN_TRADES",       int),
                "atr_cost_threshold": ("ATR_COST_THRESHOLD",   float),
                "loop_interval":      ("LOOP_INTERVAL_SECONDS", int),
                "tiered_tp_enabled":  ("TIERED_TP_ENABLED",    bool),
                "tiered_tp1_ratio":   ("TIERED_TP1_RATIO",     float),
                "tiered_tp2_ratio":   ("TIERED_TP2_RATIO",     float),
                "breakeven_rr":       ("BREAKEVEN_RR_TRIGGER", float),
            }
            for key, (attr, cast) in _MAP.items():
                if key in data:
                    try:
                        setattr(config, attr, cast(data[key]))
                        changed.append(key)
                    except (ValueError, TypeError) as exc:
                        logger.warning("Config update: bad value for %s — %s", key, exc)

            if changed:
                logger.info("Config updated via dashboard: %s", changed)
            return jsonify({"updated": changed, "ok": True})

        # ── Symbols ─────────────────────────────────────────────────

        @app.route("/api/symbols", methods=["GET"])
        def get_symbols():
            return jsonify({"symbols": config.SYMBOLS})

        @app.route("/api/symbols", methods=["POST"])
        def add_symbol():
            data = request.get_json(silent=True) or {}
            sym  = data.get("symbol", "").upper().strip()
            if not sym:
                return jsonify({"ok": False, "error": "Missing symbol"}), 400
            if sym in config.SYMBOLS:
                return jsonify({"ok": False, "error": f"{sym} already active"}), 400
            config.SYMBOLS.append(sym)
            logger.info("Symbol added via dashboard: %s", sym)
            return jsonify({"ok": True, "symbols": config.SYMBOLS})

        @app.route("/api/symbols/<symbol>", methods=["DELETE"])
        def remove_symbol(symbol):
            sym = symbol.upper()
            if sym not in config.SYMBOLS:
                return jsonify({"ok": False, "error": f"{sym} not found"}), 404
            config.SYMBOLS.remove(sym)
            logger.info("Symbol removed via dashboard: %s", sym)
            return jsonify({"ok": True, "symbols": config.SYMBOLS})

        # ── Log tail ────────────────────────────────────────────────

        @app.route("/api/logs")
        def logs():
            n    = int(request.args.get("n", 100))
            logf = Path("trading_bot.log")
            if not logf.exists():
                return jsonify({"lines": []})
            try:
                lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()
                return jsonify({"lines": lines[-n:]})
            except Exception as exc:
                return jsonify({"lines": [], "error": str(exc)})

        # ── Start ────────────────────────────────────────────────────

        port = getattr(config, "DASHBOARD_PORT", 8080)
        try:
            app.run(
                host="0.0.0.0",
                port=port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )
        except Exception as exc:
            logger.error("Dashboard server crashed: %s", exc)