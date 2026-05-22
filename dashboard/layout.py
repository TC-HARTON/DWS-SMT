"""Top-level layout for the dashboard (SPEC §16.5)."""

from __future__ import annotations

from dash import dcc, html
from dash_extensions import WebSocket

import config
from dashboard.components.account_card import build_account_card
from dashboard.components.correlation_heatmap import build_correlation_heatmap
from dashboard.components.header import build_header
from dashboard.components.strength_meter import build_strength_meter
from dashboard.components.symbol_panel import build_symbol_panel


def build_layout() -> html.Div:
    # Resolve the WebSocket URL relative to the page so the browser picks
    # the right scheme (ws:// vs wss://) automatically.
    ws_url = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"

    xl_symbols = [s for s in config.SYMBOLS if s.display_size == "xl"]
    md_symbols = [s for s in config.SYMBOLS if s.display_size == "md"]
    sm_symbols = [s for s in config.SYMBOLS if s.display_size == "sm"]

    return html.Div(
        id="app-root",
        className="app-root",
        children=[
            # --- Single source of truth for WebSocket-pushed state. ---------
            WebSocket(id="ws", url=ws_url),
            dcc.Store(id="ws-store", data={}),

            build_header(),

            html.Main(
                className="app-main",
                children=[
                    html.Div(
                        className="app-main__left",
                        children=[
                            html.Section(
                                className="symbol-grid symbol-grid--xl",
                                children=[build_symbol_panel(s) for s in xl_symbols],
                            ),
                            html.Section(
                                className="symbol-grid symbol-grid--md",
                                children=[build_symbol_panel(s) for s in md_symbols],
                            ),
                            html.Section(
                                className="symbol-grid symbol-grid--sm",
                                children=[build_symbol_panel(s) for s in sm_symbols],
                            ),
                        ],
                    ),
                    html.Div(
                        className="app-main__right",
                        children=[build_account_card()],
                    ),
                ],
            ),

            html.Footer(
                className="app-bottom",
                children=[
                    build_strength_meter(),
                    build_correlation_heatmap(),
                ],
            ),
        ],
    )
