"""
server.py — Flask dashboard API server.

Runs in a background daemon thread inside Wine Python.
Serves the dashboard HTML at http://localhost:8080
and exposes a REST API for live config/stats updates.

All /api/* endpoints (except /api/auth/*) require a valid JWT access token
in the Authorization: Bearer <token> header.

Auth endpoints:
    POST /api/auth/login          → {access_token, refresh_token, expires_in}
    POST /api/auth/refresh        → {access_token, expires_in}
    POST /api/auth/logout         → {ok: true}  (revokes refresh token)
    POST /api/auth/rotate-secret  → {ok: true}  (rotates JWT secret, invalidates all sessions)

Protected endpoints:
    GET  /                        → dashboard HTML
    GET  /api/status              → bot status + config snapshot
    GET  /api/stats               → trade analytics (global + per symbol)
    GET  /api/trades              → recent trades list
    GET  /api/config              → current config values
    PATCH /api/config             → live config update
    GET  /api/symbols             → current symbol list
    POST /api/symbols             → add a symbol
    DELETE /api/symbols/<sym>     → remove a symbol
    GET  /api/logs                → last N lines of trading_bot.log
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

import src.config as config

logger = logging.getLogger(__name__)

_BOT_START_TIME = datetime.now(tz=timezone.utc)

# File where dashboard config overrides are written so they survive restarts.
_CONFIG_OVERRIDE_FILE = Path(".checkpoints/config_overrides.json")

_CONFIG_MAP = {
    "risk_pct":           ("RISK_PERCENT",          float),
    "min_rr":             ("MIN_RR",                float),
    "max_open_trades":    ("MAX_OPEN_TRADES",        int),
    "atr_cost_threshold": ("ATR_COST_THRESHOLD",    float),
    "loop_interval":      ("LOOP_INTERVAL_SECONDS", int),
    "tiered_tp_enabled":  ("TIERED_TP_ENABLED",     bool),
    "tiered_tp1_ratio":   ("TIERED_TP1_RATIO",      float),
    "tiered_tp2_ratio":   ("TIERED_TP2_RATIO",      float),
    "breakeven_rr":       ("BREAKEVEN_RR_TRIGGER",  float),
}


def _persist_config_overrides(changed_keys, _map) -> None:
    """Write all current mutable config values to disk."""
    try:
        _CONFIG_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        overrides = {}
        for key, (attr, _) in _map.items():
            overrides[key] = getattr(config, attr, None)
        tmp = _CONFIG_OVERRIDE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
        tmp.replace(_CONFIG_OVERRIDE_FILE)
        logger.info("Config overrides persisted to %s (%s changed)",
                    _CONFIG_OVERRIDE_FILE, changed_keys)
    except Exception as exc:
        logger.error("Failed to persist config overrides: %s", exc)


def load_config_overrides() -> None:
    """
    Apply any previously-saved dashboard config overrides to the live config module.
    Call this once at bot startup (from main.py) BEFORE trading begins.
    """
    if not _CONFIG_OVERRIDE_FILE.exists():
        return
    try:
        overrides = json.loads(_CONFIG_OVERRIDE_FILE.read_text(encoding="utf-8"))
        applied = []
        for key, (attr, cast) in _CONFIG_MAP.items():
            if key in overrides and overrides[key] is not None:
                setattr(config, attr, cast(overrides[key]))
                applied.append(f"{attr}={overrides[key]}")
        if applied:
            logger.info("Loaded %d config overrides from disk: %s",
                        len(applied), ", ".join(applied))
    except Exception as exc:
        logger.warning("Could not load config overrides (%s) — using defaults.", exc)


# ─────────────────────────────────────────────────────────────────────
#  JWT utilities  (stdlib only — no PyJWT dependency)
# ─────────────────────────────────────────────────────────────────────

def _b64u_enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    rem = len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (4 - rem if rem else 0))


def _jwt_sign(header_b64: str, payload_b64: str, secret: str) -> str:
    msg = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return _b64u_enc(sig)


def _jwt_encode(payload: dict, secret: str) -> str:
    header  = _b64u_enc(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body    = _b64u_enc(json.dumps(payload).encode())
    sig     = _jwt_sign(header, body, secret)
    return f"{header}.{body}.{sig}"


def _jwt_decode(token: str, secret: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed token")
    header_b64, payload_b64, sig_b64 = parts
    expected = _jwt_sign(header_b64, payload_b64, secret)
    if not hmac.compare_digest(sig_b64, expected):
        raise ValueError("Invalid signature")
    payload = json.loads(_b64u_dec(payload_b64))
    if payload.get("exp", 0) < time.time():
        raise ValueError("Token expired")
    return payload


# ─────────────────────────────────────────────────────────────────────
#  JWTManager — secret persistence + token issuance + blocklist
# ─────────────────────────────────────────────────────────────────────

_SECRET_FILE = Path(".checkpoints/jwt_secret.txt")


class JWTManager:
    """Manages JWT signing secret and refresh-token revocation list."""

    def __init__(self) -> None:
        self._secret: str = self._load_or_create()
        self._blocklist: Set[str] = set()  # revoked refresh-token JTIs

    # ── Secret lifecycle ──────────────────────────────────────────

    def _load_or_create(self) -> str:
        if _SECRET_FILE.exists():
            s = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if s:
                logger.debug("JWT secret loaded from %s", _SECRET_FILE)
                return s
        return self._save_new_secret()

    def _save_new_secret(self) -> str:
        secret = secrets.token_urlsafe(48)
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secret, encoding="utf-8")
        return secret

    def rotate(self) -> None:
        """Generate a new secret and save it. All existing tokens immediately invalid."""
        self._secret = self._save_new_secret()
        self._blocklist.clear()
        logger.info("JWT secret rotated — all existing sessions invalidated.")

    # ── Token issuance ────────────────────────────────────────────

    def issue_access(self) -> str:
        exp = int(time.time()) + config.JWT_ACCESS_EXPIRES_MINUTES * 60
        return _jwt_encode({"sub": "dashboard", "type": "access",
                            "iat": int(time.time()), "exp": exp}, self._secret)

    def issue_refresh(self) -> str:
        jti = secrets.token_urlsafe(16)
        exp = int(time.time()) + config.JWT_REFRESH_EXPIRES_DAYS * 86400
        return _jwt_encode({"sub": "dashboard", "type": "refresh",
                            "jti": jti, "iat": int(time.time()), "exp": exp}, self._secret)

    # ── Token verification ────────────────────────────────────────

    def verify_access(self, token: str) -> dict:
        payload = _jwt_decode(token, self._secret)
        if payload.get("type") != "access":
            raise ValueError("Not an access token")
        return payload

    def verify_refresh(self, token: str) -> dict:
        payload = _jwt_decode(token, self._secret)
        if payload.get("type") != "refresh":
            raise ValueError("Not a refresh token")
        if payload.get("jti", "") in self._blocklist:
            raise ValueError("Token revoked")
        return payload

    def revoke_refresh(self, token: str) -> None:
        try:
            payload = _jwt_decode(token, self._secret)
            jti = payload.get("jti", "")
            if jti:
                self._blocklist.add(jti)
        except Exception:
            pass  # already invalid — nothing to do


# ─────────────────────────────────────────────────────────────────────
#  DashboardServer
# ─────────────────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self, trade_journal, checkpoint_service) -> None:
        self.journal    = trade_journal
        self.checkpoint = checkpoint_service
        self._thread: Optional[threading.Thread] = None
        self._jwt       = JWTManager()
        self._password  = self._resolve_password()

    def _resolve_password(self) -> str:
        pw = getattr(config, "DASHBOARD_PASSWORD", "").strip()
        if pw:
            return pw
        generated = secrets.token_urlsafe(16)
        logger.warning(
            "┌──────────────────────────────────────────────────────┐\n"
            "│  DASHBOARD_PASSWORD not set — auto-generated:         │\n"
            "│  %-52s │\n"
            "│  Add DASHBOARD_PASSWORD=<above> to your .env file.   │\n"
            "└──────────────────────────────────────────────────────┘",
            generated,
        )
        return generated

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
        except ImportError:
            logger.error(
                "Flask not installed — dashboard unavailable. "
                "Run: wine python -m pip install flask"
            )
            return

        app = Flask(__name__)
        app.config["JSON_SORT_KEYS"] = False

        jwt_mgr  = self._jwt
        password = self._password

        # ── Auth helpers ─────────────────────────────────────────────

        def require_auth(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return jsonify({"error": "Missing Authorization header"}), 401
                try:
                    jwt_mgr.verify_access(auth[7:])
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 401
                return f(*args, **kwargs)
            return wrapper

        def _check_password(candidate: str) -> bool:
            return hmac.compare_digest(
                candidate.encode("utf-8"),
                password.encode("utf-8"),
            )

        # ── Auth endpoints (public) ──────────────────────────────────

        @app.route("/api/auth/login", methods=["POST"])
        def auth_login():
            data = request.get_json(silent=True) or {}
            if not _check_password(data.get("password", "")):
                logger.warning("Dashboard: failed login attempt from %s",
                               request.remote_addr)
                return jsonify({"error": "Invalid password"}), 401
            logger.info("Dashboard: login from %s", request.remote_addr)
            return jsonify({
                "access_token":  jwt_mgr.issue_access(),
                "refresh_token": jwt_mgr.issue_refresh(),
                "expires_in":    config.JWT_ACCESS_EXPIRES_MINUTES * 60,
            })

        @app.route("/api/auth/refresh", methods=["POST"])
        def auth_refresh():
            data = request.get_json(silent=True) or {}
            rt   = data.get("refresh_token", "")
            try:
                jwt_mgr.verify_refresh(rt)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 401
            return jsonify({
                "access_token": jwt_mgr.issue_access(),
                "expires_in":   config.JWT_ACCESS_EXPIRES_MINUTES * 60,
            })

        @app.route("/api/auth/logout", methods=["POST"])
        def auth_logout():
            data = request.get_json(silent=True) or {}
            rt   = data.get("refresh_token", "")
            if rt:
                jwt_mgr.revoke_refresh(rt)
            logger.info("Dashboard: logout from %s", request.remote_addr)
            return jsonify({"ok": True})

        @app.route("/api/auth/rotate-secret", methods=["POST"])
        def auth_rotate():
            data = request.get_json(silent=True) or {}
            if not _check_password(data.get("password", "")):
                return jsonify({"error": "Invalid password"}), 401
            jwt_mgr.rotate()
            logger.info("Dashboard: JWT secret rotated by %s", request.remote_addr)
            return jsonify({
                "ok": True,
                "message": "Secret rotated. All existing sessions have been invalidated.",
            })

        # ── Static ──────────────────────────────────────────────────

        @app.route("/")
        @require_auth
        def index():
            p = Path("src/dashboard/index.html")
            if p.exists():
                return send_file(str(p.resolve()))
            return "<h2>Dashboard HTML not found at src/dashboard/index.html</h2>", 404

        # ── Status ──────────────────────────────────────────────────

        @app.route("/api/status")
        @require_auth
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
        @require_auth
        def stats():
            return jsonify({
                "global":    self.journal.get_stats(),
                "by_symbol": self.journal.get_stats_by_symbol(),
            })

        @app.route("/api/trades")
        @require_auth
        def trades():
            n = int(request.args.get("n", 50))
            return jsonify({"trades": self.journal.get_recent(n)})

        # ── Config ──────────────────────────────────────────────────

        @app.route("/api/config", methods=["GET"])
        @require_auth
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
        @require_auth
        def update_config():
            data    = request.get_json(silent=True) or {}
            changed = []
            for key, (attr, cast) in _CONFIG_MAP.items():
                if key in data:
                    try:
                        setattr(config, attr, cast(data[key]))
                        changed.append(key)
                    except (ValueError, TypeError) as exc:
                        logger.warning("Config update: bad value for %s — %s", key, exc)
            if changed:
                logger.info("Config updated via dashboard: %s", changed)
                _persist_config_overrides(changed, _CONFIG_MAP)
            return jsonify({"updated": changed, "ok": True})

        # ── Symbols ─────────────────────────────────────────────────

        @app.route("/api/symbols", methods=["GET"])
        @require_auth
        def get_symbols():
            return jsonify({"symbols": config.SYMBOLS})

        @app.route("/api/symbols", methods=["POST"])
        @require_auth
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
        @require_auth
        def remove_symbol(symbol):
            sym = symbol.upper()
            if sym not in config.SYMBOLS:
                return jsonify({"ok": False, "error": f"{sym} not found"}), 404
            config.SYMBOLS.remove(sym)
            logger.info("Symbol removed via dashboard: %s", sym)
            return jsonify({"ok": True, "symbols": config.SYMBOLS})

        # ── Log tail ────────────────────────────────────────────────

        @app.route("/api/logs")
        @require_auth
        def logs():
            n    = int(request.args.get("n", 100))
            logf = Path("logs/trading_bot.log")
            if not logf.exists():
                return jsonify({"lines": []})
            try:
                chunk_size = n * 512
                with open(logf, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    read_from = max(0, file_size - chunk_size)
                    f.seek(read_from)
                    raw = f.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()
                if read_from > 0 and lines:
                    lines = lines[1:]
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
