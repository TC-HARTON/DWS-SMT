"""Per-symbol panel skeleton.

The panel is rendered server-side as an empty shell for each configured
symbol. The clientside callback registered in :mod:`dashboard.callbacks`
fills the panel body whenever the shared WebSocket store updates, so
1-second price refreshes do not pay a Python round-trip.

Layout per SPEC §16.6:
    Header:  base symbol + broker name + current bid
    Structure:    placeholder banner (SPEC Phase 2)
    MTF Status:   one row per timeframe (D1 / H4 / H1 / M15)
    Price Action: placeholder banner (SPEC Phase 2)
    Currency Bias: placeholder banner (SPEC Phase 3)
"""

from __future__ import annotations

from dash import html

import config
from config import TIMEFRAMES, SymbolSpec


def _build_mtf_row(sym: SymbolSpec, tf) -> html.Div:
    """Build one MTF row, omitting indicators that SPEC §6 does not show for *tf*."""
    cells: list = [
        html.Span(tf.label, className="mtf-row__tf"),
        html.Span(
            "·",
            id=f"symbol-mtf-{sym.base}-{tf.label}-arrow",
            className="mtf-row__arrow",
        ),
        # EMA is always shown (SPEC §6.1: one EMA per TF).
        html.Span(
            f"EMA{tf.ema_period} --",
            id=f"symbol-mtf-{sym.base}-{tf.label}-ema",
            className="mtf-row__ema",
        ),
    ]
    if tf.label in config.ADX_DISPLAY_TFS:                # SPEC §6.2 — D1, H4
        cells.append(html.Span(
            "ADX --",
            id=f"symbol-mtf-{sym.base}-{tf.label}-adx",
            className="mtf-row__adx",
        ))
    if tf.label in config.RSI_DISPLAY_TFS:                # SPEC §6.3 — H1, M15
        cells.append(html.Span(
            "RSI --",
            id=f"symbol-mtf-{sym.base}-{tf.label}-rsi",
            className="mtf-row__rsi",
        ))
    if tf.label in config.ATR_DISPLAY_TFS:                # SPEC §6.4 — H4 only
        cells.append(html.Span(
            "ATR --",
            id=f"symbol-mtf-{sym.base}-{tf.label}-atr",
            className="mtf-row__atr",
        ))
    return html.Div(
        className=f"mtf-row mtf-row--{tf.label.lower()}",
        id=f"symbol-mtf-{sym.base}-{tf.label}",
        children=cells,
    )


def _placeholder_section(title: str, note: str) -> html.Section:
    """Render a 'feature ships in Phase N' placeholder banner."""
    return html.Section(
        className="symbol-panel__section symbol-panel__section--placeholder",
        children=[
            html.Header(title, className="symbol-panel__section-title"),
            html.Div(note, className="symbol-panel__placeholder-note"),
        ],
    )


def build_symbol_panel(sym: SymbolSpec) -> html.Article:
    """Build the static shell for one symbol.

    Clientside callback ``update-symbol-<base>`` will rewrite this
    panel's children on each WebSocket message.
    """
    return html.Article(
        id=f"symbol-panel-{sym.base}",
        className=f"symbol-panel symbol-panel--{sym.display_size}",
        children=[
            html.Header(
                className="symbol-panel__header",
                children=[
                    html.Div(
                        className="symbol-panel__title-block",
                        children=[
                            html.Span(sym.base, className="symbol-panel__base"),
                            html.Span("", className="symbol-panel__broker",
                                      id=f"symbol-broker-{sym.base}"),
                        ],
                    ),
                    html.Div(
                        className="symbol-panel__price-block",
                        children=[
                            html.Div(
                                className="symbol-panel__quote",
                                children=[
                                    html.Span("BID",
                                              className="symbol-panel__quote-label"),
                                    html.Span("--", id=f"symbol-bid-{sym.base}",
                                              className="symbol-panel__bid"),
                                ],
                            ),
                            html.Div(
                                className="symbol-panel__quote",
                                children=[
                                    html.Span("ASK",
                                              className="symbol-panel__quote-label"),
                                    html.Span("--", id=f"symbol-ask-{sym.base}",
                                              className="symbol-panel__ask"),
                                ],
                            ),
                            html.Div(
                                className="symbol-panel__spread-block",
                                children=[
                                    html.Span("SPREAD",
                                              className="symbol-panel__quote-label"),
                                    html.Span("--", id=f"symbol-spread-{sym.base}",
                                              className="symbol-panel__spread"),
                                ],
                            ),
                            html.Span("--", id=f"symbol-tick-age-{sym.base}",
                                      className="symbol-panel__tick-age"),
                        ],
                    ),
                ],
            ),
            # Hero: at-a-glance "what matters right now" — distance to the
            # nearest structure level + direction. Replaces having to read
            # the full Structure list to know if anything is actionable.
            html.Div(
                id=f"symbol-hero-{sym.base}",
                className="symbol-panel__hero",
                children=[
                    html.Span("--",
                              id=f"symbol-hero-text-{sym.base}",
                              className="symbol-panel__hero-text"),
                ],
            ),
            html.Section(
                className="symbol-panel__section symbol-panel__section--structure",
                children=[
                    html.Header(
                        className="symbol-panel__section-title-row",
                        children=[
                            html.Span("Structure", className="symbol-panel__section-title"),
                            html.Span(
                                "",
                                id=f"symbol-confluence-{sym.base}",
                                className="symbol-panel__confluence",
                            ),
                        ],
                    ),
                    html.Div(
                        id=f"symbol-structure-{sym.base}",
                        className="structure-list",
                        children=[
                            html.Div("no levels yet",
                                     className="structure-list__empty"),
                        ],
                    ),
                ],
            ),
            html.Section(
                className="symbol-panel__section",
                children=[
                    html.Header("MTF Status", className="symbol-panel__section-title"),
                    html.Div(
                        className="mtf-table",
                        id=f"symbol-mtf-{sym.base}",
                        children=[_build_mtf_row(sym, tf) for tf in TIMEFRAMES],
                    ),
                ],
            ),
            html.Section(
                className="symbol-panel__section symbol-panel__section--pa",
                children=[
                    html.Header("Price Action (M15)", className="symbol-panel__section-title"),
                    html.Div(
                        id=f"symbol-pa-{sym.base}",
                        className="pa-list",
                        children=[
                            html.Div("no recent patterns",
                                     className="pa-list__empty"),
                        ],
                    ),
                ],
            ),
            html.Section(
                className="symbol-panel__section symbol-panel__section--bias",
                children=[
                    html.Header("Currency Bias",
                                className="symbol-panel__section-title"),
                    # SPEC §16.6 single-line format: "XAU vs USD: -53 (SELL優位)".
                    html.Div(
                        "--",
                        id=f"symbol-bias-{sym.base}",
                        className="currency-bias",
                    ),
                ],
            ),
        ],
    )
