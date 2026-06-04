# セッション申し送り (Session Handoff)

> 最終更新: 2026-06-01 / 直近コミット: `86e3dbb`(+本申し送り更新) origin/main と同期。
> §9–§11 は全て**コミット済み**(9070ae5 以降: f509595 / 56aee6b / e151c03 / 9499f04 / 86e3dbb)。
> **最新の作業は §12 を読む**こと(カレンダー時刻バグ根治・通貨強弱検証・IC較正・縮小DWS 等)。
> 新しいセッションを開始したら**まずこれ(特に §2/§5/§12)を読む**こと。
>
> ⚠️ **運用の最重要ハマりどころ**(§12詳細): **バックエンド(Python)修正はプロセスの“完全再起動”が必須**。
> `Desktop\Dashboard.bat` は健全なサーバを検知すると**再起動せずブラウザを開くだけ**なので、
> 反映には **8050 を握る python PID を明示 kill → 再起動** が必要(`Stop-Process -Id <PID> -Force`)。
> フロント(app.js/css/html)のみの変更は no-cache 配信なので**ブラウザ再読込で反映**。

---

## 1. このアプリは何か

MT5 (MetaTrader 5) のトレーディング・ダッシュボード。
- バックエンド: Python — Flask + flask-sock WebSocket。`MetaTrader5` パッケージで MT5 端末に接続し、解析スナップショットを `/ws` で配信。
- フロント: **素の JS / CSS（フレームワーク無し）**。`static/app.js` が WS を受けて DOM をその場パッチ。`static/app.css` はデザイントークン駆動。
- 主要機能: DWS-SMT 3TF EMA一致トリガー、16年OOS検証(統計)、通貨強弱、相関、経済カレンダー、マクロ金利、裁量注文パネル。

---

## 2. 環境（重要・ハマりどころ）

- **Python 実体**: `C:\Users\ohuch\AppData\Local\Python\pythoncore-3.14-64\python.exe`
  - 素の `python` は**壊れた Windows Store スタブ**。必ずフルパスを使う。
- **Windows コンソールは cp932**: 使い捨てスクリプトで `✓` や em-dash 等の非ASCIIを print すると `UnicodeEncodeError` で落ちる。**ASCII のみ**。
- OS: Windows 11 / シェルは PowerShell（`$null`、`$env:VAR`、バッククォート継続）。Bash ツールも利用可。
- 追加作業ディレクトリ: `...MQL5\Indicators`（MQL5側、通常は触らない）。

---

## 3. リポジトリ / Git

- origin = `github.com/TC-HARTON/DWS-SMT.git`
- 作業ブランチ = **main**（このプロジェクトは main に直接コミット/プッシュする運用。ユーザが「プッシュ」と言ったら origin main へ push）。
- **コミット/プッシュはユーザが指示した時のみ**。フック skip / 署名バイパス / git config 変更は禁止。
- コミット末尾に `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 未追跡で**意図的に放置中**: `docs/DISTRIBUTION_PLAN.md`（本バッチと無関係）。

---

## 4. 動かし方・検証

```
# サーバ起動（Webを先にバインド→MT5は背景で再接続）
"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" main.py
# → http://127.0.0.1:8050

# テスト（全件）
"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q   # 現在 295 passed

# JS構文
node --check static/app.js
```

- **統合検証は必須**: ユニットテストだけで完了報告しない。サーバを実際に動かし、ブラウザで描画/応答/コンソールエラーを**実際に目視**する（Claude_in_Chrome / preview ツール）。
- frontend のみの変更はブラウザ再読込で反映（`Cache-Control: no-cache`）。**backend 変更はサーバ再起動が必要**。
- 現在のブローカー: **Exness demo**（login 41269839 / Exness-MT5Trial3 / JPY、`trade_allowed=True`）。`.env` の `MT5_TERMINAL_PATH` で切替（ダッシュボードのブローカー切替ドロップダウンが .bat 再起動を行う）。

---

## 5. 壊してはいけない設計（非自明・要注意）

- **WS は軽量/重量の2系統**: 価格・口座は ~2Hz の light（`snapshot_light` / `state.light_snapshot`）、重い解析は変化時のみ full（`snapshot_to_json`）。**解析ブロックを 2Hz で再配信しない**。
- **oos_baseline（約2MB・静的）は接続初回の full にのみ載せる**。以降の full は `include_baseline=False`。クライアント(app.js)が `OOS_BASELINE` にキャッシュし後続 full へ再付与。**毎スナップに戻すと帯域/CPUが爆発する（過去の不具合）**。
- **時刻は全て JST(UTC+9)表示**。MT5 はサーバ時刻でバー/ティックを刻むので UTC へ変換（`server_offset_sec`）。Dukascopy CSV は Europe/Bucharest(EET/EEST, DST有)。JS側の時刻ラベルは `getUTC*`+9h で算出（`getHours` のローカルtzは使わない）。
- **ブローカー時刻→UTC は DST 対応必須(§9 で導入)**。ICMarkets サーバは **Europe/Bucharest(EET冬+2/EEST夏+3)**。`config.BROKER_TZ_BY_SERVER` に載るサーバは `copy_rates`→`_bar_index_utc` が**バー毎に IANA tz でローカライズ**(単一オフセットだと季節跨ぎの深い履歴が1hズレ、トリガーストアが重複する)。載らないサーバ(Exness=固定オフセット)は従来の flat offset。`server_offset_sec` も DST ブローカーは**tz から決定論的に算出**(stale tick 誤検出を根絶)。**この設計を単一オフセットに戻さない**。
- **Pips換算**: gold pip = $0.10。baseline は 3桁(point 0.001)→pts/100、live gold は 2桁(point 0.01)→pts/10。`pips = net_pts × (source_point / pip_price)` で baseline/live を一致させる。スプレッドは1回だけ控除。
- **スプレッド/コストの扱い(2026-05-30 改訂・重要)**:
  - **ヒストグラム = タイミング表示の目安。スプレッド控除しない=gross pips**(`app.js drawDwsCanvas`、`pips = tr.p*ptMult*liveF`)。
  - **トリガー履歴(ライブ) = 一律 `config.LIVE_SPREAD_COST_PIPS=2.0` pips を控除**(スプレッド≈1pip+手数料)。MT5 の `copy_rates` バー spread フィールドは**信頼できない**(0率: M15 18.6% / H1 48.6% / H4 83.7%。live録画分のみ埋まる)ため、バー spread は使わず**一律コスト**。`signal_validator._evaluate_window` が `cost_pts = 2.0×pip_size/point` の定数配列を `evaluate_trades`/`_recent_triggers_from_window` に渡す。→ 履歴 pips = gross − 2.0(常にヒストより 2.0 小さい)。
  - **16Yベースラインは別**: Dukascopy の**実BID/ASK**スプレッドを使う(`evaluate_trades(spread_pts=...)` は配列のまま、バックテスト群 `scripts/_backtest_*`・`_oos_xauusd_16y.py` が実スプレッドを渡す)。**`evaluate_trades` のシグネチャを壊さない**。
  - IC は直近~2週しか tick(実BID/ASK)を保持せず、2026 全期間の実 bid/ask は取得不能(=一律2.0採用の理由)。
- **トリガー履歴 = 日付ピッカー型カレンダー(2026-05-30 全面作替え)**: 旧「年pill+30件リスト」「年×月リターン表」を**痕跡なく削除**し、よくある**カレンダー型セレクタ**(`app.js buildTriggerCalendar`)に作り直した。`◀ 年 ▶` ナビ + 年タイトルクリックで**年ピッカー(全期間2010-2026グリッド)** + **12ヶ月グリッド**(各月=純pipsヒートセル) + 選択期間の集計。コンパクト(一年表示)で**下の時間別勝率に被らない**。
  - 月次データ: ライブは `trigger_store.load_by_year`→`months`(JST月別`_period_stats`)。**16Yバックテストも月次対応**: `scripts/_oos_xauusd_16y.py _trigger_history` に `months` を追加し、**frozen `oos_baseline.json` に月次のみ注入**(年次統計・CI は不変=既存と完全一致を検証済。CI ドリフトは採用せず)。→ baseline(≤last_year) + live(>last_year) を frontend がマージし **2010-2026 全期間**を月次表示。
  - クラス名は `cal-*`/`tcal-*`(`.anlx-triggers` スコープ)。**経済カレンダー(ForexFactory)も `.cal-row`/`.cal-list` を使うので衝突注意** — 旧実装は `.cal-row{display:flex}` で経済カレンダーを上書きしていた(修正済)。状態は `UI.calYear/calMonth/calView`、データ駆動・ハードコード無し。
- **推奨ロットは BALANCE 基準**（equity ではない）: `clamp(0.01 × floor(balance/100000), 0.01, 10)`。
- **トリガー検出は確定足のみ**（forming bar でトリガーしない / look-ahead 厳禁）。`_pair_trades` は stop-and-reverse + EXIT。
- **裁量注文の鉄則**: コードは私が書くが**実発注はユーザがクリック**（私は実注文しない）。`/api/order`・`/api/close` は same-origin ガード + `TRADING_ENABLED` + `trade_allowed`/端末Algo 許可ゲート。open/close 両方にガードあり。最小ロット未満は拒否（黙って引き上げない）。
- **UIレイアウトは超慎重に**: ヘッダーは `XAUUSD(左) | BID/ASK(中央, 絶対配置) | SELL/LOT/BUY+✕(右)`。LOT入力枠はボタンと下端揃え(`align-items:flex-end`)。**勝手にレイアウトを変えない**。変更前に確認し、実測(getBoundingClientRect)で検証する。

---

## 6. 直近の作業（2026-05-30 / コミット 9070ae5）

「手抜きゼロの高精度監査(5領域並列)→全件修正→統合検証→push」を実施。
- 資金安全: close権限ガード、最小ロット拒否、刻み精度動的化、SL/TP `is not None`、テスト+7。
- 性能: oos_baseline 初回限定配信(≈25MB/分削減)、2Hz deepcopy 除去、未ゲート再描画抑制。
- フロント: 相関ペア/OOS verdict の XSS 修正、部分決済後のstale修正、Escモーダル競合修正、JST固定、null/NaNガード。
- 統計: Welch を厳密 Student-t 化(scipyと1e-13一致)、Wilson z 厳密化。
- 死蔵除去: `paintBias`/`JP_BIAS`、死CSS8群、死config7件+2dataclass、`serialize_symbol_structures`/`jsonable`/pair_biases、未使用import6件。
- 検証: pytest 295 passed / node OK / ライブ再起動でコンソールエラー0・全パネル正常描画。

---

## 7. 未着手 / 保留（TODO）

1. **structures パイプライン撤去（やるべき・別タスク化済の chip あり）**
   フロントは `structures`(levels/price_action/confluence) を**一切消費していない**のに、`snapshot_to_json` が毎 full で配信し、`analysis_loop._publish_structures` が毎サイクル `detect_all` を実行(CPU浪費)。**バックエンドに他消費者がいないか検証**のうえ、シリアライズ+計算を撤去。検出器モジュール本体とそのテストは残す。
2. **Bonferroni 族 k=3（症状毎=3TF）→ 据え置き**。パネル単位の主張として妥当で UI に明示済み。k=24 への変更は別の問いへの過保守化で「より正しい」訳ではない（変更しない）。
3. **oos_baseline 再生成 → 今は不要**。Welch/Wilson 厳密化の差は表示精度以下。次回データ追加で正規再生成時に自動反映。
4. （軽微）性能 HIGH-2: 2Hz light に静的な口座identityを毎回載せている。トリミングはクライアントのマージをフィールド単位にしてから（さもないと identity が消える）。
5. （軽微）統計 L-2: DWSキャンバス上の建玉P/Lマーカーが現在スプレッドを使用。トリガー履歴表(バー時点スプレッド)と微差。バー時点スプレッドを serialize して使うと整合。

---

## 8. ユーザの仕事の流儀（厳守）

- **手抜き・簡素化禁止**。常に目的最適・SPEC通り完全実装。最適化でアプリを劣化させない。
- **指示は加算的**に解釈（新制約は既存に追加）。
- bare except 禁止 / 型ヒント+docstring 必須 / `FRED_API_KEY` は env のみ / `.env` は gitignore。
- 完了報告は**統合検証(実サーバ+目視)後**にのみ。「できた」は実証してから。
- 強い UI/UX センスを要求。**画面を実際に見て**判断する。

---

## 9. トリガー履歴 重複バグ修正（2026-05-30・未コミット）

**症状**: XAUUSD トリガー履歴が全TFで「同じ取引が不自然に反復」(ユーザ報告)。

**根本原因**: `server_offset_sec()` が**単一 tick から**サーバ→UTC オフセットを時間丸めで算出。
市場クローズ/再接続直後の **stale tick** を拾うと丸め値が時間単位でズレ(真+3hに対し −1/−2/0h と誤検出)、
`copy_rates` のバー時刻 `server−offset` がズレ→トレードの `entry_ms` が変わり、
`trigger_store` は entry_ms 重複除去なので**同一確定足を別時刻で再追記**。整数時間シフト重複が全8シンボルで
18k+ 行(M15 で 1960→663 等)。+ ICMarkets は **Europe/Bucharest(DST有)** で夏冬跨ぎも単一オフセットでは1hズレ。

**修正(コード)**:
- `mt5_connector.server_offset_sec`: stale tick 棄却(生 `server−now` が整数時間から離れるサンプルは拒否)、
  broker毎 last-good 永続(再接続で維持)、変更は1サイクル確認。**DST ブローカー(`config.BROKER_TZ_BY_SERVER`)は
  tz から決定論的に算出**(tick 非依存)。
- `mt5_connector._bar_index_utc`(新): DST ブローカーはバー epoch を IANA tz でローカライズ→UTC(季節別オフセット)。
  他ブローカーは従来 flat offset。`config.BROKER_TZ_BY_SERVER = {"ICMarketsSC-MT5-3": "Europe/Bucharest"}`。
- テスト+5(`tests/test_mt5_connector.py`): stale 棄却 / last-good / 変更確認 / DST copy_rates / DST offset。**計300 passed**。

**修正(データ)**: `scripts/_regen_trigger_store.py` — 接続中ブローカーの全シンボル×TFを現行バーから
**DST正規 UTC で再生成**(`--dry-run` 有)。原本は `<name>.jsonl.bak` で保全。窓が全履歴をカバーするので損失なし。
重複は完全消滅(M15/H1/H4 で重複timestamp=0、バグsignature `[0,4,5]` 消滅)。**ブローカー切替後は各ブローカーで再実行**。

**恒久ガード(人の目に依存しない再発検知)**:
- `trigger_store.scan_corruption(recs)` — 再stamp署名(同一timestamp重複 / `(dir,net_pts)` の≤6h整数時間「三つ組」)を検出。
  **偶然 net_pts 一致の別トレードは検出しない**(実トレードを消して metric をゼロにするのは逆の偽装)。
- `load_by_year` が読込毎に検査し、腐敗があれば `log.error` で**大声で暴く**(隠さない)。
- 回帰テスト `test_real_store_has_no_restamp_corruption` が**実ストア全48ファイルを走査**し腐敗0を assert(再汚染で自動失敗)。
- ops: `scripts/_scan_trigger_store.py`(全ファイル走査、腐敗あれば exit 1)。

**全48ファイル網羅スキャン結果**: 23,380行 / 同一timestamp重複=0 / 三つ組=0 / **VERDICT CLEAN**(両ブローカー)。
残る「同一(dir,pips)値の別行」は別時刻の実トレード(=正当、削除しない)。

**検証**: pytest 304 passed / 実サーバ再起動→検証パス後もストア件数不変(再汚染なし) / ブラウザでトリガー履歴(2026/2024)
が全て別時刻・別結果で反復ゼロを目視確認 / コンソールエラー0 / ops スキャン CLEAN。

**残課題(軽微・別タスク)**: `latest_tick` のティック時刻変換は依然 flat offset 系。DST ブローカーでは
`server_offset_sec` が tz 算出になったので実害は小さいが、ティック経路も tz 統一すると完全。Exness は固定オフセット
前提(DST非対応)— もし Exness が将来 DST を観測するなら `BROKER_TZ_BY_SERVER` に追記が必要。

---

## 10. 提言5件バッチ(2026-05-30・未コミット) — ユーザ承認順に逐次実装

「ロット/SL幅は裁量(手出し無用)。それ以外の提言を順次」指示。**#2は「両方」**(バナー+直近を上)。**実発注は私はしない**(裁量クリック)。

1. **高確信セットアップ・アラート** — ヘッダーの ACTIVE SETUPS に通知ベル(`🔕/🔔`)を追加。`app.js` の `ALERT`(enabled/prev/seeded/ctx) + `fireSetupAlerts(scored)` が **STRONG-BUY/SELL** の新規出現を `sym|cls` で重複排除し、初回シードは黙らせ、Notification API + WebAudio ビープで通知。状態は localStorage。`paintAlertBell()`。
2. **見出し数値を「直近ライブ優先」** — (a) `_buildRegimeBanner` が現行PF vs 16Y baseline のドリフトを計算(≤−20%で警告/≥+20%で良好バナー)。(b) パネル組立順を `バナー+直近(secondary) → ヘッダ → 統計 → スパーク` に変更し、**地合い悪化時に16Y側へ逃げない**ようライブ直近を上に出す。
3. **broker_meta 欠落ガード** — `signal_validator.compute` で `sym_meta`/`point` 欠落時は `log.warning`+`continue`(以前は KeyError/誤値の恐れ)。
4. **タグ付きトレード日誌**(新規) — 発注成功時にその時の**3TF市況(TF別 EMA上下/ADX/DI/RSI)**を添えて記録。
   - backend: `analyzer/journal_store.py`(ブローカー別 append-only JSONL、`data/journal/<slug>/orders.jsonl`、newest-first、破損行スキップ)。`lite_server`: 発注成功フックで `_journal_order`(`_tf_context` を防御的に読む)、`GET /api/journal?limit=`。
   - frontend: side に「トレード日誌」カード。`fetchJournal`/`paintJournal`/`renderJournalEntry`(BUY/SELL/lot/価格/時刻 + TF別 ↑↓+ADX チップ、RSI/DI は title)。起動時 + 発注成功後 + **ブローカー切替時**(`maybeRefreshJournal`)に更新。
   - test: `tests/test_journal_store.py` +6(append/順序/limit/破損行/None server/Unicode往復)。
5. **冗長パネル整理(方向情報3重)** — BIASゲージ / TFトレンド表 / DWS が方向を重複表示。**ユーザ「続行」で盲目変更を承認**し、保留分も実施。
   - **構造統合(両モード)**: BIAS(`.composite`)を **`.signals` の最初の子=トレンド表のヘッダー**に DOM移動。`paintSignals` は data-bind id 参照なので**改修不要**(`sig-body` は別 div なので再描画で消えない)。`.composite` は自前の bg/角丸を捨て、下罫線で表と区切る 1枚のカードに。**オーバーフロー安全**: 2列目を `minmax(0,auto)` + `comp-text` を ellipsis 化(フォント拡大でも溢れない)。
   - **展開グリッド**: `grid-template-areas` から全幅 `composite` 帯を削除(4行→3行 `auto min-content 1fr`)。展開時 BIAS フォントは全幅band用の 30-42px → 半幅カラム用に縮小(arrow26/text22/score lg)=**縮小方向のみ**(溢れ得ない)。帯1行削減で**パネル自体も縦に短く**=縦圧縮。
   - **±10軸ラベル削除**(「強売-10/0/+10強買」。`comp-text`の語 + `comp-score`の x/10 + 中央線で十分)。
   - **DWS縦圧縮**: `.dws-canvas-wrap` の `min-height` 200→168(床のみ低下、flex-fill のため高パネルは不変=安全)。
   - **セッション折りたたみは見送り(私の判断)**: 時刻別ヒートマップを `<details>` 化しても、**展開パネルは「内部クリックで畳む」**実装(`onPanelClick`)のため summary クリックがパネル畳みと競合。innerHTML 注入後に stopPropagation を後付け配線する必要があり脆弱・最低価値のため見送り。要望あれば別途。

**検証**: `pytest -q` **313 passed** / `node --check static/app.js` OK / **Flask test client で実 `lite_server` を叩く統合チェック**全パス: `/` 200・journalパネル在、`app.js` で **composite が signals 内(ヘッダー→matrix の順)**・軸除去、`app.css` で**全幅band削除・3行グリッド・軸CSS除去・DWS 168・journal/overflow安全 CSS 在**、`/api/journal` が `{server,entries[]}`。**#5 は構造変更のため見た目の最終確認はユーザ環境で**(`grid-template-areas` 由来の半幅カラム内 BIAS フォント感や行間)。盲目変更ゆえ違和感あれば微調整します。

---

## 11. 実機レビュー対応(2026-05-30・未コミット) — ユーザが実DOM/ネットワークで観測した指摘の裏取り+修正

ユーザ報告6件をコードで裏取り。**2件は実バグ(修正済)、2件は前提がコードと不一致(誠実に指摘)、2件は要判断(保留・要指示)**。

- **[実バグ修正] #1 トレード日誌 `/api/journal` 404 + 二重発火**:
  - 404 の真因 = **エンドポイントは実装済**(`build_app` 内に無条件登録、test client で 200 確認)。ユーザの稼働サーバが**ルート追加前に起動した古いプロセス**(backend 追加はサーバ再起動が必要=§4)。→ **再起動で解消**。※「Dashコールバック重複バインド」説は**本アプリは Dash 非使用**(`main.py`→`lite_server.build_app`, flask-sock)のため不該当。
  - 二重発火は実在(私のJS2箇所)。**`DOMContentLoaded` の seed fetch を撤去**し、`maybeRefreshJournal`(初回 account.server 到達時=ブローカー確定の正タイミング)に一本化。発注成功後の fetch は残置。
- **[実バグ修正] #2 相関「waiting for data…」固着(H1×500)**: 真因 = `correlation.py` が**バー数<窓サイズの窓を無言スキップ**し、利用可能バー数を公開していない→フロントが loading と恒久不足を区別できず固着。
  - backend: `CorrelationSnapshot.bars_available`(=usable returns 行数)追加、両 return パスで設定。`serialize_correlation` に `bars_available` 公開。
  - frontend: `_paintCorrWindowAvail` が**バー数に満たない窓ピルを無効化**(`.pill-disabled`、蓄積で自動再有効)。選択窓が無いとき **`データ不足: H1 N本必要・取得 M本`** と件数明示(loading とは別文言)。
  - test: `test_correlation.py` +1(`bars_available` と窓計算の整合)。
- **[実バグ対応外/UI改善] #4 ヒートマップ小標本セル**: `N<HM_MIN_N(30)` のセルを**赤緑で着色しない**(中立グレー+破線+WR淡色、tooltip「標本不足: 参考値」、凡例「灰=標本不足 N<30」)。偶然の高WRをエッジと誤認させない。
- **[前提不一致・誠実指摘] #6 「ブローカー切替」ボタン**: コード上 `broker-toggle` は**ブローカー切替専用**(`/api/broker` GET/POST)。8ペア俯瞰グリッドは**既定のメイン表示**(`symbols-grid`)で別物。「グリッド表示を兼ねる」前提はコードと不一致のため改名見送り(要望あれば即対応)。
- **[実装済] #3 直近劣化を執行制御へ反映(劣化ゲート)**: ユーザ承認(「#3 劣化ゲート実装」)で実装。
  - `_regimeDrift(sym, snap)` = `(rolling.raw.profit_factor − oos_baseline.profit_factor)/baseline`(UI.dwsBase、バナーと同一値を共有)。`REGIME_GATE_DRIFT = -0.30`。
  - **2層構成**: 警告バナーは従来通り **-20%**、ゲート(降格+アラート抑制)は **-30%**(より深い別Tier)。
  - `paintActiveSetups`: drift≤-30% の高確信セットアップを**「様子見」に降格**(破線/アンバー/方向色なし、末尾へソート、tooltip に 16Y比%)。`fireSetupAlerts`: **degraded は alert 集合から除外**(回復したら再 alert=「地合い回復」シグナル)。
  - **ロット/SL/実発注は一切不変**(逓減案は不採用、表示降格+通知抑制のみ)。`.active-chip.degraded` CSS 追加。
  - 検証: gate 算術の境界(`<=-0.30` 包含 / null安全 / 改善側は降格しない、ユーザ例 1.90→1.11=-41.6%→様子見)を node で確認、app.js に定数/ヘルパ/降格/alert除外が配信されることを test client で確認。
- **[実装済] #5(レビュー) サマリーバー**: ユーザ承認(設計案提示→「折りたたみ可能(既定開)」選択)で実装。
  - `index.html`: ヘッダー直下に `<details class="summary-bar" open>`(ネイティブ折りたたみ=堅牢)。body グリッドに full幅 `summary` 行(`auto`)を追加(`56px auto 1fr 280px`、`.grid`/`.symbols` 不変)。
  - `paintSummaryBar(snap)`: 全8銘柄を1セル/銘柄で集約 — **BIAS総合**(`compositeSignal`)+**4TF EMA一致**(D1/H4/H1/M15 の `last_close≷ema`)+**様子見フラグ**(`_regimeDrift≤-0.30`、#3と共有)。analysis 更新時のみ再描画。セルクリックで `focusPanel(sym)` が該当パネル展開。
  - ヘッダーの ACTIVE SETUPS(高確信のみ)と役割分離: サマリーは**全銘柄俯瞰**。開閉状態は localStorage `mt5-summary` で永続。
  - CSS: `sb-*`/`.summary-bar`(衝突なし、`cal-summary` とは別)。`.sb-cell.degraded` は #3 と同じアンバー破線。
  - **見た目の最終確認はユーザ環境**(常設1行ぶんの縦圧迫・8セルの可読性)。盲目実装ゆえ違和感あれば微調整。

**検証**: `pytest -q` **314 passed**(+1) / `node --check` OK / 統合チェック: `serialize_correlation` が `bars_available` を出力、`/api/journal` 200、`app.js` に相関可用性UI在・load時二重fetch除去。**相関の見た目とトレード日誌の実発注記録はユーザ環境(実MT5/再起動後)で最終確認**。

---

## 12. 2026-05-31〜06-01 セッション(全コミット済み) — UI拡張・IC較正・カレンダー根治

コミット対応: `f509595`(日誌/劣化ゲート/サマリー/トリガー重複/相関) → `56aee6b`(サマリー視認性) → `e151c03`(国旗) → `9499f04`(劣化フロア/サマリー高さ/IC較正ツール) → `86e3dbb`(カレンダー時刻+リンク/強弱検証+CAD/CHF/縮小DWS) → 本コミット(パネル常時明+申し送り)。現在 **pytest 322 passed**。

### 12.1 ⚠️ 運用ハマりどころ(最重要・再発防止)
- **バックエンド修正はプロセス完全再起動が必須**。`Dashboard.bat` は 8050 が健全応答(200+"MT5 Dashboard")だと**再起動せずブラウザを開くだけで exit**。→ 旧 python プロセスが古いコードのまま生き続ける。手順: `Get-NetTCPConnection -LocalPort 8050 -State Listen` で PID 特定 → `Stop-Process -Id <PID> -Force` → `Dashboard.bat` 再実行。WS スナップショット(`ws://127.0.0.1:8050/ws`、`simple_websocket.Client`)で実データを直接検証できる。
- フロント(static/*)変更は no-cache 配信なので**ブラウザ再読込のみ**で反映。

### 12.2 サマリーバー(新規・ヘッダー直下の常設帯)
- `index.html` の `<details class="summary-bar" open>` + body グリッドに full幅 `summary` 行。全8銘柄を1セル: **BIAS総合(符号で色: +緑/−赤/0灰)+ 4TF EMA一致チップ + 様子見ピル + 国旗**。
- **国旗 = インラインSVG**(`CCY_FLAGS`、Windows は絵文字国旗を持たないため)。基軸/決済通貨を左右に。`flagSvg()`。
- **様子見ピルは常時DOM(非該当時 `visibility:hidden`=`.sb-flag-off`)** → カードの高さが不変=バー伸縮せずアプリが上下しない。
- セルクリックで `focusPanel(sym)`。開閉は localStorage `mt5-summary`。

### 12.3 劣化ゲート/バナーに絶対PFフロア(`9499f04`)
- `_regimeState(sym,snap)` = `{drift, pf, wr, n}`、`_regimeGated(st)` で**2条件 AND**判定: `drift ≤ 閾値` **かつ** `pf < REGIME_PF_FLOOR(1.30)`。**黒字(PF≥1.30)なら鳴らさない**。警告 -20% / 降格+アラート抑制 -30%。バナー(#2)・ActiveSetups降格(#3)・サマリー様子見が同一ロジック共有。**ロット/SL不変**。

### 12.4 H4 を UI のトリガーTFセレクタからのみ隠す
- `DWS_BASES=['H1','M15']`、デフォルト `UI.dwsBase='M15'`。**BIAS計算(`TF_LABELS=D1/H4/H1/M15`)・シグナル行列・サマリーチップ・エンジンの `DWS_SMT_STACKS` は H4 を保持**(M15/H1 ベースの3TFスタックの構成要素)。隠すのは“トリガー選択ピル”だけ。

### 12.5 IC ヒストリ取得 + フィード較正(Option A)— **結論: IC≒Dukascopy、補正不要**
- IC は **2023-03-20 以降の全足しか保持しない**(下位足は2-3年、ICは全TFこの日が底)。**ルートに IC CSV**(`SYMBOL_TF_開始_終了.csv`、MT5タブ区切り・intradayはDATE+TIME/D1-W1はDATEのみ・サーバ時刻=Europe/Bucharest)。**`.gitignore` の `/SYMBOL*.csv` で無視**(コミットされない)。
- `scripts/_calib_ic_vs_duka.py`(新規): **本番 `dws_smt.compute_symbol` + `config.DWS_SMT_STACKS`** を IC/Dukascopy 両フィードに同一窓(2023-03〜2025-12)・一律2.0pipで流しフィード差を抽出。pips=`points/pip_price`(ポイント非依存)。
- **結果: 全8銘柄で IC≒Dukascopy(PF ±1〜6%)** → 深いDukascopyベースラインはICにそのまま使える。「FXが16Y比 −20〜40%」は**ブローカー無関係の地合い軟化**(Dukascopyも同じ・全銘柄黒字)。金は+23%。M5追加は**見送り**(コスト相対重・データ巨大)。

### 12.6 縮小パネルに DWS シグナル要約
- `buildCompactDws(snap,sym)`: 折りたたみ時の空き下半分に **3TF整列状態 + 直近トリガー(方向/グロスpips/何本前) + 足確定カウントダウン**。展開DWSと**同一 `win` データ再利用**(`win.c`/`win.g`/`win.trades`(`t.i`/`t.p`)、`pips=t.p*ptMult*liveF`)。`.panel.expanded .dws-compact{display:none}`。カウントダウンは `.dws-cd[data-close]` ティッカー相乗り。

### 12.7 通貨強弱: 厳密検証 + カバレッジ可視化 + CAD/CHF
- **強弱は接続中ブローカーのライブ足**(各ペア直近N+2本、`fetch_rates_parallel`)。Dukascopy/16Yではない。
- 設計は **8メジャー全28ペア行列・各通貨7ペア対称・z-score(平均50/±2σ=0/100)**。回帰テスト追加: **28ペア厳密復元(raw_avg と真値 相関1.0)**・1通貨全面高・pair-bias符号整合(`tests/test_currency_strength.py`)。
- **カバレッジ⚠**: ある通貨が7ペア未満(ブローカーがクロス欠落)だと通貨コード横にアンバー⚠+行淡色(`.s-row.low-cov`)。`n_pairs` は元々シリアライズ済。満額なら非表示(`.s-cov:empty{display:none}`)。
- 表示を **5→7通貨(USD/EUR/GBP/JPY/AUD/CAD/CHF)**。`STRENGTH_CCYS`。非表示は NZD のみ。**注意**: z-score は8通貨基準(平均50)なので、表示通貨が全て50未満でも正しい(隠れた NZD が強いだけ)。

### 12.8 経済カレンダー: 時刻バグ根治 + ソースリンク + ADP区別
- **根本原因**: faireconomy `ff_calendar_thisweek.xml` は **GMT/UTC** なのに `_parse_ff_datetime` が **US-Eastern 解釈** → 全FFイベント **+4h(夏)/+5h(冬)** ズレ(NFP が 01:30、正21:30)。既知時刻(ADP/ISM/失業保険/豪GDP)で実証 → **UTC解釈に修正**。`_et_to_utc_ts`(FRED/FOMC/CB)は ET 由来なので不変。
- **ソースリンク**: `CalendarEvent.source_url` 追加。FFは `<url>` を取込、合成は **FOMC→Fed / NFP→BLS / ECB / BoE / BoJ / RBA** 公式URL。フロントは ↗ リンク、**https + ホスト許可リスト(`CAL_SRC_HOSTS`)** で検証(`javascript:`/`http:`/不明ホスト拒否)。
- **ADP を NFP と区別**: `_jp_calendar_title` で ADP→「ADP雇用統計」を先に判定。
- 自動取得 = **毎時(`CALENDAR_REFRESH_SEC=3600`)・別スレッド・3リトライ・失敗時キャッシュ(`external/forex_factory_xml/thisweek.xml`)フォールバック**。

### 12.9 パネル常時明るく(本コミット)
- `.panel.quiet` の `opacity:0.6`(+ホバーで1)を撤去 → 全パネル `opacity:1` 固定。カーソル出入りの明暗チラつき(目の疲れ)を解消。

### 12.10 現在のライブ状態 / 残課題
- 稼働サーバ: **新コード反映済み**(セッション中に PID 明示kill→再起動を2回実施)。接続ブローカーは MT5 端末依存(切替はダッシュボードのドロップダウン)。
- 残(任意): カレンダーの ADP は**時刻21:15で正・ラベルのみ区別**(値ソースは別物)。中銀URLはホスト許可リスト追加済。`Average Hourly Earnings` 等一部は英語ラベルのまま(必要なら和訳追加可)。**Bonferroni/structures撤去**(§7)は据え置き。

---

## 13. 2026-06-02 セッション — ロジック厳密監査 + GoldMacroScore 試作(結論: REJECT)

### 13.1 ロジック厳密監査(5領域並列レビュー)→ 修正 push 済み(コミット `25d24e8`)
- **最重要: forming sub-TF look-ahead 根治**(`dws_smt.compute_symbol`)。`_diff_series` に `df.iloc[:-1]` を渡し、未確定の高位TF足 close が**確定済 base 足のトリガーへ伝播**するのを遮断(tick毎フリッカ→`trigger_store` への誤値永続化を防止)。回帰テスト `test_forming_subtf_bar_never_changes_confirmed_base_triggers`。
- 他: `_generate_oos_baseline.py` に schema ダウングレード refuse ガード / `.cal-*` 30 セレクタを `.anlx-triggers` スコープ化 / Welch×scipy 1e-10 一致テスト新規 / `_regimeState` PF=∞ 仕様明記 / `trigger_store.load_by_year` の `path.exists()` ロック内化 / calendar `%p` 大文字化(Windows CRT) / journal `except json.JSONDecodeError` / 他軽微。
- **誤検知と判定(修正不要)**: `_bar_index_utc` の `ambiguous=True`(pandas は曖昧時刻のみ作用、非曖昧バーは正しい季節別オフセット=実機検証済) / `correlation.bars_available` の NaN 混入(末尾 `dropna()` で intersect 済)。

### 13.2 GoldMacroScore 試作 — **VERDICT: REJECT(ライブ配線は撤去済み・研究資産は保存)**
- 狙い: XAUUSD 特化マクロ合成指標(実質金利 DFII10 / 期待インフレ T10YIE / VIX VIXCLS / 広義ドル DTWEXBGS を水準z・等加重で -10..+10、BIASスケール)。spec `docs/superpowers/specs/2026-06-02-gold-macro-score-design.md` / plan `docs/superpowers/plans/2026-06-02-gold-macro-score.md`。
- **検証(`scripts/_validate_gold_macro.py`、look-ahead無し)**: IC 5d=+0.002 / 20d=−0.026(両方 CI が 0 を跨ぐ)。OOS ゲートも M15/H1 は PF 悪化、H4 のみ小標本で改善=過学習。→ **REJECT**。
- **探索(`scripts/_experiment_gold_macro.py`、ICマトリクス)**: 4ドライバ×{水準z,変化z}×{5/20/60日} で**理論方向の有意な正 IC はゼロ**。マクロ系の設計反復(変化z/PCA直交化)では救えないと実証。→ 次に意味あるのは COT/GLD(別仮説・別データ)。
- **ユーザ判断**: 「テクニカルで最重視するのはチャート形状=今のAIの限界領域。現状ロジックは自分のTFと大差ない。一旦終了」→ **ライブ配線除去・研究資産保存**(コミット `00c7910`)。
- **教訓**: 検証ゲート(IC+OOS)が「もっともらしいがノイズな指標」をUI配信手前で正しく止めた。`16Y OOS で証明する`規律の価値。
- **再挑戦するなら**: `analyzer/gold_macro.py`(純スコア)+ 上記2ハーネス + config の FRED系列/窓定数 が残置済み。COT(CFTC週次)/ GLD保有高(日次)フェッチャを新設して同ハーネスで再検証する手順から。

### 13.3 コミット状況(2026-06-02 時点)
- push 済み: `25d24e8`(ロジック監査修正)。
- **ローカル main のみ・未 push**: GoldMacroScore 関連の commit 群(`00b4a1f`〜`00c7910`)。research 資産 + spec/plan + 検証ハーネス + ライブ配線除去。ユーザが「プッシュ」と言うまで push しない(§3)。
- pytest: **339 passed**。稼働サーバはライブ配線除去後の新コード反映済み(`gold_macro` は WS 非配信を確認)。
- (2026-06-03 追記) §13 の GoldMacroScore 群 + §14 の flip-proximity 群は**この日に origin/main へ push 済み**。

---

## 14. 2026-06-03 — DWS ヒストグラム「反転接近度」グラデーション(新機能・採用)

ユーザ要望②「M15(等)が反転寸前=もう少しでトリガー、を価格変動でカラー変動表示」を実装。spec `docs/superpowers/specs/2026-06-02-dws-flip-proximity-design.md` / plan `docs/superpowers/plans/2026-06-02-dws-flip-proximity.md`。

### 14.1 何をするか
- DWS の各段(行)の色を二値(緑/赤/灰)から、**反転接近度グラデーション**へ。`flip_norm = clamp(smoothed_diff / (k·rolling_std), -1, +1)`(`analyzer/dws_smt._flip_norm`、`config.DWS_FLIP_STD_WINDOW=96` / `DWS_FLIP_K=1.0`)。エンジンが計算済みで**色化時に捨てていた平滑値の大きさ**を露出するだけ(新シグナルではない=統計検証不要)。
- **色 = 符号色 ↔ ニュートラル灰(#3f4760)の不透明補間 + ニー0.45**。`|flip_norm|≥0.45` は完全単色(クリスプな帯)、本当に反転寸前のセルだけ灰へ。透明フェード(暗背景で濁る)は不採用。`app.js dwsCellFill` / 定数 `_DWS_FLIP_KNEE`。
- **暗色(灰寄り)= その段が反転境界**(方向が弱い/迷い)。新規点火寸前にも整列崩壊(EXIT/反転)寸前にもなる中立的近接シグナル。
- **holdout 強調**: 2段整列 + 残り1段が現在足で `|flip_norm|<DWS_FLIP_IMMINENT(0.25)` のとき、その段に枠 + TFラベル(例 `M15▲`)= トリガー完成寸前。`drawDwsCanvas`。

### 14.2 壊さない設計(重要)
- **トリガー検出・`win.c`・マーカーは完全不変**。flip_norm は**確定足の同じ平滑系列を再利用**(forming除外=look-ahead安全)。当初の forming-inclusive 二重パスは**性能劣化(29→54ms, SPEC50超過)**のため不採用 → 確定足ベースで floor ~33ms に回復(§14.3)。
- `flip_norm` は full スナップの DWS ブロックに `fn`(`[n][rows]`、[-1,1]、3dp)で同梱。`dashboard/serialize.serialize_dws_smt`。

### 14.3 性能(劣化させない)
- `_flip_norm` は **pandas rolling 不使用**(72回/サイクルで固定オーバーヘッド~20ms積上)→ **numpy cumsum rolling-std** に。解析 compute floor ≈ **33ms < SPEC 50ms**。
- budget テスト `test_engine_compute_under_budget_for_full_load` は単発計測の脆さを **best-of-3(floor計測)** に修正(許容値不変、回帰検出力維持)。

### 14.4 検証 / 状態
- **pytest 346 passed**(look-ahead 回帰ガード red-green 実証済 / flip_norm も forming除外で安全と assert)。`node --check` OK。
- 実機: 全8銘柄でグラデ描画、holdout `▲` ラベル描画(GBPUSD M15 で確認)、コンソールエラー0、`fn` 配信確認。
- 調整ダイヤル: `_DWS_FLIP_KNEE`(クリスプ↔グラデ量)/ `DWS_FLIP_IMMINENT`(holdout閾値)/ `config.DWS_FLIP_K`,`DWS_FLIP_STD_WINDOW`(正規化)。

---

## 15. 2026-06-03 — XAUUSD 完全特化 + DXY 追加

ユーザ指示「XAUUSDに完全特化。XAUUSD以外の計算は全て除去。金利パネルは残す」+「DXY追加・経済カレンダーは残す」。

### 15.1 特化(銘柄8→XAUUSD単独)
- `config.SYMBOLS = (XAUUSD,)` のみ。指標/検証/DWS/トリガーストア/パネルは SYMBOLS を走査するので、これだけで**全 per-symbol 計算が XAUUSD に限定**。
- frontend `SYMBOL_ORDER = ["XAUUSD"]`。`buildSymbolGrid` は単独銘柄時に**パネルを常時展開(`has-expanded`)・メイン全域・折りたたみボタン除去**。
- **除去**: 通貨強弱(`analyzer/currency_strength.py`)+ 通貨相関(`analyzer/correlation.py`)を engine/state/serialize/loop/config/test ごと削除、サマリーバー、強弱パネル。`_do_heavy_refresh`+heavy schedule も撤去。obsolete スクリプト `export_for_fx_site.py`/`_profile_dispatch.py` 削除。`HEAVY_REFRESH_SEC` 剪定。
- **残置**: XAUUSDパネル / ヘッダ / 金利(Macro Rates) / 実質金利 / 経済カレンダー / Account / トレード日誌。
- カレンダー通貨は SYMBOLS 派生をやめ `CALENDAR_CURRENCIES = frozenset(FIAT_CURRENCIES)` 固定(金は世界マクロに反応するため USD 単独に狭めない)。
- テスト更新: macro `by_pair` は XAUUSD のみ / connector 解決は XAUUSD のみ、を反映(改竄でなく新挙動)。

### 15.2 DXY(ドル指数)= ドル地合いコンテキスト
- ブローカーは **DXY 先物のみ**(連続スポット無し): `DXY_M6`(6月限)/`DXY_U6`(9月限)等の四半期限月。
- **フロント限月オートロール**(`mt5_connector.resolve_dxy`): ① live(bid≠0)の限月だけ残す(満了限月は bid 0 で自動脱落)→ ② 残った中で**限月コード(M=6月/U=9月…)で最寄り月=フロント**を選択。両限月は同一フィードで同時ティックするため tick 時刻では front/back を区別できない=月コードで判定。`_resolved["DXY"]` に登録し既存 `latest_tick/copy_rates` を再利用。
- `analyzer/dxy_feed.py`: `DxySnapshot`(price/change/EMA/確定足 closes)。analysis ワーカーで `set_dxy`。serialize `dxy` ブロック(full スナップ)。
- frontend: side の DXY カード。**金への影響を主役**に(ドル↑=「金に逆風」赤 / ↓=「金に追風」緑)+ level + 変化 + EMA トレンド + スパークライン。`paintDxy`。
- 定数: `config.DXY_SYMBOL_PREFIX/DXY_CHART_TF/DXY_CHART_BARS/DXY_EMA_PERIOD`。

### 15.3 検証 / コミット
- pytest **327 passed**(通貨強弱/相関テスト削除・DXY +5)。`node --check` OK。
- 実機: サーバ XAUUSD単独起動・`DXY_M6` フロント月解決ログ・WS `dxy` 配信・単独XAUUSDパネル全域+DXYカード描画・コンソールエラー0。
- コミット: `985125b`(XAUUSD特化)→ DXY コミット →(本剪定+本節)。**未 push**(ユーザ「プッシュ」指示待ち)。

### 15.4 残注記
- DXY 先物は限月交代する。`resolve_dxy` は起動時に1回解決(再起動 or 再接続で再解決)。長時間無停止運用で限月跨ぎが心配なら定期再解決を足す余地あり(現状は起動時解決で実用十分)。
- 相関削除に伴い「ポジション集中(同一ベット)警告」も同カード内だったため消滅(相関依存のため)。


## 16. 2026-06-04 — ①残骸コード削除 + ②COT(金先物 投機筋建玉)追加

ユーザ指示「①残骸コード死蔵コード厳格検証・不要なものは全て削除。②GOLD先物ポジション残高+ETF残高取得」。

### 16.1 ① 死蔵コード削除(コミット済み)
- **GoldMacroScore 研究一式**(REJECT 済・再挑戦なし)と **structures パイプライン**(毎サイクル計算するがフロント未消費=旧 §7.1 TODO)を、engine/state/serialize/loop/config/test/spec/plan ごと削除。
- 削除: `analyzer/gold_macro.py` + `_validate_gold_macro.py`/`_experiment_gold_macro.py` + spec/plan/test、`analyzer/{confluence,price_action,structure_detector}.py` + 各 test + `scripts/_verify_phase2.py`。剪定: config の GoldMacroScore FRED 定数、macro_feed の `fetch_gold_drivers`/`parse_fred_series` + gold-driver キャッシュ、state の structures フィールド/setter/property、serialize の `serialize_structures`、loop の structures publish/import。
- **残置(使用継続を確認して残した)**: `structure_types.py`(line_reader の EA ライン PMH/PML 依存)、`STRUCTURE_TFS`(W1 が DWS-SMT H4 スタックに必要・validator の TF 定数マップ)、oos_baseline の Welch、実質金利 DFII10、DXY、Macro Rates。
- 検証: 残骸 grep クリーン(コードは0件・履歴ドキュメントのみ言及)、import OK、pytest 通過、サーバ XAUUSD単独で structures/gold_macro 無しに起動・単独パネル+DXY+macro 描画・コンソールエラー0。

### 16.2 ② COT(CFTC 金先物 投機筋建玉)= 週次ポジショニング/センチメント
- ② のスコープ確定: **GLD ETF 残高(現物トン数)はクリーンな無料フィードが消滅**(SPDR 公式が Next.js 化し PDF/404、Stooq は要 API キー、Yahoo 純資産は crumb 認証で 401)。脆いスクレイパは「壊れるコード禁止」方針に反するため、ユーザ判断で **COT のみ実装**(GLD は見送り)。
- データ源: **CFTC 公開 Socrata API**(`publicreporting.cftc.gov/resource/6dca-aqww.json`, Legacy Futures-Only, **無認証**)。COMEX 金は market 完全一致 `GOLD - COMMODITY EXCHANGE INC.`(`LIKE %GOLD%` だとマイクロ金等が混入するため完全一致)。週次(火曜断面・金曜公表)。
- `analyzer/cot_feed.py`: `CotEngine`(macro/dxy と同型)。`compute()` は HTTP 取得→`parse_cot_rows`(純関数)。投機筋(non-commercial)ネット(long−short)・前週比・実需筋(commercial)ネット・OI・net/OI%・long_share%・**過去52週レンジ内パーセンタイル**(`pctile_1y`)・**extreme フラグ**(±1=1年レンジの上/下限=過熱/手仕舞い)・52週ネット履歴(スパークライン)。失敗時はディスクキャッシュを stale 再利用(`external/cot/cot_cache.json`、`.gitignore` 済)、worker が `MACRO_RETRY_SEC` で早期リトライ。
- **重要な意味**: `extreme=-1` は「ネットが1年レンジの**下限**」=投機筋が1年来で最も手仕舞い(絶対値はネットロングのまま)。`+1` は「上限」=ロング積み上がり(逆張り警戒)。COT は**逆張り/ポジショニング**指標であり方向ドライバーではないため、フロント文言は中立・事実ベース(DXY のような「追風/逆風」断定はしない)。
- 配線: state `_cot/set_cot/cot`、serialize `serialize_cot`(full スナップに `cot`)、loop に `_Schedule("cot", COT_REFRESH_SEC=6h)` + off-thread worker(plain HTTP・MT5 ロック非依存)。
- frontend: side の **COT カード**(DXY の直下、ゴールド枠線)。`paintCot` = ネット大数字+ロング/ショート+前週比矢印 + **1年レンジゲージ**(マーカー位置=pctile)+ extreme ノート + 52週スパークライン(amber、0ライン)+ 実需筋ネット/OI/net%OI フッタ。定数 `config.COT_*`。
- 定数: `config.COT_SOCRATA_URL/COT_GOLD_MARKET/COT_HISTORY_WEEKS(52)/COT_REFRESH_SEC(6h)/COT_FETCH_TIMEOUT_SEC/COT_CACHE_FILE/COT_EXTREME_HIGH_PCT(90)/COT_EXTREME_LOW_PCT(10)`。

### 16.3 検証
- pytest **292 passed**(COT +8)。
- 実機統合: サーバ XAUUSD単独起動・エラー0。**実 WS クライアントで配信スナップを確認** → `cot`: report_date 2026-05-26 / net +154,260(ネットロング)/ 前週比 −5,573 / comm_net −185,766 / OI 353,489 / pctile 1.9%(=1年来低水準)/ 52週履歴。DXY+macro も同時配信(回帰なし)。
- ブラウザ(preview MCP)実描画: カード順 Account→DXY→**COT**→Calendar→Macro→Journal、本文「+154,260 投機筋ネットロング 前週比 -5,573 ▼ … 1年来の低水準—投機筋が手仕舞い 実需筋(ヘッジ) -185,766 OI 353,489 net/OI 43.6%」、ゲージ 1.9%、スパークライン+ノート描画、**コンソールエラー0**。DWS 反転接近グラデも健在。

### 16.4 コミット状況
- ① 死蔵削除はコミット済み(本セッション冒頭)。② COT は**未コミット**(ユーザ確認後にコミット/プッシュ)。未 push 群: `4a7d4b9` 以降 + ① + ②。「プッシュ」指示でまとめて push。


## 17. 2026-06-04 セッション後半 — DWS UI 整理 + トリガー履歴データ整合修正 + 全体フォント統一

### 17.1 ⚠️ フォント規約(最重要・厳守)
- **ダッシュボードのフォントは全体 mono に完全統一済み。今後いかなる修正も現行フォント(mono)に完全準拠させること(ユーザ強指示)。**
- `static/app.css` の `body { font-family: var(--mono) }` が唯一の基底。全テキスト(タイトル/ラベル/数値/本文)はこれを継承して mono。`--mono` = `"JetBrains Mono","Cascadia Mono","Consolas",monospace`。
- `--ui`(Inter/sans)は**定義は残すが未使用**。**新規 CSS で `var(--ui)`/Inter/sans-serif を指定しないこと。** 個別指定が要るなら必ず `var(--mono)`。数値は `font-variant-numeric: tabular-nums` 併用。
- 経緯: 当初は数値だけ mono・文字 sans 混在 → ユーザが「箱ごとに不統一」を繰り返し指摘し「Sans に勝手に倒すな」と却下 → body 基底を mono 化して sans の出所を断った。検証で残存 sans 0 件・サイドバー overflow なしを確認。
- 検証時の罠: preview MCP の既定ビューポートは **幅 87px** と異常に狭く `applyDisplayFit` の scale が 0.034 になる。必ず 1920×1080 に resize + `dispatchEvent(new Event('resize'))` で再フィットしてから計測すること(リロード後は WS 再接続が不安定なので、確実なのは preview 再起動)。

### 17.2 DWS パネル UI 整理(app.js / app.css)
- **OOS 統計ボックス削除**: 16Y ディープ評価の7セルグリッド(勝率/Wilson CI/Bootstrap CI/PF/期待値/Breakeven WR/MaxDD)+ PF スパークラインを UI から除去(ユーザ「枠内情報は不要」)。見出し(信頼/N/DRIFT/説明)+ 地合いバナー + ローリング行は残置。`cell`/`DESC`/`drawValidationSparkline`/`pct`/`fmtPf` 等の死蔵コード+専用 CSS も一掃。
- **ヒストグラム圧縮**: グリッドを4行化し最下部に空のスペーサ行(`grid-template-rows: auto min-content minmax(0,1fr) minmax(0,0.8fr)`)。ヒストグラムは flex 充填のまま約半分の高さに。下部空白は意図的(エクイティカーブ案は試作→ユーザ却下で撤去済み)。
- **時間軸ホバー化**: ヒストグラム下部の静的時間ラベルを削除し、カーソルホバーで該当バーの時刻を下部チップ+縦ガイド線で表示(`drawDwsCanvas` が `canvas._dws` に座標を保存→`ensureDwsSkeleton` のハンドラが参照、canvas 再描画なし)。`dwsAxisLabel` は死蔵化し削除。
- **カラム高さ整合**: 左 analytics の最終カード(時刻別ヒートマップ)を `flex:1 1 auto` で下端まで伸ばし、右 dws カードと下端を一致(`.panel.expanded .anlx-heatmap`)。

### 17.3 トリガー履歴 データ整合バグ修正(trigger_store.py)— 重要
- 症状: ヒストグラム(+172 グロス)と履歴表(−10.4)が乖離。**ヒストは正しく、履歴表(永続ストア)が陳腐値**だった。
- 根本原因: `append_closed` が追記専用・entry_ms 重複排除で**一度書いた値を更新しなかった**。DWS のエグジットは右端付近の最近バーでは未確定(窓スライドで EXIT が移動しうる)→ 早期決済値が凍結された。クロスチェックで古いトレードは全一致・最新1件のみ陳腐と確認(既知の offset/DST バグとは別物)。
- 修正: `append_closed` を **UPSERT** 化(entry_ms キーで新規追加 + 値変更時は更新、窓外=決済確定済みは不変で凍結)。原子的書き換え(temp→os.replace)。`_seen`→`_records`(全レコードキャッシュ)。**`load_by_year` の (size,mtime) キャッシュは upsert 同値長書換でサイズ不変になり自己無効化しないため、書換時に `_by_year_cache.pop(path)` で明示無効化**(これが無いと配信が陳腐化する第2のバグだった)。
- 検証: TDD で赤→緑、`trigger_store` 14/14・全体 **293 passed**。実機で 06/01 16:00 が −10.4→+170 に自己修復、配信スナップでヒスト+172 と履歴+170(=差2pip スプレッド)が整合。

### 17.4 コミット状況
- 本節の作業(17.2/17.3/17.4 フォント)は「プッシュ」指示でコミット&push 済み。トリガーストア修正は独立コミット、UI+フォント+本ハンドオフは別コミット。


## 18. 2026-06-04 セッション終盤 — 右パネル再構成(実質金利リアルタイム化)+ 死蔵コード一掃

### 18.1 右サイドバー再構成
- 並び順: **Account → 実質金利 → DXY → COT → カレンダー → トレード日誌**。**Macro Rates パネルは削除**(米雇用/NFP 含む)。
- **実質金利を独立パネルへ昇格 + DXY型チャート + リアルタイム化**: DFII10(FRED・日次・公表1〜2営業日遅延)は intraday 不可なので、**官製 DFII10 を基準に、ライブ名目10年(CBOE `^TNX` via Yahoo・intraday)の日中変化を加算** = `実質金利 = DFII10 + (TNX現在 − 前日終値)`。実質金利の指標性を保ったまま **~30秒更新**(米国市場開時に動く)。`analyzer/macro_feed.py`: `fetch_nominal_10y()` + `fetch_real_yield_live()`(DFII10 アンカー hourly + ^TNX 30秒)、`RealYieldSnapshot` に `series`(スパークライン用)/`nominal_10y`/`nominal_prev`/`is_live` 追加。`analysis_loop` に `realyield_live`(30秒)スケジュール。frontend `paintRealYield`(DXY の `.dxy-*` 再利用)= 値・前日比・スパークライン・金に追風/逆風・`名目 … · 基準 MM-DD`・`● ライブ`。
- **値は四捨五入せず3桁表示**(`toFixed(3)`)= データ精度の上限(DFII10 2桁 + ^TNX 3桁)。例 `2.106%`(従来 `2.11%`)。
- 自動更新まとめ: 実質金利=~30秒(ライブ)/ DXY=5秒 / COT=6時間。
- `paintMacro` 関数 + macro 専用 CSS は除去。**backend の macro は `dwsTriggerMacroAlign` の counter-carry フォールバック用にのみ残置**(real_yield が主、macro.by_pair は予備)。

### 18.2 死蔵コード一掃(「全体検証→クリア」)
3並列の調査エージェントで全体監査 → 検証して除去:
- `config.py`: §10/§11/§10.4 の vestigial 定数ブロックを丸ごと削除(`ROUND_NUMBER_*`/`FRACTAL_*`/`SessionSpec`/`SESSIONS`/`JST_OFFSET_HOURS`/`PREV_PERIOD_TFS`/`PIN_*`/`INSIDE_BREAK_LOOKBACK`/`PA_KEEP_RECENT`/`CONFLUENCE_*` — 全て全ファイル0参照を確認。構造/PA/合流/セッション backend 削除後の残骸)。
- `static/app.css`: 未使用 CSS 変数 `--ui`(body を mono 化した際に未使用化)。
- `dashboard/lite_server.py`: 未使用定数 `_RESTART_SCRIPT`。
- `scripts/`: `_verify_phase3.py`(削除済み strength/correlation 参照)/`_verify_running.py`(削除済み Dash layout endpoint 参照)= 破損した orphaned スクリプト削除。
- **見送り(意図的)**: ① macro の employment + 非USD利率(EUR/GBP/JPY/AUD)— XAUUSD特化後は dormant だが、稼働中フィード+テスト多数に波及する大規模改修のため別途集中対応推奨(employment は消費者ゼロ=真の dead、非USDは tested/汎用で config 再有効化可能)。② `structure_types.py` の未使用 enum 値(無害・line_reader が使う kept モジュール)。③ `pattern_matcher.py`(「将来 walk-forward 検証後に再導入」と明記の意図的アーカイブ)。
- 検証: 全体 **293 passed**・サーバ XAUUSD単独で正常起動・全パネル描画・順序維持・コンソールエラー0。
