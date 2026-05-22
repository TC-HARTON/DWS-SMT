# 24時間稼働マニュアル

SPEC §18 — Windows で MT5 + Python ダッシュボードを 24/7 で安定稼働させる手順。

## 1. 自動起動シーケンス(SPEC §18.1)

```
1. Windows 起動
2. MT5 自動起動(MT5 のスタートアップオプション利用)
3. MT5 自動ログイン(保存済認証情報)
4. LineExporter EA 自動起動(チャートテンプレート利用)
5. Python 起動(タスクスケジューラ + scripts\start_dashboard.bat)
   - MT5 起動から 30 秒待機後に Python を起動
6. Dash サーバー起動(localhost:8050)
7. ブラウザ自動起動(kiosk モード / F11 全画面)
8. 監視運用開始
```

## 2. MT5 側設定

### 2.1 自動ログイン
1. `ファイル → ログイン` で口座にログイン
2. `ツール → オプション → サーバ` で「保存」をチェック
3. これで MT5 再起動時に自動ログイン

### 2.2 LineExporter EA を全チャートで自動起動

1. `表示 → エキスパートアドバイザ → LineExporter` を XAUUSD のチャートにドラッグ
2. 「コモン → 自動売買を許可する」をチェック
3. チャートを右クリック → `テンプレート → テンプレートの保存` → `Default.tpl` で上書き
4. これで MT5 起動時に同じチャートが復元され、EA が自動アタッチされる

(EA は 1 つだけ貼ればよい — Phase 2 設計で全チャートを巡回する)

### 2.3 MT5 の Windows スタートアップ登録

`ファイル → 自動起動` をチェック、または:

```
ショートカットを「シェル:startup」フォルダ(%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup)に置く。
```

## 3. Python 自動起動

Phase 1 で同梱の `scripts\setup_autostart.bat` を**ユーザー権限で 1 回だけ**実行する:

```cmd
cd C:\Users\ohuch\Desktop\MT5_Python\scripts
setup_autostart.bat
```

これで Windows ログオン時に `start_dashboard.bat` が走り、30 秒待機後に `python main.py` が起動する。

### 確認方法

```cmd
schtasks /Query /TN "MT5_Dashboard"
```

## 4. ブラウザ自動起動

1. `start_dashboard.bat` の末尾に下記を追加:

   ```bat
   timeout /t 5 /nobreak >nul
   start "" "msedge.exe" --kiosk "http://localhost:8050" --edge-kiosk-type=fullscreen
   ```

2. または ブラウザのお気に入りバーに `http://localhost:8050` をピン留めしておき、ログオン時に手動で `F11` 全画面。

## 5. Windows 電源設定(SPEC §18.3)

`コントロール パネル → ハードウェアとサウンド → 電源オプション → 電源プランの設定変更`:

- 電源プラン: **高パフォーマンス**
- ディスプレイの電源を切る: **30 分**(任意)
- コンピューターをスリープ状態にする: **適用しない** ← 必須
- ハードディスクの電源を切る: **適用しない** ← 必須

### より細かい設定

```cmd
powercfg /change standby-timeout-ac 0      :: AC 接続時スリープ無効
powercfg /change hibernate-timeout-ac 0    :: ハイバネーション無効
powercfg /h off                            :: ハイバネーション機能自体オフ
```

## 6. ネットワーク

- 有線推奨。Wi-Fi の場合は「自動再接続」「ローミングなし」を確認
- IPv4 のみ使う場合は IPv6 を無効化するとブローカー再接続が早くなることがある

## 7. 異常時の自動復旧(SPEC §18.4)

| 異常 | 動作 |
|---|---|
| **MT5 接続切断** | Python 側で `analysis_loop._attempt_reconnect` が `MT5_RECONNECT_INTERVAL_SEC=5s` 間隔で再試行 |
| **Forex Factory XML 失敗** | `analyzer.calendar_feed`: 3 回リトライ後、`CALENDAR_FAILURE_FALLBACK_AFTER=2` 連続失敗で MT5 内蔵 Calendar に自動切替 |
| **Python プロセス落ち** | タスクスケジューラのオプションで「タスクが失敗した場合」→「タスクを再起動する(1分後/3回)」を設定 |
| **EA 停止** | MT5 ツールボックス「エキスパート」タブにエラーが残る。Python は最後の `lines_*.json` を保持し続けるので即障害にはならない(SPEC §8.1 一次データの欠落として扱う) |
| **メモリリーク** | 週次再起動推奨(SPEC §18.4)。タスクスケジューラに毎週日曜 03:00 などに `taskkill /IM python.exe /F` + `schtasks /Run /TN MT5_Dashboard` を仕掛ける |
| **ログ肥大** | `analyzer.logging_setup` が `RotatingFileHandler` で 5MB × 5 ローテーション。`logs/dashboard.log.*` が増えなければ OK |

## 8. 週次再起動タスクの例

```cmd
schtasks /Create ^
  /SC WEEKLY /D SUN /ST 03:00 /TN "MT5_Dashboard_Restart" ^
  /TR "powershell -Command \"Stop-Process -Name python -Force; Start-Sleep 5; Start-ScheduledTask -TaskName MT5_Dashboard\"" ^
  /RU "%USERNAME%"
```

## 9. ヘルスチェック

ダッシュボードヘッダの:

- **MT5 ステータス** 緑/赤ドット → 接続状態
- **Compute** ms → SPEC §19 50ms 内なら緑
- **State v.** カウンタ → WS が止まれば増えない

WS が止まっていればブラウザを F5 する(WS 自動再接続を実装済 = `dash_extensions.WebSocket` のデフォルト挙動)。

## 10. 確認チェックリスト

- [ ] MT5 自動ログインが動く(MT5 を一度落として再起動して確認)
- [ ] LineExporter EA がチャート起動と同時にアタッチされる
- [ ] `setup_autostart.bat` を 1 回実行済み
- [ ] `schtasks /Query /TN MT5_Dashboard` で登録確認
- [ ] 電源プランで「スリープしない」に設定済み
- [ ] `python main.py` 手動起動 → http://localhost:8050 が見える
- [ ] ブラウザの自動起動(任意)
- [ ] 週次再起動タスク(任意)
