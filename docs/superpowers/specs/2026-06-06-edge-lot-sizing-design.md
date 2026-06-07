# 管理画面 Phase 3: 実績エッジ連動 適正Lot — 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち
> 管理画面ロードマップ Phase 3/3（Phase 1=分析タブ・Phase 2=エッジ表 完了）

## ゴール

資金管理タブに「実績ベース 適正Lot (¼Kelly)」を追加し、**実現エッジ（by_range の勝率/RR）から Kelly 経由で適正ロットを自動提示**する。現状は固定%(risk%)サイジングが主で、Kelly は別表示・手入力依存。これを実績駆動の権威ある推奨に昇格。**バックエンド変更なし**（送信済データ＋既存ヘルパのみ）。

## 背景(grounding)

- 資金管理タブ `_moneyFormHtml`/`_recalcMoney`/`_wireMoneyForm`。既存ヘルパ: `pipValueUsd(lots)`(=$10/pip/lot), `positionSizeLots(riskAmt, slPips, pipValPerLot)`, `kellyFraction(winRate, RR)`, `rrRatio`, `expectancyR`, `breakevenWR`, `riskOfRuin`。
- 実績は `snap.performance.by_range[default_range]`（win_rate, risk_reward, trade_count）。既に「勝率%」「RR(手入力)」へ default 流用＋pristine時 prefill 済。
- 口座は `snap.account`（balance, equity）。slPips = `|entry-sl|/0.10`（XAUUSD, 0.10=1pip）。
- 現「推奨Lot」(mm-lot) は `balance×risk% / (slPips×$10)` の固定%サイジング（Kelly 非連動）。

## スコープ

含む: 「実績ベース 適正Lot (¼Kelly)」バナー、サンプル警告、実績由来マーク。
非対象(YAGNI): バケツ別文脈連動(案B)、手入力フィールド削除、バックエンド/serialize 変更、自動発注。

## 設計

### 配置
資金管理タブ最上部（`.mm-grid` の上）に全幅バナー `<div class="mm-edge" data-bind="mm-edge"></div>` を追加（ヘッドライン）。`_recalcMoney` が毎入力で innerHTML を更新。

### 算出（`_recalcMoney` 内、既存ヘルパ再利用）
- `rw = byRange.win_rate`, `rr = byRange.risk_reward`, `nTr = byRange.trade_count`。
- 実績あり（rw/rr が有限）なら:
  - `f = kellyFraction(rw, rr)`（負＝エッジ無しは 0 にクランプ表示）。
  - `quarter = max(f,0)/4`。
  - `riskAmt = quarter * balance`（balance>0 のとき）。
  - `slPips = (entry,sl 有限) ? |entry-sl|/0.10 : NaN`。
  - `lot = (riskAmt>0 && slPips>0) ? positionSizeLots(riskAmt, slPips, pipValueUsd(1)) : null`。
- 表示文字列:
  - `実績(<range>, N=<nTr>取引)  勝率 <rw%>  RR <rr>  →  f*=<f%>  ¼=<quarter%>  →  適正Lot <lot> (リスク$≈<riskAmt>)`。
  - `lot` 不能時（SL未入力 or balance0）は Lot 部を「SL入力で算出」or「残高0」。
- **サンプル警告**: `nTr < 20` → 末尾に `⚠ サンプル少・参考程度`（`.mm-edge-warn`）。
- **実績なし**（rw/rr 無し or nTr=0）→ バナーは `実績データ不足（<range>に確定取引なし）`。

### 権威化マーク
既存ラベル「勝率 %」→「勝率 %(実績既定)」、「RR (手入力 or 計算値)」→「RR(実績既定/上書可)」の小注記のみ（フィールドは不変）。

### CSS
`.mm-edge`（accent 左枠 + 角丸 + padding、mono、tabular-nums）、`.mm-edge-warn`（sell色）。`.mm-edge-num`（強調値）。

## データフロー
WS full → `snap.performance.by_range` + `snap.account` → `_recalcMoney` が実績勝率/RR→`kellyFraction`→¼Kelly→`positionSizeLots`(entry/SL由来 slPips) → `.mm-edge` に適正Lot＋リスク$＋サンプル警告を描画。entry/SL/残高変化で再計算。

## テスト/検証
- `node --check static/app.js`。
- 統合(feedback_integration_verify): ブラウザで
  1. 合成 `account{balance}` + `by_range{win_rate,risk_reward,trade_count}` 注入＋entry/SL入力 → `.mm-edge` に f*/¼/適正Lot/リスク$ が出る。
  2. `trade_count<20` でサンプル警告。
  3. SL未入力 → Lot 部「SL入力で算出」。
  4. 実績なし(win_rate null) → 「実績データ不足」。
  5. console 0、mono 維持。

## 受け入れ基準
1. 資金管理タブに「実績ベース 適正Lot (¼Kelly)」バナーがあり、実績勝率/RR→f*/¼Kelly→適正Lot(entry/SL併用)を表示。
2. サンプル小(trade_count<20)で警告、実績なしで「実績データ不足」、SL未入力/残高0 を適切表示。
3. 既存「勝率/RR(手入力)」に実績由来の注記。手入力フィールドは温存。
4. バックエンド/serialize 変更なし（送信済データ＋既存ヘルパのみ）。
5. console 0、mono 維持。
