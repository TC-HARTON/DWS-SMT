# TFテーブル 折りたたみ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **コミット/プッシュはユーザ明示指示時のみ。** Commit ステップは go サインまで実行しない。
> ⚠️ フロントのみ。検証は `node --check` ＋ gstack browse（本アプリは Flask 8050、CSS/JS は no-cache 配信＝再読込で反映、サーバ再起動不要）。

**Goal:** TFテーブル(`.sig-body`)を BIASバーのトグルで折りたため、折畳時に EMA チャートが上に伸長する。

**Architecture:** `.composite`(BIASバー、常時表示)末尾にシェブロン・トグルを追加。クリックで panel に `.tf-collapsed` を付与し `.sig-body{display:none}`。grid の `.emastack`(1fr) が空きを吸収して伸長。状態は localStorage `mt5-tftable` で保存・復元。

**Tech Stack:** vanilla JS + CSS。

**Spec:** `docs/superpowers/specs/2026-06-06-collapsible-tf-table-design.md`

---

## File Structure
- Modify: `static/app.js` — `createPanel`: `.composite` にトグルボタン、状態復元 + click 配線。
- Modify: `static/app.css` — `.composite` の5列目追加、`.sig-toggle`、`.panel.tf-collapsed .sig-body`。

---

## Task 1: トグル追加 + 折りたたみ + 状態保存

**Files:** Modify `static/app.js`, `static/app.css`

- [ ] **Step 1: トグルボタンを `.composite` に追加**

In `static/app.js` `createPanel`, the `.composite` block ends with `comp-score` then `</div>`. Add the toggle button after `comp-score`:

```javascript
                <span class="comp-score" data-bind="comp-score-${sym}" title="複合スコア = TF別シグナル × TF加重 / 正規化">--</span>
                <button class="sig-toggle" type="button" title="TF表 表示/非表示">▾</button>
```

- [ ] **Step 2: 状態復元 + click 配線**

In `createPanel`, after the trade-panel wiring (`tp.querySelectorAll('.trade-btn')...`) and before `return a;`, add:

```javascript
    // TF-table collapse toggle: folds .sig-body (BIAS bar stays); the grid's
    // 1fr emastack row then grows up. stopPropagation so it never triggers the
    // panel-level expand/collapse handler. State persists in localStorage.
    const sigT = a.querySelector('.sig-toggle');
    let tfHide = false;
    try { tfHide = localStorage.getItem('mt5-tftable') === 'hide'; } catch (_e) {}
    if (tfHide) { a.classList.add('tf-collapsed'); if (sigT) sigT.textContent = '▸'; }
    if (sigT) sigT.addEventListener('click', (e) => {
        e.stopPropagation();
        const hidden = a.classList.toggle('tf-collapsed');
        sigT.textContent = hidden ? '▸' : '▾';
        try { localStorage.setItem('mt5-tftable', hidden ? 'hide' : 'show'); } catch (_e) {}
    });
```

- [ ] **Step 3: CSS — 5列目 + トグル + 折りたたみ**

In `static/app.css`, update `.composite` grid to add a 5th (auto) column for the toggle:

Change:
```css
    grid-template-columns: auto minmax(0,auto) 1fr auto;
```
to:
```css
    grid-template-columns: auto minmax(0,auto) 1fr auto auto;
```

Then append (e.g., after the `.ema-tab.on` rule near the end):
```css
/* TF-table collapse toggle (lives on the always-visible BIAS bar). */
.sig-toggle {
    font-family: var(--mono); font-size: var(--text-sm); line-height: 1;
    color: var(--fg-2); background: transparent; border: 1px solid var(--border);
    border-radius: 4px; padding: 1px 7px; cursor: pointer;
}
.sig-toggle:hover { color: var(--fg); border-color: var(--accent); }
/* Folded: hide the TF matrix; the .signals grid row shrinks to the BIAS bar
   and the 1fr .emastack row grows up to fill the freed height. */
.panel.tf-collapsed .sig-body { display: none; }
```

- [ ] **Step 4: Syntax check**

```
node --check static/app.js
```
Expected: no output (valid). (CSS は構文チェック不要。)

- [ ] **Step 5: Commit (go サインまで実行しない)**

```bash
git add static/app.js static/app.css
git commit -m "feat(ui): collapsible TF table — chart grows when folded

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 統合検証(ブラウザ)

**Files:** なし(検証のみ)。

- [ ] **Step 1: ブラウザで折りたたみ挙動を確認**

サーバ(8050)稼働中のまま再読込:
```
B=~/.claude/skills/gstack/browse/dist/browse
"$B" goto http://127.0.0.1:8050/
"$B" console                               # エラー0
# トグル前: sig-body 可視
"$B" js "(()=>{const sb=document.querySelector('.sig-body');return JSON.stringify({sigVisible: sb && getComputedStyle(sb).display !== 'none'});})()"
# emastack の高さ(折畳前)
"$B" js "(()=>Math.round(document.querySelector('.emastack').getBoundingClientRect().height))()"
# トグルクリック
"$B" js "(()=>{document.querySelector('.sig-toggle').click();return 'clicked';})()"
# トグル後: sig-body 非表示 + emastack が高くなる + BIASバー(.composite)は残る
"$B" js "(()=>{const sb=document.querySelector('.sig-body');const comp=document.querySelector('.composite');return JSON.stringify({sigHidden: getComputedStyle(sb).display==='none', biasVisible: comp && getComputedStyle(comp).display!=='none', emaH: Math.round(document.querySelector('.emastack').getBoundingClientRect().height)});})()"
"$B" screenshot "C:/Users/ohuch/Desktop/MT5_Python/_collapse_verify.png"
```
Expected: トグル後 `sigHidden:true`、`biasVisible:true`、`emaH` が折畳前より**増加**（チャート上伸長）、console 0。スクショで TF表消失＋チャート拡大＋BIASバー残存を確認。確認後 `_collapse_verify.png` 削除。

- [ ] **Step 2: 状態保持を確認**

```
"$B" goto http://127.0.0.1:8050/      # リロード
"$B" js "(()=>document.querySelector('.panel').classList.contains('tf-collapsed'))()"   # => true (hide 保持)
# 元に戻す
"$B" js "(()=>{document.querySelector('.sig-toggle').click();return 'restored';})()"
"$B" js "(()=>document.querySelector('.panel').classList.contains('tf-collapsed'))()"   # => false
```
Expected: リロード後も折畳保持(true)、再トグルで復帰(false)。

- [ ] **Step 3: コミット不要(検証のみ)**

---

## Self-Review

**1. Spec coverage:**
- 受入1(BIASバーにトグル、`.sig-body`のみ折畳、BIASバー常時) → T1 Step1-3。✓
- 受入2(折畳でチャート伸長、展開で現状) → `.panel.tf-collapsed .sig-body{display:none}` + grid 1fr → T1 Step3 / T2 で emaH 増加検証。✓
- 受入3(localStorage 保持・復元) → T1 Step2 / T2 Step2。✓
- 受入4(トグルが panel 展開/折畳を誤発火しない) → T1 Step2 `e.stopPropagation()`。✓
- 受入5(console 0・mono 維持) → `.sig-toggle{font-family:var(--mono)}` / T2 Step1。✓

**2. Placeholder scan:** TBD/TODO 無し。全コードブロック実コード。✓

**3. Type consistency:** クラス名 `.sig-toggle` / `.tf-collapsed` / `.sig-body` / `.composite` が markup・click配線・CSS で一致。localStorage キー `mt5-tftable`（'show'/'hide'）一貫。✓

ギャップ無し。
