"""Confluence detection (SPEC §10.4).

Given a list of :class:`StructureLevel` for a symbol and the current H4
ATR, find clusters of ``CONFLUENCE_MIN_ELEMENTS+`` levels whose prices
all sit within ``CONFLUENCE_ATR_MULTIPLE × ATR`` of each other.

A simple sweep-and-merge algorithm is sufficient because the level
count per symbol is small (a few dozen at most). We avoid scoring
schemes for now — the UI just renders ★ + element-count badge and
highlights the strongest cluster nearest to price.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from analyzer.structure_types import StructureLevel


@dataclass(frozen=True)
class ConfluenceCluster:
    """Group of structure levels packed into a single price band."""

    center: float                          # weighted (by importance) mean price
    price_low: float
    price_high: float
    width: float                           # = price_high - price_low
    levels: tuple[StructureLevel, ...]
    score: int                             # Σ importance — higher is stronger
    # The biggest cluster nearest to current price is the one most worth
    # surfacing; the loop also computes the |center - current_price|.
    distance: float = 0.0
    importance_label: str = "★"           # "★", "★★", "★★★" by tier


def detect(
    levels: list[StructureLevel],
    atr_h4: float | None,
    current_price: float | None,
    *,
    atr_multiple: float = config.CONFLUENCE_ATR_MULTIPLE,
    min_elements: int = config.CONFLUENCE_MIN_ELEMENTS,
) -> list[ConfluenceCluster]:
    """Return clusters sorted by descending score, then ascending distance."""
    if not levels or atr_h4 is None or atr_h4 <= 0:
        return []
    band = atr_multiple * atr_h4
    if band <= 0:
        return []

    # Sort by price ascending, then sweep: extend cluster while the next
    # level is within `band` of the cluster *centre* (recomputed at each
    # extension, so multiple ticks can drift the centre but stay packed).
    sorted_levels = sorted(levels, key=lambda lv: lv.price)
    clusters: list[list[StructureLevel]] = []
    cur: list[StructureLevel] = [sorted_levels[0]]
    for lv in sorted_levels[1:]:
        cur_center = sum(x.price for x in cur) / len(cur)
        # Maintain the invariant that every member is within `band` of the
        # cluster centre. If extending would breach this, start a new cluster.
        # Also enforce: lv.price - first.price <= band (overall width cap).
        if (lv.price - cur[0].price) <= band and abs(lv.price - cur_center) <= band:
            cur.append(lv)
        else:
            clusters.append(cur)
            cur = [lv]
    clusters.append(cur)

    out: list[ConfluenceCluster] = []
    for grp in clusters:
        if len(grp) < min_elements:
            continue
        score = sum(lv.importance for lv in grp)
        # Weighted centre by importance so a single "strong" line tugs the
        # centre toward itself rather than being diluted by many weak ones.
        total_w = sum(lv.importance for lv in grp)
        center = sum(lv.price * lv.importance for lv in grp) / total_w
        lo = min(lv.price for lv in grp)
        hi = max(lv.price for lv in grp)
        distance = abs(center - current_price) if current_price is not None else 0.0
        # SPEC §10.4 importance bands: 3 elements weak, 4 medium, 5+ strong.
        n = len(grp)
        label = "★" if n < 4 else ("★★" if n < 5 else "★★★")
        out.append(ConfluenceCluster(
            center=center, price_low=lo, price_high=hi, width=hi - lo,
            levels=tuple(grp), score=score, distance=distance,
            importance_label=label,
        ))

    # Highest score first; ties broken by closer-to-price first.
    out.sort(key=lambda c: (-c.score, c.distance))
    return out
