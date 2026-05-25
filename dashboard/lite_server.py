"""Lightweight HTML + WebSocket server (replaces Dash).

The Phase 1-5 Dash UI used React-driven callbacks. Phase 6 swaps that out
for a plain Flask app serving:

* a single static ``index.html`` (no framework, no React, no Plotly)
* a ``/ws`` WebSocket endpoint shipping the same JSON snapshot the old
  broadcaster sent, so the analyzer backend is untouched
* ``/static/*`` files (CSS + JS) cached forever — the client patches the
  DOM in-place via the WebSocket stream
* a ``/api/broker`` POST endpoint that flips the MT5 terminal in .env
  and triggers a self-restart so the user can switch brokers from the
  ACCOUNT badge dropdown without leaving the dashboard

This is dramatically lighter than Dash: ~30 KB total payload, zero
clientside framework overhead, direct DOM updates only on the cells
that actually changed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import config
from dashboard.ws_broadcaster import mount_websocket

log = logging.getLogger(__name__)

_STATIC_DIR = config.PROJECT_ROOT / "static"
_ENV_FILE = config.PROJECT_ROOT / ".env"
_RESTART_SCRIPT = config.PROJECT_ROOT / "Dashboard.bat"


def _rewrite_env_terminal_path(new_path: str) -> None:
    """Replace (or append) the MT5_TERMINAL_PATH line in .env, preserving
    every other line and the file's UTF-8 BOM-less encoding."""
    lines: list[str] = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    target = f"MT5_TERMINAL_PATH={new_path}"
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("MT5_TERMINAL_PATH="):
            lines[i] = target
            replaced = True
            break
    if not replaced:
        lines.append(target)
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spawn_restart_then_exit() -> None:
    """Detach a Dashboard.bat relaunch and exit this process.

    Sleeps briefly so the HTTP response reaches the browser, then launches
    Dashboard.bat in a new console window (DETACHED so it survives this
    process exiting) and calls ``os._exit`` — bypassing Flask's blocking
    shutdown handshake which doesn't work under Werkzeug's dev server.
    Dashboard.bat detects port 8050 free once we exit and starts a fresh
    main.py with the just-written MT5_TERMINAL_PATH.
    """
    import time
    time.sleep(0.4)
    # CREATE_NEW_CONSOLE so the child gets its own window like the original
    # Dashboard.bat launch; DETACHED_PROCESS so it isn't tied to our exit.
    CREATE_NEW_CONSOLE = 0x00000010
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            ["cmd", "/c", "timeout", "/t", "3", "/nobreak", ">", "nul", "&",
             "start", "", str(_RESTART_SCRIPT)],
            cwd=str(config.PROJECT_ROOT),
            creationflags=CREATE_NEW_CONSOLE | DETACHED_PROCESS,
            close_fds=True,
            shell=False,
        )
    except OSError:
        log.exception("broker-switch restart spawn failed")
    log.info("broker-switch: exiting so Dashboard.bat can take over")
    os._exit(0)


def build_app() -> Flask:
    """Construct the Flask app, mount static + ``/ws``, return it."""
    app = Flask(
        __name__,
        static_folder=str(_STATIC_DIR),
        static_url_path="/static",
    )

    @app.route("/")
    def index():  # pragma: no cover — exercised via the smoke verifier
        return send_from_directory(str(_STATIC_DIR), "index.html")

    @app.route("/favicon.ico")
    def favicon():  # pragma: no cover
        return ("", 204)

    @app.route("/api/broker", methods=["GET"])
    def get_broker():
        return jsonify({
            "current_path": config.MT5_TERMINAL_PATH,
            "presets": config.BROKER_PRESETS,
        })

    @app.route("/api/broker", methods=["POST"])
    def post_broker():
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        if name not in config.BROKER_PRESETS:
            return jsonify({"ok": False,
                            "error": f"unknown broker {name!r}"}), 400
        new_path = config.BROKER_PRESETS[name]
        try:
            _rewrite_env_terminal_path(new_path)
        except OSError as exc:
            log.exception(".env rewrite failed")
            return jsonify({"ok": False, "error": str(exc)}), 500
        log.info("broker-switch requested: %s → %s", name, new_path)
        # Detach the restart so we can return the HTTP response first.
        threading.Thread(target=_spawn_restart_then_exit, daemon=True).start()
        return jsonify({"ok": True, "name": name, "path": new_path})

    mount_websocket(app)
    log.info(
        "Lite dashboard built on http://%s:%d (static=%s, WS=%s)",
        config.DASH_HOST, config.DASH_PORT, _STATIC_DIR, config.DASH_WS_PATH,
    )
    return app
