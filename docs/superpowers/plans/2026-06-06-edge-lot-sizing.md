# 管理画面 Phase 3: 実績エッジ連動 適正Lot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **コミット/プッシュはユーザ明示指示時のみ。** Commit ステップは go サインまで実行しない。
> ⚠️ フロントのみ・バックエンド変更なし。検証は `node --check` ＋ gstack browse（Flask 8050、no-cache）。

**Goal:** 資金管理タブに「実績ベース 適正Lot (¼Kelly)」を追加し、実現エッジ(by_range の勝率/RR)→Kelly→適正ロットを自動提示。

**Architecture:** `_moneyFormHtml` に全幅バナー `.mm-edge` を追加、`_recalcMoney` 末尾で実績勝率/RR→`kellyFraction`→¼Kelly→`positionSizeLots`(entry/SL由来 slPips)を算出し `.mm-edge` を更新。既存ヘルパ・送信済データのみ。手入力フィールドは温存。

**Tech Stack:** vanilla JS + CSS。

**Spec:** `docs/superpowers/specs/2026-06-06-edge-lot-sizing-design.md`

---

## File Structure
- Modify: `static/app.js` — `_moneyFormHtml`(mm-edge div + ラベル注記)、`_recalcMoney`(末尾に実績エッジ block)。
- Modify: `static/app.css` — `.mm-edge` / `.mm-edge-num` / `.mm-edge-warn`。

---

## Task 1: 実績エッジ連動 適正Lot 実装

**Files:** Modify `static/app.js`, `static/app.css`

- [ ] **Step 1: バナー div + ラベル注記 (_moneyFormHtml)**

`_moneyFormHtml` の return 冒頭、`mm-gate` と `mm-grid` の間に `.mm-edge` を追加:
```javascript
    return `<div class="mm-gate" data-bind="mm-gate"></div>
<div class="mm-edge" data-bind="mm-edge"></div>
<div class="mm-grid">
```

同 `_moneyFormHtml` 内の2ラベルへ実績由来の注記:
- `<label>勝率 %</label>` → `<label>勝率 %(実績既定)</label>`
- `<label>RR (手入力 or 計算値)</label>` → `<label>RR (実績既定/上書可)</label>`

- [ ] **Step 2: 実績エッジ block (_recalcMoney 末尾)**

`_recalcMoney` の Kelly/RoR 計算の後（`writeOut('mm-ror', ...)` の直後）に追加（`balance`/`slPips`/`PIP_VAL_PER_LOT`/`byRange`/`defRange` は同関数内で既定義）:
```javascript
    // --- 実績ベース 適正Lot (¼Kelly): realized edge -> Kelly -> lot via SL ---
    const edgeEl = root.querySelector('[data-bind="mm-edge"]');
    if (edgeEl) {
        const rw = byRange.win_rate, rrR = byRange.risk_reward, nTr = byRange.trade_count;
        if (rw == null || !isFinite(rw) || rrR == null || !isFinite(rrR) || !nTr) {
            edgeEl.innerHTML = `<b>実績ベース 適正Lot</b> `
                + `<span class="mute">実績データ不足（${defRange}に確定取引なし）</span>`;
        } else {
            const f = Math.max(0, kellyFraction(rw, rrR));
            const q = f / 4;
            const riskAmtE = balance > 0 ? q * balance : NaN;
            const lotE = (isFinite(riskAmtE) && riskAmtE > 0 && isFinite(slPips) && slPips > 0)
                ? positionSizeLots(riskAmtE, slPips, PIP_VAL_PER_LOT) : null;
            const lotTxt = lotE != null
                ? `<span class="mm-edge-num">${lotE.toFixed(2)} lot</span> (リスク$≈${riskAmtE.toFixed(0)})`
                : (balance <= 0 ? '<span class="mute">残高0</span>' : '<span class="mute">SL入力で算出</span>');
            const warn = nTr < 20 ? ` <span class="mm-edge-warn">⚠ サンプル少・参考程度</span>` : '';
            edgeEl.innerHTML = `<b>実績ベース 適正Lot (¼Kelly)</b> `
                + `実績(${defRange}, N=${nTr}) 勝率 ${(rw * 100).toFixed(1)}% / RR ${Number(rrR).toFixed(2)} `
                + `→ f*=${(f * 100).toFixed(1)}% ¼=${(q * 100).toFixed(1)}% → ${lotTxt}${warn}`;
        }
    }
```

- [ ] **Step 3: CSS (app.css 末尾)**

```css
/* 実績エッジ連動 適正Lot バナー (Phase 3)。 */
.mm-edge { font-family: var(--mono); font-size: var(--text-sm); font-variant-numeric: tabular-nums;
    color: var(--fg-2); background: var(--bg); border-left: 3px solid var(--accent);
    border-radius: 5px; padding: 6px 10px; margin-bottom: var(--space-3); }
.mm-edge b { color: var(--fg); margin-right: 6px; }
.mm-edge-num { color: var(--accent); font-weight: 700; }
.mm-edge-warn { color: var(--sell); font-weight: 600; }
```

- [ ] **Step 4: Syntax check**

```
node --check static/app.js
```
Expected: no output (valid).

- [ ] **Step 5: Commit (go サインまで実行しない)**

```bash
git add static/app.js static/app.css
git commit -m "feat(ui): realized-edge position sizing (¼Kelly) in money tab

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 統合検証(ブラウザ)

**Files:** なし(検証のみ)。

- [ ] **Step 1: 合成 account+by_range 注入 + entry/SL で適正Lot**

サーバ(8050)稼働中のまま再読込:
```
B=~/.claude/skills/gstack/browse/dist/browse
"$B" goto http://127.0.0.1:8050/
# 資金管理タブをアクティブに
"$B" js "(()=>{const t=document.querySelector('.fund-tabs .pill[data-fundtab=money]');t&&t.click();return 'ok';})()"
# 合成 snap(balance=10000, 勝率55%/RR1.8/40取引) + entry/SL 入力 → 再計算
"$B" js "(()=>{latestSnap=window.latestSnap||{};latestSnap.account={balance:10000,equity:10000,margin:0};latestSnap.performance={default_range:'30d',by_range:{'30d':{win_rate:0.55,risk_reward:1.8,trade_count:40}}};const root=document.querySelector('.fund-body');const set=(n,v)=>{const e=root.querySelector('[data-bind='+n+']');if(e){e.value=v;}};set('mm-entry','4300');set('mm-sl','4290');root.querySelector('[data-bind=mm-entry]').dispatchEvent(new Event('input',{bubbles:true}));const edge=root.querySelector('[data-bind=mm-edge]');return JSON.stringify({edge: edge && edge.textContent.replace(/\s+/g,' ').trim(), hasLot: !!(edge&&edge.querySelector('.mm-edge-num'))});})()"
```
Expected: edge に「実績ベース 適正Lot (¼Kelly) 実績(30d, N=40) 勝率 55.0% / RR 1.80 → f*=... ¼=... → X.XX lot (リスク$≈...)」、`hasLot:true`。N=40≥20 なので警告なし。

- [ ] **Step 2: サンプル警告 / SL未入力 / 実績なし**

```
# trade_count=8 → 警告
"$B" js "(()=>{latestSnap.performance.by_range['30d'].trade_count=8;document.querySelector('[data-bind=mm-entry]').dispatchEvent(new Event('input',{bubbles:true}));return JSON.stringify({warn:!!document.querySelector('.mm-edge-warn')});})()"
# SL クリア → Lot 部「SL入力で算出」
"$B" js "(()=>{const root=document.querySelector('.fund-body');root.querySelector('[data-bind=mm-sl]').value='';root.querySelector('[data-bind=mm-sl]').dispatchEvent(new Event('input',{bubbles:true}));return document.querySelector('[data-bind=mm-edge]').textContent.includes('SL入力で算出');})()"
# 実績なし → データ不足
"$B" js "(()=>{latestSnap.performance.by_range['30d']={};document.querySelector('[data-bind=mm-entry]').dispatchEvent(new Event('input',{bubbles:true}));return document.querySelector('[data-bind=mm-edge]').textContent.includes('実績データ不足');})()"
"$B" console
"$B" screenshot "C:/Users/ohuch/Desktop/MT5_Python/_lot_verify.png"
```
Expected: `warn:true`、SL未入力で `true`、実績なしで `true`。console 0。スクショ確認後 `_lot_verify.png` 削除。

- [ ] **Step 3: コミット不要(検証のみ)**

---

## Self-Review

**1. Spec coverage:**
- 受入1(バナー: 実績勝率/RR→f*/¼Kelly→適正Lot, entry/SL併用) → T1 Step1-2、T2 Step1。✓
- 受入2(n<20警告・実績なし・SL未入力/残高0) → T1 Step2 分岐、T2 Step2。✓
- 受入3(勝率/RR ラベル注記・手入力温存) → T1 Step1（ラベルのみ変更、input 不変）。✓
- 受入4(バックエンド変更なし・既存ヘルパ) → frontend のみ、`kellyFraction`/`positionSizeLots`/`pipValueUsd` 再利用。✓
- 受入5(console0・mono) → `.mm-edge{font-family:var(--mono)}` + T2。✓

**2. Placeholder scan:** TBD/TODO 無し。全コードブロック実コード。合成データは検証用実値。✓

**3. Type consistency:** `byRange.win_rate/risk_reward/trade_count`(serialize 準拠)、`kellyFraction(winRate,RR)`/`positionSizeLots(riskAmt,slPips,pipValPerLot)`/`PIP_VAL_PER_LOT`(=pipValueUsd(1)) は _recalcMoney 既存。`balance`/`slPips`/`defRange`/`byRange` は同関数内既定義。data-bind `mm-edge` が html と JS で一致。クラス `.mm-edge*` が JS 生成と CSS 一致。✓

ギャップ無し。
