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
// XAUUSD-specialised dashboard: gold only (matches backend config.SYMBOLS).
const SYMBOL_ORDER = ["XAUUSD"];

// Per-domain version stamp cache so callbacks can skip no-op renders.
const STAMPS = {};
function changed(key, stamp) {
    if (stamp == null) return false;
    if (STAMPS[key] === stamp) return false;
    STAMPS[key] = stamp;
    return true;
}

// Clientside-only selected state (bottom fund/trade-management panel tab).
const UI = {
    fundTab:        (() => { try { return localStorage.getItem('mt5-fundtab') || 'positions'; }
                             catch (_e) { return 'positions'; } })(),
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
// Money-management math helpers (XAUUSD-specific, no order logic)
// ------------------------------------------------------------

// XAUUSD: pip = 0.10 price units. 1 lot = 100 oz -> pip value = 100 * 0.10 = $10 / lot / pip.
function pipValueUsd(lots) { return 100 * 0.10 * lots; }
function positionSizeLots(riskAmount, slPips, pipValPerLot) {
    if (slPips <= 0 || pipValPerLot <= 0) return null;
    return riskAmount / (slPips * pipValPerLot);
}
function rrRatio(entry, sl, tp) {
    const risk = Math.abs(entry - sl), reward = Math.abs(tp - entry);
    if (risk <= 0) return null;
    return reward / risk;
}
function expectancyR(winRate, rr) { return winRate * rr - (1 - winRate); }   // assumes 1R loss
function breakevenWR(rr) { return rr > 0 ? 1 / (1 + rr) : null; }
function kellyFraction(winRate, rr) {                  // f* = W - (1-W)/RR
    if (rr <= 0) return null;
    return winRate - (1 - winRate) / rr;
}
function riskOfRuin(winRate, rr, units) {
    const edge = expectancyR(winRate, rr);
    if (edge <= 0) return 1;
    const a = (1 - edge) / (1 + edge);
    return Math.max(0, Math.min(1, Math.pow(a, units)));
}
function liquidationPips(equity, marginUsed, lots) {   // pips of adverse move to 100% margin
    const buffer = equity - marginUsed;
    const perPip = pipValueUsd(lots);
    return perPip > 0 ? buffer / perPip : null;
}

// ------------------------------------------------------------
// One-time DOM building
// ------------------------------------------------------------

function buildSymbolGrid() {
    const grid = $bind('symbols-grid');
    grid.innerHTML = '';
    // XAUUSD-specialised: with a single symbol the panel is permanently
    // expanded and fills the main area (no collapse). With more than one it
    // keeps the click-to-expand grid behaviour.
    const single = SYMBOL_ORDER.length === 1;
    for (const sym of SYMBOL_ORDER) {
        const panel = buildPanel(sym);
        if (single) {
            panel.classList.add('expanded');
            panel.classList.remove('quiet');
            const cb = panel.querySelector('.panel-close');
            if (cb) cb.remove();            // nothing to collapse back to
        } else {
            panel.addEventListener('click', (ev) => onPanelClick(ev, panel, grid));
        }
        grid.appendChild(panel);
    }
    if (single) grid.classList.add('has-expanded');
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
        <div class="emastack" data-bind="emastack-${sym}"><div class="empty mute">EMA 読み込み中…</div></div>
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
        // Re-render so the signal panels repaint for the new mode.
        if (latestSnap) { delete STAMPS['sig']; paintAll(); }
        return;
    }
    // Clear any other expansion + toggle this one
    grid.querySelectorAll('.panel.expanded').forEach(p => p.classList.remove('expanded'));
    grid.classList.toggle('has-expanded', !wasExpanded);
    if (!wasExpanded) panel.classList.add('expanded');
    if (latestSnap) { delete STAMPS['sig']; paintAll(); }
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
    if (latestSnap) { delete STAMPS['sig']; paintAll(); }
});

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
        scored.push({ sym, c });
    }
    // Strongest |score| first.
    scored.sort((a, b) => Math.abs(b.c.score) - Math.abs(a.c.score));
    fireSetupAlerts(scored);                  // notify the moment a NEW setup appears
    if (scored.length === 0) {
        root.innerHTML = '<span class="active-empty">no high-conviction signals</span>';
        return;
    }
    root.innerHTML = scored.slice(0, 8).map(({ sym, c }) => {
        const scoreStr = (c.score > 0 ? '+' : '') + c.score.toFixed(1);
        return `<span class="active-chip ${c.cls}">
            <span class="ac-sym">${sym}</span>
            <span class="ac-side">${c.arrow} ${c.label}</span>
            <span class="ac-score">${scoreStr}</span>
        </span>`;
    }).join('');
}

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

/** Detect newly-appeared high-conviction setups and notify (deduped by sym|cls). */
function fireSetupAlerts(scored) {
    const active = scored;
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


// ------------------------------------------------------------
// Discretionary order panel — confirm-then-send (no auto trading)
// ------------------------------------------------------------

/** Enable/disable trade buttons by the account's trade permission and prefill
 *  the lot field with the recommended lot (unless the user is editing it). */
function applyTradeGating(acc) {
    const ok = !!(acc && acc.trade_allowed);
    // Gate: only touch the DOM when permission or recommended lot changes
    // (this runs at ~2 Hz; nothing here changes per tick otherwise).
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
        const src = _calSrcUrl(e.source_url);
        const srcLink = src
            ? ` <a class="cal-src" href="${esc(src)}" target="_blank" rel="noopener noreferrer" title="ソース元を開く">↗</a>`
            : '';
        return `<div class="cal-row${day}" data-ccy="${esc(e.currency)}">
            <span class="cal-date">${esc(fmtJSTdate(e.release_ts))}</span>
            <span class="time">${esc(fmtJSTclockNoSec(e.release_ts))}</span>
            <span class="ccy">${esc(e.currency)}</span>
            <span class="cat ${cat.cls}">${cat.label}</span>
            <span class="title" title="${esc(e.title)}">${esc(e.title)}${srcLink}</span>
        </div>`;
    }).join('');
}

// Calendar source links come from an external feed, so only render a link when
// it is https AND points at a known source host — never let the feed inject an
// arbitrary (or javascript:) URL into the DOM.
const CAL_SRC_HOSTS = new Set([
    'forexfactory.com', 'www.forexfactory.com',
    'federalreserve.gov', 'www.federalreserve.gov',
    'bls.gov', 'www.bls.gov',
    'ecb.europa.eu', 'www.ecb.europa.eu',                 // ECB
    'bankofengland.co.uk', 'www.bankofengland.co.uk',     // BoE
    'boj.or.jp', 'www.boj.or.jp',                         // BoJ
    'rba.gov.au', 'www.rba.gov.au',                       // RBA
]);
function _calSrcUrl(url) {
    if (typeof url !== 'string' || !/^https:\/\//i.test(url)) return '';
    try {
        return CAL_SRC_HOSTS.has(new URL(url).hostname.toLowerCase()) ? url : '';
    } catch (e) { return ''; }
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

/** Paint the DXY (US Dollar Index) context panel. Gold is inverse-USD, so a
 *  rising dollar is a HEADWIND for gold and a falling dollar a TAILWIND — the
 *  panel leads with that gold-impact read, then the level / change / trend.
 *  A small SVG sparkline shows the recent dollar path. */
// EMA oscillator zoom: visible-bar window (TradingView-style wheel zoom).
const EMA_VIEW_MIN = 30;          // fewest visible bars (max zoom-in)
const EMA_VIEW_MAX = 2000;        // most bars drawn at once (zoom-out cap; pan covers the rest)
const EMA_VIEW_DEFAULT = 160;     // default visible bars
const EMA_W = 1000, EMA_H = 300, EMA_PAD = 6;   // SVG viewBox geometry
const EMA_HISTORY_POLL_MS = 120000;             // re-fetch deep history every 2 min

// Deep EMA history (~10 months M15) from /api/ema_history — preferred over the
// small live WS window so the oscillator can drag months into the past without
// bloating the WS snapshot. Polled; the WS block is the instant initial paint.
let EMA_HISTORY = null;
function fetchEmaHistory() {
    fetch('/api/ema_history')
        .then(r => (r.ok ? r.json() : null))
        .then(d => { if (d && d.t && d.t.length > 1) { EMA_HISTORY = d; paintEmaStack(latestSnap); } })
        .catch(() => {});
}

function _jstDayKey(ms) {
    const d = new Date(ms + 9 * 3600 * 1000);   // JST calendar day key
    return d.getUTCFullYear() * 10000 + (d.getUTCMonth() + 1) * 100 + d.getUTCDate();
}

/** EMA-stack oscillator (center panel): three EMAs on the M15 series shown as
 *  %-deviation from the EMA320 centerline (price / EMA20 / EMA80 oscillating
 *  around a flat 0 = EMA320). Above center = uptrend, below = downtrend — the
 *  read is the user's; no trigger / arrow is drawn. Repaint-free upstream.
 *  Wheel = zoom (visible-bar window); daily JST boundaries are marked. */
function paintEmaStack(snap) {
    const el = $bind('emastack-XAUUSD');
    if (!el) return;
    // Persistent shell built ONCE: the crosshair + chip live OUTSIDE the
    // re-rendered content so a content redraw never wipes them (no flicker).
    if (!el._emaInit) {
        el._emaInit = true;
        el.innerHTML = `<div class="ema-content"></div>`
                     + `<div class="ema-cursor" hidden></div><div class="ema-hover" hidden></div>`;
        el.addEventListener('mousemove', ev => (el._drag ? _emaDrag(el, ev) : _emaHover(el, ev)));
        el.addEventListener('mouseleave', () => {
            const c = el.querySelector('.ema-cursor'), h = el.querySelector('.ema-hover');
            if (c) c.hidden = true;
            if (h) h.hidden = true;
        });
        el.addEventListener('wheel', ev => _emaWheel(el, ev), { passive: false });
        el.addEventListener('mousedown', ev => _emaDragStart(el, ev));
        // End the drag even if the mouse is released outside the panel.
        window.addEventListener('mouseup', () => {
            if (el._drag) { el._drag = null; el.classList.remove('ema-dragging'); }
        });
        // "直近" button snaps the view back to the latest bar (realtime).
        el.addEventListener('click', ev => {
            if (ev.target.closest('.ema-latest') && el._view) {
                el._view.off = 0;
                _emaRender(el);
            }
        });
    }
    const content = el.querySelector('.ema-content');
    // Prefer the deep /api/ema_history series; fall back to the live WS window
    // until the first history fetch returns.
    const d = EMA_HISTORY || (snap && snap.ema_stack);
    if (!d || d.stale || !d.t || d.t.length < 2) {
        content.innerHTML = `<div class="empty mute">${d && d.symbol ? 'EMA データ取得待ち' : 'EMA320 用の履歴待ち'}</div>`;
        el._ema = null;
        el._emaStamp = null;
        return;
    }
    const n = d.dev_price.length;
    // Full series stashed; the view window (count + offset-from-right) is what's
    // drawn. off=0 keeps the right edge pinned to the newest bar (auto-follow).
    el._ema = { t: d.t, dp: d.dev_price, df: d.dev_fast, dm: d.dev_mid, n,
                price: d.price, ema_fast: d.ema_fast, ema_mid: d.ema_mid,
                ema_center: d.ema_center, bands: d.bands || null };
    if (!el._view) el._view = { count: Math.min(EMA_VIEW_DEFAULT, n), off: 0 };
    el._view.count = Math.min(el._view.count, n);
    // Re-render the SVG only when a new confirmed bar arrives (data is fixed
    // between bar closes). Wheel zoom calls _emaRender directly.
    const stamp = d.t[n - 1] + ':' + n;
    if (el._emaStamp === stamp) return;
    el._emaStamp = stamp;
    _emaRender(el);
}

// (1) EMA-disparity overextension tier from the 16Y bands. Shared by the
// readout (_emaRender) and the hover crosshair (_emaHover). side picked by
// sign; absent band => '' (feature degrades to no colour).
function emaOxTier(val, band) {
    if (val == null || !band) return '';
    const side = val >= 0 ? band.pos : band.neg;
    if (!side) return '';
    const a = Math.abs(val);
    if (side.p99 && a >= side.p99) return ' ema-overext-x';
    if (side.p95 && a >= side.p95) return ' ema-overext';
    return '';
}

/** Render the SVG + readout for the current view window. */
function _emaRender(el) {
    const data = el._ema, content = el.querySelector('.ema-content');
    if (!data || !content) return;
    const n = data.n, v = el._view;
    const i1 = Math.max(0, n - 1 - v.off);
    const i0 = Math.max(0, i1 - v.count + 1);
    const cnt = i1 - i0 + 1;
    const dp = data.dp, df = data.df, dm = data.dm, t = data.t;
    let m = 0;
    for (let i = i0; i <= i1; i++) m = Math.max(m, Math.abs(dp[i]), Math.abs(df[i]), Math.abs(dm[i]));
    m = (m || 1) * 1.08;
    const W = EMA_W, H = EMA_H, pad = EMA_PAD;
    const X = k => pad + (cnt > 1 ? k / (cnt - 1) : 0) * (W - 2 * pad);    // k = 0..cnt-1
    const Y = val => pad + (1 - (val + m) / (2 * m)) * (H - 2 * pad);      // 0 at center
    const path = (arr) => {
        let s = '';
        for (let k = 0; k < cnt; k++) s += (k ? ' L' : 'M') + X(k).toFixed(1) + ',' + Y(arr[i0 + k]).toFixed(1);
        return s;
    };
    const line = (arr, col, w) => `<path d="${path(arr)}" fill="none" stroke="${col}" stroke-width="${w}"/>`;
    const y0 = Y(0).toFixed(1);
    // Daily JST boundary lines (period dividers).
    let dividers = '', prevDay = null;
    for (let k = 0; k < cnt; k++) {
        const day = _jstDayKey(t[i0 + k]);
        if (prevDay !== null && day !== prevDay) {
            const x = X(k).toFixed(1);
            dividers += `<line x1="${x}" y1="0" x2="${x}" y2="${H}" class="ema-day" vector-effect="non-scaling-stroke"/>`;
        }
        prevDay = day;
    }
    // Colour the gap BETWEEN the centerline (EMA320 = zero) and the EMA20 line:
    // the area between the EMA20 curve and the zero line. A vertical gradient
    // switches hard at the centerline so the gap is blue where EMA20 is ABOVE
    // EMA320 and red where BELOW — the colour is the EMA20-vs-EMA320 spread.
    // Two solid bands split at the centerline (NO fade): the whole gap between
    // EMA320 (zero) and EMA20 reads as one clear colour — blue above, red below.
    const zeroFrac = (parseFloat(y0) / H).toFixed(4);
    const grad =
        `<defs><linearGradient id="emaGrad" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${H}">`
      + `<stop offset="0" stop-color="#4d8eff" stop-opacity="0.30"/>`
      + `<stop offset="${zeroFrac}" stop-color="#4d8eff" stop-opacity="0.30"/>`
      + `<stop offset="${zeroFrac}" stop-color="#ff5b6b" stop-opacity="0.30"/>`
      + `<stop offset="1" stop-color="#ff5b6b" stop-opacity="0.30"/>`
      + `</linearGradient></defs>`;
    // Fill the gap to the centerline for BOTH EMA80 and EMA20. Drawn EMA80 first
    // (usually the wider gap) then EMA20 on top — where both sit the same side of
    // EMA320 the overlap reads a touch deeper.
    const areaOf = arr => `<path d="${path(arr)} L${X(cnt - 1).toFixed(1)},${y0} `
                        + `L${X(0).toFixed(1)},${y0} Z" fill="url(#emaGrad)" stroke="none"/>`;
    const svg =
        `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="ema-osc" role="img">`
      + grad
      + areaOf(dm)            // centerline ↔ EMA80
      + areaOf(df)            // centerline ↔ EMA20
      + dividers
      + `<line x1="0" y1="${y0}" x2="${W}" y2="${y0}" class="ema-center" vector-effect="non-scaling-stroke"/>`
      + line(dm, '#4d8eff', 1.6)            // EMA80 (~1H)
      + line(df, '#ffb74d', 1.6)            // EMA20 (M15)
      + line(dp, '#e8edf5', 1.2)            // price (line stays; only the readout value was removed)
      + `</svg>`;
    // 乖離率 (disparity ratio) = (price − EMA) / EMA × 100, shown for every EMA
    // including EMA320. Price itself is not displayed.
    const dr = (e) => (data.price == null || !e) ? null : (data.price - e) / e * 100;
    const sp = (v) => (v == null ? '--' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%');
    const d320 = dr(data.ema_center);
    const upCls = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg');
    // (1) overextension tier from the 16Y bands: |乖離率| >= p99 blinks, >= p95
    // warns. side picked by sign; absent bands => no class (feature degrades).
    const B = data.bands || null;
    const spO = (val, key) =>
        `<span class="ema-val${emaOxTier(val, B && B[key])}">${sp(val)}</span>`;
    const read =
        `<div class="ema-read">`
      + `<span class="ema-side ${upCls(d320)}">乖離率</span>`
      + `<span class="ema-k"><i class="ema-dot" style="background:#ffb74d"></i>EMA20 ${spO(dr(data.ema_fast), 'ema20')}</span>`
      + `<span class="ema-k"><i class="ema-dot" style="background:#4d8eff"></i>EMA80 ${spO(dr(data.ema_mid), 'ema80')}</span>`
      + `<span class="ema-k"><i class="ema-dot ema-dot-center"></i>EMA320 ${spO(d320, 'ema320')}</span>`
      + `<span class="ema-k mute">${cnt}本 (ホイール拡縮/ドラッグで遡る)</span>`
      + `<button type="button" class="ema-latest${v.off > 0 ? '' : ' at-latest'}">▶ 直近</button>`
      + `</div>`;
    content.innerHTML = read + svg;
    el._geom = { i0, i1, cnt, W, pad };       // for hover index mapping
}

/** Wheel = zoom the visible-bar window, anchored at the bar under the cursor. */
function _emaWheel(el, ev) {
    const data = el._ema, g = el._geom;
    if (!data || !g) return;
    ev.preventDefault();
    const svg = el.querySelector('.ema-content .ema-osc');
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    // absolute index of the bar currently under the cursor (the zoom anchor)
    let vi = Math.round((frac * g.W - g.pad) / (g.W - 2 * g.pad) * (g.cnt - 1));
    vi = Math.max(0, Math.min(g.cnt - 1, vi));
    const aiCursor = g.i0 + vi;
    const v = el._view;
    const factor = ev.deltaY < 0 ? (1 / 1.2) : 1.2;        // wheel up = zoom in
    let count = Math.round(v.count * factor);
    count = Math.max(EMA_VIEW_MIN, Math.min(Math.min(EMA_VIEW_MAX, data.n), count));
    // keep the anchor bar at the same fractional x
    let i0 = Math.round(aiCursor - frac * (count - 1));
    i0 = Math.max(0, Math.min(data.n - count, i0));
    v.count = count;
    v.off = Math.max(0, (data.n - 1) - (i0 + count - 1));
    _emaRender(el);
    _emaHover(el, ev);                                      // refresh crosshair/chip in place
}

/** Begin a horizontal pan-drag (only when the press lands on the chart). */
function _emaDragStart(el, ev) {
    if (!el._ema || !el._geom) return;
    if (!ev.target.closest('.ema-osc')) return;        // ignore presses on the readout/button
    const svg = el.querySelector('.ema-content .ema-osc');
    if (!svg) return;
    el._drag = { x0: ev.clientX, off0: el._view.off, w: svg.getBoundingClientRect().width,
                 cnt: el._geom.cnt };
    el.classList.add('ema-dragging');
    const cur = el.querySelector('.ema-cursor'), hov = el.querySelector('.ema-hover');
    if (cur) cur.hidden = true;
    if (hov) hov.hidden = true;
    ev.preventDefault();
}

/** Pan the view window: dragging RIGHT reveals older bars (scroll into the past). */
function _emaDrag(el, ev) {
    const d = el._drag, data = el._ema;
    if (!d || !data || !d.w) return;
    const deltaBars = Math.round((ev.clientX - d.x0) * d.cnt / d.w);
    let off = d.off0 + deltaBars;                       // drag right (+dx) -> +off -> past
    off = Math.max(0, Math.min(data.n - el._view.count, off));
    if (off !== el._view.off) { el._view.off = off; _emaRender(el); }
}

/** Crosshair for the EMA oscillator: a vertical line UNDER the cursor and a chip
 *  centered on it showing the nearest VISIBLE bar's JST time (+ each line's
 *  %-deviation at that bar). Pure read-out, no signal. */
function _emaHover(el, ev) {
    const data = el._ema, g = el._geom;
    const content = el.querySelector('.ema-content');
    const svg = content && content.querySelector('.ema-osc');
    const cur = el.querySelector('.ema-cursor');
    const hov = el.querySelector('.ema-hover');
    if (!data || !g || !svg) return;
    const r = svg.getBoundingClientRect();
    const elR = el.getBoundingClientRect();
    if (!r.width || ev.clientX < r.left || ev.clientX > r.right) {
        if (cur) cur.hidden = true;
        if (hov) hov.hidden = true;
        return;
    }
    // The whole UI is scaled via `transform: scale()` (applyDisplayFit), so
    // getBoundingClientRect/clientX are REAL px while absolutely-positioned
    // children live in unscaled local px — convert by this factor.
    const scale = (el.offsetWidth && r.width) ? (elR.width / el.offsetWidth) : 1;
    const frac = (ev.clientX - r.left) / r.width;          // ratio — scale-invariant
    let vi = Math.round((frac * g.W - g.pad) / (g.W - 2 * g.pad) * (g.cnt - 1));
    vi = Math.max(0, Math.min(g.cnt - 1, vi));
    const ai = g.i0 + vi;                                   // absolute index into the series
    const localX = (ev.clientX - elR.left) / scale;
    const top = (r.top - elR.top) / scale;
    const height = r.height / scale;
    if (cur) {
        cur.hidden = false;
        cur.style.left = localX.toFixed(1) + 'px';
        cur.style.top = top.toFixed(1) + 'px';
        cur.style.height = height.toFixed(1) + 'px';
    }
    if (hov) {
        const sec = data.t[ai] / 1000;
        const sgn = val => (val >= 0 ? '+' : '') + val.toFixed(2);
        hov.hidden = false;
        // Same metric as the readout: each EMA's 乖離率 (price vs that EMA) at the
        // hovered bar, derived from the per-bar deviations-from-EMA320:
        //   (price−EMA)/EMA = (1+devPrice/100)/(1+devEMA/100) − 1.  EMA320乖離率
        //   is just devPrice (price vs EMA320).
        const kairi = (dP, dE) => { const den = 1 + dE / 100; return den ? ((1 + dP / 100) / den - 1) * 100 : 0; };
        const B = data.bands || null;
        const k20 = kairi(data.dp[ai], data.df[ai]);
        const k80 = kairi(data.dp[ai], data.dm[ai]);
        const k320 = data.dp[ai];
        hov.innerHTML = `<b>${fmtJSTdate(sec)} ${fmtJSTclockNoSec(sec)}</b>`
            + `<span class="${emaOxTier(k20, B && B.ema20)}">EMA20 ${sgn(k20)}%</span>`
            + `<span class="${emaOxTier(k80, B && B.ema80)}">EMA80 ${sgn(k80)}%</span>`
            + `<span class="${emaOxTier(k320, B && B.ema320)}">EMA320 ${sgn(k320)}%</span>`;
        const hw = hov.offsetWidth || 200;
        const panelW = el.offsetWidth || (elR.width / scale);
        const hx = Math.max(0, Math.min(panelW - hw, localX - hw / 2));
        hov.style.left = hx.toFixed(1) + 'px';
        hov.style.top = (top + 4).toFixed(1) + 'px';
    }
}

function paintDxy(snap) {
    const el = $bind('dxy');
    const symEl = $bind('dxy-sym');
    const d = snap.dxy;
    if (!d || d.price == null || d.stale) {
        if (symEl) symEl.textContent = (d && d.symbol) ? esc(d.symbol) : '--';
        if (el) el.innerHTML = `<div class="empty mute">${d && d.symbol ? 'データ取得待ち' : 'DXY シンボル未取得'}</div>`;
        return;
    }
    if (symEl) symEl.textContent = esc(d.symbol || '--');
    const chg = d.change != null ? d.change : 0;
    const chgPct = d.change_pct != null ? d.change_pct : 0;
    const up = chg > 0, dn = chg < 0;
    // Gold impact (inverse): dollar up = headwind (red for gold), down = tailwind.
    const goldCls = up ? 'neg' : dn ? 'pos' : '';
    const goldTxt = up ? '金に逆風' : dn ? '金に追風' : '中立';
    const arrow = up ? '▲' : dn ? '▼' : '·';
    // Sparkline over the recent closes.
    const cs = (d.closes || []).filter(v => isFinite(v));
    let spark = '';
    if (cs.length >= 2) {
        const W = 240, H = 96, pad = 2;
        const lo = Math.min(...cs), hi = Math.max(...cs), span = (hi - lo) || 1;
        const xO = i => pad + (i / (cs.length - 1)) * (W - 2 * pad);
        const yO = v => pad + (1 - (v - lo) / span) * (H - 2 * pad);
        const path = 'M' + cs.map((v, i) => `${xO(i).toFixed(1)},${yO(v).toFixed(1)}`).join(' L');
        const col = up ? '#00d09c' : dn ? '#ff5b6b' : '#8a93a6';   // dollar's own direction
        spark = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="dxy-spark" role="img">`
              + `<path d="${path}" fill="none" stroke="${col}" stroke-width="1.4"/></svg>`;
    }
    const trendTxt = d.above_ema == null ? '' :
        (d.above_ema ? `EMA上(ドル堅調)` : `EMA下(ドル軟調)`);
    el.innerHTML =
        `<div class="dxy-top">`
      + `<span class="dxy-price">${d.price.toFixed(3)}</span>`
      + `<span class="dxy-chg ${up ? 'pos' : dn ? 'neg' : ''}">${chg >= 0 ? '+' : ''}${chg.toFixed(3)} (${chgPct >= 0 ? '+' : ''}${chgPct.toFixed(2)}%) ${arrow}</span>`
      + `</div>`
      + spark
      + `<div class="dxy-foot"><span class="dxy-gold ${goldCls}">${goldTxt}</span>`
      + `<span class="dxy-trend mute">${trendTxt}</span></div>`;
}

/** Paint the US 10Y real-yield panel — gold trades inverse to real yields, so a
 *  rising real yield is a HEADWIND (金に逆風) and a falling one a TAILWIND. Mirrors
 *  the DXY card exactly: level / daily change / sparkline of recent daily closes
 *  / gold-impact label / 5-day trend. Data = snap.real_yield (FRED DFII10,
 *  hourly auto-refresh). */
function paintRealYield(snap) {
    const el = $bind('realyield');
    const stEl = $bind('ry-status');
    const d = snap.real_yield;
    if (!d || d.value == null) {
        if (stEl) stEl.textContent = '--';
        if (el) el.innerHTML = `<div class="empty mute">読み込み中…</div>`;
        return;
    }
    // Header tag: "ライブ" when the value carries the intraday nominal-10Y move,
    // otherwise the official DFII10 daily date. (Addresses "what's the date?" —
    // the value is now real-time; the daily basis date shows in the footer.)
    if (stEl) stEl.innerHTML = d.stale ? '<span class="ry-stale">stale</span>'
                             : d.is_live ? '<span class="ry-live">● ライブ</span>'
                             : esc(d.as_of || '');
    const ch = d.change_1d;
    const up = ch != null && ch > 0, dn = ch != null && ch < 0;
    const arrow = up ? '▲' : dn ? '▼' : '·';
    const chTxt = ch == null ? '--' : (ch >= 0 ? '+' : '') + ch.toFixed(2);
    // Gold impact is the INVERSE of the yield move (separate from the change
    // colour, exactly like the DXY card): rising real yield → headwind (red).
    const gd = d.gold_dir;
    const goldCls = gd < 0 ? 'neg' : gd > 0 ? 'pos' : '';
    const goldTxt = gd < 0 ? '金に逆風' : gd > 0 ? '金に追風' : '中立';
    // Sparkline over the recent daily closes (same geometry as the DXY chart).
    const cs = (d.series || []).filter(v => isFinite(v));
    let spark = '';
    if (cs.length >= 2) {
        const W = 240, H = 96, pad = 2;
        const lo = Math.min(...cs), hi = Math.max(...cs), span = (hi - lo) || 1;
        const xO = i => pad + (i / (cs.length - 1)) * (W - 2 * pad);
        const yO = v => pad + (1 - (v - lo) / span) * (H - 2 * pad);
        const path = 'M' + cs.map((v, i) => `${xO(i).toFixed(1)},${yO(v).toFixed(1)}`).join(' L');
        const col = up ? '#00d09c' : dn ? '#ff5b6b' : '#8a93a6';   // yield's own direction
        spark = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="dxy-spark" role="img">`
              + `<path d="${path}" fill="none" stroke="${col}" stroke-width="1.4"/></svg>`;
    }
    const t5 = d.trend_5d;
    // Footer: 5-day daily trend + (when live) the live nominal 10Y driving the
    // intraday move + the DFII10 daily basis date.
    const parts = [];
    if (t5 != null) parts.push(`5日 ${t5 >= 0 ? '+' : ''}${t5.toFixed(2)}`);
    if (d.is_live && d.nominal_10y != null) parts.push(`名目 ${d.nominal_10y.toFixed(3)}%`);
    if (d.as_of) parts.push(`基準 ${esc(d.as_of.slice(5))}`);
    const trendTxt = parts.join(' · ');
    el.innerHTML =
        `<div class="dxy-top">`
      + `<span class="dxy-price">${d.value.toFixed(3)}%</span>`
      + `<span class="dxy-chg ${up ? 'pos' : dn ? 'neg' : ''}">前日比 ${chTxt} ${arrow}</span>`
      + `</div>`
      + spark
      + `<div class="dxy-foot"><span class="dxy-gold ${goldCls}">${goldTxt}</span>`
      + `<span class="dxy-trend mute">${trendTxt}</span></div>`;
}

/** Paint the CFTC COT (gold-futures positioning) panel.
 *  COT is a weekly *positioning / sentiment* gauge, NOT a directional price
 *  driver — so the panel reports facts: the large-speculator (fund) net long,
 *  its week-over-week change, where it sits in its 1-year range (a contrarian
 *  read — net longs at a 1-year high = a crowded book), the commercial/hedger
 *  net (the structural mirror), and a 52-week sparkline of the net. */
function paintCot(snap) {
    const el = $bind('cot');
    const dateEl = $bind('cot-date');
    const c = snap.cot;
    if (!c || c.net == null) {
        if (dateEl) dateEl.textContent = '--';
        if (el) el.innerHTML = `<div class="empty mute">${c && c.last_error ? 'データ取得待ち' : '読み込み中…'}</div>`;
        return;
    }
    const staleTag = c.stale ? ' <span class="cot-stale">stale</span>' : '';
    if (dateEl) dateEl.innerHTML = `${esc(c.report_date)} 週次${staleTag}`;

    const fmtN = n => (n == null ? '--' : Number(n).toLocaleString('en-US'));
    const fmtSigned = n => (n == null ? '--' : (n >= 0 ? '+' : '') + Number(n).toLocaleString('en-US'));

    const netLong = c.net > 0;
    const dirTxt = netLong ? 'ネットロング' : (c.net < 0 ? 'ネットショート' : 'ニュートラル');
    const dirCls = netLong ? 'pos' : (c.net < 0 ? 'neg' : '');
    // Week-over-week change in the net (more long / covering).
    const chg = c.net_change;
    const chgArrow = chg == null ? '·' : (chg > 0 ? '▲' : chg < 0 ? '▼' : '·');
    const chgCls = chg == null ? '' : (chg > 0 ? 'pos' : chg < 0 ? 'neg' : '');
    const chgTxt = chg == null ? '' : `前週比 <span class="mono">${fmtSigned(chg)}</span> ${chgArrow}`;

    // 1-year range gauge: marker at the net's percentile within the window.
    const hist = (c.net_history || []).filter(v => isFinite(v));
    let gauge = '';
    if (c.pctile_1y != null && hist.length >= 2) {
        const lo = Math.min(...hist), hi = Math.max(...hist);
        const p = Math.max(0, Math.min(100, c.pctile_1y));
        gauge =
            `<div class="cot-gauge" title="現在のネットが過去1年レンジのどこにあるか">`
          + `<div class="cot-gauge-track"><span class="cot-gauge-mark" style="left:${p.toFixed(1)}%"></span></div>`
          + `<div class="cot-gauge-scale"><span>安値 ${fmtN(lo)}</span>`
          + `<span class="mono">1年内 ${p.toFixed(0)}%</span>`
          + `<span>高値 ${fmtN(hi)}</span></div></div>`;
    }
    // Contrarian extreme note (where in the 1-year range, not absolute side).
    let note = '', noteCls = '';
    if (c.extreme > 0) { note = '1年来の高水準 — ロング積み上がり(逆張り警戒)'; noteCls = 'hi'; }
    else if (c.extreme < 0) { note = '1年来の低水準 — 投機筋が手仕舞い'; noteCls = 'lo'; }

    // 52-week sparkline of the net, with a zero baseline when it's in range.
    let spark = '';
    if (hist.length >= 2) {
        const W = 240, H = 96, pad = 2;
        const lo = Math.min(...hist, 0), hi = Math.max(...hist, 0), span = (hi - lo) || 1;
        const xO = i => pad + (i / (hist.length - 1)) * (W - 2 * pad);
        const yO = v => pad + (1 - (v - lo) / span) * (H - 2 * pad);
        const path = 'M' + hist.map((v, i) => `${xO(i).toFixed(1)},${yO(v).toFixed(1)}`).join(' L');
        const zeroY = yO(0).toFixed(1);
        const zeroLine = (lo < 0 && hi > 0)
            ? `<line x1="${pad}" y1="${zeroY}" x2="${W - pad}" y2="${zeroY}" stroke="#3f4760" stroke-width="0.8" stroke-dasharray="3 3"/>` : '';
        spark = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="cot-spark" role="img">`
              + zeroLine
              + `<path d="${path}" fill="none" stroke="#e3b341" stroke-width="1.4"/></svg>`;
    }

    el.innerHTML =
        `<div class="cot-top">`
      + `<span class="cot-net ${dirCls}">${fmtSigned(c.net)}</span>`
      + `<span class="cot-dir ${dirCls}">投機筋${dirTxt}</span>`
      + (chgTxt ? `<span class="cot-chg ${chgCls}">${chgTxt}</span>` : '')
      + `</div>`
      + gauge
      + (note ? `<div class="cot-note ${noteCls}">${note}</div>` : '')
      + spark
      + `<div class="cot-foot mute">`
      + `<span>実需筋(ヘッジ) <span class="mono">${fmtSigned(c.comm_net)}</span></span>`
      + `<span>OI <span class="mono">${fmtN(c.open_interest)}</span></span>`
      + (c.net_pct_oi != null ? `<span>net/OI <span class="mono">${c.net_pct_oi.toFixed(1)}%</span></span>` : '')
      + `</div>`;
}

// NOTE: the Macro Rates sidebar panel was removed (real yield promoted to its
// own card; per-pair policy rates + employment dropped). The macro snapshot is
// still served + computed, but there is no longer a paintMacro renderer.

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
            // Full snapshot.
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

// ------------------------------------------------------------
// Fund / trade-management panel — bottom row
// ------------------------------------------------------------
function paintFundKpi(snap) {
    const root = $bind('fund-kpi');
    if (!root) return;
    const a = snap.account, p = snap.performance;
    if (!a) { root.innerHTML = '<div class="empty mute">口座待ち…</div>'; return; }
    const def = (p && p.by_range && p.default_range) ? p.by_range[p.default_range] : null;
    const w7 = (p && p.by_range && p.by_range['7d']) ? p.by_range['7d'].net_profit : null;
    const m30 = (p && p.by_range && p.by_range['30d']) ? p.by_range['30d'].net_profit : null;
    const today = (p && p.today_total_pnl != null) ? p.today_total_pnl : null;
    const adv = p && p.advanced;
    const dailyLimit = a.balance ? a.balance * 0.03 : 0;
    const usedLoss = (today != null && today < 0) ? -today : 0;
    const remainPct = dailyLimit > 0 ? Math.max(0, 1 - usedLoss / dailyLimit) : 1;
    const sgn = (v, d) => v == null ? '--' : (v > 0 ? '+' : '') + Number(v).toFixed(d);
    const clsOf = v => v == null ? '' : v > 0 ? 'pos' : v < 0 ? 'neg' : '';
    const row = (label, val, cls) =>
        `<div class="fk-row"><span class="fk-l">${label}</span>`
        + `<span class="fk-v mono ${cls || ''}">${val}</span></div>`;
    root.innerHTML =
        `<div class="fk-acct">${esc(String(a.login))} / ${esc(String(a.server))}</div>`
        + row('Balance', fmtPrice(a.balance, 0))
        + row('Equity', fmtPrice(a.equity, 0))
        + row('Today', sgn(today, 0), clsOf(today))
        + row('7日', sgn(w7, 0), clsOf(w7))
        + row('30日', sgn(m30, 0), clsOf(m30))
        + row('Margin', fmtPrice(a.margin, 0))
        + row('Free', fmtPrice(a.margin_free, 0))
        + row('維持率', a.margin_level != null ? a.margin_level.toFixed(0) + '%' : '--')
        + row('MaxDD', (def && def.max_drawdown_pct != null) ? '-' + def.max_drawdown_pct.toFixed(1) + '%' : '--')
        + row('連勝/連敗', adv ? `${adv.max_win_streak}/${adv.max_loss_streak}` : '--')
        + row('推奨Lot', a.recommended_lot != null ? Number(a.recommended_lot).toFixed(2) : '--', 'accent')
        + `<div class="fk-bar"><div class="fk-bar-fill" style="width:${(remainPct*100).toFixed(0)}%"></div></div>`
        + `<div class="fk-bar-cap mute">日次損失上限まで ${(remainPct*100).toFixed(0)}%</div>`;
}
function paintPositionsTab(snap) {
    const root = $bind('fund-body');
    if (!root || UI.fundTab !== 'positions') return;
    const a = snap.account;
    const positions = (a && a.positions) || [];
    const sig = positions.map(p => `${p.ticket}:${p.type}:${p.volume}`).join(',')
        + '|' + (a && a.trade_allowed ? 1 : 0) + '|positions';
    if (sig !== root._fsig) {
        root._fsig = sig;
        if (!positions.length) {
            root.innerHTML = '<div class="empty">no open positions</div>';
        } else {
            const allBtn = a.trade_allowed
                ? `<button class="pos-close-all" type="button">全決済 (${positions.length})</button>` : '';
            root.innerHTML = allBtn + '<div class="ftab-pos">' + positions.map(p => {
                const cls = p.type === 'BUY' ? 'buy' : 'sell';
                const closeBtn = a.trade_allowed
                    ? `<button class="pos-close" type="button" data-ticket="${p.ticket}" title="この建玉を成行決済">✕</button>` : '';
                return `<div class="pos-row ${cls}" data-ticket="${p.ticket}">`
                    + `<span class="type-${cls}">${esc(p.type)}</span>`
                    + `<span class="pos-vol">${(p.volume || 0).toFixed(2)}L</span>`
                    + `<span class="pos-px mono"></span>`
                    + `<span class="pos-pnl mono"></span>`
                    + `<span class="pos-r mono mute"></span>`
                    + `<span class="pos-sltp mute"></span>`
                    + `<span class="pos-age mute"></span>`
                    + closeBtn + `</div>`;
            }).join('') + '</div>';
        }
        wireFundCloseButtons(root);
    }
    const pip = 0.10;   // XAUUSD pip in price units
    for (const p of positions) {
        const row = root.querySelector(`.pos-row[data-ticket="${p.ticket}"]`);
        if (!row) continue;
        const d = priceDigits(p.price_open, p.symbol);
        const px = row.querySelector('.pos-px');
        if (px) px.textContent = `${fmtPrice(p.price_open, d)}→${fmtPrice(p.price_current, d)}`;
        const pnlEl = row.querySelector('.pos-pnl');
        const pips = (p.type === 'BUY'
            ? (p.price_current - p.price_open)
            : (p.price_open - p.price_current)) / pip;
        if (pnlEl) {
            pnlEl.textContent = `${fmtSigned(p.profit, 0)} (${pips >= 0 ? '+' : ''}${pips.toFixed(1)}pip)`;
            pnlEl.className = 'pos-pnl mono ' + (p.profit > 0 ? 'pos' : p.profit < 0 ? 'neg' : '');
        }
        const rEl = row.querySelector('.pos-r');
        if (rEl) {
            let rtxt = '--';
            if (p.sl && Math.abs(p.price_open - p.sl) > 0) {
                const risk = Math.abs(p.price_open - p.sl);
                const move = p.type === 'BUY'
                    ? (p.price_current - p.price_open)
                    : (p.price_open - p.price_current);
                rtxt = (move / risk >= 0 ? '+' : '') + (move / risk).toFixed(2) + 'R';
            }
            rEl.textContent = rtxt;
        }
        const sltp = row.querySelector('.pos-sltp');
        if (sltp) sltp.textContent = (p.sl ? `SL ${fmtPrice(p.sl, d)}` : '') + (p.tp ? ` TP ${fmtPrice(p.tp, d)}` : '');
        const age = row.querySelector('.pos-age');
        if (age && p.time) age.textContent = _fmtAge(Date.now() / 1000 - p.time);
    }
}
function _fmtAge(sec) {
    if (sec < 0) sec = 0;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? `${h}h${String(m).padStart(2, '0')}m` : `${m}m`;
}
/** Delegated close-button handler on the fund panel positions tab (one-time per root). */
function wireFundCloseButtons(root) {
    if (!root || root._wiredClose) return;
    root._wiredClose = true;
    root.addEventListener('click', (e) => {
        if (e.target.closest('.pos-close-all')) { confirmClose({ all: true }); return; }
        const one = e.target.closest('.pos-close');
        if (one) confirmClose({ ticket: Number(one.dataset.ticket) });
    });
}
// ------------------------------------------------------------
// Fund panel — 資金管理 tab (F4)
// ------------------------------------------------------------

function _moneyFormHtml(snap) {
    const a = (snap && snap.account) || {};
    const p = (snap && snap.performance) || {};
    const defRange = p.default_range || '30d';
    const byRange = (p.by_range && p.by_range[defRange]) || {};
    const recLot = (a.recommended_lot != null && isFinite(a.recommended_lot))
        ? Number(a.recommended_lot).toFixed(2) : '0.01';
    const defaultWrPct = (byRange.win_rate != null && isFinite(byRange.win_rate))
        ? (byRange.win_rate * 100).toFixed(1) : '';
    const defaultRrVal = (byRange.risk_reward != null && isFinite(byRange.risk_reward))
        ? Number(byRange.risk_reward).toFixed(2) : '';

    return `<div class="mm-gate" data-bind="mm-gate"></div>
<div class="mm-grid">
  <div class="mm-group">
    <h4>ポジションサイジング</h4>
    <div class="mm-field">
      <label>エントリー価格</label>
      <input type="number" step="0.01" min="0" placeholder="例: 2350.00" data-bind="mm-entry">
    </div>
    <div class="mm-field">
      <label>ストップロス価格</label>
      <input type="number" step="0.01" min="0" placeholder="例: 2340.00" data-bind="mm-sl">
    </div>
    <div class="mm-field">
      <label>リスク率 %</label>
      <input type="number" step="0.1" min="0.1" max="100" value="1" data-bind="mm-risk-pct">
    </div>
    <div class="mm-field">
      <label>推奨 Lot</label>
      <span class="mm-out" data-bind="mm-lot">--</span>
    </div>
    <div class="mm-field">
      <label>リスク額 ≈ USD</label>
      <span class="mm-out" data-bind="mm-risk-amt">--</span>
    </div>
  </div>
  <div class="mm-group">
    <h4>リスク:リワード</h4>
    <div class="mm-field">
      <label>テイクプロフィット価格</label>
      <input type="number" step="0.01" min="0" placeholder="例: 2380.00" data-bind="mm-tp">
    </div>
    <div class="mm-field">
      <label>勝率 %</label>
      <input type="number" step="0.1" min="0" max="100" value="${esc(defaultWrPct)}" placeholder="例: 55.0" data-bind="mm-wr">
    </div>
    <div class="mm-field">
      <label>RR 比</label>
      <span class="mm-out" data-bind="mm-rr">--</span>
    </div>
    <div class="mm-field">
      <label>期待値 R</label>
      <span class="mm-out" data-bind="mm-exp">--</span>
    </div>
    <div class="mm-field">
      <label>損益分岐 WR %</label>
      <span class="mm-out" data-bind="mm-bewr">--</span>
    </div>
  </div>
  <div class="mm-group">
    <h4>ケリー基準 / 破産確率</h4>
    <div class="mm-field">
      <label>RR (手入力 or 計算値)</label>
      <input type="number" step="0.01" min="0" value="${esc(defaultRrVal)}" placeholder="例: 2.00" data-bind="mm-rr-manual">
    </div>
    <div class="mm-field">
      <label>連続トレード数 (RoR)</label>
      <input type="number" step="1" min="1" value="20" data-bind="mm-ror-n">
    </div>
    <div class="mm-field">
      <label>f* / ¼Kelly</label>
      <span class="mm-out" data-bind="mm-kelly">--</span>
    </div>
    <div class="mm-field">
      <label>破産確率 %</label>
      <span class="mm-out" data-bind="mm-ror">--</span>
    </div>
  </div>
  <div class="mm-group">
    <h4>マージンシミュレーション</h4>
    <div class="mm-field">
      <label>ロット数</label>
      <input type="number" step="0.01" min="0.01" value="${esc(recLot)}" data-bind="mm-lot-sim">
    </div>
    <div class="mm-field">
      <label>1pip価値 ≈ USD</label>
      <span class="mm-out" data-bind="mm-pipval">--</span>
    </div>
    <div class="mm-field">
      <label>維持率100%まで (pip)</label>
      <span class="mm-out" data-bind="mm-liq">--</span>
    </div>
    <div class="mm-field">
      <label>強制ロスカット目安</label>
      <span class="mm-out mute" data-bind="mm-liq-px">--</span>
    </div>
  </div>
</div>`;
}

function _wireMoneyForm(root) {
    root.addEventListener('input', () => {
        if (latestSnap) _recalcMoney(root, latestSnap);
    });
}

function _recalcMoney(root, snap) {
    if (!root) return;
    const a = (snap && snap.account) || {};
    const p = (snap && snap.performance) || {};
    const defRange = p.default_range || '30d';
    const byRange = (p.by_range && p.by_range[defRange]) || {};

    // Helper to read a bound input value as float; returns NaN if empty/invalid.
    function readF(name) { const el = root.querySelector(`[data-bind="${name}"]`); return el ? parseFloat(el.value) : NaN; }
    function writeOut(name, txt) { const el = root.querySelector(`[data-bind="${name}"]`); if (el) el.textContent = txt; }

    // Pre-fill win-rate / RR from snap only when the input is still pristine (empty value).
    const wrEl = root.querySelector('[data-bind="mm-wr"]');
    if (wrEl && wrEl.value === '' && byRange.win_rate != null && isFinite(byRange.win_rate)) {
        wrEl.value = (byRange.win_rate * 100).toFixed(1);
    }
    const rrManEl = root.querySelector('[data-bind="mm-rr-manual"]');
    if (rrManEl && rrManEl.value === '' && byRange.risk_reward != null && isFinite(byRange.risk_reward)) {
        rrManEl.value = Number(byRange.risk_reward).toFixed(2);
    }

    // --- Account values ---
    const balance  = (a.balance  != null && isFinite(a.balance))  ? a.balance  : 0;
    const equity   = (a.equity   != null && isFinite(a.equity))   ? a.equity   : 0;
    const marginUsed = (a.margin != null && isFinite(a.margin))   ? a.margin   : 0;

    // --- Position sizing ---
    const entry   = readF('mm-entry');
    const sl      = readF('mm-sl');
    const riskPct = readF('mm-risk-pct');
    const riskAmt = (isFinite(riskPct) && balance > 0) ? balance * riskPct / 100 : NaN;
    const slPips  = (isFinite(entry) && isFinite(sl)) ? Math.abs(entry - sl) / 0.10 : NaN;
    const PIP_VAL_PER_LOT = pipValueUsd(1);   // $10
    const lots = (isFinite(riskAmt) && isFinite(slPips) && slPips > 0)
        ? positionSizeLots(riskAmt, slPips, PIP_VAL_PER_LOT) : null;

    writeOut('mm-lot',      lots != null ? lots.toFixed(2) + ' lot' : '--');
    writeOut('mm-risk-amt', isFinite(riskAmt) ? riskAmt.toFixed(0) : '--');

    // --- RR / expectancy ---
    const tp       = readF('mm-tp');
    const rrCalc   = (isFinite(entry) && isFinite(sl) && isFinite(tp))
        ? rrRatio(entry, sl, tp) : null;
    const wrRaw    = readF('mm-wr');
    const winRate  = isFinite(wrRaw) ? wrRaw / 100 : null;

    writeOut('mm-rr',   rrCalc != null ? rrCalc.toFixed(2) : '--');

    const exp  = (winRate != null && rrCalc != null) ? expectancyR(winRate, rrCalc) : null;
    const bewr = rrCalc != null ? breakevenWR(rrCalc) : null;
    writeOut('mm-exp',  exp  != null ? exp.toFixed(3) + ' R' : '--');
    writeOut('mm-bewr', bewr != null ? (bewr * 100).toFixed(1) + '%' : '--');

    // --- Kelly / RoR (use manual RR field, fallback to computed RR) ---
    const rrManRaw = readF('mm-rr-manual');
    const rrForKelly = isFinite(rrManRaw) && rrManRaw > 0 ? rrManRaw
                     : (rrCalc != null && rrCalc > 0 ? rrCalc : null);
    const rorN     = readF('mm-ror-n');
    const kelly    = (winRate != null && rrForKelly != null) ? kellyFraction(winRate, rrForKelly) : null;
    const quarter  = kelly != null ? kelly / 4 : null;
    writeOut('mm-kelly',
        kelly != null ? `f*=${(kelly*100).toFixed(1)}% / ¼=${quarter != null ? (quarter*100).toFixed(1) : '--'}%` : '--');

    const rorUnits = isFinite(rorN) && rorN > 0 ? rorN : 20;
    const ror = (winRate != null && rrForKelly != null)
        ? riskOfRuin(winRate, rrForKelly, rorUnits) : null;
    writeOut('mm-ror', ror != null ? (ror * 100).toFixed(2) + '%' : '--');

    // --- Margin simulation ---
    const lotSim  = readF('mm-lot-sim');
    const pipVal  = isFinite(lotSim) && lotSim > 0 ? pipValueUsd(lotSim) : null;
    writeOut('mm-pipval', pipVal != null ? '$' + pipVal.toFixed(2) : '--');

    const liqPips = (pipVal != null && equity > 0)
        ? liquidationPips(equity, marginUsed, lotSim) : null;
    writeOut('mm-liq', liqPips != null ? liqPips.toFixed(1) + ' pip' : '--');

    // Liquidation price estimate (assuming a SELL exposure against current bid)
    // We don't know direction; just note both.
    if (liqPips != null && isFinite(entry)) {
        const liqPrice = entry - liqPips * 0.10;  // assumes BUY open
        writeOut('mm-liq-px', `買方向: ↓${fmtPrice(liqPrice, 2)} 付近`);
    } else {
        writeOut('mm-liq-px', '--');
    }

    // --- Gate banner ---
    const gateEl = root.querySelector('[data-bind="mm-gate"]');
    if (gateEl) {
        const todayPnl = (p.today_total_pnl != null) ? p.today_total_pnl : 0;
        const maxDd    = (byRange.max_drawdown_pct != null) ? byRange.max_drawdown_pct : 0;
        if (balance > 0 && todayPnl <= balance * -0.03) {
            gateEl.className = 'mm-gate warn';
            gateEl.innerHTML = '<span>日次損失上限到達 — 様子見推奨</span>';
        } else if (maxDd >= 10) {
            gateEl.className = 'mm-gate warn';
            gateEl.innerHTML = '<span>累計DD上限到達 — 様子見推奨</span>';
        } else {
            gateEl.className = 'mm-gate';
            gateEl.innerHTML = '';
        }
    }
}

function paintMoneyTab(snap) {
    const root = $bind('fund-body');
    if (!root || UI.fundTab !== 'money') return;
    if (root._fsig !== 'money') {
        root._fsig = 'money';
        root.innerHTML = _moneyFormHtml(snap);
        _wireMoneyForm(root);
    }
    _recalcMoney(root, snap);
}
function paintHistoryTab(snap) {
    const root = $bind('fund-body');
    if (!root || UI.fundTab !== 'history') return;
    const p = snap.performance;
    const trades = (p && p.trades) || [];
    const stamp = (p && p.generated_at) || 0;
    const fsig = 'history:' + stamp + ':' + trades.length;
    if (root._fsig === fsig) return;       // only repaint on full-snapshot change
    root._fsig = fsig;
    if (!trades.length) {
        root.innerHTML = '<div class="empty mute">確定取引なし(90日)。発注すると3TF状況つきで記録されます</div>';
        return;
    }
    const rows = trades.slice().sort((a,b)=>b.exit_time-a.exit_time).map(_histRow).join('');
    root.innerHTML = '<div class="ftab-hist">' + rows + '</div>';
}
function _histRow(t) {
    const cls = t.type === 'BUY' ? 'buy' : 'sell';
    const pnlCls = (t.profit||0) > 0 ? 'pos' : (t.profit||0) < 0 ? 'neg' : '';
    const d = priceDigits(t.entry_price, t.symbol);
    const r = t.r_multiple != null ? (t.r_multiple>=0?'+':'') + t.r_multiple.toFixed(2) + 'R' : '--';
    const pips = t.pips != null ? (t.pips>=0?'+':'') + t.pips.toFixed(1) : '--';
    const mae = t.mae_pips != null ? t.mae_pips.toFixed(0) : '--';
    const mfe = t.mfe_pips != null ? '+' + t.mfe_pips.toFixed(0) : '--';
    const ctx = t.ctx || {};
    const order = ['D1','H4','H1','M15'];
    const chips = Object.keys(ctx).sort((a,b)=>order.indexOf(a)-order.indexOf(b)).map(tf => {
        const c = ctx[tf] || {}; const up = !!c.ae;
        const tip = 'EMA ' + (up?'上':'下') + (c.adx!=null?` / ADX ${Math.round(c.adx)}`:'') + (c.rsi!=null?` / RSI ${c.rsi}`:'');
        return `<span class="jr-tf ${up?'up':'dn'}" title="${esc(tip)}">${esc(tf)} ${up?'↑':'↓'}</span>`;
    }).join('');
    return `<div class="hist-row ${cls}">`
        + `<span class="h-time mute">${esc(fmtJSTdate(t.exit_time))} ${esc(fmtJSTclockNoSec(t.exit_time))}</span>`
        + `<span class="h-side h-side-${cls}">${esc(t.type)}</span>`
        + `<span class="h-lot mono">${(t.volume||0).toFixed(2)}L</span>`
        + `<span class="h-px mono">${fmtPrice(t.entry_price,d)}→${fmtPrice(t.exit_price,d)}</span>`
        + `<span class="h-pips mono ${pnlCls}">${pips}pip</span>`
        + `<span class="h-pnl mono ${pnlCls}">${fmtSigned(t.profit,0)}</span>`
        + `<span class="h-r mono">${r}</span>`
        + `<span class="h-mae mute" title="MAE/MFE pips">${mae}/${mfe}</span>`
        + `<span class="h-ctx">${chips}</span>`
        + `</div>`;
}

/** Risk-limit gate: returns true when daily-loss or max-drawdown limits are hit.
 *  DISPLAY ONLY — does NOT disable buttons, does NOT alter order logic or lot size.
 *  The trader retains full discretion; this is a visual caution (様子見) signal. */
function _riskGated(snap) {
    const a = snap.account, p = snap.performance;
    if (!a || !p) return false;
    const today = p.today_total_pnl;
    const dailyHit = (a.balance > 0 && today != null && today <= a.balance * -0.03);
    const def = (p.by_range && p.default_range) ? p.by_range[p.default_range] : null;
    const ddHit = !!(def && def.max_drawdown_pct != null && def.max_drawdown_pct >= 10.0);
    return dailyHit || ddHit;
}

function paintFundPanel(snap) {
    paintFundKpi(snap);
    const tab = UI.fundTab;
    document.querySelectorAll('.fund-tabs .pill').forEach(p =>
        p.classList.toggle('on', p.dataset.fundtab === tab));
    if (tab === 'positions') paintPositionsTab(snap);
    else if (tab === 'money') paintMoneyTab(snap);
    else if (tab === 'history') paintHistoryTab(snap);
    // Risk gate: toggle .degraded on all panel-head order buttons (visual caution only).
    const gated = _riskGated(snap);
    document.querySelectorAll('.panel-head .trade-btn').forEach(b =>
        b.classList.toggle('degraded', gated));
    // Trade-permission gating + recommended-lot prefill (keeps the LOT input
    // pre-filled with the recommended lot).
    if (snap.account) applyTradeGating(snap.account);
}

let pendingFrame = null;
function paintAll() {
    pendingFrame = null;
    if (!latestSnap) return;
    paintHeader(latestSnap);
    paintPrices(latestSnap);
    paintSignals(latestSnap);
    paintEmaStack(latestSnap);
    paintFundPanel(latestSnap);
    paintRealYield(latestSnap);
    paintDxy(latestSnap);
    paintCot(latestSnap);
    paintCalendar(latestSnap);
}

// 1-second clock tick (independent of WS)
function startTickers() {
    setInterval(() => {
        if (!latestSnap) return;
        $bind('clock').textContent = fmtJSTclock(Date.now() / 1000);
    }, 1000);
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

// Fund tab click handler — event delegation on the bottom panel tab row.
document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.fund-tabs .pill[data-fundtab]');
    if (!btn) return;
    UI.fundTab = btn.dataset.fundtab;
    try { localStorage.setItem('mt5-fundtab', UI.fundTab); } catch (_e) {}
    if (latestSnap) paintFundPanel(latestSnap);
});

document.addEventListener('DOMContentLoaded', () => {
    applyDisplayFit();
    buildSymbolGrid();
    startTickers();
    setupOrderModal();
    paintAlertBell();                 // reflect the saved high-conviction-alert state
    connect();
    fetchEmaHistory();                // deep EMA history for drag-to-the-past
    setInterval(fetchEmaHistory, EMA_HISTORY_POLL_MS);
});

window.addEventListener('resize', () => {
    applyDisplayFit();
    if (latestSnap && !pendingFrame) {
        pendingFrame = requestAnimationFrame(paintAll);
    }
});
