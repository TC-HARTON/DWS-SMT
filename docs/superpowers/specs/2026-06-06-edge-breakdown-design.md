# 管理画面 Phase 2: エッジ別ブレイクダウン — 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち
> 管理画面ロードマップ Phase 2/3（Phase 1=分析タブ済、Phase 3=文脈連動LOT は別spec）

## ゴール

「どの条件が自分の勝ち筋か」を可視化する。`account_monitor` の EdgeStats（9次元、各バケツ→{n, win_rate, pf}）を分析タブにセレクタ式で表示。MT5 Strategy Tester に無い独自分析で、無駄打ち防止＋（Phase 3 で）適正LOTの土台。**バックエンド変更なし**（`serialize_edge_stats` 送信済）。

## 背景(grounding)

- `snap.performance.edge`（WS full、`serialize_edge_stats` 送信済）。形: `edge[dim][bucketLabel] = {n, win_rate, pf}`。
- 9次元: `by_alignment`, `by_adx`, `by_rsi`, `by_weekday_jst`, `by_hold_min`, `by_dxy`, `by_cot_extreme`, `by_real_yield`, `by_flip`。
- 一部次元は env/ctx 未取得で空になりうる（DWS削除で `by_flip` 等）。→ 非空のみ描画で吸収。
- 分析タブ(`paintAnalyticsTab`)は Phase 1 で実装済。`$bind('fund-body')` + `UI.fundTab==='analytics'` + `_fsig` stamp。

## スコープ

含む: 分析タブにエッジ別セクション（次元ピル＋選択次元の表）。
非対象(YAGNI): 文脈連動LOT(Phase 3)、エッジの統計的有意性検定、バックエンド/serialize 変更。

## 設計

### 配置
`paintAnalyticsTab` の出力末尾（R分布の後）に「エッジ別」セクションを連結。

### 次元定義
JS の順序付き配列で dim キー→JPラベルを定義:
```
[ ['by_alignment','整合'], ['by_adx','ADX'], ['by_rsi','RSI'],
  ['by_weekday_jst','曜日'], ['by_hold_min','保有時間'], ['by_dxy','DXY'],
  ['by_cot_extreme','COT極端'], ['by_real_yield','実質金利'], ['by_flip','フリップ'] ]
```
非空判定: `edge[dim]` が存在し、キー(バケツ)が1つ以上。

### コンポーネント
1. **次元ピル** `.anlx-edge-pills`: 非空次元のみ `<button class="anlx-edge-pill" data-edge="<dim>">ラベル</button>`。現選択に `.on`。
2. **選択次元の表** `.anlx-edge-table`: ヘッダ `バケツ | 件数 | 勝率 | PF`、各バケツ行。
   - 勝率: `(win_rate*100).toFixed(1)%`、`win_rate>=0.5` 緑 / `<0.5` 赤。
   - PF: `pf.toFixed(2)`（∞/null は `--`）。
   - **n が小さいバケツ（n < 5）は行を薄表示（`.lown`）**＝サンプル不足の示唆。
   - バケツ順は `edge[dim]` のキー順（バックエンドが意味順で出力）。
3. **状態**: module-level `let _edgeDim = null`。描画時 `_edgeDim` が非空次元に無ければ先頭の非空次元へフォールバック。
4. **インタラクション**: `root.innerHTML` 設定後に `.anlx-edge-pill` へ `el.onclick = () => { _edgeDim = el.dataset.edge; root._fsig = null; paintAnalyticsTab(latestSnap); }`（`.onclick=` で再描画毎の重複回避）。
5. **空**: 非空次元ゼロ（取引なし/edge無し）ならエッジセクションを出さない。

### CSS
`.anlx-edge-pills`（flex・gap）、`.anlx-edge-pill`（mono・枠、`.on` で accent 反転、`.ema-tab` に準ずる）、`.anlx-edge-table`（grid 4列、tabular-nums）、勝率 `.pos`/`.neg`、`.lown { opacity:.5 }`。

## データフロー
WS full → `snap.performance.edge`（送信済） → `paintAnalyticsTab` がエッジセクション生成（非空次元ピル＋選択次元表） → ピル click で `_edgeDim` 切替＋再描画。

## テスト/検証
- `node --check static/app.js`。
- 統合(feedback_integration_verify): ブラウザで合成 edge 注入（複数次元、一部 n<5、一部空次元）→
  1. 非空次元のみピル表示（空次元は出ない）。
  2. ピル click で表が切替、選択ピルに `.on`。
  3. 勝率の緑/赤、n<5 の薄表示。
  4. edge 無し時はエッジセクション無し。
  5. console 0、mono 維持。

## 受け入れ基準
1. 分析タブに非空エッジ次元のピル＋選択次元の表（バケツ|件数|勝率|PF）が出る。
2. ピルで次元切替、選択状態が分かる。
3. 勝率 緑/赤、低サンプル(n<5)は薄表示。
4. 空次元はピル非表示、edge全空はセクション省略。
5. バックエンド/serialize 変更なし。console 0、mono 維持。
