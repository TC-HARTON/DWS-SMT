"""Global configuration values for the MT5-Python Trading Dashboard.

All tunable constants live here per SPEC 23.2 ("設定値は config.py に集約").
Values that depend on the user environment (MT5 path, credentials, ports)
are loaded from a .env file via python-dotenv; everything else is a literal
constant traceable back to a specific SPEC section.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Load .env from project root if present (production deployments may use real env vars instead).
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# MT5 connection (SPEC 9, 14, 18)
# --------------------------------------------------------------------------- #

MT5_TERMINAL_PATH: Final[str] = _get_env(
    "MT5_TERMINAL_PATH",
    r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe",
)

# Known MT5 broker presets exposed by the in-app broker switcher (the
# ACCOUNT badge dropdown). Order = display order. The lite_server's
# /api/broker endpoint validates the user's pick against this map
# so a malicious WS client can't write an arbitrary path into .env.
BROKER_PRESETS: Final[dict[str, str]] = {
    "Exness":     r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe",
    "IC Markets": r"C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe",
}
MT5_LOGIN: Final[str] = _get_env("MT5_LOGIN", "")  # empty → use saved login
MT5_PASSWORD: Final[str] = _get_env("MT5_PASSWORD", "")
MT5_SERVER: Final[str] = _get_env("MT5_SERVER", "")
MT5_TIMEOUT_MS: Final[int] = _get_env_int("MT5_TIMEOUT_MS", 10_000)
MT5_RECONNECT_INTERVAL_SEC: Final[float] = 5.0  # SPEC 18.4 "Python側で5秒間隔再接続試行"


# --------------------------------------------------------------------------- #
# Symbols (SPEC 7)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SymbolSpec:
    """One symbol displayed on the dashboard."""

    base: str                         # broker-independent base name (e.g. "XAUUSD")
    display_size: str                 # "xl" | "md" | "sm" — drives panel size (SPEC 7.2)
    round_step: float                 # round-number grid (SPEC 10.2)

    def __post_init__(self) -> None:
        if self.display_size not in {"xl", "md", "sm"}:
            raise ValueError(f"display_size must be xl|md|sm, got {self.display_size!r}")


# XAUUSD-specialised dashboard: gold only. The whole indicator pipeline iterates
# this tuple, so reducing it to XAUUSD alone confines every per-symbol
# computation to gold (the other majors and their panels are gone). DXY is
# tracked separately as dollar context.
SYMBOLS: Final[tuple[SymbolSpec, ...]] = (
    SymbolSpec("XAUUSD", "xl", 50.0),       # SPEC 10.2: $50
)


# --------------------------------------------------------------------------- #
# Timeframes & indicators (SPEC 5, 6)
# --------------------------------------------------------------------------- #

import MetaTrader5 as mt5  # noqa: E402  — import after env load is intentional

@dataclass(frozen=True)
class TimeframeSpec:
    label: str
    mt5_const: int
    ema_period: int       # SPEC 6.1
    bars_to_fetch: int    # how many bars we pull each refresh


# 全TF統一トレンドEMA(②, 2026-06-06)。SPEC 5/6.1 の TF別期間(D1=200/H4=50/H1=20/
# M15=13)から意図的に逸脱し、中央オシレーター(ema_stack: 全て EMA20 基準)と物差しを
# 揃える。bars_to_fetch は EMA20 + ADX/RSI/ATR(14) + 履歴に十分なため据え置き。
TREND_EMA_PERIOD: Final[int] = 20
TIMEFRAMES: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("D1",  mt5.TIMEFRAME_D1,  TREND_EMA_PERIOD, 400),
    TimeframeSpec("H4",  mt5.TIMEFRAME_H4,  TREND_EMA_PERIOD, 300),
    TimeframeSpec("H1",  mt5.TIMEFRAME_H1,  TREND_EMA_PERIOD, 240),
    TimeframeSpec("M15", mt5.TIMEFRAME_M15, TREND_EMA_PERIOD, 200),
)

TIMEFRAME_BY_LABEL: Final[dict[str, TimeframeSpec]] = {tf.label: tf for tf in TIMEFRAMES}

# SPEC §6.2 ADX(14)
ADX_PERIOD: Final[int] = 14

# SPEC §6.3 RSI(14)
RSI_PERIOD: Final[int] = 14

# SPEC 6.4 ATR(14) Wilder
ATR_PERIOD: Final[int] = 14


# --------------------------------------------------------------------------- #
# DXY (US Dollar Index) — dollar context for gold
# --------------------------------------------------------------------------- #
# US Dollar Index — dollar context for gold (gold is inverse-USD). Brokers
# expose the index in one of two shapes, resolved at startup under base "DXY"
# (see MT5Connector.resolve_dxy), continuous-first:
#   1. a CONTINUOUS spot/CFD index — e.g. TitanFX "USDX" (path Indices\USDX),
#      no expiry / no roll. Matched by exact name against DXY_INDEX_SYMBOLS.
#   2. QUARTERLY index futures — e.g. IC Markets "DXY_M6"/"DXY_U6"; the active
#      front-month is auto-rolled via the futures month-code (DXY_SYMBOL_PREFIX).
# Display-only context, not a tradeable symbol.
# Continuous index names tried first, in priority order (exact, case-insensitive).
DXY_INDEX_SYMBOLS: Final[tuple[str, ...]] = ("USDX", "DXY", "USDIDX", "USDOLLAR")
DXY_SYMBOL_PREFIX: Final[str] = "DXY"   # quarterly-futures prefix: DXY_M6, DXY_U6, ...
DXY_CHART_TF: Final[str] = "H1"          # timeframe for the trend/sparkline
DXY_CHART_BARS: Final[int] = 120         # bars to fetch (sparkline + change)
DXY_EMA_PERIOD: Final[int] = 20          # trend EMA on DXY closes


# --------------------------------------------------------------------------- #
# EMA-stack oscillator (single-series, repaint-free) — center panel
# --------------------------------------------------------------------------- #
# Three EMAs on the M15 CLOSED-bar series: EMA20 (M15), EMA80 (~1H EMA20),
# EMA320 (~4H EMA20). EMA320 is the trend centerline; price / EMA80 / EMA20 are
# shown as % deviation from it — an RSI-style oscillator around a flat EMA320
# center (above = uptrend, below = downtrend; the read is the user's, NO trigger
# is computed). Causal EMA on confirmed bars only, NO multi-TF mapping → there
# is structurally no place for look-ahead / repaint.
EMA_STACK_TF: Final[str] = "M15"
EMA_STACK_PERIODS: Final[tuple[int, int, int]] = (20, 80, 320)  # fast, mid, center
EMA_STACK_FETCH_BARS: Final[int] = 1500    # deep enough for EMA320 to fully settle
EMA_STACK_DISPLAY_BARS: Final[int] = 480   # trailing bars on the LIVE WS snapshot
                                           # (~5 days M15) — kept small so the 2 Hz/5 s
                                           # WS stays light; the deep history comes from
                                           # the /api/ema_history endpoint below.
# Deep history for the oscillator's drag-to-the-past. Served on demand via
# /api/ema_history (fetched once on load + polled every few minutes) so the full
# multi-month series never bloats the live WS snapshot. ~20k M15 bars ≈ 10 months
# is the broker's practical depth.
EMA_STACK_HISTORY_FETCH_BARS: Final[int] = 20000
EMA_STACK_HISTORY_BARS: Final[int] = 20000
EMA_STACK_HISTORY_REFRESH_SEC: Final[float] = 120.0   # frontend poll cadence


# --------------------------------------------------------------------------- #
# Position sizing — recommended lot (fixed-fractional "lot ladder")
# --------------------------------------------------------------------------- #
# Add LOT_BASE lots for every LOT_EQUITY_STEP of account equity, floored to the
# 0.01 lot grid and capped at LOT_MAX. Validated on the 16-year XAUUSD M15
# backtest (start 0.01 lot @ 100k JPY): grows size with the account while
# keeping peak drawdown small (~6%). Single source of truth for the dashboard's
# 推奨ロット readout — and for a future auto-trade EA.
LOT_BASE: Final[float] = 0.01              # lot increment per equity step
LOT_EQUITY_STEP: Final[float] = 100_000.0  # account-currency equity per +LOT_BASE
LOT_MIN: Final[float] = 0.01               # broker minimum / floor
LOT_MAX: Final[float] = 10.0               # ceiling (liquidity / risk cap)


# --------------------------------------------------------------------------- #
# Pips display — convert net "points" to PIPS
# --------------------------------------------------------------------------- #
# The dashboard shows P/L in PIPS, a broker-independent unit. Conversion:
#     pips = net_pts * (broker_point / PIP_PRICE)
# where the live feed uses the broker's own _Point (e.g. IC gold = 0.01).
# PIP_PRICE is the market pip in PRICE units and MUST match pip_size_for():
#   gold $0.10, JPY pairs 0.01, FX majors 0.0001 — regardless of broker digits.
PIP_PRICE: Final[dict[str, float]] = {
    "XAUUSD": 0.10,                                   # $1.00 = 10 pips
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "AUDJPY": 0.01,
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDUSD": 0.0001,
}


# --------------------------------------------------------------------------- #
# Discretionary order panel (manual market orders from the dashboard)
# --------------------------------------------------------------------------- #
# Master kill-switch + guards for the in-dashboard BUY/SELL/close buttons. Real
# orders still require the account's + terminal's trade permission at runtime;
# these are the *additional* app-side safety rails. Discretionary only — NOT an
# auto-trading EA. ORDER_MAX_LOT is a hard ceiling the server clamps every order
# to, independent of what the UI sends.
TRADING_ENABLED: Final[bool] = True                   # app-side master switch
ORDER_MAGIC: Final[int] = 770115                      # tags dashboard-placed orders
ORDER_DEVIATION_POINTS: Final[int] = 20               # max slippage (points) for market fills
ORDER_MAX_LOT: Final[float] = LOT_MAX                 # hard per-order lot ceiling (10.0)


# --------------------------------------------------------------------------- #
# Broker server-clock timezones (DST handling)
# --------------------------------------------------------------------------- #
# Broker server clocks that observe DST. MT5 stamps bars in the broker's SERVER
# wall-clock; a DST-observing server (e.g. IC Markets = Europe/Bucharest, EET in
# winter / EEST in summer) shifts by an hour across the year. A single detected
# whole-hour offset is correct only for the CURRENT season, so the deep-history
# fetch would stamp off-season bars an hour wrong. For these servers we localize
# the raw server time in the named IANA zone (DST-correct per bar) instead of
# subtracting one flat offset. Servers NOT listed here (e.g. Exness, which runs
# a fixed offset) keep the flat-offset path. Key by the MT5 server name as
# reported by ``account_info().server``.
BROKER_TZ_BY_SERVER: Final[dict[str, str]] = {
    "ICMarketsSC-MT5-3": "Europe/Bucharest",
    # TitanFX runs a GMT+2/+3 server (EET winter / EEST summer) — verified +3h in
    # June against the Friday gold close (last tick server 23:54 = 20:54 UTC =
    # NY 17:00 EDT). Listing it here makes the offset tz-computed (DST-correct,
    # weekend-proof) instead of relying on a live tick, which a weekend restart
    # lacks → it had defaulted to 0 and shifted every time 3h late.
    "TitanFX-MT5-01": "Europe/Athens",
}


# --------------------------------------------------------------------------- #
# Macro / rate-differential layer (precision-optimization spec, Section B)
# --------------------------------------------------------------------------- #
# Central-bank policy rates change ~8x/year on scheduled dates; a 6 h refresh
# catches a decision same-day at negligible cost. The fetch is pure HTTP (no
# MT5 connector lock) and runs off-thread, mirroring the calendar feed.
MACRO_REFRESH_SEC: Final[float] = 21600.0          # 6 hours
# On a macro / real-yield FETCH FAILURE (e.g. FRED 504/timeout), retry after
# this short delay instead of waiting the full 6 h / 1 h interval — so the
# panel self-heals within minutes once the upstream source recovers.
MACRO_RETRY_SEC: Final[float] = 300.0              # 5 minutes
MACRO_FETCH_TIMEOUT_SEC: Final[float] = 20.0
MACRO_CACHE_FILE: Final[Path] = PROJECT_ROOT / "external" / "macro" / "macro_cache.json"
# FRED API key — read from env / .env, never hard-coded.
FRED_API_KEY: Final[str] = _get_env("FRED_API_KEY", "")
# A browser UA — the Bank of England IADB returns HTTP 403 to bot UAs.
MACRO_HTTP_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Per-currency policy-rate sources.
# USD + AUD both go through FRED: the Fed funds target, and the RBA cash rate
# (the RBA's own site blocks automated requests via TLS fingerprinting, so
# FRED — which carries the same RBA figure — is the reliable route for AUD).
MACRO_FRED_RATE_SERIES: Final[str] = "DFEDTARU"      # Fed funds target, upper
# RBA cash rate via FRED — the call money / interbank overnight rate, which
# the RBA steers to its cash rate target (monthly, currently updating).
MACRO_FRED_AUD_SERIES: Final[str] = "IRSTCI01AUM156N"
MACRO_FRED_PAYEMS_SERIES: Final[str] = "PAYEMS"      # nonfarm payrolls, level
MACRO_FRED_UNRATE_SERIES: Final[str] = "UNRATE"      # unemployment rate
MACRO_ECB_URL: Final[str] = (
    "https://data-api.ecb.europa.eu/service/data/FM/"
    "D.U2.EUR.4F.KR.DFR.LEV?lastNObservations=8&format=csvdata"
)
MACRO_BOE_URL: Final[str] = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
    "?csv.x=yes&Datefrom=01/Jan/2020&Dateto=now&SeriesCodes=IUDBEDR"
    "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
)
MACRO_BOJ_URL: Final[str] = "https://www.stat-search.boj.or.jp/ssi/mtshtml/fm01_d_1_en.html"
# The five fiat currencies whose central-bank rates we track. XAU (gold) has
# no policy rate — handled specially in pair_macro_bias.
MACRO_CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP", "JPY", "AUD")

# Real-yield layer (spec §B.11). Policy rates are step functions (fixed between
# meetings); the US 10Y TIPS real yield moves every day, so it gets its own
# faster schedule than the 6 h policy-rate refresh.
MACRO_FRED_REALYIELD_SERIES: Final[str] = "DFII10"   # 10Y TIPS real yield, daily
MACRO_REALYIELD_REFRESH_SEC: Final[float] = 3600.0   # 1 hour (the daily anchor)
# Trailing daily closes shipped for the real-yield sidebar sparkline (the panel
# mirrors the DXY card's chart). ~3 months of business days.
MACRO_REALYIELD_CHART_POINTS: Final[int] = 60
# Real-TIME real yield: DFII10 is daily/lagged, so the panel anchors on the
# latest official DFII10 and adds the INTRADAY move in the nominal 10Y yield
# (CBOE ^TNX, live). real ≈ nominal − breakeven, and the breakeven is ≈constant
# intraday, so Δnominal ≈ Δreal-yield over the day. Updates ~every 30 s for live
# movement (only moves while the US Treasury market is open; static otherwise).
MACRO_NOMINAL10Y_URL: Final[str] = (
    "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX"
)
MACRO_REALYIELD_LIVE_REFRESH_SEC: Final[float] = 30.0


# --------------------------------------------------------------------------- #
# CFTC Commitment of Traders (COT) — gold-futures speculative positioning
# --------------------------------------------------------------------------- #
# Weekly large-speculator (non-commercial) net positioning in COMEX gold
# futures, from the CFTC's public Socrata API (Legacy Futures-Only report, no
# auth). A contrarian / sentiment gauge: an extreme spec net-long is a crowded
# trade. Display-only context — never feeds trigger / trade / order logic. The
# fetch is plain HTTP (no MT5 connector lock) and runs off-thread, mirroring the
# macro feed.
COT_SOCRATA_URL: Final[str] = (
    "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
)
# Exact market name in the Legacy Futures-Only dataset for COMEX gold. Matched
# exactly (not LIKE %GOLD%) so micro-gold / other gold contracts never mix in.
COT_GOLD_MARKET: Final[str] = "GOLD - COMMODITY EXCHANGE INC."
# Trailing weeks to fetch: ~1 year, enough for a year-context percentile plus a
# sparkline of the net-position trend.
COT_HISTORY_WEEKS: Final[int] = 52
# Weekly data (released Fridays for the prior-Tuesday snapshot); a 6 h refresh
# catches the new report same-day at negligible cost. Reuses MACRO_RETRY_SEC on
# a fetch failure so the panel self-heals within minutes.
COT_REFRESH_SEC: Final[float] = 21600.0            # 6 hours
COT_FETCH_TIMEOUT_SEC: Final[float] = 20.0
COT_CACHE_FILE: Final[Path] = PROJECT_ROOT / "external" / "cot" / "cot_cache.json"
# Percentile thresholds (within the trailing window) that flag a crowded book.
COT_EXTREME_HIGH_PCT: Final[float] = 90.0
COT_EXTREME_LOW_PCT: Final[float] = 10.0


# --------------------------------------------------------------------------- #
# Background loop intervals (SPEC 19, 14.4, 12.5)
# --------------------------------------------------------------------------- #

PRICE_REFRESH_SEC: Final[float] = 0.5       # ticks + account at 2 Hz. SPEC §14.4 was 1s; we tightened for XAUUSD freshness but 250ms over-loaded the browser. 500ms keeps XAUUSD within half a second of MT5 with half the CPU cost.
ANALYSIS_REFRESH_SEC: Final[float] = 5.0    # SPEC 19 ダッシュボード更新 5s
HISTORY_REFRESH_SEC: Final[float] = 60.0    # SPEC 14.4 取引履歴 60s (Phase 3)

TARGET_ANALYSIS_BUDGET_MS: Final[int] = 50  # SPEC 19 計算 50ms 以内


# --------------------------------------------------------------------------- #
# Fiat currency universe
# --------------------------------------------------------------------------- #

# The fiat universe used as the economic-calendar currency filter
# (CALENDAR_CURRENCIES = FIAT_CURRENCIES).
FIAT_CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP", "JPY", "AUD", "CHF", "NZD")


# --------------------------------------------------------------------------- #
# Account history / performance (SPEC §14)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HistoryRange:
    label: str
    days: int | None    # None ⇒ all-time


# SPEC §14.2 期間オプション
HISTORY_RANGES: Final[tuple[HistoryRange, ...]] = (
    HistoryRange("24h",   1),
    HistoryRange("7d",    7),
    HistoryRange("30d",   30),
    HistoryRange("90d",   90),
    HistoryRange("180d",  180),
    HistoryRange("1Y",    365),
    HistoryRange("all",   None),
)
HISTORY_DEFAULT_RANGE: Final[str] = "90d"   # was 30d (spec §5)


# --------------------------------------------------------------------------- #
# Economic calendar (SPEC §15)
# --------------------------------------------------------------------------- #

# SPEC §15.1 メインソース: Forex Factory thisweek XML.
CALENDAR_FF_URL: Final[str] = _get_env(
    "CALENDAR_FF_URL",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
)
# Forex Factory's free XML feed only publishes the current week — there is no
# free next-week / this-month feed (those URLs 404). A true forward horizon
# therefore needs a different source; CalendarEngine accepts multiple URLs so
# one can be added here without further code changes.
CALENDAR_FF_URLS: Final[tuple[str, ...]] = (CALENDAR_FF_URL,)

# Forward "next key events" feed (spec §B.12). Forex Factory only covers the
# current week, so the next FOMC / NFP are added from deterministic schedules
# — the calendar then always shows what is coming next.
#
# FOMC announcement dates: the Fed's published schedule (second/announcement
# day). UPDATE ANNUALLY from federalreserve.gov/monetarypolicy/fomccalendars.htm
# — the Fed publishes ~2 years ahead.
FOMC_MEETING_DATES: Final[tuple[str, ...]] = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28",
    "2027-09-15", "2027-10-27", "2027-12-08",
)
# Release clock times, US Eastern: FOMC statement ~14:00, NFP ~08:30.
FOMC_ANNOUNCE_ET: Final[tuple[int, int]] = (14, 0)
NFP_RELEASE_ET: Final[tuple[int, int]] = (8, 30)
# FRED release id for the Employment Situation report — its release/dates
# endpoint carries the scheduled future NFP dates.
FRED_NFP_RELEASE_ID: Final[int] = 50
# How many upcoming FOMC / NFP events to surface in the forward feed.
CALENDAR_UPCOMING_COUNT: Final[int] = 3

# --------------------------------------------------------------------------- #
# Non-USD central bank meeting schedules (SPEC §B.12 forward horizon).
# Forex Factory only covers the current week, so the next ECB/BoE/BoJ/RBA
# rate decision is sourced from each bank's published meeting calendar.
# UPDATE ANNUALLY from the official source listed next to each block.
# Times are the LOCAL announcement time at the central bank's headquarters
# (DST handled via zoneinfo at lookup time).
# --------------------------------------------------------------------------- #

# ECB Governing Council monetary policy meetings — decision Thursdays.
# https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
ECB_MEETING_DATES: Final[tuple[str, ...]] = (
    "2026-01-29", "2026-03-12", "2026-04-23", "2026-06-04",
    "2026-07-23", "2026-09-10", "2026-10-29", "2026-12-17",
)
# Decision is announced 14:15 CET; the press conference at 14:45 carries the
# market move — we use 14:15 as the "release_ts".
ECB_ANNOUNCE_CET: Final[tuple[int, int]] = (14, 15)

# Bank of England MPC "Super Thursdays" — Bank Rate decision.
# https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates
BOE_MEETING_DATES: Final[tuple[str, ...]] = (
    "2026-02-05", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-08-06", "2026-09-17", "2026-11-05", "2026-12-17",
)
BOE_ANNOUNCE_LON: Final[tuple[int, int]] = (12, 0)

# Bank of Japan Monetary Policy Meeting — announcement on day 2.
# https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm
BOJ_MEETING_DATES: Final[tuple[str, ...]] = (
    "2026-01-23", "2026-03-19", "2026-04-28", "2026-06-17",
    "2026-07-31", "2026-09-19", "2026-10-29", "2026-12-18",
)
# Announcement timing varies; 12:00 JST is the historical midpoint.
BOJ_ANNOUNCE_JST: Final[tuple[int, int]] = (12, 0)

# Reserve Bank of Australia cash-rate decision — first Tuesday or as scheduled.
# https://www.rba.gov.au/schedules-events/
RBA_MEETING_DATES: Final[tuple[str, ...]] = (
    "2026-02-18", "2026-04-01", "2026-05-06", "2026-07-01",
    "2026-08-05", "2026-09-23", "2026-11-04", "2026-12-09",
)
RBA_ANNOUNCE_AET: Final[tuple[int, int]] = (14, 30)
CALENDAR_FETCH_TIMEOUT_SEC: Final[float] = 10.0
CALENDAR_FETCH_RETRIES: Final[int] = 3
# SPEC §15.4 更新頻度: XML 取得 1 時間に 1 回 (HTTP).
CALENDAR_REFRESH_SEC: Final[float] = 3600.0
# How many consecutive failed XML fetches before we mark Forex Factory
# down and fall back to MT5's built-in calendar (SPEC §15.1 backup).
CALENDAR_FAILURE_FALLBACK_AFTER: Final[int] = 2

# SPEC §15.2 高インパクト(🔴)のみ表示 — Low / Medium は完全非表示。
CALENDAR_IMPACT_ALLOW: Final[frozenset[str]] = frozenset({"High"})

# SPEC §15.3 発表前後 30 分は警告色 (UI window).
CALENDAR_WARNING_WINDOW_SEC: Final[int] = 30 * 60

# SPEC §15.3 currencies to surface in the economic calendar. The dashboard is
# XAUUSD-specialised, but gold reacts to GLOBAL macro (FOMC, ECB, BoJ, risk
# events), so the calendar deliberately keeps the full major-fiat universe
# rather than narrowing to USD only — USD is the primary driver, the rest give
# the risk backdrop. Fixed (not derived from the single SYMBOLS entry).
CALENDAR_CURRENCIES: Final[frozenset[str]] = frozenset(FIAT_CURRENCIES)

# Event-type filter: only central-bank rate decisions and employment releases
# are surfaced (the two highest-impact macro categories). An event passes if
# its title contains any of these lowercase substrings; CPI / ISM / retail /
# sentiment etc. are dropped. Empty tuple = no title filter (all events).
CALENDAR_EVENT_KEYWORDS: Final[tuple[str, ...]] = (
    # rate decisions (central-bank press conferences follow the decision)
    "fomc", "federal funds rate", "bank rate", "cash rate", "policy rate",
    "refinancing rate", "rate statement", "rate decision", "monetary policy",
    "interest rate", "press conference",
    # employment
    "employment", "non-farm", "nonfarm", "payroll", "unemployment",
    "jobless", "hourly earnings", "earnings index", "claimant count",
    "jolts", "adp",
)

# How many upcoming events to render; older items disappear.
CALENDAR_DISPLAY_COUNT: Final[int] = 12

# On-disk cache so a quick restart shows the last fetched payload
# immediately instead of waiting for the first 1h cycle to fire.
CALENDAR_CACHE_FILE: Final[Path] = PROJECT_ROOT / "external" / "forex_factory_xml" / "thisweek.xml"


# --------------------------------------------------------------------------- #
# Symbol fetch concurrency
# --------------------------------------------------------------------------- #

# 10 symbols × 4 TF = 40 copy_rates_from_pos calls; pool sized for IO-bound parallelism.
SYMBOL_FETCH_WORKERS: Final[int] = 8


# --------------------------------------------------------------------------- #
# Dash server (SPEC 16.2, 16.3)
# --------------------------------------------------------------------------- #

DASH_HOST: Final[str] = _get_env("DASH_HOST", "127.0.0.1")
DASH_PORT: Final[int] = _get_env_int("DASH_PORT", 8050)
DASH_DEBUG: Final[bool] = _get_env_bool("DASH_DEBUG", False)
DASH_WS_PATH: Final[str] = "/ws"             # dash-extensions WebSocket endpoint

# WebSocket broadcaster cadence (kept in config per SPEC §23.2).
WS_HEARTBEAT_INTERVAL_SEC: Final[float] = 15.0   # keep-alive resend interval
WS_WAIT_TIMEOUT_SEC: Final[float] = 5.0          # poll cadence to check ws.connected




# --------------------------------------------------------------------------- #
# Logging (SPEC 23.2 — print 禁止、logging モジュール使用)
# --------------------------------------------------------------------------- #

LOG_LEVEL: Final[str] = _get_env("LOG_LEVEL", "INFO").upper()
LOG_DIR: Final[Path] = PROJECT_ROOT / "logs"
LOG_FILE: Final[Path] = LOG_DIR / "dashboard.log"
LOG_FILE_MAX_BYTES: Final[int] = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT: Final[int] = 5

# --------------------------------------------------------------------------- #
# 資金管理(リスク上限ゲート)— spec §4.1。降格は表示・通知のみ、実発注は不変。
# --------------------------------------------------------------------------- #
DAILY_LOSS_LIMIT_PCT: Final[float] = -3.0    # 当日損失上限(残高比%)
MAX_DD_LIMIT_PCT: Final[float] = -10.0       # 累計DD上限(%)

# --------------------------------------------------------------------------- #
# パフォーマンス分析の分類 bin(spec §6.4 / §7)。境界は「内側の閾値」のみ。
# 値 v は bisect で [- inf, b0, b1, ..., +inf] のどの区間かに割り当てる。
# --------------------------------------------------------------------------- #
R_MULTIPLE_BINS: Final[tuple[float, ...]] = (-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0)
EDGE_ADX_BINS: Final[tuple[float, ...]] = (20.0, 25.0, 30.0)
EDGE_RSI_BINS: Final[tuple[float, ...]] = (30.0, 50.0, 70.0)
EDGE_HOLD_MIN_BINS: Final[tuple[float, ...]] = (15.0, 60.0, 240.0)  # 保有分
