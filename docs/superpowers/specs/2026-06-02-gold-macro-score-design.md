# GoldMacroScore — XAUUSD-specific macro composite (design)

> Date: 2026-06-02 · Status: APPROVED (design), pending spec review
> Scope decision: **FRED 3-series only** for the prototype.
> Validation decision: **IC + OOS gate (both required)**.

## 1. Motivation

The dashboard already computes seven logic layers (BIAS, DWS-SMT trigger, 16Y
OOS validation, currency strength, correlation, macro rates/employment, US 10Y
real yield). Crucially the macro layer is **already gold-aware**:
`macro_feed.pair_macro_bias()` special-cases `XAU` (US-rate trend = gold
headwind/tailwind) and `RealYieldSnapshot.gold_dir = -sign(trend_5d)` encodes
"gold moves inverse to real yields".

What is missing is a **fusion layer** that combines the dominant gold drivers
into one economically-justified composite — the gold analogue of the Buffett
Indicator — on the same `-10..+10` scale as BIAS so it slots into existing UI
machinery. This spec defines that composite (**GoldMacroScore**) and, just as
importantly, the **validation that decides whether it earns a place in the
product**.

The Buffett Indicator's lesson is explicit here: a single composite can be
"eventually right" yet useless for timing. The discipline that protects against
that is the project's existing 16Y OOS validation harness. We therefore build
the score AND its validation, and we do **not** surface a tradeable number in
the UI until validation confirms it carries signal.

## 2. Drivers and gold direction

Four orthogonal-ish drivers, each a daily-moving FRED series. `sign_gold` maps
the driver to gold's expected direction.

| Driver | FRED series | sign_gold | Economic rationale |
|---|---|---|---|
| US 10Y real yield | `DFII10` | **−1** | Gold is zero-yield; rising real yield raises opportunity cost = headwind |
| 10Y breakeven inflation | `T10YIE` | **+1** | Rising inflation expectations lift inflation-hedge demand = tailwind |
| Risk sentiment (VIX) | `VIXCLS` | **+1** | Risk-off lifts safe-haven demand = tailwind |
| US dollar (broad index) | `DTWEXBGS` | **−1** | Gold is USD-priced; a stronger dollar = headwind |

Notes:
- `DFII10` is already fetched by the real-yield path, but only as a short window
  (latest + a few prior). GoldMacroScore needs a long history for z-scoring, so
  it fetches its own copy. The one extra `DFII10` request per refresh is
  accepted in exchange for clean separation (the real-yield display snapshot is
  unchanged).
- `DTWEXBGS` is the nominal **Broad** trade-weighted USD index (FRED H.10,
  daily). It is the true dollar measure, replacing the currency-strength
  USD-z-score proxy for this composite.

## 3. Score math

```
For each driver i with LEVEL history h_i (most recent WINDOW obs):
    z_i   = ( h_i[-1] - mean(h_i) ) / std(h_i)        # population z over the window
    zc_i  = clamp(z_i, -2.5, +2.5)                     # cap tail leverage
raw      = mean_i( sign_gold_i * zc_i )                # EQUAL weights, no fitting
score    = clamp(raw / 2.5 * 10, -10, +10)             # map to the BIAS scale
```

Design choices and their justification:
- **Level z-score (not change):** Buffett-analogue. We measure "how extreme is
  this driver relative to its recent regime", not its momentum. This is a
  deliberate, approved choice; a change-based variant is explicitly out of scope
  for the prototype.
- **Equal weights, no fitting:** weight-fitting on historical data is the
  primary overfitting risk. Equal weighting is the disciplined default; any
  fitted/regime-conditional weighting is deferred until after validation proves
  the equal-weight version carries signal.
- **WINDOW = 252 trading days (~1 year):** long enough to define a regime,
  short enough to adapt. A config constant so it can be tuned in validation.
- **±2.5 clamp:** prevents a single blown-out driver from saturating the
  composite.
- **Known limitation (documented, not hidden):** real yield and the dollar are
  correlated, so equal weighting double-counts the "real-rate/dollar" axis. We
  do NOT pre-orthogonalise in the prototype; instead the validation measures the
  *net* information coefficient, which is the honest test of whether the naive
  composite works. If validation shows the correlation hurts, orthogonalisation
  (e.g. PCA on the driver matrix) becomes a documented follow-up.

### Missing-data policy
- **Per-driver cached fallback (resilience, added after P1 integration
  testing).** A driver that fails a given fetch falls back to its last-good
  cached history, so a transient single-driver outage (e.g. a FRED 429 on one
  series) does NOT swing the composite or drop coverage. The driver cache is
  MERGED on each fetch, never wholesale-replaced — a partial success keeps every
  previously-good driver. `fetch_gold_drivers` returns `n_fresh` (drivers
  fetched fresh this call); the loop flags the snapshot `stale` and reschedules
  soon when `n_fresh < len(GOLD_DRIVERS)`, restoring full fresh coverage quickly
  rather than after a whole interval.
- A driver with **no cache yet** (never fetched successfully) and a failed fetch
  is dropped from the mean (it cannot be fabricated); the composite averages
  over the present drivers, mirroring `pair_macro_bias`'s "never penalise on
  bad/absent data" rule. The snapshot records which drivers were used
  (`n_drivers`, `contributions`) so the UI can flag reduced coverage.
- If **zero** drivers are usable (no fresh fetch and no cache), the score is
  `None` (UI shows "data pending").
- Rationale: a regime gauge must be stable. Integration testing showed a single
  dropped driver shifting the score materially (e.g. -0.30 → -1.75 when the lone
  tailwind driver blipped), so per-driver caching keeps the composite on its
  full driver set across transient outages.

## 4. Module architecture

Each unit has one purpose, a well-defined interface, and is testable in
isolation.

### 4.1 `analyzer/gold_macro.py` (new, pure)
- `@dataclass GoldDriver`: `key`, `series_id`, `sign_gold`, `label_ja`.
- `@dataclass GoldDriverContribution`: `key`, `value`, `z`, `signed_z`,
  `sign_gold`, `label_ja`.
- `@dataclass GoldMacroSnapshot`: `score: float | None`, `band: str`
  (構造的追風 / 中立 / 構造的逆風), `contributions: tuple[GoldDriverContribution, ...]`,
  `n_drivers: int`, `window: int`, `as_of: str`, `stale: bool`,
  `generated_at: float`.
- `GOLD_DRIVERS: tuple[GoldDriver, ...]` — the four-driver registry.
- `compute_gold_macro_score(histories: dict[str, list[float]], *, window, asof,
  stale) -> GoldMacroSnapshot` — pure: takes per-driver LEVEL histories
  (newest-last), computes z, signed contribution, composite, band. No network,
  no MT5 → fully unit-testable.
- Band thresholds: `score >= +3 → 構造的追風`, `score <= -3 → 構造的逆風`,
  else `中立` (constants in config).

### 4.2 `analyzer/macro_feed.py` (extend)
- `fetch_gold_drivers() -> dict[str, list[float]] + as_of`: for each driver in
  `gold_macro.GOLD_DRIVERS`, call the existing `_fred_get(series_id,
  limit=GOLD_MACRO_WINDOW + buffer)` and parse the full observation list into a
  chronological LEVEL history (reuse/extend the existing FRED observation
  parser, which currently returns only the latest point — add a list parser).
- Returns histories keyed by driver `key`, plus the newest observation date for
  `as_of`. On a per-series failure: that driver is omitted (logged at WARNING,
  redacted), never aborts the batch.
- Cache last-good gold-driver histories on disk alongside the existing macro
  cache so a restart / FRED outage degrades gracefully (`stale=True`).

### 4.3 `analyzer/state.py` (extend)
- `_gold_macro: GoldMacroSnapshot | None`, `set_gold_macro()`,
  `gold_macro` property, and inclusion in the analysis-version full snapshot —
  mirroring the existing `real_yield` plumbing exactly.

### 4.4 `analyzer/analysis_loop.py` (extend)
- New `_Schedule("goldmacro", GOLD_MACRO_REFRESH_SEC)` (hourly, matching the
  real-yield cadence since the drivers are daily-moving).
- `_do_goldmacro_refresh` / `_goldmacro_refresh_worker` on a daemon thread with
  an in-flight guard and `_reschedule_soon` retry on failure — same pattern as
  `_do_realyield_refresh`.
- Worker: `histories = macro_engine.fetch_gold_drivers()` →
  `snap = gold_macro.compute_gold_macro_score(...)` → `state.set_gold_macro(snap)`.

### 4.5 `dashboard/serialize.py` (extend)
- Add a `gold_macro` block to the **full** snapshot only (daily-moving, must not
  ride the ~2 Hz light snapshot — §5 invariant). Shape:
  `{score, band, n_drivers, window, as_of, stale, contributions:[{key, label,
  value, z, signed_z, sign}]}`. `None`/Inf guarded via `_opt_float`.

### 4.6 Config constants (new, in `config.py`)
- `MACRO_FRED_BREAKEVEN_SERIES = "T10YIE"`
- `MACRO_FRED_VIX_SERIES = "VIXCLS"`
- `MACRO_FRED_DXY_SERIES = "DTWEXBGS"`
- `GOLD_MACRO_WINDOW = 252`
- `GOLD_MACRO_Z_CLAMP = 2.5`
- `GOLD_MACRO_BAND_THRESHOLD = 3.0`
- `GOLD_MACRO_REFRESH_SEC = 3600`
- `GOLD_DRIVERS` registry lives in `gold_macro.py` (logic), series IDs in config.

## 5. Validation — `scripts/_validate_gold_macro.py` (new, offline)

The verdict that decides P3. ASCII-only output (cp932 console safe). Full Python
path, no Windows-store stub.

### 5.1 Information Coefficient (IC)
- Pull each driver's **full** FRED history.
- Reconstruct the daily GoldMacroScore time series with the same level-z /
  equal-weight math (reuse `gold_macro.compute_gold_macro_score` per day over an
  expanding/rolling window so there is **no look-ahead** — each day's z uses
  only data up to that day).
- Load XAUUSD **daily** closes (Dukascopy CSV / the 16Y dataset already used by
  `_oos_xauusd_16y.py`).
- Compute forward returns at horizons **5d and 20d**.
- **Spearman IC** = rank correlation(score_t, fwd_return_t) per horizon.
- **Bootstrap CI** on IC (moving-block, reuse the project's bootstrap approach)
  to test whether IC's CI excludes 0.

### 5.2 OOS gate
- Condition DWS-SMT triggers on the score regime: take longs only when
  `score >= +threshold`, shorts only when `score <= -threshold` (threshold swept
  over a small grid).
- Re-run the existing 16Y backtest harness (`signal_validator.evaluate_trades`
  via the `_oos_xauusd_16y` path) on the conditioned trade set.
- Compare conditioned PF / expectancy vs the unconditioned baseline,
  out-of-sample (respect the existing period split so the threshold is not
  tuned on the test half).

### 5.3 Verdict
- **ADOPT** iff: IC CI lower bound > 0 (at least one horizon) **AND** OOS
  conditioned PF improves materially and significantly over baseline.
- Otherwise **REJECT** — print the numbers and stop. We do not ship a noisy
  gauge. (Rejecting is a valid, documented outcome.)

## 6. Phasing

- **P1 — Score pipeline.** `gold_macro.py` + `fetch_gold_drivers` + state +
  serialize + config + unit tests. Deliverable: the score flows to the full WS
  snapshot. No UI yet.
- **P2 — Validation.** `_validate_gold_macro.py`. Deliverable: the IC + OOS
  verdict (ADOPT/REJECT) with numbers.
- **P3 — conditional on P2 = ADOPT.** Regime-gauge UI near the XAUUSD panel
  (`-10..+10`, four driver mini-bars, 追風/中立/逆風 band), reusing BIAS styling;
  optionally promote the score into the DWS degradation-gate machinery as a
  gold-specific filter. If P2 = REJECT, P3 is not built; the score may remain as
  an informational backend value or be removed.

## 7. Invariants preserved
- `gold_macro` rides the **full** snapshot only, never the ~2 Hz light path.
- No look-ahead in validation: each day's z-score uses only past data.
- `FRED_API_KEY` from env only; never logged (existing `_redact`).
- No bare excepts; type hints + docstrings on all new code.
- Equal-weight / no-fitting keeps the prototype free of in-sample tuning.
- ASCII-only script output (cp932 console).

## 8. Out of scope (deferred / explicitly not now)
- COT (CFTC positioning) and GLD ETF-holding feeds — deferred (new pipelines).
- Central-bank physical buying — not real-time; informational regime context
  only, not modelled.
- Fitted / regime-conditional weights and PCA orthogonalisation — only if
  validation shows the naive composite needs them.
- Change-based (momentum) z-score variant.
- Promoting to the DWS gate (P3, and only if validation passes).

## 9. Test plan (P1)
- `tests/test_gold_macro.py`:
  - z-score + signed contribution + composite math on a hand-computed fixture.
  - sign mapping per driver (rising real yield → negative contribution, etc.).
  - missing-driver drop (mean over present drivers; `n_drivers` correct).
  - all-missing → `score = None`.
  - band thresholds (追風/中立/逆風 boundaries, inclusive/exclusive).
  - clamp at ±2.5.
- `tests/test_macro_feed.py` (extend): full-observation-list parser; per-series
  failure omits the driver without aborting; stale cache fallback.
- `tests/test_state_and_serialize.py` (extend): `gold_macro` round-trips into
  the full snapshot; absent on the light snapshot; None/Inf guarded.
