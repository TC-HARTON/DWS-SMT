# 管理画面 Phase 1: 分析タブ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **コミット/プッシュはユーザ明示指示時のみ。** Commit ステップは go サインまで実行しない。
> ⚠️ フロントのみ・バックエンド変更なし。検証は `node --check` ＋ gstack browse（Flask 8050、CSS/JS は no-cache＝再読込で反映）。

**Goal:** 送信済みだが未表示の高度分析（指標グリッド/エクイティ・DDチャート/R分布）を新「分析」タブで可視化。

**Architecture:** `.fund-tabs` に4つ目 `分析`(data-fundtab="analytics")。`paintFundPanel` の dispatch に分岐、新 `paintAnalyticsTab(snap)` が `snap.performance`（advanced / by_range[default_range]、serialize 済）を描画。既存タブと同じ `$bind('fund-body')`＋`UI.fundTab` ガード＋`_fsig` stamp。

**Tech Stack:** vanilla JS + CSS + SVG。

**Spec:** `docs/superpowers/specs/2026-06-06-analytics-tab-design.md`

---

## File Structure
- Modify: `static/index.html` — `.fund-tabs` に4つ目ボタン。
- Modify: `static/app.js` — `paintFundPanel` dispatch 分岐 + `paintAnalyticsTab` + ヘルパ `_anlxSparkArea`/`_anlxRBars`。
- Modify: `static/app.css` — `.anlx-*`。

---

## Task 1: 分析タブ実装

**Files:** Modify `static/index.html`, `static/app.js`, `static/app.css`

- [ ] **Step 1: タブボタン追加 (index.html)**

`.fund-tabs` の history ボタンの後に追加:
```html
      <button class="pill" data-fundtab="history">履歴</button>
      <button class="pill" data-fundtab="analytics">分析</button>
```

- [ ] **Step 2: dispatch 分岐 (app.js paintFundPanel)**

`paintFundPanel` の dispatch（`else if (tab === 'history') paintHistoryTab(snap);` の後）に追加:
```javascript
    else if (tab === 'history') paintHistoryTab(snap);
    else if (tab === 'analytics') paintAnalyticsTab(snap);
```

- [ ] **Step 3: paintAnalyticsTab + ヘルパ実装 (app.js)**

`paintHistoryTab` 関数の直後に追加:
```javascript
function _anlxSparkArea(arr, col) {
    if (!arr || arr.length < 2) return '';
    const W = 1000, H = 100, pad = 6, n = arr.length;
    const max = Math.max(...arr), min = Math.min(...arr), rng = (max - min) || 1;
    const X = i => pad + (i / (n - 1)) * (W - 2 * pad);
    const Y = v => pad + (1 - (v - min) / rng) * (H - 2 * pad);
    let d = 'M' + X(0).toFixed(1) + ',' + Y(arr[0]).toFixed(1);
    for (let i = 1; i < n; i++) d += ' L' + X(i).toFixed(1) + ',' + Y(arr[i]).toFixed(1);
    const fill = d + ` L${X(n - 1).toFixed(1)},${(H - pad).toFixed(1)} L${X(0).toFixed(1)},${(H - pad).toFixed(1)} Z`;
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="anlx-svg" role="img">`
         + `<path d="${fill}" fill="${col}" opacity="0.16" stroke="none"/>`
         + `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
         + `</svg>`;
}

function _anlxRBars(rdist) {
    const keys = Object.keys(rdist || {});
    if (!keys.length) return '';
    const mx = Math.max(...keys.map(k => rdist[k]), 1);
    const bars = keys.map(k => {
        const h = (rdist[k] / mx * 100).toFixed(1);
        return `<div class="anlx-bar"><div class="anlx-bar-n">${rdist[k]}</div>`
             + `<div class="anlx-bar-fill" style="height:${h}%"></div>`
             + `<div class="anlx-bar-k">${k}</div></div>`;
    }).join('');
    return `<div class="anlx-rdist"><div class="anlx-cap">R倍数分布</div>`
         + `<div class="anlx-bars">${bars}</div></div>`;
}

function paintAnalyticsTab(snap) {
    const root = $bind('fund-body');
    if (!root || UI.fundTab !== 'analytics') return;
    const p = snap.performance;
    const adv = p && p.advanced;
    const br = p && p.by_range && p.by_range[p.default_range];
    const stamp = (p && p.generated_at) || 0;
    const fsig = 'anlx:' + stamp;
    if (root._fsig === fsig) return;
    root._fsig = fsig;
    if (!adv || !br || !br.trade_count) {
        root.innerHTML = '<div class="empty mute">約定履歴がまだありません</div>';
        return;
    }
    const n2 = v => (v == null || !isFinite(v)) ? '--' : Number(v).toFixed(2);
    const pct = v => (v == null || !isFinite(v)) ? '--' : (v * 100).toFixed(1) + '%';
    const kpi = (k, v) => `<div class="anlx-kpi"><span class="k">${k}</span><span class="v">${v}</span></div>`;
    const grid = `<div class="anlx-grid">`
        + kpi('取引数', br.trade_count)
        + kpi('勝率', pct(br.win_rate))
        + kpi('PF', n2(br.profit_factor))
        + kpi('RR', n2(br.risk_reward))
        + kpi('平均勝', fmtSigned(br.avg_win, 0))
        + kpi('平均負', fmtSigned(br.avg_loss, 0))
        + kpi('純損益', fmtSigned(br.net_profit, 0))
        + kpi('Sharpe', n2(adv.sharpe))
        + kpi('Sortino', n2(adv.sortino))
        + kpi('Calmar', n2(adv.calmar))
        + kpi('Recovery', n2(adv.recovery_factor))
        + kpi('Ulcer', n2(adv.ulcer_index))
        + kpi('最大DD', fmtSigned(adv.max_drawdown_abs != null ? -Math.abs(adv.max_drawdown_abs) : null, 0))
        + kpi('VaR95', fmtSigned(adv.var_95, 0))
        + kpi('CVaR95', fmtSigned(adv.cvar_95, 0))
        + kpi('最大連勝', adv.max_win_streak != null ? adv.max_win_streak : '--')
        + kpi('最大連敗', adv.max_loss_streak != null ? adv.max_loss_streak : '--')
        + kpi('現在連続', adv.current_streak != null ? adv.current_streak : '--')
        + `</div>`;
    const eq = adv.equity_curve || [], uw = adv.underwater_curve || [];
    const eqCol = (eq.length && eq[eq.length - 1] >= eq[0]) ? '#3fb98a' : '#ff5b6b';
    const chart = (eq.length >= 2 || uw.length >= 2)
        ? `<div class="anlx-chart"><div class="anlx-cap">エクイティカーブ(累積損益)</div>`
          + _anlxSparkArea(eq, eqCol)
          + `<div class="anlx-cap">ドローダウン</div>` + _anlxSparkArea(uw, '#ff5b6b')
          + `</div>`
        : '';
    root.innerHTML = grid + chart + _anlxRBars(adv.r_distribution || {});
}
```

- [ ] **Step 4: CSS (app.css 末尾に追加)**

```css
/* 分析タブ (Phase 1): 指標グリッド + エクイティ/DD + R分布。 */
.anlx-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
             gap: 6px; margin-bottom: var(--space-4); }
.anlx-kpi { display: flex; flex-direction: column; gap: 1px; padding: 4px 8px;
            background: var(--bg); border: 1px solid var(--border); border-radius: 5px; }
.anlx-kpi .k { font-size: var(--text-2xs); color: var(--fg-2); }
.anlx-kpi .v { font-family: var(--mono); font-size: var(--text-md); font-weight: 700;
               font-variant-numeric: tabular-nums; }
.anlx-cap { font-size: var(--text-2xs); color: var(--fg-2); margin: 6px 0 2px; }
.anlx-svg { width: 100%; height: 84px; display: block; }
.anlx-chart .anlx-svg + .anlx-cap { margin-top: 8px; }
.anlx-rdist .anlx-bars { display: flex; align-items: flex-end; gap: 4px; height: 96px; margin-top: 2px; }
.anlx-bar { flex: 1; display: flex; flex-direction: column; align-items: center;
            justify-content: flex-end; height: 100%; gap: 2px; }
.anlx-bar-fill { width: 68%; background: var(--accent); border-radius: 2px 2px 0 0; min-height: 1px; }
.anlx-bar-n { font-size: var(--text-3xs); color: var(--fg-2); font-variant-numeric: tabular-nums; }
.anlx-bar-k { font-size: var(--text-3xs); color: var(--fg-3); }
```

- [ ] **Step 5: Syntax check**

```
node --check static/app.js
```
Expected: no output (valid).

- [ ] **Step 6: Commit (go サインまで実行しない)**

```bash
git add static/index.html static/app.js static/app.css
git commit -m "feat(ui): analytics tab — equity/DD, advanced stats, R-distribution

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 統合検証(ブラウザ)

**Files:** なし(検証のみ)。

- [ ] **Step 1: 分析タブの存在・切替・空状態**

サーバ(8050)稼働中のまま再読込:
```
B=~/.claude/skills/gstack/browse/dist/browse
"$B" goto http://127.0.0.1:8050/
"$B" console                                   # エラー0
# 分析タブが存在し、クリックで on になる
"$B" js "(()=>{const t=document.querySelector('.fund-tabs .pill[data-fundtab=analytics]');t&&t.click();return JSON.stringify({exists:!!t, on:t&&t.classList.contains('on'), body:document.querySelector('.fund-body').textContent.slice(0,40)});})()"
```
Expected: `exists:true`、`on:true`、履歴ゼロなら body に「約定履歴がまだありません」。

- [ ] **Step 2: 合成データ注入で3ブロック描画確認**

```
"$B" js "(()=>{window.latestSnap=window.latestSnap||{};const eq=[];let s=0;for(let i=0;i<40;i++){s+=(Math.sin(i/3)*50);eq.push(s);} const uw=eq.map((v,i)=>v-Math.max(...eq.slice(0,i+1))); latestSnap.performance={generated_at:Date.now()/1000, default_range:'30d', by_range:{'30d':{trade_count:40,win_rate:0.55,profit_factor:1.4,risk_reward:1.8,avg_win:120,avg_loss:-80,net_profit:1600}}, advanced:{sharpe:1.4,sortino:2.1,calmar:0.9,recovery_factor:3.0,ulcer_index:1.2,var_95:-90,cvar_95:-140,max_win_streak:5,max_loss_streak:3,current_streak:2,max_drawdown_abs:-260,underwater_pct:0.2,r_distribution:{'<-1':4,'-1~0':10,'0~1':14,'1~2':9,'>2':3},equity_curve:eq,underwater_curve:uw}}; const root=document.querySelector('.fund-body'); root._fsig=null; paintAnalyticsTab(latestSnap); return JSON.stringify({kpis:document.querySelectorAll('.anlx-kpi').length, svgs:document.querySelectorAll('.anlx-svg').length, bars:document.querySelectorAll('.anlx-bar').length});})()"
"$B" screenshot "C:/Users/ohuch/Desktop/MT5_Python/_anlx_verify.png"
```
Expected: `kpis:18`（指標カード）, `svgs:2`（エクイティ+DD）, `bars:5`（R分布）。スクショで3ブロック描画＋ console 0 を確認。確認後 `_anlx_verify.png` 削除。

- [ ] **Step 3: コミット不要(検証のみ)**

---

## Self-Review

**1. Spec coverage:**
- 受入1(分析タブ追加・排他切替・localStorage) → T1 Step1-2（既存 `.fund-tabs` 委譲 click + `paintFundPanel` の `.on` トグル + `UI.fundTab` localStorage は既存機構）。✓
- 受入2(履歴あり時 指標/エクイティ・DD/R分布 描画) → T1 Step3 + T2 Step2。✓
- 受入3(履歴ゼロ時 空状態・エラーなし) → T1 Step3 ガード + T2 Step1。✓
- 受入4(バックエンド/serialize 変更なし) → 全タスク frontend のみ。✓
- 受入5(console 0・mono) → `.anlx-kpi .v{font-family:var(--mono)}` + T2。✓

**2. Placeholder scan:** TBD/TODO 無し。全コードブロック実コード。合成データは検証用の実値。✓

**3. Type consistency:** `snap.performance.advanced`/`by_range[default_range]` のフィールド名は serialize_performance 準拠（sharpe/sortino/calmar/recovery_factor/ulcer_index/var_95/cvar_95/max_win_streak/max_loss_streak/current_streak/max_drawdown_abs/equity_curve/underwater_curve/r_distribution、by_range: trade_count/win_rate/profit_factor/risk_reward/avg_win/avg_loss/net_profit）。`$bind`/`fmtSigned`/`UI.fundTab` は既存。クラス `.anlx-*` が JS 生成と CSS で一致。✓

ギャップ無し。
