# 管理画面 Phase 1: 分析タブ（高度履歴分析の可視化）— 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち
> 管理画面ロードマップ Phase 1/3（Phase 2=エッジ別表、Phase 3=文脈連動LOT は別spec）

## ゴール

`account_monitor` が既に計算し `serialize_performance` で送信済みだが UI 未表示の高度分析を、新「分析」タブで可視化する。MT5 Strategy Tester レポート相当。**バックエンド変更なし**（送信済データの描画のみ）。

## 背景(grounding)

- 下段 fund パネルは `.fund-tabs`（`data-fundtab`: positions / money / history）＋ `paintFund` が tab で `paintPositionsTab`/`paintMoneyTab`/`paintHistoryTab` を切替（localStorage `mt5-fundtab`）。
- `snap.performance`（WS full スナップ）に既に同梱:
  - `advanced`: sharpe, sortino, calmar, recovery_factor, ulcer_index, var_95, cvar_95, max_win_streak, max_loss_streak, current_streak, max_drawdown_abs, underwater_pct, r_distribution(dict), equity_curve(list), underwater_curve(list)。
  - `by_range`: range→{trade_count, win_count, loss_count, win_rate, profit_factor, risk_reward, avg_win, avg_loss, max_drawdown_abs, max_drawdown_pct, net_profit, gross_profit, gross_loss, ...}。
  - `default_range`（既定の集計窓、例 "30d"）, `trades`（約定配列）。

## スコープ

含む: 新「分析」タブ＋指標グリッド＋エクイティ/DDチャート＋R分布ヒストグラム（全て送信済データ）。
非対象(YAGNI): エッジ別ブレイクダウン(Phase 2)、文脈連動LOT(Phase 3)、range セレクタUI（既定 default_range を使用）、バックエンド/serialize 変更。

## 設計

### 配置（新タブ）
- `static/index.html` `.fund-tabs` に4つ目: `<button class="pill" data-fundtab="analytics">分析</button>`。
- `static/app.js`:
  - タブ dispatch（`paintFund` の `if (tab===...)` 連鎖）に `else if (tab === 'analytics') paintAnalyticsTab(snap);` を追加。
  - 新関数 `paintAnalyticsTab(snap)`。

### paintAnalyticsTab の中身
データ取得: `const p = snap && snap.performance; const adv = p && p.advanced; const br = p && p.by_range && p.by_range[p.default_range];`

**空状態**: `adv` 無し or `(br && br.trade_count) ` がゼロ/未定義 → `<div class="empty mute">約定履歴がまだありません</div>` を表示し以降を描かない。

**1. 指標グリッド** `.anlx-grid`（ラベル+値の小カード群、tabular-nums）:
- by_range[default] 由来: 取引数(trade_count), 勝率(win_rate×100%), PF(profit_factor), 平均勝(avg_win), 平均負(avg_loss), RR(risk_reward), 純損益(net_profit)。
- advanced 由来: Sharpe, Sortino, Calmar, Recovery(recovery_factor), Ulcer(ulcer_index), 最大DD(max_drawdown_abs/ underwater% は underwater_pct), VaR95(var_95), CVaR95(cvar_95), 最大連勝(max_win_streak), 最大連敗(max_loss_streak), 現在連続(current_streak)。
- null/NaN は `--` 表示（既存 fmt ヘルパに倣う）。

**2. エクイティ＆DDチャート** `.anlx-chart`（SVG, `viewBox="0 0 W H"` + `preserveAspectRatio="none"` + `vector-effect="non-scaling-stroke"`）:
- 上段: `advanced.equity_curve`（累積損益）を面＋線（緑/赤は最終値の符号 or 一律）。
- 下段(または同一SVG下部): `advanced.underwater_curve`（≤0 のDD）を赤面。
- 2要素は縦に積む（上=エクイティ、下=アンダーウォーター）。点数 <2 ならチャート省略。

**3. R分布ヒストグラム** `.anlx-bars`（SVG 縦棒）:
- `advanced.r_distribution`（{bucket: count}）。バケツをキー順に並べ、本数を棒高で。各棒の下にバケツ名。空 dict なら省略。

### CSS（`static/app.css`）
`.anlx-grid`（grid、小カード）、`.anlx-kpi`/`.anlx-kpi .k`/`.v`、`.anlx-chart`/`.anlx-svg`、`.anlx-bars`。mono・tabular-nums。配色は既存トークン（buy/sell/fg/border）。

## データフロー
WS full → `snap.performance`（送信済） → `paintFund` が `analytics` タブで `paintAnalyticsTab` → advanced/by_range から指標グリッド・SVGチャート・棒を描画。再描画は既存 fund 再描画サイクルに乗る。

## テスト/検証
- `node --check static/app.js`。
- 統合(feedback_integration_verify): ブラウザで
  1. `分析` タブが出る・クリックで切替（他タブと排他、localStorage 保持）。
  2. ライブ履歴ゼロ時は空状態が出る（現在 Balance=0）。
  3. **合成 performance 注入**（browser eval で `latestSnap.performance` に擬似 advanced/by_range/r_distribution/equity_curve をセット→`paintAnalyticsTab` 呼出）で、指標グリッド・エクイティ/DD・R分布の3ブロックが描画されることを確認。
  4. console エラー 0、mono 維持。

## 受け入れ基準
1. fund パネルに「分析」タブが追加され、他3タブと排他切替＋localStorage 保持。
2. 約定履歴がある時、指標グリッド（by_range+advanced）・エクイティ/DDチャート・R分布が描画される。
3. 履歴ゼロ時は空状態（エラーなし）。
4. バックエンド/serialize 変更なし（送信済データのみ使用）。
5. console エラー 0、フォント mono 維持。
