"""Macro / rate-differential layer (precision-optimization spec, Section B).

Fetches each tracked currency's central-bank policy rate (USD/EUR/GBP/JPY/AUD)
plus US employment, then derives a per-currency-pair rate differential and a
macro direction. The dashboard uses this to (a) show a reference panel and
(b) flag DWS-SMT triggers that fight the carry.

Honest scope note: policy rates give the structural *carry* direction plus
actual hike/cut events — not the market-implied rate-expectation momentum
(which needs OIS / rate futures, out of scope). The macro direction here is a
carry-alignment signal, useful for catching counter-carry trades, not a
substitute for rate-expectation analysis.

Every fetch is plain HTTP — this module never touches the MT5 connector.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MacroRate:
    """One currency's central-bank policy rate."""

    currency: str
    rate: float
    as_of: str
    prev_rate: float | None
    source: str
    stale: bool


@dataclass(frozen=True)
class MacroEmployment:
    """Latest US employment readings (NFP change + unemployment rate)."""

    nonfarm_change: float | None
    unemployment_rate: float | None
    as_of: str
    prev_nonfarm_change: float | None
    source: str


@dataclass(frozen=True)
class MacroPairBias:
    """Rate differential + macro direction for one currency pair."""

    pair: str
    base_ccy: str
    quote_ccy: str
    differential: float
    macro_dir: int
    label: str


@dataclass(frozen=True)
class MacroSnapshot:
    """One macro refresh: rates, employment, and per-pair bias."""

    generated_at: float
    fetched_at: float
    rates: dict[str, MacroRate]
    employment: MacroEmployment | None
    by_pair: dict[str, MacroPairBias]
    last_error: str | None
    consecutive_failures: int


# --------------------------------------------------------------------------- #
# Per-pair bias
# --------------------------------------------------------------------------- #

def _split_pair(pair: str) -> tuple[str, str]:
    """Split a 6/7-char symbol into (base ccy, quote ccy).

    ``"USDJPY" -> ("USD", "JPY")``; ``"XAUUSD" -> ("XAU", "USD")``.
    """
    return pair[:3], pair[3:]


def pair_macro_bias(pair: str, rates: dict[str, MacroRate]) -> MacroPairBias:
    """Compute the rate differential and macro direction for *pair*.

    For a fiat/fiat pair: ``differential = rate(base) - rate(quote)`` and
    ``macro_dir`` is its sign — the high-yield currency has structural carry
    support. ``XAUUSD`` is special: gold carries no yield, so the macro driver
    is the US rate *trend* — a rising US rate is a headwind for gold
    (``macro_dir = -1``), a falling rate a tailwind (``+1``).

    If either leg's rate is missing or ``stale``, ``macro_dir`` is ``0`` — the
    filter must never penalise a trigger on bad/absent data.
    """
    base_ccy, quote_ccy = _split_pair(pair)

    if base_ccy == "XAU":
        usd = rates.get("USD")
        if usd is None or usd.stale or usd.prev_rate is None:
            return MacroPairBias(pair, base_ccy, quote_ccy, 0.0, 0, "—")
        delta = usd.rate - usd.prev_rate
        macro_dir = -1 if delta > 0 else (1 if delta < 0 else 0)
        label = ("米金利上昇=金に逆風" if macro_dir < 0
                 else "米金利低下=金に追風" if macro_dir > 0 else "—")
        return MacroPairBias(pair, base_ccy, quote_ccy, delta, macro_dir, label)

    base = rates.get(base_ccy)
    quote = rates.get(quote_ccy)
    if base is None or quote is None or base.stale or quote.stale:
        return MacroPairBias(pair, base_ccy, quote_ccy, 0.0, 0, "—")

    differential = base.rate - quote.rate
    macro_dir = 1 if differential > 0 else (-1 if differential < 0 else 0)
    if macro_dir > 0:
        label = f"{base_ccy}金利優位"
    elif macro_dir < 0:
        label = f"{quote_ccy}金利優位"
    else:
        label = "金利差なし"
    return MacroPairBias(pair, base_ccy, quote_ccy, differential, macro_dir, label)
