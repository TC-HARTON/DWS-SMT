"""Unit tests for the GoldMacroScore composite (analyzer/gold_macro.py)."""

from __future__ import annotations

import math

import pytest

import config
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
