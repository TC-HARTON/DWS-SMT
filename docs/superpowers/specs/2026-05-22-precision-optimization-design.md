# 精度最適化 設計書 — マクロ層 ＋ シグナル検証層

- 日付: 2026-05-22
- 対象: MT5 トレーディングダッシュボード (`C:\Users\ohuch\Desktop\MT5_Python`)
- 目的: 専業トレーダーが満足する「精度」へ最適化する。機関向け水準は狙わない。

## 1. 背景と目標

FX 情報サービスとしての検証で、エンジニアリング品質（レイテンシ・no-repaint 規律・
誠実な統計）は既にプロ水準。評価を縛っているのは **データの幅** だった。
本設計はそのうち専業トレーダーの「精度」に直結する 2 軸を閉じる:

1. **マクロ層** — FX トレンドの根本ドライバである金利差（キャリー方向）を取り込み、
   参照パネルで可視化し、逆行する BIAS/DWS トリガーを減点・警告する。
2. **シグナル検証層** — BIAS/DWS-SMT に統計的な優位性が本当にあるかを深い履歴で
   計測し、各シグナルに常時「信頼度」を表示する。

スコープ外（合意済み）: カレンダー鮮度、価格フィードのサニティチェック、
シグナルの自動重み調整（過学習リスクのため除外）、OIS/金利先物（金利期待モメンタム）。

実装順: **検証層 → マクロ層**（計測してから調整、というファンドマネージャーの原則）。

## 2. 全体アーキテクチャ

```
analysis_loop の独立スケジュール:
  price(0.5s) / analysis(5s) / heavy(30s) / history(60s) / calendar(1h)
  + macro(6h,  off-thread worker)      ← 新規
  + validation(5min, off-thread worker) ← 新規
state: + macro, + validation  （いずれも heavy domain → analysis_version をバンプ）
WS:   既存の light/full 分離に従う。macro/validation の変化は full を送る。
```

両 worker は calendar と同じパターン（`threading.Event` の in-flight ガード ＋
daemon worker thread）。

**重要な訂正（統合テストで判明）**: 「off-thread worker だから価格 tick を
ブロックしない」は誤り。MT5 connector は全スレッドで単一ロックを共有するため、
深い履歴 fetch がそのロック（と GIL）を数秒占有すると価格 tick が飢餓状態になる。
検証 worker は次の3点で*スロットル*する必要がある:
- 1通貨ずつ fetch し、通貨間に `VALIDATION_FETCH_GAP_SEC` のポーズを入れる
- 上位 TF（D1/W1）の fetch 本数は実在範囲に制限（`VALIDATION_TF_BARS`）。
  数千本の D1/W1 要求はブローカーの空同期を招きロックを長時間占有する
- 検証対象は DWS 表示通貨（`config.SYMBOLS`）のみ。強弱用クロスは除外
初回パスは起動 `VALIDATION_STARTUP_DELAY_SEC` 後に遅延させ、ウォームアップと
衝突させない。

---

# セクション A — シグナル検証層（先に実装）

## A.1 目的と前提

各シグナルに常時「信頼度メトリクス」を表示し、96 バーのバックテストがノイズか
実エッジかを可視化する。

**前提（正直な framing）**: BIAS と DWS-SMT は**ルールベース・パラメータなし**の
シグナル。学習する対象がない。よって「ウォークフォワード」とは固定ルールを大量の
履歴で評価し、サブ期間で安定性を確認することを指す。train/test ではなく
「浅い直近窓 vs 深い履歴」の検証である。

## A.2 検証内容

深い履歴（`VALIDATION_HISTORY_BARS` 本、初期値 5000 closed bars / TF）で既存の
シグナルルールを再評価する。`(symbol × base-TF)` ごとに以下を算出:

- 取引数 `n_trades`（コスト控除後）
- 勝率 `win_rate` ＋ **Wilson 95% 信頼区間** `ci_low` / `ci_high`
  （小 N で偏らないよう正規近似でなく Wilson スコア区間を用いる）
- プロフィットファクタ `profit_factor`
- 期待値／取引 `expectancy`（ポイント、コスト控除後）
- 最大ドローダウン `max_drawdown`
- 平均 MAE `avg_mae`
- **安定性 `thirds`**: 履歴を 3 等分し、サブ期間ごとの勝率・期待値を算出。
  エッジが一貫しているか前半偏りかを判定する。
- **レジーム別 `regime`**: エントリー時 ADX でトレンド/レンジに分割。
  指標エンジンが既に ADX を計算済みのため低コスト。
- **ティア `tier`**: `信頼` / `要注意` / `データ不足`

## A.3 ティア判定ロジック

- **信頼**: `n_trades >= VALIDATION_MIN_TRADES` ∧ `ci_low > breakeven`
  ∧ `thirds` の 3 期間すべて期待値プラス
- **要注意**: エッジはあるが不安定（thirds のいずれかがマイナス）または低 N
- **データ不足**: `n_trades < VALIDATION_MIN_TRADES`

`breakeven` 勝率は既存バックテストのコストモデルから算出（純損益ゼロとなる勝率）。

## A.4 2 層の接続 — マクロフィルタ自体を検証する

検証器は **生シグナル** と **マクロフィルタ適用後** の両方を評価する。
`ValidationStats` は `raw` と `macro_filtered` の 2 系列を持つ。
パネルに「マクロフィルタ: 勝率 +X%」を表示し、セクション B のフィルタが本当に
エッジを上げるかを実証/反証する。これでマクロ層と検証層が一体化する。

（実装順は検証層が先のため、初版では `macro_filtered` 系列は `raw` と同値を返し、
セクション B 完了時に実値を埋める。プレースホルダではなく「マクロ未適用時は
両系列が一致する」という正しい初期状態。）

## A.5 データモデル

```python
@dataclass(frozen=True)
class SubPeriodStats:        # thirds の 1 要素
    win_rate: float
    expectancy: float
    n_trades: int

@dataclass(frozen=True)
class RegimeStats:           # trend / range それぞれ
    win_rate: float
    expectancy: float
    n_trades: int

@dataclass(frozen=True)
class ValidationStats:
    symbol: str
    base_tf: str
    n_trades: int
    win_rate: float
    ci_low: float
    ci_high: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    avg_mae: float
    thirds: tuple[SubPeriodStats, SubPeriodStats, SubPeriodStats]
    regime_trend: RegimeStats
    regime_range: RegimeStats
    tier: str               # "信頼" | "要注意" | "データ不足"
    # raw / macro_filtered の 2 系列。上記フィールドは raw 値。
    # macro_filtered は同型のサブ構造として保持する。

@dataclass(frozen=True)
class ValidationSnapshot:
    generated_at: float
    compute_ms: float
    by_symbol: dict[str, dict[str, ValidationStats]]   # sym -> base_tf -> stats
```

## A.6 計算コストと cadence

深い履歴（5000 バー × 3 TF × 10 通貨）の評価は数百 ms オーダー。
5s / 50ms 予算には収まらない。

→ 新スケジュール `validation`（`VALIDATION_REFRESH_SEC = 300`、5 分ごと）。
深い履歴の fetch ＋ 評価を **worker thread** で実行する（calendar と同パターン、
in-flight ガードで重複起動を防ぐ）。価格 1 秒 tick を絶対にブロックしない。

## A.7 ファイル構成

- 新規 `analyzer/signal_validator.py`
  — 深い履歴評価。`dws_smt._pair_trades` と BIAS 寄与ロジック
  (`indicator_engine._bias_contribution_series`) を再利用する。
- 新規 `tests/test_signal_validator.py`
- `config.py`: `VALIDATION_REFRESH_SEC=300`、`VALIDATION_HISTORY_BARS=5000`、
  `VALIDATION_MIN_TRADES`（初期値 30）、ティア閾値定数
- `analyzer/state.py`: `set_validation` / `validation` プロパティ追加。
  `set_validation` は `_analysis_version` をバンプする。
- `analyzer/analysis_loop.py`: `_Schedule("validation", ...)` 追加、
  `_do_validation_refresh` ＋ `_validation_refresh_worker`（off-thread）
- `dashboard/serialize.py`: `serialize_validation()` 追加、snapshot に組み込み
- `static/app.js` ＋ `static/app.css`: DWS パネルに信頼度メトリクス表示
  （base-TF ごと: ティアバッジ ＋ N ＋ 勝率 CI ＋ PF ＋ 期待値 ＋ 安定性）

## A.8 エラー処理

- `(symbol, TF)` ごとの深い履歴 fetch 失敗 → そのセルは `tier="データ不足"`、
  クラッシュなし。
- worker は `(symbol, TF)` ごとに `try/except`（具体例外: `MT5ConnectionError`,
  `ValueError`）で隔離。
- worker 全体は daemon、例外は loop に伝播しない（calendar worker と同じ規律）。
- オフスレッドのため価格/解析パスを阻害しない。

## A.9 テスト

- 深い履歴での取引ペアリング（既存 `_pair_trades` の deep-history 適用）
- Wilson 95% 信頼区間の数値（既知入力に対する期待値）
- 3 分割安定性（境界バー数で off-by-one がないこと）
- レジーム分割（ADX 閾値での trend/range 振り分け）
- ティア判定の境界（`VALIDATION_MIN_TRADES`、`ci_low` 境界）
- raw vs macro_filtered の差分計算（初版は同値）
- 履歴不足時のフォールバック（`データ不足` を返しクラッシュしない）

---

# セクション B — マクロ / 金利差層

## B.1 目的

FX トレンドの根本ドライバである **金利差（キャリー方向）** を取り込み、
(a) 参照パネルで可視化し、(b) これに逆行する BIAS/DWS トリガーを減点・警告する。

## B.2 データソース（精度優先で一次ソースを採用）

通貨は USD / EUR / GBP / JPY / AUD の 5 つ（XAU は金利なし＝特別扱い）。

| 通貨 | ソース | 取得値 |
|---|---|---|
| USD | FRED `DFEDTARU` | FF 金利上限 |
| EUR | ECB Data Portal SDMX `FM` dataflow | 中銀預金ファシリティ金利 |
| GBP | BoE IADB `IUDBEDR` | Bank Rate |
| JPY | BoJ 時系列ページ `fm01_d_1_en.html` | 無担保コールO/N（政策金利プロキシ） |
| AUD | FRED `IRSTCI01AUM156N` | RBAコール/インターバンクO/N（RBA直サイトはbot遮断） |
| US 雇用 | FRED `PAYEMS` / `UNRATE` | NFP 月次変化・失業率 |

## B.11 実質利回りレイヤー（追補 — 2026-05-22 承認）

**動機**: 政策金利は階段関数（発表時のみ変動、間は固定）。対して市場の実質利回りは
日々変動する生きた信号で情報量が大きい。特に金（XAUUSD）は実質利回りで動く。

- ソース: FRED `DFII10`（10年TIPS実質利回り、日次）。米のみ（他国は市場実質利回りの
  クリーンなFRED系列が乏しいため）。
- cadence: 政策金利は低速6h、**実質利回りは別系統の高速1hスケジュール**で取得。
  FRED `DFII10` は1営業日1値のため1hポーリングで実用上ほぼ即時。真のイントラデイは
  債券マーケットフィードが必要でスコープ外。
- データモデル `RealYieldSnapshot`: value / prev_value / change_1d / trend_5d /
  gold_dir / as_of / stale / generated_at。`gold_dir = -sign(trend_5d)`
  （実質利回り上昇＝金に逆風＝−1）。
- 用途: (a) XAUUSD のマクロ方向を「名目金利トレンド」から `gold_dir` へ差替、
  (b) マクロパネルに「米実質利回り ＋X.XX%（前日比±）」を常時表示。

## B.12 カレンダー前方ホライズン（追補 — 2026-05-22 承認）

**問題**: Forex Factory の無料 XML は今週分のみ（nextweek/thismonth は 404）。
MT5 内蔵カレンダー API もこのビルドに非搭載。よって今週分が発表済みになると
カレンダーが空になる。

**解決**: `CalendarEngine.compute()` が毎回、前方の「次の主要イベント」を追加:
- 次回 FOMC — 連邦準備制度の公表会合スケジュール（`config.FOMC_MEETING_DATES`、
  年次更新）。
- 次回 NFP — FRED リリースカレンダー API（release 50 = Employment Situation）。
  既存の FRED キーを再利用。
- ライブフィードが同 UTC 日を既にカバーする場合は重複を抑止。

これでカレンダーは常に次の FOMC・NFP をカウントダウン表示する。

US 雇用は BLS CES と同一系列のため、API キーを増やさず FRED に統一する。
FRED 以外の政策金利は各中銀の一次ソース＝FRED プロキシより正確。
各ソースは独立したアダプタ関数として実装し、1 つの障害が他に波及しないこと。

API キーは環境変数（`FRED_API_KEY`）から `config._get_env` で読む。
キーはコミットしない。

## B.3 データモデル

```python
@dataclass(frozen=True)
class MacroRate:
    currency: str
    rate: float             # %
    as_of: str              # ISO 日付
    prev_rate: float | None
    source: str
    stale: bool             # 直近 fetch 失敗時 True

@dataclass(frozen=True)
class MacroEmployment:
    nonfarm_change: float | None
    unemployment_rate: float | None
    as_of: str
    prev_nonfarm_change: float | None
    prev_unemployment_rate: float | None
    source: str

@dataclass(frozen=True)
class MacroSnapshot:
    generated_at: float
    fetched_at: float
    rates: dict[str, MacroRate]        # ccy -> MacroRate
    employment: MacroEmployment | None
    last_error: str | None
    consecutive_failures: int
```

## B.4 1 ペアごとのマクロバイアス

ペア BASE/QUOTE について:

- `differential = rate(BASE) - rate(QUOTE)` … キャリー方向
- `macro_dir ∈ {-1, 0, +1}` … `differential` の符号。直近に実際の利上げ/利下げ
  （`prev_rate != rate`）があれば、その方向を強める。
- **XAUUSD 特別扱い**: 金は金利を持たない。US 金利の水準・トレンドを反転させて
  バイアス化する（US 金利↑ ＝ 金に逆風 → XAUUSD の `macro_dir` はマイナス寄り）。

**正直な限界（明記）**: 政策金利からは「キャリー方向 ＋ 実際の政策変更」しか
取れない。市場が織り込む金利期待のモメンタム（OIS/金利先物が必要）はスコープ外。
よって本フィルタは「構造的キャリー整合」フィルタであり、逆キャリーのシグナルを
捕まえる用途に有効。金利期待分析の代替ではない。

## B.5 シグナルへのフィルタ適用

- BIAS 合成スコアの定義は **純技術指標のまま変えない**（定義の純度を保つ）。
- 減点は **トリガー層** で行う（rec1+2 の BIAS フィルタと同じ場所）:
  - 各 DWS トリガーに `macro_aligned ∈ {-1, 0, +1}` を付与する。
  - 逆キャリーのトリガー（`macro_aligned == -1`）→ 「マクロ逆行」フラグ ＋
    確信度ティアを 1 段階ダウン（＝減点）。透明性のため表示は残し、視覚的に減衰。
  - マクロデータが `stale` なペア → `macro_dir = 0`（不正データで誤って
    減点しない）。
- マクロスナップショットは analysis ループが state から読み、`engine.compute()`
  に渡す。`indicator_engine` / `dws_smt` がトリガー生成時に整合を計算する。

## B.6 参照パネル

ペアごとのテーブル: BASE 金利・QUOTE 金利・金利差・マクロ方向、
＋ US 雇用コンテキスト行（NFP 月次変化・失業率）。

## B.7 リフレッシュ cadence

政策金利は年 8 回程度・予定日にしか変わらず、雇用は月次。
`MACRO_REFRESH_SEC = 21600`（6 時間）で十分。決定を同日に捕捉でき、低コスト。
calendar と同じくディスクキャッシュし、再起動時に即表示する。
fetch はオフスレッド worker。

## B.8 ファイル構成

- 新規 `analyzer/macro_feed.py`（取得・解析・キャッシュ・ペアごとバイアス）、
  `tests/test_macro_feed.py`
- `config.py`: 各 API URL・系列 ID・`MACRO_REFRESH_SEC=21600`・
  キャッシュファイルパス・`FRED_API_KEY` env 読み込み
- `analyzer/state.py`: `set_macro` / `macro` プロパティ追加。
  `set_macro` は `_analysis_version` をバンプする（変更は稀）。
- `analyzer/analysis_loop.py`: `_Schedule("macro", ...)` 追加、
  `_do_macro_refresh` ＋ `_macro_refresh_worker`（off-thread）。
  `_run_analysis_pass` で `self._state.macro` を読み `engine.compute()` に渡す。
- `analyzer/indicator_engine.py` / `analyzer/dws_smt.py`:
  トリガーに `macro_aligned` を適用、逆行トリガーをティアダウン。
- `dashboard/serialize.py`: `serialize_macro()` 追加、トリガーに
  `macro_aligned` フィールド追加。
- `static/app.js` ＋ `static/app.css`: 参照パネル ＋ 逆行マーカーの styling。

## B.9 エラー処理

- ソースごと独立。1 つ失敗 → その通貨のみ `stale=True` 表示、他は無事。
- HTTP 失敗は具体例外（`requests.RequestException`）で捕捉、リトライ後フォールバック。
- オフスレッド fetch で価格 1 秒 tick を絶対にブロックしない。
- ディスクキャッシュで再起動時に即表示。`consecutive_failures` を追跡。

## B.10 テスト

- 各ソース形式のパース（FRED JSON / ECB CSV / BoE CSV / BoJ JSON / RBA CSV
  のフィクスチャ）
- ペアごとのマクロバイアス計算
- XAUUSD 特別扱い（US 金利反転）
- `stale` 通貨 → `macro_dir = 0`
- トリガー減点ロジック（逆行トリガーのティアダウン）

---

## 3. 横断的な実装規律

プロジェクトメモリ（`feedback_no_shortcuts.md`）に従う:

- 手抜き・簡素化・後回しスタブ禁止。常に目的最適・SPEC 通りの完全実装。
- bare `except` 禁止 — 具体例外を捕捉する。
- 型ヒント ＋ docstring 必須。
- テストは実際に pass すること（現状 157 tests green を維持）。
- プレースホルダ UI 禁止。

## 4. 受け入れ条件

- 全テスト green（既存 157 ＋ 新規 macro / validation テスト）。
- SPEC §19 予算維持: 指標 compute ≤ 50ms、WS ≤ 100ms。
  macro / validation はオフスレッドのため price/analysis パスに影響しない。
- ダッシュボードに (a) マクロ参照パネル、(b) 逆行トリガーの視覚警告、
  (c) DWS パネルの信頼度メトリクスが表示される。
- サーバ再起動でコンソールエラーなし。
