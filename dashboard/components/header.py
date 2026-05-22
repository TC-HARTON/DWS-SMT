"""Top header: live clock, MT5 connection status, last compute time."""

from __future__ import annotations

from dash import html


def build_header() -> html.Header:
    return html.Header(
        id="app-header",
        className="app-header",
        children=[
            html.Div(
                className="app-header__title",
                children=[
                    html.Span("MT5", className="app-header__brand"),
                    html.Span("Trading Dashboard", className="app-header__sub"),
                ],
            ),
            html.Div(
                className="app-header__metrics",
                children=[
                    html.Div(
                        className="app-header__metric",
                        children=[
                            html.Div("Now (JST)", className="app-header__metric-label"),
                            html.Div("--:--:--", id="header-clock",
                                     className="app-header__metric-value"),
                        ],
                    ),
                    html.Div(
                        className="app-header__metric",
                        children=[
                            html.Div("MT5", className="app-header__metric-label"),
                            html.Div(
                                children=[
                                    html.Span(className="status-dot status-dot--off",
                                              id="header-status-dot"),
                                    html.Span("disconnected", id="header-status-text"),
                                ],
                                className="app-header__metric-value",
                            ),
                        ],
                    ),
                    html.Div(
                        className="app-header__metric",
                        children=[
                            html.Div("Compute", className="app-header__metric-label"),
                            html.Div("-- ms", id="header-compute-ms",
                                     className="app-header__metric-value"),
                        ],
                    ),
                    html.Div(
                        className="app-header__metric",
                        children=[
                            html.Div("State v.", className="app-header__metric-label"),
                            html.Div("0", id="header-version",
                                     className="app-header__metric-value"),
                        ],
                    ),
                ],
            ),
        ],
    )
