"""Construct the Dash application: layout, callbacks, WebSocket endpoint.

This module is import-side-effect free aside from constructing the Dash
``app`` object. ``main.py`` calls :func:`build_app` after starting the
analysis loop so the very first connection from the browser already has
data to display.
"""

from __future__ import annotations

import logging

import dash
from dash import Dash, html

import config
from dashboard.callbacks import register_clientside_callbacks
from dashboard.layout import build_layout
from dashboard.ws_broadcaster import mount_websocket

log = logging.getLogger(__name__)


def build_app() -> Dash:
    """Construct and return the configured Dash application."""
    assets_folder = str(config.PROJECT_ROOT / "dashboard" / "styles")
    app = Dash(
        __name__,
        title=config.DASH_PAGE_TITLE,
        update_title=None,
        suppress_callback_exceptions=False,
        # SPEC §4 places stylesheets under dashboard/styles/; tell Dash to
        # autoload from there instead of the default ./assets/ folder.
        assets_folder=assets_folder,
        meta_tags=[
            {"name": "viewport", "content": "width=device-width, initial-scale=1.0"},
        ],
    )
    app.layout = build_layout()
    register_clientside_callbacks(app)
    mount_websocket(app.server)
    log.info(
        "Dash application built on http://%s:%d (WS=%s)",
        config.DASH_HOST, config.DASH_PORT, config.DASH_WS_PATH,
    )
    return app
