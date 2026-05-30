"""Ops integrity scan over EVERY live-trigger store file (all brokers, symbols,
timeframes). Reports the server-offset re-stamp fingerprint via
``trigger_store.scan_corruption`` — the same check the live load path and the
regression test use. Exit code 1 if any corruption is found. ASCII-only.

Run::

    "C:/.../python.exe" scripts/_scan_trigger_store.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                  # noqa: E402
from analyzer import trigger_store as ts        # noqa: E402


def _load(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            out.append({"t": int(r["t"]), "d": int(r["d"]), "p": float(r["p"])})
        except (ValueError, KeyError, TypeError):
            continue
    return out


def main() -> int:
    root = config.LIVE_TRIGGER_DIR
    if not root.exists():
        print(f"no store at {root}")
        return 0
    files = sorted(root.rglob("*.jsonl"))
    dirty = 0
    total_rows = 0
    for path in files:
        recs = _load(path)
        total_rows += len(recs)
        flags = ts.scan_corruption(recs)
        bad = flags["exact_t_dups"] or flags["tight_triples"]
        if bad:
            dirty += 1
            print(f"  CORRUPT {path.relative_to(root)}: {flags} rows={len(recs)}")
    print(f"scanned files={len(files)} rows={total_rows} corrupt_files={dirty}")
    print("VERDICT:", "CLEAN" if dirty == 0 else "CORRUPTION PRESENT")
    return 1 if dirty else 0


if __name__ == "__main__":
    raise SystemExit(main())
