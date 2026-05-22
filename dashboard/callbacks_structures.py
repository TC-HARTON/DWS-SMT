"""Phase 2 clientside callbacks: structure list / confluence badge / PA list.

Split out of :mod:`dashboard.callbacks` (which already exceeded the SPEC
§23.3 300-line guideline). One ``register_structure_callbacks(app)`` entry
point installs every Phase 2 callback per configured symbol.

These callbacks read the ``ws-store`` data the Phase 1 helper populates
and write to the per-symbol IDs created by
:func:`dashboard.components.symbol_panel.build_symbol_panel`.
"""

from __future__ import annotations

from dash import Input, Output

import config

# Keep the JS namespace name in sync with dashboard.callbacks._JS_NAMESPACE.
_JS_NAMESPACE = "mt5dash"


def _register_structure_list(app, base: str) -> None:
    """Render the per-symbol Structure list with SPEC §17.1 highlight coloring."""
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const emptyChild = [NS.div({{className: 'structure-list__empty',
                                        children: 'no levels yet'}})];
            if (!data || !data.structures) return emptyChild;
            const levels = (data.structures.levels_by_symbol || {{}})['{base}'] || [];
            if (levels.length === 0) return emptyChild;

            // Current bid drives distance-to-touch coloring.
            const tick = data.price && data.price.ticks && data.price.ticks['{base}'];
            const price = tick ? tick.bid : null;
            // H4 ATR drives SPEC §17.1 threshold bands.
            const sym = data.analysis && data.analysis.by_symbol &&
                        data.analysis.by_symbol['{base}'];
            const h4 = sym && sym.by_tf && sym.by_tf['H4'];
            const atrH4 = h4 ? h4.atr : null;
            const digits = NS.priceDigits(price || (levels[0] && levels[0].price));

            // Annotate with distance and sort by ascending distance to price.
            const items = levels.filter(l => l.price != null && isFinite(l.price))
                .map(l => {{
                    const dist = price != null ? l.price - price : NaN;
                    return Object.assign({{}}, l, {{
                        _distance: dist,
                        _absDistance: isFinite(dist) ? Math.abs(dist) : Infinity,
                        _touch: NS.touchClass(dist, atrH4),
                    }});
                }})
                .sort((a, b) => a._absDistance - b._absDistance)
                .slice(0, 12);

            return items.map(l => {{
                const cls = 'structure-row structure-row--' + l.source +
                            (l._touch ? ' structure-row--' + l._touch : '');
                const cat = NS.categoryLabel(l.category);
                const distStr = isFinite(l._distance)
                    ? (l._distance > 0 ? '+' : '') + l._distance.toFixed(digits)
                    : '';
                const meta = [];
                if (l.tf) meta.push(l.tf);
                if (l.importance >= 3) meta.push('strong');
                else if (l.importance === 2) meta.push('major');
                return NS.div({{
                    className: cls,
                    children: [
                        NS.span({{className: 'structure-row__cat', children: cat}}),
                        NS.span({{className: 'structure-row__name', children: l.name}}),
                        NS.span({{className: 'structure-row__price',
                                  children: NS.fmtPrice(l.price, digits)}}),
                        NS.span({{className: 'structure-row__dist', children: distStr}}),
                        NS.span({{className: 'structure-row__meta',
                                  children: meta.join(' · ')}}),
                    ],
                }});
            }});
        }}
        """,
        Output(f"symbol-structure-{base}", "children"),
        Input("ws-store", "data"),
    )


def _register_confluence_badge(app, base: str) -> None:
    """SPEC §10.4 confluence badge: ★ + element count + centre price."""
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            if (!data || !data.structures) return ['', 'symbol-panel__confluence'];
            const clusters = (data.structures.confluences_by_symbol || {{}})['{base}'] || [];
            if (clusters.length === 0) return ['', 'symbol-panel__confluence'];
            const tick = data.price && data.price.ticks && data.price.ticks['{base}'];
            const price = tick ? tick.bid : null;
            const digits = NS.priceDigits(price || (clusters[0] && clusters[0].center));
            // Show the highest-score cluster (already sorted by the backend).
            const c = clusters[0];
            const n = c.level_names.length;
            const label = c.importance_label + ' ' + n + 'x @ ' +
                          NS.fmtPrice(c.center, digits);
            return [label,
                    'symbol-panel__confluence symbol-panel__confluence--active'];
        }}
        """,
        [
            Output(f"symbol-confluence-{base}", "children"),
            Output(f"symbol-confluence-{base}", "className"),
        ],
        Input("ws-store", "data"),
    )


def _register_pa_list(app, base: str) -> None:
    """SPEC §11.3 gated PA list: only displays patterns when at least one
    structure level is within the ATR×0.5 touch band of current price."""
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const emptyChild = [NS.div({{className: 'pa-list__empty',
                                         children: 'no recent patterns near structure'}})];
            if (!data || !data.structures) return emptyChild;
            const events = (data.structures.price_action_by_symbol || {{}})['{base}'] || [];
            if (events.length === 0) return emptyChild;

            // SPEC §11.3 gate: at least one structure level must be within
            // the ATR×0.5 touch band.
            const levels = (data.structures.levels_by_symbol || {{}})['{base}'] || [];
            const tick = data.price && data.price.ticks && data.price.ticks['{base}'];
            const price = tick ? tick.bid : null;
            const sym = data.analysis && data.analysis.by_symbol &&
                        data.analysis.by_symbol['{base}'];
            const h4 = sym && sym.by_tf && sym.by_tf['H4'];
            const atrH4 = h4 ? h4.atr : null;
            if (price == null || atrH4 == null || !isFinite(atrH4) || atrH4 <= 0) {{
                return emptyChild;
            }}
            const touching = levels.some(l =>
                l.price != null && isFinite(l.price)
                && NS.touchClass(l.price - price, atrH4) !== '');
            if (!touching) return emptyChild;

            const digits = NS.priceDigits(price || (events[0] && events[0].close));

            // Show newest-first; cap to 6 to keep panels compact.
            return events.slice().reverse().slice(0, 6).map(ev => {{
                const cls = 'pa-row pa-row--' + (ev.direction > 0 ? 'bull'
                          : ev.direction < 0 ? 'bear' : 'neutral');
                const icon = NS.kindIcon(ev.kind, ev.direction);
                const age = ev.bar_index_from_end === 0
                          ? 'now'
                          : '-' + ev.bar_index_from_end + 'b';
                return NS.div({{
                    className: cls,
                    children: [
                        NS.span({{className: 'pa-row__icon', children: icon}}),
                        NS.span({{className: 'pa-row__kind', children: ev.kind}}),
                        NS.span({{className: 'pa-row__close',
                                  children: NS.fmtPrice(ev.close, digits)}}),
                        NS.span({{className: 'pa-row__age', children: age}}),
                    ],
                }});
            }});
        }}
        """,
        Output(f"symbol-pa-{base}", "children"),
        Input("ws-store", "data"),
    )


def _register_hero(app, base: str) -> None:
    """Hero line: 'NEAREST R1 +12.3 pip · TOUCHING' — the headline metric.

    Updates panel state via class names too: ``--quiet`` when nothing is in
    play, ``--active`` when at least one level is within ATR×0.5,
    ``--touch`` when within ATR×0.1.
    """
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const empty = ['--', 'symbol-panel symbol-panel--{{size}} symbol-panel--quiet'];
            // We need the size class from the static DOM; read it once.
            const panel = document.getElementById('symbol-panel-{base}');
            const baseCls = panel ? Array.from(panel.classList)
                .filter(c => c.startsWith('symbol-panel--') &&
                             !c.endsWith('--quiet') &&
                             !c.endsWith('--active') &&
                             !c.endsWith('--touch'))
                .concat(['symbol-panel'])
                .join(' ') : 'symbol-panel';

            if (!data || !data.structures) return ['--', baseCls + ' symbol-panel--quiet'];

            const levels = (data.structures.levels_by_symbol || {{}})['{base}'] || [];
            const tick = data.price && data.price.ticks && data.price.ticks['{base}'];
            const price = tick ? tick.bid : null;
            const sym = data.analysis && data.analysis.by_symbol &&
                        data.analysis.by_symbol['{base}'];
            const h4 = sym && sym.by_tf && sym.by_tf['H4'];
            const atrH4 = h4 ? h4.atr : null;

            if (price == null || atrH4 == null || levels.length === 0) {{
                return ['no signal', baseCls + ' symbol-panel--quiet'];
            }}

            // Find nearest level.
            let nearest = null;
            let nearestDist = Infinity;
            for (const l of levels) {{
                if (l.price == null || !isFinite(l.price)) continue;
                const d = Math.abs(l.price - price);
                if (d < nearestDist) {{
                    nearestDist = d;
                    nearest = l;
                }}
            }}
            if (!nearest) return ['no signal', baseCls + ' symbol-panel--quiet'];

            const digits = NS.priceDigits(price);
            const signed = nearest.price - price;
            const pip = signed * Math.pow(10, digits);
            const tier = NS.touchClass(signed, atrH4);
            const arrow = signed > 0 ? '↑' : signed < 0 ? '↓' : '·';
            const cat = NS.categoryLabel(nearest.category);
            const stateLabel = tier === 'touch' ? 'TOUCHING'
                             : tier === 'near'  ? 'NEAR'
                             : tier === 'far'   ? 'APPROACH'
                             : 'idle';
            const txt = arrow + ' ' + cat + ' ' + NS.fmtPrice(nearest.price, digits) +
                        '  ' + (pip > 0 ? '+' : '') + pip.toFixed(1) + ' pip' +
                        '  · ' + stateLabel;

            let panelCls = baseCls + ' symbol-panel--quiet';
            if (tier === 'touch') panelCls = baseCls + ' symbol-panel--touch';
            else if (tier === 'near' || tier === 'far')
                panelCls = baseCls + ' symbol-panel--active';

            return [txt, panelCls];
        }}
        """,
        [
            Output(f"symbol-hero-text-{base}", "children"),
            Output(f"symbol-panel-{base}", "className"),
        ],
        Input("ws-store", "data"),
    )


def register_structure_callbacks(app) -> None:
    """Bind structure, confluence, and PA callbacks for every configured symbol."""
    for sym in config.SYMBOLS:
        _register_hero(app, sym.base)
        _register_structure_list(app, sym.base)
        _register_confluence_badge(app, sym.base)
        _register_pa_list(app, sym.base)
