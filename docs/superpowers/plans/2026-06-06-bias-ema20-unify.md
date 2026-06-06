# BIAS EMA20 統一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **本プロジェクト規約: コミット/プッシュはユーザが明示指示した時のみ。** 各タスクの Commit ステップはユーザの go サインがあるまで実行しないこと。フック skip / 署名バイパス / git config 変更は禁止。
>
> ⚠️ **Python 実体**: `C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe`（素の `python` は壊れた Store スタブ）。

**Goal:** TFトレンド/複合BIAS が参照する EMA を全TF(D1/H4/H1/M15)で EMA20 に統一する。

**Architecture:** `config.py` に共有定数 `TREND_EMA_PERIOD=20` を新設し、`TIMEFRAMES` の全 `TimeframeSpec` がそれを参照。`indicator_engine`/`app.js` は `ema_period` 由来の `tf.ema`/`tf.above_ema` を読むだけなので、EMA方向・乖離%・複合BIAS が自動で EMA20 基準になる。複合ロジック(重み・しきい値・レジームゲート)は不変(範囲A)。

**Tech Stack:** Python 3.14 / pytest / MetaTrader5 / Flask + flask-sock / vanilla JS。

**Spec:** `docs/superpowers/specs/2026-06-06-bias-ema20-unify-design.md`

---

## File Structure

- Modify: `config.py:109-115` — `TREND_EMA_PERIOD` 定数追加、`TIMEFRAMES` 全TFが参照、コメント更新。
- Create: `tests/test_config_trend_ema.py` — 統一不変条件のガードテスト。
- 他のコード変更なし(自動追従)。

---

## Task 1: 全TFのトレンドEMAを EMA20 に統一

**Files:**
- Create: `tests/test_config_trend_ema.py`
- Modify: `config.py:109-115`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_trend_ema.py`:

```python
"""ガード: 全TFが統一トレンドEMA期間(② EMA20統一)を参照することを保証する。

1本だけ ema_period が書き換わって「統一」が黙って崩れる事故を防ぐ不変条件テスト。
"""

from __future__ import annotations

import config


def test_trend_ema_period_is_20():
    assert config.TREND_EMA_PERIOD == 20


def test_all_timeframes_use_unified_trend_ema():
    assert config.TIMEFRAMES, "TIMEFRAMES must not be empty"
    for tf in config.TIMEFRAMES:
        assert tf.ema_period == config.TREND_EMA_PERIOD, (
            f"{tf.label} ema_period={tf.ema_period} "
            f"!= TREND_EMA_PERIOD={config.TREND_EMA_PERIOD}"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_config_trend_ema.py -v
```
Expected: FAIL — `AttributeError: module 'config' has no attribute 'TREND_EMA_PERIOD'`（定数未定義のため両テストが収集時/実行時にエラー）。

- [ ] **Step 3: Write minimal implementation**

In `config.py`, replace the current block (lines ≈109-115):

```python
# SPEC 5 / 6.1: D1=EMA200, H4=EMA50, H1=EMA20, M15=EMA13
TIMEFRAMES: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("D1",  mt5.TIMEFRAME_D1,  200, 400),
    TimeframeSpec("H4",  mt5.TIMEFRAME_H4,   50, 300),
    TimeframeSpec("H1",  mt5.TIMEFRAME_H1,   20, 240),
    TimeframeSpec("M15", mt5.TIMEFRAME_M15,  13, 200),
)
```

with:

```python
# 全TF統一トレンドEMA(②, 2026-06-06)。SPEC 5/6.1 の TF別期間(D1=200/H4=50/H1=20/
# M15=13)から意図的に逸脱し、中央オシレーター(ema_stack: 全て EMA20 基準)と物差しを
# 揃える。bars_to_fetch は EMA20 + ADX/RSI/ATR(14) + 履歴に十分なため据え置き。
TREND_EMA_PERIOD: Final[int] = 20
TIMEFRAMES: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("D1",  mt5.TIMEFRAME_D1,  TREND_EMA_PERIOD, 400),
    TimeframeSpec("H4",  mt5.TIMEFRAME_H4,  TREND_EMA_PERIOD, 300),
    TimeframeSpec("H1",  mt5.TIMEFRAME_H1,  TREND_EMA_PERIOD, 240),
    TimeframeSpec("M15", mt5.TIMEFRAME_M15, TREND_EMA_PERIOD, 200),
)
```

(`Final` は既に config.py で import 済み。)

- [ ] **Step 4: Run test to verify it passes**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_config_trend_ema.py -v
```
Expected: PASS（2 passed）。

- [ ] **Step 5: Run full suite to verify no regression**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest -q
```
Expected: `221 passed`（既存 219 + 新規 2）。

- [ ] **Step 6: Commit (ユーザ指示があるまで実行しない)**

```bash
git add config.py tests/test_config_trend_ema.py
git commit -m "feat(bias): unify all-TF trend EMA to EMA20 (TREND_EMA_PERIOD)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 統合検証(サーバ実稼働で無回帰確認)

**Files:** なし(検証のみ)。spec の feedback_integration_verify 準拠。

- [ ] **Step 1: サーバ再起動(バックエンド変更は完全再起動が必須)**

8050 を握る python PID を確認して kill 後、再起動:
```
# PID 確認
Get-NetTCPConnection -LocalPort 8050 -State Listen | Select-Object OwningProcess
# kill
Stop-Process -Id <PID> -Force
# 再起動
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe main.py
```
Expected: ログに `MT5 connected ... TitanFX-MT5-01` / `Analysis loop started`、エラーなし。

- [ ] **Step 2: ブラウザで EMA20 基準への切替を確認**

`http://127.0.0.1:8050/` を開き、TFテーブルを確認:
- D1/H4 行の EMA% が変更前(EMA200/EMA50 基準)と**異なる値**になっている(EMA20 基準)。
- 複合BIAS チップ(-10〜+10)が EMA20 ベースの `above_ema` で再計算されている。
- console エラー 0 / network 失敗 0。

確認手段(gstack browse):
```
B=~/.claude/skills/gstack/browse/dist/browse
"$B" goto http://127.0.0.1:8050/
"$B" console        # エラー無しを確認
"$B" text           # TFテーブル/ BIAS の値を確認
"$B" screenshot "C:/Users/ohuch/Desktop/MT5_Python/_bias_verify.png"   # 視覚確認後に削除
```
Expected: console クリーン、TFテーブルが EMA20 基準で描画。確認後 `_bias_verify.png` を削除。

- [ ] **Step 3: コミット不要(検証のみ)**

---

## Self-Review

**1. Spec coverage:**
- 受け入れ基準1(`TREND_EMA_PERIOD==20` かつ全TF参照) → Task 1 Step 3 + ガードテスト。✓
- 受け入れ基準2(D1/H4 の EMA% が EMA20 基準に変化) → Task 2 Step 2。✓
- 受け入れ基準3(複合BIAS が EMA20 `above_ema` で再計算) → Task 2 Step 2。✓
- 受け入れ基準4(pytest 全緑+新規ガード) → Task 1 Step 4-5。✓
- 受け入れ基準5(サーバ実稼働でエラー無し) → Task 2 Step 1-2。✓
- 非対象(bars_to_fetch/複合ロジック/ヘッダラベル/seeding) → 変更タスク無し。✓

**2. Placeholder scan:** TBD/TODO 無し。全コードブロックは実コード。`<PID>` はランタイム値(プレースホルダではなく実行時に確認する指示)。✓

**3. Type consistency:** `TREND_EMA_PERIOD`(int)・`TimeframeSpec.ema_period`(int)・`config.TIMEFRAMES`(tuple) はテストと実装で一致。✓

ギャップ無し。
