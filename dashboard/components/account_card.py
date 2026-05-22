"""Account information card (SPEC §14.1) + performance card (SPEC §14.3)."""

from __future__ import annotations

from dash import dcc, html

import config
from dashboard.components.calendar_card import build_calendar_card


def _metric(label: str, target_id: str, value: str = "--") -> html.Div:
    return html.Div(
        className="account-card__metric",
        children=[
            html.Div(label, className="account-card__metric-label"),
            html.Div(value, id=target_id, className="account-card__metric-value"),
        ],
    )


def build_account_card() -> html.Aside:
    """Static shell; values are populated by the WebSocket clientside callback."""
    return html.Aside(
        className="account-card",
        id="account-card",
        children=[
            html.Header(
                className="account-card__header",
                children=[
                    html.Div("Account", className="account-card__title"),
                    html.Div(
                        className="account-card__identity",
                        children=[
                            html.Span("--", id="account-login",
                                      className="account-card__login"),
                            html.Span("/", className="account-card__sep"),
                            html.Span("--", id="account-server",
                                      className="account-card__server"),
                        ],
                    ),
                ],
            ),
            html.Section(
                className="account-card__grid",
                children=[
                    _metric("Balance", "account-balance"),
                    _metric("Equity", "account-equity"),
                    _metric("Profit", "account-profit"),
                    _metric("Today P&L", "account-today-pnl"),  # SPEC §14.1
                    _metric("Margin", "account-margin"),
                    _metric("Free", "account-margin-free"),
                    _metric("Level", "account-margin-level"),
                    _metric("Leverage", "account-leverage"),
                    _metric("Currency", "account-currency"),
                ],
            ),
            html.Section(
                className="account-card__positions",
                children=[
                    html.Header(
                        className="account-card__positions-header",
                        children=[
                            html.Span("Open Positions",
                                      className="account-card__section-title"),
                            html.Span("0", id="account-position-count",
                                      className="account-card__badge"),
                        ],
                    ),
                    html.Div(
                        id="account-positions-list",
                        className="positions-list",
                        children=[
                            html.Div("No open positions",
                                     className="positions-list__empty"),
                        ],
                    ),
                ],
            ),
            _build_performance_card(),
            build_calendar_card(),
        ],
    )


def _build_performance_card() -> html.Section:
    """SPEC §14.3 performance metrics with a 24h/7d/30d/90d/all range picker."""
    return html.Section(
        className="performance-card",
        id="performance-card",
        children=[
            dcc.Store(id="performance-range-store",
                      data=config.HISTORY_DEFAULT_RANGE),
            html.Header(
                className="performance-card__header",
                children=[
                    html.Span("Performance",
                              className="account-card__section-title"),
                    html.Div(
                        className="performance-card__ranges",
                        children=[
                            html.Button(
                                r.label,
                                id=f"perf-range-btn-{r.label}",
                                n_clicks=0,
                                className="performance-card__range-btn",
                            ) for r in config.HISTORY_RANGES
                        ],
                    ),
                ],
            ),
            html.Div(
                className="performance-card__grid",
                children=[
                    _perf_metric("Trades", "perf-trades"),
                    _perf_metric("Win %", "perf-winrate"),
                    _perf_metric("Net", "perf-net"),
                    _perf_metric("PF", "perf-pf"),
                    _perf_metric("Max DD", "perf-dd"),
                    _perf_metric("R:R", "perf-rr"),
                ],
            ),
            html.Section(
                className="performance-card__by-symbol",
                children=[
                    html.Header(
                        "Per-symbol PnL",
                        className="performance-card__subtitle",
                    ),
                    html.Div(
                        id="perf-by-symbol",
                        className="perf-symbol-list",
                        children=[
                            html.Div(
                                "no closed trades in window",
                                className="perf-symbol-list__empty",
                            ),
                        ],
                    ),
                ],
            ),
            html.Section(
                className="performance-card__by-hour",
                children=[
                    html.Header(
                        "PnL by JST hour",
                        className="performance-card__subtitle",
                    ),
                    html.Div(
                        id="perf-by-hour",
                        className="perf-hour-bars",
                        children=[
                            html.Div(
                                "no closed trades in window",
                                className="perf-hour-bars__empty",
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )


def _perf_metric(label: str, target_id: str) -> html.Div:
    return html.Div(
        className="performance-card__metric",
        children=[
            html.Div(label, className="performance-card__metric-label"),
            html.Div("--", id=target_id, className="performance-card__metric-value"),
        ],
    )
