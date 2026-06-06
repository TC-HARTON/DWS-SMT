# EMA 乖離率 歴史的過伸張バンド Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **コミット/プッシュはユーザ明示指示時のみ。** Commit ステップは go サインまで実行しない。フック skip / 署名バイパス禁止。
> ⚠️ **Python 実体**: `C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe`。
> ⚠️ **スクリプト出力は ASCII のみ**（Windows cp932）。

**Goal:** 16Y(Dukascopy M15)から3乖離率(price vs EMA20/80/320)のパーセンタイル帯を求め、現在値が歴史的に大きい時に既存 readout を色強調/点滅させる。

**Architecture:** ロジックは `analyzer/disparity_bands.py`（compute + load + CSV読み）に集約。`scripts/gen_ema_disparity_bands.py` が CSV→`data/ema_disparity_bands.json` を生成（オフライン・committed・極小）。サーバは起動時に1回ロードし `EmaStackSnapshot.bands` で配信。フロントは既存 readout の乖離率値に p95/p99 ティアの色/点滅クラスを付与。乖離率式・EMA seeding は `app.js:833` の `dr()` と `ema_stack._ema` に完全一致。

**Tech Stack:** Python 3.14 / numpy / pandas / pytest / Flask + flask-sock / vanilla JS + CSS。

**Spec:** `docs/superpowers/specs/2026-06-06-ema-disparity-bands-design.md`

---

## File Structure

- Create: `analyzer/disparity_bands.py` — `compute_bands` / `load_bands` / `read_dukascopy_closes`。
- Create: `scripts/gen_ema_disparity_bands.py` — CSV→JSON 生成 glue（ASCII）。
- Create: `data/ema_disparity_bands.json` — 生成物（committed・極小）。
- Modify: `analyzer/ema_stack.py` — `EmaStackSnapshot.bands` 追加、compute/stale で `load_bands()` 同梱。
- Modify: `dashboard/serialize.py:508-516` — `"bands"` 出力。
- Modify: `static/app.js:756-758, 833-845` — bands stash + readout ティア色/点滅。
- Modify: `static/app.css` — `.ema-overext` / `.ema-overext-x` / `@keyframes ema-blink`。
- Create: `tests/test_disparity_bands.py`, `tests/test_ema_disparity_bands_artifact.py`。

---

## Task 1: `analyzer/disparity_bands.py` — バンド算出/ロード (TDD)

**Files:**
- Create: `analyzer/disparity_bands.py`
- Test: `tests/test_disparity_bands.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_disparity_bands.py`:

```python
"""disparity_bands: 乖離率パーセンタイル帯の算出 + ロード + CSV読み。"""

from __future__ import annotations

import json

import numpy as np

from analyzer.disparity_bands import (
    compute_bands,
    load_bands,
    read_dukascopy_closes,
)


def test_compute_bands_structure_and_ordering():
    rng = np.random.default_rng(0)
    closes = 1000 + np.cumsum(rng.normal(0, 1.0, 5000))
    bands = compute_bands(closes, periods=(20, 80, 320))
    assert set(bands) == {"ema20", "ema80", "ema320"}
    for key in bands:
        for side in ("pos", "neg"):
            s = bands[key][side]
            assert set(s) == {"p90", "p95", "p99", "max", "n"}
            assert s["p90"] <= s["p95"] <= s["p99"] <= s["max"]
            assert s["n"] >= 0


def test_compute_bands_pos_neg_split():
    closes = np.concatenate([
        np.full(400, 100.0),
        np.linspace(100, 130, 200),   # rally -> price above EMA (pos)
        np.linspace(130, 90, 300),    # drop  -> price below EMA (neg)
    ])
    bands = compute_bands(closes, periods=(20, 80, 320))
    assert bands["ema20"]["pos"]["n"] > 0
    assert bands["ema20"]["neg"]["n"] > 0


def test_load_bands_missing(tmp_path):
    assert load_bands(tmp_path / "nope.json") is None


def test_load_bands_valid(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"bands": {"ema20": {"pos": {}, "neg": {}}}}),
                 encoding="utf-8")
    assert load_bands(p) == {"ema20": {"pos": {}, "neg": {}}}


def test_load_bands_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_bands(p) is None


def test_read_dukascopy_closes(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        "Time (EET),Open,High,Low,Close,Volume \n"
        "2010.01.01 00:00:00,1.0,2.0,0.5,1.5,10\n"
        "2010.01.01 00:15:00,1.5,2.5,1.0,2.0,11\n",
        encoding="utf-8",
    )
    out = read_dukascopy_closes(p)
    assert list(out) == [1.5, 2.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_disparity_bands.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'analyzer.disparity_bands'`.

- [ ] **Step 3: Write the implementation**

Create `analyzer/disparity_bands.py`:

```python
"""Historical EMA-disparity bands for the oscillator readout (feature 1).

compute_bands (offline, called by scripts/gen_ema_disparity_bands.py) turns a
long close series into per-EMA, per-side percentile thresholds of the disparity
ratio (close-EMA)/EMA*100 -- the same metric the oscillator readout shows
(static/app.js dr()). The EMA is the first-value-seeded ewm used everywhere
(analyzer.ema_stack._ema).

load_bands (runtime) reads the committed JSON once (cached) and returns the
"bands" sub-object, or None when absent/unreadable -> the readout degrades to
no coloring.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

_PCTLS = (90, 95, 99)


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """Causal first-value-seeded EMA, identical to analyzer.ema_stack._ema."""
    return pd.Series(values).ewm(span=period, adjust=False).mean().to_numpy()


def _side_stats(disp: np.ndarray) -> dict:
    """Percentile thresholds (abs %) + max + n for one side's disparities.

    *disp* are the signed disparities for one side (all > 0 or all < 0). Stats
    are taken on the absolute value so pos / neg are symmetric to consume."""
    a = np.abs(disp)
    if a.size == 0:
        return {"p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "n": 0}
    out = {f"p{p}": float(np.percentile(a, p)) for p in _PCTLS}
    out["max"] = float(a.max())
    out["n"] = int(a.size)
    return out


def compute_bands(closes, periods=config.EMA_STACK_PERIODS) -> dict:
    """Per-EMA, per-side disparity bands from a close series.

    Drops the first max(periods) bars (EMA warm-up) before collecting
    disparities. Returns {"ema20": {"pos": {...}, "neg": {...}}, ...}."""
    closes = np.asarray(closes, dtype=float)
    warm = max(periods)
    bands: dict = {}
    for p in periods:
        ema = _ema(closes, p)
        disp = (closes - ema) / ema * 100.0
        disp = disp[warm:]
        disp = disp[np.isfinite(disp)]
        bands[f"ema{p}"] = {
            "pos": _side_stats(disp[disp > 0]),
            "neg": _side_stats(disp[disp < 0]),
        }
    return bands


def read_dukascopy_closes(path: Path) -> np.ndarray:
    """Close column from a Dukascopy CSV (Time (EET),Open,High,Low,Close,Volume)."""
    df = pd.read_csv(path, usecols=["Close"])
    return df["Close"].to_numpy(dtype=float)


def _read(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        bands = doc.get("bands")
        return bands if isinstance(bands, dict) else None
    except (OSError, ValueError):
        return None


_cached: dict | None = None
_cached_done = False


def load_bands(path: Path | None = None) -> dict | None:
    """Return the "bands" dict from the committed JSON, or None.

    The default-path load is cached (called every analysis cycle). An explicit
    *path* (tests) bypasses the cache."""
    if path is not None:
        return _read(path)
    global _cached, _cached_done
    if not _cached_done:
        _cached = _read(config.PROJECT_ROOT / "data" / "ema_disparity_bands.json")
        _cached_done = True
    return _cached
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_disparity_bands.py -v
```
Expected: PASS (6 passed).

- [ ] **Step 5: Commit (go サインまで実行しない)**

```bash
git add analyzer/disparity_bands.py tests/test_disparity_bands.py
git commit -m "feat(disparity): EMA-disparity band compute/load module

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 生成スクリプト + JSON 生成 + スキーマ検証

**Files:**
- Create: `scripts/gen_ema_disparity_bands.py`
- Create: `data/ema_disparity_bands.json` (生成物)
- Test: `tests/test_ema_disparity_bands_artifact.py`

- [ ] **Step 1: Write the failing artifact test**

Create `tests/test_ema_disparity_bands_artifact.py`:

```python
"""committed data/ema_disparity_bands.json のスキーマ検証。"""

from __future__ import annotations

import json

import config


def test_committed_bands_artifact_schema():
    path = config.PROJECT_ROOT / "data" / "ema_disparity_bands.json"
    assert path.exists(), "run scripts/gen_ema_disparity_bands.py first"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["tf"] == config.EMA_STACK_TF
    assert doc["periods"] == list(config.EMA_STACK_PERIODS)
    bands = doc["bands"]
    expected_keys = {f"ema{p}" for p in config.EMA_STACK_PERIODS}
    assert set(bands) == expected_keys
    for key in expected_keys:
        for side in ("pos", "neg"):
            s = bands[key][side]
            assert set(s) == {"p90", "p95", "p99", "max", "n"}
            assert s["p90"] <= s["p95"] <= s["p99"] <= s["max"]
            assert s["n"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_ema_disparity_bands_artifact.py -v
```
Expected: FAIL — `AssertionError: run scripts/gen_ema_disparity_bands.py first`（JSON 未生成）。

- [ ] **Step 3: Write the generator script**

Create `scripts/gen_ema_disparity_bands.py`:

```python
"""Generate data/ema_disparity_bands.json from the 16Y Dukascopy M15 Bid CSV.

Offline tool (run on the machine that holds the bulk CSV). Reads the close
series, computes per-EMA per-side disparity percentile bands
(analyzer.disparity_bands), and writes the small committed JSON the server
serves to the oscillator readout. ASCII-only output (Windows cp932).

Usage:
    python scripts/gen_ema_disparity_bands.py [csv_path]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from analyzer.disparity_bands import compute_bands, read_dukascopy_closes

DEFAULT_CSV = config.PROJECT_ROOT / "XAUUSD_15 Mins_Bid_2010.01.01_2025.12.31.csv"
OUT_PATH = config.PROJECT_ROOT / "data" / "ema_disparity_bands.json"


def main(argv: list[str]) -> int:
    csv_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        print("ERROR: CSV not found: %s" % csv_path)
        return 1
    closes = read_dukascopy_closes(csv_path)
    bands = compute_bands(closes, periods=config.EMA_STACK_PERIODS)
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": csv_path.name,
        "tf": config.EMA_STACK_TF,
        "periods": list(config.EMA_STACK_PERIODS),
        "bands": bands,
    }
    OUT_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print("OK wrote %s (bars=%d)" % (OUT_PATH, closes.size))
    for key, b in bands.items():
        print("  %-6s pos p95=%.3f p99=%.3f max=%.3f n=%d | "
              "neg p95=%.3f p99=%.3f max=%.3f n=%d" % (
                  key,
                  b["pos"]["p95"], b["pos"]["p99"], b["pos"]["max"], b["pos"]["n"],
                  b["neg"]["p95"], b["neg"]["p99"], b["neg"]["max"], b["neg"]["n"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run the generator to produce the JSON**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe scripts/gen_ema_disparity_bands.py
```
Expected: `OK wrote .../data/ema_disparity_bands.json (bars=~380000)` + per-EMA pos/neg lines. Sanity: ema320 pos max は数十%オーダー、p95 < p99 < max。

- [ ] **Step 5: Run the artifact test to verify it passes**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_ema_disparity_bands_artifact.py -v
```
Expected: PASS (1 passed).

- [ ] **Step 6: Commit (go サインまで実行しない)**

```bash
git add scripts/gen_ema_disparity_bands.py data/ema_disparity_bands.json tests/test_ema_disparity_bands_artifact.py
git commit -m "feat(disparity): generate 16Y EMA-disparity bands JSON

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: スナップショット同梱 + serialize (TDD)

**Files:**
- Modify: `analyzer/ema_stack.py` (dataclass + _stale_snapshot + compute return)
- Modify: `dashboard/serialize.py:508-516`
- Test: `tests/test_disparity_bands.py` (追記)

- [ ] **Step 1: Write the failing test (append to tests/test_disparity_bands.py)**

```python
def test_serialize_ema_stack_includes_bands():
    from analyzer.ema_stack import EmaStackSnapshot
    from dashboard.serialize import serialize_ema_stack

    snap = EmaStackSnapshot(
        symbol="XAUUSD", periods=(20, 80, 320),
        price=4000.0, ema_fast=4000.0, ema_mid=3990.0, ema_center=3950.0,
        times_ms=(1, 2), dev_price=(0.1, 0.2), dev_fast=(0.0, 0.1),
        dev_mid=(0.0, 0.0), as_of=1.0, stale=False,
        bands={"ema20": {"pos": {}, "neg": {}}},
    )
    out = serialize_ema_stack(snap)
    assert out["bands"] == {"ema20": {"pos": {}, "neg": {}}}
    assert serialize_ema_stack(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_disparity_bands.py::test_serialize_ema_stack_includes_bands -v
```
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'bands'`.

- [ ] **Step 3a: Add the `bands` field to EmaStackSnapshot**

In `analyzer/ema_stack.py`, add (with default so other constructors keep working) at the end of the dataclass (after `stale: bool`):

```python
    stale: bool                        # True when no live data
    bands: dict | None = None          # 16Y disparity percentile bands (feature 1)
```

- [ ] **Step 3b: Load bands in compute + stale snapshots**

In `analyzer/ema_stack.py`, add the import near the top (after `import config`):

```python
from analyzer.disparity_bands import load_bands
```

In `_stale_snapshot`, add `bands=load_bands()` to the constructor call (before the closing `)`):

```python
        times_ms=(), dev_price=(), dev_fast=(), dev_mid=(),
        as_of=time.time(), stale=True, bands=load_bands(),
    )
```

In `compute_ema_stack`'s successful return, add `bands=load_bands()` (after `stale=False,`):

```python
        as_of=time.time(),
        stale=False,
        bands=load_bands(),
    )
```

- [ ] **Step 3c: Emit bands from serialize**

In `dashboard/serialize.py`, in `serialize_ema_stack` return dict, add after `"ema_center": ...` (keep the rest):

```python
        "ema_center": _opt_float(s.ema_center),
        "bands": s.bands,
        "t": list(s.times_ms),
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest tests/test_disparity_bands.py tests/test_ema_stack.py -v
```
Expected: PASS (all, incl. existing test_ema_stack.py — `bands` default None keeps them green).

- [ ] **Step 5: Commit (go サインまで実行しない)**

```bash
git add analyzer/ema_stack.py dashboard/serialize.py tests/test_disparity_bands.py
git commit -m "feat(disparity): carry bands on EmaStackSnapshot + serialize

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: フロント readout 色/点滅

**Files:**
- Modify: `static/app.js` (paintEmaStack ~756-758, _emaRender readout ~833-845)
- Modify: `static/app.css`

- [ ] **Step 1: Stash bands on el._ema**

In `static/app.js` paintEmaStack, the `el._ema = { ... }` literal (around line 756-758), add `bands`:

```javascript
    el._ema = { t: d.t, dp: d.dev_price, df: d.dev_fast, dm: d.dev_mid, n,
                price: d.price, ema_fast: d.ema_fast, ema_mid: d.ema_mid,
                ema_center: d.ema_center, bands: d.bands || null };
```

- [ ] **Step 2: Add the overextension tier + wrap readout values**

In `_emaRender`, after the existing `const upCls = ...` line (around 836) and before `const read =`, add:

```javascript
    // (1) overextension tier from the 16Y bands: |乖離率| >= p99 blinks, >= p95
    // warns. side picked by sign; absent bands => no class (feature degrades).
    const B = data.bands || null;
    const oxTier = (val, band) => {
        if (val == null || !band) return '';
        const side = val >= 0 ? band.pos : band.neg;
        if (!side) return '';
        const a = Math.abs(val);
        if (side.p99 && a >= side.p99) return ' ema-overext-x';
        if (side.p95 && a >= side.p95) return ' ema-overext';
        return '';
    };
    const spO = (val, key) =>
        `<span class="ema-val${oxTier(val, B && B[key])}">${sp(val)}</span>`;
```

Then change the three readout value spans to use `spO(...)`:

```javascript
      + `<span class="ema-k"><i class="ema-dot" style="background:#ffb74d"></i>EMA20 ${spO(dr(data.ema_fast), 'ema20')}</span>`
      + `<span class="ema-k"><i class="ema-dot" style="background:#4d8eff"></i>EMA80 ${spO(dr(data.ema_mid), 'ema80')}</span>`
      + `<span class="ema-k"><i class="ema-dot ema-dot-center"></i>EMA320 ${spO(d320, 'ema320')}</span>`
```

- [ ] **Step 3: Add CSS (keep mono font)**

Append to `static/app.css`:

```css
/* (1) EMA-disparity overextension on the oscillator readout. Font stays mono;
   only colour/weight + a subtle blink at the >=p99 extreme. */
.ema-val.ema-overext   { color: #ffb02e; font-weight: 600; }
.ema-val.ema-overext-x { color: #ff5b6b; font-weight: 700;
                         animation: ema-blink 1s steps(2, start) infinite; }
@keyframes ema-blink { 50% { opacity: 0.25; } }
```

- [ ] **Step 4: Syntax check**

Run:
```
node --check static/app.js
```
Expected: no output (valid). (CSS は構文チェック不要。)

- [ ] **Step 5: Commit (go サインまで実行しない)**

```bash
git add static/app.js static/app.css
git commit -m "feat(disparity): colour/blink oscillator readout on historic overextension

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 統合検証(サーバ実稼働 + ブラウザ)

**Files:** なし(検証のみ)。feedback_integration_verify 準拠。

- [ ] **Step 1: 全スイート緑を確認**

Run:
```
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest -q
```
Expected: all passed（221 + 新規 8 = 229 目安）。

- [ ] **Step 2: サーバ再起動(バックエンド変更=完全再起動必須)**

```
Get-NetTCPConnection -LocalPort 8050 -State Listen | Select-Object OwningProcess
Stop-Process -Id <PID> -Force
C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe main.py
```
Expected: `Analysis loop started`、エラーなし。

- [ ] **Step 3: ブラウザで bands 配信と色/点滅ロジックを確認**

```
B=~/.claude/skills/gstack/browse/dist/browse
"$B" goto http://127.0.0.1:8050/
"$B" console        # エラー0
# bands が届いているか + 現在の各乖離率とティア判定
"$B" js "(()=>{const e=document.querySelector('.emastack');const d=(window.EMA_HISTORY)||(window.latestSnap&&window.latestSnap.ema_stack);const b=d&&d.bands;const vals=[...document.querySelectorAll('.ema-val')].map(x=>x.className+'='+x.textContent);return JSON.stringify({hasBands:!!b, ema20:b&&b.ema20, vals});})()"
"$B" screenshot "C:/Users/ohuch/Desktop/MT5_Python/_disp_verify.png"
```
Expected: `hasBands:true`、`vals` に各乖離率。現在値が p95/p99 未満なら `ema-val`(無印=平常)で正しい。p95/p99 超のEMAがあれば `ema-overext`/`ema-overext-x` が付与され、スクショで色/点滅を確認。console エラー0。確認後 `_disp_verify.png` 削除。

- [ ] **Step 4: コミット不要(検証のみ)**

---

## Self-Review

**1. Spec coverage:**
- 乖離率定義=readout `dr()` 一致 → Task 1 `_ema`/`compute_bands`(同式)。✓
- 16Y分布→per-EMA×per-side×p90/95/99+max+n → Task 1 `_side_stats`/`compute_bands` + Task 2 生成。✓
- committed JSON スキーマ → Task 2 artifact test。✓
- サーバ起動ロード+full同梱+欠如時None → Task 3 `load_bands`(cached) + dataclass default None + serialize。✓
- readout p95色/p99点滅・bands=null時不変・mono維持 → Task 4。✓
- 受け入れ基準1-5 → Task 2(1)/Task 3(2)/Task 4(3)/Task 1+全体(4)/Task 5(5)。✓

**2. Placeholder scan:** TBD/TODO 無し。`<PID>` は実行時確認値。`...`/`0.0` はテストのスキーマ例。全コードブロック実コード。✓

**3. Type consistency:** `compute_bands`→dict、`load_bands`→dict|None、`EmaStackSnapshot.bands: dict|None=None`、serialize `"bands": s.bands`、フロント `d.bands`→`el._ema.bands`→`data.bands`→`oxTier`。キー `ema{p}`(ema20/ema80/ema320) が生成・スキーマ・フロントで一致。`pos/neg`・`p90/p95/p99/max/n` 一貫。✓

ギャップ無し。
