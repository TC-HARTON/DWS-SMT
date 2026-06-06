# ① EMA 乖離率 歴史的過伸張バンド — 設計書

> 作成: 2026-06-06 / ステータス: 承認済(設計) → 実装計画待ち / 前提: ②(全TF EMA20統一)完了済

## ゴール

過去16Y(Dukascopy M15)から、オシレーター readout が表示する3乖離率(price vs EMA20/EMA80/EMA320)の歴史的分布を求め、現在値が歴史的に大きい(過伸張)時に**既存 readout をカラー強調/点滅**させて視覚認識できるようにする。新規UI・新規パネルは作らない。

## 動機

「過剰トレンド・反転示唆」を一目で認識し、無駄打ち防止＋逆張り判断に使う。新しい表示物は不要で、既に見ている乖離率が「歴史的にどれだけ行き過ぎか」を色/点滅で示せれば良い(ユーザ確定)。

## 乖離率の定義(チャートと完全一致)

オシレーター readout の式に合わせる(`static/app.js:833`):

```
乖離率(e) = (price − e) / e × 100        # e ∈ {EMA20, EMA80, EMA320}
```

- `price` = M15 最終確定足 close。`EMA` = `ema_stack._ema` と同一の **first-value seeded ewm**(`pd.Series(x).ewm(span=period, adjust=False).mean()`)。
- 3本とも M15 単一系列(EMA80≈H1-EMA20 / EMA320≈H4-EMA20 の period-multiple proxy)。リペイント無関係(静的統計)。

## スコープ

含む: 16Y 分布のオフライン算出 → 小 JSON → サーバ配信 → フロントで readout 色/点滅。
非対象(YAGNI): チャート上の水平バンド、新パネル、4TF個別バンド、recency 加重、ライブ再計算。

## アーキテクチャ(案1: オフライン生成 → committed JSON → 配信)

重い16Y計算は一度きりオフライン。実行時はバンド値を読んで現在乖離率と比較するだけ。oos_baseline の静的リファレンス先例に倣うが極小(数十数値)。

### コンポーネント1: 生成器 `scripts/gen_ema_disparity_bands.py`

- ASCII 限定(Windows cp932)。維持されるツール(使い捨て研究スクリプトではない)。
- 入力: `XAUUSD_15 Mins_Bid_2010.01.01_2025.12.31.csv`(`Time (EET),Open,High,Low,Close,Volume`)。
- 手順:
  1. `Close` 系列を読む。
  2. EMA20/80/320 を `ewm(span=p, adjust=False)` で算出(ema_stack と同一)。
  3. warmup 除外: 先頭 `EMA_STACK_PERIODS` の center(320) 本を捨て、以降の確定足のみ。
  4. 各足で3乖離率 `(close−EMA)/EMA×100` を計算。
  5. **EMA別×符号別**(pos = 乖離率>0, neg = 乖離率<0)に分け、各群で `p90/p95/p99`(絶対値ベース)・`max`(符号側の極値)・`n` を算出。
  6. `data/ema_disparity_bands.json` に書き出し。
- 実行は CSV のあるユーザ機で手動。新データ追加時に再生成。

### コンポーネント2: JSON スキーマ `data/ema_disparity_bands.json`(committed・極小)

```json
{
  "generated_at": "2026-06-06T...",
  "source": "Dukascopy XAUUSD M15 Bid 2010.01.01-2025.12.31",
  "tf": "M15",
  "periods": [20, 80, 320],
  "bands": {
    "ema20":  {"pos": {"p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "n": 0},
               "neg": {"p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "n": 0}},
    "ema80":  {"pos": {...}, "neg": {...}},
    "ema320": {"pos": {...}, "neg": {...}}
  }
}
```

- `pos`/`neg` の `p90/p95/p99/max` は**絶対値%**(常に正)。`neg.max` は最も負側に伸びた絶対値。

### コンポーネント3: サーバ(ロード + 配信)

- 起動時に1回 JSON をロード(`analyzer/ema_stack` に小ローダ、または専用 `analyzer/disparity_bands.py`)。
- `EmaStackSnapshot` に `bands: dict | None` フィールドを追加し同梱。静的・極小なので full スナップ毎同梱でも WS 膨張なし。
- JSON 欠如/不正時は `bands=None`(機能オフ、既存表示は不変・エラーにしない)。
- `dashboard/serialize.serialize_ema_stack` が `bands` を出力に含める。

### コンポーネント4: フロント `static/app.js paintEmaStack`

- 既存 `dr(e)` の各乖離率値について、符号でバンド群(pos/neg)を選び、`|乖離率|` でティア判定:
  - `|v| ≥ p99` → クラス `ema-overext-x`(warn色 + 点滅)。
  - `|v| ≥ p95` → クラス `ema-overext`(warn色・強調)。
  - それ未満 → 現状(`pos`/`neg` 色)のまま。
  - `bands == null` → 一切変更なし。
- 対象は readout の EMA20/EMA80/EMA320 各 `sp(dr(...))` 表示。
- `static/app.css` に `.ema-overext` / `.ema-overext-x` / `@keyframes ema-blink` を追加。**フォントは mono 維持**(数値は tabular-nums)。

## データフロー

(オフライン) CSV → `gen_ema_disparity_bands.py` → `data/ema_disparity_bands.json`。
(実行時) JSON → サーバ起動ロード → `EmaStackSnapshot.bands` → full WS → フロントがキャッシュ → 現在乖離率と比較し色/点滅。

## ティア閾値(既定・承認済)

`p95` = 色強調 / `p99` = 点滅。`config` に定数化(`DISPARITY_WARN_PCTL`/`DISPARITY_BLINK_PCTL` 相当)してフロントへ配るか、フロント定数。→ JSON が p95/p99 を持つので、フロントは「現在値 ≥ その閾値か」を見るだけ。閾値定数は不要(JSON のキーで表現)。

## 承知の前提(v1)

- ①バンド = Dukascopy、ライブ = TitanFX。乖離率は%なので価格水準/フィード差に頑健。
- 16Y レジーム差(金 $1000→$4300)は%乖離で吸収。recency 加重は将来オプション。

## テスト

- 生成器単体 `tests/test_gen_ema_disparity_bands.py`: 合成 close 系列(既知乖離率)で、出力 JSON 構造と pos/neg 分離・パーセンタイル順序(p90≤p95≤p99≤max)を検証。
- serialize: スナップに `bands` が載る/`bands=None` 時の形(`tests/test_state_and_serialize.py` か新規)。
- スキーマ: committed `data/ema_disparity_bands.json` が期待スキーマ(3 EMA × pos/neg × p90/p95/p99/max/n)を満たす。
- フロント: `node --check static/app.js`。
- 統合(feedback_integration_verify): サーバ再起動 → ブラウザで、乖離率が大きい EMA の readout 値が warn色/点滅、平常時は不変、console 0。

## 受け入れ基準

1. `data/ema_disparity_bands.json` が3EMA×pos/neg×(p90/p95/p99/max/n)で生成される。
2. サーバが起動時にロードし full スナップに `bands` を同梱(欠如時 `None` でエラーなし)。
3. readout の各乖離率が `|v|≥p95` で色強調、`|v|≥p99` で点滅。`bands=null` で従来表示。
4. 乖離率の式・EMA seeding が既存 readout と完全一致。
5. pytest 全緑(生成器/serialize/スキーマ)。サーバ実稼働でエラーなし。
