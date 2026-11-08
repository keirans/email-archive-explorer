"""Browser accessibility & smoke audit (axe-core, WCAG 2.1 AA + best practice).

Usage (with the server running, e.g. `uv run email-archive serve --no-browser`):

    uv run --with playwright --with axe-playwright-python python scripts/ui_audit.py [URL] [SCREENSHOT_DIR]

Uses the system Google Chrome (channel="chrome"); run `playwright install chromium`
and drop channel= if Chrome isn't installed. Exits non-zero on any violation or
JavaScript error.
"""
import os, sys, time
from playwright.sync_api import sync_playwright
from axe_playwright_python.sync_playwright import Axe
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/"
out = sys.argv[2] if len(sys.argv) > 2 else "."
os.makedirs(out, exist_ok=True)
total = 0
errors = []
axe = Axe()
def audit(pg, name, context=None):
    res = axe.run(pg, context=context, options={"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]}})
    global total
    v = res.response["violations"]
    total += len(v)
    print(f"[{name}] violations: {len(v)}")
    for x in v:
        print(f"   - {x['id']} ({x['impact']}) ×{len(x['nodes'])}: {x['nodes'][0]['target']} :: {x['nodes'][0].get('failureSummary','').splitlines()[1:2]}")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    for scheme in ("dark", "light"):
        pg = b.new_page(viewport={"width": 1440, "height": 900}, color_scheme=scheme, reduced_motion="reduce")
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        pg.goto(URL)
        pg.wait_for_selector(".message-card", timeout=20000); time.sleep(1)
        pg.click(".message-card"); pg.wait_for_selector(".view-subject"); time.sleep(0.5)
        if scheme == "dark":
            # keyboard tree navigation
            pg.focus("[role=treeitem][tabindex='0']")
            before = pg.evaluate("document.activeElement.dataset.name")
            pg.keyboard.press("ArrowDown"); pg.keyboard.press("ArrowDown")
            moved = pg.evaluate("document.activeElement.dataset.name")
            pg.keyboard.press("Enter"); time.sleep(0.8)
            print(f"tree keys: {before} -> {moved}; list title now: {pg.inner_text('#list-title')}; selected aria: {pg.get_attribute('[role=treeitem][aria-selected=true]', 'data-name')}")
            print("roles:", pg.evaluate("[document.querySelector('[role=tree]')?.getAttribute('aria-label'), document.querySelectorAll('[role=option]').length, document.querySelector('#message-list').getAttribute('role')]"))
            pg.screenshot(path=f"{out}/f1-mail.png")
        audit(pg, f"mail/{scheme}")
        # Spec 10: audit again with the conversation list expanded
        if pg.query_selector(".thread-bar"):
            pg.evaluate("localStorage.setItem('eae-thread-open', 'closed')")
            pg.locator("body").press("t"); pg.wait_for_selector(".thread-row"); time.sleep(0.3)
            audit(pg, f"mail+thread/{scheme}")
            pg.locator("body").press("t"); time.sleep(0.2)
        pg.click("#tab-attachments"); pg.wait_for_selector(".att-row"); time.sleep(1)
        audit(pg, f"attachments/{scheme}")
        if scheme == "dark": pg.screenshot(path=f"{out}/f2-att.png")
        pg.click("#tab-mail"); time.sleep(0.3)
        pg.keyboard.press("Shift+O"); pg.wait_for_selector("#file-browser[open] .fb-item"); time.sleep(0.5)
        audit(pg, f"filebrowser/{scheme}", context="#file-browser")
        pg.keyboard.press("Escape"); time.sleep(0.3)
        pg.keyboard.press("?"); pg.wait_for_selector("#help-dialog[open]")
        audit(pg, f"help/{scheme}", context="#help-dialog")
        pg.keyboard.press("Escape"); time.sleep(0.3)
        pg.keyboard.press("i"); pg.wait_for_selector(".inspector.open .insp-section"); time.sleep(0.5)
        audit(pg, f"inspector/{scheme}", context="#inspector")
        pg.keyboard.press("i")
        pg.close()
    b.close()
print("ERRORS:", errors or "none")
sys.exit(1 if (total or errors) else 0)
