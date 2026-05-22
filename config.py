"""Global configuration values for the MT5-Python Trading Dashboard.

All tunable constants live here per SPEC 23.2 ("設定値は config.py に集約").
Values that depend on the user environment (MT5 path, credentials, ports)
are loaded from a .env file via python-dotenv; everything else is a literal
constant traceable back to a specific SPEC section.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


# Order matters — top-down render order per SPEC 7.1.
SYMBOLS: Final[tuple[SymbolSpec, ...]] = (
    SymbolSpec("XAUUSD", "xl", 50.0),       # SPEC 10.2: $50
    SymbolSpec("USDJPY", "md", 0.500),      # SPEC 10.2: 50pips
    SymbolSpec("EURUSD", "md", 0.01000),    # SPEC 10.2: 100pips
    SymbolSpec("GBPUSD", "md", 0.01000),
    SymbolSpec("AUDUSD", "md", 0.01000),
    SymbolSpec("GBPJPY", "sm", 0.500),
    SymbolSpec("EURJPY", "sm", 0.500),
    SymbolSpec("AUDJPY", "sm", 0.500),
    SymbolSpec("EURGBP", "sm", 0.01000),
    SymbolSpec("EURAUD", "sm", 0.01000),
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


# SPEC 5 / 6.1: D1=EMA200, H4=EMA50, H1=EMA20, M15=EMA13
TIMEFRAMES: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("D1",  mt5.TIMEFRAME_D1,  200, 400),
    TimeframeSpec("H4",  mt5.TIMEFRAME_H4,   50, 300),
    TimeframeSpec("H1",  mt5.TIMEFRAME_H1,   20, 240),
    TimeframeSpec("M15", mt5.TIMEFRAME_M15,  13, 200),
)

TIMEFRAME_BY_LABEL: Final[dict[str, TimeframeSpec]] = {tf.label: tf for tf in TIMEFRAMES}

# Phase 2 補助 TF — 指標計算には使わないが構造検出 (PWH/PWL/PMH/PML/VWAP) で必要。
# ema_period は使われないので便宜上 0 を入れる。
# W1 は DWS-SMT の 4H ベース・スタック最上段にも使うため、EMA(20) が十分に
# ウォームアップする本数 (60本 ≈ 14か月) を確保する。
STRUCTURE_TFS: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("W1",  mt5.TIMEFRAME_W1,  0,  60),
    TimeframeSpec("MN1", mt5.TIMEFRAME_MN1, 0,  12),
    TimeframeSpec("M1",  mt5.TIMEFRAME_M1,  0, 1440),  # 1 day for VWAP
)

# SPEC §6.2 ADX(14)
ADX_PERIOD: Final[int] = 14

# SPEC §6.3 RSI(14)
RSI_PERIOD: Final[int] = 14

# SPEC 6.4 ATR(14) Wilder
ATR_PERIOD: Final[int] = 14

# SPEC §6 mandated *display* timeframes for each indicator. Computation runs
# on every TF unconditionally (cheap and useful for derived signals), but
# only the labels in these sets are shown in the symbol panels.
EMA_DISPLAY_TFS: Final[frozenset[str]] = frozenset({"D1", "H4", "H1", "M15"})  # §6.1
ADX_DISPLAY_TFS: Final[frozenset[str]] = frozenset({"D1", "H4"})               # §6.2
RSI_DISPLAY_TFS: Final[frozenset[str]] = frozenset({"H1", "M15"})              # §6.3
ATR_DISPLAY_TFS: Final[frozenset[str]] = frozenset({"H4"})                     # §6.4


# --------------------------------------------------------------------------- #
# DWS-SMT indicator — port of MQL5/Indicators/DWS_SMT.mq5 v2.00
# --------------------------------------------------------------------------- #
# The .mq5 stacks three timeframe rows (its TF1/TF2/TF3 inputs). Here the
# selected base timeframe *anchors* its own stack: the base TF is the bottom
# row and the two next-higher timeframes stack above it. Switching the base
# therefore slides the whole 3-TF stack up/down the timeframe ladder. Each
# tuple is listed top→bottom, the order the histogram draws the rows.
DWS_SMT_STACKS: Final[dict[str, tuple[str, ...]]] = {
    "M15": ("H4", "H1", "M15"),
    "H1":  ("D1", "H4", "H1"),
    "H4":  ("W1", "D1", "H4"),
}
# Switchable base timeframes — also the histogram x-axis resolution.
DWS_SMT_BASE_TFS: Final[tuple[str, ...]] = tuple(DWS_SMT_STACKS)
DWS_SMT_DEFAULT_BASE: Final[str] = "H4"
DWS_SMT_PERIOD: Final[int] = 20        # .mq5 input SMT_Period — EMA for close−EMA diff
DWS_SMT_SMOOTH: Final[int] = 5         # .mq5 input Smooth — EMA for diff smoothing
DWS_SMT_BARS: Final[int] = 96          # base bars emitted per base timeframe


# --------------------------------------------------------------------------- #
# Composite BIAS — per-TF tfSignal → regime-gated weighted composite
# --------------------------------------------------------------------------- #
# The dashboard computes the *live* BIAS in static/app.js; the backend computes
# the *historical* BIAS series (per DWS base bar) so the trigger filter can
# judge each past trigger by the BIAS as it was then — not by today's BIAS
# (look-ahead). These constants MUST stay in sync with TF_WEIGHTS and
# tfTrendFactor() in static/app.js.
BIAS_TF_WEIGHTS: Final[dict[str, float]] = {
    "D1": 3.0, "H4": 2.0, "H1": 1.5, "M15": 1.0,
}
BIAS_REGIME_ADX_LOW: Final[float] = 15.0    # regime gate: ADX ≤ low → factor 0
BIAS_REGIME_ADX_HIGH: Final[float] = 25.0   #              ADX ≥ high → factor 1


# --------------------------------------------------------------------------- #
# Background loop intervals (SPEC 19, 14.4, 12.5)
# --------------------------------------------------------------------------- #

PRICE_REFRESH_SEC: Final[float] = 0.5       # ticks + account at 2 Hz. SPEC §14.4 was 1s; we tightened for XAUUSD freshness but 250ms over-loaded the browser. 500ms keeps XAUUSD within half a second of MT5 with half the CPU cost.
ANALYSIS_REFRESH_SEC: Final[float] = 5.0    # SPEC 19 ダッシュボード更新 5s
HEAVY_REFRESH_SEC: Final[float] = 30.0      # SPEC 12.5 通貨強弱 30s (Phase 3 で使う土台)
HISTORY_REFRESH_SEC: Final[float] = 60.0    # SPEC 14.4 取引履歴 60s (Phase 3)

TARGET_ANALYSIS_BUDGET_MS: Final[int] = 50  # SPEC 19 計算 50ms 以内


# --------------------------------------------------------------------------- #
# Currency strength (SPEC §12)
# --------------------------------------------------------------------------- #

# SPEC §12.1 fiat universe + XAU shown for reference only (XAU is *not*
# included in the cross-pair averaging because there are no XAU/EUR etc.).
FIAT_CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP", "JPY", "AUD", "CHF", "NZD")
GOLD_SYMBOL: Final[str] = "XAUUSD"           # SPEC §7 — primary; XAU strength derived from it

# SPEC §12.2 the full 27-pair compute set. Symbols not exposed by the
# broker are dropped from the calculation with a warning at startup.
CURRENCY_STRENGTH_PAIRS: Final[tuple[str, ...]] = (
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD",
    "USDJPY", "USDCHF", "USDCAD",
    "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "EURGBP", "EURAUD", "EURNZD", "EURCHF", "EURCAD",
    "GBPAUD", "GBPNZD", "GBPCHF", "GBPCAD",
    "AUDNZD", "AUDCHF", "AUDCAD",
    "NZDCHF", "NZDCAD",
    "CADCHF",
)

# CAD is required by SPEC §12.2 (USDCAD, CADJPY, etc.) but is not in
# SPEC §12.1's 7-fiat display list. We still compute its strength so the
# averaging maths is sound, just hide it from the UI by default.
ALL_STRENGTH_CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP", "JPY", "AUD", "CHF", "NZD", "CAD")


# SPEC §12.4 switchable strength windows — reused as TimeframeSpec entries so
# the connector / fetch path needs no adapter type. We fetch 6 bars so the
# engine can measure a cumulative % change over the last 3 closed bars
# (endpoint close[-2], reference close[-5]) while ignoring the in-progress
# bar[-1]. Closed-bar-only = no per-tick wobble; 3-bar span smooths spikes.
STRENGTH_WINDOWS: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("H1", mt5.TIMEFRAME_H1, 0, 6),    # 直近数時間 (H1×3本)
    TimeframeSpec("H4", mt5.TIMEFRAME_H4, 0, 6),    # 直近半日強 (H4×3本)
    TimeframeSpec("D1", mt5.TIMEFRAME_D1, 0, 6),    # 直近数日 (D1×3本)
    TimeframeSpec("W1", mt5.TIMEFRAME_W1, 0, 6),    # 直近数週 (W1×3本)
)
STRENGTH_DEFAULT_WINDOW: Final[str] = "H4"

# SPEC §12.6 pair-bias thresholds (Δ = base_strength - quote_strength).
STRENGTH_PAIR_BIAS_STRONG: Final[float] = 30.0
STRENGTH_PAIR_BIAS_WEAK: Final[float] = 10.0


# --------------------------------------------------------------------------- #
# Correlation matrix (SPEC §13)
# --------------------------------------------------------------------------- #

# SPEC §13.4 三段階の本数切替
CORRELATION_WINDOWS_BARS: Final[tuple[int, ...]] = (20, 100, 500)
CORRELATION_DEFAULT_BARS: Final[int] = 100
# SPEC §13.1 uses the 10 monitored symbols. We compute on H1 closes by
# default — short enough to keep the 500-bar window inside ~20 days.
CORRELATION_TIMEFRAME: Final[str] = "H1"


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
    HistoryRange("all",   None),
)
HISTORY_DEFAULT_RANGE: Final[str] = "30d"


# --------------------------------------------------------------------------- #
# Economic calendar (SPEC §15)
# --------------------------------------------------------------------------- #

# SPEC §15.1 メインソース: Forex Factory thisweek XML.
CALENDAR_FF_URL: Final[str] = _get_env(
    "CALENDAR_FF_URL",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
)
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

# SPEC §15.3 currencies to surface. Defaults to the same fiat universe as
# the strength meter so the calendar agrees with the rest of the dashboard.
CALENDAR_CURRENCIES: Final[frozenset[str]] = frozenset(FIAT_CURRENCIES)

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
# LineExporter EA (SPEC 9)
# --------------------------------------------------------------------------- #

# APPDATA exists on every Windows user session; fall back gracefully on
# CI / non-Windows so the module still imports (used by tests).
_appdata = os.environ.get("APPDATA", str(PROJECT_ROOT / "data"))
LINES_DIR: Final[Path] = Path(
    _get_env(
        "LINES_DIR",
        str(Path(_appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files"),
    )
)
LINES_FILE_PREFIX: Final[str] = "lines_"   # SPEC §9.1 filename pattern: lines_{symbol}.json
LINES_FILE_SUFFIX: Final[str] = ".json"

# Watchdog debounce: when an EA rewrites a file the FS often emits both a
# CREATED and a MODIFIED event in rapid succession. We coalesce events
# arriving within this window per (path) into a single reload.
LINES_DEBOUNCE_SEC: Final[float] = 0.15


# --------------------------------------------------------------------------- #
# Auto-detected structure levels (SPEC §10)
# --------------------------------------------------------------------------- #

# SPEC §10.2 ラウンドナンバー刻み。Symbol-keyed override; falls back to a
# heuristic derived from current price when a symbol is absent.
ROUND_NUMBER_STEPS: Final[dict[str, float]] = {
    "XAUUSD": 50.0,        # SPEC §10.2: $50
    "USDJPY": 0.500,       # SPEC §10.2: 50pips
    "EURJPY": 0.500,
    "GBPJPY": 0.500,
    "AUDJPY": 0.500,
    "EURUSD": 0.01000,     # SPEC §10.2: 100pips
    "GBPUSD": 0.01000,
    "AUDUSD": 0.01000,
    "EURGBP": 0.01000,
    "EURAUD": 0.01000,
}
# How many round-number rungs above and below the current price to publish.
ROUND_NUMBER_RUNGS: Final[int] = 3

# Fractal swing-point lookback (Bill Williams 5-bar fractal). A bar at index
# i is a swing-high when high[i] > high[i±1..k] for all k in 1..N. M15 is the
# active TF for trader-relevant swings; H1/H4 also detected for context.
FRACTAL_LOOKBACK: Final[int] = 2          # 2 bars on each side → 5-bar pattern
FRACTAL_TFS: Final[tuple[str, ...]] = ("M15", "H1", "H4")
# How many most-recent swings per TF/side to surface (older ones drop off).
FRACTAL_KEEP_PER_TF: Final[int] = 3

# Trading sessions, JST-defined per SPEC §10.3 but converted to UTC at use.
@dataclass(frozen=True)
class SessionSpec:
    name: str
    start_jst: int          # hour, 0-23
    end_jst: int            # hour, 0-23 (may roll past midnight if < start)


SESSIONS: Final[tuple[SessionSpec, ...]] = (
    SessionSpec("Asia",   7, 16),   # SPEC §10.3 Asian:  07:00 - 16:00 JST
    SessionSpec("Europe", 16, 22),  # SPEC §10.3 Europe: 16:00 - 22:00 JST
    SessionSpec("NY",     21,  6),  # SPEC §10.3 NY:     21:00 - 06:00 JST (rolls)
)

# JST is fixed at UTC+9 with no DST. Defined here so backend (structure
# detector / account monitor) and the injected clientside JS share one
# constant — SPEC §23.2 forbids loose magic numbers.
JST_OFFSET_HOURS: Final[int] = 9

# Previous-period high/low source TFs (high/low of the closed prior bar).
PREV_PERIOD_TFS: Final[dict[str, str]] = {
    "PD": "D1",   # PDH/PDL
    "PW": "W1",   # PWH/PWL
    "PM": "MN1",  # PMH/PML (MT5 monthly)
}

# --------------------------------------------------------------------------- #
# Price action detection (SPEC §11)
# --------------------------------------------------------------------------- #

# Pin-bar geometry: tail >= body * PIN_TAIL_RATIO and opposite wick <= body * PIN_WICK_MAX.
PIN_TAIL_RATIO: Final[float] = 2.0
PIN_WICK_MAX: Final[float] = 0.3
# Inside-bar break: bars to look back for the inside-bar after which a break
# in either direction is considered a continuation signal.
INSIDE_BREAK_LOOKBACK: Final[int] = 5
# Number of recent M15 patterns retained per symbol (older ones drop off).
PA_KEEP_RECENT: Final[int] = 6


# --------------------------------------------------------------------------- #
# Confluence detection (SPEC §10.4)
# --------------------------------------------------------------------------- #

# Cluster width as a multiple of current H4 ATR.
CONFLUENCE_ATR_MULTIPLE: Final[float] = 0.3       # SPEC §10.4: "ATR×0.3以内"
# Minimum number of structure levels in a cluster for it to count.
CONFLUENCE_MIN_ELEMENTS: Final[int] = 3            # SPEC §10.4 "3要素 < 4要素 < 5要素以上"


# --------------------------------------------------------------------------- #
# Dash server (SPEC 16.2, 16.3)
# --------------------------------------------------------------------------- #

DASH_HOST: Final[str] = _get_env("DASH_HOST", "127.0.0.1")
DASH_PORT: Final[int] = _get_env_int("DASH_PORT", 8050)
DASH_DEBUG: Final[bool] = _get_env_bool("DASH_DEBUG", False)
DASH_WS_PATH: Final[str] = "/ws"             # dash-extensions WebSocket endpoint
DASH_PAGE_TITLE: Final[str] = "MT5 Trading Dashboard"

# WebSocket broadcaster cadence (kept in config per SPEC §23.2).
WS_HEARTBEAT_INTERVAL_SEC: Final[float] = 15.0   # keep-alive resend interval
WS_WAIT_TIMEOUT_SEC: Final[float] = 5.0          # poll cadence to check ws.connected


# --------------------------------------------------------------------------- #
# Colors (SPEC 16.4) — referenced from CSS variables and Plotly figures
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Theme:
    bg: str = "#0d0d0d"
    bg_panel: str = "#1a1a1a"
    fg: str = "#e0e0e0"
    fg_muted: str = "#888888"
    buy: str = "#00ff7f"
    sell: str = "#ff4444"
    neutral: str = "#888888"
    warning: str = "#ffaa00"
    critical: str = "#ff0066"


THEME: Final[Theme] = Theme()


# --------------------------------------------------------------------------- #
# Structure-touch coloring (SPEC 17.1)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TouchThresholds:
    """Distance-to-structure thresholds expressed as multiples of H4 ATR."""

    far: float = 0.5      # within 0.5 * ATR → yellow
    near: float = 0.3     # within 0.3 * ATR → orange
    touch: float = 0.1    # within 0.1 * ATR → red + bold border


TOUCH_THRESHOLDS: Final[TouchThresholds] = TouchThresholds()


# --------------------------------------------------------------------------- #
# Logging (SPEC 23.2 — print 禁止、logging モジュール使用)
# --------------------------------------------------------------------------- #

LOG_LEVEL: Final[str] = _get_env("LOG_LEVEL", "INFO").upper()
LOG_DIR: Final[Path] = PROJECT_ROOT / "logs"
LOG_FILE: Final[Path] = LOG_DIR / "dashboard.log"
LOG_FILE_MAX_BYTES: Final[int] = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT: Final[int] = 5
