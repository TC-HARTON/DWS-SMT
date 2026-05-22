# Real-Yield Layer Implementation Plan

> **For agentic workers:** implement task-by-task; each task is TDD where a test
> applies. Steps use `- [ ]` checkboxes.

**Goal:** Add the US 10-year TIPS real yield (FRED `DFII10`) as a fast-refresh
(1 h), daily-moving signal — used to drive the XAUUSD macro direction (gold
moves inverse to real yields) and shown in the macro panel.

**Architecture:** A new `realyield` schedule (1 h) in the analysis loop fetches
`DFII10` off-thread and publishes a `RealYieldSnapshot` to `LatestState`,
separate from the 6 h `MacroSnapshot` (policy rates are step functions; the real
yield moves daily, so it gets its own faster cadence). Serialization ships it;
the front end shows it and routes XAUUSD's macro direction through it.

**Spec:** `docs/superpowers/specs/2026-05-22-precision-optimization-design.md` §B.11.

## Pre-flight
- Python: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe`.
- Branch `feature/signal-validation-layer`. Repo under git.
- `FRED_API_KEY` is set in `.env` (verified working).
- 198 tests currently pass.

---

## Task 1: Config

Modify `config.py` — add after `MACRO_CURRENCIES`:
```python
# Real-yield layer (spec §B.11): the US 10Y TIPS real yield moves daily, so it
# gets a faster schedule than the 6 h policy-rate refresh.
MACRO_FRED_REALYIELD_SERIES: Final[str] = "DFII10"   # 10Y TIPS real yield
MACRO_REALYIELD_REFRESH_SEC: Final[float] = 3600.0   # 1 hour
```
Verify import; commit `feat: add real-yield config constants`.

---

## Task 2: `RealYieldSnapshot` + `MacroEngine.fetch_real_yield`

`RealYieldSnapshot` dataclass:
```python
@dataclass(frozen=True)
class RealYieldSnapshot:
    value: float | None        # latest US 10Y TIPS real yield, %
    prev_value: float | None   # prior business day
    change_1d: float | None    # value - prev_value
    trend_5d: float | None     # value - value ~5 business days ago
    gold_dir: int              # -1 rising (gold headwind) / +1 falling / 0
    as_of: str
    stale: bool
    generated_at: float
```

`MacroEngine.fetch_real_yield()` — fetch `DFII10` (limit=12), sort observations
ascending, take latest as `value`, prior as `prev_value`, the point ~5 rows
back as the 5-day reference. `gold_dir = -sign(trend_5d)` with a 0.02 (2 bp)
dead-band. On fetch failure, reuse an in-memory `_cached_real_yield` flagged
`stale`, else an all-None snapshot. `_cached_real_yield` initialised to `None`
in `__init__`.

Tests (`tests/test_macro_feed.py`): a stubbed `_fred_get` returning rising
observations → `gold_dir == -1`; falling → `+1`; flat → `0`; failure → stale.

Commit `feat: RealYieldSnapshot + fetch_real_yield`.

---

## Task 3: State slot

`analyzer/state.py` — add `set_real_yield` / `real_yield` mirroring `set_macro`
(heavy domain, bumps `_analysis_version`); add to `snapshot()`. Test + commit.

---

## Task 4: `realyield` schedule + worker

`analyzer/analysis_loop.py` — add `realyield_interval` param, an
`_Schedule("realyield", realyield_interval)`, dispatch handler, and
`_do_realyield_refresh` / `_realyield_refresh_worker` mirroring the macro worker
(in-flight guard, daemon thread). Commit.

---

## Task 5: serialize

`dashboard/serialize.py` — `serialize_real_yield(s)` → dict with all fields
(`_opt_float` for floats); wire into `snapshot_to_json` after `"macro"`. Test +
commit.

---

## Task 6: Front end

`static/app.js` + `app.css`:
- `paintMacro`: add a real-yield line — `米実質利回り {value}% (前日比 {±change_1d})`,
  green/red on `change_1d` sign. Source: `snap.real_yield`.
- `dwsTriggerMacroAlign`: for `sym === 'XAUUSD'`, use
  `snap.real_yield.gold_dir` instead of `snap.macro.by_pair`.
- The macro panel's XAUUSD row likewise shows the real-yield-driven direction.

Commit.

---

## Task 7: Verify

Full suite; restart `main.py`; confirm the `realyield` fetch succeeds (DFII10
via FRED); browser check the macro panel shows the real-yield line; HTTP
responsive. Commit.
