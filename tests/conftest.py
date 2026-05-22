"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path so `import config`, `import analyzer.*`
# work regardless of the directory pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
