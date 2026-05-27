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
const JP_BIAS = {
    "STRONG BUY":  "STRONG BUY優位",
    "BUY":         "BUY優位",
    "NEUTRAL":     "NEUTRAL",
    "SELL":        "SELL優位",
    "STRONG SELL": "STRONG SELL優位",
};
const STRENGTH_WINDOWS = ["H1", "H4", "D1", "W1"];
const CORR_WINDOWS = [20, 100, 500];

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
function touchClass(distance, atr) {
    if (atr == null || !isFinite(atr) || atr <= 0) return '';
    const a = Math.abs(distance);
    if (a <= 0.10 * atr) return 'touch';
    if (a <= 0.30 * atr) return 'near';
    if (a <= 0.50 * atr) return 'far';
    return '';
}
function categoryLabel(cat) {
    return ({
        resistance: 'R',  support: 'S',
        trend_up: 'TL↑',  trend_down: 'TL↓',
        supply_zone: 'Sup', demand_zone: 'Dem',
        channel: 'CH', fibonacci: 'Fib', note: 'Txt',
        previous: 'Prev', round: 'Rnd', swing: 'Sw',
        session: 'Ses', vwap: 'VWAP', other: 'Lv',
    })[cat] || 'Lv';
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
            <button class="panel-close" type="button" title="閉じる (Esc)">✕</button>
        </div>
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
                <div class="comp-gauge-axis">
                    <span>強売 -10</span><span>0</span><span>+10 強買</span>
                </div>
            </div>
            <span class="comp-score" data-bind="comp-score-${sym}" title="複合スコア = TF別シグナル × TF加重 / 正規化">--</span>
        </div>
        <div class="signals" data-bind="signals-${sym}">
            <div class="sig-row sig-head">
                <span>TF</span><span>TREND</span><span>EMA</span><span>ADX</span><span>DI±</span><span>RSI</span>
            </div>
            <div class="sig-row" data-bind="sig-D1-${sym}"></div>
            <div class="sig-row" data-bind="sig-H4-${sym}"></div>
            <div class="sig-row" data-bind="sig-H1-${sym}"></div>
            <div class="sig-row" data-bind="sig-M15-${sym}"></div>
        </div>
        <div class="analytics" data-bind="analytics-${sym}"></div>
        <div class="dws" data-bind="dws-${sym}"></div>
    `;
    return a;
}

/** Toggle the clicked panel between compact and expanded.
 *  Clicking the close button (✕) inside an expanded panel collapses it
 *  without re-triggering expansion. */
function onPanelClick(ev, panel, grid) {
    const closeBtn = ev.target.closest('.panel-close');
    const wasExpanded = panel.classList.contains('expanded');
    if (closeBtn && wasExpanded) {
        ev.stopPropagation();
        panel.classList.remove('expanded');
        grid.classList.remove('has-expanded');
        // Re-render to refresh struct row count for new mode
        if (latestSnap) { delete STAMPS['struct']; delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
        return;
    }
    // Clear any other expansion + toggle this one
    grid.querySelectorAll('.panel.expanded').forEach(p => p.classList.remove('expanded'));
    grid.classList.toggle('has-expanded', !wasExpanded);
    if (!wasExpanded) panel.classList.add('expanded');
    if (latestSnap) { delete STAMPS['struct']; delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
}

// Esc key collapses expanded panel
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const grid = document.querySelector('.symbols');
    if (!grid || !grid.classList.contains('has-expanded')) return;
    grid.querySelectorAll('.panel.expanded').forEach(p => p.classList.remove('expanded'));
    grid.classList.remove('has-expanded');
    if (latestSnap) { delete STAMPS['struct']; delete STAMPS['sig']; delete STAMPS['dws']; paintAll(); }
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
            // Force re-render of strength + bias on next snapshot.
            for (const c of STRENGTH_CCYS) delete STAMPS[`strength:${c}`];
            for (const s of SYMBOL_ORDER) delete STAMPS[`bias:${s}`];
            if (latestSnap) {
                paintStrength(latestSnap.strength, true);
                paintBias(latestSnap.strength, true);
            }
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
    const scored = [];
    for (const sym of SYMBOL_ORDER) {
        const sa = analysis.by_symbol[sym];
        if (!sa || !sa.by_tf) continue;
        const c = compositeSignal(sa.by_tf);
        if (c.cls === 'na' || c.cls === 'neutral') continue;
        if (Math.abs(c.score) < 5) continue;  // only high-conviction
        scored.push({ sym, c });
    }
    if (scored.length === 0) {
        root.innerHTML = '<span class="active-empty">no high-conviction signals</span>';
        return;
    }
    scored.sort((a, b) => Math.abs(b.c.score) - Math.abs(a.c.score));
    root.innerHTML = scored.slice(0, 8).map(({ sym, c }) => {
        const scoreStr = (c.score > 0 ? '+' : '') + c.score.toFixed(1);
        return `<span class="active-chip ${c.cls}">
            <span class="ac-sym">${sym}</span>
            <span class="ac-side">${c.arrow} ${c.label}</span>
            <span class="ac-score">${scoreStr}</span>
        </span>`;
    }).join('');
}

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

function _setPanelMod(panel, mod) {
    // Preserve `expanded` across paint cycles; just swap the activity tier.
    panel.classList.remove('quiet', 'active', 'touch');
    panel.classList.add(mod);
}

function paintHero(snap) {
    // Hero = nearest structure with state classification on the panel.
    const ticks = (snap.price && snap.price.ticks) || {};
    const analysis = snap.analysis;
    const structures = snap.structures;
    if (!structures) return;
    for (const sym of SYMBOL_ORDER) {
        const t = ticks[sym];
        const price = t ? t.bid : null;
        const sa = analysis && analysis.by_symbol && analysis.by_symbol[sym];
        const h4 = sa && sa.by_tf && sa.by_tf.H4;
        const atr = h4 ? h4.atr : null;
        const levels = (structures.levels_by_symbol || {})[sym] || [];
        const panel = document.getElementById('panel-' + sym);
        const hero  = $bind('hero-' + sym);
        if (!panel || !hero) continue;

        if (price == null || atr == null || levels.length === 0) {
            _setPanelMod(panel, 'quiet');
            hero.textContent = 'no signal';
            continue;
        }
        let nearest = null, dist = Infinity;
        for (const l of levels) {
            if (l.price == null || !isFinite(l.price)) continue;
            const d = Math.abs(l.price - price);
            if (d < dist) { dist = d; nearest = l; }
        }
        if (!nearest) {
            _setPanelMod(panel, 'quiet');
            hero.textContent = 'no signal';
            continue;
        }
        const dig = priceDigits(price);
        const signed = nearest.price - price;
        const pip = signed * Math.pow(10, dig);
        const tier = touchClass(signed, atr);
        const arrow = signed > 0 ? '↑' : signed < 0 ? '↓' : '·';
        const stateLabel = tier === 'touch' ? 'TOUCHING'
                         : tier === 'near'  ? 'NEAR'
                         : tier === 'far'   ? 'APPROACH'
                         : 'idle';
        hero.textContent = arrow + ' ' + categoryLabel(nearest.category) +
            ' ' + fmtPrice(nearest.price, dig) +
            '  ' + (pip > 0 ? '+' : '') + pip.toFixed(1) + ' pip  · ' + stateLabel;
        let mod = 'quiet';
        if (tier === 'touch') mod = 'touch';
        else if (tier === 'near' || tier === 'far') mod = 'active';
        _setPanelMod(panel, mod);
    }
}

function paintStruct(snap, force) {
    const structures = snap.structures;
    if (!structures) return;
    if (!force && !changed('struct', structures.generated_at)) return;
    const ticks = (snap.price && snap.price.ticks) || {};
    const analysis = snap.analysis;
    for (const sym of SYMBOL_ORDER) {
        const root = $bind('struct-' + sym);
        if (!root) continue;
        const panel = document.getElementById('panel-' + sym);
        const isExpanded = panel && panel.classList.contains('expanded');
        const cap = isExpanded ? 16 : 3;

        const levels = (structures.levels_by_symbol || {})[sym] || [];
        const t = ticks[sym];
        const price = t ? t.bid : null;
        const sa = analysis && analysis.by_symbol && analysis.by_symbol[sym];
        const h4 = sa && sa.by_tf && sa.by_tf.H4;
        const atr = h4 ? h4.atr : null;
        const dig = priceDigits(price || (levels[0] && levels[0].price));

        const items = levels.filter(l => l.price != null && isFinite(l.price))
            .map(l => {
                const d = price != null ? l.price - price : NaN;
                const pip = isFinite(d) ? d * Math.pow(10, dig) : NaN;
                const atrMult = (atr != null && atr > 0 && isFinite(d))
                    ? Math.abs(d) / atr : null;
                return Object.assign({}, l, {
                    _d: d,
                    _pip: pip,
                    _abs: isFinite(d) ? Math.abs(d) : Infinity,
                    _tier: touchClass(d, atr),
                    _atrMult: atrMult,
                });
            })
            .sort((a, b) => a._abs - b._abs)
            .slice(0, cap);

        const dataHtml = items.map(l => {
            const tier = l._tier;
            const dir   = l._d > 0 ? 'R' : l._d < 0 ? 'S' : '·';
            const dirCls= l._d > 0 ? 'resist' : 'support';
            const pipStr = isFinite(l._pip)
                ? (l._pip > 0 ? '+' : '') + l._pip.toFixed(1) : '--';
            const atrStr = (l._atrMult != null) ? l._atrMult.toFixed(2) + '×' : '--';
            const tf  = l.tf || '--';
            const imp = Math.min(5, Math.max(1, l.importance || 1));
            const stars = '★'.repeat(imp) + '☆'.repeat(5 - imp);
            return `<div class="struct-row ${tier}" title="${l.category} | source=${l.source || '--'} | tf=${tf}">
                <span class="cat" title="カテゴリ">${categoryLabel(l.category)}</span>
                <span class="name" title="${l.name}">${l.name}</span>
                <span class="src" title="検出した足">${tf}</span>
                <span class="imp" title="重要度 (1-5)">${stars}</span>
                <span class="dir ${dirCls}" title="${dirCls === 'resist' ? '現在価格より上 (レジスタンス)' : '現在価格より下 (サポート)'}">${dir}</span>
                <span class="price" title="価格">${fmtPrice(l.price, dig)}</span>
                <span class="pip"   title="pip距離">${pipStr}</span>
                <span class="atr"   title="H4 ATR 倍">${atrStr}</span>
            </div>`;
        }).join('');

        // Header row (only meaningful when expanded — compact panel is too narrow)
        const headerHtml = isExpanded ? `
            <div class="struct-row struct-head" title="">
                <span class="cat" title="カテゴリ (Sup/Res/Swing/Rnd/Ses/VWAP/...)">CAT</span>
                <span class="name">NAME</span>
                <span class="src" title="検出した足 (M15/H1/H4/D1/EA)">TF</span>
                <span class="imp" title="重要度 (1-5 ★)">IMP</span>
                <span class="dir" title="R=現価より上 / S=現価より下">R/S</span>
                <span class="price">PRICE</span>
                <span class="pip" title="現在価格との pip 距離">PIPS</span>
                <span class="atr" title="距離 ÷ H4 ATR (≤0.1 で TOUCH 判定)">ATR×</span>
            </div>` : '';

        root.innerHTML = headerHtml + (dataHtml || '<div class="empty">no levels</div>');
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
    if (!a.positions || a.positions.length === 0) {
        posRoot.innerHTML = '<div class="empty">no open positions</div>';
    } else {
        posRoot.innerHTML = a.positions.map(p => {
            const d = priceDigits(p.price_open, p.symbol);
            const cls = p.type === 'BUY' ? 'buy' : 'sell';
            return `<div class="pos-row ${cls}">
                <span class="type-${cls}">${p.type}</span>
                <span>${p.symbol}</span>
                <span>${p.volume.toFixed(2)}L</span>
                <span>${fmtPrice(p.price_open, d)}→${fmtPrice(p.price_current, d)}</span>
                <span class="pos-pnl ${p.profit > 0 ? 'pos' : 'neg'}">${fmtSigned(p.profit, 2)}</span>
            </div>`;
        }).join('');
    }
}

function paintBias(strength, force) {
    if (!strength) return;
    const w = (strength.by_window || {})[UI.strengthWindow];
    if (!w) return;
    for (const sym of SYMBOL_ORDER) {
        const el = $bind('bias-' + sym);
        if (!el) continue;
        const b = (w.pair_biases || {})[sym];
        if (!b || b.label === 'NEUTRAL') {
            el.className = 'bias hidden';
            el.textContent = '';
            continue;
        }
        const sign = b.delta > 0 ? '+' : '';
        const txt = `${b.base} vs ${b.quote}: ${sign}${Number(b.delta).toFixed(0)} (${JP_BIAS[b.label] || b.label})`;
        let mod = 'buy';
        if (b.label === 'STRONG BUY')   mod = 'strong-buy';
        else if (b.label === 'SELL')    mod = 'sell';
        else if (b.label === 'STRONG SELL') mod = 'strong-sell';
        el.className = 'bias ' + mod;
        el.textContent = txt;
    }
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
    const nowJst = new Date();
    const todayKey = nowJst.toLocaleDateString('en-CA'); // YYYY-MM-DD JST
    const tomorrowKey = new Date(nowJst.getTime() + 86400000).toLocaleDateString('en-CA');
    root.innerHTML = events.map(e => {
        const cat = calendarCategory(e.title);
        const dateJst = new Date(e.release_ts * 1000).toLocaleDateString('en-CA');
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
              + `<span class="macro-rynum mute">失業率 ${esc(ur)}</span></div>`;
    }
    root.innerHTML = keyBlock + rows;
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

/** Build the ANALYTICS section — Trade Plan + Conviction Breakdown + Risk Warnings.
 *  These turn raw indicators into actionable analyst output: where to enter,
 *  stop, target, and what's contributing positively/negatively to the bias. */
function buildAnalytics(snap, sym) {
    const sa = snap.analysis && snap.analysis.by_symbol && snap.analysis.by_symbol[sym];
    if (!sa || !sa.by_tf) return '';
    const byTf = sa.by_tf;
    const t = (snap.price && snap.price.ticks) ? snap.price.ticks[sym] : null;
    const price = t && t.bid;
    const ask   = t && t.ask;
    const h4 = byTf.H4;
    const atr = h4 && h4.atr;
    const comp = compositeSignal(byTf);
    const sections = [];

    // Trade Plan removed per user direction — SL/TP suggestions were mechanical
    // ATR-based arithmetic and not the analyst value-add.

    // ───── Conviction Breakdown ─────
    // Decompose the composite per-TF so the sum mathematically equals the
    // displayed score. Formula: composite = Σ(tfSig.code × weight) / (2×Σweight) × 10
    // ⇒ each TF's contribution = (tfSig.code × weight) / (2×Σweight) × 10
    const totalWeight = TF_LABELS.reduce(
        (s, l) => s + (byTf[l] && byTf[l].rsi != null && byTf[l].adx != null ? TF_WEIGHTS[l] : 0),
        0
    );
    const factors = TF_LABELS.map(tf => {
        const tx = byTf[tf];
        if (!tx || tx.rsi == null || tx.adx == null) {
            return { name: tf, score: 0, cls: 'na', hasData: false };
        }
        const sig = tfSignal(tx);
        const weight = TF_WEIGHTS[tf];
        const tfac = tfTrendFactor(tx);                  // regime gate 0..1
        const contrib = totalWeight > 0
            ? (sig.code * weight * tfac) / (2 * totalWeight) * 10
            : 0;
        return {
            name: tf, score: contrib, cls: sig.cls, hasData: true,
            label: sig.label, weight, tfac,
            adx: tx.adx, adxState: tx.adx >= 25 ? 'トレンド' : 'レンジ',
            rsi: tx.rsi,
        };
    });

    // Fixed-column table (header + one row per TF) so every field — 寄与 /
    // シグナル / 重み / ADX / RSI — lines up vertically across the four rows.
    const bdHead = `<div class="anlx-bd-row anlx-bd-head">`
        + `<span>TF</span><span>寄与</span><span>シグナル</span>`
        + `<span>重み</span><span>ADX</span><span>RSI</span></div>`;
    const breakdownHtml = bdHead + factors.map(f => {
        const sign = f.score >= 0 ? '+' : '';
        if (!f.hasData) {
            return `<div class="anlx-bd-row na">
                <span class="bd-tf">${f.name}</span><span class="bd-score">--</span>
                <span class="bd-sig">データなし</span><span class="bd-dim">--</span>
                <span class="bd-dim">--</span><span class="bd-dim">--</span>
            </div>`;
        }
        return `<div class="anlx-bd-row ${f.cls}">
            <span class="bd-tf">${f.name}</span>
            <span class="bd-score">${sign}${f.score.toFixed(2)}</span>
            <span class="bd-sig">${f.label}</span>
            <span class="bd-dim">${f.weight}×</span>
            <span class="bd-dim">${f.adx.toFixed(0)} ${f.adxState}`
            + `${f.tfac < 1 ? ' <span class="bd-gate">×' + f.tfac.toFixed(1) + '</span>' : ''}</span>
            <span class="bd-dim">${f.rsi.toFixed(0)}</span>
        </div>`;
    }).join('');

    // Sum verification (will exactly equal composite due to per-TF decomposition)
    const sumCheck = factors.reduce((s, f) => s + f.score, 0);
    sections.push(`
        <div class="anlx-block">
            <div class="anlx-title">確信度の内訳
                <span class="anlx-sub">(各TF寄与の合計 = ${sumCheck >= 0 ? '+' : ''}${sumCheck.toFixed(2)} = 複合スコア ${comp.score >= 0 ? '+' : ''}${comp.score.toFixed(2)}/10)</span>
            </div>
            <div class="anlx-breakdown">${breakdownHtml}</div>
        </div>`);

    // ───── Risk Warnings ─────
    const warns = [];
    for (const tf of TF_LABELS) {
        const tx = byTf[tf];
        if (!tx) continue;
        if (tx.rsi != null && tx.rsi >= 75) warns.push(`${tf} RSI ${tx.rsi.toFixed(1)} — 極端な過熱 (反転下落リスク)`);
        if (tx.rsi != null && tx.rsi <= 25) warns.push(`${tf} RSI ${tx.rsi.toFixed(1)} — 極端な売られすぎ (反転上昇リスク)`);
    }
    if (ask != null && price != null) {
        const sp = (ask - price) * pipMultiplierFor(sym);
        if (sp > 5) warns.push(`スプレッド ${sp.toFixed(1)} pip — 取引コスト高 (流動性低 or イベント直前)`);
    }
    if (Math.abs(comp.score) < 2) warns.push(`複合 BIAS ±${Math.abs(comp.score).toFixed(1)} — 確信度不足 (見送り推奨)`);
    if (warns.length) {
        sections.push(`
            <div class="anlx-block warn">
                <div class="anlx-title warn">⚠ リスク警告</div>
                ${warns.map(w => `<div class="anlx-warn">${w}</div>`).join('')}
            </div>`);
    }

    return sections.join('');
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
        for (const tfLabel of TF_LABELS) {
            const row = $bind(`sig-${tfLabel}-${sym}`);
            if (!row) continue;
            const tf = byTf && byTf[tfLabel];
            if (!tf) {
                row.innerHTML = `<span class="tf">${tfLabel}</span>
                    <span class="trend na">--</span>
                    <span class="num">--</span>
                    <span class="num">--</span>
                    <span class="num">--</span>`;
                row.className = 'sig-row na';
                continue;
            }
            const sig = tfSignal(tf);
            const rsi = tf.rsi != null ? tf.rsi.toFixed(1) : '--';
            const adx = tf.adx != null ? tf.adx.toFixed(1) : '--';
            const adxBadge = (tf.adx != null && tf.adx >= 25) ? ' strong' : '';
            const dist = pctEmaDist(tf.last_close, tf.ema);
            const distStr = dist == null ? '--' : (dist > 0 ? '+' : '') + dist.toFixed(2) + '%';
            const emaCls = dist == null ? 'num' : (dist > 0 ? 'num pos' : 'num neg');
            // DI± spread (DI+ minus DI-) — sign shows dominant side
            const di = (tf.di_plus != null && tf.di_minus != null)
                ? tf.di_plus - tf.di_minus : null;
            const diStr = di == null ? '--' : (di > 0 ? '+' : '') + di.toFixed(1);
            const diCls = di == null ? 'num'
                        : Math.abs(di) >= 5 ? (di > 0 ? 'num pos' : 'num neg')
                        : 'num';
            // Indicator-state coloring: extreme RSI as warning.
            const rsiCls = tf.rsi == null ? 'num'
                         : tf.rsi >= 70 ? 'num warn'
                         : tf.rsi <= 30 ? 'num warn'
                         : 'num';
            row.innerHTML = `
                <span class="tf">${tfLabel}</span>
                <span class="trend ${sig.cls}">${sig.label}</span>
                <span class="${emaCls}">${distStr}</span>
                <span class="num${adxBadge}">${adx}</span>
                <span class="${diCls}">${diStr}</span>
                <span class="${rsiCls}">${rsi}</span>`;
            row.className = `sig-row ${sig.cls}`;
        }
        // Composite badge
        const comp = compositeSignal(byTf);
        const wrap = $bind(`composite-${sym}`);
        const arrow = $bind(`comp-arrow-${sym}`);
        const text  = $bind(`comp-text-${sym}`);
        const score = $bind(`comp-score-${sym}`);
        const fill  = $bind(`comp-fill-${sym}`);
        if (wrap)  wrap.className  = 'composite ' + comp.cls;
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
        // Analytics section (Conviction Breakdown + Warnings)
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

function paintCorrelationList(corr, force) {
    if (!corr) return;
    if (!force && !changed('corr-list', corr.generated_at + ':' + UI.corrWindow)) return;
    const root = $bind('corr-list');
    if (!root) return;
    const w = (corr.by_window || {})[String(UI.corrWindow)];
    if (!w || !w.matrix || !w.symbols || w.symbols.length === 0) {
        root.innerHTML = '<div class="empty mute">waiting for data...</div>';
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
                <span class="pair"><b>${p.a}</b> ${arrow} <b>${p.b}</b></span>
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
                    <span class="pair"><b>${p.a}</b> ⚠ <b>${p.b}</b></span>
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
            // Force a redraw despite the paintDws memoization gate.
            delete STAMPS['dws'];
            if (latestSnap) paintDws(latestSnap);
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
    const dt = new Date(ms);
    if (isNaN(dt.getTime())) return '';
    const p = n => String(n).padStart(2, '0');
    const md = `${p(dt.getMonth() + 1)}/${p(dt.getDate())}`;
    const hm = `${p(dt.getHours())}:${p(dt.getMinutes())}`;
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

/** Update the state line above the canvas (current alignment + latest trigger). */
function updateDwsState(el, win) {
    if (!el) return;
    const N = win.c.length;
    const last = win.c[N - 1];
    const allUp = last.every(c => c === 0);
    const allDown = last.every(c => c === 1);
    let txt, cls;
    if (allUp)        { txt = '▲ 3TF 全green 揃い'; cls = 'dws-state buy'; }
    else if (allDown) { txt = '▼ 3TF 全red 揃い';   cls = 'dws-state sell'; }
    else              { txt = '— 不一致 (待機)';     cls = 'dws-state'; }
    for (let j = N - 1; j >= 0; j--) {
        if (win.g[j]) {
            txt += `  ·  最新 ${win.g[j]} (${N - 1 - j}本前)`;
            break;
        }
    }
    el.className = cls;
    el.textContent = txt;
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
    const expVal = Math.round(base.expectancy);
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

    const verdictDescHtml = ps ? `<div class="dws-vverdict-desc">
        <b>期間ドリフト検定</b>:
        <em>2010-2017</em> (N=${fmtN(ps.early.n)} WR ${(ps.early.win_rate*100).toFixed(2)}%)
        vs <em>2018-2025</em> (N=${fmtN(ps.late.n)} WR ${(ps.late.win_rate*100).toFixed(2)}%) を
        <b>2-proportion z-test</b> で比較。drift <em>${ps.drift_wr_pp>=0?'+':''}${ps.drift_wr_pp.toFixed(2)}pp</em>,
        p=<em>${ps.p_wr_raw.toExponential(2)}</em>,
        <b>Bonferroni α/3=0.0167</b> ${ps.p_wr_bonferroni_significant?'<span class="dws-sig">クリア (有意)</span>':'未クリア (非有意)'}。
        Verdict <b>${ps.verdict}</b>: ${ps.verdict==='STABLE'?'両期間で統計的に差なし':
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
        ev: `1 トレードあたりの平均純損益 (<em>points 単位、スプレッドコスト込み</em>)。<b>期待値 × 取引回数 ≈ 累積損益</b>。`
          + `これがプラスである限り、長期的にトレードを続ければ利益が積み上がる`,
        be: `現状の <em>(平均勝ち額 / 平均負け額)</em> 比率で損益をゼロにするのに必要な勝率。<b>実勝率がこれを上回れば長期プラス</b>。`
          + `現状実勝率 <em>${(base.win_rate*100).toFixed(2)}%</em> は Breakeven <em>${(base.breakeven_wr*100).toFixed(1)}%</em> を `
          + `<b>+${((base.win_rate-base.breakeven_wr)*100).toFixed(1)}pp 上回る</b>`,
        dd: `16 年累積損益曲線のピークから谷までの最大下落幅 (<em>points</em>)。<b>過去最悪のドローダウン</b>。`
          + `期待値 <em>${fmtN(Math.round(base.expectancy))} pt × ${Math.ceil(base.max_drawdown / Math.max(1, base.expectancy))} 回</em> のトレードで回復計算`,
    };

    const statsHtml =
        `<div class="dws-vstats">`
      + cell('勝率',         pct(base.win_rate),            '',                       DESC.wr)
      + cell('Wilson 95%CI', esc(wilsonCi),                 '',                       DESC.wilson)
      + (bootCi ? cell('Bootstrap 95%CI', esc(bootCi),      '',                       DESC.boot) : '')
      + cell('PF',           esc(fmtPf(base.profit_factor)), '',                      DESC.pf)
      + cell('期待値',       `${expVal >= 0 ? '+' : ''}${fmtN(expVal)} pt`,
             'dws-num ' + expCls,                                                     DESC.ev)
      + cell('Breakeven WR', esc(breakeven),                '',                       DESC.be)
      + cell('MaxDD',        `${fmtN(Math.round(base.max_drawdown))} pt`,
             '',                                                                      DESC.dd)
      + `</div>`;

    // ---- Secondary: rolling ~7M live validation ----
    const secondaryHtml = _buildSecondaryRolling(sym, snap, base);

    // Sparkline canvas placeholder.
    const sparkHtml = histArr.length >= 2
        ? `<canvas class="dws-vspark" data-bind="dws-vspark-${sym}" width="160" height="22"></canvas>`
        : '';

    el.innerHTML = headerHtml + statsHtml + secondaryHtml + sparkHtml;

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
    if (!ps) return '';
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
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
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
    const tk = snap.price && snap.price.ticks && snap.price.ticks[sym];
    const costPts = (tk && tk.ask != null && tk.bid != null)
        ? (tk.ask - tk.bid) * ptMult : 0;        // round-trip cost ≈ spread
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
            const pts = Math.round(tr.p * ptMult - costPts);   // net of cost
            ctx.fillStyle = pts > 0 ? '#00d09c' : pts < 0 ? '#ff5b6b' : '#8089a0';
            ctx.font = '700 9px monospace';
            ctx.textAlign = 'center'; ctx.textBaseline = 'top';
            ctx.fillText((pts >= 0 ? '+' : '') + pts.toLocaleString('en-US'),
                         cx, markY + 19);
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
    paintPrices(latestSnap);
    paintSignals(latestSnap);
    paintAccount(latestSnap);
    paintStrength(latestSnap.strength);
    paintCorrelationList(latestSnap.correlation);
    paintConcentration(latestSnap);
    paintCalendar(latestSnap);
    paintMacro(latestSnap);
    paintDws(latestSnap);
}

// 1-second clock + countdown tick (independent of WS)
function startTickers() {
    setInterval(() => {
        if (latestSnap) {
            $bind('clock').textContent = fmtJSTclock(Date.now() / 1000);
        }
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
    connect();
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
