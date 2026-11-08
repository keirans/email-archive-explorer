"""Spec 08 §3.5: Archive Inspector details and checksum."""

from __future__ import annotations

import hashlib
import mailbox

import pytest
from conftest import local_client
from test_drivers import MESSAGES, _plain

from email_archive_explorer.api import create_app
from email_archive_explorer.manager import ArchiveManager


def _items(details, title):
    section = next(s for s in details["sections"] if s["title"] == title)
    return {i["label"]: i["value"] for i in section["items"]}


@pytest.fixture(scope="module")
def pst(real_pst, tmp_path_factory):
    app = create_app(archive_path=real_pst, cache_dir=tmp_path_factory.mktemp("details"))
    with local_client(app) as c:
        yield c


def test_pst_details(pst):
    d = pst.get("/api/archive/details").json()
    titles = [s["title"] for s in d["sections"]]
    assert titles == ["File", "Format", "PST header", "Contents", "Search index"]
    header = _items(d, "PST header")
    assert header["Signature"].startswith("21 42 44 4E")
    assert header["File version"].endswith(("(ANSI)", "(Unicode)", "(Unicode, 4K pages)"))
    assert header["Page size"].endswith("bytes")
    assert header["Encryption"]
    info = pst.get("/api/archive/info").json()
    contents = _items(d, "Contents")
    assert contents["Messages"] == f"{info['total_messages']:,}"
    assert contents["Folders"] == f"{info['total_folders']:,}"
    index = _items(d, "Search index")
    assert index["Status"] == "Up to date"
    assert "s)" in index["Indexed"]  # index duration recorded
    assert _items(d, "File")["Opened"].startswith("Read-only")
    assert d["checksum"] is None


def test_checksum_matches_and_is_cached(pst, real_pst):
    r = pst.post("/api/archive/checksum")
    assert r.status_code == 200
    h = hashlib.sha256()
    with open(real_pst, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    assert r.json() == {"algorithm": "SHA-256", "value": h.hexdigest(), "scope": "file"}
    assert pst.get("/api/archive/details").json()["checksum"]["value"] == h.hexdigest()


def test_mbox_checksum_is_whole_file(demo_mbox, tmp_path):
    mgr = ArchiveManager(tmp_path / "cache")
    mgr.open_archive(demo_mbox)
    with local_client(create_app(manager=mgr)) as c:
        r = c.post("/api/archive/checksum").json()
    assert r == {"algorithm": "SHA-256", "value": hashlib.sha256(demo_mbox.read_bytes()).hexdigest(), "scope": "file"}


def test_mbox_and_eml_details(tmp_path):
    box_path = tmp_path / "Inbox.mbox"
    box = mailbox.mbox(str(box_path))
    for build in MESSAGES:
        box.add(build())
    box.close()
    eml = tmp_path / "Export"
    (eml / "Inbox").mkdir(parents=True)
    (eml / "Inbox" / "1.eml").write_bytes(_plain("a", "b").as_bytes())

    mgr = ArchiveManager(tmp_path / "cache")
    mgr.open_archive(box_path)
    with local_client(create_app(manager=mgr)) as c:
        d = c.get("/api/archive/details").json()
        mb = _items(d, "MBOX structure")
        assert mb["Messages"] == "3"
        assert mb["Line endings"] == "LF (Unix)"
        assert mb["First envelope"].startswith("From ")
        assert "quoting" in mb["From_ quoting"] or "Unquoted" in mb["From_ quoting"]
        assert _items(d, "Format")["Format"] == "MBOX Archive"

        assert c.post("/api/archives/open", json={"file_path": str(eml), "wait": True}).status_code == 200
        d = c.get("/api/archive/details").json()
        layout = _items(d, "EML layout")
        assert layout["Layout"] == "Folder tree of .eml files"
        assert layout["Message files"] == "1"
        r = c.post("/api/archive/checksum").json()
        assert r["scope"] == "manifest" and len(r["value"]) == 64
        # Details never include message bodies
        assert "b" not in [i["value"] for s in d["sections"] for i in s["items"] if i["label"] == "Body"]
