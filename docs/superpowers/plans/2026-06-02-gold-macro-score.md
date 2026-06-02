# GoldMacroScore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an XAUUSD-specific macro composite (GoldMacroScore) from 4 FRED daily series, flow it to the WS snapshot, then validate it (IC + OOS gate) before exposing any UI.

**Architecture:** A pure scoring module (`analyzer/gold_macro.py`) computes a `-10..+10` level-z equal-weight composite from per-driver level histories. `MacroEngine` gains a `fetch_gold_drivers()` method (reusing the existing `_fred_get` HTTP + disk-cache plumbing). `LatestState` + `serialize.py` carry the snapshot on the FULL WS payload only. An offline validation script (`scripts/_validate_gold_macro.py`) decides ADOPT/REJECT. UI + gate promotion are P3, gated on the verdict.

**Tech Stack:** Python 3.14 (full path `C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe`), numpy, pandas, requests, FRED API, pytest. Console is cp932 → ASCII-only script output.

**Reference spec:** `docs/superpowers/specs/2026-06-02-gold-macro-score-design.md`

**Conventions (from SESSION_HANDOFF):**
- Run tests: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q`
- Backend changes need a FULL process restart (kill PID on 8050, relaunch).
- No bare excepts; type hints + docstrings on all new code.
- `gold_macro` rides the FULL snapshot only, never the ~2 Hz light path.
- Commits only stage the files named in each task. End commit messages with the project's Co-Authored-By trailer.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `analyzer/gold_macro.py` | Pure driver registry + z-score/sign/composite math + dataclasses | Create |
| `analyzer/macro_feed.py` | `fetch_gold_drivers()` + full-observation list parser + cache | Modify |
| `analyzer/state.py` | `_gold_macro` field, setter, property, full-snapshot inclusion | Modify |
| `dashboard/serialize.py` | `serialize_gold_macro()` + wire into `snapshot_to_json` | Modify |
| `config.py` | New FRED series IDs + GoldMacroScore constants | Modify |
| `scripts/_validate_gold_macro.py` | Offline IC + OOS-gate validation, ASCII verdict | Create (P2) |
| `tests/test_gold_macro.py` | Math/sign/missing/band/clamp unit tests | Create |
| `tests/test_macro_feed.py` | List-parser + per-series-failure + cache tests | Modify |
| `tests/test_state_and_serialize.py` | `gold_macro` round-trip + light-snapshot absence | Modify |

---

# PHASE 1 — Score pipeline

## Task 1: Config constants

**Files:**
- Modify: `config.py` (after line 351, the real-yield block)

- [ ] **Step 1: Add the GoldMacroScore constants**

Insert immediately after the `MACRO_REALYIELD_REFRESH_SEC` line (currently line 351):

```python

# GoldMacroScore — XAUUSD-specific macro composite (spec
# docs/superpowers/specs/2026-06-02-gold-macro-score-design.md). Four daily
# FRED drivers fused into a -10..+10 level-z equal-weight score on the BIAS
# scale. DFII10 is shared with the real-yield layer above but re-fetched with a
# long window here for z-scoring.
MACRO_FRED_BREAKEVEN_SERIES: Final[str] = "T10YIE"     # 10Y breakeven inflation, daily
MACRO_FRED_VIX_SERIES: Final[str] = "VIXCLS"           # CBOE VIX close, daily
MACRO_FRED_DXY_SERIES: Final[str] = "DTWEXBGS"         # broad trade-weighted USD index, daily
GOLD_MACRO_WINDOW: Final[int] = 252                    # ~1Y trading days for z-score
GOLD_MACRO_Z_CLAMP: Final[float] = 2.5                 # cap per-driver tail leverage
GOLD_MACRO_BAND_THRESHOLD: Final[float] = 3.0          # |score| >= this → tailwind/headwind band
GOLD_MACRO_REFRESH_SEC: Final[float] = 3600.0          # 1 hour (drivers are daily-moving)
```

- [ ] **Step 2: Verify it imports**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -c "import config; print(config.GOLD_MACRO_WINDOW, config.MACRO_FRED_BREAKEVEN_SERIES, config.MACRO_FRED_VIX_SERIES, config.MACRO_FRED_DXY_SERIES)"`
Expected: `252 T10YIE VIXCLS DTWEXBGS`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): GoldMacroScore FRED series + composite constants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `analyzer/gold_macro.py` — dataclasses + driver registry

**Files:**
- Create: `analyzer/gold_macro.py`
- Test: `tests/test_gold_macro.py`

- [ ] **Step 1: Write the failing test for the registry**

Create `tests/test_gold_macro.py`:

```python
"""Unit tests for the GoldMacroScore composite (analyzer/gold_macro.py)."""

from __future__ import annotations

import math

import pytest

from analyzer import gold_macro as gm


def test_driver_registry_is_the_four_spec_drivers():
    keys = [d.key for d in gm.GOLD_DRIVERS]
    assert keys == ["real_yield", "breakeven", "vix", "dxy"]
    signs = {d.key: d.sign_gold for d in gm.GOLD_DRIVERS}
    # Rising real yield / dollar = headwind (-1); rising inflation / VIX = tailwind (+1).
    assert signs == {"real_yield": -1, "breakeven": +1, "vix": +1, "dxy": -1}
    # Every driver carries a FRED series id and a Japanese label.
    for d in gm.GOLD_DRIVERS:
        assert d.series_id and isinstance(d.series_id, str)
        assert d.label_ja and isinstance(d.label_ja, str)
```

- [ ] **Step 2: Run it to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_gold_macro.py::test_driver_registry_is_the_four_spec_drivers -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analyzer.gold_macro'`

- [ ] **Step 3: Create `analyzer/gold_macro.py` with dataclasses + registry**

```python
"""GoldMacroScore — an XAUUSD-specific macro composite (pure, testable).

Fuses four daily FRED drivers into one ``-10..+10`` score on the same scale as
the dashboard BIAS, the gold analogue of the Buffett Indicator. Each driver is
z-scored over a trailing window of its LEVEL, sign-adjusted to gold's expected
direction, clamped, then equal-weighted and rescaled. Equal weighting (no
fitting) is deliberate: weight-fitting on history is the primary overfitting
risk, so the prototype stays untuned and its edge is decided by the offline
IC + OOS validation, not by in-sample tuning.

This module is pure — no network, no MT5, no I/O — so it is fully unit-testable.
``MacroEngine.fetch_gold_drivers`` supplies the level histories; the analysis
loop calls :func:`compute_gold_macro_score` and stores the snapshot.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import config


@dataclass(frozen=True)
class GoldDriver:
    """One macro driver feeding the composite."""

    key: str
    series_id: str
    sign_gold: int          # +1 = rising value is bullish gold, -1 = bearish
    label_ja: str


@dataclass(frozen=True)
class GoldDriverContribution:
    """A driver's resolved contribution for one snapshot."""

    key: str
    label_ja: str
    value: float            # latest level
    z: float                # raw z-score over the window
    signed_z: float         # sign_gold * clamp(z)
    sign_gold: int


@dataclass(frozen=True)
class GoldMacroSnapshot:
    """The fused gold-macro composite at one point in time."""

    score: float | None     # -10..+10, or None when no driver is usable
    band: str               # 構造的追風 / 中立 / 構造的逆風 / データ待ち
    contributions: tuple[GoldDriverContribution, ...]
    n_drivers: int
    window: int
    as_of: str
    stale: bool
    generated_at: float = field(default_factory=time.time)


# The four-driver registry. Series ids live in config so they are tunable in
# one place; the signs encode the economic direction and never change.
GOLD_DRIVERS: tuple[GoldDriver, ...] = (
    GoldDriver("real_yield", config.MACRO_FRED_REALYIELD_SERIES, -1, "米10年実質金利"),
    GoldDriver("breakeven", config.MACRO_FRED_BREAKEVEN_SERIES, +1, "期待インフレ(10Y)"),
    GoldDriver("vix", config.MACRO_FRED_VIX_SERIES, +1, "リスク(VIX)"),
    GoldDriver("dxy", config.MACRO_FRED_DXY_SERIES, -1, "米ドル(広義)"),
)
```

- [ ] **Step 4: Run it to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_gold_macro.py::test_driver_registry_is_the_four_spec_drivers -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analyzer/gold_macro.py tests/test_gold_macro.py
git commit -m "feat(gold_macro): driver registry + snapshot dataclasses

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `compute_gold_macro_score` — z-score / sign / composite

**Files:**
- Modify: `analyzer/gold_macro.py`
- Test: `tests/test_gold_macro.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gold_macro.py`:

```python
def _flat_then_spike(n: int, base: float, last: float) -> list[float]:
    """n-1 identical values then a different last value → deterministic z."""
    return [base] * (n - 1) + [last]


def test_score_zero_when_all_drivers_flat():
    # Every driver perfectly flat → std 0 → each z is 0 → score 0, band 中立.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="2026-06-01", stale=False)
    assert snap.score == pytest.approx(0.0)
    assert snap.band == "中立"
    assert snap.n_drivers == 4


def test_rising_real_yield_pushes_score_negative():
    # Only the real-yield driver moves, upward → sign -1 → negative contribution.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    hist["real_yield"] = _flat_then_spike(252, 1.0, 5.0)
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    rc = next(c for c in snap.contributions if c.key == "real_yield")
    assert rc.z > 0                 # the level jumped up
    assert rc.signed_z < 0          # sign_gold = -1 flips it bearish for gold
    assert snap.score < 0


def test_rising_vix_pushes_score_positive():
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    hist["vix"] = _flat_then_spike(252, 10.0, 40.0)
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    vc = next(c for c in snap.contributions if c.key == "vix")
    assert vc.signed_z > 0          # sign_gold = +1
    assert snap.score > 0


def test_z_is_clamped_at_configured_bound():
    # A monstrous spike must clamp |z| at GOLD_MACRO_Z_CLAMP.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    hist["vix"] = _flat_then_spike(252, 1.0, 1e9)
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    vc = next(c for c in snap.contributions if c.key == "vix")
    assert abs(vc.signed_z) == pytest.approx(config.GOLD_MACRO_Z_CLAMP)


def test_missing_driver_is_dropped_from_mean():
    # Drop DXY entirely → mean is over the 3 present drivers.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS if d.key != "dxy"}
    hist["vix"] = _flat_then_spike(252, 10.0, 40.0)
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    assert snap.n_drivers == 3
    assert {c.key for c in snap.contributions} == {"real_yield", "breakeven", "vix"}


def test_too_short_history_drops_driver():
    # A driver with fewer than 2 usable points cannot be z-scored → dropped.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    hist["dxy"] = [100.0]            # single point
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    assert snap.n_drivers == 3
    assert all(c.key != "dxy" for c in snap.contributions)


def test_no_usable_driver_yields_none_score():
    snap = gm.compute_gold_macro_score({}, window=252, as_of="", stale=True)
    assert snap.score is None
    assert snap.band == "データ待ち"
    assert snap.n_drivers == 0


def test_band_thresholds():
    # Force a strongly bullish composite: VIX and breakeven max up, others flat.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    hist["vix"] = _flat_then_spike(252, 1.0, 1e9)
    hist["breakeven"] = _flat_then_spike(252, 1.0, 1e9)
    hist["real_yield"] = _flat_then_spike(252, 1.0, -1e9)   # falling = bullish
    hist["dxy"] = _flat_then_spike(252, 1.0, -1e9)          # falling = bullish
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    assert snap.score == pytest.approx(10.0)   # all four max-bullish → +10
    assert snap.band == "構造的追風"


def test_window_uses_only_trailing_obs():
    # An ancient outlier outside the window must not affect the z-score.
    hist = {d.key: [1.0] * 252 for d in gm.GOLD_DRIVERS}
    hist["vix"] = [1e9] + [10.0] * 252          # 253 points; window=252 drops the spike
    snap = gm.compute_gold_macro_score(hist, window=252, as_of="x", stale=False)
    vc = next(c for c in snap.contributions if c.key == "vix")
    assert vc.z == pytest.approx(0.0)           # trailing 252 are all 10.0 → flat
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_gold_macro.py -v`
Expected: FAIL — `AttributeError: module 'analyzer.gold_macro' has no attribute 'compute_gold_macro_score'`

- [ ] **Step 3: Implement `compute_gold_macro_score`**

Append to `analyzer/gold_macro.py`:

```python
def _zscore_last(history: list[float], window: int) -> tuple[float, float] | None:
    """Population z-score of the LAST level over the trailing *window*.

    Returns ``(latest_value, z)`` or ``None`` when the trailing window has
    fewer than 2 points or zero variance (a flat series → z 0 is returned, not
    None, because a flat driver is informative: it is simply neutral)."""
    if not history:
        return None
    window_vals = history[-window:] if window > 0 else list(history)
    if len(window_vals) < 2:
        return None
    n = len(window_vals)
    mean = sum(window_vals) / n
    var = sum((v - mean) ** 2 for v in window_vals) / n      # population variance
    latest = window_vals[-1]
    if var <= 0.0:
        return latest, 0.0
    return latest, (latest - mean) / math.sqrt(var)


def _band_for(score: float | None) -> str:
    """Map a composite score to its interpretation band."""
    if score is None:
        return "データ待ち"
    thr = config.GOLD_MACRO_BAND_THRESHOLD
    if score >= thr:
        return "構造的追風"
    if score <= -thr:
        return "構造的逆風"
    return "中立"


def compute_gold_macro_score(
    histories: dict[str, list[float]],
    *,
    window: int = config.GOLD_MACRO_WINDOW,
    as_of: str,
    stale: bool,
) -> GoldMacroSnapshot:
    """Fuse the driver level histories into a GoldMacroSnapshot.

    Each driver is z-scored over the trailing *window* of its LEVEL, clamped to
    ``±GOLD_MACRO_Z_CLAMP``, sign-adjusted to gold's direction, then the present
    drivers are equal-weighted and the mean rescaled from the clamp range onto
    ``-10..+10``. Drivers with no usable history are dropped from the mean. With
    zero usable drivers the score is ``None``.

    Args:
        histories: ``{driver_key: [level, ...]}`` newest-LAST per driver.
        window: trailing observation count for the z-score.
        as_of: ISO date of the newest observation (for display).
        stale: True when served from cache after a fetch failure.
    """
    clamp = config.GOLD_MACRO_Z_CLAMP
    contribs: list[GoldDriverContribution] = []
    for d in GOLD_DRIVERS:
        res = _zscore_last(histories.get(d.key) or [], window)
        if res is None:
            continue
        value, z = res
        signed = d.sign_gold * max(-clamp, min(clamp, z))
        contribs.append(GoldDriverContribution(
            key=d.key, label_ja=d.label_ja, value=value, z=z,
            signed_z=signed, sign_gold=d.sign_gold,
        ))

    if not contribs:
        return GoldMacroSnapshot(
            score=None, band=_band_for(None), contributions=(),
            n_drivers=0, window=window, as_of=as_of, stale=stale,
        )

    raw = sum(c.signed_z for c in contribs) / len(contribs)
    score = max(-10.0, min(10.0, raw / clamp * 10.0))
    return GoldMacroSnapshot(
        score=score, band=_band_for(score), contributions=tuple(contribs),
        n_drivers=len(contribs), window=window, as_of=as_of, stale=stale,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_gold_macro.py -v`
Expected: all PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add analyzer/gold_macro.py tests/test_gold_macro.py
git commit -m "feat(gold_macro): level-z equal-weight composite + band

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: FRED full-observation list parser

**Files:**
- Modify: `analyzer/macro_feed.py` (add `parse_fred_series` near `parse_fred_json`, ~line 190)
- Test: `tests/test_macro_feed.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_macro_feed.py`:

```python
def test_parse_fred_series_returns_chronological_levels():
    body = _json.dumps({"observations": [
        {"date": "2026-05-29", "value": "2.10"},
        {"date": "2026-05-28", "value": "."},      # missing → skipped
        {"date": "2026-05-27", "value": "2.00"},
    ]})
    as_of, levels = mf.parse_fred_series(body)
    # Sorted oldest→newest, missing dropped, newest date returned as as_of.
    assert as_of == "2026-05-29"
    assert levels == [2.00, 2.10]


def test_parse_fred_series_raises_on_empty():
    body = _json.dumps({"observations": [{"date": "2026-05-29", "value": "."}]})
    with pytest.raises(ValueError):
        mf.parse_fred_series(body)
```

(`_json` is already imported in this file — see the existing real-yield tests.)

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_macro_feed.py::test_parse_fred_series_returns_chronological_levels -v`
Expected: FAIL — `AttributeError: module 'analyzer.macro_feed' has no attribute 'parse_fred_series'`

- [ ] **Step 3: Implement the parser**

In `analyzer/macro_feed.py`, immediately after `parse_fred_json` (after its `return` near line 190), add:

```python
def parse_fred_series(body: str) -> tuple[str, list[float]]:
    """Parse a FRED ``series/observations`` body → (newest ISO date, levels).

    Returns the full chronological (oldest→newest) list of usable level values
    plus the most recent observation date. Missing observations (``"."``) are
    skipped. Raises ``ValueError`` when no usable observation exists. Used by
    the GoldMacroScore path, which needs a long history to z-score (unlike
    :func:`parse_fred_json`, which returns only the latest point)."""
    doc = json.loads(body)
    usable: list[tuple[str, float]] = []
    for row in doc.get("observations") or []:
        date = str(row.get("date") or "")[:10]
        raw = (row.get("value") or "").strip()
        if date and raw and raw != ".":
            usable.append((date, float(raw)))
    if not usable:
        raise ValueError("FRED response had no usable observation")
    usable.sort(key=lambda t: t[0])             # ISO dates sort lexically
    return usable[-1][0], [v for _, v in usable]
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_macro_feed.py -k parse_fred_series -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add analyzer/macro_feed.py tests/test_macro_feed.py
git commit -m "feat(macro_feed): parse_fred_series full-history list parser

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `MacroEngine.fetch_gold_drivers` + cache

**Files:**
- Modify: `analyzer/macro_feed.py` (add cached field in `__init__` ~line 290; add `fetch_gold_drivers` after `fetch_real_yield` ~line 399; extend `_bootstrap_from_cache` ~line 502 and `_persist_cache` ~line 536)
- Test: `tests/test_macro_feed.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_macro_feed.py`:

```python
def _fred_series_body(values: list[float], start="2025-01-01") -> str:
    import datetime
    d0 = datetime.date.fromisoformat(start)
    obs = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
            "value": f"{v}"} for i, v in enumerate(values)]
    # FRED is fetched sort_order=desc; emulate newest-first.
    return _json.dumps({"observations": list(reversed(obs))})


def test_fetch_gold_drivers_returns_all_present(monkeypatch, tmp_path):
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    def fake_fred_get(series_id, limit=6):
        return _fred_series_body([1.0, 2.0, 3.0])
    monkeypatch.setattr(eng, "_fred_get", fake_fred_get)

    histories, as_of = eng.fetch_gold_drivers()
    from analyzer import gold_macro as gm
    assert set(histories) == {d.key for d in gm.GOLD_DRIVERS}
    assert histories["vix"] == [1.0, 2.0, 3.0]
    assert as_of == "2025-01-03"


def test_fetch_gold_drivers_omits_failed_series(monkeypatch, tmp_path):
    import requests
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    def fake_fred_get(series_id, limit=6):
        if series_id == cfg_dxy():
            raise requests.RequestException("boom")
        return _fred_series_body([1.0, 2.0, 3.0])
    monkeypatch.setattr(eng, "_fred_get", fake_fred_get)

    histories, _ = eng.fetch_gold_drivers()
    assert "dxy" not in histories          # failed series dropped, not raised
    assert "vix" in histories


def cfg_dxy():
    import config
    return config.MACRO_FRED_DXY_SERIES
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_macro_feed.py -k fetch_gold_drivers -v`
Expected: FAIL — `AttributeError: 'MacroEngine' object has no attribute 'fetch_gold_drivers'`

- [ ] **Step 3a: Add the cached field**

In `analyzer/macro_feed.py` `__init__`, after the line `self._cached_real_yield: RealYieldSnapshot | None = None` (line 290), add:

```python
        self._cached_gold_drivers: dict[str, list[float]] = {}
        self._cached_gold_as_of: str = ""
```

- [ ] **Step 3b: Add `fetch_gold_drivers`**

Immediately after `fetch_real_yield` (after its `return snap` near line 399), add:

```python
    def fetch_gold_drivers(self) -> tuple[dict[str, list[float]], str]:
        """Fetch the GoldMacroScore driver LEVEL histories from FRED.

        For each driver in ``gold_macro.GOLD_DRIVERS`` request a long window
        (``GOLD_MACRO_WINDOW`` + buffer) and parse the full chronological level
        list. A per-series failure omits that driver (logged, redacted) instead
        of aborting — the composite averages over the present drivers, mirroring
        the macro layer's "never penalise on bad/absent data" rule. The newest
        observation date across drivers is returned as ``as_of``. Successful
        results refresh the on-disk cache; a total failure falls back to it
        (callers see ``stale`` via the snapshot)."""
        from analyzer import gold_macro      # local import avoids a cycle

        histories: dict[str, list[float]] = {}
        as_of = ""
        limit = config.GOLD_MACRO_WINDOW + 30
        for d in gold_macro.GOLD_DRIVERS:
            try:
                date, levels = parse_fred_series(self._fred_get(d.series_id, limit=limit))
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("macro: gold driver %s fetch failed - %s",
                            d.series_id, _redact(exc))
                continue
            histories[d.key] = levels
            as_of = max(as_of, date)
        if histories:
            self._cached_gold_drivers = histories
            self._cached_gold_as_of = as_of
            self._persist_cache()
        return histories, as_of
```

- [ ] **Step 3c: Extend `_bootstrap_from_cache`**

In `_bootstrap_from_cache`, after the `real_yield` block (after the `RealYieldSnapshot(...)` assignment, before the `self._last_fetch_ok = ...` line near line 509), add:

```python
            gd = doc.get("gold_drivers")
            if isinstance(gd, dict):
                self._cached_gold_drivers = {
                    k: [float(v) for v in (vals or [])]
                    for k, vals in (gd.get("histories") or {}).items()
                }
                self._cached_gold_as_of = str(gd.get("as_of") or "")
```

- [ ] **Step 3d: Extend `_persist_cache`**

In `_persist_cache`, add a `"gold_drivers"` key to the `payload` dict (after the `"real_yield"` entry, before the closing `}` of `payload` near line 541):

```python
                "gold_drivers": {
                    "histories": self._cached_gold_drivers,
                    "as_of": self._cached_gold_as_of,
                },
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_macro_feed.py -v`
Expected: all PASS (existing + 2 new fetch tests + parser tests)

- [ ] **Step 5: Commit**

```bash
git add analyzer/macro_feed.py tests/test_macro_feed.py
git commit -m "feat(macro_feed): fetch_gold_drivers with per-series isolation + cache

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: State plumbing for `gold_macro`

**Files:**
- Modify: `analyzer/state.py` (import line 24; field ~line 94; setter after `set_real_yield` ~line 236; property after `real_yield` ~line 318; snapshot dict ~line 357)
- Test: `tests/test_state_and_serialize.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_state_set_and_read_gold_macro():
    from analyzer.state import LatestState
    from analyzer.gold_macro import GoldMacroSnapshot
    st = LatestState()
    assert st.gold_macro is None
    snap = GoldMacroSnapshot(score=2.5, band="中立", contributions=(),
                             n_drivers=4, window=252, as_of="2026-06-01",
                             stale=False, generated_at=1.0)
    st.set_gold_macro(snap)
    assert st.gold_macro is snap
    # It must ride the FULL snapshot, never the light one.
    assert st.snapshot()["gold_macro"] is snap
    assert "gold_macro" not in st.light_snapshot()
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_state_and_serialize.py::test_state_set_and_read_gold_macro -v`
Expected: FAIL — `AttributeError: 'LatestState' object has no attribute 'gold_macro'`

- [ ] **Step 3a: Import the type**

In `analyzer/state.py` line 24, extend the macro import:

```python
from analyzer.macro_feed import MacroSnapshot, RealYieldSnapshot
from analyzer.gold_macro import GoldMacroSnapshot
```

- [ ] **Step 3b: Add the field**

After `self._real_yield: Optional[RealYieldSnapshot] = None` (line 94), add:

```python
        self._gold_macro: Optional[GoldMacroSnapshot] = None
```

- [ ] **Step 3c: Add the setter**

After the `set_real_yield` method (after its `self._cond.notify_all()` near line 236), add:

```python
    def set_gold_macro(self, snapshot: GoldMacroSnapshot) -> None:
        with self._cond:
            self._gold_macro = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()
```

- [ ] **Step 3d: Add the property**

After the `real_yield` property (after its `return self._real_yield` near line 318), add:

```python
    @property
    def gold_macro(self) -> GoldMacroSnapshot | None:
        with self._lock:
            return self._gold_macro
```

- [ ] **Step 3e: Add to the full snapshot**

In `snapshot()`, after the line `"real_yield": self._real_yield,` (line 357), add:

```python
                "gold_macro": self._gold_macro,
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_state_and_serialize.py::test_state_set_and_read_gold_macro -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analyzer/state.py tests/test_state_and_serialize.py
git commit -m "feat(state): carry gold_macro on the full snapshot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Serialize `gold_macro` into the WS payload

**Files:**
- Modify: `dashboard/serialize.py` (import ~line 76; add `serialize_gold_macro` after `serialize_real_yield` ~line 690; wire into `snapshot_to_json` after the `real_yield` line ~line 753)
- Test: `tests/test_state_and_serialize.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_serialize_gold_macro_shape():
    from dashboard.serialize import serialize_gold_macro
    from analyzer.gold_macro import GoldMacroSnapshot, GoldDriverContribution
    snap = GoldMacroSnapshot(
        score=2.5, band="中立",
        contributions=(GoldDriverContribution(
            key="vix", label_ja="リスク(VIX)", value=18.0, z=0.5,
            signed_z=0.5, sign_gold=1),),
        n_drivers=4, window=252, as_of="2026-06-01", stale=False,
        generated_at=1.0)
    out = serialize_gold_macro(snap)
    assert out["score"] == 2.5
    assert out["band"] == "中立"
    assert out["n_drivers"] == 4
    assert out["contributions"][0]["key"] == "vix"
    assert out["contributions"][0]["sign"] == 1
    assert serialize_gold_macro(None) is None


def test_serialize_gold_macro_none_score():
    from dashboard.serialize import serialize_gold_macro
    from analyzer.gold_macro import GoldMacroSnapshot
    snap = GoldMacroSnapshot(score=None, band="データ待ち", contributions=(),
                             n_drivers=0, window=252, as_of="", stale=True,
                             generated_at=1.0)
    out = serialize_gold_macro(snap)
    assert out["score"] is None
    assert out["band"] == "データ待ち"
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_state_and_serialize.py -k serialize_gold_macro -v`
Expected: FAIL — `ImportError: cannot import name 'serialize_gold_macro'`

- [ ] **Step 3a: Import the type**

In `dashboard/serialize.py`, find the `RealYieldSnapshot,` import line (line 76) and add after it:

```python
    GoldMacroSnapshot,
```

(Confirm whether `RealYieldSnapshot` is imported from `analyzer.macro_feed` or `analyzer.state`. `GoldMacroSnapshot` lives in `analyzer.gold_macro`; add `from analyzer.gold_macro import GoldMacroSnapshot` as its own import line near the other analyzer imports if the existing import block is module-specific.)

- [ ] **Step 3b: Add the serializer**

After `serialize_real_yield` (after its closing `}` near line 690), add:

```python
def serialize_gold_macro(s: GoldMacroSnapshot | None) -> dict[str, Any] | None:
    """Serialise the GoldMacroScore snapshot for the WebSocket payload.

    Rides the FULL snapshot only (daily-moving; must never load the ~2 Hz light
    path). ``score`` is None when no driver was usable; contributions list every
    present driver so the UI can show per-driver bars and flag reduced coverage.
    """
    if s is None:
        return None
    return {
        "score": _opt_float(s.score),
        "band": s.band,
        "n_drivers": int(s.n_drivers),
        "window": int(s.window),
        "as_of": s.as_of,
        "stale": bool(s.stale),
        "generated_at": float(s.generated_at),
        "contributions": [
            {
                "key": c.key,
                "label": c.label_ja,
                "value": _opt_float(c.value),
                "z": _opt_float(c.z),
                "signed_z": _opt_float(c.signed_z),
                "sign": int(c.sign_gold),
            }
            for c in s.contributions
        ],
    }
```

- [ ] **Step 3c: Wire into `snapshot_to_json`**

In `snapshot_to_json`, after the line `"real_yield": serialize_real_yield(snap["real_yield"]),  # type: ignore[arg-type]` (line 753), add:

```python
        "gold_macro": serialize_gold_macro(snap.get("gold_macro")),  # type: ignore[arg-type]
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_state_and_serialize.py -k serialize_gold_macro -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add dashboard/serialize.py tests/test_state_and_serialize.py
git commit -m "feat(serialize): gold_macro on the full WS snapshot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Wire the refresh into the analysis loop

**Files:**
- Modify: `analyzer/analysis_loop.py` (constructor arg ~line 98; in-flight guard ~line 126; `_Schedule` tuple ~line 154; dispatcher map ~line 213; handler + worker after `_realyield_refresh_worker` ~line 472)
- Test: `tests/test_analysis_loop.py` if present, else a focused smoke test

- [ ] **Step 1: Add the constructor parameter**

After `realyield_interval: float = config.MACRO_REALYIELD_REFRESH_SEC,` (line 98), add:

```python
        goldmacro_interval: float = config.GOLD_MACRO_REFRESH_SEC,
```

- [ ] **Step 2: Add the in-flight guard**

After `self._realyield_inflight = threading.Event()` (line 126), add:

```python
        # GoldMacroScore drivers are daily-moving (same cadence as the real
        # yield); same MacroEngine, separate in-flight guard + schedule.
        self._goldmacro_inflight = threading.Event()
```

- [ ] **Step 3: Add the schedule**

In the `self._schedules = (...)` tuple, after `_Schedule("realyield", realyield_interval),` (line 154), add:

```python
            _Schedule("goldmacro", goldmacro_interval),
```

- [ ] **Step 4: Add the dispatcher entry**

In `_dispatch`'s handler map, after `"realyield": self._do_realyield_refresh,` (line 213), add:

```python
            "goldmacro": self._do_goldmacro_refresh,
```

- [ ] **Step 5: Add the handler + worker**

After `_realyield_refresh_worker` (after its `finally: self._realyield_inflight.clear()` near line 472), add:

```python
    def _do_goldmacro_refresh(self, bases: list[str]) -> None:
        """Refresh the GoldMacroScore hourly (its FRED drivers move daily)."""
        if self._goldmacro_inflight.is_set():
            log.debug("goldmacro: previous fetch still in flight, skipping tick")
            return
        self._goldmacro_inflight.set()
        worker = threading.Thread(
            target=self._goldmacro_refresh_worker,
            name="goldmacro-fetch", daemon=True,
        )
        worker.start()

    def _goldmacro_refresh_worker(self) -> None:
        try:
            histories, as_of = self._macro_engine.fetch_gold_drivers()
            snap = gold_macro.compute_gold_macro_score(
                histories, as_of=as_of, stale=not histories,
            )
            self._state.set_gold_macro(snap)
            if not histories:       # total fetch failure → retry soon
                self._reschedule_soon("goldmacro", config.MACRO_RETRY_SEC)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("goldmacro worker failed")
            self._reschedule_soon("goldmacro", config.MACRO_RETRY_SEC)
        finally:
            self._goldmacro_inflight.clear()
```

- [ ] **Step 6: Add the import**

At the top of `analyzer/analysis_loop.py`, with the other `from analyzer import ...` lines, ensure `gold_macro` is imported:

```python
from analyzer import gold_macro
```

(If the file imports analyzer modules individually, add this line alongside them; if it uses `from analyzer.gold_macro import ...`, import `compute_gold_macro_score` and call it unqualified — match the file's existing style.)

- [ ] **Step 7: Verify the loop constructs and dispatches**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -c "import analyzer.analysis_loop as al; print('goldmacro' in [s.name for s in al.AnalysisLoop.__init__.__doc__ and []] or 'ok')"`

Then the real check — full import + unit suite:

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q`
Expected: all PASS (no import errors; the new schedule wired)

- [ ] **Step 8: Commit**

```bash
git add analyzer/analysis_loop.py
git commit -m "feat(loop): hourly goldmacro refresh worker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: P1 integration verification (live server)

**Files:** none (verification only)

- [ ] **Step 1: Full unit suite green**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q`
Expected: all PASS (329 + new gold_macro/macro/serialize tests).

- [ ] **Step 2: Restart the server (backend change → full restart required)**

```powershell
$conn = Get-NetTCPConnection -LocalPort 8050 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Stop-Process -Id $conn.OwningProcess -Force }
```

Then relaunch in background:

```bash
cd C:/Users/ohuch/Desktop/MT5_Python && nohup "C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" main.py > /tmp/mt5_server.log 2>&1 &
```

- [ ] **Step 3: Confirm `gold_macro` arrives on the full WS snapshot**

After the loop has run one goldmacro tick (FRED fetch; may take up to a minute), connect to `ws://127.0.0.1:8050/ws` and assert the full snapshot has a `gold_macro` block with `score` (float or null), `band`, and a `contributions` list whose driver keys ⊆ {real_yield, breakeven, vix, dxy}.

Expected: `gold_macro` present; if `FRED_API_KEY` is set, `score` is a float in [-10, 10] and `n_drivers` ≥ 1; if FRED is unreachable, `score` is null and `stale` true (graceful degrade, no crash).

- [ ] **Step 4: Confirm the light snapshot does NOT carry it**

Assert a light (~2 Hz) message has no `gold_macro` key (invariant: heavy data never on the light path).

- [ ] **Step 5: Console / log check**

`/tmp/mt5_server.log` shows no traceback from `goldmacro`; if FRED failed, exactly the `macro: gold driver ... fetch failed` WARNING (redacted, no API key).

---

# PHASE 2 — Validation (decides P3)

## Task 10: `scripts/_validate_gold_macro.py` — IC + OOS gate

**Files:**
- Create: `scripts/_validate_gold_macro.py`

This is an offline analysis script (ASCII-only output, full Python path). It is NOT TDD'd like the pipeline — it is an experiment harness — but it MUST be deterministic (fixed bootstrap seed) and free of look-ahead.

- [ ] **Step 1: Reconstruct the historical daily score series (no look-ahead)**

Load the full FRED history for each driver (one `_fred_get` with a large `limit`, or the FRED CSV). For each trading day `t` from the first day with `GOLD_MACRO_WINDOW` prior obs, call `gold_macro.compute_gold_macro_score` using ONLY levels up to and including `t` (slice each history to `[: t+1]`). This guarantees each day's z-score uses only past data. Build a date→score series.

- [ ] **Step 2: Align with forward XAUUSD returns**

Load XAUUSD daily closes from the same Dukascopy dataset `scripts/_oos_xauusd_16y.py` uses (reuse its loader). For horizons H ∈ {5, 20} trading days compute `fwd_ret[t] = close[t+H]/close[t] - 1`. Align on dates present in both the score series and the price series.

- [ ] **Step 3: Information Coefficient + bootstrap CI**

Compute Spearman rank correlation IC between `score[t]` and `fwd_ret[t]` per horizon. Bootstrap a CI on IC with a moving-block bootstrap (reuse the project's `BOOTSTRAP_*` constants / approach from `_oos_xauusd_16y.py`; fixed seed). Record whether the CI lower bound > 0 for each horizon.

- [ ] **Step 4: OOS gate (conditioned DWS backtest)**

Using the existing 16Y backtest path, condition DWS-SMT XAUUSD triggers on the score regime: keep BUY trades only when `score >= +thr`, SELL only when `score <= -thr`, sweeping `thr` over a small grid (e.g. {1, 2, 3}). Pick `thr` on the in-sample (early) half only, then report conditioned vs unconditioned PF / expectancy on the out-of-sample (late) half — respecting `PERIOD_SPLIT_YEAR` so the threshold is not tuned on the test data.

- [ ] **Step 5: Print an ASCII verdict**

```
print("=== GoldMacroScore validation ===")
print(f"IC  5d : {ic5:+.4f}  CI[{ic5_lo:+.4f},{ic5_hi:+.4f}]  {'PASS' if ic5_lo>0 else 'fail'}")
print(f"IC 20d : {ic20:+.4f} CI[{ic20_lo:+.4f},{ic20_hi:+.4f}] {'PASS' if ic20_lo>0 else 'fail'}")
print(f"OOS PF : baseline {pf_base:.3f} -> conditioned {pf_cond:.3f} (thr={thr})")
verdict = "ADOPT" if ((ic5_lo>0 or ic20_lo>0) and pf_cond > pf_base) else "REJECT"
print(f"VERDICT: {verdict}")
```

(ASCII only — no `→`/`±`/em-dash in printed strings; use `->`.)

- [ ] **Step 6: Run it**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" scripts/_validate_gold_macro.py`
Expected: prints IC per horizon with CIs, OOS PF comparison, and `VERDICT: ADOPT|REJECT`. No traceback; ASCII clean on cp932.

- [ ] **Step 7: Commit the script + record the verdict**

```bash
git add scripts/_validate_gold_macro.py
git commit -m "feat(validate): GoldMacroScore IC + OOS-gate validation harness

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Paste the printed verdict into the PR / session notes. **The verdict decides P3.**

---

# PHASE 3 — Conditional on P2 = ADOPT

> Build P3 ONLY if Task 10 prints `VERDICT: ADOPT`. If `REJECT`, stop here:
> the score remains a backend value (or is removed) and is NOT shown as a
> tradeable signal. Record the decision in SESSION_HANDOFF.

## Task 11 (conditional): Regime-gauge UI

**Files:**
- Modify: `static/app.js` (a `buildGoldMacroGauge(snap)` renderer; mount near the XAUUSD panel / macro area, reusing the BIAS `-10..+10` styling), `static/app.css` (`.gm-*` classes, no collisions), `templates/index.html` if a mount point is needed.

High-level (detailed steps to be written when P3 is greenlit, following the existing `real_yield` rendering at `app.js:1002` and `app.js:2117` as the pattern):
- Read `snap.gold_macro`; if `score == null` show "データ待ち".
- Render the composite as a `-10..+10` gauge (reuse BIAS gauge styling), the band label (構造的追風/中立/構造的逆風), and a mini-bar per `contributions[]` entry (`signed_z`, coloured by sign).
- Flag reduced coverage when `n_drivers < 4`.
- Escape all dynamic strings via `esc()`. No hardcoded driver list — drive off `contributions`.

## Task 12 (conditional): Promote to the DWS degradation gate

**Files:** `static/app.js` (extend `_regimeState` / `_regimeGated` consumers or add a parallel gold-macro filter), or backend gating in `signal_validator`.

High-level: use the validated threshold from Task 10 to demote/mute XAUUSD setups whose direction fights a strong GoldMacroScore headwind/tailwind, reusing the existing degradation-gate machinery (amber dashed, alert suppression). Lot/SL unchanged. Add tests mirroring the existing regime-gate tests.

---

## Self-Review (completed)

**Spec coverage:**
- §2 drivers → Task 2 (registry) ✓
- §3 math (level-z, equal-weight, ±2.5 clamp, 252 window, band, missing-data) → Task 3 + tests ✓
- §4.1 gold_macro.py → Tasks 2-3 ✓; §4.2 fetch_gold_drivers + list parser → Tasks 4-5 ✓; §4.3 state → Task 6 ✓; §4.4 loop → Task 8 ✓; §4.5 serialize (full only) → Task 7 ✓; §4.6 config → Task 1 ✓
- §5 validation (IC + OOS gate, no look-ahead, ASCII) → Task 10 ✓
- §6 phasing (P1/P2/P3) → Tasks 1-9 / 10 / 11-12 ✓
- §7 invariants (full-only snapshot, no look-ahead, FRED env-only/redact, no bare except) → enforced in Tasks 5,7,8,10 ✓
- §8 out-of-scope (COT/GLD/fitted weights/PCA/momentum-z) → not in any task ✓
- §9 test plan → Tasks 2,3,5,6,7 tests ✓

**Placeholder scan:** P3 (Tasks 11-12) intentionally high-level because it is gated on the P2 verdict; all P1/P2 tasks have complete code. No TBD in executable tasks.

**Type consistency:** `GoldMacroSnapshot` / `GoldDriverContribution` / `compute_gold_macro_score(histories, *, window, as_of, stale)` / `fetch_gold_drivers() -> (histories, as_of)` / `set_gold_macro` / `serialize_gold_macro` used consistently across Tasks 2-8. Driver keys `{real_yield, breakeven, vix, dxy}` consistent throughout.
