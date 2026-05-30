/* ============================================================
   MT5 Lite Dashboard — vanilla JS, no React/Plotly/Dash.
   ------------------------------------------------------------
   Connects to /ws, parses snapshots, patches the DOM directly.
   Correlation matrix is drawn on a Canvas.
   ============================================================ */
'use strict';

// Row 1: XAU + USD-quote majors. Row 2: JPY-crosses + EUR-crosses.
// 4×2 grid order — mirrors config.SYMBOLS:
//   row 1 (top)    : gold + USD majors  (金 + ＄)
//   row 2 (bottom) : JPY crosses        (円)
const SYMBOL_ORDER = ["XAUUSD", "EURUSD", "GBPUSD", "AUDUSD",
                      "USDJPY", "EURJPY", "GBPJPY", "AUDJPY"];
// Strength is a pure 7-fiat metric; XAU is excluded entirely (gold is not
// a fiat currency). CHF/NZD computed backend-side but not displayed.
const STRENGTH_CCYS = ["USD", "EUR", "GBP", "JPY", "AUD"];
const STRENGTH_WINDOWS = ["H1", "H4", "D1", "W1"];
const CORR_WINDOWS = [20, 100, 500];
// Min samples for an hourly-heatmap cell to be treated as signal (not noise).
const HM_MIN_N = 30;
// #3 regime gate: when the live rolling PF is this far BELOW the 16Y baseline,
// a high-conviction setup is DEMOTED to 様子見 and its high-conviction alert is
// suppressed. Deeper than the -0.20 warn banner so "warn" and "demote" are two
// distinct tiers. Execution (lot/SL) stays the user's discretion — untouched.
const REGIME_GATE_DRIFT = -0.30;

// Per-domain version stamp cache so callbacks can skip no-op renders.
const STAMPS = {};
function changed(key, stamp) {
    if (stamp == null) return false;
    if (STAMPS[key] === stamp) return false;
    STAMPS[key] = stamp;
    return true;
}

// Selected windows (clientside state only).
const UI = {
    strengthWindow: 'H4',
    corrWindow:     100,
    dwsBase:        'H4',   // DWS-SMT base timeframe (x-axis): H4 / H1 / M15
    calYear:        null,   // trigger-calendar selected year (null → newest in data)
    calMonth:       null,   // trigger-calendar selected month 1-12 (JST), or null = whole year
    calView:        'months', // calendar body: 'months' grid or 'years' picker
};

// ------------------------------------------------------------
// Utilities
// ------------------------------------------------------------

function $bind(name) {
    return document.querySelector(`[data-bind="${name}"]`);
}

/** HTML-escape a value before it goes into innerHTML — used for any string
 *  that originates outside our code (e.g. the external Forex Factory feed). */
function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function priceDigits(v, sym) {
    // Prefer broker-supplied digits — they always match the MT5 quote display.
    if (sym && latestSnap && latestSnap.symbol_meta &&
        latestSnap.symbol_meta[sym] && latestSnap.symbol_meta[sym].digits != null) {
        return latestSnap.symbol_meta[sym].digits | 0;
    }
    if (v == null || !isFinite(v)) return 2;
    const abs = Math.abs(v);
    if (abs >= 1000) return 2;
    if (abs >= 50)   return 3;
    return 5;
}

/** Broker-pip multiplier (price-units → pips) for a symbol.
 *  Prefers the broker-supplied ``pip_size`` from MT5 ``symbol_info``
 *  (delivered in the snapshot's ``symbol_meta``). Falls back to convention
 *  inference if the snapshot hasn't arrived yet. */
function pipMultiplierFor(sym) {
    if (!sym) return 10000;
    const meta = latestSnap && latestSnap.symbol_meta && latestSnap.symbol_meta[sym];
    if (meta && meta.pip_size && meta.pip_size > 0) {
        return 1 / meta.pip_size;
    }
    // Fallback (used before first snapshot lands)
    if (sym.endsWith('JPY')) return 100;
    if (sym.startsWith('XAU') || sym.startsWith('XAG')) return 10;
    return 10000;
}

/** Points-per-price-unit = 1 / MT5 _Point, the broker's finest price step.
 *  Taken straight from symbol_meta.point so trade P/L is in true MT5 points
 *  (e.g. XAUUSD _Point 0.001 → 1000). Distinct from pipMultiplierFor, which
 *  uses the coarser pip_size. */
function pointMultiplierFor(sym) {
    const meta = latestSnap && latestSnap.symbol_meta && latestSnap.symbol_meta[sym];
    if (meta && meta.point && meta.point > 0) {
        return 1 / meta.point;
    }
    // Fallback before the first snapshot — broker-typical digit counts.
    if (sym && (sym.startsWith('XAU') || sym.startsWith('XAG'))) return 1000;
    if (sym && sym.endsWith('JPY')) return 1000;
    return 100000;
}
function fmtPrice(v, d) {
    if (v == null || !isFinite(v)) return '--';
    return Number(v).toFixed(d ?? priceDigits(v));
}
function fmtSigned(v, d) {
    if (v == null || !isFinite(v)) return '--';
    const s = Number(v).toFixed(d);
    return v > 0 ? '+' + s : s;
}
function fmtJSTclock(epochSec) {
    if (!epochSec) return '--:--:--';
    const d = new Date(epochSec * 1000 + 9 * 3600 * 1000);
    const p = n => String(n).padStart(2, '0');
    return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
}
function fmtJSTclockNoSec(epochSec) {
    if (!epochSec) return '--:--';
    const d = new Date(epochSec * 1000 + 9 * 3600 * 1000);
    const p = n => String(n).padStart(2, '0');
    return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes());
}
function fmtJSTdate(epochSec) {
    if (!epochSec) return '--';
    const d = new Date(epochSec * 1000 + 9 * 3600 * 1000);
    return (d.getUTCMonth() + 1) + '/' + d.getUTCDate();
}

// ------------------------------------------------------------
// One-time DOM building
// ------------------------------------------------------------

function buildSymbolGrid() {
    const grid = $bind('symbols-grid');
    grid.innerHTML = '';
    for (const sym of SYMBOL_ORDER) {
        const panel = buildPanel(sym);
        panel.addEventListener('click', (ev) => onPanelClick(ev, panel, grid));
        grid.appendChild(panel);
    }
}

function buildPanel(sym) {
    const a = document.createElement('article');
    a.className = 'panel quiet';
    a.id = `panel-${sym}`;
    a.innerHTML = `
        <div class="panel-head">
            <span class="panel-sym">${sym}</span>
            <div class="quote">
                <span><span class="lbl">BID</span><span class="bid" data-bind="bid-${sym}">--</span></span>
                <span><span class="lbl">ASK</span><span class="ask" data-bind="ask-${sym}">--</span></span>
                <span class="spread" data-bind="spread-${sym}"></span>
                <span class="age" data-bind="age-${sym}"></span>
            </div>
            <div class="trade-panel">
                <button class="trade-btn sell" type="button" data-side="SELL"><span class="tb-ar">▼</span><span class="tb-lbl">SELL</span></button>
                <label class="trade-lot-wrap">LOT<input class="trade-lot" type="number" step="0.01" min="0.01" inputmode="decimal"></label>
                <button class="trade-btn buy" type="button" data-side="BUY"><span class="tb-ar">▲</span><span class="tb-lbl">BUY</span></button>
                <span class="trade-note" data-bind="trade-note-${sym}"></span>
            </div>
            <button class="panel-close" type="button" title="閉じる (Esc)">✕</button>
        </div>
        <div class="signals" data-bind="signals-${sym}">
            <div class="composite" data-bind="composite-${sym}">
                <span class="comp-label" title="複合 BIAS スコア (-10=STRONG SELL ⇆ +10=STRONG BUY)">BIAS</span>
                <span class="comp-main">
                    <span class="comp-arrow" data-bind="comp-arrow-${sym}">·</span>
                    <span class="comp-text" data-bind="comp-text-${sym}">--</span>
                </span>
                <div class="comp-gauge">
                    <div class="comp-gauge-track">
                        <div class="comp-gauge-mid"></div>
                        <div class="comp-gauge-fill" data-bind="comp-fill-${sym}"></div>
                    </div>
                </div>
                <span class="comp-score" data-bind="comp-score-${sym}" title="複合スコア = TF別シグナル × TF加重 / 正規化">--</span>
            </div>
            <div class="sig-body" data-bind="sig-body-${sym}"></div>
        </div>
        <div class="analytics" data-bind="analytics-${sym}"></div>
        <div class="dws" data-bind="dws-${sym}"></div>
    `;
    // Wire the trade controls. stopPropagation so clicking buttons/inputs does
    // NOT toggle the panel's expand/collapse (the panel-level click handler).
    const tp = a.querySelector('.trade-panel');
    tp.addEventListener('click', (e) => e.stopPropagation());
    tp.querySelectorAll('.trade-btn').forEach(btn =>
        btn.addEventListener('click', () => onTradeClick(sym, btn.dataset.side, a)));
    return a;
}

/** Toggle the clicked panel between compact and expanded.
 *  Clicking the close button (✕) inside an expanded panel collapses it
 *  without re-triggering expansion. */
function onPanelClick(ev, panel, grid) {
    // Trade controls handle their own clicks (and stopPropagation), but guard
    // here too so a stray bubble never toggles the panel while trading.
    if (ev.target.closest('.trade-panel')) return;
    const closeBtn = ev.target.closest('.panel-close');
    const wasExpanded = panel.classList.contains('expanded');
    if (closeBtn && wasExpanded) {
        ev.stopPropagation();
        panel.classList.remove('expanded');
        grid.classList.remove('has-expanded');
        // Re-render so the sig / dws panels repaint for the new mode.
        if (latestSnap) { delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
        return;
    }
    // Clear any other expansion + toggle this one
    grid.querySelectorAll('.panel.expanded').forEach(p => p.classList.remove('expanded'));
    grid.classList.toggle('has-expanded', !wasExpanded);
    if (!wasExpanded) panel.classList.add('expanded');
    if (latestSnap) { delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
}

// Esc key collapses expanded panel
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    // If the order-confirm modal is open, Esc dismisses THAT, not the panel —
    // and must not trigger the repaint below (which would reset the lot field).
    const ocOv = document.getElementById('order-confirm');
    if (ocOv && !ocOv.hidden) return;
    const grid = document.querySelector('.symbols');
    if (!grid || !grid.classList.contains('has-expanded')) return;
    grid.querySelectorAll('.panel.expanded').forEach(p => p.classList.remove('expanded'));
    grid.classList.remove('has-expanded');
    if (latestSnap) { delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
});

function buildStrengthRows() {
    const root = $bind('strength-rows');
    for (const c of STRENGTH_CCYS) {
        const row = document.createElement('div');
        row.className = 's-row';
        row.innerHTML = `
            <span class="ccy">${c}</span>
            <div class="bar-track">
                <div class="bar-mid"></div>
                <div class="bar" data-bind="bar-${c}" style="width:0;left:50%"></div>
            </div>
            <span class="val" data-bind="sval-${c}">--</span>
        `;
        root.appendChild(row);
    }
    const wins = $bind('strength-windows');
    for (const w of STRENGTH_WINDOWS) {
        const b = document.createElement('button');
        b.className = 'pill' + (w === UI.strengthWindow ? ' on' : '');
        b.dataset.win = w;
        b.textContent = w;
        b.onclick = () => {
            UI.strengthWindow = w;
            wins.querySelectorAll('.pill').forEach(p =>
                p.classList.toggle('on', p.dataset.win === w));
            // Force re-render of strength on next snapshot.
            for (const c of STRENGTH_CCYS) delete STAMPS[`strength:${c}`];
            if (latestSnap) paintStrength(latestSnap.strength, true);
        };
        wins.appendChild(b);
    }
}

function buildCorrelationButtons() {
    const wins = $bind('corr-windows');
    for (const b of CORR_WINDOWS) {
        const btn = document.createElement('button');
        btn.className = 'pill' + (b === UI.corrWindow ? ' on' : '');
        btn.dataset.win = b;
        btn.textContent = b;
        btn.onclick = () => {
            UI.corrWindow = b;
            wins.querySelectorAll('.pill').forEach(p =>
                p.classList.toggle('on', Number(p.dataset.win) === b));
            delete STAMPS['corr-list'];
            if (latestSnap) paintCorrelationList(latestSnap.correlation, true);
        };
        wins.appendChild(btn);
    }
}

// ------------------------------------------------------------
// Render functions (called from snapshot handler)
// ------------------------------------------------------------

let latestSnap = null;
// Cached once per session: the static 16Y OOS baseline arrives only in the
// first full WS snapshot (it never changes), then is re-attached to later fulls.
let OOS_BASELINE = null;

function paintHeader(snap) {
    $bind('clock').textContent = fmtJSTclock(snap.ts);
    $bind('version').textContent = String(snap.version);
    $bind('compute').textContent =
        (snap.analysis && isFinite(snap.analysis.compute_ms))
            ? Number(snap.analysis.compute_ms).toFixed(1) + ' ms' : '-- ms';
    const status = snap.status || {};
    const dot = $bind('conn-dot');
    dot.className = 'dot ' + (status.connected ? 'on' : 'off');
    $bind('conn-text').textContent = status.connected ? 'connected' : 'disconnected';
    paintActiveSetups(snap);
}

/** Rank symbols by composite score and surface high-conviction (|score|≥5) ones. */
function paintActiveSetups(snap) {
    const root = $bind('active-chips');
    if (!root) return;
    const analysis = snap.analysis;
    if (!analysis || !analysis.by_symbol) {
        root.innerHTML = '<span class="active-empty">waiting for data...</span>';
        return;
    }
    // Active-setups are 100% analysis-derived; skip the 8x compositeSignal +
    // innerHTML rebuild on the 2 Hz light ticks where analysis is unchanged.
    if (!changed('active', analysis.generated_at)) return;
    const scored = [];
    for (const sym of SYMBOL_ORDER) {
        const sa = analysis.by_symbol[sym];
        if (!sa || !sa.by_tf) continue;
        const c = compositeSignal(sa.by_tf);
        if (c.cls === 'na' || c.cls === 'neutral') continue;
        if (Math.abs(c.score) < 5) continue;  // only high-conviction
        // #3 regime gate: a high-conviction setup whose live rolling PF has
        // fallen ≥30% below its 16Y baseline is DEMOTED to 様子見 (still shown,
        // de-emphasised, sorted last) and excluded from alerts.
        const drift = _regimeDrift(sym, snap);
        const degraded = drift != null && drift <= REGIME_GATE_DRIFT;
        scored.push({ sym, c, degraded, drift });
    }
    // Non-degraded high-conviction first; 様子見 sink to the end. Strongest
    // |score| first within each group.
    scored.sort((a, b) => (a.degraded - b.degraded)
        || (Math.abs(b.c.score) - Math.abs(a.c.score)));
    fireSetupAlerts(scored);                  // notify the moment a NEW setup appears
    if (scored.length === 0) {
        root.innerHTML = '<span class="active-empty">no high-conviction signals</span>';
        return;
    }
    root.innerHTML = scored.slice(0, 8).map(({ sym, c, degraded, drift }) => {
        const scoreStr = (c.score > 0 ? '+' : '') + c.score.toFixed(1);
        if (degraded) {
            const pct = Math.round(drift * 100);
            return `<span class="active-chip degraded"
                title="直近地合い悪化: 16Y比 ${pct}% → 降格(様子見)。執行・ロット・SL は裁量。">
                <span class="ac-sym">${sym}</span>
                <span class="ac-side">${c.arrow} 様子見</span>
                <span class="ac-score">${scoreStr}</span>
            </span>`;
        }
        return `<span class="active-chip ${c.cls}">
            <span class="ac-sym">${sym}</span>
            <span class="ac-side">${c.arrow} ${c.label}</span>
            <span class="ac-score">${scoreStr}</span>
        </span>`;
    }).join('');
}

// Per-currency flags as inline SVG — Windows has no emoji-flag glyphs (it shows
// the 2-letter code), so we draw simplified-but-recognisable flags that render
// identically everywhere and offline. XAU = a gold coin (no nation). A little
// personality on each pair: [base flag] SYMBOL [quote flag].
const CCY_FLAGS = {
    USD: '<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#b22234"/><g fill="#fff"><rect y="1.23" width="24" height="1.23"/><rect y="3.69" width="24" height="1.23"/><rect y="6.15" width="24" height="1.23"/><rect y="8.62" width="24" height="1.23"/><rect y="11.08" width="24" height="1.23"/><rect y="13.54" width="24" height="1.23"/></g><rect width="10" height="8.62" fill="#3c3b6e"/><g fill="#fff"><circle cx="2" cy="1.7" r=".55"/><circle cx="5" cy="1.7" r=".55"/><circle cx="8" cy="1.7" r=".55"/><circle cx="3.5" cy="3.4" r=".55"/><circle cx="6.5" cy="3.4" r=".55"/><circle cx="2" cy="5.1" r=".55"/><circle cx="5" cy="5.1" r=".55"/><circle cx="8" cy="5.1" r=".55"/><circle cx="3.5" cy="6.8" r=".55"/><circle cx="6.5" cy="6.8" r=".55"/></g></svg>',
    EUR: '<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#039"/><g fill="#fc0"><circle cx="12" cy="3" r=".82"/><circle cx="14.5" cy="3.67" r=".82"/><circle cx="16.33" cy="5.5" r=".82"/><circle cx="17" cy="8" r=".82"/><circle cx="16.33" cy="10.5" r=".82"/><circle cx="14.5" cy="12.33" r=".82"/><circle cx="12" cy="13" r=".82"/><circle cx="9.5" cy="12.33" r=".82"/><circle cx="7.67" cy="10.5" r=".82"/><circle cx="7" cy="8" r=".82"/><circle cx="7.67" cy="5.5" r=".82"/><circle cx="9.5" cy="3.67" r=".82"/></g></svg>',
    GBP: '<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#012169"/><path d="M0,0 L24,16 M24,0 L0,16" stroke="#fff" stroke-width="3.2"/><path d="M0,0 L24,16 M24,0 L0,16" stroke="#c8102e" stroke-width="1.3"/><rect x="9.6" width="4.8" height="16" fill="#fff"/><rect y="5.6" width="24" height="4.8" fill="#fff"/><rect x="10.8" width="2.4" height="16" fill="#c8102e"/><rect y="6.8" width="24" height="2.4" fill="#c8102e"/></svg>',
    AUD: '<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#012169"/><g><rect width="11" height="8" fill="#012169"/><path d="M0,0 L11,8 M11,0 L0,8" stroke="#fff" stroke-width="1.6"/><path d="M0,0 L11,8 M11,0 L0,8" stroke="#c8102e" stroke-width=".7"/><rect x="4.4" width="2.2" height="8" fill="#fff"/><rect y="2.9" width="11" height="2.2" fill="#fff"/><rect x="4.95" width="1.1" height="8" fill="#c8102e"/><rect y="3.45" width="11" height="1.1" fill="#c8102e"/></g><g fill="#fff"><circle cx="5.5" cy="12.7" r="1.1"/><circle cx="18.5" cy="3.8" r=".62"/><circle cx="20.5" cy="6.5" r=".62"/><circle cx="17.5" cy="8.8" r=".62"/><circle cx="19.6" cy="11" r=".62"/><circle cx="18.8" cy="7.3" r=".4"/></g></svg>',
    JPY: '<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="4.4" fill="#bc002d"/></svg>',
    XAU: '<svg viewBox="0 0 24 16"><rect width="24" height="16" rx="2" fill="#2b2b2b"/><circle cx="12" cy="8" r="6.2" fill="#f3c344"/><circle cx="12" cy="8" r="6.2" fill="none" stroke="#b8860b" stroke-width="1"/><circle cx="9.8" cy="5.9" r="1.5" fill="#fce08a"/></svg>',
};
/** Inline flag for a 3-letter currency code (empty string if unknown). */
function flagSvg(ccy) {
    const s = CCY_FLAGS[ccy];
    return s ? `<span class="ccy-flag" title="${ccy}">${s}</span>` : '';
}

/** Collapsible summary bar (#5): one cell per symbol with the order-relevant
 *  read — BIAS composite, 4-TF EMA alignment, and the 様子見 regime flag — so a
 *  glance at the top replaces scanning 8 dense panels. It complements the
 *  header's ACTIVE SETUPS (high-conviction only) by covering ALL symbols.
 *  Repaints only when the analysis advances; a cell click focuses that panel. */
function paintSummaryBar(snap) {
    const root = $bind('summary-cells');
    if (!root) return;
    const analysis = snap.analysis;
    if (!analysis || !analysis.by_symbol) {
        if (!root.innerHTML) root.innerHTML = '<div class="sb-empty">分析待ち…</div>';
        return;
    }
    if (!changed('summary', analysis.generated_at)) return;
    root.innerHTML = SYMBOL_ORDER.map(sym => {
        // [base flag] SYMBOL [quote flag] — all 6-char (base=0..3, quote=3..6).
        const symHtml = `${flagSvg(sym.slice(0, 3))}`
            + `<span class="sb-sym-txt">${sym}</span>${flagSvg(sym.slice(3))}`;
        const sa = analysis.by_symbol[sym];
        if (!sa || !sa.by_tf) {
            return `<div class="sb-cell na" data-sb-sym="${sym}">
                <div class="sb-sym">${symHtml}</div>
                <div class="sb-bias na">--</div>
                <div class="sb-tfs sb-nodata">データ無</div>
            </div>`;
        }
        const c = compositeSignal(sa.by_tf);
        const drift = _regimeDrift(sym, snap);
        const degraded = drift != null && drift <= REGIME_GATE_DRIFT;
        const scoreStr = c.cls === 'na' ? '--'
            : (c.score > 0 ? '+' : '') + c.score.toFixed(1);
        // 4-TF EMA side as colour chips (D1/H4/H1/M15): green=above, red=below.
        const tfHtml = TF_LABELS.map(lab => {
            const tf = sa.by_tf[lab];
            if (!tf || tf.last_close == null || tf.ema == null) {
                return `<span class="sb-tf">${lab}</span>`;
            }
            const up = tf.last_close >= tf.ema;
            return `<span class="sb-tf ${up ? 'up' : 'dn'}">${lab}</span>`;
        }).join('');
        // BIAS stays direction-coloured (the signal); the amber cell frame +
        // 様子見 pill carry the regime caution when gated.
        const flag = degraded
            ? `<div class="sb-flag" title="直近地合い悪化: 16Y比 ${Math.round(drift * 100)}% → 様子見 (執行は裁量)">⚠ 様子見</div>`
            : '';
        const cellCls = degraded ? 'degraded' : c.cls;
        // BIAS number coloured by SIGN: + green, − red, 0 grey.
        const signCls = c.score > 0 ? 'pos' : c.score < 0 ? 'neg' : 'zero';
        return `<div class="sb-cell ${cellCls}" data-sb-sym="${sym}" title="${sym} · BIAS ${scoreStr} (${c.label})">
            <div class="sb-sym">${symHtml}</div>
            <div class="sb-bias ${signCls}"><span class="sb-arrow">${c.arrow}</span>${scoreStr}</div>
            <div class="sb-tfs">${tfHtml}</div>
            ${flag}
        </div>`;
    }).join('');
}

/** Focus a symbol's panel (expand it) — used by the summary-bar cells. */
function focusPanel(sym) {
    const grid = document.querySelector('.symbols');
    const panel = document.getElementById('panel-' + sym);
    if (!grid || !panel) return;
    grid.querySelectorAll('.panel.expanded').forEach(p => p.classList.remove('expanded'));
    panel.classList.add('expanded');
    grid.classList.add('has-expanded');
    if (latestSnap) { delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
}

// Summary-bar cell → focus that symbol's panel. Cells live in the <details>
// BODY (not <summary>), so this never toggles the bar's open/closed state.
document.addEventListener('click', (ev) => {
    const cell = ev.target.closest('.sb-cell');
    if (!cell || !cell.dataset.sbSym) return;
    focusPanel(cell.dataset.sbSym);
});

/* ============================================================
   High-conviction setup ALERTS. Browser-notify the moment a NEW
   high-conviction setup (|score|≥5) appears so a discretionary
   trader never misses the entry — NOTIFY ONLY; lot/SL/execution
   stay the user's call. The bell toggle persists in localStorage;
   firing needs the browser Notification permission. Keyed by
   sym|cls so a re-fire only happens on a genuinely new setup or a
   direction flip, never on every repaint.
   ============================================================ */
const ALERT = { enabled: false, prev: new Set(), seeded: false, ctx: null };
try { ALERT.enabled = localStorage.getItem('mt5-alert') === '1'; } catch (e) { /* private mode */ }

function alertBeep() {
    try {
        ALERT.ctx = ALERT.ctx || new (window.AudioContext || window.webkitAudioContext)();
        const ctx = ALERT.ctx, t = ctx.currentTime;
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = 'sine'; o.frequency.setValueAtTime(880, t);
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.18, t + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.25);
        o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 0.26);
    } catch (e) { /* audio blocked until a user gesture — fine */ }
}

function paintAlertBell() {
    const b = $bind('alert-toggle');
    if (!b) return;
    const supported = 'Notification' in window;
    const granted = supported && Notification.permission === 'granted';
    const on = ALERT.enabled && granted;
    b.classList.toggle('on', on);
    b.classList.toggle('pending', ALERT.enabled && supported && !granted);
    b.textContent = on ? '🔔' : '🔕';
    b.title = !supported ? 'この環境はブラウザ通知に非対応'
        : Notification.permission === 'denied' ? '高確信アラート: ブラウザがブロック中（サイト設定で許可）'
        : on ? '高確信アラート: ON（クリックでOFF）'
        : ALERT.enabled ? '高確信アラート: 通知の許可待ち（クリックで再要求）'
        : '高確信アラート: OFF（クリックでON）';
}

/** Detect newly-appeared high-conviction setups and notify (deduped by sym|cls).
 *  Regime-degraded (様子見) setups are excluded — they were demoted, so they must
 *  not fire an alert. A setup that later recovers re-enters `active` and alerts
 *  again, which is the desired "regime healed" signal. */
function fireSetupAlerts(scored) {
    const active = scored.filter(s => !s.degraded);
    const cur = new Set(active.map(s => s.sym + '|' + s.c.cls));
    // First pass after load (or after enabling) seeds the baseline WITHOUT firing,
    // so the existing setups don't all alert at once on open.
    if (ALERT.enabled && ALERT.seeded
        && 'Notification' in window && Notification.permission === 'granted') {
        const fresh = active.filter(s => !ALERT.prev.has(s.sym + '|' + s.c.cls));
        if (fresh.length) {
            const top = fresh.slice(0, 3);
            const body = top.map(s =>
                `${s.sym} ${s.c.arrow} ${s.c.label} (${s.c.score > 0 ? '+' : ''}${s.c.score.toFixed(1)})`
            ).join('\n') + (fresh.length > 3 ? `\n他 ${fresh.length - 3} 件` : '');
            try {
                const n = new Notification('🎯 高確信シグナル', {
                    body, tag: 'mt5-setup', renotify: true,
                });
                n.onclick = () => { window.focus(); n.close(); };
                setTimeout(() => n.close(), 12000);
            } catch (e) { /* notification failed — ignore */ }
            alertBeep();
        }
    }
    ALERT.prev = cur;
    ALERT.seeded = true;
}

// Bell toggle — request permission on enable, persist intent, reflect state.
document.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-bind="alert-toggle"]');
    if (!b) return;
    ev.stopPropagation();
    if (!('Notification' in window)) { paintAlertBell(); return; }
    if (!ALERT.enabled) {
        ALERT.enabled = true;
        ALERT.seeded = false;                 // re-seed so the current set doesn't all fire
        try { localStorage.setItem('mt5-alert', '1'); } catch (e) {}
        if (Notification.permission === 'default') {
            Notification.requestPermission().then(paintAlertBell);
        }
        alertBeep();                          // a short confirm beep (also unlocks audio)
    } else {
        ALERT.enabled = false;
        try { localStorage.setItem('mt5-alert', '0'); } catch (e) {}
    }
    paintAlertBell();
}, true);

function paintPrices(snap) {
    const ticks = (snap.price && snap.price.ticks) || {};
    const ts = snap.ts;
    for (const sym of SYMBOL_ORDER) {
        const t = ticks[sym];
        if (!t) continue;
        const d = priceDigits(t.bid, sym);
        $bind('bid-' + sym).textContent = fmtPrice(t.bid, d);
        $bind('ask-' + sym).textContent = fmtPrice(t.ask, d);

        // Spread — always show (cost transparency for trader).
        // Uses real broker pip (1pip JPY/XAU = 0.01, others = 0.0001), not pipette.
        const spreadEl = $bind('spread-' + sym);
        if (isFinite(t.ask) && isFinite(t.bid) && t.ask >= t.bid) {
            const sp = (t.ask - t.bid) * pipMultiplierFor(sym);
            spreadEl.textContent = sp.toFixed(1) + 'sp';
        } else {
            spreadEl.textContent = '0.0sp';
        }

        // Age
        const ageEl = $bind('age-' + sym);
        if (t.time_msc && ts) {
            const a = ts - t.time_msc / 1000;
            ageEl.textContent = (a < 60) ? a.toFixed(1) + 's'
                              : (a < 3600) ? Math.floor(a / 60) + 'm'
                                           : Math.floor(a / 3600) + 'h';
        } else ageEl.textContent = '';
    }
}

function paintAccount(snap) {
    const a = snap.account;
    if (!a) return;
    $bind('acc-identity').textContent = `${a.login} / ${a.server}`;
    $bind('acc-balance').textContent = fmtPrice(a.balance, 2);
    $bind('acc-equity').textContent  = fmtPrice(a.equity, 2);
    const profitEl = $bind('acc-profit');
    profitEl.textContent = fmtSigned(a.profit, 2);
    profitEl.className   = 'acc-val mono ' + (a.profit > 0 ? 'pos' : a.profit < 0 ? 'neg' : '');
    $bind('acc-margin').textContent  = fmtPrice(a.margin, 2);
    $bind('acc-free').textContent    = fmtPrice(a.margin_free, 2);
    $bind('acc-lev').textContent     = a.leverage ? '1:' + a.leverage : '--';

    // Recommended lot (validated fixed-fractional ladder) — server-computed.
    const recoEl = $bind('acc-reco');
    if (recoEl) {
        if (a.recommended_lot != null && isFinite(a.recommended_lot)) {
            recoEl.textContent = Number(a.recommended_lot).toFixed(2);
            const r = a.lot_rule || {};
            const stepMan = r.step ? Math.round(r.step / 10000) : null;
            $bind('acc-reco-sub').textContent = stepMan
                ? `0.01 / ${stepMan}万円 ・ 上限${r.max ?? '--'}` : '';
        } else {
            recoEl.textContent = '--';
            $bind('acc-reco-sub').textContent = '';
        }
    }
    $bind('acc-level').textContent   = (a.margin_level != null) ? a.margin_level.toFixed(1) + ' %' : '--';

    // Today P&L (from performance snapshot if available)
    const todayEl = $bind('acc-today');
    if (snap.performance && snap.performance.today_total_pnl != null) {
        const t = snap.performance.today_total_pnl;
        todayEl.textContent = fmtSigned(t, 2);
        todayEl.className   = 'acc-val big mono ' + (t > 0 ? 'pos' : t < 0 ? 'neg' : '');
    } else {
        todayEl.textContent = '--';
    }

    // Positions
    const posRoot = $bind('positions');
    const positions = a.positions || [];
    // Rebuild the DOM (and the ✕ buttons) ONLY when the set of tickets or the
    // trade permission changes. Account updates arrive at ~2 Hz; rebuilding
    // innerHTML every tick destroyed the close buttons mid-click, so individual
    // 決済 never registered. Floating price/PnL are updated in place below.
    // Include type + volume so a partial close (volume change) or a same-ticket
    // flip rebuilds the row — otherwise the static parts (lot, side) go stale.
    const sig = positions.map(p => `${p.ticket}:${p.type}:${p.volume}`).join(',')
        + '|' + (a.trade_allowed ? 1 : 0);
    if (sig !== posRoot._sig) {
        posRoot._sig = sig;
        if (!positions.length) {
            posRoot.innerHTML = '<div class="empty">no open positions</div>';
        } else {
            const rows = positions.map(p => {
                const d = priceDigits(p.price_open, p.symbol);
                const cls = p.type === 'BUY' ? 'buy' : 'sell';
                const closeBtn = a.trade_allowed
                    ? `<button class="pos-close" type="button" data-ticket="${p.ticket}" title="この建玉を成行決済">✕</button>` : '';
                return `<div class="pos-row ${cls}" data-ticket="${p.ticket}">
                    <div class="pos-line1">
                        <span class="type-${cls}">${esc(p.type)}</span>
                        <span class="pos-sym">${esc(p.symbol)}</span>
                        <span class="pos-vol">${(p.volume != null ? p.volume : 0).toFixed(2)}L</span>
                        ${closeBtn}
                    </div>
                    <div class="pos-line2">
                        <span class="pos-px"></span>
                        <span class="pos-pnl"></span>
                    </div>
                </div>`;
            }).join('');
            const allBtn = a.trade_allowed
                ? `<button class="pos-close-all" type="button">全決済 (${positions.length})</button>` : '';
            posRoot.innerHTML = allBtn + rows;
        }
    }
    // Live in-place refresh of price + floating PnL for the existing rows.
    for (const p of positions) {
        const row = posRoot.querySelector(`.pos-row[data-ticket="${p.ticket}"]`);
        if (!row) continue;
        const d = priceDigits(p.price_open, p.symbol);
        const px = row.querySelector('.pos-px');
        if (px) px.textContent = `${fmtPrice(p.price_open, d)}→${fmtPrice(p.price_current, d)}`;
        const pnl = row.querySelector('.pos-pnl');
        if (pnl) {
            pnl.textContent = fmtSigned(p.profit, 2);
            pnl.className = 'pos-pnl ' + (p.profit > 0 ? 'pos' : 'neg');
        }
    }
    applyTradeGating(a);
    wireCloseButtons();
}

// ------------------------------------------------------------
// Discretionary order panel — confirm-then-send (no auto trading)
// ------------------------------------------------------------

/** Enable/disable trade buttons by the account's trade permission and prefill
 *  the lot field with the recommended lot (unless the user is editing it). */
function applyTradeGating(acc) {
    const ok = !!(acc && acc.trade_allowed);
    // Gate: only touch the DOM when permission or recommended lot changes
    // (paintAccount runs at ~2 Hz; nothing here changes per tick otherwise).
    const reco = (acc && acc.recommended_lot != null) ? Number(acc.recommended_lot).toFixed(2) : '';
    if (!changed('tradegate', (ok ? 1 : 0) + '|' + reco)) return;
    document.querySelectorAll('.trade-panel').forEach(tp => {
        tp.classList.toggle('disabled', !ok);
        tp.querySelectorAll('.trade-btn').forEach(b => { b.disabled = !ok; });
        const note = tp.querySelector('.trade-note');
        if (note) {
            if (!ok) note.textContent = '取引不可：口座/端末の Algo Trading 許可を確認';
            else if (note.textContent.startsWith('取引不可')) note.textContent = '';
        }
        const lotEl = tp.querySelector('.trade-lot');
        if (lotEl && document.activeElement !== lotEl
            && acc && acc.recommended_lot != null) {
            lotEl.value = Number(acc.recommended_lot).toFixed(2);
        }
    });
}

function onTradeClick(sym, side, panelEl) {
    if (!latestSnap) return;
    const acc = latestSnap.account || {};
    const note = $bind('trade-note-' + sym);
    if (!acc.trade_allowed) {
        if (note) note.textContent = '取引不可：口座/端末の Algo Trading 許可を確認';
        return;
    }
    const lot = parseFloat(panelEl.querySelector('.trade-lot').value);
    if (!(lot > 0)) { if (note) note.textContent = 'ロットを入力してください'; return; }
    const tk = (latestSnap.price && latestSnap.price.ticks && latestSnap.price.ticks[sym]) || {};
    if (note) note.textContent = '';
    openOrderConfirm({ symbol: sym, side, lot, bid: tk.bid, ask: tk.ask });
}

function _ocEls() {
    return {
        ov: document.getElementById('order-confirm'),
        title: $bind('oc-title'), detail: $bind('oc-detail'),
        result: $bind('oc-result'),
        go: document.querySelector('#order-confirm .oc-go'),
        cancel: document.querySelector('#order-confirm .oc-cancel'),
    };
}

function openOrderConfirm(o) {
    const e = _ocEls();
    const dig = priceDigits(o.ask || o.bid || 0, o.symbol);
    const px = o.side === 'BUY' ? o.ask : o.bid;
    e.title.textContent = `${o.side} ${o.symbol}`;
    e.title.className = 'oc-title ' + (o.side === 'BUY' ? 'buy' : 'sell');
    e.detail.innerHTML =
        `<div>方向 <b class="${o.side === 'BUY' ? 'pos' : 'neg'}">${esc(o.side)}</b> ・ ロット <b>${o.lot.toFixed(2)}</b></div>`
      + `<div>成行 約定目安 <b>${fmtPrice(px, dig)}</b> <span class="mute">(BID ${fmtPrice(o.bid, dig)} / ASK ${fmtPrice(o.ask, dig)})</span></div>`;
    e.result.textContent = ''; e.result.className = 'oc-result';
    e.go.disabled = false; e.go.textContent = '確定して発注';
    e.go.onclick = () => submitOrder(o, e.go);
    e.cancel.onclick = () => { e.ov.hidden = true; };
    e.ov.hidden = false;
}

function submitOrder(o, goBtn) {
    goBtn.disabled = true; goBtn.textContent = '送信中…';
    fetch('/api/order', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: o.symbol, side: o.side, lots: o.lot, sl: o.sl, tp: o.tp }),
    }).then(r => r.json()).then(j => {
        const el = $bind('oc-result');
        if (j.ok) {
            el.className = 'oc-result ok';
            el.textContent = `約定 #${j.order} @ ${j.price} (${j.volume} lot)`;
            goBtn.textContent = '完了';
            fetchJournal();   // pull the freshly-logged entry (with its 3TF context)
            setTimeout(() => { document.getElementById('order-confirm').hidden = true; }, 1500);
        } else {
            el.className = 'oc-result err';
            el.textContent = 'エラー: ' + (j.error || ('retcode ' + j.retcode + ' ' + (j.comment || '')));
            goBtn.disabled = false; goBtn.textContent = '再送信';
        }
    }).catch(err => {
        const el = $bind('oc-result');
        el.className = 'oc-result err'; el.textContent = '通信エラー: ' + err;
        goBtn.disabled = false; goBtn.textContent = '再送信';
    });
}

function confirmClose(target) {
    const e = _ocEls();
    e.title.textContent = target.all ? '全決済の確認' : '決済の確認';
    e.title.className = 'oc-title warn';
    e.detail.innerHTML = target.all
        ? '<div>保有中の<b>全建玉</b>を成行で決済します。</div>'
        : `<div>建玉 <b>#${target.ticket}</b> を成行で決済します。</div>`;
    e.result.textContent = ''; e.result.className = 'oc-result';
    e.go.disabled = false; e.go.textContent = '確定して決済';
    e.go.onclick = () => submitClose(target, e.go);
    e.cancel.onclick = () => { e.ov.hidden = true; };
    e.ov.hidden = false;
}

function submitClose(target, goBtn) {
    goBtn.disabled = true; goBtn.textContent = '送信中…';
    fetch('/api/close', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(target),
    }).then(r => r.json()).then(j => {
        const el = $bind('oc-result');
        if (j.ok) {
            el.className = 'oc-result ok';
            el.textContent = target.all ? `全決済 ${j.closed}/${j.n} 完了` : '決済完了';
            goBtn.textContent = '完了';
            setTimeout(() => { document.getElementById('order-confirm').hidden = true; }, 1300);
        } else {
            el.className = 'oc-result err';
            el.textContent = 'エラー: ' + (j.error || ('retcode ' + j.retcode));
            goBtn.disabled = false; goBtn.textContent = '再試行';
        }
    }).catch(err => {
        const el = $bind('oc-result');
        el.className = 'oc-result err'; el.textContent = '通信エラー: ' + err;
        goBtn.disabled = false; goBtn.textContent = '再試行';
    });
}

/** Delegated close-button handler on the positions list (rendered each paint). */
function wireCloseButtons() {
    const root = $bind('positions');
    if (!root || root._wiredClose) return;
    root._wiredClose = true;
    root.addEventListener('click', (e) => {
        if (e.target.closest('.pos-close-all')) { confirmClose({ all: true }); return; }
        const one = e.target.closest('.pos-close');
        if (one) confirmClose({ ticket: Number(one.dataset.ticket) });
    });
}

/** One-time: dismiss the order modal on backdrop click or Esc. */
function setupOrderModal() {
    const ov = document.getElementById('order-confirm');
    if (!ov || ov._wired) return;
    ov._wired = true;
    ov.addEventListener('click', (e) => { if (e.target === ov) ov.hidden = true; });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !ov.hidden) ov.hidden = true;
    });
}

function paintStrength(strength, force) {
    if (!strength) return;
    if (!force && !changed('strength:meter:' + UI.strengthWindow,
                            strength.generated_at + ':' + UI.strengthWindow)) return;
    const w = (strength.by_window || {})[UI.strengthWindow];
    if (!w) return;
    for (const c of STRENGTH_CCYS) {
        const sc = (w.scores || {})[c];
        const bar = $bind('bar-' + c);
        const val = $bind('sval-' + c);
        if (!bar || !val) continue;
        if (!sc || sc.score == null) {
            bar.style.width = '0%'; bar.style.left = '50%';
            val.textContent = '--';
            continue;
        }
        const score = sc.score;
        const offset = Math.abs(score - 50) * 2;
        const width = Math.min(offset, 100);
        let left, mod;
        if (score >= 50) {
            left = '50%';
            mod = 'pos';
        } else {
            left = (50 - width) + '%';
            mod = 'neg';
        }
        bar.style.width = width + '%';
        bar.style.left = left;
        bar.className = 'bar ' + mod + (sc.is_reference ? ' ref' : '');
        val.textContent = score.toFixed(1);
    }
}

// Currencies that have at least one panel in the display SYMBOLS. DERIVED
// from SYMBOL_ORDER so editing the symbol list automatically keeps the
// calendar filter in sync — no hand-maintained allow-list to fall out of
// step with the panels. Mirrors backend config._calendar_currencies_from_symbols.
const CALENDAR_DISPLAY_CCY = (() => {
    const out = new Set();
    for (const s of SYMBOL_ORDER) {
        if (s.startsWith('XAU')) { out.add(s.slice(3)); continue; }
        if (s.length === 6) { out.add(s.slice(0, 3)); out.add(s.slice(3)); }
    }
    return out;
})();

function paintCalendar(snap) {
    const cal = snap.calendar;
    if (!cal) return;
    const root = $bind('calendar');
    const srcEl = $bind('cal-source');
    srcEl.textContent = cal.source + (cal.consecutive_failures
        ? ` · ${cal.consecutive_failures} fails` : '');
    if (!changed('cal', cal.generated_at)) return;
    const nowSec = Date.now() / 1000;
    const cutoff = nowSec - (cal.warning_window_sec || 1800);
    const events = (cal.events || [])
        .filter(e => e.release_ts >= cutoff)
        .filter(e => CALENDAR_DISPLAY_CCY.has(String(e.currency || '').toUpperCase()))
        .slice(0, cal.display_count || 12);
    if (events.length === 0) {
        root.innerHTML = '<div class="empty mute">no high-impact events</div>';
        return;
    }
    // Each row: date · time · ccy badge · category badge · title (JST). No countdown.
    // The currency tag drives a left-border colour and a coloured chip so
    // the reader can spot e.g. "USD event tomorrow" in one glance, and the
    // category chip distinguishes rate decisions from labour data.
    // "today/tomorrow" must be keyed in JST (UTC+9), like every other date here,
    // not the browser's local timezone — otherwise a non-JST client mis-highlights.
    const JST_MS = 9 * 3600 * 1000;
    const todayKey = new Date(Date.now() + JST_MS).toISOString().slice(0, 10);
    const tomorrowKey = new Date(Date.now() + JST_MS + 86400000).toISOString().slice(0, 10);
    root.innerHTML = events.map(e => {
        const cat = e.category ? catChip(e.category) : calendarCategory(e.title);
        const dateJst = new Date(e.release_ts * 1000 + JST_MS).toISOString().slice(0, 10);
        const day = dateJst === todayKey ? ' today' : dateJst === tomorrowKey ? ' tomorrow' : '';
        return `<div class="cal-row${day}" data-ccy="${esc(e.currency)}">
            <span class="cal-date">${esc(fmtJSTdate(e.release_ts))}</span>
            <span class="time">${esc(fmtJSTclockNoSec(e.release_ts))}</span>
            <span class="ccy">${esc(e.currency)}</span>
            <span class="cat ${cat.cls}">${cat.label}</span>
            <span class="title" title="${esc(e.title)}">${esc(e.title)}</span>
        </div>`;
    }).join('');
}

/** Map an FF calendar event title to a category chip (rate decision vs
 *  labour data) so the reader gets instant differentiation. Keywords are
 *  kept in sync with backend config.CALENDAR_EVENT_KEYWORDS — every event
 *  the backend lets through belongs to one of these two categories, so
 *  the fallback "指標" is a defensive net rather than a real outcome. */
/** Map a backend category key ('emp' | 'rate' | 'oth') to its chip. Preferred
 *  over keyword-matching the title, which is now localised to Japanese. */
function catChip(c) {
    if (c === 'emp')  return {cls: 'emp',  label: '雇用'};
    if (c === 'rate') return {cls: 'rate', label: '金利'};
    return {cls: 'oth', label: '指標'};
}
function calendarCategory(title) {
    const t = (title || '').toLowerCase();
    if (/payroll|nonfarm|non-farm|employment|unemploy|jobless|hourly earnings|earnings index|claimant count|jolts|adp/.test(t)) {
        return {cls: 'emp', label: '雇用'};
    }
    if (/fomc|federal funds rate|bank rate|cash rate|policy rate|refinanc|rate statement|rate decision|monetary policy|interest rate|press conference/.test(t)) {
        return {cls: 'rate', label: '金利'};
    }
    return {cls: 'oth', label: '指標'};
}

/** Paint the macro / rate-differential reference panel.
 *  One row per pair: base rate, quote rate, differential, macro direction. */
function paintMacro(snap) {
    const m = snap.macro;
    const ry = snap.real_yield;
    const root = $bind('macro');
    if (!root) return;
    const statusEl = $bind('macro-status');
    // --- Heartbeat status — ALWAYS updates, regardless of data freshness.
    // This is what kills the user-perceived "frozen panel" — the rate data
    // refreshes hourly but the "Xm前" tag now visibly ticks every WS pass.
    if (statusEl) {
        if (!m) {
            statusEl.textContent = '--';
            statusEl.className = 'mute';
        } else if (m.last_error) {
            statusEl.textContent = '一部ソース障害';
            statusEl.className = 'neg';
        } else if (m.fetched_at) {
            const ageMin = Math.max(0, Math.round((Date.now() / 1000 - m.fetched_at) / 60));
            statusEl.textContent = ageMin < 1 ? '更新直後'
                                 : ageMin < 60 ? `${ageMin}分前`
                                 : `${Math.floor(ageMin / 60)}時間前`;
            statusEl.className = 'mute';
        } else {
            statusEl.textContent = '--';
            statusEl.className = 'mute';
        }
    }
    // --- Expensive DOM rebuild — gated by stamp so we don't re-render the
    // grid every 0.5 s tick. Only repaints when generated_at actually advances.
    const stamp = (m && m.generated_at || 0) + ':' + (ry && ry.generated_at || 0);
    if (!changed('macro', stamp)) return;
    if (!m || !m.rates || Object.keys(m.rates).length === 0) {
        root.innerHTML = '<div class="empty mute">マクロデータ未取得</div>';
        return;
    }
    const rateStr = ccy => {
        const r = m.rates[ccy];
        return r && r.rate != null ? (r.rate.toFixed(2) + (r.stale ? '*' : '')) : '--';
    };
    const arrow = d => d > 0 ? '▲' : d < 0 ? '▼' : '·';
    // Shorter direction labels — full text remains accessible via title="" so
    // hovering still shows the verbose label even on a narrow sidebar.
    const goldLabel = gd => gd < 0 ? { short: '実質利回り↑/金逆風',  full: '実質利回り上昇 / 金は逆風'  }
                          : gd > 0 ? { short: '実質利回り↓/金追風',  full: '実質利回り低下 / 金は追い風' }
                                   : { short: '実質利回り横ばい',    full: '実質利回り横ばい'           };
    const rows = (SYMBOL_ORDER || []).map(sym => {
        if (sym === 'XAUUSD') {
            // Gold's macro direction is the US 10-year real-yield trend
            // (gold moves inverse to real yields). Row contents: pair tag |
            // current real-yield % | 5-day trend (as differential) | label.
            const gd = ry && ry.value != null ? ry.gold_dir : 0;
            const cls = gd > 0 ? 'pos' : gd < 0 ? 'neg' : 'mute';
            const rv = ry && ry.value != null ? ry.value.toFixed(2) + '%' : '--';
            const t5 = ry && ry.trend_5d != null
                     ? (ry.trend_5d >= 0 ? '+' : '') + ry.trend_5d.toFixed(2) : '--';
            const lbl = goldLabel(gd);
            return `<div class="macro-row">
                <span class="macro-pair">XAUUSD</span>
                <span class="macro-rates">実利 ${esc(rv)}</span>
                <span class="macro-diff ${cls}">${esc(t5)}</span>
                <span class="macro-dir ${cls}" title="${esc(lbl.full)}">${arrow(gd)} ${esc(lbl.short)}</span>
            </div>`;
        }
        const b = m.by_pair && m.by_pair[sym];
        if (!b) return '';
        const dirCls = b.macro_dir > 0 ? 'pos' : b.macro_dir < 0 ? 'neg' : 'mute';
        const diff = b.differential == null ? '--'
                   : (b.differential >= 0 ? '+' : '') + b.differential.toFixed(2);
        // Single combined rate string — kills the per-column alignment drift
        // that the previous base/quote 1fr 1fr layout caused across rows.
        const base = esc(rateStr(b.base_ccy));
        const quote = esc(rateStr(b.quote_ccy));
        return `<div class="macro-row">
            <span class="macro-pair">${esc(sym)}</span>
            <span class="macro-rates">${base}<span class="sep">/</span>${quote}</span>
            <span class="macro-diff ${dirCls}">${esc(diff)}</span>
            <span class="macro-dir ${dirCls}" title="${esc(b.label)}">${arrow(b.macro_dir)} ${esc(b.label)}</span>
        </div>`;
    }).join('');
    // Key indicators (real yield + US employment) lead the panel — they are
    // the highest-value macro signals, so they sit above the per-pair table.
    let keyBlock = '';
    if (ry && ry.value != null) {
        const ch = ry.change_1d;
        const chCls = ch > 0 ? 'pos' : ch < 0 ? 'neg' : 'mute';
        const chStr = ch == null ? '--' : (ch >= 0 ? '+' : '') + ch.toFixed(2);
        keyBlock += `<div class="macro-key">`
              + `<span class="macro-key-label">米10年実質利回り</span>`
              + `<span class="macro-key-val">${esc(ry.value.toFixed(2))}%</span>`
              + `<span class="macro-rynum ${chCls}">前日比 ${esc(chStr)}</span>`
              + `${ry.stale ? ' <span class="mute">*</span>' : ''}</div>`;
    }
    if (m.employment) {
        const e = m.employment;
        const nfp = e.nonfarm_change == null ? '--'
                  : (e.nonfarm_change >= 0 ? '+' : '') + Math.round(e.nonfarm_change);
        const nfpCls = e.nonfarm_change > 0 ? 'pos'
                     : e.nonfarm_change < 0 ? 'neg' : 'mute';
        const ur = e.unemployment_rate == null ? '--'
                 : e.unemployment_rate.toFixed(1) + '%';
        keyBlock += `<div class="macro-key">`
              + `<span class="macro-key-label">米雇用</span>`
              + `<span class="macro-key-val">NFP `
              + `<span class="macro-rynum ${nfpCls}">${esc(nfp)}k</span></span>`
              + `<span class="macro-rynum mute">失業率 ${esc(ur)}</span>`
              + `${e.stale ? ' <span class="mute" title="キャッシュ値 (取得失敗)">*</span>' : ''}</div>`;
    }
    root.innerHTML = keyBlock + rows;
}

// ------------------------------------------------------------
// Trade journal (discretionary) — REST-fed, broker-scoped.
// Each order placed through the dashboard is logged server-side together with
// the 3TF market context captured at entry. The panel reviews a trade against
// the setup it was actually taken on ("which alignment did I enter on?").
// ------------------------------------------------------------
const JR_TF_ORDER = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'];
let _journalServer = null;   // broker the panel currently reflects

/** Pull the recent journal from the backend and repaint the side panel. */
function fetchJournal() {
    fetch('/api/journal?limit=40')
        .then(r => (r.ok ? r.json() : null))
        .then(j => { if (j) paintJournal(j); })
        .catch(() => {});   // panel is best-effort; never surface fetch noise
}

/** Refetch the journal when the active broker changes (per-broker store). */
function maybeRefreshJournal(snap) {
    const srv = (snap && snap.account && snap.account.server) || null;
    if (srv && srv !== _journalServer) { _journalServer = srv; fetchJournal(); }
}

function paintJournal(data) {
    const root = $bind('journal');
    if (!root) return;
    const status = $bind('journal-status');
    const entries = (data && data.entries) || [];
    _journalServer = (data && data.server) || _journalServer;
    if (status) status.textContent = entries.length ? `${entries.length}件` : '記録なし';
    if (!entries.length) {
        root.innerHTML = '<div class="empty mute">発注すると、その時の3TF状況つきで自動記録されます</div>';
        return;
    }
    root.innerHTML = entries.map(renderJournalEntry).join('');
}

function renderJournalEntry(e) {
    const side = (e.side || '').toUpperCase();
    const sideCls = side === 'BUY' ? 'buy' : 'sell';
    const tsSec = (e.ts || 0) / 1000;
    const px = e.price != null ? fmtPrice(e.price, priceDigits(e.price, e.symbol)) : '--';
    const lots = e.lots != null ? Number(e.lots).toFixed(2) : '--';
    // SL/TP/ticket sub-line — only the parts that exist (SL/TP are discretionary
    // and frequently blank, so we never render empty placeholders).
    const sub = [];
    if (e.sl != null) sub.push('SL ' + fmtPrice(e.sl, priceDigits(e.sl, e.symbol)));
    if (e.tp != null) sub.push('TP ' + fmtPrice(e.tp, priceDigits(e.tp, e.symbol)));
    if (e.ticket != null) sub.push('#' + e.ticket);
    const subHtml = sub.length
        ? `<div class="jr-sub mute">${esc(sub.join(' ・ '))}</div>` : '';
    // 3TF context chips, ordered low→high TF. EMA side drives the ↑/↓ + colour;
    // ADX rides along as a strength read, with RSI / DI in the hover tooltip.
    const ctx = e.ctx || {};
    const tfs = Object.keys(ctx).sort((a, b) => {
        const ia = JR_TF_ORDER.indexOf(a), ib = JR_TF_ORDER.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    const chips = tfs.map(tf => {
        const c = ctx[tf] || {};
        const up = !!c.ae;
        const adx = c.adx != null ? ` <i>ADX${Math.round(c.adx)}</i>` : '';
        const tip = `EMA ${up ? '上' : '下'}`
            + (c.rsi != null ? ` / RSI ${c.rsi}` : '')
            + (c.dip != null && c.dim != null ? ` / +DI ${c.dip} -DI ${c.dim}` : '');
        return `<span class="jr-tf ${up ? 'up' : 'dn'}" title="${esc(tip)}">`
            + `${esc(tf)} ${up ? '↑' : '↓'}${adx}</span>`;
    }).join('');
    return `<div class="jr-row">
        <div class="jr-head">
            <span class="jr-side ${sideCls}">${esc(side)}</span>
            <span class="jr-sym">${esc(e.symbol || '')}</span>
            <span class="jr-lots mono">${esc(lots)}</span>
            <span class="jr-px mono">@${esc(px)}</span>
            <span class="jr-time mute">${esc(fmtJSTdate(tsSec))} ${esc(fmtJSTclockNoSec(tsSec))}</span>
        </div>
        ${chips ? `<div class="jr-ctx">${chips}</div>` : ''}
        ${subHtml}
    </div>`;
}

// ------------------------------------------------------------
// Per-symbol TF signal matrix + composite bias.
// This is the analytical core: ADX/RSI/EMA → "what should I do?".
// ------------------------------------------------------------

const TF_LABELS = ['D1', 'H4', 'H1', 'M15'];
const TF_WEIGHTS = { D1: 3, H4: 2, H1: 1.5, M15: 1 };

/** Map one TF's indicators to a 5-tier directional signal.
 *  Returns {code: -2..+2, label, cls}. */
function tfSignal(tf) {
    if (!tf || tf.rsi == null || tf.adx == null) {
        return { code: 0, label: 'n/a', cls: 'na' };
    }
    const adx = tf.adx, rsi = tf.rsi, aboveEma = tf.above_ema;
    const diBull = tf.di_plus != null && tf.di_minus != null && tf.di_plus > tf.di_minus;
    const diBear = tf.di_plus != null && tf.di_minus != null && tf.di_minus > tf.di_plus;
    if (aboveEma && adx >= 25 && diBull && rsi >= 55)
        return { code:  2, label: 'STRONG BUY',  cls: 'strong-buy'  };
    if (!aboveEma && adx >= 25 && diBear && rsi <= 45)
        return { code: -2, label: 'STRONG SELL', cls: 'strong-sell' };
    if (aboveEma && rsi >= 50)
        return { code:  1, label: 'BUY',         cls: 'buy'         };
    if (!aboveEma && rsi <= 50)
        return { code: -1, label: 'SELL',        cls: 'sell'        };
    return     { code:  0, label: 'NEUTRAL',     cls: 'neutral'     };
}

/** Regime gate: 0 = ranging (ADX ≤ 15) → 1 = trending (ADX ≥ 25), linear
 *  between. A TF's trend signal is scaled by this so a ranging market is
 *  pulled toward NEUTRAL instead of emitting a full-strength BUY/SELL.
 *  ADX is a crude regime proxy — good enough to damp obvious chop. */
function tfTrendFactor(tf) {
    if (!tf || tf.adx == null) return 0;
    return Math.max(0, Math.min(1, (tf.adx - 15) / 10));
}

/** Aggregate TF signals into a composite (-10..+10) + label/class.
 *  Each TF's contribution is regime-gated (see tfTrendFactor): in a range the
 *  numerator shrinks while the denominator stays full, so the composite
 *  collapses toward NEUTRAL. */
function compositeSignal(byTf) {
    let score = 0, weight = 0;
    for (const tfLabel of TF_LABELS) {
        const tf = byTf && byTf[tfLabel];
        if (!tf || tf.rsi == null || tf.adx == null) continue;
        const sig = tfSignal(tf);
        score  += sig.code * TF_WEIGHTS[tfLabel] * tfTrendFactor(tf);
        weight += TF_WEIGHTS[tfLabel];
    }
    if (weight === 0) return { score: 0, raw: 0, label: 'NO DATA', cls: 'na', arrow: '·' };
    // Score range: -2 * weightTotal .. +2 * weightTotal. Normalize to -10..+10.
    const normalized = (score / (2 * weight)) * 10;
    let label, cls, arrow;
    if (normalized >= 7)       { label = 'STRONG BUY';  cls = 'strong-buy';  arrow = '▲▲'; }
    else if (normalized >= 3)  { label = 'BUY';         cls = 'buy';         arrow = '▲';  }
    else if (normalized > -3)  { label = 'NEUTRAL';     cls = 'neutral';     arrow = '·';  }
    else if (normalized > -7)  { label = 'SELL';        cls = 'sell';        arrow = '▼';  }
    else                       { label = 'STRONG SELL'; cls = 'strong-sell'; arrow = '▼▼'; }
    return { score: normalized, raw: score, label, cls, arrow };
}

function pctEmaDist(close, ema) {
    if (close == null || ema == null || !isFinite(close) || !isFinite(ema) || ema === 0) {
        return null;
    }
    return ((close - ema) / ema) * 100;
}

// Trigger-CALENDAR controls (date-picker style). One capture-phase handler for
// every action: ◀/▶ year step (data-cal-go), open/close the year picker
// (data-cal-toggle-years), pick a year from it (data-cal-pick-year), select a
// month (data-cal-month, click again to clear), back to year-total
// (data-cal-allmonths). stopPropagation so the parent .panel collapse never fires.
document.addEventListener('click', (ev) => {
    const el = ev.target.closest(
        '[data-cal-go],[data-cal-month],[data-cal-pick-year],[data-cal-toggle-years],[data-cal-allmonths]');
    if (!el) return;
    ev.stopPropagation();
    ev.preventDefault();
    if (el.hasAttribute('data-cal-go')) {
        UI.calYear = Number(el.dataset.calGo); UI.calMonth = null; UI.calView = 'months';
    } else if (el.hasAttribute('data-cal-pick-year')) {
        UI.calYear = Number(el.dataset.calPickYear); UI.calMonth = null; UI.calView = 'months';
    } else if (el.hasAttribute('data-cal-toggle-years')) {
        UI.calView = UI.calView === 'years' ? 'months' : 'years';
    } else if (el.hasAttribute('data-cal-month')) {
        const m = Number(el.dataset.calMonth);
        UI.calMonth = (UI.calMonth === m) ? null : m;
    } else if (el.hasAttribute('data-cal-allmonths')) {
        UI.calMonth = null;
    }
    delete STAMPS['sig'];
    if (latestSnap) paintSignals(latestSnap);
}, true);

/** JST calendar year of an epoch-ms timestamp (UTC+9). */
function jstYear(ms) {
    return new Date(ms + 9 * 3600 * 1000).getUTCFullYear();
}

/** JST calendar month (1-12) of an epoch-ms timestamp (UTC+9). */
function jstMonth(ms) {
    return new Date(ms + 9 * 3600 * 1000).getUTCMonth() + 1;
}

/** Trigger-history CALENDAR — a familiar date-picker the user selects a period
 *  from. A year navigator (◀ year ▶, the year clickable to open a year grid) over
 *  the WHOLE record (16Y backtest ≤ last_year + live beyond it, data-driven — no
 *  fixed year/month list), a 12-month grid for the selected year (each month a
 *  net-pips heat cell), and the selected period's aggregate (+ that month's live
 *  trades). Compact: one year shown at a time. The 16Y backtest and live feed
 *  merge through one reader; backtest reads grey, live cyan. */
function buildTriggerCalendar(snap, sym) {
    const tf = UI.dwsBase;
    const brokerServer = snap.live_history?.server || null;
    const brokerSub = brokerServer
        ? `<span class="cal-broker" title="記録中ブローカー">${esc(brokerServer)}</span>`
        + `<span class="cal-rec" title="ライブを永続記録中">● 記録中</span>` : '';
    const head = `<div class="anlx-title">トリガー履歴 ${esc(tf)}`
        + `<span class="anlx-sub">16Y + ライブ連結 · pips（往復コスト控除 / ライブ2.0pip）${brokerSub}</span></div>`;

    // Backtest owns years ≤ last_year, live owns years beyond it (data-driven).
    const th = snap.oos_baseline?.by_symbol?.[sym]?.[tf]?.trigger_history || {};
    const csvBy = th.by_year || {};
    const csvLastYear = th.last_year
        || (Object.keys(csvBy).length ? Math.max(...Object.keys(csvBy).map(Number)) : 0);
    const liveBy = snap.live_history?.by_symbol?.[sym]?.[tf]?.by_year || {};
    const csvF = pipsFactor(sym, 'csv'), liveF = pipsFactor(sym, 'live');
    const ym = {};                                   // year -> {rec, f, isLive}
    for (const [y, r] of Object.entries(csvBy)) if (+y <= csvLastYear) ym[y] = { rec: r, f: csvF, isLive: false };
    for (const [y, r] of Object.entries(liveBy)) if (+y > csvLastYear) ym[y] = { rec: r, f: liveF, isLive: true };

    const openTrig = (snap.validation?.by_symbol?.[sym]?.[tf]?.raw?.recent_triggers || []).find(t => t.o) || null;
    const openYear = openTrig ? jstYear(openTrig.t) : null;
    const openMonth = openTrig ? jstMonth(openTrig.t) : null;

    const years = [...new Set([...Object.keys(ym).map(Number),
        ...(openYear && openYear > csvLastYear ? [openYear] : [])])].sort((a, b) => b - a);
    if (!years.length) {
        const msg = brokerServer ? 'このブローカーの記録は蓄積開始（確定トリガー待ち）'
                                 : '履歴データ取得待ち（ライブ検証は起動後 ~90 秒）';
        return `<div class="anlx-block anlx-triggers">${head}<div class="cal-empty">${esc(msg)}</div></div>`;
    }

    // --- selection state ---
    let calYear = Number(UI.calYear);
    if (!years.includes(calYear)) calYear = years[0];
    const calMonth = (UI.calMonth >= 1 && UI.calMonth <= 12) ? UI.calMonth : null;
    const yearsView = UI.calView === 'years';

    // --- colour scale anchored on the data's own |max| month (whole record) ---
    let maxAbs = 0;
    for (const k of Object.keys(ym)) {
        const ms = ym[k].rec.months || {};
        for (const m of Object.values(ms)) maxAbs = Math.max(maxAbs, Math.abs((m.cum_pts || 0) * ym[k].f));
    }
    const heat = (pips) => {
        const t = maxAbs > 0 ? Math.max(-1, Math.min(1, pips / maxAbs)) : 0;
        const a = (0.14 + 0.56 * Math.abs(t)).toFixed(3);
        return pips >= 0 ? `background:rgba(0,208,156,${a})` : `background:rgba(255,91,107,${a})`;
    };
    const yearPips = (y) => { const mm = ym[String(y)]; return mm ? (mm.rec.cum_pts || 0) * mm.f : 0; };

    // --- selected-period aggregate ---
    const meta = ym[String(calYear)];
    const f = meta ? meta.f : liveF;
    const yearRec = meta ? meta.rec : null;
    const isLive = meta ? meta.isLive : (calYear > csvLastYear);
    const toStat = (s) => ({ n: s ? s.n : 0, wins: s ? s.wins : 0, losses: s ? s.losses : 0,
        cum: s ? (s.cum_pts || 0) * f : 0, gw: s ? (s.gross_win || 0) * f : 0, gl: s ? (s.gross_loss || 0) * f : 0 });
    let stat, periodLabel, trades = [];
    if (calMonth) {
        stat = toStat((yearRec && yearRec.months || {})[String(calMonth)]);
        periodLabel = `${calYear}年 ${calMonth}月`;
        trades = ((yearRec && yearRec.trades) || []).filter(t => jstMonth(t.t) === calMonth).map(t => ({ ...t, p: t.p * f }));
    } else {
        stat = toStat(yearRec);
        periodLabel = `${calYear}年`;
        trades = ((yearRec && yearRec.trades) || []).map(t => ({ ...t, p: t.p * f }));
    }
    let nOpen = 0;
    if (openTrig && openYear === calYear && (!calMonth || openMonth === calMonth)) {
        nOpen = 1;
        trades = [{ t: openTrig.t, d: openTrig.d, p: openTrig.p * liveF, o: true }, ...trades];
    }
    const pf = stat.gl > 0 ? stat.gw / stat.gl : (stat.gw > 0 ? Infinity : 0);
    const wrTxt = stat.n ? (stat.wins / stat.n * 100).toFixed(1) + '%' : '--';
    const cumCls = stat.cum > 0 ? 'pos' : stat.cum < 0 ? 'neg' : '';

    // --- nav row (◀ older / year / ▶ newer ; year opens the year grid) ---
    const idx = years.indexOf(calYear);
    const newer = idx > 0 ? years[idx - 1] : null;      // years desc → idx-1 is more recent
    const older = idx < years.length - 1 ? years[idx + 1] : null;
    const yLiveCls = isLive ? ' is-live' : '';
    const nav = `<div class="cal-nav">`
        + `<span class="cal-arrow${older == null ? ' off' : ''}"${older != null ? ` data-cal-go="${older}"` : ''} title="前年">◀</span>`
        + `<span class="cal-ytitle${yLiveCls}" data-cal-toggle-years title="年を選ぶ">${calYear}年 <i>${yearsView ? '▴' : '▾'}</i></span>`
        + `<span class="cal-arrow${newer == null ? ' off' : ''}"${newer != null ? ` data-cal-go="${newer}"` : ''} title="翌年">▶</span>`
        + (calMonth ? `<span class="cal-chip" data-cal-allmonths title="年計に戻す">年計へ</span>`
                    : `<span class="cal-src${yLiveCls}">${isLive ? 'ライブ' : '16Y'}</span>`)
        + `</div>`;

    // --- calendar body: year-picker grid OR the 12-month grid ---
    let body;
    if (yearsView) {
        // Newest year first (top-left) — `years` is already sorted descending.
        body = `<div class="cal-years">` + years.map(y => {
            const p = yearPips(y), on = y === calYear ? ' on' : '';
            const live = ym[String(y)] && ym[String(y)].isLive ? ' is-live' : '';
            return `<span class="cal-ycell${on}${live}" style="${heat(p)}" data-cal-pick-year="${y}" `
                 + `title="${y}年 ${p >= 0 ? '+' : ''}${fmtPips(p)}pips">${y}<i>${p >= 0 ? '+' : ''}${fmtPips(p)}</i></span>`;
        }).join('') + `</div>`;
    } else {
        const months = (yearRec && yearRec.months) || {};
        body = `<div class="cal-months">` + Array.from({ length: 12 }, (_, i) => {
            const m = i + 1, s = months[String(m)], on = calMonth === m ? ' on' : '';
            if (!s) return `<span class="cal-mcell empty${on}" data-cal-month="${m}">${m}月<i>·</i></span>`;
            const p = (s.cum_pts || 0) * f;
            const wr = s.win_rate != null ? (s.win_rate * 100).toFixed(0) + '%' : '--';
            const pfm = s.profit_factor != null ? s.profit_factor.toFixed(2) : '∞';
            return `<span class="cal-mcell${on}" style="${heat(p)}" data-cal-month="${m}" `
                 + `title="${calYear}/${m}月 · ${s.n}件 · 勝率${wr} · PF ${pfm}">`
                 + `${m}月<i>${p >= 0 ? '+' : ''}${fmtPips(p)}</i></span>`;
        }).join('') + `</div>`;
    }

    // --- summary + (live month only) trade list ---
    const openNote = nOpen ? ` · <b class="cal-open">保有中 ${nOpen}</b>` : '';
    const summary = `<div class="cal-summary">`
        + `<b>${esc(periodLabel)} 確定 ${stat.n.toLocaleString('en-US')} 件</b> · 勝率 <b>${wrTxt}</b>`
        + ` · <b class="pos">${stat.wins.toLocaleString('en-US')}勝</b> <b class="neg">${stat.losses.toLocaleString('en-US')}敗</b>`
        + ` · PF <b>${pf === Infinity ? '∞' : pf.toFixed(2)}</b>`
        + ` · 累積 <b class="${cumCls}">${stat.cum >= 0 ? '+' : ''}${fmtPips(stat.cum)} pips</b>${openNote}</div>`;

    let listHtml = '';
    if (calMonth && isLive) {
        const rows = trades.map(t => {
            const dir = t.d > 0 ? 'buy' : 'sell', dt = t.d > 0 ? 'BUY' : 'SELL';
            const pt = `${t.p >= 0 ? '+' : ''}${fmtPips(t.p)}`;
            if (t.o) return `<div class="cal-trow open"><span class="cal-tt"><i>保有</i>${fmtJSTdate(t.t / 1000)} ${fmtJSTclockNoSec(t.t / 1000)}</span><span class="cal-td ${dir}">${dt}</span><span class="cal-tp muted">${pt}</span></div>`;
            const w = t.p > 0;
            return `<div class="cal-trow ${w ? 'win' : 'loss'}"><span class="cal-tt">${fmtJSTdate(t.t / 1000)} ${fmtJSTclockNoSec(t.t / 1000)}</span><span class="cal-td ${dir}">${dt}</span><span class="cal-tw ${w ? 'win' : 'loss'}">${w ? '✓' : '✗'}</span><span class="cal-tp ${w ? 'pos' : 'neg'}">${pt}</span></div>`;
        }).join('') || `<div class="cal-empty">${esc(periodLabel)}内のトリガーなし</div>`;
        listHtml = `<div class="tcal-list"><div class="tcal-scroll">${rows}</div></div>`;
    }

    return `<div class="anlx-block anlx-triggers">${head}${nav}${body}${summary}${listHtml}</div>`;
}

/** Analytics section = (A) live DWS-SMT trigger history (from the connected
 *  MT5 broker, any broker; rolling period filter) + (B) 16-year hourly
 *  win-rate heatmap. Both empirical. */
function buildAnalytics(snap, sym) {
    const sa = snap.analysis && snap.analysis.by_symbol && snap.analysis.by_symbol[sym];
    if (!sa) return '';
    return buildTriggerCalendar(snap, sym) + buildHourlyHeatmap(snap, sym);
}

/** Multiplier to convert a data source's raw net-"points" to PIPS.
 *    pips = raw_pts * (source_point / pip_price)
 *  source_point = price units per point the data was computed with: the frozen
 *  16Y baseline uses ``oos_point`` (Dukascopy 3/5-digit), the live feed uses the
 *  broker's ``point``. ``pip_price`` is the market pip in price units (gold
 *  $0.10, JPY 0.01, FX 0.0001). Returns 1 (raw points) if meta is missing, so
 *  it degrades gracefully and never yields NaN. ``source`` is 'live' or 'csv'. */
function pipsFactor(sym, source) {
    const m = (latestSnap && latestSnap.symbol_meta && latestSnap.symbol_meta[sym]) || {};
    const pip = m.pip_price || m.pip_size;
    if (!pip || !isFinite(pip) || pip <= 0) return 1;
    const srcPoint = source === 'live' ? m.point : m.oos_point;
    if (!srcPoint || !isFinite(srcPoint) || srcPoint <= 0) return 1;
    return srcPoint / pip;
}

/** Pips formatter — full magnitude, NO k/M abbreviation (user wants every
 *  digit). Sub-100 values keep one decimal; ≥100 are whole pips with comma
 *  grouping (e.g. 70,800). Sign handled by the caller. */
function fmtPips(v) {
    if (v == null || !isFinite(v)) return '--';
    if (Math.abs(v) < 100) return v.toFixed(1);
    return Math.round(v).toLocaleString('en-US');
}

/** (B) 16-year hourly win-rate heatmap for the selected base TF. Reads the
 *  static oos_baseline.json ``hourly_winrate`` (24 JST-hour buckets). Cells
 *  are coloured red→amber→green by win rate; the current JST hour is ringed
 *  so the user sees "are we in a statistically good hour right now?". */
/** EU summer time (London): last Sun Mar 01:00 UTC → last Sun Oct 01:00 UTC. */
function isDstEU(ms) {
    const y = new Date(ms).getUTCFullYear();
    const lastSun = (mon) => {                       // mon 0-based; 01:00 UTC
        const last = new Date(Date.UTC(y, mon + 1, 0));
        return Date.UTC(y, mon, last.getUTCDate() - last.getUTCDay(), 1);
    };
    return ms >= lastSun(2) && ms < lastSun(9);      // Mar → Oct
}
/** US summer time (New York): 2nd Sun Mar 07:00 UTC → 1st Sun Nov 06:00 UTC. */
function isDstUS(ms) {
    const y = new Date(ms).getUTCFullYear();
    const nthSun = (mon, n, hr) => {
        const first = new Date(Date.UTC(y, mon, 1));
        const day = 1 + ((7 - first.getUTCDay()) % 7) + (n - 1) * 7;
        return Date.UTC(y, mon, day, hr);
    };
    return ms >= nthSun(2, 2, 7) && ms < nthSun(10, 1, 6);   // Mar(2nd) → Nov(1st)
}

function buildHourlyHeatmap(snap, sym) {
    const baseTf = UI.dwsBase;
    const base = snap.oos_baseline?.by_symbol?.[sym]?.[baseTf];
    const baseHourly = base && base.hourly_winrate;
    if (!Array.isArray(baseHourly) || !baseHourly.length) return '';
    // Merge 16Y baseline + live (years past the CSV boundary) per JST hour, so
    // the time-of-day win-rate is "16Y + ライブ連結" and recomputes continuously
    // as live triggers accumulate. Live owns years > csvLastYear (same boundary
    // as the trigger-history table); the baseline owns everything ≤ it.
    const merged = Array.from({ length: 24 }, (_, h) => ({ hour: h, n: 0, wins: 0 }));
    for (const h of baseHourly) {
        const m = merged[h.hour];
        if (m) { m.n += h.n || 0; m.wins += h.wins || 0; }
    }
    const th = base.trigger_history || {};
    const csvLastYear = th.last_year
        || (Object.keys(th.by_year || {}).length
            ? Math.max(...Object.keys(th.by_year).map(Number)) : 0);
    const liveYears = snap.live_history?.by_symbol?.[sym]?.[baseTf]?.by_year || {};
    let liveN = 0;
    for (const [y, rec] of Object.entries(liveYears)) {
        if (Number(y) <= csvLastYear) continue;          // live owns > boundary
        for (const hb of (rec.hourly || [])) {
            const m = merged[hb.hour];
            if (m) { m.n += hb.n || 0; m.wins += hb.wins || 0; liveN += hb.n || 0; }
        }
    }
    // Combined population WR anchors the colour scale (honest baseline+live).
    const totN = merged.reduce((s, m) => s + m.n, 0);
    const totW = merged.reduce((s, m) => s + m.wins, 0);
    const popWr = totN ? totW / totN : (base.win_rate || 0);
    const hourly = merged.map(m => ({ hour: m.hour, n: m.n,
        win_rate: m.n ? m.wins / m.n : null }));
    const nowJst = (() => {
        const d = new Date(Date.now() + 9 * 3600 * 1000);
        return d.getUTCHours();
    })();
    // Colour scale anchored on the population WR: at/above pop → green ramp,
    // below → red ramp. Keeps the heatmap honest (a 40 % hour isn't "good" in
    // absolute terms, it's just at this symbol's baseline).
    const cellColor = (wr) => {
        if (wr == null) return 'rgba(255,255,255,0.05)';
        const d = wr - popWr;                     // deviation from baseline
        const t = Math.max(-1, Math.min(1, d / 0.10));   // ±10pp saturates
        if (t >= 0) {
            const a = 0.15 + t * 0.55;
            return `rgba(56,161,105,${a.toFixed(3)})`;     // green
        }
        const a = 0.15 + (-t) * 0.55;
        return `rgba(229,62,62,${a.toFixed(3)})`;          // red
    };
    const byHour = {};
    for (const h of hourly) byHour[h.hour] = h;

    // ① Order the boxes by FX session (Asia → London → NY) instead of 00:00.
    // ② DST-aware: Japan has no DST so the JST buckets are fixed; only the
    // London/NY session *boundaries* shift ±1h with EU/US summer time, detected
    // automatically from the current date.
    const nowMs = Date.now();
    const lonOpen = isDstEU(nowMs) ? 16 : 17;        // London 08:00 local → JST
    const nyOpen  = isDstUS(nowMs) ? 21 : 22;        // New York 08:00 local → JST
    const ASIA_OPEN = 8;                              // Tokyo-ish session start
    const SESS = { asia: 'アジア', london: 'ロンドン', ny: 'NY' };
    const seq = [];
    const push = (a, b, s) => { for (let x = a; x < b; x++) seq.push({ h: x % 24, s }); };
    push(ASIA_OPEN, lonOpen, 'asia');
    push(lonOpen, nyOpen, 'london');
    push(nyOpen, ASIA_OPEN + 24, 'ny');              // wraps past midnight

    const cell = (hr) => {
        const h = byHour[hr] || { hour: hr, n: 0, win_rate: null };
        const wr = h.win_rate;
        const isNow = hr === nowJst;
        // Below ~30 samples a cell's win rate is noise (wide CI), so it must NOT
        // be coloured as an edge. Render it neutral grey with the WR muted; the
        // tooltip says it's a reference value, not a signal.
        const lowN = wr != null && h.n < HM_MIN_N;
        const wrTxt = wr == null ? '--' : Math.round(wr * 100);
        const title = wr == null
            ? `${hr}時 JST — データなし`
            : `${hr}時 JST — WR ${(wr*100).toFixed(1)}% (N=${h.n}` + (lowN
                ? `・標本不足 N<${HM_MIN_N}: 参考値)`
                : `、母集団比 ${((wr-popWr)*100>=0?'+':'')}${((wr-popWr)*100).toFixed(1)}pp)`);
        const bg = lowN ? 'rgba(255,255,255,0.05)' : cellColor(wr);
        return `<div class="hm-cell${isNow ? ' is-now' : ''}${lowN ? ' hm-lown' : ''}"
                     style="background:${bg}" title="${esc(title)}">
            <span class="hm-hour">${String(hr).padStart(2,'0')}</span>
            <span class="hm-wr">${wrTxt}</span>
        </div>`;
    };
    const rowsHtml = ['asia', 'london', 'ny'].map(s => {
        const hrs = seq.filter(x => x.s === s).map(x => x.h);
        if (!hrs.length) return '';
        const rng = `${String(hrs[0]).padStart(2,'0')}–${String((hrs[hrs.length-1]+1)%24).padStart(2,'0')}`;
        return `<div class="hm-session">
            <div class="hm-session-label ${s}">${esc(SESS[s])}<span class="hm-sess-rng">${rng} JST</span></div>
            <div class="hm-session-cells">${hrs.map(cell).join('')}</div>
        </div>`;
    }).join('');

    const dstTag = (isDstEU(nowMs) || isDstUS(nowMs)) ? '夏時間' : '冬時間';
    return `<div class="anlx-block anlx-heatmap">
        <div class="anlx-title">時刻別勝率 ${esc(UI.dwsBase)}
            <span class="anlx-sub">16Y${liveN ? ' + ライブ ' + liveN.toLocaleString('en-US') + '件' : ''}・セッション別 (${dstTag}・母集団 WR ${(popWr*100).toFixed(1)}%基準、■=現在、灰=標本不足 N&lt;${HM_MIN_N})</span>
        </div>
        <div class="hm-sessions">${rowsHtml}</div>
    </div>`;
}


// -- Per-cell formatters (shared by both signal-table modes) -------------- //
// Each returns {txt, cls} so layout code can drop them into either grid.
function _sigCellEma(tf) {
    if (!tf) return { txt: '--', cls: 'num na' };
    const dist = pctEmaDist(tf.last_close, tf.ema);
    if (dist == null) return { txt: '--', cls: 'num' };
    return {
        txt: (dist > 0 ? '+' : '') + dist.toFixed(2) + '%',
        cls: dist > 0 ? 'num pos' : 'num neg',
    };
}
function _sigCellAdx(tf) {
    if (!tf || tf.adx == null) return { txt: '--', cls: 'num na' };
    return {
        txt: tf.adx.toFixed(1),
        cls: 'num' + (tf.adx >= 25 ? ' strong' : ''),
    };
}
function _sigCellDi(tf) {
    if (!tf || tf.di_plus == null || tf.di_minus == null)
        return { txt: '--', cls: 'num na' };
    const di = tf.di_plus - tf.di_minus;
    return {
        txt: (di > 0 ? '+' : '') + di.toFixed(1),
        cls: 'num' + (Math.abs(di) >= 5 ? (di > 0 ? ' pos' : ' neg') : ''),
    };
}
function _sigCellRsi(tf) {
    if (!tf || tf.rsi == null) return { txt: '--', cls: 'num na' };
    // Split the extremes so the zone reads at a glance: ≥70 overbought (red),
    // ≤30 oversold (green); mid-range stays neutral.
    let z = '';
    if (tf.rsi >= 70) z = ' warn-high';
    else if (tf.rsi <= 30) z = ' warn-low';
    return { txt: tf.rsi.toFixed(1), cls: 'num' + z };
}

/** Mode 1 (default, original): rows = TFs, columns = indicators. */
function _renderSignalsByTf(body, byTf) {
    const head = `<div class="sig-row sig-head">
        <span>TF</span><span>TREND</span><span>EMA</span><span>ADX</span>
        <span>DI±</span><span>RSI</span>
    </div>`;
    const rows = TF_LABELS.map(tfLabel => {
        const tf = byTf && byTf[tfLabel];
        if (!tf) {
            return `<div class="sig-row na">
                <span class="tf">${tfLabel}</span>
                <span class="trend na">--</span>
                <span class="num">--</span><span class="num">--</span>
                <span class="num">--</span><span class="num">--</span>
            </div>`;
        }
        const sig = tfSignal(tf);
        const ema = _sigCellEma(tf), adx = _sigCellAdx(tf);
        const di  = _sigCellDi(tf),  rsi = _sigCellRsi(tf);
        return `<div class="sig-row ${sig.cls}">
            <span class="tf">${tfLabel}</span>
            <span class="trend ${sig.cls}">${sig.label}</span>
            <span class="${ema.cls}">${ema.txt}</span>
            <span class="${adx.cls}">${adx.txt}</span>
            <span class="${di.cls}">${di.txt}</span>
            <span class="${rsi.cls}">${rsi.txt}</span>
        </div>`;
    }).join('');
    body.innerHTML = head + rows;
}

function paintSignals(snap) {
    const analysis = snap.analysis;
    if (!analysis || !analysis.by_symbol) return;
    // Skip on price-only frames — the signal matrix / composite / analytics
    // are analysis-derived. STAMPS['sig'] is busted on panel expand/collapse.
    if (!changed('sig', analysis.generated_at)) return;
    for (const sym of SYMBOL_ORDER) {
        const sa = analysis.by_symbol[sym];
        const byTf = sa && sa.by_tf;
        const body = $bind('sig-body-' + sym);
        if (body) _renderSignalsByTf(body, byTf);
        // Composite badge
        const comp = compositeSignal(byTf);
        const compWrap = $bind(`composite-${sym}`);
        const arrow = $bind(`comp-arrow-${sym}`);
        const text  = $bind(`comp-text-${sym}`);
        const score = $bind(`comp-score-${sym}`);
        const fill  = $bind(`comp-fill-${sym}`);
        if (compWrap)  compWrap.className  = 'composite ' + comp.cls;
        if (arrow) arrow.textContent = comp.arrow;
        if (text)  text.textContent  = comp.label;
        if (score) score.textContent =
            (comp.score > 0 ? '+' : '') + comp.score.toFixed(1) + '/10';
        // Gauge fill: width grows from center (50%) toward edge based on |score|.
        if (fill) {
            const pct = Math.min(50, Math.abs(comp.score) * 5);  // 10pt → 50%
            if (comp.score >= 0) {
                fill.style.left  = '50%';
                fill.style.width = pct + '%';
                fill.className = 'comp-gauge-fill ' + (comp.score >= 7 ? 'buy strong' : 'buy');
            } else {
                fill.style.left  = (50 - pct) + '%';
                fill.style.width = pct + '%';
                fill.className = 'comp-gauge-fill ' + (comp.score <= -7 ? 'sell strong' : 'sell');
            }
        }
        // Analytics section: intentionally blank — indicator charts were
        // removed per user direction. The .analytics div renders empty.
        const analytics = $bind(`analytics-${sym}`);
        if (analytics) analytics.innerHTML = buildAnalytics(snap, sym);
        // Panel activity glow now driven by composite signal strength
        // (replaces the previous structure-proximity tier).
        const panel = document.getElementById('panel-' + sym);
        if (panel) {
            panel.classList.remove('quiet', 'active', 'touch');
            if (comp.cls === 'strong-buy' || comp.cls === 'strong-sell') {
                panel.classList.add('touch');
            } else if (comp.cls === 'buy' || comp.cls === 'sell') {
                panel.classList.add('active');
            } else {
                panel.classList.add('quiet');
            }
        }
    }
}

// ------------------------------------------------------------
// Correlation insights — rank noteworthy pairs instead of a heatmap.
// The 10x10 grid was illegible; this picks the absolute strongest
// correlations and flags divergence (current vs the longest window).
// ------------------------------------------------------------

function _strengthLabel(abs) {
    if (abs >= 0.85) return { kind: 'very-strong', txt: '極強' };
    if (abs >= 0.7)  return { kind: 'strong',      txt: '強'   };
    if (abs >= 0.5)  return { kind: 'moderate',    txt: '中'   };
    return              { kind: 'weak',        txt: '弱'   };
}

// Effective correlation ≥ this links two positions into one bet. 0.6 catches
// meaningful overlap (≈36% shared variance); H1-window correlations sit lower
// than daily, so a 0.7 cutoff would miss real concentration.
const CONCENTRATION_THRESHOLD = 0.6;

/** Concentration warning (recommendation 5): cluster directional BIAS signals
 *  by effective correlation so correlated positions are counted as one bet, not
 *  several. Two positions are the same bet when corr × dirA × dirB is high —
 *  that also catches opposite-labelled pairs (e.g. EURUSD-short ≈ USDCHF-long). */
function paintConcentration(snap) {
    const el = $bind('concentration');
    if (!el) return;
    const analysis = snap.analysis, corr = snap.correlation;
    if (!analysis || !analysis.by_symbol || !corr || !corr.by_window) {
        el.innerHTML = ''; return;
    }
    // Skip on price-only frames (analysis + correlation drive this).
    if (!changed('conc', analysis.generated_at + ':' + UI.corrWindow)) return;
    const cw = corr.by_window[String(UI.corrWindow)]
            || corr.by_window[String(corr.default_window)];
    if (!cw || !cw.symbols || !cw.matrix) { el.innerHTML = ''; return; }
    const idx = {};
    cw.symbols.forEach((s, i) => { idx[s] = i; });

    // Directional BIAS signals that exist in the correlation matrix.
    const sigs = [];
    for (const sym of SYMBOL_ORDER) {
        const sa = analysis.by_symbol[sym];
        if (!sa || !sa.by_tf || idx[sym] == null) continue;
        const c = compositeSignal(sa.by_tf);
        if (Math.abs(c.score) < 3) continue;          // non-NEUTRAL only
        sigs.push({ sym, dir: c.score > 0 ? 1 : -1 });
    }
    if (sigs.length < 2) { el.innerHTML = ''; return; }

    // Union-find: link positions whose P/L moves together.
    const parent = sigs.map((_, i) => i);
    const find = i => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    for (let a = 0; a < sigs.length; a++) {
        for (let b = a + 1; b < sigs.length; b++) {
            const r = cw.matrix[idx[sigs[a].sym]][idx[sigs[b].sym]];
            if (r == null) continue;
            if (r * sigs[a].dir * sigs[b].dir >= CONCENTRATION_THRESHOLD)
                parent[find(a)] = find(b);
        }
    }
    const groups = {};
    sigs.forEach((s, i) => {
        const root = find(i);
        (groups[root] = groups[root] || []).push(s);
    });
    const allGroups = Object.values(groups);
    const clusters = allGroups.filter(g => g.length >= 2);
    if (clusters.length === 0) { el.innerHTML = ''; return; }

    const arrow = d => d > 0 ? '▲' : '▼';
    const head = `<div class="conc-head">⚠ 相関集中 — 方向シグナル ${sigs.length} 件 `
        + `≒ 独立 ${allGroups.length} ベット</div>`;
    const rows = clusters.map(g => {
        // Average effective correlation across the cluster's pairs.
        let sum = 0, cnt = 0;
        for (let a = 0; a < g.length; a++)
            for (let b = a + 1; b < g.length; b++) {
                const r = cw.matrix[idx[g[a].sym]][idx[g[b].sym]];
                if (r != null) { sum += r * g[a].dir * g[b].dir; cnt++; }
            }
        const avg = cnt ? sum / cnt : 0;
        const items = g.map(s =>
            `<span class="conc-sym ${s.dir > 0 ? 'buy' : 'sell'}">`
            + `${s.sym}${arrow(s.dir)}</span>`).join(' ');
        return `<div class="conc-row">${items} `
            + `<span class="conc-tag">≒ 1ベット（相関 ${avg >= 0 ? '+' : ''}`
            + `${avg.toFixed(2)}）</span></div>`;
    }).join('');
    el.innerHTML = head + rows;
}

/** Enable/disable the window pills by how many bars are actually loaded: a
 *  window of N bars needs ≥ N return rows. Without this, a window beyond the
 *  loaded history (e.g. 500 on a fresh terminal) is silently skipped by the
 *  backend and the list hangs on "waiting for data..." forever. Pills re-enable
 *  on their own as bars accumulate (this runs on every correlation update). */
function _paintCorrWindowAvail(corr) {
    const avail = Number((corr && corr.bars_available) || 0);
    const wins = $bind('corr-windows');
    if (!wins) return;
    const tf = (corr && corr.timeframe) || 'H1';
    wins.querySelectorAll('.pill').forEach(p => {
        const win = Number(p.dataset.win);
        const ok = avail >= win;
        p.classList.toggle('pill-disabled', !ok);
        p.disabled = !ok;          // a disabled <button> can't be clicked
        p.title = ok ? '' : `${tf} ${win}本ぶんの履歴が必要 (現在 ${avail}本)`;
    });
}

function paintCorrelationList(corr, force) {
    if (!corr) return;
    if (!force && !changed('corr-list', corr.generated_at + ':' + UI.corrWindow)) return;
    const root = $bind('corr-list');
    if (!root) return;
    _paintCorrWindowAvail(corr);
    const w = (corr.by_window || {})[String(UI.corrWindow)];
    if (!w || !w.matrix || !w.symbols || w.symbols.length === 0) {
        // Distinguish "not enough history for THIS window" (a permanent state
        // until more bars load) from a genuine pre-first-data wait, so the panel
        // never sits on an unexplained "waiting…".
        const avail = Number(corr.bars_available || 0);
        const need = Number(UI.corrWindow) || 0;
        const tf = esc(corr.timeframe || 'H1');
        if (avail > 0 && need > avail) {
            const usable = CORR_WINDOWS.filter(x => x <= avail);
            const hint = usable.length
                ? `より短い窓 (${usable.join(' / ')}本) を選択してください`
                : `データ蓄積をお待ちください`;
            root.innerHTML = `<div class="empty mute">データ不足: ${tf} ${need}本必要 ・ `
                + `取得 ${avail}本<br>${hint}</div>`;
        } else {
            root.innerHTML = '<div class="empty mute">データ取得中…</div>';
        }
        return;
    }
    const syms = w.symbols;
    const m = w.matrix;
    // Reference window for divergence detection = the longest configured window.
    const longestKey = String(
        (corr.windows || []).reduce((a, b) => (b > a ? b : a), 0));
    const refW = (corr.by_window || {})[longestKey];
    const refM = refW && refW.matrix;

    // Collect unique pairs (upper triangle).
    const pairs = [];
    for (let i = 0; i < syms.length; i++) {
        for (let j = i + 1; j < syms.length; j++) {
            const v = m[i][j];
            if (v == null || !isFinite(v)) continue;
            let refV = null;
            if (refM && refM[i] && refM[i][j] != null && isFinite(refM[i][j])) {
                refV = refM[i][j];
            }
            pairs.push({ a: syms[i], b: syms[j], v, refV });
        }
    }
    // Sort by abs(corr) desc — surface what's actually significant.
    pairs.sort((p, q) => Math.abs(q.v) - Math.abs(p.v));

    // Top 10 strongest.
    const top = pairs.slice(0, 10);

    // Divergence picks: pairs whose current correlation differs from the
    // reference by ≥ 0.35 and the reference is itself ≥ 0.6 (so we only
    // flag breaks of a *normally* strong relationship).
    const divergent = pairs
        .filter(p => p.refV != null && Math.abs(p.refV) >= 0.6 &&
                     Math.abs(p.v - p.refV) >= 0.35)
        .slice(0, 4);

    const rowHtml = (p) => {
        const abs = Math.abs(p.v);
        const strength = _strengthLabel(abs);
        const sign = p.v >= 0 ? 'pos' : 'neg';
        const arrow = p.v >= 0 ? '⟷' : '⇄';
        const dirTxt = p.v >= 0 ? '同調' : '逆相関';
        const barWidth = Math.round(abs * 100);
        const valStr = (p.v >= 0 ? '+' : '') + p.v.toFixed(2);
        return `
            <div class="corr-row ${sign} ${strength.kind}">
                <span class="pair"><b>${esc(p.a)}</b> ${arrow} <b>${esc(p.b)}</b></span>
                <span class="bar"><i style="width:${barWidth}%"></i></span>
                <span class="val">${valStr}</span>
                <span class="lbl">${strength.txt}${dirTxt}</span>
            </div>`;
    };

    let html = '<div class="corr-section-title">強い相関 TOP 10</div>';
    html += top.map(rowHtml).join('');
    if (divergent.length) {
        html += '<div class="corr-section-title warn">ダイバージェンス警告</div>';
        html += divergent.map(p => {
            const sign = p.v >= 0 ? 'pos' : 'neg';
            const dirTxt = p.v >= 0 ? '同調' : '逆相関';
            const refStr = (p.refV >= 0 ? '+' : '') + p.refV.toFixed(2);
            const curStr = (p.v    >= 0 ? '+' : '') + p.v.toFixed(2);
            return `
                <div class="corr-row diverge ${sign}">
                    <span class="pair"><b>${esc(p.a)}</b> ⚠ <b>${esc(p.b)}</b></span>
                    <span class="div-detail">通常 ${refStr} → 現在 ${curStr}</span>
                    <span class="lbl warn">${dirTxt}崩れ</span>
                </div>`;
        }).join('');
    }
    root.innerHTML = html;
}

// ------------------------------------------------------------
// DWS-SMT panel — 3-TF trend histogram + triggers (port of DWS_SMT.mq5)
// Backend computes the colours/triggers; this just renders them on a
// Canvas and lets the user switch the base timeframe (x-axis).
// ------------------------------------------------------------

const DWS_BASES = ['H4', 'H1', 'M15'];
const DWS_BASE_LABEL = { H4: '4H', H1: '1H', M15: 'M15' };
// Histogram cell colours by index: 0 up / 1 down / 2 flat.
const DWS_CELL = ['#00d09c', '#ff5b6b', '#3f4760'];

function dwsResult(snap, sym) {
    const sa = snap && snap.analysis && snap.analysis.by_symbol
             && snap.analysis.by_symbol[sym];
    return sa ? sa.dws : null;
}

/** Build the DWS panel skeleton once and wire the 4H/1H/M15 pills. */
function ensureDwsSkeleton(sym) {
    const host = $bind('dws-' + sym);
    if (!host || host.dataset.built) return host;
    host.innerHTML = `
        <div class="dws-head">
            <span class="dws-title">DWS-SMT</span>
            <span class="dws-sub">3TF一致トリガー</span>
            <span class="dws-pills">${DWS_BASES.map(b =>
                `<button class="pill${b === UI.dwsBase ? ' on' : ''}" `
                + `data-dws="${b}">${DWS_BASE_LABEL[b]}</button>`
            ).join('')}</span>
        </div>
        <div class="dws-state" data-bind="dws-state-${sym}">--</div>
        <div class="dws-validation" data-bind="dws-validation-${sym}">--</div>
        <div class="dws-sync" data-bind="dws-sync-${sym}">--</div>
        <div class="dws-canvas-wrap"><canvas data-bind="dws-canvas-${sym}"></canvas></div>
        <div class="dws-legend">
            <span><i class="sw" style="background:${DWS_CELL[0]}"></i>上</span>
            <span><i class="sw" style="background:${DWS_CELL[1]}"></i>下</span>
            <span><i class="sw" style="background:${DWS_CELL[2]}"></i>中立</span>
            <span class="tg buy">▲BUY</span>
            <span class="tg sell">▼SELL</span>
            <span class="tg exit">✕EXIT</span>
            <span class="dws-leg-sep">実線=BIAS整合 / 淡中抜き=未整合</span>
        </div>`;
    host.querySelectorAll('button[data-dws]').forEach(btn => {
        btn.onclick = (ev) => {
            // The panel itself collapses on click — stop the pill click from
            // bubbling up to that handler so switching the base TF keeps the
            // panel open.
            ev.stopPropagation();
            UI.dwsBase = btn.dataset.dws;
            document.querySelectorAll('.dws-pills .pill').forEach(p =>
                p.classList.toggle('on', p.dataset.dws === UI.dwsBase));
            // Force a redraw despite the paintDws / paintSignals memoization
            // gates — the hourly heatmap in analytics follows the selected
            // base TF, so it must re-render on a TF switch too.
            delete STAMPS['dws'];
            delete STAMPS['sig'];
            if (latestSnap) { paintDws(latestSnap); paintSignals(latestSnap); }
        };
    });
    host.dataset.built = '1';
    return host;
}

function paintDws(snap) {
    // Skip on price-only frames — the histogram is analysis-derived. The
    // 'dws' stamp is busted on panel expand/collapse and on a TF-pill click
    // so those still force a redraw. The validation layer refreshes on its
    // own cadence, so its timestamp is folded into the stamp key.
    const analysis = snap.analysis;
    const vts = (snap.validation && snap.validation.generated_at) || 0;
    if (analysis && !changed('dws', analysis.generated_at + ':' + vts)) return;
    for (const sym of SYMBOL_ORDER) {
        ensureDwsSkeleton(sym);
        drawDwsCanvas(snap, sym);
    }
}

/** Format a base-bar epoch-ms time for the x-axis.
 *  The label must stay unambiguous across the window's span:
 *   - M15 (96 bars ≈ 24h)  → HH:MM
 *   - H1  (96 bars ≈ 4 days) → M/D HH:MM  (HH:MM alone repeats every 24h)
 *   - H4  (96 bars ≈ 16 days) → M/D */
function dwsAxisLabel(ms, base) {
    // Render in JST (UTC+9), consistent with the clock, trigger history and
    // heatmap — getMonth/getHours would use the browser's local tz instead.
    const dt = new Date(ms + 9 * 3600 * 1000);
    if (isNaN(dt.getTime())) return '';
    const p = n => String(n).padStart(2, '0');
    const md = `${p(dt.getUTCMonth() + 1)}/${p(dt.getUTCDate())}`;
    const hm = `${p(dt.getUTCHours())}:${p(dt.getUTCMinutes())}`;
    if (base === 'H4') return md;
    if (base === 'H1') return `${md} ${hm}`;
    return hm;
}

/** A BUY/SELL trigger is "tradeable" only when the composite BIAS agrees with
 *  it (BIAS out of the NEUTRAL band, same direction). EXIT is a risk signal,
 *  not a directional entry, so it is always treated as relevant. */
function dwsTriggerTradeable(g, biasScore) {
    if (g === 'EXIT') return true;
    if (g === 'BUY')  return biasScore >= 3;
    if (g === 'SELL') return biasScore <= -3;
    return false;
}

/** Macro alignment of a BUY/SELL trigger for *sym*: +1 aligned with the carry,
 *  -1 counter-carry, 0 when there is no macro data. EXIT is direction-neutral. */
function dwsTriggerMacroAlign(g, sym, snap) {
    if (g !== 'BUY' && g !== 'SELL') return 0;
    let macroDir;
    if (sym === 'XAUUSD') {
        // Gold: drive direction off the US real yield (gold ∝ −real yield);
        // fall back to the policy-rate trend when the real yield is missing.
        const ry = snap.real_yield;
        if (ry && ry.value != null) {
            macroDir = ry.gold_dir;
        } else {
            const b = snap.macro && snap.macro.by_pair && snap.macro.by_pair[sym];
            macroDir = b ? b.macro_dir : 0;
        }
    } else {
        const b = snap.macro && snap.macro.by_pair && snap.macro.by_pair[sym];
        macroDir = b ? b.macro_dir : 0;
    }
    if (!macroDir) return 0;
    const triggerDir = g === 'BUY' ? 1 : -1;
    return triggerDir === macroDir ? 1 : -1;
}

/** Draw a trigger marker. BIAS-confirmed BUY/SELL are filled; unconfirmed ones
 *  are drawn hollow (outline only) so the eye lands on the tradeable ones. */
function drawDwsMarker(ctx, cx, cy, g, tradeable) {
    if (g === 'EXIT') {
        ctx.strokeStyle = '#ffb74d'; ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(cx - 4, cy - 4); ctx.lineTo(cx + 4, cy + 4);
        ctx.moveTo(cx + 4, cy - 4); ctx.lineTo(cx - 4, cy + 4);
        ctx.stroke();
        return;
    }
    const col = g === 'BUY' ? '#00d09c' : '#ff5b6b';
    ctx.beginPath();
    if (g === 'BUY') {
        ctx.moveTo(cx, cy - 5); ctx.lineTo(cx - 5, cy + 4); ctx.lineTo(cx + 5, cy + 4);
    } else {
        ctx.moveTo(cx, cy + 5); ctx.lineTo(cx - 5, cy - 4); ctx.lineTo(cx + 5, cy - 4);
    }
    ctx.closePath();
    if (tradeable) {
        // BIAS-confirmed: bright filled triangle.
        ctx.fillStyle = col;
        ctx.fill();
    } else {
        // Unconfirmed: faded hollow outline — clearly de-emphasised vs filled.
        ctx.save();
        ctx.globalAlpha = 0.45;
        ctx.strokeStyle = col;
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
    }
}

// Base-TF bar length in minutes — used for the "confirm in …" countdown.
const TF_MINUTES = { M15: 15, H1: 60, H4: 240, D1: 1440, W1: 10080 };

/** Format ms-until-close as "確定まで H:MM:SS" (or M:SS under an hour). */
function fmtCountdown(ms) {
    if (ms <= 0) return '足確定 (更新待ち)';
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const p = n => String(n).padStart(2, '0');
    return '確定まで ' + (h > 0 ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`);
}

/** Update the state line above the canvas (current alignment + latest trigger). */
function updateDwsState(el, win) {
    if (!el) return;
    const N = win.c.length;
    const last = win.c[N - 1];
    const allUp = last.every(c => c === 0);
    const allDown = last.every(c => c === 1);
    // Each semantic element gets its OWN visual identity (not all one grey):
    //  STATE  = filled pill, direction-coloured (the headline);
    //  SIGNAL = chip, direction word in its colour + bars-ago muted;
    //  TIME   = cool (blue) chip — "time" is its own colour family, kept apart
    //           from the warm buy/sell palette. The rightmost bar is FORMING
    //           (EMA colours flicker intra-bar); the countdown shows when it
    //           confirms, ticked every 1 s by startTickers().
    let pillCls, pillTxt;
    if (allUp)        { pillCls = 'buy';  pillTxt = '▲ 揃い BUY'; }
    else if (allDown) { pillCls = 'sell'; pillTxt = '▼ 揃い SELL'; }
    else              { pillCls = 'wait'; pillTxt = '— 待機 (不一致)'; }
    let html = `<span class="dws-pill ${pillCls}">${esc(pillTxt)}</span>`;

    for (let j = N - 1; j >= 0; j--) {
        if (win.g[j]) {
            const g = win.g[j];
            const gc = g === 'BUY' ? 'tg-buy' : g === 'SELL' ? 'tg-sell' : 'tg-exit';
            html += `<span class="dws-chip"><span class="ck">最新</span>`
                  + `<span class="${gc}">${esc(g)}</span>`
                  + `<span class="cv">${N - 1 - j}本前</span></span>`;
            break;
        }
    }

    const mins = TF_MINUTES[UI.dwsBase];
    if (mins && win.t && win.t.length) {
        const closeMs = win.t[win.t.length - 1] + mins * 60000;
        html += `<span class="dws-chip cd"><span class="dws-cd" data-close="${closeMs}">`
              + `${esc(fmtCountdown(closeMs - Date.now()))}</span></span>`;
    }
    el.className = 'dws-state';
    el.innerHTML = html;
}

/** Composite BIAS score (-10..+10) for a symbol, or 0 when unavailable. */
function dwsBiasScore(snap, sym) {
    const sa = snap.analysis && snap.analysis.by_symbol && snap.analysis.by_symbol[sym];
    return (sa && sa.by_tf) ? compositeSignal(sa.by_tf).score : 0;
}

/** Update the BIAS ⇄ DWS alignment line (recommendation 1).
 *  The actionable read is the *divergence*: agreement is expected (both are
 *  EMA-based), so the line flags when the two methods disagree. */
function updateDwsSync(sym, snap, win) {
    const el = $bind('dws-sync-' + sym);
    if (!el) return;
    const biasScore = dwsBiasScore(snap, sym);
    const biasDir = biasScore >= 3 ? 'BUY' : biasScore <= -3 ? 'SELL' : 'NEUTRAL';
    const last = win.c[win.c.length - 1];
    const dwsDir = last.every(c => c === 0) ? 'BUY'
                 : last.every(c => c === 1) ? 'SELL' : 'NEUTRAL';
    const ar = d => d === 'BUY' ? '▲' : d === 'SELL' ? '▼' : '·';
    let txt, cls;
    if (biasDir !== 'NEUTRAL' && biasDir === dwsDir) {
        txt = `✓ 整合 — BIAS ${ar(biasDir)} と DWS ${ar(dwsDir)} が一致`;
        cls = 'dws-sync ' + (biasDir === 'BUY' ? 'buy' : 'sell');
    } else if (biasDir !== 'NEUTRAL' && dwsDir !== 'NEUTRAL') {
        txt = `⚠ 逆行 — DWS ${ar(dwsDir)}${dwsDir} / BIAS ${ar(biasDir)}${biasDir}（要注意）`;
        cls = 'dws-sync conflict';
    } else if (dwsDir !== 'NEUTRAL') {
        txt = `⚠ 乖離 — DWS ${ar(dwsDir)}${dwsDir} / BIAS NEUTRAL（DWS先行）`;
        cls = 'dws-sync diverge';
    } else if (biasDir !== 'NEUTRAL') {
        txt = `⚠ 乖離 — BIAS ${ar(biasDir)}${biasDir} / DWS 不一致（DWS未追随）`;
        cls = 'dws-sync diverge';
    } else {
        txt = '— BIAS・DWS とも待機';
        cls = 'dws-sync';
    }
    if (dwsTriggerMacroAlign(dwsDir, sym, snap) < 0) {
        txt += '・マクロ逆行';
        cls += ' macro-counter';
    }
    el.className = cls;
    el.textContent = txt;
}


// Per-symbol persisted state for the "説明" disclosure on the validation
// block. Defaults to collapsed; the user opens once and the choice survives
// reloads. localStorage key prefix kept short to avoid collision noise.
const DWS_DESC_OPEN_KEY = 'mt5dash.dwsDescOpen';
const DWS_DESC_OPEN = (() => {
    try { return new Set(JSON.parse(localStorage.getItem(DWS_DESC_OPEN_KEY) || '[]')); }
    catch (_e) { return new Set(); }
})();
function _saveDwsDescOpen() {
    try { localStorage.setItem(DWS_DESC_OPEN_KEY, JSON.stringify([...DWS_DESC_OPEN])); }
    catch (_e) { /* private mode etc — non-fatal */ }
}

// Delegate clicks on the "説明" disclosure toggle. Toggles class on the
// per-symbol validation block; CSS hides/shows every .dws-vdesc inside.
// stopPropagation is critical — the parent .panel has its own click-to-collapse
// listener (onPanelClick), so without it the panel would fold up underneath us
// and the user would never get to read the descriptions we just opened.
document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-explain-toggle]');
    if (!btn) return;
    ev.stopPropagation();
    ev.preventDefault();
    const sym = btn.dataset.explainToggle;
    const wrap = $bind('dws-validation-' + sym);
    if (!wrap) return;
    const open = wrap.classList.toggle('desc-open');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.querySelector('.dws-vexplain-icon').textContent = open ? '▼' : '▶';
    if (open) DWS_DESC_OPEN.add(sym); else DWS_DESC_OPEN.delete(sym);
    _saveDwsDescOpen();
}, true);    // capture phase — fires before panel's own bubble-phase listener

/** Render the deep-history OOS confidence block for the selected base TF.
 *
 *  PRIMARY data: the 16-year offline baseline (snap.oos_baseline), produced by
 *  scripts/_oos_xauusd_16y.py over the FULL Dukascopy CSV history (no year
 *  filter, no warmup skip). Includes Wilson + moving-block-bootstrap CIs and
 *  a chronological 2-period drift z-test (Bonferroni α/3 corrected).
 *
 *  SECONDARY data: the live rolling ~7-month broker-fetched validation
 *  (snap.validation), used only as a "recent regime drift" indicator
 *  alongside the 16Y figures. */
function updateDwsValidation(sym, snap) {
    const el = $bind('dws-validation-' + sym);
    if (!el) return;
    const pct = x => (x == null ? '--' : (x * 100).toFixed(2) + '%');
    const pct0 = x => (x == null ? '--' : Math.round(x * 100) + '%');
    const fmtN = n => (n == null ? '--' : Number(n).toLocaleString('en-US'));
    const fmtPf = pf => (pf == null ? '∞' : Number(pf).toFixed(2));

    // ---- Primary: 16Y deep-history evaluation ----
    const base = snap.oos_baseline && snap.oos_baseline.by_symbol
              && snap.oos_baseline.by_symbol[sym]
              && snap.oos_baseline.by_symbol[sym][UI.dwsBase];

    const histArr = (snap.validation_history
                  && snap.validation_history[sym]
                  && snap.validation_history[sym][UI.dwsBase]) || [];

    // No 16Y baseline available (CSV missing for this symbol) — fall back to
    // empty state + secondary if any.
    if (!base) {
        el.className = 'dws-validation';
        el.innerHTML = `<div class="dws-vempty">16Y 評価 — データ未取得</div>`
                     + _buildSecondaryRolling(sym, snap, null);
        return;
    }

    const tierCls = base.tier === '信頼' ? 'trusted'
                  : base.tier === '要注意' ? 'caution' : 'insufficient';
    el.className = 'dws-validation ' + tierCls;

    // Period-drift verdict — STABLE / DRIFT / REGIME-CHANGE.
    const ps = base.period_split || null;
    const verdictHtml = _buildPeriodVerdict(ps);

    const wilsonCi = `${(base.ci_low * 100).toFixed(1)}–${(base.ci_high * 100).toFixed(1)}%`;
    const bootCi = base.bootstrap_ci
        ? `${(base.bootstrap_ci.ci_low * 100).toFixed(1)}–${(base.bootstrap_ci.ci_high * 100).toFixed(1)}%`
        : null;
    // Convert the frozen-baseline points to PIPS for display (gold $1.00=10pips).
    const pipF = pipsFactor(sym, 'csv');
    const expPips = base.expectancy * pipF;
    const ddPips = base.max_drawdown * pipF;
    const expCls = base.expectancy > 0 ? 'pos' : base.expectancy < 0 ? 'neg' : '';
    const breakeven = base.breakeven_wr != null
        ? `${(base.breakeven_wr * 100).toFixed(1)}%` : '--';

    // cell(label, value, valueClass, description)
    //   description is always visible — full sentence explanation below the row.
    const cell = (k, v, vcls, desc) =>
        `<div class="dws-vcell">`
      + `<div class="dws-vrow">`
      +   `<span class="dws-vk">${k}</span>`
      +   `<span class="dws-vv ${vcls || ''}">${v}</span>`
      + `</div>`
      + (desc ? `<div class="dws-vdesc">${desc}</div>` : '')
      + `</div>`;

    const verdictDescHtml = (ps && ps.early && ps.late
            && ps.drift_wr_pp != null && ps.p_wr_raw != null) ? `<div class="dws-vverdict-desc">
        <b>期間ドリフト検定</b>:
        <em>2010-2017</em> (N=${fmtN(ps.early.n)} WR ${(ps.early.win_rate*100).toFixed(2)}%)
        vs <em>2018-2025</em> (N=${fmtN(ps.late.n)} WR ${(ps.late.win_rate*100).toFixed(2)}%) を
        <b>2-proportion z-test</b> で比較。drift <em>${ps.drift_wr_pp>=0?'+':''}${ps.drift_wr_pp.toFixed(2)}pp</em>,
        p=<em>${ps.p_wr_raw.toExponential(2)}</em>,
        <b>Bonferroni α/3=0.0167（症状毎=3TF）</b> ${ps.p_wr_bonferroni_significant?'<span class="dws-sig">クリア (有意)</span>':'未クリア (非有意)'}。
        Verdict <b>${esc(ps.verdict)}</b>: ${ps.verdict==='STABLE'?'両期間で統計的に差なし':
            ps.verdict==='DRIFT'?'有意差あり、ただし <em>|drift|<5pp</em>':'有意差あり、<em>|drift|≥5pp</em> で局面変化レベル'}
    </div>` : '';

    const headerHtml =
        `<div class="dws-vhead">`
      + `<span class="dws-vtier">${esc(base.tier)}</span>`
      + `<span class="dws-vlabel">16Y ディープ評価 · N=${fmtN(base.n_trades)}</span>`
      + verdictHtml
      + `<button type="button" class="dws-vexplain" data-explain-toggle="${esc(sym)}"`
      +   ` aria-expanded="false" title="各項目の統計的説明を展開">`
      +   `<span class="dws-vexplain-icon">▶</span><span class="dws-vexplain-label">説明</span>`
      + `</button>`
      + `</div>`
      + `<div class="dws-vhead-desc">`
      +   `Dukascopy CSV <em>16 年 (2010-2025)</em> の全データを <b>deterministic DWS-SMT ルール</b> で再評価した結果。`
      +   `<b>year filter / warmup skip 一切なし</b>。<em>N</em> はシグナル発出回数。`
      +   `Tier <b>${esc(base.tier)}</b>: ${
            base.tier === '信頼' ? '<b>Wilson CI 下限 > Breakeven</b> かつ <b>全 3 期間の期待値 > 0</b> — 統計的に支持された継続的エッジ'
            : base.tier === '要注意' ? 'CI 下限が Breakeven 未満、または期間ごとに不安定 — エッジ不確実'
            : '<em>N < 30</em> — サンプル不足で評価不可'}`
      + `</div>`
      + verdictDescHtml;

    const DESC = {
        wr: `<em>16 年間</em> に発出した DWS-SMT シグナル <em>N=${fmtN(base.n_trades)}</em> 回のうち、`
          + `<b>スプレッドコスト</b>を差し引いた <b>純損益 > 0</b> となった割合。母集団全体の勝率`,
        wilson: `<b>Wilson Score 95% 信頼区間</b>。各トレードを <b>独立した Bernoulli 試行</b> と仮定した二項分布の信頼区間。`
              + `小サンプルでも <em>[0%, 100%]</em> 外に出ない頑健な推定。母集団真の WR がこの範囲に <em>95%</em> の確率で存在`,
        boot: `<b>Moving-Block Bootstrap</b> (<em>block=50 trades, iterations=10,000</em>) 95% 信頼区間。`
            + `連続するトレードが同じ相場環境を共有することによる <b>自己相関を考慮</b> した CI。`
            + `Wilson CI と概ね一致 → 自己相関の影響は軽微と確認`,
        pf: `<b>Profit Factor</b> = 勝ちトレード総利益 ÷ 負けトレード総損失 (絶対値)。<b>> 1 で総合プラス収支</b>、`
          + `<em>> 2</em> で十分に強いエッジとされる。スプレッドコスト込みの計算`,
        ev: `1 トレードあたりの平均純損益 (<em>pips 単位 ($1.00=10pips)、スプレッドコスト込み</em>)。<b>期待値 × 取引回数 ≈ 累積損益</b>。`
          + `これがプラスである限り、長期的にトレードを続ければ利益が積み上がる`,
        be: `現状の <em>(平均勝ち額 / 平均負け額)</em> 比率で損益をゼロにするのに必要な勝率。<b>実勝率がこれを上回れば長期プラス</b>。`
          + `現状実勝率 <em>${(base.win_rate*100).toFixed(2)}%</em> は Breakeven <em>${(base.breakeven_wr*100).toFixed(1)}%</em> を `
          + `<b>+${((base.win_rate-base.breakeven_wr)*100).toFixed(1)}pp 上回る</b>`,
        dd: `16 年累積損益曲線のピークから谷までの最大下落幅 (<em>pips</em>)。<b>過去最悪のドローダウン</b>。`
          + `期待値 <em>${fmtPips(expPips)} pips × ${Math.ceil(base.max_drawdown / Math.max(1, base.expectancy))} 回</em> のトレードで回復計算`,
    };

    // Quality colours so "is this edge good?" reads at a glance:
    //   勝率 green when it clears Breakeven (an edge exists), red if below.
    //   PF green ≥2 (strong), amber 1-2 (marginal), red <1 (losing); ∞→green.
    const wrCls = 'dws-num ' + (base.breakeven_wr != null && base.win_rate < base.breakeven_wr
        ? 'neg' : 'pos');
    const pfv = base.profit_factor;
    const pfCls = 'dws-num ' + (pfv == null || pfv >= 2 ? 'pos' : pfv >= 1 ? 'warn' : 'neg');

    const statsHtml =
        `<div class="dws-vstats">`
      + cell('勝率',         pct(base.win_rate),            wrCls,                    DESC.wr)
      + cell('Wilson 95%CI', esc(wilsonCi),                 '',                       DESC.wilson)
      + (bootCi ? cell('Bootstrap 95%CI', esc(bootCi),      '',                       DESC.boot) : '')
      + cell('PF',           esc(fmtPf(base.profit_factor)), pfCls,                   DESC.pf)
      + cell('期待値',       `${expPips >= 0 ? '+' : ''}${fmtPips(expPips)} pips`,
             'dws-num ' + expCls,                                                     DESC.ev)
      + cell('Breakeven WR', esc(breakeven),                '',                       DESC.be)
      + cell('MaxDD',        `${fmtPips(ddPips)} pips`,
             '',                                                                      DESC.dd)
      + `</div>`;

    // ---- Secondary: rolling ~7M live validation ----
    const secondaryHtml = _buildSecondaryRolling(sym, snap, base);

    // Sparkline canvas placeholder.
    const sparkHtml = histArr.length >= 2
        ? `<canvas class="dws-vspark" data-bind="dws-vspark-${sym}" width="160" height="22"></canvas>`
        : '';

    // Recent regime FIRST (a drawdown banner + the rolling line), THEN the 16Y
    // deep-eval — so the eye anchors on the CURRENT regime, not the favourable
    // long-run when conditions have deteriorated.
    el.innerHTML = _buildRegimeBanner(sym, snap, base)
                 + secondaryHtml + headerHtml + statsHtml + sparkHtml;

    // Re-apply the persisted "説明" disclosure state for this symbol.
    if (DWS_DESC_OPEN.has(sym)) {
        el.classList.add('desc-open');
        const btn = el.querySelector('[data-explain-toggle]');
        if (btn) {
            btn.setAttribute('aria-expanded', 'true');
            const icon = btn.querySelector('.dws-vexplain-icon');
            if (icon) icon.textContent = '▼';
        }
    }

    if (sparkHtml) {
        drawValidationSparkline(sym, histArr,
            base ? base.profit_factor : null);
    }
}

/** Build the chronological-split verdict badge (STABLE / DRIFT / REGIME-CHANGE).
 *  Verdict is precomputed offline in the OOS baseline JSON using a
 *  Bonferroni-corrected 2-prop z-test on WR + Welch t-test on expectancy. */
function _buildPeriodVerdict(ps) {
    if (!ps || ps.drift_wr_pp == null || ps.p_wr_raw == null) return '';
    const v = ps.verdict;
    const drift = ps.drift_wr_pp;
    let cls = 'stable', label = 'STABLE', dir = '';
    if (v === 'DRIFT') {
        cls = 'drift';
        label = 'DRIFT';
        dir = drift > 0 ? ' ↑改善' : ' ↓悪化';
    } else if (v === 'REGIME-CHANGE') {
        cls = 'regime';
        label = 'REGIME-CHANGE';
        dir = drift > 0 ? ' ↑改善' : ' ↓悪化';
    }
    const sign = drift > 0 ? '+' : '';
    const driftTxt = `${sign}${drift.toFixed(2)}pp`;
    return `<span class="dws-vverdict ${cls}" `
         + `title="2010-2017 vs 2018-2025: drift ${driftTxt}, p=${ps.p_wr_raw.toExponential(2)}, Bonferroni ${ps.p_wr_bonferroni_significant ? '有意' : '非有意'}">`
         + `${esc(label)}${esc(dir)} · ${esc(driftTxt)}</span>`;
}

/** Live rolling PF vs the 16Y baseline PF for the active base TF (UI.dwsBase),
 *  as a signed fraction (-0.30 = 30% below baseline), or null when either side
 *  is missing/degenerate. Shared by the regime banner and the #3 regime gate so
 *  the warning the trader sees and the gating that demotes a setup use the SAME
 *  number. Pure read — never throws on partial snapshots. */
function _regimeDrift(sym, snap) {
    const base = snap.oos_baseline && snap.oos_baseline.by_symbol
              && snap.oos_baseline.by_symbol[sym]
              && snap.oos_baseline.by_symbol[sym][UI.dwsBase];
    const stats = snap.validation && snap.validation.by_symbol
               && snap.validation.by_symbol[sym]
               && snap.validation.by_symbol[sym][UI.dwsBase];
    const c = stats && stats.raw;
    if (!base || !base.profit_factor || base.profit_factor <= 0) return null;
    if (!c || c.profit_factor == null || c.profit_factor === Infinity) return null;
    return (c.profit_factor - base.profit_factor) / base.profit_factor;
}

/** Regime banner shown ABOVE everything: when the live rolling PF has drifted
 *  materially BELOW the 16Y baseline (< -20%), warn loudly so the trader doesn't
 *  anchor on the favourable long-run during a deteriorating regime. A materially
 *  BETTER regime (> +20%) gets a quiet positive note; in-line with 16Y → nothing
 *  (the rolling line already carries the detail). */
function _buildRegimeBanner(sym, snap, base) {
    const stats = snap.validation && snap.validation.by_symbol
                  && snap.validation.by_symbol[sym]
                  && snap.validation.by_symbol[sym][UI.dwsBase];
    const c = stats && stats.raw;
    if (!c || !base || !base.profit_factor || base.profit_factor <= 0
        || c.profit_factor == null || c.profit_factor === Infinity) return '';
    const drift = (c.profit_factor - base.profit_factor) / base.profit_factor;
    const pct = Math.round(drift * 100);
    const wr = c.win_rate == null ? '--' : Math.round(c.win_rate * 100) + '%';
    if (drift <= -0.20) {
        return `<div class="dws-regime warn">⚠ <b>直近地合い悪化</b>`
             + ` · 直近PF <b>${c.profit_factor.toFixed(2)}</b> <span class="neg">(16Y比 ${pct}%)</span>`
             + ` · 勝率 ${esc(wr)} · N=${c.n_trades}`
             + ` <em>— 16Y統計を過信せず、直近の劣化を前提に判断を</em></div>`;
    }
    if (drift >= 0.20) {
        return `<div class="dws-regime good">直近地合い良好`
             + ` · 直近PF <b>${c.profit_factor.toFixed(2)}</b> <span class="pos">(16Y比 +${pct}%)</span>`
             + ` · 勝率 ${esc(wr)} · N=${c.n_trades}</div>`;
    }
    return '';
}

/** Build the secondary "直近 ローリング ~7M" line — N, WR, PF drift vs 16Y. */
function _buildSecondaryRolling(sym, snap, base) {
    const v = snap.validation;
    const stats = v && v.by_symbol && v.by_symbol[sym]
                  && v.by_symbol[sym][UI.dwsBase];
    if (!stats || !stats.raw) {
        return `<div class="dws-vsecondary dws-vsec-empty">`
             + `直近ローリング — 検証中</div>`;
    }
    const c = stats.raw;
    const pf = c.profit_factor == null ? '∞' : c.profit_factor.toFixed(2);
    const wrTxt = c.win_rate == null ? '--' : Math.round(c.win_rate * 100) + '%';
    const thirds = (c.thirds || [])
        .map(t => (t.expectancy > 0 ? '✓' : '✗')).join('') || '--';

    // PF drift vs the 16Y baseline. ±20% is alarm.
    let driftHtml = '';
    if (base && base.profit_factor && c.profit_factor != null
        && c.profit_factor !== Infinity && base.profit_factor > 0) {
        const drift = (c.profit_factor - base.profit_factor) / base.profit_factor;
        const driftPct = Math.round(drift * 100);
        const driftCls = Math.abs(drift) > 0.20
            ? (drift > 0 ? 'pos warn' : 'neg warn')
            : (drift > 0 ? 'pos' : drift < 0 ? 'neg' : '');
        const arrow = drift > 0 ? '↑' : drift < 0 ? '↓' : '·';
        driftHtml = ` <span class="dws-pf-drift ${driftCls}">`
                  + `${arrow}${driftPct > 0 ? '+' : ''}${driftPct}% vs 16Y</span>`;
    }
    return `<div class="dws-vsecondary">`
         + `<div class="dws-vsec-row">`
         +   `<span class="dws-vsec-label">直近ローリング (~7M)</span>`
         +   `<span class="dws-vsec-fig">N=${c.n_trades}`
         +     ` · 勝率 ${esc(wrTxt)} · PF ${esc(pf)}${driftHtml}`
         +     ` · 安定性 ${esc(thirds)}</span>`
         + `</div>`
         + `<div class="dws-vsec-desc">`
         +   `直近 <em>~7 ヶ月 (M15 で 20,000 バー)</em> を broker から fetch して同じルールで再評価。`
         +   `<b>16Y baseline からのドリフト確認用</b>。`
         +   `PF 比較で <em>+20% 以上</em> の乖離は要警戒。`
         +   `安定性 <b>${esc(thirds)}</b> = 直近期間を時系列で <em>3 等分</em> し、各期間の期待値の符号 `
         +   `(✓ = プラス、✗ = マイナス)`
         + `</div>`
         + `</div>`;
}

/** Draw a tiny PF-vs-time sparkline into the validation block's canvas.
 *  Shows the rolling PF trace, a horizontal 16y-baseline reference line,
 *  and a final-point marker so the "current vs trend" gap is glanceable. */
function drawValidationSparkline(sym, history, baselinePF) {
    const canvas = $bind('dws-vspark-' + sym);
    if (!canvas) return;
    const W = canvas.width, H = canvas.height;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    const pad = 2;
    const pts = history.map(v => (v == null ? null : v));
    const numeric = pts.filter(v => v != null);
    if (numeric.length === 0) return;
    let lo = Math.min(...numeric, baselinePF != null ? baselinePF : Infinity);
    let hi = Math.max(...numeric, baselinePF != null ? baselinePF : -Infinity);
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    const span = hi - lo;
    const xStep = (W - pad * 2) / Math.max(1, pts.length - 1);
    const y = v => pad + (H - pad * 2) * (1 - (v - lo) / span);

    // 16y baseline horizontal line (dashed, muted).
    if (baselinePF != null && isFinite(baselinePF)) {
        ctx.strokeStyle = 'rgba(212, 218, 230, 0.45)';
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 2]);
        const yb = y(baselinePF);
        ctx.beginPath();
        ctx.moveTo(pad, yb);
        ctx.lineTo(W - pad, yb);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // History trace.
    ctx.strokeStyle = '#4d8eff';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    pts.forEach((v, i) => {
        if (v == null) { started = false; return; }
        const x = pad + i * xStep;
        const yy = y(v);
        if (!started) { ctx.moveTo(x, yy); started = true; }
        else { ctx.lineTo(x, yy); }
    });
    ctx.stroke();

    // Final point dot — coloured by drift sign vs baseline (if any).
    const last = numeric[numeric.length - 1];
    const lastX = pad + (pts.length - 1) * xStep;
    const lastY = y(last);
    let dotCol = '#4d8eff';
    if (baselinePF != null && baselinePF > 0) {
        const drift = (last - baselinePF) / baselinePF;
        dotCol = Math.abs(drift) > 0.20
            ? (drift > 0 ? '#00d09c' : '#ff5b6b')
            : '#d0d6e2';
    }
    ctx.fillStyle = dotCol;
    ctx.beginPath(); ctx.arc(lastX, lastY, 2.2, 0, Math.PI * 2); ctx.fill();
}

function drawDwsCanvas(snap, sym) {
    const canvas = $bind('dws-canvas-' + sym);
    if (!canvas) return;
    const wrap = canvas.parentElement;
    const W = wrap.clientWidth, H = wrap.clientHeight;
    if (W === 0 || H === 0) return;            // panel collapsed / hidden

    const dpr = window.devicePixelRatio || 1;
    const bw = Math.round(W * dpr), bh = Math.round(H * dpr);
    if (canvas.width !== bw || canvas.height !== bh) {
        // Reassigning width/height reallocates + clears the bitmap; only do it
        // on a real size change. setTransform+clearRect below clear every draw.
        canvas.width = bw; canvas.height = bh;
        canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const stateEl = $bind('dws-state-' + sym);
    const d = dwsResult(snap, sym);
    const win = d && d.by_base && d.by_base[UI.dwsBase];
    if (!win || !win.c || win.c.length === 0) {
        ctx.fillStyle = '#8089a0';
        ctx.font = '12px monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText('データなし', W / 2, H / 2);
        if (stateEl) { stateEl.textContent = 'データなし'; stateEl.className = 'dws-state'; }
        const syncEl0 = $bind('dws-sync-' + sym);
        if (syncEl0) { syncEl0.textContent = '--'; syncEl0.className = 'dws-sync'; }
        updateDwsValidation(sym, snap);
        return;
    }

    const rows = win.rows, N = win.c.length;
    const biasArr = win.bias || [];              // per-bar historical BIAS
    const ptMult = pointMultiplierFor(sym);      // price → MT5 points
    const liveF = pipsFactor(sym, 'live');       // MT5 points → pips (live feed)
    const tradeByEntry = {};                     // entry bar idx → trade record
    (win.trades || []).forEach(t => { tradeByEntry[t.i] = t; });
    const gutter = 30, axisH = 14, markH = 32;   // markH fits marker + P/L label
    const plotX = gutter, plotW = W - gutter - 4;
    const plotY = 2, plotH = H - axisH - markH - 4;
    const rowH = plotH / rows.length, barW = plotW / N;

    // 3 stacked colour rows
    for (let r = 0; r < rows.length; r++) {
        const y = plotY + r * rowH;
        for (let j = 0; j < N; j++) {
            ctx.fillStyle = DWS_CELL[win.c[j][r]] || DWS_CELL[2];
            ctx.fillRect(plotX + j * barW, y + 1, Math.max(1, barW - 0.4), rowH - 2);
        }
        ctx.fillStyle = '#f2f4f9';
        ctx.font = '700 11px monospace';
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillText(rows[r], 4, y + rowH / 2);
    }

    // Trigger guide lines + markers — BIAS-confirmed triggers are solid with a
    // bright guide line; unconfirmed ones are hollow with a faint line.
    const markY = plotY + plotH;
    for (let j = 0; j < N; j++) {
        const g = win.g[j];
        if (!g) continue;
        const cx = plotX + j * barW + barW / 2;
        // Judge each trigger by the BIAS as it was *at that bar* (no look-ahead).
        const barBias = biasArr[j] != null ? biasArr[j] : 0;
        const tradeable = dwsTriggerTradeable(g, barBias);
        const macroAlign = dwsTriggerMacroAlign(g, sym, snap);
        const a = tradeable ? 0.55 : 0.16;
        ctx.strokeStyle = g === 'BUY' ? `rgba(0,208,156,${a})`
                        : g === 'SELL' ? `rgba(255,91,107,${a})`
                        : `rgba(255,183,77,${a})`;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(cx, plotY); ctx.lineTo(cx, markY); ctx.stroke();
        drawDwsMarker(ctx, cx, markY + 9, g, tradeable);
        if (macroAlign < 0) {
            // Counter-carry: this BUY/SELL fights the rate differential.
            ctx.fillStyle = '#ffb74d';
            ctx.font = '700 9px monospace';
            ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
            ctx.fillText('逆', cx, plotY - 1);
        }
        // Per-trigger trade result (recommendation A): the P/L of the trade
        // this BUY/SELL opened. EXIT triggers open no trade → no label.
        const tr = tradeByEntry[j];
        if (tr) {
            // The histogram is a TIMING guide, not a P/L ledger: show the GROSS
            // move in pips with NO spread deduction. The 履歴 table is the
            // spread-accurate record (each trade net of its own bar's spread),
            // so it reads slightly smaller than this gross figure — never larger.
            const pips = tr.p * ptMult * liveF;                 // gross, in pips
            ctx.fillStyle = pips > 0 ? '#00d09c' : pips < 0 ? '#ff5b6b' : '#8089a0';
            ctx.font = '700 9px monospace';
            ctx.textAlign = 'center'; ctx.textBaseline = 'top';
            ctx.fillText((pips >= 0 ? '+' : '') + fmtPips(pips), cx, markY + 19);
        }
    }

    // x-axis time labels
    ctx.fillStyle = '#f2f4f9';
    ctx.font = '10px monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const ticks = 4;
    for (let t = 0; t <= ticks; t++) {
        const j = Math.min(N - 1, Math.round(t / ticks * (N - 1)));
        const cx = Math.min(W - 14, Math.max(plotX + 14, plotX + j * barW + barW / 2));
        ctx.fillText(dwsAxisLabel(win.t[j], UI.dwsBase), cx, markY + markH + 1);
    }

    updateDwsState(stateEl, win);
    updateDwsValidation(sym, snap);
    updateDwsSync(sym, snap, win);
}

// ------------------------------------------------------------
// WebSocket lifecycle
// ------------------------------------------------------------

function connect() {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    const ws = new WebSocket(url);
    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.partial && latestSnap) {
            // Price-only update — merge the light fields into the held
            // snapshot, keeping the heavy analysis blocks from the last full.
            latestSnap.version = msg.version;
            latestSnap.ts = msg.ts;
            latestSnap.status = msg.status;
            latestSnap.price = msg.price;
            latestSnap.account = msg.account;
        } else {
            // Full snapshot. The static 16Y oos_baseline (~2 MB) ships only on
            // the FIRST full per connection; cache it and re-attach to every
            // later full so all readers keep seeing snap.oos_baseline.
            if (msg.oos_baseline) OOS_BASELINE = msg.oos_baseline;
            else if (OOS_BASELINE) msg.oos_baseline = OOS_BASELINE;
            latestSnap = msg;
        }
        // Schedule paint on next frame so we coalesce bursts.
        if (!pendingFrame) {
            pendingFrame = requestAnimationFrame(paintAll);
        }
    };
    ws.onclose = () => {
        const dot = $bind('conn-dot'); if (dot) dot.className = 'dot off';
        const ct  = $bind('conn-text'); if (ct) ct.textContent = 'reconnecting...';
        setTimeout(connect, 1000);
    };
    ws.onerror = () => ws.close();
}

let pendingFrame = null;
function paintAll() {
    pendingFrame = null;
    if (!latestSnap) return;
    paintHeader(latestSnap);
    paintSummaryBar(latestSnap);
    paintPrices(latestSnap);
    paintSignals(latestSnap);
    paintAccount(latestSnap);
    paintStrength(latestSnap.strength);
    paintCorrelationList(latestSnap.correlation);
    paintConcentration(latestSnap);
    paintCalendar(latestSnap);
    paintMacro(latestSnap);
    paintDws(latestSnap);
    maybeRefreshJournal(latestSnap);
}

// 1-second clock + countdown tick (independent of WS)
function startTickers() {
    setInterval(() => {
        if (!latestSnap) return;
        $bind('clock').textContent = fmtJSTclock(Date.now() / 1000);
        // Smoothly tick the DWS bar-close countdown(s) between 5 s snapshots.
        const now = Date.now();
        document.querySelectorAll('.dws-cd[data-close]').forEach(s => {
            const c = Number(s.dataset.close);
            if (c) s.textContent = fmtCountdown(c - now);
        });
    }, 1000);
}

// ------------------------------------------------------------
// Broker switcher — click the ▾ next to the ACCOUNT identity to swap
// MT5 terminals. The server writes .env and self-restarts; the existing
// WebSocket reconnect loop picks the new instance up automatically.
// ------------------------------------------------------------

async function setupBrokerSwitcher() {
    const toggle = $bind('broker-toggle');
    const menu = $bind('broker-menu');
    if (!toggle || !menu) return;

    let info;
    try {
        info = await fetch('/api/broker').then(r => r.json());
    } catch (e) {
        toggle.disabled = true;
        return;
    }
    const current = info.current_path || '';
    const presets = info.presets || {};

    menu.innerHTML = Object.entries(presets).map(([name, path]) => {
        const active = path === current ? ' active' : '';
        return `<button class="broker-opt${active}" data-name="${esc(name)}">`
             + `<span class="broker-check">${path === current ? '●' : '○'}</span>`
             + `<span>${esc(name)}</span></button>`;
    }).join('');

    toggle.onclick = (ev) => {
        ev.stopPropagation();
        menu.hidden = !menu.hidden;
    };
    document.addEventListener('click', (ev) => {
        if (!menu.hidden && !menu.contains(ev.target) && ev.target !== toggle) {
            menu.hidden = true;
        }
    });

    menu.querySelectorAll('.broker-opt').forEach(btn => {
        btn.onclick = async (ev) => {
            ev.stopPropagation();
            const name = btn.dataset.name;
            if (btn.classList.contains('active')) { menu.hidden = true; return; }
            menu.hidden = true;
            showSwitchOverlay(`Switching to ${name}…`);
            try {
                const res = await fetch('/api/broker', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name}),
                }).then(r => r.json());
                if (!res.ok) {
                    showSwitchOverlay(`切替失敗: ${res.error || 'unknown'}`, true);
                    return;
                }
                showSwitchOverlay(
                    `Switching to ${name}…  サーバー再起動中(約7秒)`,
                    false);
            } catch (e) {
                showSwitchOverlay('サーバー再起動中…', false);
            }
        };
    });
}

function showSwitchOverlay(msg, isError) {
    let ov = document.getElementById('broker-switch-overlay');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'broker-switch-overlay';
        document.body.appendChild(ov);
    }
    ov.textContent = msg;
    ov.className = isError ? 'err' : '';
}


// ------------------------------------------------------------
// Display auto-fit — render the dashboard at a fixed 2560-wide design
// and scale it to whatever monitor / window it is shown on, so the
// layout looks identical on 2560×1440, 1920×1080, ultrawide, half-width
// windowed, etc. Width drives the scale; the body's logical height is
// derived so the grid still fills the viewport vertically.
// ------------------------------------------------------------

const DESIGN_WIDTH = 2560;

function applyDisplayFit() {
    const scale = Math.min(1, window.innerWidth / DESIGN_WIDTH);
    const b = document.body;
    b.style.width = DESIGN_WIDTH + 'px';
    b.style.height = (window.innerHeight / scale) + 'px';
    b.style.transform = 'scale(' + scale + ')';
}

// ------------------------------------------------------------
// Boot
// ------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    applyDisplayFit();
    buildSymbolGrid();
    buildStrengthRows();
    buildCorrelationButtons();
    startTickers();
    setupBrokerSwitcher();
    setupOrderModal();
    paintAlertBell();                 // reflect the saved high-conviction-alert state
    // Summary bar: restore + persist its open/closed state (default open).
    const sb = document.querySelector('.summary-bar');
    if (sb) {
        try { if (localStorage.getItem('mt5-summary') === '0') sb.open = false; } catch (e) {}
        sb.addEventListener('toggle', () => {
            try { localStorage.setItem('mt5-summary', sb.open ? '1' : '0'); } catch (e) {}
        });
    }
    connect();
    // NB: the journal is seeded by maybeRefreshJournal() on the first snapshot
    // that carries account.server (the broker-scoped store key) — NOT here. A
    // load-time fetch would fire a second, broker-unknown request (the observed
    // double GET /api/journal).
});

window.addEventListener('resize', () => {
    applyDisplayFit();
    // Canvas-drawn elements (DWS-SMT histogram) need an explicit redraw
    // when the logical viewport height changes — DOM-based panels reflow
    // via CSS automatically.
    delete STAMPS['dws'];
    if (latestSnap && !pendingFrame) {
        pendingFrame = requestAnimationFrame(paintAll);
    }
});
