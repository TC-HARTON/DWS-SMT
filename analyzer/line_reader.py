"""Read TL/SR JSON files emitted by ``mql5_ea/LineExporter.mq5``.

The EA writes one ``lines_{SYMBOL}.json`` per chart symbol to MT5's
``Common\\Files`` directory (SPEC §9.1 / Phase 3 user choice "(A) Common
共有"). We watch that directory with :mod:`watchdog`, debounce the
modify-then-create event burst Windows tends to emit, and convert each
JSON payload into a list of :class:`StructureLevel` objects published into
shared :class:`LinesState`.

Why a dedicated state object
----------------------------
The Phase 1 :class:`LatestState` is the single source of truth for the
WebSocket broadcaster, but it is updated only by the analysis loop. The
line reader runs on its own thread (watchdog's observer is a daemon
thread), so we keep its state in a separate small holder and have the
analysis loop *poll* it once per 5 s cycle. That keeps the lock
ownership graph one-directional: writers → LatestState → readers.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

import config
from analyzer.structure_types import (
    LevelCategory,
    LevelKind,
    LevelSource,
    StructureLevel,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Name → category / importance / TF heuristics (SPEC §9.3)
# --------------------------------------------------------------------------- #

_RE_PREFIX = re.compile(
    r"^(?P<head>R\d*|S\d*|TL_up|TL_dn|zone_supply|zone_demand)",
    re.IGNORECASE,
)
# Underscore counts as a word character in regex so plain ``\b`` would not
# fire between "strong" and "_D1"; we use an explicit ``_|$`` look-ahead.
_RE_TF = re.compile(r"_(M1|M5|M15|M30|H1|H4|D1|W1|MN1)(?=_|$)", re.IGNORECASE)
_RE_IMPORTANCE = re.compile(r"_(strong|major|weak)(?=_|$)", re.IGNORECASE)


def _classify(name: str, kind: LevelKind) -> tuple[LevelCategory, int, str | None]:
    """Derive UI category, importance, and originating TF from an EA name.

    Defaults that fall through when the user did not follow SPEC §9.3:
      * unknown horizontal / fibo / rectangle → ``other`` / importance 1
      * importance defaults to 1 (weak)
    """
    head_match = _RE_PREFIX.match(name)
    head = head_match.group("head").upper() if head_match else None

    category: LevelCategory
    if head and head.startswith("R") and (len(head) == 1 or head[1:].isdigit()):
        category = "resistance"
    elif head and head.startswith("S") and (len(head) == 1 or head[1:].isdigit()):
        category = "support"
    elif head == "TL_UP":
        category = "trend_up"
    elif head == "TL_DN":
        category = "trend_down"
    elif head == "ZONE_SUPPLY":
        category = "supply_zone"
    elif head == "ZONE_DEMAND":
        category = "demand_zone"
    else:
        # Fall back to the kind itself for fibonacci / channel / text. An
        # unknown rectangle is *not* a supply zone — SPEC §9.3 reserves that
        # label for explicitly-named zone_supply_* objects.
        category = {
            LevelKind.FIBONACCI: "fibonacci",
            LevelKind.CHANNEL_MAIN: "channel",
            LevelKind.CHANNEL_PARALLEL: "channel",
            LevelKind.TEXT_NOTE: "note",
        }.get(kind, "other")

    importance = 1
    imp_match = _RE_IMPORTANCE.search(name)
    if imp_match:
        word = imp_match.group(1).lower()
        importance = {"weak": 1, "major": 2, "strong": 3}[word]

    tf_match = _RE_TF.search(name)
    tf = tf_match.group(1).upper() if tf_match else None
    return category, importance, tf


# --------------------------------------------------------------------------- #
# JSON → StructureLevel converter
# --------------------------------------------------------------------------- #

def parse_lines_payload(payload: dict) -> list[StructureLevel]:
    """Convert one parsed JSON document into a list of structure levels.

    Schema follows SPEC §9.2 / what ``LineExporter.mq5`` emits. Unknown or
    malformed entries are skipped with a warning rather than raising, so
    one bad object cannot starve the dashboard.
    """
    symbol = payload.get("symbol", "")
    if not symbol:
        log.warning("line_reader: missing 'symbol' in payload, dropping")
        return []
    lines = payload.get("lines") or {}
    out: list[StructureLevel] = []

    for raw in lines.get("horizontal", []) or []:
        try:
            level = _make_horizontal(symbol, raw)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("line_reader: bad horizontal entry %r: %s", raw.get("name"), exc)
            continue
        out.append(level)

    for raw in lines.get("trendlines", []) or []:
        try:
            level = _make_trendline(symbol, raw)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("line_reader: bad trendline %r: %s", raw.get("name"), exc)
            continue
        out.append(level)

    for raw in lines.get("rectangles", []) or []:
        try:
            for level in _make_rectangle(symbol, raw):
                out.append(level)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("line_reader: bad rectangle %r: %s", raw.get("name"), exc)
            continue

    for raw in lines.get("channels", []) or []:
        try:
            for level in _make_channel(symbol, raw):
                out.append(level)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("line_reader: bad channel %r: %s", raw.get("name"), exc)
            continue

    for raw in lines.get("fibonacci", []) or []:
        try:
            for level in _make_fibonacci(symbol, raw):
                out.append(level)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("line_reader: bad fibonacci %r: %s", raw.get("name"), exc)
            continue

    for raw in lines.get("texts", []) or []:
        try:
            level = _make_text(symbol, raw)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("line_reader: bad text %r: %s", raw.get("name"), exc)
            continue
        out.append(level)

    return out


def _make_horizontal(symbol: str, raw: dict) -> StructureLevel:
    name = str(raw["name"])
    price = float(raw["price"])
    category, importance, tf = _classify(name, LevelKind.HORIZONTAL)
    return StructureLevel(
        symbol=symbol, name=name, kind=LevelKind.HORIZONTAL,
        category=category, source=LevelSource.EA_USER, price=price,
        importance=importance, color=raw.get("color"), tf=tf,
    )


def _make_trendline(symbol: str, raw: dict) -> StructureLevel:
    name = str(raw["name"])
    price = float(raw["current_value"])
    category, importance, tf = _classify(name, LevelKind.TREND_LINE)
    meta = {
        "point1": raw.get("point1"),
        "point2": raw.get("point2"),
        "ray_right": bool(raw.get("ray_right", False)),
        "slope_per_day": float(raw.get("slope_per_day", 0.0)),
    }
    return StructureLevel(
        symbol=symbol, name=name, kind=LevelKind.TREND_LINE,
        category=category, source=LevelSource.EA_USER, price=price,
        importance=importance, color=raw.get("color"), tf=tf, meta=meta,
    )


def _make_rectangle(symbol: str, raw: dict) -> list[StructureLevel]:
    """Split a rectangle into its two horizontal edges so the UI can render
    distance-to-touch the same way as a regular horizontal level."""
    name = str(raw["name"])
    high = float(raw["price_high"])
    low = float(raw["price_low"])
    category, importance, tf = _classify(name, LevelKind.RECTANGLE)
    meta = {"price_high": high, "price_low": low,
            "time1": raw.get("time1"), "time2": raw.get("time2")}
    color = raw.get("color")
    return [
        StructureLevel(
            symbol=symbol, name=f"{name}/top", kind=LevelKind.RECTANGLE,
            category=category, source=LevelSource.EA_USER, price=high,
            importance=importance, color=color, tf=tf, meta=meta,
        ),
        StructureLevel(
            symbol=symbol, name=f"{name}/bot", kind=LevelKind.RECTANGLE,
            category=category, source=LevelSource.EA_USER, price=low,
            importance=importance, color=color, tf=tf, meta=meta,
        ),
    ]


def _make_channel(symbol: str, raw: dict) -> list[StructureLevel]:
    name = str(raw["name"])
    main = float(raw["main_value"])
    parallel = float(raw["parallel_value"])
    category, importance, tf = _classify(name, LevelKind.CHANNEL_MAIN)
    meta = {
        "main_point1": raw.get("main_point1"),
        "main_point2": raw.get("main_point2"),
        "parallel_anchor": raw.get("parallel_anchor"),
        "ray_right": bool(raw.get("ray_right", False)),
    }
    color = raw.get("color")
    return [
        StructureLevel(
            symbol=symbol, name=f"{name}/main", kind=LevelKind.CHANNEL_MAIN,
            category=category, source=LevelSource.EA_USER, price=main,
            importance=importance, color=color, tf=tf, meta=meta,
        ),
        StructureLevel(
            symbol=symbol, name=f"{name}/parallel", kind=LevelKind.CHANNEL_PARALLEL,
            category=category, source=LevelSource.EA_USER, price=parallel,
            importance=importance, color=color, tf=tf, meta=meta,
        ),
    ]


def _make_fibonacci(symbol: str, raw: dict) -> list[StructureLevel]:
    name = str(raw["name"])
    category, importance, tf = _classify(name, LevelKind.FIBONACCI)
    color = raw.get("color")
    out: list[StructureLevel] = []
    for lvl in raw.get("levels", []):
        price = float(lvl["price"])
        ratio = float(lvl["ratio"])
        label = str(lvl.get("label") or "")
        out.append(StructureLevel(
            symbol=symbol, name=f"{name}/{ratio:.3f}", kind=LevelKind.FIBONACCI,
            category=category, source=LevelSource.EA_USER, price=price,
            importance=importance, color=color, tf=tf,
            meta={"ratio": ratio, "label": label},
        ))
    return out


def _make_text(symbol: str, raw: dict) -> StructureLevel:
    name = str(raw["name"])
    price = float(raw["price"])
    text = str(raw.get("text") or "")
    category, importance, tf = _classify(name, LevelKind.TEXT_NOTE)
    return StructureLevel(
        symbol=symbol, name=name, kind=LevelKind.TEXT_NOTE,
        category=category, source=LevelSource.EA_USER, price=price,
        importance=importance, color=raw.get("color"), tf=tf,
        meta={"text": text, "time": raw.get("time")},
    )


# --------------------------------------------------------------------------- #
# Lines state: symbol → list[StructureLevel] + last-updated timestamp
# --------------------------------------------------------------------------- #

@dataclass
class LinesState:
    """Thread-safe holder for the latest EA-sourced structure levels.

    Producer: :class:`LinesWatcher` (watchdog observer thread).
    Consumer: the analysis loop, which pulls the per-symbol levels each
    5 s cycle and folds them into the snapshot the WS broadcaster ships.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _by_symbol: dict[str, list[StructureLevel]] = field(default_factory=dict)
    _updated_at: dict[str, float] = field(default_factory=dict)

    def update_symbol(self, symbol: str, levels: list[StructureLevel]) -> None:
        with self._lock:
            self._by_symbol[symbol] = list(levels)
            self._updated_at[symbol] = time.time()

    def levels_for(self, symbol: str) -> list[StructureLevel]:
        with self._lock:
            return list(self._by_symbol.get(symbol, ()))

    def snapshot(self) -> dict[str, list[StructureLevel]]:
        with self._lock:
            return {s: list(v) for s, v in self._by_symbol.items()}

    def updated_at(self, symbol: str) -> float | None:
        with self._lock:
            return self._updated_at.get(symbol)


# Module-level singleton — kept symmetric with analyzer.state.STATE.
LINES: LinesState = LinesState()


# --------------------------------------------------------------------------- #
# Watchdog observer
# --------------------------------------------------------------------------- #

class _Handler(FileSystemEventHandler):
    """Debounces FS events per-path and delegates to the owning watcher."""

    def __init__(self, watcher: "LinesWatcher") -> None:
        super().__init__()
        self._watcher = watcher
        self._last_event: dict[str, float] = {}
        self._lock = threading.Lock()

    def _maybe_dispatch(self, path: str) -> None:
        # Coalesce burst events (Windows often emits CREATED + MODIFIED
        # within ~50 ms when an EA rewrites a file).
        now = time.time()
        with self._lock:
            last = self._last_event.get(path, 0.0)
            if now - last < config.LINES_DEBOUNCE_SEC:
                return
            self._last_event[path] = now
        self._watcher.reload_file(Path(path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_dispatch(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_dispatch(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Treat deletion as "the user removed the chart / EA wiped the file".
        # Publish an empty list so the UI clears immediately.
        path = Path(event.src_path)
        symbol = self._watcher.extract_symbol(path)
        if symbol:
            self._watcher.state.update_symbol(symbol, [])

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_dispatch(event.dest_path)


class LinesWatcher:
    """Watch :data:`config.LINES_DIR` for ``lines_*.json`` updates."""

    def __init__(
        self,
        directory: Path = config.LINES_DIR,
        state: LinesState = LINES,
        prefix: str = config.LINES_FILE_PREFIX,
        suffix: str = config.LINES_FILE_SUFFIX,
    ) -> None:
        self.directory = Path(directory)
        self.state = state
        self.prefix = prefix
        self.suffix = suffix
        self._observer: Observer | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.reload_existing()
        observer = Observer()
        observer.schedule(_Handler(self), str(self.directory), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        log.info("LinesWatcher started on %s", self.directory)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3.0)
            self._observer = None
            log.info("LinesWatcher stopped")

    # ----------------------------------------------------------- bootstrap
    def reload_existing(self) -> None:
        """Read every ``lines_*.json`` already present at startup."""
        if not self.directory.exists():
            return
        for p in sorted(self.directory.glob(f"{self.prefix}*{self.suffix}")):
            self.reload_file(p)

    # ----------------------------------------------------------- file load
    def extract_symbol(self, path: Path) -> str | None:
        name = path.name
        if not (name.startswith(self.prefix) and name.endswith(self.suffix)):
            return None
        return name[len(self.prefix): -len(self.suffix)]

    def reload_file(self, path: Path) -> None:
        symbol = self.extract_symbol(path)
        if symbol is None:
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                raw = f.read()
            if not raw.strip():
                # EA is mid-write; skip and let the next event re-trigger.
                return
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("line_reader: failed to read %s: %s", path.name, exc)
            return

        # Defence in depth: trust nobody, including ourselves.
        if not isinstance(payload, dict):
            log.warning("line_reader: %s root is %s, expected object",
                        path.name, type(payload).__name__)
            return
        if payload.get("symbol") and payload["symbol"] != symbol:
            log.warning(
                "line_reader: %s symbol field %r differs from filename %r — using filename",
                path.name, payload["symbol"], symbol,
            )
            payload = {**payload, "symbol": symbol}

        levels = parse_lines_payload(payload)
        self.state.update_symbol(symbol, levels)
        log.debug("line_reader: %s → %d levels", path.name, len(levels))

    # ----------------------------------------------------------- context manager
    def __enter__(self) -> "LinesWatcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
