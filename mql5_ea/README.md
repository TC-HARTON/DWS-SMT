# LineExporter EA

MT5 上で描画したトレンドライン・水平線・チャネル等を JSON にエクスポートし、
Python ダッシュボードの `analyzer/line_reader.py` が読み取って構造レベルとして表示するためのエキスパートアドバイザ。

## 設置とコンパイル

ソース `LineExporter.mq5` は本リポジトリ管理用に `mql5_ea/` に置いてあります。
Phase 2 セットアップ時に自動で MT5 の Experts フォルダにもコピー済み:

```
C:\Users\ohuch\AppData\Roaming\MetaQuotes\Terminal\53785E099C927DB68A545C249CDBCE06\MQL5\Experts\LineExporter.mq5
```

(Hash `53785E099...` は EXNESS 1 つ目のターミナルに紐づくフォルダ)

### コンパイル(初回 / 更新時)

1. MT5 を開く
2. 上部ツールバー「IDE」または `F4` で MetaEditor を起動
3. ナビゲータ左ペインで Experts → `LineExporter.mq5` をダブルクリック
4. `F7` でコンパイル(下部「ツールボックス」に `0 error(s), 0 warning(s)` が出ればOK)
5. MetaEditor を閉じる

## チャートへの貼り付け

監視したい銘柄のチャートを 1 つ開き(SPEC §7 の 10 銘柄が対象)、
ナビゲータ → Expert Advisors → `LineExporter` をチャートにドラッグ。
ダイアログでは:

- **コモン** タブ → 「自動売買を許可する」にチェック(描画読取りのみで発注はしないが、EA 動作のため)
- **入力パラメータ** タブ → デフォルトのままで OK

**1 つの EA が `ChartFirst()`/`ChartNext()` で全チャートを巡回します**。
複数チャートを開いていても、貼り付けるのは 1 つのチャートだけで十分。
ただし、ライン描画を *即時* 反映したいチャートには EA を直接貼ると `OnChartEvent`
経由で 100ms 未満の即時反映になります(他チャートは 1 秒以内)。

## 出力先

```
C:\Users\ohuch\AppData\Roaming\MetaQuotes\Terminal\Common\Files\lines_{SYMBOL}.json
```

例: `lines_XAUUSD.json`, `lines_USDJPY.json` ...

Python `analyzer.line_reader` がこのフォルダを `watchdog` で監視し、
ファイル更新を検知すると即時パースしてダッシュボードに反映します(SPEC §9.4 1 秒以内)。

## 命名規則(SPEC §9.3)

| 接頭辞 | 意味 |
|---|---|
| `R_` `R1_` `R2_` | レジスタンス |
| `S_` `S1_` `S2_` | サポート |
| `TL_up_` | 上昇トレンドライン |
| `TL_dn_` | 下降トレンドライン |
| `zone_supply` | 売り需要ゾーン |
| `zone_demand` | 買い需要ゾーン |
| `_strong` `_major` `_weak` | 重要度修飾子 |
| `_D1` `_H4` `_H1` | 描画基準TF |

Python 側は接頭辞からカテゴリ・重要度を自動分類します。

## トラブルシュート

- 出力ファイルができない → 「ツール」→「オプション」→「エキスパート」→
  「自動売買を許可する」にチェック
- JSON が更新されない → 「エキスパート」タブで `LineExporter:` のログを確認
- Python 側で読み込まれない → `Common\Files` フォルダのパスが
  `LINES_DIR`(.env / config.py)と一致しているか確認
