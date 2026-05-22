"""Correlation heatmap (SPEC §13).

Bottom-right replacement for the Phase-2 placeholder. Uses Plotly's
``Heatmap`` because Dash already ships plotly, and an HTML/CSS grid
heatmap would force the clientside callback to inject ~100 divs per
update. A figure update is cheaper.
"""

from __future__ import annotations

from dash import dcc, html

import config

# SPEC §13.3 5-tier divergent colorscale, TradingView-inspired calm
# palette: cool teal-blue for negative, warm coral for positive.
HEATMAP_COLORSCALE = [
    [0.00, "#1565c0"],   # < -0.7  deep blue
    [0.15, "#42a5f5"],   # -0.7 .. -0.3  light blue
    [0.40, "#37404e"],   # -0.3 .. +0.3  neutral panel tone
    [0.85, "#ef9a9a"],   # +0.3 .. +0.7  light coral
    [1.00, "#c62828"],   # > +0.7  deep red
]


def _window_button(bars: int) -> html.Button:
    return html.Button(
        f"{bars}",
        id=f"correlation-window-btn-{bars}",
        n_clicks=0,
        className="correlation-window-btn",
    )


def build_correlation_heatmap() -> html.Section:
    return html.Section(
        id="correlation-heatmap",
        className="correlation-heatmap",
        children=[
            dcc.Store(id="correlation-window-store",
                      data=config.CORRELATION_DEFAULT_BARS),
            html.Header(
                className="correlation-heatmap__header",
                children=[
                    html.Div(
                        className="correlation-heatmap__title-block",
                        children=[
                            html.Span("Correlation Matrix",
                                      className="correlation-heatmap__title"),
                            html.Span(
                                f"H1 close-to-close returns · SPEC §13",
                                className="correlation-heatmap__subtitle",
                            ),
                        ],
                    ),
                    html.Div(
                        className="correlation-heatmap__windows",
                        children=[_window_button(b)
                                  for b in config.CORRELATION_WINDOWS_BARS],
                    ),
                ],
            ),
            dcc.Graph(
                id="correlation-figure",
                className="correlation-heatmap__graph",
                config={"displayModeBar": False, "staticPlot": False},
            ),
        ],
    )
