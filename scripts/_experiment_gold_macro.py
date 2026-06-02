"""GoldMacroScore design exploration -- which transform/driver carries signal?

The production level-z equal-weight composite was REJECTED (zero IC). Before
spending effort on new data pipelines (COT / GLD) or a redesigned production
score, this offline experiment measures, on the SAME four FRED drivers we
already have, a diagnostic IC matrix:

    {driver} x {level-z, change-z} x {5d, 20d, 60d horizon}
    + composite variants (level-equal, change-equal)

Goal: learn WHERE (if anywhere) predictive signal lives -- in levels vs changes,
in which driver, at which horizon -- so any redesign is evidence-led, not a
guess. Read-only research: no production code changes, ASCII-only output.

No look-ahead: every signal uses trailing windows only; IC pairs signal[t] with
forward XAUUSD return over [t, t+h].
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import config  # noqa: E402
from analyzer import gold_macro as gm  # noqa: E402
import _oos_xauusd_16y as oos  # noqa: E402
import _validate_gold_macro as val  # noqa: E402  (reuse fetch + bootstrap IC)

WINDOW = config.GOLD_MACRO_WINDOW
CLAMP = config.GOLD_MACRO_Z_CLAMP
HORIZONS = (5, 20, 60)
CHG = 20                      # change-z lookback (trading days, ~1 month)


def _zscore(s: pd.Series, window: int) -> pd.Series:
    """Trailing population z-score (matches production ddof=0), flat -> 0."""
    mean = s.rolling(window).mean()
    std = s.rolling(window).std(ddof=0)
    z = (s - mean) / std
    return z.where(std > 0.0, 0.0).clip(-CLAMP, CLAMP)


def _ic_row(signal: pd.Series, xau_close: pd.Series) -> dict[int, tuple]:
    """(ic, lo, hi, n) per horizon for one daily signal vs fwd XAUUSD return."""
    sig = signal.reindex(
        xau_close.index.union(signal.index)).ffill().reindex(xau_close.index)
    out: dict[int, tuple] = {}
    for h in HORIZONS:
        fwd = xau_close.shift(-h) / xau_close - 1.0
        a = pd.concat([sig, fwd], axis=1, keys=["s", "r"]).dropna()
        if len(a) < 100:
            out[h] = (float("nan"), float("nan"), float("nan"), len(a))
            continue
        ic, lo, hi = val._moving_block_ic_ci(a["s"].to_numpy(), a["r"].to_numpy())
        out[h] = (ic, lo, hi, len(a))
    return out


def _fmt_cell(t: tuple) -> str:
    ic, lo, hi, n = t
    if not np.isfinite(ic):
        return "   n/a    "
    star = "*" if (np.isfinite(lo) and lo > 0.0) else (
        "x" if (np.isfinite(hi) and hi < 0.0) else " ")
    return "%+.4f%s" % (ic, star)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    print("=== GoldMacroScore design exploration (IC matrix) ===", flush=True)
    print("[1/3] fetching FRED histories ...", flush=True)
    hist = val._fetch_full_histories()
    if not hist:
        print("BLOCKED: no FRED data"); return 2

    print("[2/3] loading XAUUSD daily ...", flush=True)
    xau = oos._load_tf("XAUUSD", "D1", val.XAU_POINT)["close"]
    xau.index = pd.DatetimeIndex(xau.index).tz_localize(None).normalize()
    xau = xau[~xau.index.duplicated(keep="last")]

    # Build per-driver level-z and change-z signals (sign-adjusted so a positive
    # IC means "moves with gold as theory predicts").
    df = pd.DataFrame(hist).sort_index().ffill()
    level_sig: dict[str, pd.Series] = {}
    change_sig: dict[str, pd.Series] = {}
    for d in gm.GOLD_DRIVERS:
        if d.key not in df.columns:
            continue
        s = df[d.key]
        level_sig[d.key] = _zscore(s, WINDOW) * d.sign_gold
        change_sig[d.key] = _zscore(s.diff(CHG), WINDOW) * d.sign_gold

    print("[3/3] computing IC matrix (this runs the bootstrap per cell) ...",
          flush=True)
    hdr = "  %-22s " % "signal" + " ".join("%-11s" % ("%dd" % h) for h in HORIZONS)
    print("", flush=True)
    print("  (* = 95%% CI lower bound > 0 ; x = upper bound < 0 ; else noise)",
          flush=True)
    print(hdr, flush=True)
    print("  " + "-" * (22 + 1 + 12 * len(HORIZONS)), flush=True)

    def emit(label: str, sig: pd.Series) -> dict[int, tuple]:
        row = _ic_row(sig, xau)
        print("  %-22s " % label
              + " ".join("%-11s" % _fmt_cell(row[h]) for h in HORIZONS), flush=True)
        return row

    print("  -- per driver: LEVEL-z --", flush=True)
    for d in gm.GOLD_DRIVERS:
        if d.key in level_sig:
            emit("%s (level)" % d.key, level_sig[d.key])

    print("  -- per driver: CHANGE-z (%dd) --" % CHG, flush=True)
    for d in gm.GOLD_DRIVERS:
        if d.key in change_sig:
            emit("%s (chg%d)" % (d.key, CHG), change_sig[d.key])

    print("  -- composites (equal weight) --", flush=True)
    if level_sig:
        emit("composite LEVEL", pd.DataFrame(level_sig).mean(axis=1))
    if change_sig:
        emit("composite CHANGE", pd.DataFrame(change_sig).mean(axis=1))

    print("", flush=True)
    print("Read: a '*' cell is a driver/transform/horizon with statistically "
          "non-zero forward-return IC. If the whole matrix is noise, these four "
          "macro drivers do not time daily/weekly XAUUSD in any simple form, and "
          "a redesign should pivot to positioning/flow data (COT/GLD) rather "
          "than more macro-level engineering.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
