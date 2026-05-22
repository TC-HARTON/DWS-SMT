"""Unit tests for analyzer.confluence (SPEC §10.4)."""

from __future__ import annotations

import pytest

from analyzer.confluence import detect
from analyzer.structure_types import LevelKind, LevelSource, StructureLevel


def _lv(price: float, importance: int = 1, name: str | None = None) -> StructureLevel:
    return StructureLevel(
        symbol="X",
        name=name or f"L{price}",
        kind=LevelKind.HORIZONTAL,
        category="resistance",
        source=LevelSource.AUTO_DETECT,
        price=price,
        importance=importance,
    )


# --------------------------------------------------------------------------- #
# No-input / no-ATR guards
# --------------------------------------------------------------------------- #

def test_no_clusters_without_atr():
    assert detect([_lv(100)], atr_h4=None, current_price=100.0) == []


def test_no_clusters_without_levels():
    assert detect([], atr_h4=1.0, current_price=100.0) == []


def test_no_clusters_with_nonpositive_atr():
    assert detect([_lv(100), _lv(100.05), _lv(100.1)],
                  atr_h4=0.0, current_price=100.0) == []


# --------------------------------------------------------------------------- #
# Band / min-element behaviour
# --------------------------------------------------------------------------- #

def test_three_levels_within_band_form_one_cluster():
    # ATR=1.0, band = 0.3 → all three within 0.3 of centre.
    levels = [_lv(100.0), _lv(100.1), _lv(100.2)]
    clusters = detect(levels, atr_h4=1.0, current_price=100.0)
    assert len(clusters) == 1
    assert clusters[0].score == 3
    assert clusters[0].importance_label == "★"


def test_two_levels_do_not_meet_min_elements():
    levels = [_lv(100.0), _lv(100.1)]
    clusters = detect(levels, atr_h4=1.0, current_price=100.0)
    assert clusters == []


def test_levels_outside_band_break_into_separate_clusters():
    # ATR=1.0, band=0.3. Group A: 100.0, 100.1, 100.2 (within band).
    # Group B: 101.0, 101.1, 101.2 (within band).
    levels = [_lv(100.0), _lv(100.1), _lv(100.2),
              _lv(101.0), _lv(101.1), _lv(101.2)]
    clusters = detect(levels, atr_h4=1.0, current_price=100.5)
    assert len(clusters) == 2
    centres = sorted(c.center for c in clusters)
    assert centres[0] == pytest.approx(100.1, abs=1e-9)
    assert centres[1] == pytest.approx(101.1, abs=1e-9)


# --------------------------------------------------------------------------- #
# Importance label (SPEC §10.4: 3 < 4 < 5+ elements)
# --------------------------------------------------------------------------- #

def test_importance_label_increases_with_element_count():
    base = 100.0
    levels_3 = [_lv(base + i * 0.05) for i in range(3)]
    levels_4 = [_lv(base + i * 0.05) for i in range(4)]
    levels_5 = [_lv(base + i * 0.05) for i in range(5)]
    assert detect(levels_3, atr_h4=1.0, current_price=base)[0].importance_label == "★"
    assert detect(levels_4, atr_h4=1.0, current_price=base)[0].importance_label == "★★"
    assert detect(levels_5, atr_h4=1.0, current_price=base)[0].importance_label == "★★★"


# --------------------------------------------------------------------------- #
# Sort order (score desc, then distance asc)
# --------------------------------------------------------------------------- #

def test_clusters_sorted_score_desc_then_distance_asc():
    base = 100.0
    # Cluster A: 4 weak levels close to price (score 4, distance ~0)
    cluster_a = [_lv(base + i * 0.05) for i in range(4)]
    # Cluster B: 3 strong levels far from price (score 9, distance ~5)
    cluster_b = [_lv(base + 5.0 + i * 0.05, importance=3) for i in range(3)]
    clusters = detect(cluster_a + cluster_b, atr_h4=1.0, current_price=base)
    assert len(clusters) == 2
    # Cluster B has higher score (9 vs 4), must come first.
    assert clusters[0].score == 9
    assert clusters[1].score == 4


def test_weighted_center_pulled_by_high_importance_level():
    levels = [
        _lv(100.0, importance=1),
        _lv(100.3, importance=3),  # strong line near upper edge
    ] + [_lv(100.1, importance=1)]
    clusters = detect(levels, atr_h4=1.0, current_price=100.0,
                      min_elements=3)
    assert len(clusters) == 1
    # weighted by importance: (100*1 + 100.1*1 + 100.3*3) / 5 = 100.2
    assert clusters[0].center == pytest.approx(100.2, abs=1e-6)
    # Sanity: a higher-importance level near the upper edge pulls the centre
    # closer to itself than the unweighted arithmetic mean would.
    unweighted = (100.0 + 100.1 + 100.3) / 3   # = 100.1333
    assert clusters[0].center > unweighted
