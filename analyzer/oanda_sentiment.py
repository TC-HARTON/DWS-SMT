"""OANDA-client positioning sentiment layer (precision-review item ②).

For each tracked pair we fetch ``/v3/instruments/{INST}/positionBook`` from
the OANDA REST API (free practice tier) and reduce the price-bucket
distribution to three numbers:

* ``long_avg`` — weighted-average price of OANDA clients' long positions
* ``short_avg`` — weighted-average price of their short positions
* ``market`` — current market price OANDA stamped the book at

From those we derive a single per-pair bias label:

* ``long_squeeze`` — longs in loss + shorts in profit → potential further
  downside as longs unwind
* ``short_squeeze`` — longs in profit + shorts in loss → potential upward
  acceleration as shorts cover
* ``neutral`` — both sides have similar P/L footing

The whole module is pure / side-effect free except :meth:`SentimentEngine.compute`,
which hits OANDA over HTTPS once per pair on its own off-thread cadence
(:data:`config.OANDA_SENTIMENT_REFRESH_SEC`).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PairSentiment:
    """Per-pair sentiment derived from OANDA's positionBook."""

    symbol: str            # dashboard base name, e.g. "USDJPY"
    market_price: float    # OANDA-quoted current price at snapshot time
    long_avg: float        # weighted-mean price of long positions
    short_avg: float       # weighted-mean price of short positions
    long_pnl: float        # market_price - long_avg (positive = longs in profit)
    short_pnl: float       # short_avg - market_price (positive = shorts in profit)
    bias: str              # "short_squeeze" | "long_squeeze" | "neutral"


@dataclass(frozen=True)
class SentimentSnapshot:
    """One full sentiment pass across every supported pair."""

    generated_at: float            # epoch seconds
    fetched_at: float
    by_symbol: dict[str, PairSentiment]
    last_error: str | None
    consecutive_failures: int


def _weighted_mean(buckets: list[dict], pct_key: str) -> float | None:
    """Return ``sum(price * pct_key) / sum(pct_key)`` over the buckets.

    Returns ``None`` when every bucket has 0 % on the requested side
    (no positions on that side at all — very rare for liquid pairs).
    """
    num = 0.0
    den = 0.0
    for b in buckets:
        try:
            price = float(b["price"])
            pct = float(b[pct_key])
        except (KeyError, TypeError, ValueError):
            continue
        num += price * pct
        den += pct
    return (num / den) if den > 0.0 else None


def parse_position_book(body: str, symbol: str) -> PairSentiment | None:
    """Reduce an OANDA positionBook JSON payload to a :class:`PairSentiment`.

    Returns ``None`` when the payload is unusable (missing buckets, no
    positions on either side, malformed JSON).
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    pb = data.get("positionBook") or {}
    buckets = pb.get("buckets") or []
    if not buckets:
        return None
    try:
        market = float(pb["price"])
    except (KeyError, TypeError, ValueError):
        return None
    long_avg = _weighted_mean(buckets, "longCountPercent")
    short_avg = _weighted_mean(buckets, "shortCountPercent")
    if long_avg is None or short_avg is None:
        return None
    long_pnl = market - long_avg
    short_pnl = short_avg - market
    # Bias: when one side is clearly winning AND the other clearly losing
    # (both in opposite signs), name the squeeze direction. Otherwise neutral.
    if long_pnl > 0 and short_pnl < 0:
        bias = "short_squeeze"     # longs winning → shorts must cover
    elif long_pnl < 0 and short_pnl > 0:
        bias = "long_squeeze"      # shorts winning → longs forced to unwind
    else:
        bias = "neutral"
    return PairSentiment(
        symbol=symbol, market_price=market,
        long_avg=long_avg, short_avg=short_avg,
        long_pnl=long_pnl, short_pnl=short_pnl, bias=bias,
    )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class SentimentEngine:
    """Fetch + parse OANDA positionBook for every tracked pair.

    Only needs ``OANDA_API_TOKEN`` set in the environment / .env. Without
    a token, :meth:`compute` short-circuits and returns an empty
    ``SentimentSnapshot`` with the token-missing notice so the UI can
    display "OANDA: トークン未設定" instead of a noisy crash loop.
    """

    def __init__(
        self,
        *,
        api_token: str = config.OANDA_API_TOKEN,
        api_host: str = config.OANDA_API_HOST,
        timeout_sec: float = config.OANDA_HTTP_TIMEOUT_SEC,
        symbol_map: dict[str, str] = config.OANDA_SYMBOL_MAP,
    ) -> None:
        self._token = api_token
        self._host = api_host.rstrip("/")
        self._timeout = timeout_sec
        self._symbols = dict(symbol_map)
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ #
    # HTTP fetch (mockable in tests)
    # ------------------------------------------------------------------ #
    def _http_get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            # An explicit version pin keeps the parse contract stable.
            "Accept-Datetime-Format": "RFC3339",
        })
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _fetch_one(self, symbol: str) -> PairSentiment | None:
        """Fetch and parse one symbol; returns ``None`` on transport error."""
        instrument = self._symbols.get(symbol)
        if instrument is None:
            return None
        url = (f"{self._host}/v3/instruments/{instrument}"
               f"/positionBook?time=latest")
        try:
            body = self._http_get(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            log.warning("oanda: %s positionBook fetch failed: %s", symbol, exc)
            return None
        return parse_position_book(body, symbol)

    # ------------------------------------------------------------------ #
    # Public entry — runs off-thread on its 4 h schedule
    # ------------------------------------------------------------------ #
    def compute(self) -> SentimentSnapshot:
        t0 = time.time()
        if not self._token:
            return SentimentSnapshot(
                generated_at=t0, fetched_at=t0, by_symbol={},
                last_error="OANDA_API_TOKEN is not set",
                consecutive_failures=0,
            )

        out: dict[str, PairSentiment] = {}
        errors: list[str] = []
        for sym in self._symbols:
            try:
                ps = self._fetch_one(sym)
            except Exception as exc:                # noqa: BLE001 — defensive
                log.exception("oanda: unexpected error for %s", sym)
                errors.append(f"{sym}: {exc}")
                continue
            if ps is not None:
                out[sym] = ps
            else:
                errors.append(f"{sym}: empty/parse")
        # Counter logic mirrors macro_feed: a totally empty result counts as a
        # failure cycle; a partial result resets the counter even with errors.
        if not out:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0
        return SentimentSnapshot(
            generated_at=time.time(), fetched_at=t0,
            by_symbol=out,
            last_error=("; ".join(errors) if errors else None),
            consecutive_failures=self._consecutive_failures,
        )
