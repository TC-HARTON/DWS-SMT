# ② BIAS EMA20 統一 — 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち

## ゴール

TFトレンド/複合BIAS が参照する EMA を、全タイムフレーム(D1/H4/H1/M15)で **EMA20 に統一**する。現行は `config.TIMEFRAMES` で TF ごとに異なる期間(D1=200 / H4=50 / H1=20 / M15=13)。複合BIAS の重み・しきい値・レジームゲートは**変更しない**(範囲A)。

## 背景・動機

- 現行ダッシュボードの中央オシレーター(`analyzer/ema_stack.py`)は M15 単一系列の EMA20/80/320 で、EMA80≈H1-EMA20・EMA320≈H4-EMA20 という「EMA20 を各時間軸で見る」思想。
- 一方 TFテーブル/複合BIAS は TF ごとに別期間の EMA を使っており、オシレーターと物差しがズレている。
- 全TFを EMA20 に統一すると、TFテーブルとオシレーターが**同じ物差し**になり、後続の ①(16Y の EMA20 乖離率分布 → 過伸張バンド表示)の測定基盤が揃う。だから ② を ① より先に行う。

## スコープ

**範囲A（確定）**: EMA 期間のみ全TF=20。複合ロジック(`TF_WEIGHTS`・`tfSignal` の ADX25/RSI55/45・DI・`tfTrendFactor` レジームゲート)は現状維持。

非対象(YAGNI): 複合の重み・しきい値再設計(範囲B)、`bars_to_fetch` 変更、EMA%列ヘッダのラベル変更、`indicators.ema()` の seeding 変更。

## 現状(grounding)

- `config.py:101-115` `TimeframeSpec(label, mt5_const, ema_period, bars_to_fetch)`、`TIMEFRAMES` が D1=200/H4=50/H1=20/M15=13。
- `analyzer/indicator_engine.py` は各TFの `ema_period` を読み、EMA と `above_ema`(close vs その EMA)を算出。
- `static/app.js`:
  - `tfSignal()` (≈1152) … `tf.above_ema` + ADX/RSI/DI → −2..+2。
  - `compositeSignal()` (≈1183) … Σ(code × `TF_WEIGHTS{D1:3,H4:2,H1:1.5,M15:1}` × `tfTrendFactor`) を −10..+10 に正規化。
  - `_sigCellEma()` / `pctEmaDist()` (≈1204) … EMA%列 = (close−`tf.ema`)/`tf.ema`×100。
  - `tf.above_ema` も `tf.ema` も `ema_period` 由来 → 期間を変えれば方向・乖離%・シグナルが**自動追従**。

## 設計

### 変更1: config.py に共有定数を新設し全TFが参照

```python
# 全TF統一トレンドEMA(②, 2026-06-06)。SPEC 5/6.1 の TF別期間(D1=200/H4=50/H1=20/
# M15=13)から意図的に逸脱し、中央オシレーター(ema_stack: 全て EMA20 基準)と物差しを揃える。
TREND_EMA_PERIOD: Final[int] = 20

TIMEFRAMES: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("D1",  mt5.TIMEFRAME_D1,  TREND_EMA_PERIOD, 400),
    TimeframeSpec("H4",  mt5.TIMEFRAME_H4,  TREND_EMA_PERIOD, 300),
    TimeframeSpec("H1",  mt5.TIMEFRAME_H1,  TREND_EMA_PERIOD, 240),
    TimeframeSpec("M15", mt5.TIMEFRAME_M15, TREND_EMA_PERIOD, 200),
)
```

採用理由: 「統一」を単一ノブで自己文書化し、1本だけ書き換わって統一が崩れる事故を防ぐ。①も同じ定数を参照可能。

### 変更2: それ以外コード変更なし

`indicator_engine` / `app.js` は `tf.ema`・`tf.above_ema` 経由で自動的に EMA20 基準になる。複合ロジックは不変。

## データフロー(変更後)

`config.TREND_EMA_PERIOD=20` → `indicator_engine` が各TFで EMA20 と `above_ema`(close vs EMA20) を算出 → WS full スナップ → `app.js`: TFテーブル各行 = 「EMA20 上/下 ＋ EMA20 乖離%」、複合BIAS が EMA20 ベースの方向で再計算。

## 意味論の帰結(承知の上)

D1 も EMA20(≈過去1ヶ月)基準になり、長期トレンド軸(EMA200)は消える。4TFが「各時間軸の EMA20 上下」= モメンタムの梯子になり、現行オシレーターと同じ物差しに揃う。これは設計意図であり、デメリット(長期トレンド軸の喪失)も承知済み。

## 既知の小さな非整合(無害・明記のみ)

TFテーブルの EMA は `indicators.ema()`＝SMA seeded、オシレーターは pandas `ewm`＝first-value seeded。EMA20 かつ各TF ≥200本取得では収束後の値が実質一致するため「同じ物差し」は実用上成立。今回は触らない。

## テスト

- 新規 `tests/test_config_trend_ema.py`: 全 `TIMEFRAMES` の `ema_period == config.TREND_EMA_PERIOD == 20` を assert(統一不変条件のガード)。
- 既存 `tests/test_indicator_engine.py` / `tests/test_state_and_serialize.py` はそのまま緑(特定期間の assert は無い)。
- 統合検証(feedback_integration_verify 準拠): サーバを PID kill→`main.py` 再起動 → ブラウザで TFテーブル各 EMA% と複合BIAS が EMA20 基準に変わったことを確認、console エラー 0 / network 失敗 0。

## 受け入れ基準

1. `config.TREND_EMA_PERIOD == 20` かつ全TFがそれを参照。
2. ダッシュボードの TFテーブル D1/H4 行の EMA% が、変更前(EMA200/EMA50 基準)と異なり EMA20 基準の値になる。
3. 複合BIAS が EMA20 ベースの `above_ema` で再計算される。
4. pytest 全緑(新規ガードテスト含む)。
5. サーバ実稼働でエラーなし。

## ①への接続

② 完了後、各TFは EMA20 乖離% を表示する。① はこの EMA20 乖離の 16Y 分布(ルートの Dukascopy CSV)を計算し、パーセンタイル帯(95/99%tile 等)で「過伸張・反転示唆」を表示する。② が ① の測定基準を確定させる。
