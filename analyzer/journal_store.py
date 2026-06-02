"""Per-broker discretionary-trade journal (append-only JSONL).

Every order the user places through the dashboard is logged here together with
the multi-timeframe market context at entry (per-TF EMA side / ADX / DI), so the
trade can later be reviewed against the conditions it was taken in — "which
setup did I actually enter on?". Keyed by BROKER (MT5 server), like the trigger
store, because the context is broker-price-derived.

Logging never blocks or fails an order: callers wrap ``append`` in try/except and
a write error is swallowed (the order has already been placed).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

JOURNAL_DIR: Path = config.PROJECT_ROOT / "data" / "journal"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_lock = threading.Lock()


def _slug(server: str | None) -> str:
    s = _SLUG_RE.sub("_", (server or "unknown").strip())
    s = s.replace("..", "_")   # never let a name resolve a parent directory
    return s or "unknown"


def store_path(server: str | None) -> Path:
    """Path of the journal JSONL for one broker."""
    return JOURNAL_DIR / _slug(server) / "orders.jsonl"


def append(server: str | None, entry: dict[str, Any]) -> None:
    """Append one journal entry (a JSON object) for *server*."""
    path = store_path(server)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_recent(server: str | None, limit: int = 200) -> list[dict[str, Any]]:
    """Most-recent journal entries for *server*, newest first (``[]`` if none)."""
    path = store_path(server)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with _lock, path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # skip a corrupt line rather than abort
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]
