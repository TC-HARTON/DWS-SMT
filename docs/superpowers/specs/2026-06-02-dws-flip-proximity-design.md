# DWS histogram — flip-proximity gradient + holdout emphasis (design)

> Date: 2026-06-02 · Status: design (pending spec review)
> Approved direction: 3-row proximity gradient + holdout emphasis;
> proximity preview INCLUDES the forming bar (display-only, isolated from
> trigger detection).

## 1. Motivation

The DWS-SMT histogram colours each of a base TF's three stack rows by the SIGN
of that row's smoothed diff (`_colorize`: >0 green / <0 red / =0 grey); a trigger
fires when all three align. The continuous magnitude of each row's smoothed diff
— how far it is from the zero-cross that flips its colour — is computed in
`_build_window` and then **discarded**. That discarded magnitude is exactly
"how close is this row to flipping", i.e. "how close are we to a trigger".

This feature surfaces that magnitude as a per-row gradient so the user can see
"もう少しでトリガー" before it fires. It invents NO new signal — it exposes a
value the engine already computes — so it needs no statistical validation, only
visual verification.

Empirical grounding (measured across 8 symbols, full history): for the M15
stack the row that completes the alignment is M15 69.4%, H1 14.7%, H4 8.7%,
simultaneous 7.2%. Because ~31% of triggers are completed by a NON-M15 row, the
gradient must cover **all three rows**, not M15 alone.

## 2. The quantity: flip-proximity

For each stack row, the smoothed diff series is `sd = EMA(mapped_diff,
smooth=5, seed=0)` (the value `_colorize` reduces to a sign). Define a signed,
self-normalised "flip-norm":

```
flip_norm[bar][row] = clamp( sd / (k * rolling_std(sd, W)), -1, +1 )
```

- `rolling_std(sd, W)` — trailing population std of that row's own smoothed-diff
  series (self-referential scale; no new data; matches the codebase's z-score
  idiom). `W` and `k` are config constants (defaults `W = DWS_FLIP_STD_WINDOW =
  96`, `k = DWS_FLIP_K = 1.0`), tuned visually.
- **sign(flip_norm)** = the row's current direction (green/red).
- **|flip_norm|** = how firmly aligned: `~0` = at the zero-cross (flip / trigger
  imminent), `~1` = firmly in its colour.
- **flip-proximity = 1 − |flip_norm|** (closeness to a flip), used for the
  gradient brightness.

Degenerate cases (matching the production module's conventions): a flat window
(`rolling_std == 0`) yields `flip_norm = 0`; the warmup region (fewer than `W`
bars) yields `flip_norm` from whatever trailing data exists (never NaN/inf —
guarded).

## 3. Trigger / preview separation (no look-ahead regression)

Two parallel computations, never mixed:

- **Trigger + colour path (unchanged):** row diffs are computed forming-EXCLUDED
  (`_diff_series(df.iloc[:-1], period)` — the existing look-ahead fix), colorised,
  and `_detect_triggers` further excludes the forming base bar. Triggers and the
  existing `colors` array are byte-for-byte unchanged.
- **Flip-proximity path (new, display-only):** row diffs computed
  forming-INCLUSIVE (`_diff_series(df, period)`) so the current forming bar's
  `flip_norm` reflects live prices — this is the real-time "もう少しで". The
  proximity is NEVER consulted by trigger detection, trade pairing, the
  validation layer, or order logic. It is a visualisation field only.

This reconciles with the look-ahead root-cause fix: that fix protects the
TRIGGER (which still waits for bar close); the proximity is an explicit
"watch this" preview, not a tradeable trigger.

## 4. Data contract

`DwsSmtWindow` gains one field:

- `flip_norm: np.ndarray` shape `(n_out, n_rows)`, float in `[-1, +1]`, one value
  per emitted bar per stack row (same indexing as `colors`).

Serialised into the full WS snapshot alongside the existing window arrays (the
DWS block already rides the full snapshot, not the light path). Front-end key:
`win.fn` (mirroring the terse `win.c` / `win.g` keys), a `[n][rows]` array.

`None`/Inf guarded via the existing serialiser helpers.

## 5. Rendering (`static/app.js drawDwsCanvas`)

- Each of the 3 row cells, currently a flat `DWS_CELL[win.c[j][r]]` fill, becomes
  a **gradient**: hue = `sign(win.fn[j][r])` (green/red; grey when ~0),
  brightness/saturation from `|win.fn[j][r]|` (near-zero → pale, ~1 → saturated).
  The current flat look is the `|fn|≈1` end, so firmly-aligned bars look
  essentially as they do today; only near-flip bars get visibly paler.
  - Hue source is `flip_norm`, NOT `win.c`: for confirmed bars `sign(flip_norm)`
    equals the existing colour (they differ only at the recent tail), and on the
    forming/current bar it shows the LIVE direction — which is the whole point.
    `win.c` is retained unchanged and continues to drive trigger detection and
    the trigger markers/guide lines (so markers stay anchored to confirmed
    triggers, never to the live preview).
  - `buildCompactDws` (the collapsed-panel summary) is UNCHANGED — it reports
    alignment state, not the gradient. Only `drawDwsCanvas`'s 3 row cells change.
- **Holdout emphasis (current bar):** when exactly two rows share an aligned
  colour AND the third row's `|fn|` for the current (rightmost) bar is below
  `DWS_FLIP_IMMINENT` (default 0.25), draw an emphasis on that bar's third-row
  cell — a bright ring/border plus the holdout TF label (e.g. "H1 ▲ soon"). This
  is the literal "2 aligned, 1 about to complete" state.
- Tunable constants (`k`, `W`, imminent threshold, ring/pulse strength) are
  adjusted by eye on the live dashboard.

## 6. Testing & verification

- **Backend unit tests** (`tests/test_dws_smt.py`):
  - `flip_norm` shape matches `colors` (n_out × n_rows).
  - Near a zero-cross the magnitude `|flip_norm|` → ~0; firmly aligned → ~1.
  - Sign of `flip_norm` matches the smoothed-diff sign.
  - Clamp at ±1; flat window → 0; no NaN/inf on short/degenerate input.
  - Forming-inclusive: the proximity path sees the forming bar while
    `_detect_triggers` does not (a probe asserting triggers are unchanged when
    only the forming bar moves — i.e. the existing look-ahead regression test
    still passes, and `flip_norm` of the current bar DOES move).
- **Serialise test** (`tests/test_state_and_serialize.py` or dws serialise
  test): `flip_norm` present in the serialised window, correct shape, guarded.
- **Frontend:** `node --check`; then live server restart → browser before/after
  screenshots confirming gradient + holdout emphasis render with zero console
  errors. No statistical validation (this surfaces an existing computed value).

## 7. Scope / files

- `analyzer/dws_smt.py` — compute forming-inclusive smoothed diffs, `flip_norm`
  array, add to `DwsSmtWindow`.
- `analyzer/indicator_engine.py` — pass forming-inclusive row diffs (or compute
  inside dws_smt); keep the forming-excluded trigger path intact.
- `dashboard/serialize.py` — serialise `flip_norm` as `fn`.
- `static/app.js` — gradient render + holdout emphasis in `drawDwsCanvas`.
- `config.py` — `DWS_FLIP_STD_WINDOW`, `DWS_FLIP_K`, `DWS_FLIP_IMMINENT`.
- Tests as above.

Small, single-plan scope. No subagent fan-out needed — TDD on the backend math +
visual verification on the frontend.

## 8. Out of scope

- The composite "synthesized graph" (① — held by the user).
- Any use of proximity in trade/trigger/validation/order logic (display only).
- Per-tick updates beyond the existing analysis-cycle cadence.
- Changing the trigger definition, trade pairing, or pips/cost handling.
