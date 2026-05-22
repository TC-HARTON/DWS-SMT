"""Phase 4 clientside callbacks: economic-calendar render + 1 s countdown.

The render callback (``ws-store`` change) builds the row list once per
WebSocket message — the calendar refresh is hourly, so this fires very
rarely. The countdown callback (``calendar-tick`` 1 s interval) walks
the rendered rows and updates only the countdown text + warning class
so we don't repaint the entire list every second.
"""

from __future__ import annotations

from dash import Dash, Input, Output

import config

_JS_NAMESPACE = "mt5dash"


def _register_calendar_list(app: Dash) -> None:
    """Build the calendar rows from the latest WS snapshot.

    ``ws-store`` fires every 1 s (driven by price ticks) but the calendar
    payload only changes once per hour. We therefore short-circuit with
    ``no_update`` when ``calendar.generated_at`` is unchanged — otherwise
    the render would clobber the countdown callback's in-place DOM mutation
    every second.
    """
    app.clientside_callback(
        f"""
        function(data) {{
            const NS = window.dash_clientside.{_JS_NAMESPACE};
            const empty = [NS.div({{className: 'calendar-list__empty',
                                    children: 'no high-impact events this week'}})];
            if (!data || !data.calendar) return empty;
            const cal = data.calendar;
            // Idempotency guard: skip the render unless the calendar payload
            // actually changed. Without it, the 1 s price tick repaints the
            // rows and wipes the countdown JS that just wrote to them.
            const stamp = cal.generated_at;
            if (window._calLastStamp === stamp) {{
                return window.dash_clientside.no_update;
            }}
            window._calLastStamp = stamp;

            const events = cal.events || [];
            if (events.length === 0) return empty;
            const nowSec = Date.now() / 1000;
            const maxRows = cal.display_count || 12;
            // Only forward-looking + freshly-released events make it onto the
            // panel; we keep events for up to `warning_window_sec` *after*
            // release so the warning colour stays visible right after the print.
            const cutoff = nowSec - (cal.warning_window_sec || 1800);
            const rows = events
                .filter(e => e.release_ts >= cutoff)
                .slice(0, maxRows)
                .map(e => {{
                    return NS.div({{
                        // Pre-tag the release timestamp on the DOM via id so
                        // the countdown callback can read it without round-trips.
                        id: 'calendar-row-' + Math.floor(e.release_ts) + '-' +
                            e.currency,
                        className: 'calendar-row',
                        children: [
                            NS.span({{className: 'calendar-row__time',
                                      'data-release': e.release_ts,
                                      children: '--:--'}}),
                            NS.span({{className: 'calendar-row__ccy',
                                      children: e.currency}}),
                            NS.span({{className: 'calendar-row__impact',
                                      children: '🔴'}}),
                            NS.span({{className: 'calendar-row__title',
                                      title: e.title, children: e.title}}),
                            NS.span({{className: 'calendar-row__forecast',
                                      title: 'forecast',
                                      children: e.forecast || '--'}}),
                            NS.span({{className: 'calendar-row__previous',
                                      title: 'previous',
                                      children: e.previous || '--'}}),
                            NS.span({{className: 'calendar-row__countdown',
                                      'data-release': e.release_ts,
                                      children: '--'}}),
                        ],
                    }});
                }});
            return rows;
        }}
        """,
        Output("calendar-list", "children"),
        Input("ws-store", "data"),
    )


def _register_calendar_source(app: Dash) -> None:
    """Tiny badge next to the calendar header showing the active source."""
    app.clientside_callback(
        f"""
        function(data) {{
            if (!data || !data.calendar)
                return ['--', 'calendar-card__source'];
            const cal = data.calendar;
            const src = cal.source;
            let label, cls;
            if (src === 'forex_factory') {{
                label = 'ForexFactory';
                cls = 'calendar-card__source calendar-card__source--primary';
            }} else if (src === 'mt5') {{
                label = 'MT5 fallback';
                cls = 'calendar-card__source calendar-card__source--fallback';
            }} else if (src === 'stale_cache') {{
                label = 'cached';
                cls = 'calendar-card__source calendar-card__source--stale';
            }} else {{
                label = src || '--';
                cls = 'calendar-card__source';
            }}
            if (cal.consecutive_failures && cal.consecutive_failures > 0) {{
                label += ' · ' + cal.consecutive_failures + ' fails';
                cls += ' calendar-card__source--warn';
            }}
            return [label, cls];
        }}
        """,
        [Output("calendar-source", "children"),
         Output("calendar-source", "className")],
        Input("ws-store", "data"),
    )


def _register_countdown(app: Dash) -> None:
    """1 s tick — update every row's clock text and warning class in place.

    Walking the DOM directly avoids re-creating the whole row list every
    second (which would cause Plotly to recompute layout for the heatmap
    sibling). We rely on the ``data-release`` attribute set by the render
    callback.
    """
    warning_sec = int(config.CALENDAR_WARNING_WINDOW_SEC)
    jst_offset_sec = int(config.JST_OFFSET_HOURS) * 3600
    app.clientside_callback(
        f"""
        function(_n) {{
            const root = document.getElementById('calendar-list');
            if (!root) return window.dash_clientside.no_update;
            const rows = root.querySelectorAll('.calendar-row');
            const nowSec = Date.now() / 1000;
            const warn = {warning_sec};
            const jstOffsetSec = {jst_offset_sec};
            rows.forEach(row => {{
                const timeEl = row.querySelector('.calendar-row__time');
                const cdEl = row.querySelector('.calendar-row__countdown');
                if (!cdEl) return;
                const release = parseFloat(cdEl.getAttribute('data-release'));
                if (!isFinite(release)) return;
                // JST clock display (offset from config.JST_OFFSET_HOURS).
                if (timeEl) {{
                    const d = new Date((release + jstOffsetSec) * 1000);
                    const pad = n => String(n).padStart(2, '0');
                    timeEl.textContent = pad(d.getUTCHours()) + ':' +
                                          pad(d.getUTCMinutes());
                }}
                // Countdown / since-release.
                const diff = release - nowSec;
                let txt;
                if (diff >= 0) {{
                    const h = Math.floor(diff / 3600);
                    const m = Math.floor((diff % 3600) / 60);
                    const s = Math.floor(diff % 60);
                    if (h > 0) {{
                        txt = h + 'h ' + String(m).padStart(2,'0') + 'm';
                    }} else if (m > 0) {{
                        txt = m + 'm ' + String(s).padStart(2,'0') + 's';
                    }} else {{
                        txt = s + 's';
                    }}
                }} else {{
                    const s = Math.floor(-diff);
                    if (s < 60) txt = '-' + s + 's';
                    else if (s < 3600) txt = '-' + Math.floor(s/60) + 'm';
                    else txt = '-' + Math.floor(s/3600) + 'h';
                }}
                cdEl.textContent = txt;
                // SPEC §15.3 warning colour: ±30 min around the release.
                let cls = 'calendar-row';
                if (Math.abs(diff) <= warn) cls += ' calendar-row--warning';
                if (diff < 0) cls += ' calendar-row--past';
                row.className = cls;
            }});
            return window.dash_clientside.no_update;
        }}
        """,
        Output("calendar-countdown-sink", "children"),
        Input("calendar-tick", "n_intervals"),
    )


def register_phase4_callbacks(app: Dash) -> None:
    """Wire every Phase 4 clientside callback."""
    _register_calendar_list(app)
    _register_calendar_source(app)
    _register_countdown(app)
