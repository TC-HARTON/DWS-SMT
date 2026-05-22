"""Unit tests for analyzer.correlation (SPEC §13)."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import config
from analyzer.correlation import CorrelationEngine


def _make_df(closes: np.ndarray) -> pd.DataFrame:
    n = closes.size
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": closes, "high": closes + 0.001, "low": closes - 0.001,
        "close": closes, "tick_volume": np.ones(n, dtype=np.int64),
        "spread": np.zeros(n, dtype=np.int32),
        "real_volume": np.zeros(n, dtype=np.int64),
    }, index=idx)


def _stub_connector(rate_map: dict[str, pd.DataFrame]) -> MagicMock:
    conn = MagicMock()

    def fake_fetch(symbols, windows):
        # Single TF passed in; emit map for each requested symbol.
        out: dict[tuple[str, str], pd.DataFrame] = {}
        for s in symbols:
            if s in rate_map:
                for w in windows:
                    out[(s, w.label)] = rate_map[s]
        return out
    conn.fetch_rates_parallel.side_effect = fake_fetch
    return conn


# --------------------------------------------------------------------------- #

def test_perfect_positive_correlation_when_series_are_identical():
    n = 600
    same = 100 + np.arange(n) * 0.001 + np.cumsum(np.random.default_rng(0).normal(0, 0.01, n))
    rates = {s.base: _make_df(same) for s in config.SYMBOLS}
    conn = _stub_connector(rates)
    snap = CorrelationEngine(conn).compute()
    for matrix in snap.by_window.values():
        m = matrix.matrix
        n_sym = m.shape[0]
        # Inspect every off-diagonal cell.
        off_diag = m[~np.eye(n_sym, dtype=bool)]
        assert off_diag.min() == pytest.approx(1.0, abs=1e-9)
        assert off_diag.max() == pytest.approx(1.0, abs=1e-9)


def test_perfect_negative_correlation_when_series_are_inverse():
    n = 600
    rng = np.random.default_rng(0)
    base = np.cumsum(rng.normal(0, 0.01, n)) + 100
    inv = -base + 200
    syms = list(config.SYMBOLS)
    rates = {syms[0].base: _make_df(base), syms[1].base: _make_df(inv)}
    for s in syms[2:]:
        rates[s.base] = _make_df(base)
    conn = _stub_connector(rates)
    snap = CorrelationEngine(conn).compute()
    for matrix in snap.by_window.values():
        i = matrix.symbols.index(syms[0].base)
        j = matrix.symbols.index(syms[1].base)
        # Tiny floating-point noise from base+inv arithmetic — ~1e-6.
        assert matrix.matrix[i, j] == pytest.approx(-1.0, abs=1e-5)


def test_skip_window_when_insufficient_bars():
    n = 50  # only enough for the 20-bar window
    rng = np.random.default_rng(0)
    closes = 100 + np.cumsum(rng.normal(0, 0.01, n))
    rates = {s.base: _make_df(closes) for s in config.SYMBOLS}
    conn = _stub_connector(rates)
    snap = CorrelationEngine(conn).compute()
    # 50 - 1 (pct_change consumes one) = 49 bars; only the 20-bar window
    # should compute. 100 and 500 must be omitted.
    assert 20 in snap.by_window
    assert 100 not in snap.by_window
    assert 500 not in snap.by_window


def test_no_data_returns_empty_snapshot():
    conn = _stub_connector({})
    snap = CorrelationEngine(conn).compute()
    assert snap.by_window == {}


def test_matrix_diagonal_is_exactly_one():
    n = 600
    rng = np.random.default_rng(0)
    rates = {
        s.base: _make_df(100 + np.cumsum(rng.normal(0, 0.01, n)))
        for s in config.SYMBOLS
    }
    conn = _stub_connector(rates)
    snap = CorrelationEngine(conn).compute()
    for matrix in snap.by_window.values():
        assert np.all(np.diag(matrix.matrix) == 1.0)
