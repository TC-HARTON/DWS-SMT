"""Lightweight HTML + WebSocket server (replaces Dash).

The Phase 1-5 Dash UI used React-driven callbacks. Phase 6 swaps that out
for a plain Flask app serving:

* a single static ``index.html`` (no framework, no React, no Plotly)
* a ``/ws`` WebSocket endpoint shipping the same JSON snapshot the old
  broadcaster sent, so the analyzer backend is untouched
* ``/static/*`` files (CSS + JS) cached forever — the client patches the
  DOM in-place via the WebSocket stream

This is dramatically lighter than Dash: ~30 KB total payload, zero
clientside framework overhead, direct DOM updates only on the cells
that actually changed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, send_from_directory

import config
from dashboard.ws_broadcaster import mount_websocket

log = logging.getLogger(__name__)

_STATIC_DIR = config.PROJECT_ROOT / "static"


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

    mount_websocket(app)
    log.info(
        "Lite dashboard built on http://%s:%d (static=%s, WS=%s)",
        config.DASH_HOST, config.DASH_PORT, _STATIC_DIR, config.DASH_WS_PATH,
    )
    return app
