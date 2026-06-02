"""GoldMacroScore validation -- IC + OOS gate. Prints ADOPT / REJECT.

Offline, deterministic, NO look-ahead, ASCII-only output (cp932 console safe).
Decides whether the GoldMacroScore composite (analyzer/gold_macro.py) earns a
place in the product (spec docs/superpowers/specs/2026-06-02-gold-macro-score-design.md
section 5).

Two independent tests, BOTH must pass to ADOPT:

  1. Information Coefficient (IC): does the daily score predict forward XAUUSD
     returns better than noise? Spearman rank IC at 5d and 20d horizons, with a
     moving-block bootstrap CI to test whether IC's lower bound clears 0.

  2. OOS gate: does conditioning DWS-SMT XAUUSD triggers on the score regime
     improve out-of-sample profit factor? Threshold picked on the in-sample
     half (entry year < PERIOD_SPLIT_YEAR), reported on the out-of-sample half.

No look-ahead:
  * The score on day t uses only FRED levels with date <= t (trailing rolling
    window; pandas .rolling is trailing).
  * For trade conditioning the score is shifted one day (the macro level for
    day D is published after D's close, so a trade entering on D may only rely
    on the score known through D-1).

Data:
  * XAUUSD daily closes: Dukascopy CSV (reused via _oos_xauusd_16y._load_tf).
  * Driver levels: full FRED daily history (fetched here with a 429 backoff).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import config  # noqa: E402
from analyzer import dws_smt, gold_macro as gm  # noqa: E402
from analyzer.macro_feed import MacroEngine  # noqa: E402
import _oos_xauusd_16y as oos  # noqa: E402  (reuse loaders + trade builder)

try:
    from scipy.stats import spearmanr
except ImportError:  # pragma: no cover - scipy is a pandas dep, always present
    print("ERROR: scipy is required for the IC test"); sys.exit(1)

# --- parameters (deterministic) ------------------------------------------- #
WINDOW = config.GOLD_MACRO_WINDOW          # 252 trading-day z-score window
CLAMP = config.GOLD_MACRO_Z_CLAMP          # +-2.5 per-driver clamp
HORIZONS = (5, 20)                         # forward-return horizons (trading days)
SPLIT_YEAR = oos.PERIOD_SPLIT_YEAR         # 2018 in-sample / out-of-sample split
THRESHOLDS = (1.0, 2.0, 3.0)               # score-regime gate thresholds to sweep
BOOT_ITERS = 5000                          # IC bootstrap replicates
BOOT_BLOCK = 20                            # moving-block size (>= max horizon overlap)
BOOT_SEED = 20260602                       # deterministic
FRED_LIMIT = 8000                          # ~30y daily obs => full history
XAU_POINT = oos.POINT_BY_SYMBOL["XAUUSD"]


# --------------------------------------------------------------------------- #
# FRED full-history fetch (dated), with a 429 backoff
# --------------------------------------------------------------------------- #

def _fred_series_dated(body: str) -> pd.Series:
    """Parse a FRED observations body into a date-indexed level Series."""
    doc = json.loads(body)
    dates: list[pd.Timestamp] = []
    vals: list[float] = []
    for row in doc.get("observations") or []:
        raw = (row.get("value") or "").strip()
        date = str(row.get("date") or "")[:10]
        if date and raw and raw != ".":
            dates.append(pd.Timestamp(date))
            vals.append(float(raw))
    s = pd.Series(vals, index=pd.DatetimeIndex(dates), dtype=np.float64)
    return s.sort_index()


def _fetch_full_histories(max_attempts: int = 6, backoff: float = 30.0,
                          ) -> dict[str, pd.Series]:
    """Full daily FRED history per driver, date-indexed. Retries on 429."""
    eng = MacroEngine()
    out: dict[str, pd.Series] = {}
    for d in gm.GOLD_DRIVERS:
        for attempt in range(max_attempts):
            try:
                body = eng._fred_get(d.series_id, limit=FRED_LIMIT)
                s = _fred_series_dated(body)
                out[d.key] = s
                print("  fetched %-11s %5d obs  %s -> %s"
                      % (d.key, len(s), s.index.min().date(), s.index.max().date()),
                      flush=True)
                break
            except Exception as exc:  # noqa: BLE001 - retry transient FRED errors
                msg = str(exc)
                if "429" in msg and attempt < max_attempts - 1:
                    print("  %s 429, backing off %.0fs (attempt %d/%d)"
                          % (d.series_id, backoff, attempt + 1, max_attempts),
                          flush=True)
                    time.sleep(backoff)
                    continue
                print("  WARN %s fetch failed: %s" % (d.series_id, msg[:80]),
                      flush=True)
                break
    return out


# --------------------------------------------------------------------------- #
# Historical daily score reconstruction (vectorised, no look-ahead)
# --------------------------------------------------------------------------- #

def _reconstruct_daily_score(histories: dict[str, pd.Series]) -> pd.Series:
    """Daily GoldMacroScore time series, reproducing analyzer.gold_macro math.

    Each driver: trailing population z-score (matches _zscore_last's ddof=0)
    over WINDOW, clamped to +-CLAMP, sign-adjusted; the present drivers are
    equal-weighted and the mean rescaled to -10..+10. pandas .rolling is
    trailing, so the score on day t uses only levels with date <= t."""
    if not histories:
        return pd.Series(dtype=np.float64)
    df = pd.DataFrame(histories).sort_index().ffill()
    signed: dict[str, pd.Series] = {}
    for d in gm.GOLD_DRIVERS:
        if d.key not in df.columns:
            continue
        s = df[d.key]
        mean = s.rolling(WINDOW).mean()
        std = s.rolling(WINDOW).std(ddof=0)        # population, matches production
        z = (s - mean) / std
        z = z.where(std > 0.0, 0.0)                # flat window -> z 0 (as production)
        signed[d.key] = (z.clip(-CLAMP, CLAMP)) * d.sign_gold
    if not signed:
        return pd.Series(dtype=np.float64)
    raw = pd.DataFrame(signed).mean(axis=1)        # mean over present drivers
    return (raw / CLAMP * 10.0).clip(-10.0, 10.0)


# --------------------------------------------------------------------------- #
# Test 1: Information Coefficient
# --------------------------------------------------------------------------- #

def _moving_block_ic_ci(score: np.ndarray, fwd: np.ndarray,
                        ) -> tuple[float, float, float]:
    """(point IC, 2.5th, 97.5th) Spearman IC via moving-block bootstrap.

    Overlapping forward returns are autocorrelated, so a moving-block bootstrap
    (contiguous blocks) gives an honest CI where an iid bootstrap would be too
    tight. Deterministic via a fixed seed."""
    n = score.size
    point_ic, _ = spearmanr(score, fwd)
    if n < BOOT_BLOCK * 2:
        return float(point_ic), float("nan"), float("nan")
    rng = np.random.default_rng(BOOT_SEED)
    n_blocks = int(np.ceil(n / BOOT_BLOCK))
    starts_universe = np.arange(0, n - BOOT_BLOCK + 1)
    ics = np.empty(BOOT_ITERS, dtype=np.float64)
    for b in range(BOOT_ITERS):
        starts = rng.choice(starts_universe, size=n_blocks, replace=True)
        idx = (starts[:, None] + np.arange(BOOT_BLOCK)[None, :]).ravel()[:n]
        ic_b, _ = spearmanr(score[idx], fwd[idx])
        ics[b] = ic_b if np.isfinite(ic_b) else 0.0
    lo, hi = np.percentile(ics, [2.5, 97.5])
    return float(point_ic), float(lo), float(hi)


def _ic_test(score_daily: pd.Series, xau_close: pd.Series) -> dict[int, dict]:
    """Spearman IC of the score vs forward XAUUSD returns at each horizon.

    The score (FRED-calendar) is reindexed onto the XAUUSD trading calendar with
    a forward fill (each XAU day uses the latest score with date <= that day)."""
    score_on_xau = score_daily.reindex(
        xau_close.index.union(score_daily.index)).ffill().reindex(xau_close.index)
    results: dict[int, dict] = {}
    for h in HORIZONS:
        fwd = xau_close.shift(-h) / xau_close - 1.0
        aligned = pd.concat([score_on_xau, fwd], axis=1,
                            keys=["s", "r"]).dropna()
        if len(aligned) < 100:
            results[h] = {"ic": float("nan"), "lo": float("nan"),
                          "hi": float("nan"), "n": len(aligned)}
            continue
        ic, lo, hi = _moving_block_ic_ci(
            aligned["s"].to_numpy(), aligned["r"].to_numpy())
        results[h] = {"ic": ic, "lo": lo, "hi": hi, "n": len(aligned)}
    return results


# --------------------------------------------------------------------------- #
# Test 2: OOS conditioned-trade gate
# --------------------------------------------------------------------------- #

def _profit_factor(nets: list[float]) -> float:
    """gross_win / gross_loss; inf when no losses, 0 when no wins."""
    gw = sum(p for p in nets if p > 0.0)
    gl = abs(sum(p for p in nets if p < 0.0))
    if gl <= 0.0:
        return float("inf") if gw > 0.0 else 0.0
    return gw / gl


def _build_xau_trades() -> dict[str, list]:
    """Run the production DWS-SMT backtest on XAUUSD and return TradeRows per
    base TF (reusing the 16Y loaders so spreads / triggers match the baseline)."""
    frames = {tf: oos._load_tf("XAUUSD", tf, XAU_POINT)
              for tf in ("W1", "D1", "H4", "H1", "M15")}
    emit = max(len(df) for df in frames.values()) + 100
    result = dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH,
        out_bars=emit,
    )
    if result is None:
        return {}
    rows_by_tf: dict[str, list] = {}
    for base_tf in oos.BASE_TFS:
        window = result.by_base.get(base_tf)
        if window is None:
            continue
        rows_by_tf[base_tf] = oos._build_trade_rows(window, frames[base_tf], XAU_POINT)
    return rows_by_tf


def _score_for_trade(score_daily: pd.Series, entry_ms: int) -> float | None:
    """Score known the day BEFORE a trade's UTC entry date (no publication-lag
    look-ahead). None when no score exists yet."""
    entry_date = pd.Timestamp(entry_ms, unit="ms", tz="UTC").tz_localize(None).normalize()
    prior = score_daily.loc[:entry_date - pd.Timedelta(days=1)]
    if prior.empty or not np.isfinite(prior.iloc[-1]):
        return None
    return float(prior.iloc[-1])


def _oos_gate(rows_by_tf: dict[str, list], score_daily: pd.Series) -> dict[str, dict]:
    """Per base TF: pick the gate threshold on the in-sample half, then report
    conditioned vs unconditioned PF on the out-of-sample half."""
    out: dict[str, dict] = {}
    for base_tf, rows in rows_by_tf.items():
        scored = [(r, _score_for_trade(score_daily, r.entry_ms)) for r in rows]
        scored = [(r, sc) for r, sc in scored if sc is not None]
        ins = [(r, sc) for r, sc in scored if r.entry_year < SPLIT_YEAR]
        oos_set = [(r, sc) for r, sc in scored if r.entry_year >= SPLIT_YEAR]
        if len(ins) < 30 or len(oos_set) < 30:
            out[base_tf] = {"error": "too_few_trades",
                            "n_ins": len(ins), "n_oos": len(oos_set)}
            continue

        def keep(r, sc, thr):
            return (r.direction > 0 and sc >= thr) or (r.direction < 0 and sc <= -thr)

        # In-sample: pick the threshold with the best PF (require enough trades).
        best_thr, best_pf = None, -1.0
        for thr in THRESHOLDS:
            kept = [r.net_pts for r, sc in ins if keep(r, sc, thr)]
            if len(kept) < 20:
                continue
            pf = _profit_factor(kept)
            pf_cmp = pf if np.isfinite(pf) else 1e9
            if pf_cmp > best_pf:
                best_pf, best_thr = pf_cmp, thr

        pf_base_oos = _profit_factor([r.net_pts for r, _ in oos_set])
        if best_thr is None:
            out[base_tf] = {"error": "no_viable_threshold",
                            "pf_base_oos": pf_base_oos}
            continue
        kept_oos = [r.net_pts for r, sc in oos_set if keep(r, sc, best_thr)]
        pf_cond_oos = _profit_factor(kept_oos)
        out[base_tf] = {
            "best_thr": best_thr,
            "n_oos_base": len(oos_set), "n_oos_cond": len(kept_oos),
            "pf_base_oos": pf_base_oos, "pf_cond_oos": pf_cond_oos,
            "improved": (pf_cond_oos > pf_base_oos and len(kept_oos) >= 20),
        }
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _fmt(x: float) -> str:
    if x is None or not np.isfinite(x):
        return "inf" if (x is not None and x == float("inf")) else "n/a"
    return "%+.4f" % x


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    print("=== GoldMacroScore validation ===", flush=True)
    print("[1/4] fetching full FRED driver histories ...", flush=True)
    histories = _fetch_full_histories()
    if len(histories) < len(gm.GOLD_DRIVERS):
        print("WARN: only %d/%d drivers fetched; score uses the available set"
              % (len(histories), len(gm.GOLD_DRIVERS)), flush=True)
    if not histories:
        print("VERDICT: BLOCKED (no FRED data)"); return 2

    print("[2/4] reconstructing daily score (no look-ahead) ...", flush=True)
    score_daily = _reconstruct_daily_score(histories)
    valid = score_daily.dropna()
    print("  score days: %d  %s -> %s"
          % (len(valid), valid.index.min().date(), valid.index.max().date()),
          flush=True)

    print("[3/4] loading XAUUSD daily + IC test ...", flush=True)
    xau_daily = oos._load_tf("XAUUSD", "D1", XAU_POINT)
    xau_close = xau_daily["close"]
    xau_close.index = pd.DatetimeIndex(xau_close.index).tz_localize(None).normalize()
    xau_close = xau_close[~xau_close.index.duplicated(keep="last")]
    ic_res = _ic_test(score_daily, xau_close)

    print("[4/4] OOS conditioned-trade gate ...", flush=True)
    rows_by_tf = _build_xau_trades()
    gate_res = _oos_gate(rows_by_tf, score_daily) if rows_by_tf else {}

    # ---- report ----
    print("", flush=True)
    print("--- Test 1: Information Coefficient (Spearman, fwd XAUUSD return) ---",
          flush=True)
    ic_pass = False
    for h in HORIZONS:
        r = ic_res.get(h, {})
        ic, lo, hi, n = r.get("ic"), r.get("lo"), r.get("hi"), r.get("n", 0)
        clears = (lo is not None and np.isfinite(lo) and lo > 0.0)
        ic_pass = ic_pass or clears
        print("  %2dd : IC=%s  CI95=[%s, %s]  n=%d  %s"
              % (h, _fmt(ic), _fmt(lo), _fmt(hi), n,
                 "PASS (CI>0)" if clears else "fail"), flush=True)

    print("", flush=True)
    print("--- Test 2: OOS gate (PF conditioned vs unconditioned, >= %d) ---"
          % SPLIT_YEAR, flush=True)
    gate_pass = False
    for base_tf in oos.BASE_TFS:
        g = gate_res.get(base_tf)
        if not g:
            print("  %-3s : no trades" % base_tf, flush=True); continue
        if "error" in g:
            print("  %-3s : %s" % (base_tf, g["error"]), flush=True); continue
        improved = g["improved"]
        gate_pass = gate_pass or improved
        pfb = g["pf_base_oos"]; pfc = g["pf_cond_oos"]
        pfb_s = "inf" if pfb == float("inf") else "%.3f" % pfb
        pfc_s = "inf" if pfc == float("inf") else "%.3f" % pfc
        print("  %-3s : thr=%.0f  PF %s -> %s  (OOS trades %d -> %d)  %s"
              % (base_tf, g["best_thr"], pfb_s, pfc_s,
                 g["n_oos_base"], g["n_oos_cond"],
                 "PASS" if improved else "no improvement"), flush=True)

    print("", flush=True)
    verdict = "ADOPT" if (ic_pass and gate_pass) else "REJECT"
    print("IC test:   %s" % ("PASS" if ic_pass else "fail"), flush=True)
    print("OOS gate:  %s" % ("PASS" if gate_pass else "fail"), flush=True)
    print("VERDICT: %s" % verdict, flush=True)
    print("(ADOPT requires BOTH tests to pass. REJECT is a valid outcome: do "
          "not ship a noisy gauge as a tradeable signal.)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
