"""Reading-pane layout check (Spec 10 §5, criterion 3).

Builds a synthetic MBOX with worst-case messages (a 2,000-message thread, 300
recipients, 60 attachments), serves it, and checks in headless Chrome that the
message body starts within the top 45% of the reading pane at 1366×768, with the
conversation bar both collapsed and expanded.

    uv run --with playwright python scripts/reading_pane_check.py [SCREENSHOT_DIR]

Uses the system Google Chrome (channel="chrome"). Exits non-zero on failure.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

THREAD = 2000
LIMIT = 0.45


def _msg(subject, mid, when, irt=None, to=1, attachments=0, body="Hello"):
    m = EmailMessage()
    m["From"] = "Alex Doe <alex@larkspur.example>"
    m["To"] = ", ".join(f"Person {i} <p{i}@larkspur.example>" for i in range(to))
    m["Subject"] = subject
    m["Date"] = format_datetime(when)
    m["Message-ID"] = f"<{mid}@larkspur.example>"
    if irt:
        m["In-Reply-To"] = f"<{irt}@larkspur.example>"
    m.set_content(body + "\n\n" + "Lorem ipsum dolor sit amet. " * 40)
    for i in range(attachments):
        m.add_attachment(f"file {i}\n".encode(), maintype="text", subtype="plain", filename=f"notes-{i:02d}.txt")
    return m


def build_mbox(path: Path) -> None:
    start = datetime(2003, 1, 1, 9, tzinfo=UTC)
    msgs = []
    for i in range(THREAD):
        msgs.append(_msg("Saltmarsh rig calibration" if i == 0 else "RE: Saltmarsh rig calibration",
                         f"t{i}", start + timedelta(minutes=10 * i), irt=f"t{i - 1}" if i else None,
                         body=f"Thread message {i}"))
    msgs.append(_msg("All-hands distribution list", "wide", start, to=300, body="WIDE-RECIPIENTS"))
    msgs.append(_msg("Drawing pack", "atts", start, attachments=60, body="MANY-ATTACHMENTS"))
    with path.open("wb") as f:
        for m in msgs:
            f.write(b"From alex@larkspur.example Sat Jan  1 09:00:00 2003\n")
            raw = m.as_bytes().replace(b"\nFrom ", b"\n>From ")
            f.write(raw.rstrip(b"\n") + b"\n\n")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    tmp = Path(tempfile.mkdtemp(prefix="eae-pane-"))
    mbox = tmp / "worst-case.mbox"
    build_mbox(mbox)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, "-m", "email_archive_explorer.cli", "serve", str(mbox), "--no-browser",
         "--port", str(port), "--cache-dir", str(tmp / "cache")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        for _ in range(240):
            try:
                urllib.request.urlopen(f"{base}/api/archive/info", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            print("server did not start")
            return 1

        def find(q):
            data = json.load(urllib.request.urlopen(f"{base}/api/search?q={q}&page_size=1"))
            return data["results"][0]["id"]

        def find_thread_mid():
            data = json.load(urllib.request.urlopen(f"{base}/api/search?q=%22Thread+message+1000%22&page_size=1"))
            return data["results"][0]["id"]

        cases = {
            "thread-2000": find_thread_mid(),
            "recipients-300": find("WIDE-RECIPIENTS"),
            "attachments-60": find("MANY-ATTACHMENTS"),
        }
        t0 = time.time()
        w = json.load(urllib.request.urlopen(f"{base}/api/messages/{cases['thread-2000']}/thread?limit=200"))
        api_ms = (time.time() - t0) * 1000
        print(f"thread API: size={w['size']} items={len(w['items'])} method={w['method']} {api_ms:.0f} ms")
        if w["size"] != THREAD or len(w["items"]) > 200 or api_ms > 200:
            failures.append("thread API")

        errors = []
        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome", headless=True)
            pg = b.new_page(viewport={"width": 1366, "height": 768})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base)
            pg.wait_for_selector(".message-card", timeout=30000)
            for open_state in ("closed", "open"):
                pg.evaluate(f"localStorage.setItem('eae-thread-open', '{open_state}')")
                for name, mid in cases.items():
                    t = time.time()
                    pg.evaluate(f"window.appSelectMessage({mid})")
                    pg.wait_for_function(
                        f"document.querySelector('.view-body') && document.querySelector('.view-body').textContent.length > 0"
                        f" && (!document.querySelector('.thread-section:not([hidden])') || '{name}' !== 'thread-2000'"
                        f" || document.querySelector('.thread-count'))")
                    render_ms = (time.time() - t) * 1000
                    m = pg.evaluate("""() => {
                        const pane = document.querySelector('#view-pane').getBoundingClientRect();
                        const body = document.querySelector('.view-body').getBoundingClientRect();
                        return {ratio: (body.top - pane.top) / pane.height, bodyH: body.height,
                                rows: document.querySelectorAll('.thread-row').length,
                                count: document.querySelector('.thread-count')?.textContent.trim() || ''};
                    }""")
                    ok = m["ratio"] <= LIMIT and m["bodyH"] >= 240 and m["rows"] <= 200
                    print(f"[{open_state:6}] {name:15} body starts at {m['ratio']:.0%} of pane, body {m['bodyH']:.0f}px, "
                          f"rows {m['rows']}, {m['count']} {render_ms:.0f} ms {'OK' if ok else 'FAIL'}")
                    if not ok:
                        failures.append(f"{open_state}/{name}")
                    if out and name == "thread-2000":
                        out.mkdir(parents=True, exist_ok=True)
                        pg.screenshot(path=str(out / f"pane-{open_state}.png"))
            # n / p keys walk the conversation
            pg.evaluate("localStorage.setItem('eae-thread-open', 'open')")
            pg.evaluate(f"window.appSelectMessage({cases['thread-2000']})")
            pg.wait_for_selector(".thread-count")
            before = pg.inner_text(".thread-count")
            pg.locator("body").press("n")
            pg.wait_for_function(f"document.querySelector('.thread-count') && document.querySelector('.thread-count').textContent !== {json.dumps(before)}")
            after = pg.inner_text(".thread-count")
            print(f"n key: {before.strip()} -> {after.strip()}")
            if "1002 of" not in after:
                failures.append("n key")
            b.close()
        if errors:
            print("JS errors:", errors)
            failures.append("js errors")
    finally:
        server.terminate()
        server.wait(timeout=10)
    print("FAILED: " + ", ".join(failures) if failures else "ALL OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
