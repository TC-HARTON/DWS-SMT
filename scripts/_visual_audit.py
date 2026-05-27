"""Visual audit via headless Chromium. Captures the dashboard in three
states and verifies the rendered DOM against the WebSocket payload.

Outputs PNG screenshots and a textual report. No edits, read-only.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

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


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 2560, "height": 1440})
    page = ctx.new_page()

    print("=" * 80)
    print("1. Initial page load")
    print("=" * 80)
    page.goto(URL, wait_until="networkidle", timeout=30000)
    time.sleep(2)
    title = page.title()
    report("page title", "MT5" in title or "Dashboard" in title, f"'{title}'")

    # Capture console errors
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    time.sleep(2)
    report("console errors", len(errors) == 0,
           f"count={len(errors)}" + (f" first='{errors[0][:100]}'" if errors else ""))

    page.screenshot(path=str(OUT_DIR / "01_initial.png"), full_page=False)
    print(f"  screenshot: 01_initial.png")

    # Confirm 8 panels rendered
    panel_count = page.eval_on_selector_all(".panel", "els => els.length")
    expected_syms = ["XAUUSD","EURUSD","GBPUSD","AUDUSD","USDJPY","EURJPY","GBPJPY","AUDJPY"]
    report("panel count", panel_count == 8, f"count={panel_count}")
    for sym in expected_syms:
        present = page.locator(f"#panel-{sym}").count() > 0
        report(f"panel {sym} present", present)

    # Check key sidebar cards
    for card_label in ("Account", "Calendar", "Macro Rates"):
        found = page.locator(f"span.title-accent:has-text('{card_label}')").count() > 0
        report(f"sidebar card '{card_label}'", found)

    # Check footer cards
    for card_label in ("Currency Strength", "Correlation Insights"):
        found = page.locator(f"span.title-accent:has-text('{card_label}')").count() > 0
        report(f"footer card '{card_label}'", found)

    # No notify-toggle (we removed it)
    no_notify = page.locator(".notify-toggle").count() == 0
    report("notify removed", no_notify)

    print()
    print("=" * 80)
    print("2. Expand XAUUSD panel")
    print("=" * 80)
    page.click("#panel-XAUUSD")
    time.sleep(2)
    page.screenshot(path=str(OUT_DIR / "02_xauusd_expanded.png"), full_page=False)
    print("  screenshot: 02_xauusd_expanded.png")

    # Confirm validation card visible
    val_visible = page.locator("[data-bind='dws-validation-XAUUSD']").is_visible()
    report("dws-validation visible", val_visible)

    # Pattern panel — must be present with the 3-row structure
    for selector in (".dws-pat", ".dws-pat-head", ".dws-pat-hero"):
        cnt = page.locator(selector).count()
        report(f"pattern selector {selector}", cnt >= 1, f"count={cnt}")

    # Get WS pattern_matches via JS
    ws_pattern = page.evaluate("""
        () => latestSnap && latestSnap.pattern_matches && latestSnap.pattern_matches.XAUUSD || null
    """)
    print(f"  WS pattern_matches.XAUUSD: {json.dumps(ws_pattern, ensure_ascii=False)[:300]}")

    # Cycle through base TFs — scope to the expanded XAUUSD panel only
    for tf in ("M15", "H1", "H4"):
        pill = page.locator(f"#panel-XAUUSD .dws-pills .pill[data-dws='{tf}']")
        if pill.count() == 0:
            report(f"base TF pill {tf}", False, "pill missing")
            continue
        pill.click()
        time.sleep(1)
        # What does the panel show now?
        hero_text = ""
        if page.locator(".dws-pat-hero-val").count() > 0:
            hero_text = page.locator(".dws-pat-hero-val").first.text_content() or ""
        empty_text = ""
        if page.locator(".dws-pat-empty-msg").count() > 0:
            empty_text = page.locator(".dws-pat-empty-msg").first.text_content() or ""

        pat_id = ""
        wr_dom = None
        n_dom = None
        rel_dom = ""
        if page.locator(".dws-pat-hero-val").count() > 0 and hero_text:
            # active state — read shape/reliability/N
            wr_dom = hero_text.strip().replace("%", "").strip()
            rel_dom = (page.locator(".dws-pat-rel").first.text_content() or "").strip()
            n_dom_raw = ""
            stats = page.locator(".dws-pat-stat .dws-pat-v").all()
            if len(stats) >= 2:
                n_dom_raw = stats[1].text_content() or ""
                n_dom = n_dom_raw.replace(",", "").strip()

        page.screenshot(path=str(OUT_DIR / f"03_tf_{tf}.png"), full_page=False)
        print(f"  screenshot: 03_tf_{tf}.png  hero='{hero_text}'  empty='{empty_text}'  rel='{rel_dom}'  N={n_dom}")

        # Cross-reference DOM vs WS for this TF
        ws_tf = ws_pattern.get(tf) if ws_pattern else None
        if ws_tf is None:
            report(f"{tf} DOM=empty matches WS=null",
                   bool(empty_text) and not hero_text,
                   f"DOM empty='{empty_text}', hero='{hero_text}'")
        else:
            ws_wr = f"{ws_tf['win_rate']*100:.1f}"
            ws_n = str(ws_tf['sample_n'])
            ws_rel = ws_tf['reliability']
            wr_match = wr_dom == ws_wr
            n_match  = n_dom == ws_n
            rel_match = ws_rel in rel_dom
            ok = wr_match and n_match and rel_match
            report(f"{tf} DOM-WS parity",
                   ok,
                   f"WR dom={wr_dom}% ws={ws_wr}% ({'OK' if wr_match else 'MISMATCH'}), "
                   f"N dom={n_dom} ws={ws_n} ({'OK' if n_match else 'MISMATCH'}), "
                   f"rel dom='{rel_dom}' ws='{ws_rel}' ({'OK' if rel_match else 'MISMATCH'})")

    print()
    print("=" * 80)
    print("3. Macro panel rendering")
    print("=" * 80)
    macro_rows = page.locator(".macro-row").count()
    report("macro rows present", macro_rows >= 7, f"count={macro_rows} (expected 8: 1 XAU + 7 fiat pairs)")
    macro_status = page.locator("[data-bind='macro-status']").text_content() or ""
    report("macro status alive", bool(macro_status.strip() and macro_status != "--"),
           f"status='{macro_status}'")

    print()
    print("=" * 80)
    print("4. Calendar — no NZD/CHF/CAD")
    print("=" * 80)
    cal_rows = page.locator(".cal-row").all()
    bad_ccy = []
    for row in cal_rows[:30]:
        ccy = row.get_attribute("data-ccy") or ""
        if ccy in ("NZD", "CHF", "CAD"):
            bad_ccy.append(ccy)
    report("no NZD/CHF/CAD events", len(bad_ccy) == 0,
           f"bad={bad_ccy}" if bad_ccy else "none found")

    browser.close()


print()
print("=" * 80)
print(f"FINAL: PASS={len(PASSES)}  FAIL={len(FAILS)}")
print("=" * 80)
if FAILS:
    print("Failures:")
    for n, d in FAILS:
        print(f"  - {n}  {d}")
print(f"Screenshots saved to: {OUT_DIR}")
sys.exit(0 if not FAILS else 2)
