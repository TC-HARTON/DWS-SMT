"""Visually verify the v2 cluster-detail modal.

v1 was a raw 21-feature table. v2 should render:
  - per-cluster archetype banner (icon + plain-language label)
  - hero WR with N/CI/median in the side-meta
  - four sections: 上位 TF / 中位 TF / ベース TF / 時刻・ボラ
  - each section has 5 metric blocks (ADX/DI/RSI/close-EMA50/ATR%)
  - the *currently matched* cluster has the accent ring (.is-current)

Headless Chromium with fresh context so the static/app.js cache is bypassed.
Captures three screenshots and emits a textual PASS/FAIL report.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "loss_analysis" / "_visual"
OUT_DIR.mkdir(parents=True, exist_ok=True)

URL = "http://127.0.0.1:8050/"

PASSES: list[tuple[str, str]] = []
FAILS:  list[tuple[str, str]] = []

def report(name: str, ok: bool, detail: str = "") -> None:
    bucket = PASSES if ok else FAILS
    bucket.append((name, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}  {detail}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Fresh context = no cache from previous runs.
        ctx = browser.new_context(
            viewport={"width": 2400, "height": 1500},
            bypass_csp=True,
        )
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        print("=" * 80)
        print("1. Load page + wait for pattern table fetch")
        print("=" * 80)
        page.goto(URL, wait_until="networkidle", timeout=30000)
        # PATTERN_TABLE is fetched at top-level on load; the click-handler
        # short-circuits if it hasn't landed yet. Give it a beat.
        time.sleep(3)
        pt_ready = page.evaluate("typeof PATTERN_TABLE !== 'undefined' && PATTERN_TABLE !== null")
        report("PATTERN_TABLE loaded in browser", pt_ready)

        # The card only renders when there's an active live match (data-driven),
        # so we can't rely on it being present mid-session. Drive the modal
        # directly via the same public entrypoint the click delegate calls —
        # that's the function we're trying to verify anyway. Iterate all base
        # TFs so we catch missing-feature bugs (top TF lacks atr_pct, etc.).
        for tf_iter in ("M15", "H1", "H4"):
            meta_iter = page.evaluate(
                "(tf) => {"
                "  if (!PATTERN_TABLE || !PATTERN_TABLE[tf]) return null;"
                "  const cs = PATTERN_TABLE[tf].clusters;"
                "  const c = cs[Math.floor(cs.length / 2)];"
                "  return { sym: 'XAUUSD', tf, pid: c.pattern_id, total: cs.length };"
                "}",
                tf_iter,
            )
            if meta_iter is None:
                continue
            try:
                page.evaluate(
                    "(args) => showPatternDetailModal(args.sym, args.tf, args.pid)",
                    {"sym": meta_iter["sym"], "tf": tf_iter, "pid": meta_iter["pid"]},
                )
                page.wait_for_selector(".pat-modal-archetype", timeout=5000)
                report(f"v2 modal renders for {tf_iter}", True,
                       f"{meta_iter['total']} clusters")
                page.screenshot(path=str(OUT_DIR / f"v2_tf_{tf_iter}.png"), full_page=False)
                # Close before next iteration
                page.evaluate("() => { const m = document.getElementById('pat-modal'); if (m) m.remove(); }")
            except Exception as exc:
                report(f"v2 modal renders for {tf_iter}", False, f"{exc!r}")
                browser.close()
                return 1

        # Re-open the M15 modal for the metric-count assertions below.
        target_tf = "M15"
        meta = page.evaluate(
            "(tf) => {"
            "  const cs = PATTERN_TABLE[tf].clusters;"
            "  const c = cs[Math.floor(cs.length / 2)];"
            "  return { sym: 'XAUUSD', tf, pid: c.pattern_id, total: cs.length };"
            "}",
            target_tf,
        )
        sym, tf, pid = meta["sym"], meta["tf"], meta["pid"]

        print()
        print("=" * 80)
        print(f"2. Open v2 modal for {sym} {tf} (highlight {pid})")
        print("=" * 80)
        page.evaluate(
            "(args) => showPatternDetailModal(args.sym, args.tf, args.pid)",
            {"sym": sym, "tf": tf, "pid": pid},
        )

        # Wait for the v2 archetype banner — strictly a v2-only selector.
        try:
            page.wait_for_selector(".pat-modal-archetype", timeout=8000)
        except PWTimeout:
            report("v2 modal opened (.pat-modal-archetype visible)", False,
                   "selector did not appear within 8s")
            page.screenshot(path=str(OUT_DIR / "v2_no_modal.png"), full_page=True)
            print(f"\n  See: {OUT_DIR / 'v2_no_modal.png'}")
            browser.close()
            return 1
        report("v2 modal opened", True)

        # Headline assertions — structure, not content.
        archetype_count = page.locator(".pat-archetype-label").count()
        report("archetype label per card", archetype_count > 0, f"count={archetype_count}")

        tf_section_count = page.locator(".pat-tf-section").count()
        # 4 sections × N cards
        report("tf sections rendered",
               tf_section_count > 0 and tf_section_count % 4 == 0,
               f"count={tf_section_count}")

        metric_count = page.locator(".pat-metric").count()
        # Per card: top (4, no ATR%) + mid (5) + base (5) + time-vol (3) = 17
        n_cards = tf_section_count // 4 if tf_section_count else 0
        expected_metrics = n_cards * 17
        report("metric blocks rendered",
               metric_count == expected_metrics,
               f"count={metric_count} (expected {expected_metrics} for {n_cards} cards)")

        is_current_count = page.locator(".pat-modal-card.is-current").count()
        report("matched cluster highlighted (.is-current)",
               is_current_count == 1,
               f"count={is_current_count}")

        # Read out the archetype labels — quick eyeball check that the
        # heuristic isn't returning "混合 (中庸)" for every cluster.
        labels = page.locator(".pat-archetype-label").all_text_contents()
        unique_labels = set(labels)
        report("archetype labels are varied",
               len(unique_labels) >= 2,
               f"distinct={len(unique_labels)} of {len(labels)}: {sorted(unique_labels)}")

        # Look at one metric to make sure value+hint trio renders, not raw numbers.
        sample = page.locator(".pat-metric").first
        sample_text = sample.text_content() or ""
        report("metric has label/value/hint text",
               len(sample_text.strip()) > 3,
               f"sample='{sample_text.strip()[:80]}'")

        # Hero WR is still in the panel
        hero_count = page.locator(".dws-pat-hero-val").count()
        report("hero WR per card", hero_count == n_cards, f"count={hero_count}/{n_cards}")

        page.screenshot(path=str(OUT_DIR / "v2_01_modal_open.png"), full_page=False)
        # Full-page for the entire grid — large viewport so all 4 cards fit.
        page.screenshot(path=str(OUT_DIR / "v2_02_modal_full.png"), full_page=True)

        # Tight crop on one card so the user can see archetype + sections clearly.
        first_card = page.locator(".pat-modal-card").first
        try:
            first_card.screenshot(path=str(OUT_DIR / "v2_03_one_card.png"))
        except Exception as exc:
            print(f"  (card-crop failed: {exc})")

        print()
        print("=" * 80)
        print("3. Console errors")
        print("=" * 80)
        # Ignore favicon 404 / harmless network blips
        critical = [e for e in errors if "favicon" not in e and "ws" not in e.lower()]
        report("no critical console errors", len(critical) == 0,
               f"count={len(critical)}" + (f" first='{critical[0][:100]}'" if critical else ""))

        browser.close()

    print()
    print("=" * 80)
    print(f"SUMMARY: {len(PASSES)} PASS, {len(FAILS)} FAIL")
    print("=" * 80)
    if FAILS:
        print("FAILURES:")
        for n, d in FAILS:
            print(f"  - {n}: {d}")
    print(f"\nScreenshots: {OUT_DIR}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
