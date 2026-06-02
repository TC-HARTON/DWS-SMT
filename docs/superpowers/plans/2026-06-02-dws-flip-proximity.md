# DWS Flip-Proximity Gradient Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface each DWS stack row's discarded smoothed-diff magnitude as a per-row "flip-proximity" gradient (how close each row is to flipping = how close to a trigger), with a holdout emphasis on the current bar, forming-bar-inclusive for live preview but isolated from confirmed trigger detection.

**Architecture:** `dws_smt.compute_symbol` already computes each row's smoothed diff then throws away everything but the sign. We keep the confirmed trigger/colour path byte-for-byte, and add a SECOND forming-inclusive smoothed-diff pass normalised to a signed `flip_norm` array on `DwsSmtWindow`. It serialises as `fn` and `drawDwsCanvas` renders the 3 row cells as a gradient (hue=sign, alpha=magnitude) plus a current-bar holdout ring.

**Tech Stack:** Python 3.14 (full path `C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe`), numpy, pandas, scipy.signal; vanilla JS canvas; pytest. cp932 console → ASCII-only script stdout.

**Reference spec:** `docs/superpowers/specs/2026-06-02-dws-flip-proximity-design.md`

**Conventions (SESSION_HANDOFF):**
- Tests: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q`
- JS: `node --check static/app.js`
- Backend changes need a FULL server restart (kill PID on 8050, relaunch); frontend is no-cache (browser reload).
- Commit only the files named per task. Trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- No bare except; type hints + docstrings.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `config.py` | `DWS_FLIP_STD_WINDOW`, `DWS_FLIP_K` (backend normalisation) | Modify |
| `analyzer/dws_smt.py` | `_flip_norm` helper; `flip_norm` field on `DwsSmtWindow`; forming-inclusive pass in `_build_window` / `compute_symbol` | Modify |
| `dashboard/serialize.py` | ship `fn` array in the DWS window block | Modify |
| `static/app.js` | gradient cell fill + holdout emphasis in `drawDwsCanvas`; `DWS_FLIP_IMMINENT` const | Modify |
| `tests/test_dws_smt.py` | `_flip_norm` unit tests + window/forming-inclusive tests | Modify |

Note: `analyzer/indicator_engine.py` is UNCHANGED — `_compute_dws` already passes full (forming-inclusive) frames to `compute_symbol`, which derives both the forming-excluded trigger diffs and the forming-inclusive proximity diffs internally.

The imminent-emphasis threshold is a pure render decision and lives in `app.js` (`DWS_FLIP_IMMINENT`), not `config.py`.

---

## Task 1: Config constants

**Files:**
- Modify: `config.py` (after the real-yield block, ~line 351, before the GoldMacroScore block)

- [ ] **Step 1: Add the constants**

Insert after `MACRO_REALYIELD_REFRESH_SEC` line:

```python

# DWS histogram flip-proximity gradient (spec
# docs/superpowers/specs/2026-06-02-dws-flip-proximity-design.md). Each stack
# row's smoothed-diff magnitude is normalised by its own trailing volatility so
# "near the zero-cross" (= near a colour flip = near a trigger) is comparable
# across symbols. Display-only; never used by trigger/trade/order logic.
DWS_FLIP_STD_WINDOW: Final[int] = 96    # trailing bars for the smoothed-diff std
DWS_FLIP_K: Final[float] = 1.0          # scale: |sd| = k*std maps to |flip_norm| = 1
```

- [ ] **Step 2: Verify import**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -c "import config; print(config.DWS_FLIP_STD_WINDOW, config.DWS_FLIP_K)"`
Expected: `96 1.0`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): DWS flip-proximity normalisation constants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `_flip_norm` helper (pure, normalisation math)

**Files:**
- Modify: `analyzer/dws_smt.py` (add `_flip_norm` after `_colorize`, ~line 151)
- Test: `tests/test_dws_smt.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dws_smt.py`:

```python
# ----------------------------------------------------- _flip_norm

def test_flip_norm_shape_and_clamp():
    sd = np.array([1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 5.0])
    out = dws_smt._flip_norm(sd, window=4, k=1.0)
    assert out.shape == sd.shape
    assert np.all(np.abs(out) <= 1.0)          # clamped to [-1, 1]
    assert out[-1] > 0.0                        # last value is positive


def test_flip_norm_zero_when_flat():
    # A flat window has zero std → undefined scale → flip_norm 0 (as production
    # _colorize treats a flat smoothed series as neutral).
    sd = np.array([3.0, 3.0, 3.0, 3.0, 3.0])
    out = dws_smt._flip_norm(sd, window=3, k=1.0)
    np.testing.assert_array_equal(out, np.zeros_like(sd))


def test_flip_norm_small_near_zero_cross():
    # A value tiny relative to its recent volatility is "near the flip" → |.|~0.
    sd = np.array([10.0, -10.0, 10.0, -10.0, 0.05])
    out = dws_smt._flip_norm(sd, window=4, k=1.0)
    assert abs(out[-1]) < 0.05


def test_flip_norm_empty_and_single():
    np.testing.assert_array_equal(dws_smt._flip_norm(np.array([]), 4, 1.0),
                                  np.array([]))
    # single point: no std defined → 0, never NaN.
    out = dws_smt._flip_norm(np.array([7.0]), 4, 1.0)
    assert out.tolist() == [0.0]
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_dws_smt.py -k flip_norm -v`
Expected: FAIL — `AttributeError: module 'analyzer.dws_smt' has no attribute '_flip_norm'`

- [ ] **Step 3: Implement `_flip_norm`**

In `analyzer/dws_smt.py`, immediately after `_colorize` (after its `return out`, ~line 151), add:

```python
def _flip_norm(smoothed: np.ndarray, window: int, k: float) -> np.ndarray:
    """Signed, self-normalised distance of the smoothed diff from its zero-cross.

    ``flip_norm = clamp(smoothed / (k * rolling_std(smoothed, window)), -1, +1)``
    where ``rolling_std`` is the trailing population std of the row's own
    smoothed-diff series. The SIGN is the row's current direction; the MAGNITUDE
    is how firmly aligned it is (``~0`` = at the zero-cross = a colour flip / a
    trigger is imminent; ``~1`` = firmly in its colour). Returns 0 wherever the
    scale is undefined (warmup with <2 points, or a flat zero-variance window),
    never NaN/inf — mirroring ``_colorize`` treating a flat series as neutral."""
    out = np.zeros(smoothed.size, dtype=np.float64)
    if smoothed.size == 0:
        return out
    std = (pd.Series(smoothed).rolling(window, min_periods=2)
           .std(ddof=0).to_numpy())
    denom = k * std
    ok = np.isfinite(denom) & (denom > 0.0)
    out[ok] = np.clip(smoothed[ok] / denom[ok], -1.0, 1.0)
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_dws_smt.py -k flip_norm -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add analyzer/dws_smt.py tests/test_dws_smt.py
git commit -m "feat(dws): _flip_norm — self-normalised distance-to-flip helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Wire `flip_norm` through the window (forming-inclusive, isolated from triggers)

**Files:**
- Modify: `analyzer/dws_smt.py` (`DwsSmtWindow` dataclass ~line 80; `_build_window` signature + body ~line 273; `compute_symbol` ~line 354)
- Test: `tests/test_dws_smt.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dws_smt.py`:

```python
def test_window_exposes_flip_norm_matching_colors_shape():
    df = _df(periods=80, step=1.0)
    frames = {tf: df for tf in ALL_TFS}
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    win = res.by_base["M15"]
    assert win.flip_norm.shape == win.colors.shape       # (n_out, n_rows)
    assert np.all(np.abs(win.flip_norm) <= 1.0)


def test_flip_norm_forming_inclusive_while_triggers_are_not():
    """The proximity path sees the forming bar (live preview) even though
    trigger detection does not. Two variants differing ONLY in the forming H4
    close must yield identical triggers but a DIFFERENT current-bar flip_norm."""
    n_m15 = 100
    m15_idx = pd.date_range("2026-01-01 00:00", periods=n_m15, freq="15min", tz="UTC")
    m15c = 100.0 + 5.0 * np.sin(np.arange(n_m15) / 8.0)
    m15_df = pd.DataFrame({"open": m15c, "high": m15c + 0.05,
                           "low": m15c - 0.05, "close": m15c}, index=m15_idx)
    h4_idx = pd.date_range("2026-01-01 00:00", periods=7, freq="4h", tz="UTC")
    h4c = 100.0 + 5.0 * np.sin(np.arange(7) / 2.0)
    h1_idx = pd.date_range("2026-01-01 00:00", periods=25, freq="1h", tz="UTC")
    h1c = 100.0 + 5.0 * np.sin(np.arange(25) / 3.0)

    def _ohlc(idx, c):
        return pd.DataFrame({"open": c, "high": c + 0.05,
                             "low": c - 0.05, "close": c}, index=idx)

    fa = {"M15": m15_df, "H1": _ohlc(h1_idx, h1c), "H4": _ohlc(h4_idx, h4c)}
    h4b = h4c.copy(); h4b[-1] += 50.0          # perturb ONLY the forming H4 close
    fb = {"M15": m15_df, "H1": _ohlc(h1_idx, h1c), "H4": _ohlc(h4_idx, h4b)}

    wa = compute_symbol(fa).by_base["M15"]
    wb = compute_symbol(fb).by_base["M15"]
    # Triggers on confirmed bars must be identical (look-ahead-safe).
    assert wa.triggers[:-1] == wb.triggers[:-1]
    # The H4 row (index 0) flip_norm on the CURRENT bar must move with the
    # live forming price (this is the "もう少しで" preview).
    assert wa.flip_norm[-1][0] != wb.flip_norm[-1][0]
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_dws_smt.py -k "flip_norm_matching or forming_inclusive" -v`
Expected: FAIL — `AttributeError: 'DwsSmtWindow' object has no attribute 'flip_norm'`

- [ ] **Step 3a: Add the dataclass field**

In `analyzer/dws_smt.py`, in `DwsSmtWindow` (after the `bias` field, ~line 87):

```python
    bias: np.ndarray                   # composite BIAS score (-10..+10) per bar
    flip_norm: np.ndarray              # signed (n_bars, n_rows) distance-to-flip
```

- [ ] **Step 3b: Extend `_build_window` to compute flip_norm**

Replace the current `_build_window` signature and the colour loop. The current signature (~line 273) is:

```python
def _build_window(
    base: str,
    base_df: pd.DataFrame,
    rows: tuple[str, ...],
    row_diffs: dict[str, tuple[np.ndarray, np.ndarray]],
    smooth: int,
    out_bars: int,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None,
) -> DwsSmtWindow | None:
```

Change it to add `row_diffs_live`, `flip_window`, `flip_k`:

```python
def _build_window(
    base: str,
    base_df: pd.DataFrame,
    rows: tuple[str, ...],
    row_diffs: dict[str, tuple[np.ndarray, np.ndarray]],
    row_diffs_live: dict[str, tuple[np.ndarray, np.ndarray]],
    smooth: int,
    out_bars: int,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None,
    flip_window: int,
    flip_k: float,
) -> DwsSmtWindow | None:
```

Then, inside the body, the current colour loop is:

```python
    salpha = 2.0 / (smooth + 1.0)
    # Colour each row over the *full* base history so the zero-seeded smoothing
    # warm-up has fully decayed before the trailing window that gets emitted.
    colors_by_row: list[np.ndarray] = []
    for label in rows:
        rd = row_diffs.get(label)
        if rd is None:
            mapped = np.zeros(n, dtype=np.float64)
        else:
            sub_ns, sub_diff = rd
            mapped = _map_onto(base_ns, sub_ns, sub_diff)
        colors_by_row.append(_colorize(_ema(mapped, salpha, seed=0.0)))

    triggers = _detect_triggers(colors_by_row)
```

Replace it with (adds a parallel forming-inclusive flip_norm pass; the colour /
trigger path is byte-for-byte the same):

```python
    salpha = 2.0 / (smooth + 1.0)
    # Colour each row over the *full* base history so the zero-seeded smoothing
    # warm-up has fully decayed before the trailing window that gets emitted.
    # row_diffs is forming-EXCLUDED (look-ahead-safe) — it drives colours and
    # triggers. row_diffs_live is forming-INCLUDED and drives ONLY the
    # display-only flip-proximity (the live "almost a trigger" preview); it is
    # never consulted by _detect_triggers / _pair_trades.
    colors_by_row: list[np.ndarray] = []
    flip_by_row: list[np.ndarray] = []
    for label in rows:
        rd = row_diffs.get(label)
        mapped = (np.zeros(n, dtype=np.float64) if rd is None
                  else _map_onto(base_ns, rd[0], rd[1]))
        colors_by_row.append(_colorize(_ema(mapped, salpha, seed=0.0)))

        rdl = row_diffs_live.get(label)
        mapped_live = (np.zeros(n, dtype=np.float64) if rdl is None
                       else _map_onto(base_ns, rdl[0], rdl[1]))
        flip_by_row.append(_flip_norm(_ema(mapped_live, salpha, seed=0.0),
                                      flip_window, flip_k))

    triggers = _detect_triggers(colors_by_row)
```

Then, in the emit section, add the `flip_norm` slice and pass it to the
constructor. The current emit block ends with:

```python
    start = max(0, n - out_bars)
    colors = np.stack([row[start:] for row in colors_by_row], axis=1)   # (n_out, n_rows)
    times_ms = base_ns[start:] // 1_000_000                             # ns → ms
    out_triggers = tuple(triggers[start:])
    out_closes = base_df["close"].to_numpy(dtype=np.float64)[start:]
    out_highs = base_df["high"].to_numpy(dtype=np.float64)[start:]
    out_lows = base_df["low"].to_numpy(dtype=np.float64)[start:]
    return DwsSmtWindow(
        base_tf=base,
        rows=tuple(rows),
        times_ms=times_ms,
        colors=colors,
        triggers=out_triggers,
        trades=_pair_trades(out_triggers, out_closes, out_highs, out_lows),
        bias=bias[start:],
    )
```

Change it to:

```python
    start = max(0, n - out_bars)
    colors = np.stack([row[start:] for row in colors_by_row], axis=1)   # (n_out, n_rows)
    flip = np.stack([row[start:] for row in flip_by_row], axis=1)       # (n_out, n_rows)
    times_ms = base_ns[start:] // 1_000_000                             # ns → ms
    out_triggers = tuple(triggers[start:])
    out_closes = base_df["close"].to_numpy(dtype=np.float64)[start:]
    out_highs = base_df["high"].to_numpy(dtype=np.float64)[start:]
    out_lows = base_df["low"].to_numpy(dtype=np.float64)[start:]
    return DwsSmtWindow(
        base_tf=base,
        rows=tuple(rows),
        times_ms=times_ms,
        colors=colors,
        triggers=out_triggers,
        trades=_pair_trades(out_triggers, out_closes, out_highs, out_lows),
        bias=bias[start:],
        flip_norm=flip,
    )
```

- [ ] **Step 3c: Build `row_diffs_live` in `compute_symbol` and pass the new args**

The current `compute_symbol` body (~line 354) is:

```python
    # A row TF's diff is identical wherever it appears, so compute the diff for
    # each distinct row TF across all stacks exactly once and reuse it.
    #
    # NO LOOK-AHEAD: the last bar of each row frame is the in-progress (forming)
    # candle whose close is still drifting tick-by-tick. Feeding it into
    # ``_diff_series`` lets the forming sub-TF colour propagate through
    # ``_map_onto`` onto every confirmed base bar whose timestamp falls inside
    # the still-open sub-TF candle — a textbook look-ahead leak that makes
    # confirmed-bar triggers flicker as the forming bar moves. ``iloc[:-1]``
    # drops it so confirmed base bars only see closed sub-TF diffs. The base
    # frame itself is NOT trimmed below: its forming bar is excluded from
    # trigger detection by ``_detect_triggers`` (``state[1:n-1]``) but its
    # latest close is still needed for the open trade's mark-to-market.
    needed = {tf for stack in stacks.values() for tf in stack}
    row_diffs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for label in needed:
        df = frames.get(label)
        if df is not None and not df.empty and len(df) > 2:
            row_diffs[label] = _diff_series(df.iloc[:-1], period)

    by_base: dict[str, DwsSmtWindow] = {}
    for base, rows in stacks.items():
        base_df = frames.get(base)
        if base_df is None or base_df.empty or len(base_df) < 2:
            continue
        window = _build_window(base, base_df, rows, row_diffs, smooth,
                               out_bars, bias_contrib)
        if window is not None:
            by_base[base] = window
```

Replace it with (adds `row_diffs_live` forming-inclusive + threads the new args):

```python
    # A row TF's diff is identical wherever it appears, so compute the diff for
    # each distinct row TF across all stacks exactly once and reuse it.
    #
    # NO LOOK-AHEAD: the last bar of each row frame is the in-progress (forming)
    # candle whose close is still drifting tick-by-tick. Feeding it into
    # ``_diff_series`` lets the forming sub-TF colour propagate through
    # ``_map_onto`` onto every confirmed base bar whose timestamp falls inside
    # the still-open sub-TF candle — a textbook look-ahead leak that makes
    # confirmed-bar triggers flicker as the forming bar moves. ``iloc[:-1]``
    # drops it so confirmed base bars only see closed sub-TF diffs. The base
    # frame itself is NOT trimmed below: its forming bar is excluded from
    # trigger detection by ``_detect_triggers`` (``state[1:n-1]``) but its
    # latest close is still needed for the open trade's mark-to-market.
    #
    # row_diffs_live is the forming-INCLUDED counterpart used ONLY for the
    # display-only flip-proximity preview (the live "almost a trigger" gauge).
    # It is never fed to _detect_triggers / _pair_trades, so the look-ahead fix
    # above is preserved.
    needed = {tf for stack in stacks.values() for tf in stack}
    row_diffs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    row_diffs_live: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for label in needed:
        df = frames.get(label)
        if df is None or df.empty:
            continue
        if len(df) > 2:
            row_diffs[label] = _diff_series(df.iloc[:-1], period)
        if len(df) > 1:
            row_diffs_live[label] = _diff_series(df, period)

    by_base: dict[str, DwsSmtWindow] = {}
    for base, rows in stacks.items():
        base_df = frames.get(base)
        if base_df is None or base_df.empty or len(base_df) < 2:
            continue
        window = _build_window(base, base_df, rows, row_diffs, row_diffs_live,
                               smooth, out_bars, bias_contrib,
                               flip_window, flip_k)
        if window is not None:
            by_base[base] = window
```

- [ ] **Step 3d: Add `flip_window` / `flip_k` kwargs to `compute_symbol`**

The current `compute_symbol` signature (~line 326) is:

```python
def compute_symbol(
    frames: dict[str, pd.DataFrame],
    *,
    stacks: dict[str, tuple[str, ...]] = config.DWS_SMT_STACKS,
    period: int = config.DWS_SMT_PERIOD,
    smooth: int = config.DWS_SMT_SMOOTH,
    out_bars: int = config.DWS_SMT_BARS,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> DwsSmtResult | None:
```

Add two kwargs:

```python
def compute_symbol(
    frames: dict[str, pd.DataFrame],
    *,
    stacks: dict[str, tuple[str, ...]] = config.DWS_SMT_STACKS,
    period: int = config.DWS_SMT_PERIOD,
    smooth: int = config.DWS_SMT_SMOOTH,
    out_bars: int = config.DWS_SMT_BARS,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
    flip_window: int = config.DWS_FLIP_STD_WINDOW,
    flip_k: float = config.DWS_FLIP_K,
) -> DwsSmtResult | None:
```

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_dws_smt.py -v`
Expected: all PASS (existing 23 + 4 flip_norm + 2 new window tests = 29). The existing `test_forming_subtf_bar_never_changes_confirmed_base_triggers` MUST still pass (triggers unchanged).

- [ ] **Step 5: Commit**

```bash
git add analyzer/dws_smt.py tests/test_dws_smt.py
git commit -m "feat(dws): forming-inclusive flip_norm on the window (display-only)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Serialise `flip_norm` as `fn`

**Files:**
- Modify: `dashboard/serialize.py` (`serialize_dws_smt` ~line 163-180)
- Test: `tests/test_state_and_serialize.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_serialize_dws_smt_includes_flip_norm():
    import numpy as np
    from analyzer.dws_smt import DwsSmtWindow, DwsSmtResult
    from dashboard.serialize import serialize_dws_smt
    win = DwsSmtWindow(
        base_tf="M15", rows=("H4", "H1", "M15"),
        times_ms=np.array([1, 2], dtype=np.int64),
        colors=np.array([[0, 1, 2], [0, 0, 0]], dtype=np.int8),
        triggers=(None, "BUY"),
        trades=(),
        bias=np.array([0.0, 1.0]),
        flip_norm=np.array([[0.5, -0.5, 0.0], [1.0, 0.2, -0.9]]),
    )
    out = serialize_dws_smt(DwsSmtResult(by_base={"M15": win}))
    blk = out["by_base"]["M15"]
    assert "fn" in blk
    assert blk["fn"] == [[0.5, -0.5, 0.0], [1.0, 0.2, -0.9]]
    assert serialize_dws_smt(None) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_state_and_serialize.py::test_serialize_dws_smt_includes_flip_norm -v`
Expected: FAIL — `KeyError: 'fn'`

- [ ] **Step 3: Add `fn` to the serialiser**

In `dashboard/serialize.py` `serialize_dws_smt`, the window dict currently is:

```python
                "t": w.times_ms.tolist(),
                "c": w.colors.tolist(),
                "g": list(w.triggers),
                "bias": [round(float(x), 2) for x in w.bias],
```

Add an `fn` line after `bias` (rounded to 3 dp — enough for a gradient, keeps the payload small):

```python
                "t": w.times_ms.tolist(),
                "c": w.colors.tolist(),
                "g": list(w.triggers),
                "bias": [round(float(x), 2) for x in w.bias],
                "fn": [[round(float(v), 3) for v in row] for row in w.flip_norm],
```

Also update the docstring line listing the arrays (add `fn` = per-bar `[row,...]` signed distance-to-flip in [-1,1]).

- [ ] **Step 4: Run to verify pass**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest tests/test_state_and_serialize.py::test_serialize_dws_smt_includes_flip_norm -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add dashboard/serialize.py tests/test_state_and_serialize.py
git commit -m "feat(serialize): ship DWS flip_norm as fn

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Frontend gradient + holdout emphasis

**Files:**
- Modify: `static/app.js` (`DWS_CELL` area ~line 1952 for a helper + const; `drawDwsCanvas` row loop ~line 2708-2718)

- [ ] **Step 1: Add the imminent const + gradient helper**

In `static/app.js`, after `const DWS_CELL = ['#00d09c', '#ff5b6b', '#3f4760'];` (~line 1952), add:

```javascript
// Flip-proximity render: a row cell's hue is the sign of its flip_norm and its
// alpha its magnitude — near the zero-cross (|fn|→0, a flip/trigger imminent)
// the cell goes pale; firmly aligned (|fn|→1) it is solid, matching the old
// flat look. DWS_FLIP_IMMINENT gates the current-bar holdout emphasis.
const DWS_FLIP_IMMINENT = 0.25;

/** Canvas fill for a DWS row cell from its signed flip-norm. Falls back to the
 *  flat colour index when fn is missing/non-finite (older snapshot). */
function dwsCellFill(fn, fallbackIdx) {
    if (fn == null || !isFinite(fn)) return DWS_CELL[fallbackIdx] || DWS_CELL[2];
    const mag = Math.min(1, Math.abs(fn));
    const a = (0.20 + 0.80 * mag).toFixed(3);     // pale near a flip, solid when aligned
    if (fn > 0) return `rgba(0,208,156,${a})`;     // up = green
    if (fn < 0) return `rgba(255,91,107,${a})`;    // down = red
    return DWS_CELL[2];                            // exactly flat = neutral grey
}
```

- [ ] **Step 2: Replace the row-cell fill with the gradient**

In `drawDwsCanvas`, the current row loop (~line 2708) is:

```javascript
    // 3 stacked colour rows
    for (let r = 0; r < rows.length; r++) {
        const y = plotY + r * rowH;
        for (let j = 0; j < N; j++) {
            ctx.fillStyle = DWS_CELL[win.c[j][r]] || DWS_CELL[2];
            ctx.fillRect(plotX + j * barW, y + 1, Math.max(1, barW - 0.4), rowH - 2);
        }
        ctx.fillStyle = '#f2f4f9';
        ctx.font = '700 11px monospace';
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillText(rows[r], 4, y + rowH / 2);
    }
```

Replace it with (gradient fill from `win.fn`; `win.c` retained as the fallback):

```javascript
    // 3 stacked rows — gradient fill: hue = sign(flip_norm), alpha = |flip_norm|
    // (pale near a flip, solid when firmly aligned). win.c is the fallback for
    // older snapshots without fn.
    const fn = win.fn || null;
    for (let r = 0; r < rows.length; r++) {
        const y = plotY + r * rowH;
        for (let j = 0; j < N; j++) {
            const fv = (fn && fn[j]) ? fn[j][r] : null;
            ctx.fillStyle = dwsCellFill(fv, win.c[j][r]);
            ctx.fillRect(plotX + j * barW, y + 1, Math.max(1, barW - 0.4), rowH - 2);
        }
        ctx.fillStyle = '#f2f4f9';
        ctx.font = '700 11px monospace';
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillText(rows[r], 4, y + rowH / 2);
    }

    // Holdout emphasis on the CURRENT (rightmost) bar: when exactly two rows
    // share an aligned colour and the third is near its flip, ring that third
    // cell and label its TF — the literal "2 aligned, 1 about to complete".
    if (fn && N > 0) {
        const cj = N - 1, cc = win.c[cj], cf = fn[cj];
        if (cc && cf) {
            for (const dir of [0, 1]) {              // 0 = all-up holdout, 1 = all-down
                const aligned = [];
                let holdout = -1;
                for (let r = 0; r < rows.length; r++) {
                    if (cc[r] === dir) aligned.push(r); else holdout = r;
                }
                if (aligned.length === rows.length - 1 && holdout >= 0
                    && Math.abs(cf[holdout]) < DWS_FLIP_IMMINENT) {
                    const y = plotY + holdout * rowH;
                    const x = plotX + cj * barW;
                    ctx.strokeStyle = dir === 0 ? 'rgba(0,208,156,0.95)'
                                                : 'rgba(255,91,107,0.95)';
                    ctx.lineWidth = 2;
                    ctx.strokeRect(x + 0.5, y + 1.5, Math.max(1, barW - 1.4), rowH - 3);
                    ctx.fillStyle = '#fff';
                    ctx.font = '700 9px monospace';
                    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
                    ctx.fillText(rows[holdout] + (dir === 0 ? '▲' : '▼'),
                                 x - 1, y + rowH / 2);
                }
            }
        }
    }
```

- [ ] **Step 3: JS syntax check**

Run: `cd C:/Users/ohuch/Desktop/MT5_Python && node --check static/app.js`
Expected: exit 0, no output.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(dws-ui): flip-proximity gradient + current-bar holdout emphasis

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Integration verification (live server + browser)

**Files:** none (verification only)

- [ ] **Step 1: Full suite green**

Run: `"C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q`
Expected: all PASS (339 + 6 new = 345).

- [ ] **Step 2: Restart server (backend changed)**

```powershell
$c = Get-NetTCPConnection -LocalPort 8050 -State Listen -ErrorAction SilentlyContinue
if ($c) { Stop-Process -Id $c.OwningProcess -Force }
```
Then: `cd C:/Users/ohuch/Desktop/MT5_Python && nohup "C:/Users/ohuch/AppData/Local/Python/pythoncore-3.14-64/python.exe" main.py > /tmp/mt5_server.log 2>&1 &`

- [ ] **Step 3: Confirm `fn` ships on the DWS block**

Connect `ws://127.0.0.1:8050/ws`, read a full snapshot, assert
`analysis.by_symbol.XAUUSD.dws.by_base.M15.fn` is a `[n][3]` array of floats in
[-1, 1] with the same length as `c`.

- [ ] **Step 4: Browser before/after**

Open `http://127.0.0.1:8050/`, expand a panel, screenshot the DWS canvas. Verify:
- row cells show a gradient (some pale near-flip cells, some solid),
- when a panel currently has 2 rows aligned + 1 near flip, the current bar shows the ring + TF label,
- `console --errors` is empty.
Read the screenshot to confirm visually.

- [ ] **Step 5: Tune (visual)**

If the gradient is too subtle / too aggressive, adjust the `0.20 + 0.80*mag`
alpha ramp and `DWS_FLIP_IMMINENT` in `app.js` (frontend, browser reload only),
and `DWS_FLIP_K` / `DWS_FLIP_STD_WINDOW` in `config.py` (needs server restart).
Re-screenshot until it reads well. Commit any tuning.

---

## Self-Review

**Spec coverage:**
- §2 flip_norm formula (clamp, rolling_std, k) → Task 2 `_flip_norm` ✓
- §3 trigger/preview separation (forming-excluded triggers, forming-inclusive proximity) → Task 3 (`row_diffs` vs `row_diffs_live`) + `test_flip_norm_forming_inclusive_while_triggers_are_not` ✓
- §4 data contract (`flip_norm` field, `fn` key) → Task 3 (field) + Task 4 (serialise) ✓
- §5 rendering (gradient hue=sign/alpha=mag; holdout emphasis; win.c retained; buildCompactDws unchanged) → Task 5 ✓
- §6 tests (backend unit + serialise + frontend visual) → Tasks 2,3,4,6 ✓
- §7 files (dws_smt, serialize, app.js, config; indicator_engine unchanged) → Tasks 1-5 ✓
- §8 out-of-scope (no trade/trigger/order use) → enforced by the row_diffs_live isolation, asserted in Task 3 ✓

**Placeholder scan:** none — every code step shows complete code; the only "tune" step (Task 6 Step 5) is explicit about which constants and is post-verification polish, not a code gap.

**Type consistency:** `flip_norm` (field) / `_flip_norm` (helper) / `fn` (JSON key) / `win.fn` (JS) / `dwsCellFill` / `DWS_FLIP_IMMINENT` / `DWS_FLIP_STD_WINDOW` / `DWS_FLIP_K` used consistently across tasks. `_build_window` new params (`row_diffs_live`, `flip_window`, `flip_k`) match `compute_symbol`'s call site and kwargs.
