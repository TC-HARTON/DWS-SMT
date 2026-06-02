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
