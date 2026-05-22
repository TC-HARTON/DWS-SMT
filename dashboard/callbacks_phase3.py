"""Phase 3 clientside callbacks: strength meter, correlation heatmap,
performance card, currency-bias per symbol.

Split from :mod:`dashboard.callbacks` so the original module stays close
to SPEC §23.3's 300-line guideline. The Phase 1 helpers
(``window.dash_clientside.mt5dash``) are already installed by the
:func:`dashboard.callbacks._inject_helpers` call.
"""

from __future__ import annotations

from dash import Input, Output, State

import config
from dashboard.components.correlation_heatmap import HEATMAP_COLORSCALE

_JS_NAMESPACE = "mt5dash"


# --------------------------------------------------------------------------- #
# Window selector buttons → store
# --------------------------------------------------------------------------- #

def _register_selector(
    app,
    *,
    values: list,
    store_id: str,
    btn_id_fmt: str,
    base_class: str,
    label_fn=lambda v: v,
    payload_fn=lambda v: repr(v),
    eq_js=lambda payload_lit: f"active === {payload_lit}",
) -> None:
    """Register N buttons → 1 store callback + N active-state class callbacks.

    Used by every Phase 3 picker (strength window / correlation window /
    performance range). Each button writes its value to ``store_id``; each
    button toggles between ``base_class`` and ``base_class + ' ' + base_class--active``
    based on the store contents.

    Args:
        values: list of picker values.
        store_id: id of the dcc.Store that holds the active value.
        btn_id_fmt: format string with ``{label}`` for the button id.
        base_class: base CSS class shared by all buttons.
        label_fn: extract the display label from a value (defaults to identity).
        payload_fn: serialise a value to a JS literal (defaults to repr).
        eq_js: build the JS equality test for the active-style callback.
    """
    for v in values:
        label = label_fn(v)
        payload = payload_fn(v)
        btn_id = btn_id_fmt.format(label=label)
        app.clientside_callback(
            f"function(_n) {{ return {payload}; }}",
            Output(store_id, "data", allow_duplicate=True),
            Input(btn_id, "n_clicks"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            f"""
            function(active) {{
                return {eq_js(payload)}
                    ? '{base_class} {base_class}--active'
                    : '{base_class}';
            }}
            """,
            Output(btn_id, "className"),
            Input(store_id, "data"),
        )


def _register_strength_selector(app) -> None:
    _register_selector(
        app,
        values=list(config.STRENGTH_WINDOWS),
        store_id="strength-window-store",
        btn_id_fmt="strength-window-btn-{label}",
        base_class="strength-window-btn",
        label_fn=lambda w: w.label,
        payload_fn=lambda w: f"'{w.label}'",
    )


def _register_correlation_selector(app) -> None:
    _register_selector(
        app,
        values=list(config.CORRELATION_WINDOWS_BARS),
        store_id="correlation-window-store",
        btn_id_fmt="correlation-window-btn-{label}",
        base_class="correlation-window-btn",
        label_fn=lambda b: str(b),
        payload_fn=lambda b: str(b),
        eq_js=lambda lit: f"Number(active) === {lit}",
    )


def _register_performance_selector(app) -> None:
    _register_selector(
        app,
        values=list(config.HISTORY_RANGES),
        store_id="performance-range-store",
        btn_id_fmt="perf-range-btn-{label}",
        base_class="performance-card__range-btn",
        label_fn=lambda r: r.label,
        payload_fn=lambda r: f"'{r.label}'",
    )


# --------------------------------------------------------------------------- #
# Currency strength meter renderer
# --------------------------------------------------------------------------- #

def _register_strength_meter(app) -> None:
    currencies = list(config.FIAT_CURRENCIES) + ["XAU"]
    for ccy in currencies:
        app.clientside_callback(
            f"""
            function(data, windowLabel) {{
                const NS = window.dash_clientside.{_JS_NAMESPACE};
                const dash = ['--', {{width: '0%', left: '50%'}}, 'strength-row__bar'];
                if (!data || !data.strength) return dash;
                // Strength refreshes every 30s; ws-store ticks every 500ms.
                if (NS.unchanged('strength:{ccy}:' + windowLabel,
                                  data.strength.generated_at + ':' + windowLabel))
                    return window.dash_clientside.no_update;
                const w = (data.strength.by_window || {{}})[windowLabel];
                if (!w) return dash;
                const sc = (w.scores || {{}})['{ccy}'];
                if (!sc || sc.score == null) return dash;
                const score = sc.score;
                // The bar starts at the midline (50) and extends in the
                // direction of strength. Width = |score - 50| * 2 (%);
                // for the negative side the bar's left edge moves so it
                // hugs the centre line growing leftward.
                const offset = Math.abs(score - 50) * 2;
                const width = Math.min(offset, 100);
                let left, mod;
                if (score >= 50) {{
                    left = '50%';
                    mod = 'strength-row__bar--pos';
                }} else {{
                    left = (50 - width / 2) + '%';
                    // Width refers to the bar length, but for negative scores
                    // we want left edge at (50 - width)%.
                    left = (50 - width) + '%';
                    mod = 'strength-row__bar--neg';
                }}
                const cls = 'strength-row__bar ' + mod +
                            (sc.is_reference ? ' strength-row__bar--ref' : '');
                return [score.toFixed(1),
                        {{width: width + '%', left: left}},
                        cls];
            }}
            """,
            [
                Output(f"strength-val-{ccy}", "children"),
                Output(f"strength-bar-{ccy}", "style"),
                Output(f"strength-bar-{ccy}", "className"),
            ],
            [Input("ws-store", "data"),
             Input("strength-window-store", "data")],
        )


# --------------------------------------------------------------------------- #
# Currency-bias section on each symbol panel
# --------------------------------------------------------------------------- #

_JP_LABEL = {
    "STRONG BUY":  "STRONG BUY優位",
    "BUY":         "BUY優位",
    "NEUTRAL":     "NEUTRAL",
    "SELL":        "SELL優位",
    "STRONG SELL": "STRONG SELL優位",
}


def _register_currency_bias(app, base: str) -> None:
    """SPEC §16.6 format: "{base} vs {quote}: {delta:+.0f} ({label})"."""
    import json as _json
    label_map_js = _json.dumps(_JP_LABEL)
    app.clientside_callback(
        f"""
        function(data, windowLabel) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            // NEUTRAL / no data → hide entirely (display: none via class).
            const hide = ['', 'currency-bias currency-bias--hidden'];
            if (!data || !data.strength) return hide;
            if (NS.unchanged('bias:{base}:' + windowLabel,
                              data.strength.generated_at + ':' + windowLabel))
                return window.dash_clientside.no_update;
            const w = (data.strength.by_window || {{}})[windowLabel];
            if (!w) return hide;
            const bias = (w.pair_biases || {{}})['{base}'];
            if (!bias || bias.label === 'NEUTRAL') return hide;
            const labelMap = {label_map_js};
            const delta = bias.delta;
            const sign = delta > 0 ? '+' : '';
            const deltaStr = sign + Number(delta).toFixed(0);
            const txt = bias.base + ' vs ' + bias.quote + ': ' + deltaStr +
                        ' (' + (labelMap[bias.label] || bias.label) + ')';
            let mod = 'buy';
            if (bias.label === 'STRONG BUY')       mod = 'strong-buy';
            else if (bias.label === 'BUY')         mod = 'buy';
            else if (bias.label === 'SELL')        mod = 'sell';
            else if (bias.label === 'STRONG SELL') mod = 'strong-sell';
            return [txt, 'currency-bias currency-bias--' + mod];
        }}
        """,
        [
            Output(f"symbol-bias-{base}", "children"),
            Output(f"symbol-bias-{base}", "className"),
        ],
        [Input("ws-store", "data"),
         Input("strength-window-store", "data")],
    )


# --------------------------------------------------------------------------- #
# Correlation heatmap figure
# --------------------------------------------------------------------------- #

def _register_correlation_figure(app) -> None:
    # Serialise the SPEC §13.3 colorscale at module import so the JS string
    # carries the literal values.
    import json as _json
    colorscale_js = _json.dumps(HEATMAP_COLORSCALE)
    app.clientside_callback(
        f"""
        function(data, bars) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const fallback = {{data: [], layout: {{
                paper_bgcolor: '#1e222d', plot_bgcolor: '#1e222d',
                annotations: [{{text: 'waiting for data…',
                                font: {{color: '#787b86'}},
                                xref: 'paper', yref: 'paper',
                                x: 0.5, y: 0.5, showarrow: false}}],
                margin: {{l: 60, r: 10, t: 10, b: 60}},
            }}}};
            if (!data || !data.correlation) return fallback;
            if (NS.unchanged('corr:' + bars,
                              data.correlation.generated_at + ':' + bars))
                return window.dash_clientside.no_update;
            const w = (data.correlation.by_window || {{}})[String(bars)];
            if (!w) return fallback;
            const colorscale = {colorscale_js};
            const figure = {{
                data: [{{
                    type: 'heatmap',
                    z: w.matrix,
                    x: w.symbols,
                    y: w.symbols,
                    zmin: -1, zmax: 1,
                    colorscale: colorscale,
                    colorbar: {{thickness: 8, tickfont: {{color: '#d1d4dc', size: 10}},
                                tickvals: [-1, -0.5, 0, 0.5, 1]}},
                    hovertemplate: '%{{x}} vs %{{y}}: %{{z:.2f}}<extra></extra>',
                }}],
                layout: {{
                    paper_bgcolor: '#1e222d',
                    plot_bgcolor: '#1e222d',
                    font: {{color: '#d1d4dc', size: 10}},
                    margin: {{l: 60, r: 30, t: 5, b: 50}},
                    xaxis: {{tickangle: -45, automargin: true,
                             showgrid: false, zeroline: false}},
                    yaxis: {{automargin: true, autorange: 'reversed',
                             showgrid: false, zeroline: false}},
                }},
            }};
            return figure;
        }}
        """,
        Output("correlation-figure", "figure"),
        [Input("ws-store", "data"),
         Input("correlation-window-store", "data")],
    )


# --------------------------------------------------------------------------- #
# Performance card
# --------------------------------------------------------------------------- #

def _register_today_pnl(app) -> None:
    """SPEC §14.1 当日P&L: realised closes since UTC midnight + floating."""
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const dash = ['--', 'account-card__metric-value'];
            if (!data || !data.performance) return dash;
            const total = data.performance.today_total_pnl;
            if (total == null) return dash;
            const realised = data.performance.today_realised_pnl;
            const floating = data.performance.today_floating_pnl;
            const sign = total > 0 ? '+' : '';
            const txt = sign + Number(total).toFixed(2) +
                ' (R ' + sign + Number(realised).toFixed(0) +
                ' / F ' + (floating >= 0 ? '+' : '') +
                Number(floating).toFixed(0) + ')';
            const mod = total > 0 ? 'account-card__metric-value--positive'
                      : total < 0 ? 'account-card__metric-value--negative' : '';
            return [txt, 'account-card__metric-value ' + mod];
        }}
        """,
        [Output("account-today-pnl", "children"),
         Output("account-today-pnl", "className")],
        Input("ws-store", "data"),
    )


def _register_performance(app) -> None:
    app.clientside_callback(
        f"""
        function(data, rangeLabel) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const dash = ['--', '--', '--', '--', '--', '--'];
            if (!data || !data.performance) return dash;
            if (NS.unchanged('perf:metrics:' + rangeLabel,
                              data.performance.generated_at + ':' + rangeLabel))
                return window.dash_clientside.no_update;
            const r = (data.performance.by_range || {{}})[rangeLabel];
            if (!r) return dash;
            const trades = String(r.trade_count);
            const wr = r.win_rate != null
                       ? (r.win_rate * 100).toFixed(1) + '%' : '--';
            const net = r.net_profit != null
                        ? (r.net_profit > 0 ? '+' : '') +
                          Number(r.net_profit).toFixed(2)
                        : '--';
            const pf = r.profit_factor != null
                       ? Number(r.profit_factor).toFixed(2) : '∞';
            const dd = r.max_drawdown_abs != null
                       ? '-' + Number(r.max_drawdown_abs).toFixed(2) +
                         (r.max_drawdown_pct ? ' (' + r.max_drawdown_pct.toFixed(1) + '%)' : '')
                       : '--';
            const rr = r.risk_reward != null
                       ? Number(r.risk_reward).toFixed(2) : '--';
            return [trades, wr, net, pf, dd, rr];
        }}
        """,
        [
            Output("perf-trades", "children"),
            Output("perf-winrate", "children"),
            Output("perf-net", "children"),
            Output("perf-pf", "children"),
            Output("perf-dd", "children"),
            Output("perf-rr", "children"),
        ],
        [Input("ws-store", "data"),
         Input("performance-range-store", "data")],
    )

    # Net profit colour
    app.clientside_callback(
        f"""
        function(data, rangeLabel) {{
            if (!data || !data.performance) return 'performance-card__metric-value';
            const r = (data.performance.by_range || {{}})[rangeLabel];
            if (!r || r.net_profit == null) return 'performance-card__metric-value';
            return 'performance-card__metric-value ' + (r.net_profit > 0
                ? 'performance-card__metric-value--positive'
                : r.net_profit < 0 ? 'performance-card__metric-value--negative' : '');
        }}
        """,
        Output("perf-net", "className"),
        [Input("ws-store", "data"),
         Input("performance-range-store", "data")],
    )

    # Per-symbol PnL list
    app.clientside_callback(
        f"""
        function(data, rangeLabel) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const emptyChild = [NS.div({{className: 'perf-symbol-list__empty',
                                         children: 'no closed trades in window'}})];
            if (!data || !data.performance) return emptyChild;
            const r = (data.performance.by_range || {{}})[rangeLabel];
            if (!r || !r.by_symbol) return emptyChild;
            const rows = Object.values(r.by_symbol);
            if (rows.length === 0) return emptyChild;
            rows.sort((a, b) => (b.net_profit || 0) - (a.net_profit || 0));
            return rows.map(s => {{
                const net = s.net_profit;
                const cls = 'perf-symbol-row perf-symbol-row--' +
                            (net > 0 ? 'win' : net < 0 ? 'loss' : 'flat');
                const netStr = (net > 0 ? '+' : '') + Number(net).toFixed(2);
                const wr = (s.win_rate * 100).toFixed(0) + '%';
                return NS.div({{
                    className: cls,
                    children: [
                        NS.span({{className: 'perf-symbol-row__sym',
                                  children: s.symbol}}),
                        NS.span({{className: 'perf-symbol-row__n',
                                  children: s.trade_count + ' tr'}}),
                        NS.span({{className: 'perf-symbol-row__wr',
                                  children: wr}}),
                        NS.span({{className: 'perf-symbol-row__net',
                                  children: netStr}}),
                    ],
                }});
            }});
        }}
        """,
        Output("perf-by-symbol", "children"),
        [Input("ws-store", "data"),
         Input("performance-range-store", "data")],
    )

    # SPEC §14.3 時間帯別損益 — 24 bars (JST hour 0..23) sized by absolute PnL,
    # coloured green/red. A bar's tooltip shows the exact PnL.
    app.clientside_callback(
        f"""
        function(data, rangeLabel) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const emptyChild = [NS.div({{className: 'perf-hour-bars__empty',
                                         children: 'no closed trades in window'}})];
            if (!data || !data.performance) return emptyChild;
            const r = (data.performance.by_range || {{}})[rangeLabel];
            if (!r || !r.by_hour_jst) return emptyChild;
            const byHour = r.by_hour_jst;
            const hours = Object.keys(byHour);
            if (hours.length === 0) return emptyChild;
            // Determine max absolute PnL to scale bars to a fixed pixel height.
            let maxAbs = 1;
            for (const h of hours) {{
                const v = byHour[h];
                if (v != null && Math.abs(v) > maxAbs) maxAbs = Math.abs(v);
            }}
            const bars = [];
            for (let h = 0; h < 24; h++) {{
                const v = byHour[String(h)] || 0;
                const pct = Math.min(100, Math.abs(v) / maxAbs * 100);
                const cls = 'perf-hour-bar perf-hour-bar--' +
                            (v > 0 ? 'pos' : v < 0 ? 'neg' : 'flat');
                bars.push(NS.div({{
                    className: cls,
                    title: 'JST ' + h + ':00  ' + (v > 0 ? '+' : '') +
                           Number(v).toFixed(2),
                    children: [
                        NS.div({{className: 'perf-hour-bar__pos',
                                 style: {{height: (v > 0 ? pct : 0) + '%'}}}}),
                        NS.div({{className: 'perf-hour-bar__neg',
                                 style: {{height: (v < 0 ? pct : 0) + '%'}}}}),
                        NS.div({{className: 'perf-hour-bar__label',
                                 children: String(h)}}),
                    ],
                }}));
            }}
            return bars;
        }}
        """,
        Output("perf-by-hour", "children"),
        [Input("ws-store", "data"),
         Input("performance-range-store", "data")],
    )


# --------------------------------------------------------------------------- #
# Public registration
# --------------------------------------------------------------------------- #

def register_phase3_callbacks(app) -> None:
    """Wire every Phase 3 clientside callback."""
    _register_strength_selector(app)
    _register_correlation_selector(app)
    _register_performance_selector(app)
    _register_strength_meter(app)
    _register_correlation_figure(app)
    _register_today_pnl(app)
    _register_performance(app)
    for sym in config.SYMBOLS:
        _register_currency_bias(app, sym.base)
