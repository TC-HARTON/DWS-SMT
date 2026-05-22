# MT5-Python Trading Dashboard

完全裁量・マルチタイムフレーム前提のローカル統合ダッシュボード。
詳細仕様は [SPEC.md](SPEC.md) を参照。

## 構成

- **MT5** — 注文端末・チャート描画・TL/SR 描画
- **MQL5 EA(LineExporter)** — 描画ライン → JSON
- **Python(本リポジトリ)** — 分析エンジン + Dash サーバー
- **Browser** — localhost:8050 で全銘柄ダッシュボード

## クイックスタート

```cmd
:: 1) 依存パッケージインストール(初回のみ)
scripts\install_dependencies.bat

:: 2) MT5 を起動 + 任意の銘柄チャートに LineExporter EA をドラッグ
:: 詳細は mql5_ea\README.md

:: 3) ダッシュボード起動
scripts\start_dashboard.bat

:: 4) ブラウザで http://127.0.0.1:8050 を開く(F11 全画面 / kiosk モード推奨)
```

## 機能(Phase 別)

### Phase 1 — コア基盤
- MT5 接続 / 10 銘柄並列取得 / EMA・ADX・RSI・ATR
- Dash UI / WebSocket 1s 配信 / 銘柄パネル / 口座カード

### Phase 2 — 構造ベース
- LineExporter EA(MQL5) + watchdog
- PDH/PDL/PWH/PWL/PMH/PML / ラウンド / フラクタル / セッション高安 / VWAP
- M15 プライスアクション(pin/engulfing/inside/break/3-bar reversal)
- ATR×0.3 以内コンフルエンス + SPEC §17.1 4 段階タッチ色

### Phase 3 — 統合分析
- 7 fiat + XAU 通貨強弱(H1/H4/D1/W1 切替)
- 10 銘柄相関ヒートマップ(20/100/500 本切替)
- 取引履歴 + パフォーマンス(勝率/PF/最大DD/銘柄別/JST 時間帯別/当日P&L)
- 銘柄パネルに Currency Bias

### Phase 4 — 外部連携・運用化
- Forex Factory XML カレンダー(1h 更新、MT5 内蔵 Calendar 自動フォールバック)
- 1秒カウントダウン + 発表前後 30 分警告色
- [24時間稼働マニュアル](docs/RUN_24H.md)

### Phase 5 — 最適化(継続)
- プロファイル駆動: analysis 周期 1060ms → 485ms(2.2x)、warmup 25.6s → 1.2s(22x)
- メモリリーク調査: 5min 計測でドリフト無し、RSS 195 MB / 500 MB 内
- ログ最適化: WS 接続/切断を DEBUG に降格(116 行/5min → 16 行)
- UI 微調整: 銘柄パネル比率を SPEC §7.2 準拠の `35fr 35fr 30fr` に
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) 新規作成 — Phase 1-5 の実装後構造を 1 枚に

## ディレクトリ

```
analyzer/        # Python 分析エンジン
dashboard/       # Dash UI(layout / callbacks / components / styles)
mql5_ea/         # LineExporter.mq5(MQL5)
data/lines/      # 旧用途のプレースホルダ(現運用では未使用、EA は Common\Files に書く)
external/forex_factory_xml/   # 経済指標 XML キャッシュ
docs/            # ARCHITECTURE.md / RUN_24H.md
scripts/         # 起動・インストール・スモークテスト・プロファイル・メモリ監視
tests/           # pytest 134 件(MT5 モックで完全分離)
config.py        # 全定数(SPEC §23.2)
main.py          # エントリポイント
SPEC.md          # 仕様書(改変禁止)
```

詳細な実装構造は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照。

## テスト

```cmd
"%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" -m pytest -q
```

## 不採用機能(SPEC §22)

通知系全般 / 自動売買 / ML 予測 / バックテスト / コピートレード — 実装しません。

## 開発支援

Claude Code(本プロジェクト主開発支援)。
