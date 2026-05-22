"""Clientside callbacks: distribute WebSocket messages to every panel.

All updates happen in the browser to keep the 1-second price refresh
free of Python round-trips. Server-side state lives in the
``dcc.Store(id='ws-store')`` element; per-panel callbacks subscribe to
that store.

We avoid pattern-matching IDs because each callback writes a single
property of a single element — the wiring is repetitive but the resulting
JavaScript is trivial and easy to debug in the browser dev tools.
"""

from __future__ import annotations

from dash import Input, Output

import config


# --------------------------------------------------------------------------- #
# JS helpers shared between callbacks (injected via app.clientside_callback).
# --------------------------------------------------------------------------- #

_JS_NAMESPACE = "mt5dash"

# JST offset injected from config so the JS shares the project-wide constant
# (SPEC §23.2 — no magic numbers).
_JST_OFFSET_MS = int(config.JST_OFFSET_HOURS) * 3600 * 1000

_JS_HELPERS = f"""
if (!window.dash_clientside) {{ window.dash_clientside = {{}}; }}
if (!window.dash_clientside.{_JS_NAMESPACE}) {{
  const NS = {{}};

  NS.fmtPrice = function(value, digits) {{
    if (value === null || value === undefined || !isFinite(value)) return '--';
    return Number(value).toFixed(digits);
  }};
  NS.fmtSigned = function(value, digits) {{
    if (value === null || value === undefined || !isFinite(value)) return '--';
    const s = Number(value).toFixed(digits);
    return value > 0 ? '+' + s : s;
  }};
  NS.priceDigits = function(value) {{
    if (value === null || value === undefined || !isFinite(value)) return 2;
    const abs = Math.abs(value);
    if (abs >= 1000) return 2;     // XAUUSD
    if (abs >= 50)   return 3;     // JPY pairs
    return 5;                       // EUR/USD etc.
  }};
  NS.parseMsg = function(msg) {{
    if (!msg || !msg.data) return null;
    try {{ return JSON.parse(msg.data); }} catch (e) {{ return null; }}
  }};
  NS.fmtTimeJST = function(epochSec) {{
    if (!epochSec) return '--:--:--';
    const d = new Date(epochSec * 1000);
    const pad = n => String(n).padStart(2, '0');
    // JST offset from config.JST_OFFSET_HOURS, injected at registration.
    const jst = new Date(d.getTime() + {_JST_OFFSET_MS});
    return pad(jst.getUTCHours()) + ':' + pad(jst.getUTCMinutes()) + ':' + pad(jst.getUTCSeconds());
  }};

  // SPEC §17.1 distance-to-touch bands measured as multiples of H4 ATR.
  // touchClass returns the CSS modifier suffix for the structure row.
  NS.touchClass = function(distance, atrH4) {{
    if (atrH4 == null || !isFinite(atrH4) || atrH4 <= 0) return '';
    const abs = Math.abs(distance);
    if (abs <= 0.10 * atrH4) return 'touch';
    if (abs <= 0.30 * atrH4) return 'near';
    if (abs <= 0.50 * atrH4) return 'far';
    return '';
  }};

  // Friendly short labels for level categories shown as a column badge.
  NS.categoryLabel = function(cat) {{
    return ({{
      resistance: 'R',
      support: 'S',
      trend_up: 'TL↑',
      trend_down: 'TL↓',
      supply_zone: 'Sup',
      demand_zone: 'Dem',
      channel: 'CH',
      fibonacci: 'Fib',
      note: 'Txt',
      previous: 'Prev',
      round: 'Rnd',
      swing: 'Sw',
      session: 'Ses',
      vwap: 'VWAP',
      other: 'Lv',
    }})[cat] || 'Lv';
  }};

  NS.kindIcon = function(kind, direction) {{
    if (kind === 'pin_bull')          return '🪡⬆';
    if (kind === 'pin_bear')          return '🪡⬇';
    if (kind === 'engulf_bull')       return '🟢⬆';
    if (kind === 'engulf_bear')       return '🔴⬇';
    if (kind === 'inside')            return '⬛';
    if (kind === 'inside_break_up')   return '🟦⬆';
    if (kind === 'inside_break_down') return '🟦⬇';
    if (kind === 'three_bar_up')      return '🔺⬆';
    if (kind === 'three_bar_down')    return '🔻⬇';
    return direction > 0 ? '⬆' : direction < 0 ? '⬇' : '·';
  }};

  // Build a Dash html.Div description (component spec) so the calling
  // clientside callback can hand the list directly back to Dash.
  NS.div = function(props) {{
    return {{namespace: 'dash_html_components', type: 'Div', props: props}};
  }};
  NS.span = function(props) {{
    return {{namespace: 'dash_html_components', type: 'Span', props: props}};
  }};

  // Idempotency guard for callbacks whose data dimension only updates
  // every N seconds while ws-store itself ticks every 500 ms. Returns
  // true when the (key, stamp) pair has already been processed — caller
  // returns ``dash_clientside.no_update`` to skip the render.
  NS._stamps = {{}};
  NS.unchanged = function(key, stamp) {{
    if (stamp === undefined || stamp === null) return true;   // no data ⇒ skip
    if (NS._stamps[key] === stamp) return true;
    NS._stamps[key] = stamp;
    return false;
  }};

  window.dash_clientside.{_JS_NAMESPACE} = NS;
}}
"""


def _inject_helpers(app) -> None:
    """Append the shared JS namespace to Dash's index page."""
    app.clientside_callback(
        # An identity callback whose function string carries the helper namespace
        # via a leading IIFE. Dash injects the function once at page load.
        f"""
        function(msg) {{
            {_JS_HELPERS}
            if (!msg) return window.dash_clientside.no_update;
            const data = window.dash_clientside.{_JS_NAMESPACE}.parseMsg(msg);
            return data || window.dash_clientside.no_update;
        }}
        """,
        Output("ws-store", "data"),
        Input("ws", "message"),
    )


# --------------------------------------------------------------------------- #
# Header callbacks
# --------------------------------------------------------------------------- #

def _register_header(app) -> None:
    app.clientside_callback(
        f"""
        function(data) {{
            if (!data) return ['--:--:--', '0', '-- ms'];
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const clock = NS.fmtTimeJST(data.ts);
            const ver = data.version != null ? String(data.version) : '0';
            const ms = (data.analysis && isFinite(data.analysis.compute_ms))
                ? Number(data.analysis.compute_ms).toFixed(1) + ' ms'
                : '-- ms';
            return [clock, ver, ms];
        }}
        """,
        [
            Output("header-clock", "children"),
            Output("header-version", "children"),
            Output("header-compute-ms", "children"),
        ],
        Input("ws-store", "data"),
    )

    app.clientside_callback(
        """
        function(data) {
            if (!data || !data.status) return ['status-dot status-dot--off', 'disconnected'];
            const ok = !!data.status.connected;
            return [
                ok ? 'status-dot status-dot--on' : 'status-dot status-dot--off',
                ok ? 'connected' : 'disconnected',
            ];
        }
        """,
        [
            Output("header-status-dot", "className"),
            Output("header-status-text", "children"),
        ],
        Input("ws-store", "data"),
    )


# --------------------------------------------------------------------------- #
# Account card callbacks
# --------------------------------------------------------------------------- #

def _register_account(app) -> None:
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const dash = '--';
            if (!data || !data.account) {{
                return [dash, dash, dash, dash, dash, dash, dash, dash, dash, dash];
            }}
            const a = data.account;
            return [
                String(a.login),
                a.server || dash,
                NS.fmtPrice(a.balance, 2),
                NS.fmtPrice(a.equity, 2),
                NS.fmtSigned(a.profit, 2),
                NS.fmtPrice(a.margin, 2),
                NS.fmtPrice(a.margin_free, 2),
                isFinite(a.margin_level) ? NS.fmtPrice(a.margin_level, 1) + ' %' : dash,
                a.leverage ? '1:' + a.leverage : dash,
                a.currency || dash,
            ];
        }}
        """,
        [
            Output("account-login", "children"),
            Output("account-server", "children"),
            Output("account-balance", "children"),
            Output("account-equity", "children"),
            Output("account-profit", "children"),
            Output("account-margin", "children"),
            Output("account-margin-free", "children"),
            Output("account-margin-level", "children"),
            Output("account-leverage", "children"),
            Output("account-currency", "children"),
        ],
        Input("ws-store", "data"),
    )

    # Profit colour: positive → buy colour, negative → sell colour.
    app.clientside_callback(
        """
        function(data) {
            if (!data || !data.account) return 'account-card__metric-value';
            const p = Number(data.account.profit);
            if (!isFinite(p) || p === 0) return 'account-card__metric-value';
            return 'account-card__metric-value ' + (p > 0
                ? 'account-card__metric-value--positive'
                : 'account-card__metric-value--negative');
        }
        """,
        Output("account-profit", "className"),
        Input("ws-store", "data"),
    )

    # Positions list and count badge.
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const positions = (data && data.account && data.account.positions) || [];
            const count = positions.length;
            if (count === 0) {{
                return [
                    [{{namespace: 'dash_html_components', type: 'Div',
                       props: {{className: 'positions-list__empty',
                                children: 'No open positions'}}}}],
                    String(count),
                ];
            }}
            const rows = positions.map(p => {{
                const cls = 'position-row position-row--' + (p.type === 'BUY' ? 'buy' : 'sell');
                const digits = NS.priceDigits(p.price_open);
                return {{
                    namespace: 'dash_html_components',
                    type: 'Div',
                    props: {{
                        className: cls,
                        children: [
                            {{namespace: 'dash_html_components', type: 'Span',
                              props: {{className: 'position-row__type', children: p.type}}}},
                            {{namespace: 'dash_html_components', type: 'Span',
                              props: {{className: 'position-row__sym', children: p.symbol}}}},
                            {{namespace: 'dash_html_components', type: 'Span',
                              props: {{className: 'position-row__vol',
                                       children: p.volume.toFixed(2) + ' lot'}}}},
                            {{namespace: 'dash_html_components', type: 'Span',
                              props: {{className: 'position-row__px',
                                       children: NS.fmtPrice(p.price_open, digits) + ' → ' +
                                                 NS.fmtPrice(p.price_current, digits)}}}},
                            {{namespace: 'dash_html_components', type: 'Span',
                              props: {{className: p.profit >= 0 ?
                                            'position-row__pnl position-row__pnl--positive' :
                                            'position-row__pnl position-row__pnl--negative',
                                       children: NS.fmtSigned(p.profit, 2)}}}},
                        ],
                    }},
                }};
            }});
            return [rows, String(count)];
        }}
        """,
        [
            Output("account-positions-list", "children"),
            Output("account-position-count", "children"),
        ],
        Input("ws-store", "data"),
    )


# --------------------------------------------------------------------------- #
# Symbol-panel callbacks (one per symbol so updates are independent)
# --------------------------------------------------------------------------- #

def _register_symbol(app, base: str) -> None:
    """Bind one symbol's clientside callback to the store.

    Surfaces BID, ASK, and SPREAD as three independent fields plus broker
    name and tick age. SPREAD is shown both in price units (decimal) and
    in pips so the user can sanity-check both at a glance.
    """
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const dash = ['', '--', '--', '--', ''];

            if (!data) return dash;

            const analysis = data.analysis && data.analysis.by_symbol &&
                             data.analysis.by_symbol['{base}'];
            const broker = analysis ? analysis.broker_name : '';

            const tick = data.price && data.price.ticks && data.price.ticks['{base}'];
            if (!tick) return [broker, '--', '--', '--', ''];

            const digits = NS.priceDigits(tick.bid);
            const bid = NS.fmtPrice(tick.bid, digits);
            const ask = NS.fmtPrice(tick.ask, digits);
            // Hide spread when broker collapses bid==ask (0-spread is
            // pure noise, not a meaningful value).
            let spread = '';
            if (isFinite(tick.bid) && isFinite(tick.ask)) {{
                const spreadPrice = tick.ask - tick.bid;
                if (spreadPrice > 0) {{
                    const spreadPips = spreadPrice * Math.pow(10, digits);
                    spread = spreadPrice.toFixed(digits) +
                             ' (' + spreadPips.toFixed(1) + ')';
                }}
            }}
            let age = '';
            if (tick.time_msc && data.ts) {{
                const ageSec = data.ts - tick.time_msc / 1000;
                if (ageSec < 60)        age = ageSec.toFixed(1) + 's';
                else if (ageSec < 3600) age = (ageSec / 60).toFixed(0) + 'm';
                else                    age = (ageSec / 3600).toFixed(1) + 'h';
            }}
            return [broker, bid, ask, spread, age];
        }}
        """,
        [
            Output(f"symbol-broker-{base}", "children"),
            Output(f"symbol-bid-{base}", "children"),
            Output(f"symbol-ask-{base}", "children"),
            Output(f"symbol-spread-{base}", "children"),
            Output(f"symbol-tick-age-{base}", "children"),
        ],
        Input("ws-store", "data"),
    )

    # Per-TF rows. We only wire outputs for cells that actually exist in the
    # panel; SPEC §6 restricts which indicators are *displayed* on each TF
    # (computation still runs on all TFs).
    for tf in config.TIMEFRAMES:
        tf_label = tf.label
        outputs: list[Output] = [
            Output(f"symbol-mtf-{base}-{tf_label}-arrow", "children"),
            Output(f"symbol-mtf-{base}-{tf_label}-arrow", "className"),
            Output(f"symbol-mtf-{base}-{tf_label}-ema", "children"),
        ]
        # Track which slots are present so the JS returns matching length.
        slot_flags = {"adx": False, "rsi": False, "atr": False}
        if tf_label in config.ADX_DISPLAY_TFS:
            outputs.append(Output(f"symbol-mtf-{base}-{tf_label}-adx", "children"))
            slot_flags["adx"] = True
        if tf_label in config.RSI_DISPLAY_TFS:
            outputs.append(Output(f"symbol-mtf-{base}-{tf_label}-rsi", "children"))
            slot_flags["rsi"] = True
        if tf_label in config.ATR_DISPLAY_TFS:
            outputs.append(Output(f"symbol-mtf-{base}-{tf_label}-atr", "children"))
            slot_flags["atr"] = True

        # Build the JS empty-state and value-state arrays to match `outputs`.
        empty_extras = []
        if slot_flags["adx"]: empty_extras.append("'ADX --'")
        if slot_flags["rsi"]: empty_extras.append("'RSI --'")
        if slot_flags["atr"]: empty_extras.append("'ATR --'")
        empty_arr = "['·', 'mtf-row__arrow', 'EMA --'" + \
                    ("," + ",".join(empty_extras) if empty_extras else "") + "]"

        value_lines = [
            "const arrow = tf.above_ema === true ? '▲' : tf.above_ema === false ? '▼' : '·';",
            "const arrowClass = tf.above_ema === true ? 'mtf-row__arrow mtf-row__arrow--up'"
            " : tf.above_ema === false ? 'mtf-row__arrow mtf-row__arrow--down'"
            " : 'mtf-row__arrow';",
            "const digits = NS.priceDigits(tf.last_close);",
            "const ema = (tf.ema != null && isFinite(tf.ema))"
            f"  ? 'EMA' + tf.ema_period + ' ' + Number(tf.ema).toFixed(digits)"
            f"  : 'EMA' + tf.ema_period + ' --';",
        ]
        return_parts = ["arrow", "arrowClass", "ema"]
        if slot_flags["adx"]:
            value_lines.append(
                "const adx = (tf.adx != null && isFinite(tf.adx))"
                " ? 'ADX ' + Number(tf.adx).toFixed(1) : 'ADX --';"
            )
            return_parts.append("adx")
        if slot_flags["rsi"]:
            value_lines.append(
                "const rsi = (tf.rsi != null && isFinite(tf.rsi))"
                " ? 'RSI ' + Number(tf.rsi).toFixed(1) : 'RSI --';"
            )
            return_parts.append("rsi")
        if slot_flags["atr"]:
            value_lines.append(
                "const atr = (tf.atr != null && isFinite(tf.atr))"
                " ? 'ATR ' + Number(tf.atr).toFixed(digits) : 'ATR --';"
            )
            return_parts.append("atr")

        body = "\n            ".join(value_lines)
        ret = ", ".join(return_parts)

        app.clientside_callback(
            f"""
            function(data) {{
                const NS = window.dash_clientside.{_JS_NAMESPACE};
                const empty = {empty_arr};
                if (!data || !data.analysis) return empty;
                // Indicators only refresh every 5 s; short-circuit the
                // 500 ms price tick to avoid 20+ no-op DOM patches per cycle.
                if (NS.unchanged('mtf:{base}:{tf_label}',
                                  data.analysis.generated_at))
                    return window.dash_clientside.no_update;
                const sym = data.analysis.by_symbol && data.analysis.by_symbol['{base}'];
                if (!sym) return empty;
                const tf = sym.by_tf && sym.by_tf['{tf_label}'];
                if (!tf) return empty;
                {body}
                return [{ret}];
            }}
            """,
            outputs,
            Input("ws-store", "data"),
        )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def register_clientside_callbacks(app) -> None:
    """Wire every clientside callback. Idempotent within a single app."""
    from dashboard.callbacks_structures import register_structure_callbacks
    from dashboard.callbacks_phase3 import register_phase3_callbacks
    from dashboard.callbacks_phase4 import register_phase4_callbacks
    _inject_helpers(app)
    _register_header(app)
    _register_account(app)
    for sym in config.SYMBOLS:
        _register_symbol(app, sym.base)
    register_structure_callbacks(app)
    register_phase3_callbacks(app)
    register_phase4_callbacks(app)
