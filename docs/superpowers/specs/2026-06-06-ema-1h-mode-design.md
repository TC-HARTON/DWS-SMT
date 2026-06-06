# EMA-stack 1H モード + M15/1H タブ切替 — 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち
> 前提: ②(全TF EMA20統一)・①(乖離率 過伸張バンド p95/p99 着色) 実装済

## ゴール

中央 EMA-stack オシレーターに **1H モード**を追加し、**M15 / 1H タブ**で切り替える。1H モードは EMA20(1H) / EMA80(≈4H) / **EMA480(≈D1, 中心線)**。周期・中心線以外は M15 モードと完全に同一（ズーム/パン/ドラッグ/日次区切り/中心線/塗り/hover/過伸張 p95/p99 着色すべて）。

## 周期マッピング（M15 設計と同一思想）

| モード | fast | mid | center | 思想 |
|---|---|---|---|---|
| M15(現行) | EMA20 (M15) | EMA80 (≈H1=20×4) | EMA320 (≈H4=20×16) | fast=EMA20・mid=4×・center=上位足換算 |
| **1H(新規)** | EMA20 (1H) | EMA80 (≈4H=20×4) | **EMA480 (≈D1=20×24)** | 同上 |

単一系列・因果EMA・確定足のみ＝リペイント無は両モードで維持（マルチTF写像なし）。

## スコープ

含む: 1H モードの計算・配信・タブ UI・1H 過伸張バンド生成・着色。
非対象(YAGNI): 3モード目、タブ装飾アニメ、H1 バンドの recency 加重。

## アーキテクチャ（A1: 両モードをサーバ計算し両方配信）

両モードを毎解析サイクルで計算し、ライブ snapshot に両方同梱。深掘り履歴は `/api/ema_history?tf=M15|H1` でモード別取得。タブ切替はライブ分即時、深掘りはモード別キャッシュ。追加コストは毎サイクル `copy_rates(H1, ~3000)` 1回（数ms）。

## コンポーネント

### 1. config.py — 2モード定義
- 既存 `EMA_STACK_TF`/`EMA_STACK_PERIODS`(M15, (20,80,320)) は M15 既定として残しつつ、モード表を追加:
  - `EMA_STACK_MODES`: 順序付き = `(("M15", "M15", (20,80,320), fetch_m15, display_m15, hist_fetch_m15, hist_m15), ("H1", "H1", (20,80,480), 3000, 480, 20000, 20000))` 形式（モード名, MT5 TFラベル, periods, live fetch, live display, history fetch, history bars）。
  - 具体値: M15 = 現行(1500/480/20000/20000)。H1 = fetch 3000・display 480・history 20000・hist_fetch 20000。
- 値の根拠: EMA480 は ~4.7×=2256 本で十分収束 → live fetch 3000。display 480 本(H1)≈20 営業日。

### 2. analyzer/ema_stack.py — モード引数化
- `compute_ema_stack(connector, *, tf, periods, fetch_bars, display_bars, mode)` に一般化（現状は config グローバル直参照）。
- `EmaStackSnapshot` に `mode: str` を追加（"M15"/"H1"）。`bands` は `load_bands(mode)`。
- `_ema_tf_const()` は引数 tf を解決（既存 `TIMEFRAME_BY_LABEL` 利用）。
- 後方互換: 既存呼び出し（引数なし）は M15 既定にフォールバック。

### 3. analyzer/analysis_loop.py — 両モード計算
- `_do_analysis` で M15・H1 両方を計算し state にセット:
  - `state.set_ema_stack(compute M15)` / `state.set_ema_stack_h1(compute H1)`。
- 例外は各モード独立にガード（片方失敗でもう片方は生存）。

### 4. analyzer/state.py + dashboard/serialize.py — 両 stack 保持/配信
- state に `_ema_stack_h1` + `set_ema_stack_h1` + プロパティ追加。snapshot_to_json に `"ema_stack_h1"` 追加（既存 `"ema_stack"`=M15 は不変＝後方互換）。
- `serialize_ema_stack` は共通（mode 非依存、`bands`/`periods` を含むので両対応）。

### 5. disparity_bands + 生成器 — H1 バンド
- JSON 再編: `data/ema_disparity_bands.json` を `{"generated_at":..., "modes": {"M15": {"tf":"M15","periods":[20,80,320],"bands":{...}}, "H1": {"tf":"H1","periods":[20,80,480],"bands":{...}}}}`。
- `load_bands(mode="M15")` が該当モードの `bands` を返す（キャッシュはモード別）。欠如時 None。
- 生成器 `gen_ema_disparity_bands.py`: M15(15 Mins CSV) と H1(`XAUUSD_Hourly_Bid` CSV) 両方を計算して書き出し。
- `compute_bands(closes, periods)` は汎用なので (20,80,480) で再利用可。

### 6. static/app.js + app.css — タブ + 動的ラベル/キー
- タブ UI: readout 内に `M15 / 1H` ピル（`.pill` 流用）。`el._emaMode`（localStorage `mt5-ematf`、既定 "M15"）。
- `paintEmaStack`: `mode = el._emaMode`; `live = mode==='H1' ? snap.ema_stack_h1 : snap.ema_stack`; `d = EMA_HISTORY[mode] || live`。
- `EMA_HISTORY` をモード別キャッシュ `{}` 化。`fetchEmaHistory(mode)` が `/api/ema_history?tf=<mode>` を取得し `EMA_HISTORY[mode]` に格納。タブ切替時に未取得なら fetch。
- ラベル/キー動的化: readout のドット名は `EMA${data.periods[i]}`、bands キーは `ema${data.periods[i]}`（M15→...320 / 1H→...480 自動）。`emaOxTier` の引数 band は `bands['ema'+periods[i]]`。
- 共通コード（_emaRender/_emaHover/_emaWheel/_emaDrag/塗り/区切り/中心線）は不変で流用。
- タブ切替で `el._emaStamp=null`（再描画強制）+ 必要なら fetchEmaHistory(mode)。

### 7. dashboard/lite_server.py — /api/ema_history に tf param
- `?tf=M15|H1`（既定 M15）。tf に応じて該当モードの spec で `compute_ema_stack` を deep params で実行。未知 tf は 400 か M15 フォールバック（M15 フォールバックを採用＝安全）。

## データフロー
(ライブ) loop が M15+H1 計算 → state → snapshot に両 stack → フロントが active モードを選択描画。
(深掘り) フロント `/api/ema_history?tf=<mode>` → サーバが該当 spec で計算 → フロントがモード別キャッシュ。
(バンド) オフライン生成器が M15(15Mins)+H1(Hourly) を 1 JSON に → サーバ起動ロード → 両 stack に同梱 → フロントが active モードの bands で着色。

## テスト
- `compute_bands((20,80,480))`: 構造/順序（汎用関数、既存テストで担保＋1ケース追加）。
- 生成器/artifact: JSON が `modes.M15`/`modes.H1` 両方を持ち、各 3EMA×pos/neg×(p90/p95/p99/max/n)、H1 のキーは ema20/ema80/ema480。
- `load_bands("H1")`/`load_bands("M15")`/欠如時 None。
- `compute_ema_stack(tf="H1", periods=(20,80,480), ...)`: center EMA が 480 周期、`mode=="H1"`、`bands` が H1。
- serialize: snapshot に `ema_stack` と `ema_stack_h1` 両方。
- フロント: `node --check`。統合: サーバ再起動→ブラウザで M15/1H タブ切替、1H で readout が EMA480 表示・過伸張着色が H1 バンドで発火、ズーム/hover 動作、console 0。

## 受け入れ基準
1. config に M15/H1 両モード定義、H1=(20,80,480)。
2. ライブ snapshot に `ema_stack`(M15) と `ema_stack_h1`(H1) が同梱。
3. `data/ema_disparity_bands.json` が `modes.M15`/`modes.H1` を持ち、H1 は ema20/80/480。
4. フロントに M15/1H タブ、切替で系列・readout ラベル(EMA480)・bands 着色・深掘り履歴が 1H に切り替わる。
5. 1H もリペイント無（単一H1系列・確定足・因果EMA）。
6. pytest 全緑（新規 H1 ケース含む）。サーバ実稼働でタブ切替動作・console 0。

## リスク/留意
- H1 深掘り 20000 本はブローカー供給に依存（足りなければ取得可能分で描画＝既存挙動）。
- JSON スキーマ変更（modes 化）で artifact テスト/`load_bands` を更新。旧 M15 専用 JSON を読む箇所は他に無い（grep 確認済の前提で実装時に再確認）。
