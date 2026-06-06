"""EMA-stack mode table: M15 + H1 specs."""

from __future__ import annotations

import config


def test_modes_present_and_periods():
    names = [m.name for m in config.EMA_STACK_MODES]
    assert names == ["M15", "H1"]
    by = config.EMA_STACK_MODE_BY_NAME
    assert by["M15"].tf == "M15" and by["M15"].periods == (20, 80, 320)
    assert by["H1"].tf == "H1" and by["H1"].periods == (20, 80, 480)


def test_modes_center_is_largest():
    for m in config.EMA_STACK_MODES:
        assert m.periods[2] == max(m.periods)
