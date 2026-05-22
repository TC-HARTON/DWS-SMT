# Architecture

実装後の最新構造を 1 枚にまとめたドキュメント。SPEC.md は仕様凍結、こちらは「いま実際に動いている」中身。

## 全体像

```
                ┌──────────────────────────────────────────────────────────┐
                │ Browser  (localhost:8050)                                 │
                │                                                          │
                │  Dash layout (index)                                     │
                │  ↓                                                       │
                │  dash_extensions.WebSocket  →  ws-store (dcc.Store)      │
                │                                  ↓                       │
                │  clientside_callbacks → DOM patch (per panel)            │
                └────────────────────────────────────────┬────────────────┘
                              │ WebSocket /ws            │ dcc.Interval(1s)
                              ↓                          ↓
                ┌──────────────────────────────────────────────────────────┐
                │ Python (single process, multi-thread)                    │
                │                                                          │
                │  Flask  (Dash) ──── flask-sock (ws_broadcaster)          │
                │                          ↑ wait_for_update (Condition)   │
                │                                                          │
                │  AnalysisLoop (daemon thread)                            │
                │   schedule │ interval │ handler          │ engines used  │
                │   ────────┼──────────┼─────────────────┼──────────────  │
                │   price   │   1 s    │ _do_price       │ MT5Connector  │
                │   analysis│   5 s    │ _do_analysis    │ Indicator+    │
                │           │          │   _publish_struc│ Structure+PA+ │
                │           │          │                 │ Confluence    │
                │   heavy   │  30 s    │ _do_heavy       │ Strength+Corr │
                │   history │  60 s    │ _do_history     │ Performance   │
                │   calendar│  60 m    │ _do_calendar    │ CalendarFeed  │
                │            \         │   (daemon worker for HTTP)      │
                │                                                          │
                │  LinesWatcher (watchdog)                                 │
                │   AppData\Roaming\…\Common\Files\lines_*.json → LinesState
                └────────────────┬─────────────────────────────────────────┘
                                 │ MetaTrader5 IPC (mt5.*)
                                 ↓
                ┌──────────────────────────────────────────────────────────┐
                │ MetaTrader 5 EXNESS  (terminal64.exe, 1 instance)        │
                │   LineExporter.mq5 EA on at least one chart              │
                │   → writes lines_{SYMBOL}.json to Common\Files           │
                └──────────────────────────────────────────────────────────┘
```

## モジュールの責務

### `analyzer/` — pure analysis (no Dash, no HTTP server)

| ファイル | 役割 |
|---|---|
| `mt5_connector.py` | **MT5 と話す唯一のモジュール**。`mt5.initialize`/`copy_rates_from_pos`/`account_info`/`positions_get`/`history_deals_get` を thread-safe にラップ。`resolve_symbols` / `resolve_optional` で broker-side suffix を吸収。 |
| `indicators.py` | EMA / ATR / RSI / ADX を numpy + scipy.signal.lfilter で実装。1D / 2D 両対応 — `indicator_engine` が 10 銘柄を 1 つの 2D 行列で渡せるように。 |
| `indicator_engine.py` | TF 単位バッチ計算。10 銘柄 × 4 TF を 1 lfilter ×指標 回呼び出しに集約(SPEC §19 50ms 死守)。 |
| `structure_types.py` | EA 由来 / 自動検出の `StructureLevel` を共通化。`LevelKind` / `LevelSource` / `LevelCategory` 列挙。 |
| `line_reader.py` | watchdog + xmltodict-style JSON 解析(SPEC §9.2)。EA からの命名規約(R_/S_/TL_up_/zone_supply 等)を `_classify` で UI カテゴリに変換。 |
| `structure_detector.py` | PDH/PDL/PWH/PWL/PMH/PML、ラウンド、5 バーフラクタル、セッション高安、当日 VWAP を 1 pass で算出。 |
| `price_action.py` | M15 限定: pin / engulfing(body 完全包含)/ inside / inside-break(N=5 内)/ 3 バー反転。`detect_engulfing` はベクトル化。 |
| `confluence.py` | ATR×0.3 帯の sweep-and-merge。星評価 ★/★★/★★★ は要素数 3/4/5+。 |
| `currency_strength.py` | 27 ペア × 4 窓 → 7 fiat + CAD + XAU の 0–100 score。pair bias は base − quote(SPEC §12.6)。 |
| `correlation.py` | 10 銘柄の H1 close return 相関を 20/100/500 本で。zero-variance NaN は 0 に置換。 |
| `account_monitor.py` | 5y history を 1 fetch、UTC midnight 以降の closed deals + floating positions を `Today P&L` に集約。range 別に win rate / PF / max DD / RR / 銘柄別 / JST 時間帯別。 |
| `calendar_feed.py` | Forex Factory XML → 高インパクト × FIAT のフィルタ。MT5 内蔵 calendar フォールバック。ディスクキャッシュで起動直後にも表示。HTTP は AnalysisLoop の daemon worker 経由(SPEC §14.4 1s tick 死守)。 |
| `state.py` | 全 8 ドメイン snapshot を抱える `LatestState`。`threading.Condition` で WS broadcaster を起こす。 |
| `analysis_loop.py` | 5 schedule (price/analysis/heavy/history/calendar) を 1 daemon thread で駆動。MT5 切断時は 5s 間隔で再試行(SPEC §18.4)。 |
| `logging_setup.py` | rotating file 5MB × 5 + console。`werkzeug` / `flask_sock` 等は WARNING 以上のみ。 |

### `dashboard/` — Dash UI

| ファイル | 役割 |
|---|---|
| `app.py` | `build_app()` — Dash インスタンス、layout 設定、callbacks 登録、WebSocket route mount。 |
| `layout.py` | SPEC §16.5 全体レイアウト。Header / Main (left grid + right account) / Bottom (strength + correlation)。 |
| `serialize.py` | 全 snapshot dataclass を JSON-safe dict に。`_opt_float` で NaN/Inf を null に、`_safe_meta` で cycle guard。 |
| `ws_broadcaster.py` | flask-sock route。`LatestState.wait_for_update` で notification 駆動、`HEARTBEAT_INTERVAL_SEC=15s` で keep-alive。 |
| `callbacks.py` | Phase 1 callbacks(header / account / 10 symbol panels)+ `_inject_helpers` JS namespace。 |
| `callbacks_structures.py` | Phase 2 callbacks(structure list / confluence / PA)。 |
| `callbacks_phase3.py` | Phase 3 callbacks(strength meter / correlation figure / performance / currency bias)+ selector factory。 |
| `callbacks_phase4.py` | Phase 4 callbacks(calendar list / source badge / 1s countdown)。 |
| `components/` | `header`, `symbol_panel`, `account_card`, `calendar_card`, `strength_meter`, `correlation_heatmap`。 |
| `styles/main.css` | SPEC §16.4 ダーク + §17.1 タッチ色 + Phase 3-4 拡張(pill-btn 共通化済み)。 |

### 外部 / 補助

| ファイル | 役割 |
|---|---|
| `mql5_ea/LineExporter.mq5` | 全チャート巡回 + 6 オブジェクト型 → Common\Files JSON。OnChartEvent + OnTimer 1s + 5s 整合性。 |
| `config.py` | **全 tunable constant の唯一の home**(SPEC §23.2)。`SYMBOLS` / `TIMEFRAMES` / `STRUCTURE_TFS` / `STRENGTH_*` / `CORRELATION_*` / `HISTORY_*` / `CALENDAR_*` / `JST_OFFSET_HOURS` 等。 |
| `main.py` | エントリポイント: logging → MT5Connector → AnalysisLoop.start() → Dash app.run()。SIGINT/SIGTERM で clean shutdown。 |
| `scripts/` | 起動 .bat、smoke / verify / profile / memory_watch スクリプト。 |
| `tests/` | pytest 134 件。`mocker.patch("analyzer.mt5_connector.mt5", ...)` で MT5 完全分離。 |
| `docs/` | `ARCHITECTURE.md`(本ファイル)、`RUN_24H.md`(24h 稼働マニュアル)。 |

## データフロー(典型 1 秒)

```
t=0.0s  AnalysisLoop.price tick
        ↓
        MT5Connector.latest_ticks(10 syms)              ~7 ms
        MT5Connector.account_snapshot()                  ~2 ms
        LatestState.set_price() + set_account()
          → Condition.notify_all()
        ↓
        ws_broadcaster._ws_handler resumes from wait_for_update
        ↓
        snapshot_to_json(state)                        ~3 ms
        ws.send(JSON)                                  ~1 ms
        ↓
        Browser: WebSocket 'message' event
        ↓
        clientside callback 'parseMsg' → ws-store
        ↓
        50+ per-symbol clientside callbacks read store
        → DOM mutation (textContent / className)
```

合計 < 50 ms。SPEC §19 WS 100 ms 内。

## 性能(Phase 5 計測)

| 項目 | 実測 | SPEC §19 目標 |
|---|---|---|
| price 周期 | 3.4 ms | 1 s 内 ✓ |
| analysis 周期 | 485 ms | 5 s 内 ✓ |
| heavy 周期(strength+corr) | 187 ms | 30 s 内 ✓ |
| history 周期 | 0.4 ms | 60 s 内 ✓ |
| calendar 周期 | (daemon thread、非ブロック) | 1 h 内 ✓ |
| 指標計算(engine.compute のみ) | ~12 ms | 50 ms 以内 ✓ |
| WebSocket 遅延 | < 50 ms | 100 ms 内 ✓ |
| RSS 定常 | 195 MB | 500 MB 以下 ✓ |
| 24h 連続稼働 メモリ drift | -0.25 MB/min(リーク無し) | リーク無し ✓ |

## スレッドモデル

| thread | 役割 |
|---|---|
| MainThread | Flask 起動・main.py の app.run / signal handlers |
| `mt5-analysis-loop` daemon | 5 schedule の dispatch、`MT5Connector.lock` を保持して IPC 呼び出し |
| `mt5-fetch-*` (`config.SYMBOL_FETCH_WORKERS` workers) | `fetch_rates_parallel` の per-symbol fetch(IPC は connector lock で serialise されるが Python overhead を並列に消化) |
| `indicator-engine-*` (廃止) | Phase 2 で導入 → Phase 2 final で削除(バッチ化が勝った) |
| `calendar-fetch` daemon(都度生成) | Forex Factory HTTP 取得。`_calendar_inflight` Event で重複防止 |
| `process_request_thread` | Flask (waitress 系)が割り当てる WS 接続ごとの worker |
| watchdog Observer daemon | `LinesWatcher` の inotify ループ |

## SPEC 不採用ライン(再確認)

| 項目 | 状態 |
|---|---|
| 通知音 / Popup / ブラウザ通知 / LINE/Telegram/Discord/メール/Webhook | 未実装(SPEC §17.2, §22) |
| 自動売買 / SL/TP 自動操作 / コピートレード / グリッド | 未実装(SPEC §22) |
| ML/AI 予測 / 強化学習 | 未実装(SPEC §22) |
| MA 反発 / 雲タッチ / MACD クロス / RSI 30/70 単独 | シグナル化していない(SPEC §6.5) |
| バックテスト | 未実装(SPEC §22) |
| 履歴ログ蓄積 | rotating file 5MB × 5 のみ(SPEC §22) |

## Phase ロードマップ進捗

- ✅ Phase 1: コア基盤(MT5 / 指標 / Dash / WebSocket)
- ✅ Phase 2: 構造ベース(LineExporter / 構造検出 / PA / コンフルエンス / SPEC §17.1)
- ✅ Phase 3: 統合分析(通貨強弱 / 相関 / 取引履歴 / Today P&L / JST 時間帯別)
- ✅ Phase 4: 外部連携(Forex Factory + MT5 fallback + 24h 稼働手順)
- ✅ Phase 5: 最適化(プロファイル駆動、メモリリーク無し確認、ログ最適化、UI 修正、analysis 2.2x 高速化)
