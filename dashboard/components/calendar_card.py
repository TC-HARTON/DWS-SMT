"""Economic-calendar card (SPEC §15).

Renders the upcoming high-impact events with a 1-second clientside
countdown driven by a hidden :class:`dcc.Interval`. The card sits in the
right-column account stack — Phase 1 left a placeholder there which this
module replaces.
"""

from __future__ import annotations

from dash import dcc, html


def build_calendar_card() -> html.Section:
    """Static shell; clientside callbacks fill rows + countdown."""
    return html.Section(
        className="calendar-card",
        id="calendar-card",
        children=[
            # 1 Hz local tick driving the countdown — independent of the WS
            # which only ships fresh data once an hour.
            dcc.Interval(id="calendar-tick", interval=1000, n_intervals=0),
            html.Header(
                className="calendar-card__header",
                children=[
                    html.Span("Economic Calendar",
                              className="account-card__section-title"),
                    html.Span(
                        "--",
                        id="calendar-source",
                        className="calendar-card__source",
                    ),
                ],
            ),
            html.Div(
                id="calendar-list",
                className="calendar-list",
                children=[
                    html.Div("waiting for first fetch…",
                             className="calendar-list__empty"),
                ],
            ),
            # Hidden sink so the 1 s countdown callback has somewhere to write
            # without retriggering itself through the dcc.Interval output.
            html.Div(id="calendar-countdown-sink",
                     style={"display": "none"}),
        ],
    )
