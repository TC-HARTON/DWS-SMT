# 管理画面 Phase 2: エッジ別ブレイクダウン Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **コミット/プッシュはユーザ明示指示時のみ。** Commit ステップは go サインまで実行しない。
> ⚠️ フロントのみ・バックエンド変更なし。検証は `node --check` ＋ gstack browse（Flask 8050、no-cache）。

**Goal:** 分析タブにエッジ別ブレイクダウン（非空次元のピル＋選択次元の勝率/PF表）を追加し、「どの条件が勝ち筋か」を可視化。

**Architecture:** `paintAnalyticsTab`（Phase 1）に edge セクションを連結。`snap.performance.edge`（`serialize_edge_stats` 送信済、`edge[dim][bucket]={n,win_rate,pf}`）の非空次元のみピル表示、選択次元を表描画。状態は module-level `_edgeDim`、ピルは `.onclick` 再アタッチ。

**Tech Stack:** vanilla JS + CSS。

**Spec:** `docs/superpowers/specs/2026-06-06-edge-breakdown-design.md`

---

## File Structure
- Modify: `static/app.js` — module-level `_edgeDim` + ヘルパ `_anlxEdgeSection`、`paintAnalyticsTab` に連結＋ピル onclick。
- Modify: `static/app.css` — `.anlx-edge-*`。

---

## Task 1: エッジセクション実装

**Files:** Modify `static/app.js`, `static/app.css`

- [ ] **Step 1: `_edgeDim` 状態 + `_anlxEdgeSection` ヘルパ (app.js)**

`paintAnalyticsTab` の直前（`function paintAnalyticsTab` の上）に追加:
```javascript
// Edge-breakdown dimension labels (order = pill order). Empty dims are skipped.
const ANLX_EDGE_DIMS = [
    ['by_alignment', '整合'], ['by_adx', 'ADX'], ['by_rsi', 'RSI'],
    ['by_weekday_jst', '曜日'], ['by_hold_min', '保有時間'], ['by_dxy', 'DXY'],
    ['by_cot_extreme', 'COT極端'], ['by_real_yield', '実質金利'], ['by_flip', 'フリップ'],
];
let _edgeDim = null;   // selected edge dimension (persists across repaints)

function _anlxEdgeSection(edge) {
    if (!edge) return '';
    const avail = ANLX_EDGE_DIMS.filter(([k]) => edge[k] && Object.keys(edge[k]).length);
    if (!avail.length) return '';
    if (!avail.some(([k]) => k === _edgeDim)) _edgeDim = avail[0][0];
    const pills = avail.map(([k, lbl]) =>
        `<button class="anlx-edge-pill${k === _edgeDim ? ' on' : ''}" data-edge="${k}">${lbl}</button>`
    ).join('');
    const buckets = edge[_edgeDim] || {};
    const wr = v => (v == null || !isFinite(v)) ? '--' : (v * 100).toFixed(1) + '%';
    const pf = v => (v == null || !isFinite(v)) ? '--' : Number(v).toFixed(2);
    const rows = Object.keys(buckets).map(b => {
        const s = buckets[b] || {};
        const lown = (s.n != null && s.n < 5) ? ' lown' : '';
        const wcls = (s.win_rate != null && isFinite(s.win_rate))
            ? (s.win_rate >= 0.5 ? ' pos' : ' neg') : '';
        return `<div class="anlx-edge-row${lown}"><span class="b">${b}</span>`
             + `<span class="num">${s.n != null ? s.n : '--'}</span>`
             + `<span class="num${wcls}">${wr(s.win_rate)}</span>`
             + `<span class="num">${pf(s.pf)}</span></div>`;
    }).join('');
    return `<div class="anlx-edge"><div class="anlx-cap">エッジ別 勝率 / PF</div>`
         + `<div class="anlx-edge-pills">${pills}</div>`
         + `<div class="anlx-edge-table"><div class="anlx-edge-row head">`
         + `<span class="b">条件</span><span class="num">件数</span>`
         + `<span class="num">勝率</span><span class="num">PF</span></div>`
         + rows + `</div></div>`;
}
```

- [ ] **Step 2: paintAnalyticsTab に連結 + ピル onclick (app.js)**

In `paintAnalyticsTab`, change the final render line:
```javascript
    root.innerHTML = grid + chart + _anlxRBars(adv.r_distribution || {});
```
to:
```javascript
    root.innerHTML = grid + chart + _anlxRBars(adv.r_distribution || {})
                   + _anlxEdgeSection(p.edge);
    root.querySelectorAll('.anlx-edge-pill').forEach(el => {
        el.onclick = () => {
            _edgeDim = el.dataset.edge;
            root._fsig = null;            // force a full re-render
            paintAnalyticsTab(latestSnap);
        };
    });
```

- [ ] **Step 3: CSS (app.css 末尾、`.anlx-bar-k` ルールの後に追加)**

```css
.anlx-edge { margin-top: var(--space-4); }
.anlx-edge-pills { display: flex; flex-wrap: wrap; gap: 3px; margin-bottom: 4px; }
.anlx-edge-pill { font-family: var(--mono); font-size: var(--text-2xs); font-weight: 600;
    color: var(--fg-2); background: transparent; border: 1px solid var(--border);
    border-radius: 5px; padding: 1px 8px; cursor: pointer; }
.anlx-edge-pill.on { color: var(--bg); background: var(--accent); border-color: var(--accent); }
.anlx-edge-table { display: flex; flex-direction: column; gap: 1px; }
.anlx-edge-row { display: grid; grid-template-columns: 1fr 56px 64px 56px;
    font-family: var(--mono); font-size: var(--text-xs); font-variant-numeric: tabular-nums;
    padding: 2px 6px; }
.anlx-edge-row .num { text-align: right; }
.anlx-edge-row.head { color: var(--fg-2); border-bottom: 1px solid var(--border); }
.anlx-edge-row.lown { opacity: 0.5; }
.anlx-edge-row .num.pos { color: var(--buy); }
.anlx-edge-row .num.neg { color: var(--sell); }
```

- [ ] **Step 4: Syntax check**

```
node --check static/app.js
```
Expected: no output (valid).

- [ ] **Step 5: Commit (go サインまで実行しない)**

```bash
git add static/app.js static/app.css
git commit -m "feat(ui): edge breakdown (win-rate/PF by condition) in analytics tab

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 統合検証(ブラウザ)

**Files:** なし(検証のみ)。

- [ ] **Step 1: 合成 edge 注入でピル+表を確認**

サーバ(8050)稼働中のまま再読込:
```
B=~/.claude/skills/gstack/browse/dist/browse
"$B" goto http://127.0.0.1:8050/
# 分析タブをアクティブに
"$B" js "(()=>{const t=document.querySelector('.fund-tabs .pill[data-fundtab=analytics]');t&&t.click();return 'ok';})()"
# 合成 performance(advanced 最小 + edge: 2非空次元 + 1空次元) 注入
"$B" js "(()=>{latestSnap=window.latestSnap||{};latestSnap.performance={generated_at:Date.now()/1000,default_range:'30d',by_range:{'30d':{trade_count:30,win_rate:0.5,profit_factor:1.2,risk_reward:1.5,avg_win:100,avg_loss:-80,net_profit:600}},advanced:{sharpe:1,sortino:1,calmar:1,recovery_factor:1,ulcer_index:1,var_95:-50,cvar_95:-80,max_win_streak:3,max_loss_streak:2,current_streak:1,max_drawdown_abs:-120,underwater_pct:0.1,r_distribution:{'0~1':10,'1~2':5},equity_curve:[0,50,30,80],underwater_curve:[0,0,-20,0]},edge:{by_adx:{'<20':{n:3,win_rate:0.33,pf:0.8},'20-30':{n:12,win_rate:0.58,pf:1.6},'>30':{n:8,win_rate:0.62,pf:1.9}},by_rsi:{'oversold':{n:6,win_rate:0.66,pf:2.1},'mid':{n:15,win_rate:0.46,pf:1.0}},by_flip:{}}};const root=document.querySelector('.fund-body');root._fsig=null;paintAnalyticsTab(latestSnap);return JSON.stringify({pills:[...document.querySelectorAll('.anlx-edge-pill')].map(p=>p.textContent), rows:document.querySelectorAll('.anlx-edge-table .anlx-edge-row').length});})()"
```
Expected: `pills:["ADX","RSI"]`（by_flip は空なので非表示）、`rows` = 1(head)+3(ADX バケツ) = 4。

- [ ] **Step 2: ピル切替 + n<5 薄表示 + 勝率色**

```
# RSI ピルへ切替
"$B" js "(()=>{const p=[...document.querySelectorAll('.anlx-edge-pill')].find(x=>x.textContent==='RSI');p&&p.click();return JSON.stringify({on:[...document.querySelectorAll('.anlx-edge-pill.on')].map(x=>x.textContent), rows:document.querySelectorAll('.anlx-edge-table .anlx-edge-row').length, lown:document.querySelectorAll('.anlx-edge-row.lown').length, pos:document.querySelectorAll('.anlx-edge-row .num.pos').length});})()"
"$B" console
"$B" screenshot "C:/Users/ohuch/Desktop/MT5_Python/_edge_verify.png"
```
Expected: `on:["RSI"]`、rows = 1+2 = 3。ADX に戻すと `<20`(n=3) が `.lown`。勝率 `.pos`（≥50%）が複数。console 0。スクショで確認後 `_edge_verify.png` 削除。

- [ ] **Step 3: 空エッジでセクション無し**

```
"$B" js "(()=>{latestSnap.performance.edge={by_flip:{}};const root=document.querySelector('.fund-body');root._fsig=null;paintAnalyticsTab(latestSnap);return JSON.stringify({edgeSections:document.querySelectorAll('.anlx-edge').length});})()"
```
Expected: `edgeSections:0`（非空次元ゼロ→省略）。

- [ ] **Step 4: コミット不要(検証のみ)**

---

## Self-Review

**1. Spec coverage:**
- 受入1(非空次元ピル＋選択次元表 バケツ|件数|勝率|PF) → T1 Step1-2、T2 Step1。✓
- 受入2(ピル切替・選択状態) → T1 Step2 onclick + `.on`、T2 Step2。✓
- 受入3(勝率 緑/赤、n<5 薄表示) → T1 `wcls`/`lown` + CSS、T2 Step2。✓
- 受入4(空次元ピル非表示・edge全空セクション省略) → T1 `avail` フィルタ + `if(!avail.length)return''`、T2 Step1/Step3。✓
- 受入5(バックエンド変更なし・console0・mono) → frontend のみ + `.anlx-edge-pill{font-family:var(--mono)}` + T2。✓

**2. Placeholder scan:** TBD/TODO 無し。全コードブロック実コード。合成データは検証用実値。✓

**3. Type consistency:** `snap.performance.edge[dim][bucket]={n,win_rate,pf}`（serialize_edge_stats 準拠）。`ANLX_EDGE_DIMS` のキーは by_alignment/by_adx/by_rsi/by_weekday_jst/by_hold_min/by_dxy/by_cot_extreme/by_real_yield/by_flip。`_edgeDim`/`latestSnap`/`paintAnalyticsTab`/`$bind` 既存。クラス `.anlx-edge-*` が JS 生成と CSS 一致。✓

ギャップ無し。
