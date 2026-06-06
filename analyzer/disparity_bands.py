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
