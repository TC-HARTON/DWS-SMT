"""ガード: 全TFが統一トレンドEMA期間(② EMA20統一)を参照することを保証する。

1本だけ ema_period が書き換わって「統一」が黙って崩れる事故を防ぐ不変条件テスト。
"""

from __future__ import annotations

import config


def test_trend_ema_period_is_20():
    assert config.TREND_EMA_PERIOD == 20


def test_all_timeframes_use_unified_trend_ema():
    assert config.TIMEFRAMES, "TIMEFRAMES must not be empty"
    for tf in config.TIMEFRAMES:
        assert tf.ema_period == config.TREND_EMA_PERIOD, (
            f"{tf.label} ema_period={tf.ema_period} "
            f"!= TREND_EMA_PERIOD={config.TREND_EMA_PERIOD}"
        )
