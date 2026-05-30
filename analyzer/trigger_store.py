"""Persistent live trigger-history store (append-only, per broker).

The dashboard's トリガー履歴 table merges the frozen 16-year backtest baseline
(``oos_baseline.json``, years ≤ its ``last_year``) with the LIVE broker feed.
The live feed on its own is only a sliding window of the broker's resident bars
(M15 ≈ 7 months), so live triggers older than that window — and everything
recorded before the most recent restart — would be lost.

This module persists every CLOSED live trigger to an append-only JSONL store
keyed by BROKER (MT5 server) × symbol × base timeframe, so the live history
accumulates permanently: it survives restarts and broker-window slides, and a
year stays selectable at year-end and beyond.

Triggers are price-derived (broker price + spread), so the BROKER is the correct
boundary — the account / login is irrelevant (the same broker yields identical
triggers regardless of which account is logged in). Different brokers get
separate sub-directories so their spreads / prices never mix.

Open (still-running) triggers are NOT persisted — only settled outcomes count
toward the recorded win-rate / PF.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Iterable, Protocol

import pandas as pd

import config

log = logging.getLogger(__name__)

# Per-year recent-trade cap shipped to the dashboard (newest first). Mirrors the
# 16Y baseline's TRIGGER_LIST_CAP so live + backtest years render identically.
_TRIGGER_LIST_CAP = 30
_JST = "Asia/Tokyo"

# Filesystem-safe slug for an MT5 server name (e.g. "ICMarketsSC-MT5-3").
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")

# One lock per store file (process-wide) so concurrent append/read on the same
# file are serialised. Plus a cache of the entry-ms already on disk per file so
# repeated 5-minute cycles append only genuinely new triggers without re-reading
# the whole file each time. ``_by_year_cache`` memoises the fully-bucketed
# load_by_year() result keyed by the file's (size, mtime) so an unchanged store
# is not re-read / re-parsed / re-bucketed every cycle — it grows with years of
# live history, and append_closed bumps the size so the cache self-invalidates.
_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}
_seen: dict[Path, set[int]] = {}
_by_year_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


class _ClosableTrigger(Protocol):
    entry_ms: int
    direction: int
    net_pts: float
    is_open: bool


def _slug(server: str | None) -> str:
    """Filesystem-safe directory name for a broker server."""
    s = _SLUG_RE.sub("_", (server or "unknown").strip())
    return s or "unknown"


def store_path(server: str | None, symbol: str, tf: str) -> Path:
    """Path of the JSONL store for one broker × symbol × timeframe."""
    return config.LIVE_TRIGGER_DIR / _slug(server) / f"{symbol}_{tf}.jsonl"


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(path)
        if lk is None:
            lk = threading.Lock()
            _locks[path] = lk
        return lk


def _seen_set(path: Path) -> set[int]:
    """The set of entry-ms already persisted to *path* (loaded once, cached)."""
    cached = _seen.get(path)
    if cached is not None:
        return cached
    seen: set[int] = set()
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(int(json.loads(line)["t"]))
                except (ValueError, KeyError, TypeError):
                    continue  # skip a corrupt line rather than abort the load
    _seen[path] = seen
    return seen


def append_closed(
    server: str | None, symbol: str, tf: str,
    triggers: Iterable[_ClosableTrigger],
) -> int:
    """Append the CLOSED triggers in *triggers* that are not already stored.

    Each trigger needs ``entry_ms`` / ``direction`` / ``net_pts`` / ``is_open``
    (the ``RecentTrigger`` dataclass). Open triggers are skipped. De-duplicated
    by ``entry_ms``. Returns the number of newly written rows.
    """
    closed = [t for t in triggers if not bool(getattr(t, "is_open", False))]
    if not closed:
        return 0
    path = store_path(server, symbol, tf)
    with _lock_for(path):
        seen = _seen_set(path)
        fresh = [t for t in closed if int(t.entry_ms) not in seen]
        if not fresh:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for t in fresh:
                fh.write(json.dumps(
                    {"t": int(t.entry_ms), "d": int(t.direction),
                     "p": round(float(t.net_pts), 1)},
                    ensure_ascii=False,
                ) + "\n")
                seen.add(int(t.entry_ms))
        return len(fresh)


# --- Corruption invariant ------------------------------------------------- #
# A mis-detected server-clock offset used to re-stamp the SAME bar under several
# whole-hour offsets, so the entry-time-keyed store recorded one trade many times
# (the [0,+4h,+5h] fingerprint). The offset is now DST-correct and deterministic
# (mt5_connector), so a bar's entry_ms is stable and the entry_ms dedup below
# prevents recurrence. This invariant is the *tripwire*: it flags the re-stamp
# fingerprint loudly so a regression can never rot the store unnoticed. It does
# NOT flag two genuinely distinct trades that merely share a rounded net_pts —
# deleting real trades to make a metric look clean is itself appearance-faking.
_HOUR_MS = 3_600_000
_RESTAMP_WINDOW_MS = 6 * _HOUR_MS          # observed offset errors were <= 5 h


def scan_corruption(recs: list[dict[str, Any]]) -> dict[str, int]:
    """Detect server-offset re-stamp corruption in store records.

    Returns ``{"exact_t_dups": n, "tight_triples": n}``:
    * ``exact_t_dups`` — the SAME entry_ms recorded more than once (a bar stamped
      twice; should be impossible given the entry_ms dedup).
    * ``tight_triples`` — a ``(direction, round(net_pts, 1))`` group with 3+
      members inside a 6 h window, all whole-hour-aligned: the offset-bug
      fingerprint. Coincidental same-value pairs are intentionally NOT counted.
    Both zero ⇒ no re-stamp corruption.
    """
    ts_all = [int(r["t"]) for r in recs]
    exact = len(ts_all) - len(set(ts_all))
    groups: dict[tuple[int, float], list[int]] = {}
    for r in recs:
        groups.setdefault((int(r["d"]), round(float(r["p"]), 1)), []).append(int(r["t"]))
    triples = 0
    for members in groups.values():
        uniq = sorted(set(members))
        if len(uniq) < 3:
            continue
        for anchor in uniq:
            cluster = [t for t in uniq
                       if 0 <= t - anchor <= _RESTAMP_WINDOW_MS
                       and (t - anchor) % _HOUR_MS == 0]
            if len(cluster) >= 3:
                triples += 1
                break
    return {"exact_t_dups": exact, "tight_triples": triples}


def _period_stats(nets: list[float]) -> dict[str, Any]:
    """Summary stats over a year's net-point list. Mirrors the 16Y baseline so
    the front-end aggregates live + backtest years with one code path. Gross
    win/loss are exposed because PF is not additive across years."""
    n = len(nets)
    wins = sum(1 for p in nets if p > 0.0)
    cum = sum(nets)
    gross_win = sum(p for p in nets if p > 0.0)
    gross_loss = abs(sum(p for p in nets if p < 0.0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win > 0 else 0.0)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "profit_factor": (None if pf is None else round(pf, 4)),
        "cum_pts": round(cum, 1),
        "gross_win": round(gross_win, 1),
        "gross_loss": round(gross_loss, 1),
    }


def _hourly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """24 JST-hour buckets ``[{hour, n, wins}]`` over *rows* (each ``{t, p}``).

    Mirrors the 16Y baseline's ``hourly_winrate`` shape so the dashboard can sum
    baseline + live per hour into one time-of-day win-rate heatmap. ``win`` is a
    net-positive trade (``p > 0``); ``t`` is true-UTC ms, bucketed by JST hour."""
    buckets = [{"hour": h, "n": 0, "wins": 0} for h in range(24)]
    for r in rows:
        hour = int(pd.Timestamp(r["t"], unit="ms", tz="UTC").tz_convert(_JST).hour)
        b = buckets[hour]
        b["n"] += 1
        if r["p"] > 0.0:
            b["wins"] += 1
    return buckets


def _by_month(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-JST-month aggregate ``{"1".."12": stats}`` for one year's rows.

    Only months that actually have trades appear (the front end fills the rest of
    the 12-column calendar as empty). Each month carries the same summary shape as
    a year so the monthly-returns calendar and its drill-down read one code path."""
    buckets: dict[int, list[float]] = {}
    for r in rows:
        month = int(pd.Timestamp(r["t"], unit="ms", tz="UTC").tz_convert(_JST).month)
        buckets.setdefault(month, []).append(r["p"])
    return {str(m): _period_stats(nets) for m, nets in sorted(buckets.items())}


def load_by_year(server: str | None, symbol: str, tf: str) -> dict[str, Any]:
    """Read the store and bucket it into ``{by_year: {YYYY(JST): {stats,
    trades:[last 30 newest-first]}}}`` — the same shape the 16Y baseline ships,
    so the front-end renders live years identically. Empty if no store yet."""
    path = store_path(server, symbol, tf)
    if not path.exists():
        return {"by_year": {}}
    with _lock_for(path):
        st = path.stat()
        sig = (st.st_size, st.st_mtime_ns)
        cached = _by_year_cache.get(path)
        if cached is not None and cached[0] == sig:
            return copy.deepcopy(cached[1])     # unchanged file → skip re-parse

        recs: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    recs.append({"t": int(rec["t"]), "d": int(rec["d"]),
                                 "p": float(rec["p"])})
                except (ValueError, KeyError, TypeError):
                    continue

        # Tripwire: surface re-stamp corruption LOUDLY rather than let it rot the
        # store silently. (It must stay zero now the offset is DST-deterministic.)
        flags = scan_corruption(recs)
        if flags["exact_t_dups"] or flags["tight_triples"]:
            log.error(
                "trigger store CORRUPTION in %s: %s — run scripts/_regen_trigger_store.py",
                path, flags,
            )

        by_year_rows: dict[int, list[dict[str, Any]]] = {}
        for rec in recs:
            # JST-year bucketing — matches the front-end and the CSV baseline.
            year = int(pd.Timestamp(rec["t"], unit="ms", tz="UTC")
                       .tz_convert(_JST).year)
            by_year_rows.setdefault(year, []).append(rec)

        by_year: dict[str, dict[str, Any]] = {}
        for year, rows in by_year_rows.items():
            ordered = sorted(rows, key=lambda r: r["t"], reverse=True)
            trades = [{"t": r["t"], "d": r["d"], "p": round(r["p"], 1)}
                      for r in ordered[:_TRIGGER_LIST_CAP]]
            by_year[str(year)] = {**_period_stats([r["p"] for r in rows]),
                                  "trades": trades,
                                  # Per-year 24-hour breakdown so the dashboard
                                  # merges live hours into the 16Y heatmap.
                                  "hourly": _hourly(rows),
                                  # Per-month aggregate so the dashboard renders a
                                  # monthly-returns calendar over the full record.
                                  "months": _by_month(rows)}
        result = {"by_year": by_year}
        _by_year_cache[path] = (sig, result)
        return copy.deepcopy(result)
