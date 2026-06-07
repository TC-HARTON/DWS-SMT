# TFテーブル 折りたたみ（チャート伸長）— 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち

## ゴール

中央パネルの **TFテーブル（D1/H4/H1/M15 の行 = `.sig-body`）を折りたたみ可能**にする。OFF（折畳）時は EMA オシレーターチャートが上に伸長、ON（展開）時は現状維持。**BIASバー（`.composite` = ▼▼STRONG SELL -7.4/10 サマリ）は常時表示**（折りたたみ対象外）。

## 背景(grounding)

拡大パネル `.panel.expanded` は CSS grid:
```
grid-template-areas: "head" "signals" "emastack";
grid-template-rows:  auto      min-content   1fr;
```
`.signals` ブロック = `.composite`(BIASバー) ＋ `.sig-body`(TF表)。`.emastack`(1fr) が残り高さを埋める。
→ `.sig-body` を `display:none` にすると `.signals` 行(min-content)が BIASバー高さに縮み、`.emastack`(1fr) が自動的にその分まで伸びる。SVG は `viewBox="0 0 1000 300"` + `preserveAspectRatio="none"` で高さに自動追従（追加の JS リサイズ不要）。

## スコープ

含む: TF表(`.sig-body`)の折りたたみトグル、状態保存、チャート自動伸長。
非対象(YAGNI): 折畳アニメーション、コンパクト4枚グリッド時専用挙動（現状 XAUUSD 単独＝常に拡大パネル）。

## 設計

### トグル UI
- 常時表示の `.composite`(BIASバー)末尾に折りたたみボタンを追加:
  `<button class="sig-toggle" type="button" title="TF表 表示/非表示">▾</button>`
- 展開時 `▾` / 折畳時 `▸`（クリックで切替）。
- クリックは `stopPropagation`（パネル全体の expand/collapse ハンドラ `onPanelClick` に干渉させない。trade-panel と同パターン）。

### 状態
- localStorage `mt5-tftable`: `"show"`(既定=現状) / `"hide"`。
- パネル生成(`createPanel`)時に読み、`"hide"` なら panel に `.tf-collapsed` クラス＋ボタン `▸` を初期適用。
- トグル時: panel.classList.toggle('tf-collapsed') ＋ localStorage 更新 ＋ ボタン chevron 更新。

### レイアウト(CSS)
```css
.panel.tf-collapsed .sig-body { display: none; }
```
これだけで `.signals` 行が縮み `.emastack` が伸長（grid 任せ）。`.sig-toggle` はボタン用に mono/地味枠/hover強調を付与。フォントは mono 維持。

### データフロー
クリック → panel に `.tf-collapsed` トグル → CSS が `.sig-body` を隠す → grid が `.signals` 行を縮め `.emastack` を伸長 → SVG が viewBox で高さ追従。状態は localStorage に保存し次回起動で復元。

## テスト
- `node --check static/app.js`。
- 統合(feedback_integration_verify): ブラウザで
  1. トグル → TF表が消え、チャートが上に伸長、BIASバーは残る。
  2. 再トグル → 現状復帰。
  3. リロード → 折畳状態が保持される。
  4. console エラー 0。

## 受け入れ基準
1. BIASバーにトグルがあり、TF表(`.sig-body`)のみ折りたためる（BIASバーは常時表示）。
2. 折畳時にチャートが上へ伸長（grid 1fr 吸収）、展開時は現状と同一。
3. 状態が localStorage で保持され、リロード後も復元。
4. トグルクリックがパネル全体の展開/折畳を誤発火しない。
5. console エラー 0、フォント mono 維持。
