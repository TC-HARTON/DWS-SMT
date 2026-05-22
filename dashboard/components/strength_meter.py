"""Currency strength meter (SPEC §12).

The bottom-left panel previously showed a "Phase 3 で…" placeholder.
We replace it with a horizontal-bar meter — one row per displayed
currency (SPEC §12.1) — and a window-selector pill row (H1/H4/D1/W1).
Bar widths are 0-100 with a midline at 50 (= neutral).
"""

from __future__ import annotations

from dash import dcc, html

import config


def _bar_row(currency: str) -> html.Div:
    """One currency row: label, 0-100 bar, score readout."""
    return html.Div(
        id=f"strength-row-{currency}",
        className="strength-row",
        children=[
            html.Span(currency, className="strength-row__ccy"),
            html.Div(
                className="strength-row__track",
                children=[
                    # Midline at 50 — a vertical guide drawn purely with CSS.
                    html.Div(className="strength-row__midline"),
                    html.Div(
                        id=f"strength-bar-{currency}",
                        className="strength-row__bar",
                        style={"width": "0%", "left": "50%"},
                    ),
                ],
            ),
            html.Span("--", id=f"strength-val-{currency}",
                      className="strength-row__val"),
        ],
    )


def _window_button(label: str) -> html.Button:
    return html.Button(
        label,
        id=f"strength-window-btn-{label}",
        n_clicks=0,
        className="strength-window-btn",
    )


def build_strength_meter() -> html.Section:
    """Static shell; clientside callback fills bar widths and class names."""
    currencies = list(config.FIAT_CURRENCIES) + ["XAU"]
    return html.Section(
        id="strength-meter",
        className="strength-meter",
        children=[
            # Persistent selected-window state.
            dcc.Store(id="strength-window-store",
                      data=config.STRENGTH_DEFAULT_WINDOW),
            html.Header(
                className="strength-meter__header",
                children=[
                    html.Div(
                        className="strength-meter__title-block",
                        children=[
                            html.Span("Currency Strength",
                                      className="strength-meter__title"),
                            html.Span(
                                "0–100 normalised · SPEC §12",
                                className="strength-meter__subtitle",
                            ),
                        ],
                    ),
                    html.Div(
                        className="strength-meter__windows",
                        children=[_window_button(w.label)
                                  for w in config.STRENGTH_WINDOWS],
                    ),
                ],
            ),
            html.Div(
                className="strength-meter__rows",
                children=[_bar_row(c) for c in currencies],
            ),
        ],
    )
