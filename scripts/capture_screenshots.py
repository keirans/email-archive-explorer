"""Capture the README screenshots from the fictional demo archive (Spec 11 §4).

    uv run --with playwright python scripts/capture_screenshots.py [OUT_DIR]

Everything shown is generated: the demo archive is written to a scratch "home"
folder, the server runs in-process with that folder as $HOME and with mounted
drives hidden, so no real path, drive name or mail can appear. The script
refuses to capture unless the open archive is the generated demo.

Uses the system Google Chrome (channel="chrome"). Default OUT_DIR is
docs/images/screenshots.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

BASE = Path("/tmp/eae-screenshots")
HOME = BASE / "home" / "alex"
MAIL = HOME / "Mail"

# Must happen before anything calls Path.home()
shutil.rmtree(BASE, ignore_errors=True)
MAIL.mkdir(parents=True)
os.environ["HOME"] = str(HOME)

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import email_archive_explorer.api as api_module  # noqa: E402
from email_archive_explorer import fs_browser  # noqa: E402
from email_archive_explorer.api import create_app  # noqa: E402
from email_archive_explorer.demo import write_demo  # noqa: E402
from email_archive_explorer.manager import ArchiveManager  # noqa: E402

fs_browser._mount_points = lambda: []  # hide this machine's drives
api_module.picker_backend = lambda: None  # no "System dialog…" button

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "images" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    paths = write_demo(MAIL)
    mgr = ArchiveManager(BASE / "cache")
    mgr.scan_directory(MAIL, recursive=True)
    mgr.open_archive(paths["eml"])
    app = create_app(manager=mgr, data_dir=MAIL)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    import json
    import urllib.request
    info = json.load(urllib.request.urlopen(f"{base}/api/archive/info"))
    if not info["file_path"].startswith(str(MAIL)) or not info["file_path"].endswith("larkspur-mail"):
        print("REFUSING: the open archive is not the generated demo:", info["file_path"])
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)

        def page(scheme: str, store: dict):
            pg = browser.new_page(viewport=VIEWPORT, color_scheme=scheme, reduced_motion="reduce", device_scale_factor=1)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.add_init_script("localStorage.clear();" + "".join(
                f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in store.items()))
            pg.goto(base)
            pg.wait_for_selector("[role=treeitem]", state="attached", timeout=20000)
            return pg

        # 1. Mail view, dark: conversation expanded. (The Inspector stays closed:
        #    it shows the file owner's user name.)
        pg = page("dark", {"eae-thread-open": "open", "eae-inspector": "closed"})
        pg.click(".tree-label:text-is('Saltmarsh Rig')")
        pg.wait_for_function("document.querySelector('#list-title')?.textContent.includes('Saltmarsh')")
        time.sleep(0.4)
        target = pg.evaluate("""() => fetch('/api/attachments?q=drift%20chart&hide_noise=false&collapse=false')
                                .then(r => r.json()).then(d => d.items.find(i => i.message.folder_path.endsWith('Saltmarsh Rig')).message.id)""")
        pg.evaluate(f"window.appSelectMessage({target})")
        pg.wait_for_selector(".thread-row.active")
        time.sleep(0.6)
        pg.screenshot(path=str(OUT / "mail-view.png"))
        pg.close()

        # 2. Attachments view, light: Discover + a ZIP's contents
        pg = page("light", {"eae-view": "attachments", "eae-inspector": "closed"})
        pg.wait_for_selector(".att-row")
        time.sleep(0.4)
        pg.locator(".att-row", has_text="Tidewater field trial data.zip").first.click()
        pg.wait_for_selector("text=field-trial/README.txt", timeout=10000)
        time.sleep(0.6)
        pg.screenshot(path=str(OUT / "attachments.png"))
        pg.close()

        # 3. File browser, dark
        pg = page("dark", {"eae-inspector": "closed"})
        pg.keyboard.press("Shift+O")
        pg.wait_for_selector("#file-browser[open] .fb-item")
        pg.locator(".fb-places button", has_text="Project data").click()
        pg.wait_for_selector(".fb-item:has-text('larkspur-mail')")
        pg.locator(".fb-item", has_text="larkspur-mail").first.click()
        time.sleep(0.5)
        pg.screenshot(path=str(OUT / "file-browser.png"))
        pg.close()
        browser.close()

    server.should_exit = True
    mgr.close()
    for f in sorted(OUT.glob("*.png")):
        print(f"{f.name}: {f.stat().st_size / 1024:.0f} KB")
    if errors:
        print("JS errors:", errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
