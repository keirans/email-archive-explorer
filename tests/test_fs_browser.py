"""Spec 08 §3.2–3.3: confined file browser and native dialog."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest
from conftest import local_client
from test_drivers import _plain

import email_archive_explorer.api as api_mod
import email_archive_explorer.fs_browser as fsb
from email_archive_explorer.api import create_app
from email_archive_explorer.fs_browser import MAX_ENTRIES, FileSystemBrowser
from email_archive_explorer.manager import ArchiveManager


@pytest.fixture()
def data(tmp_path):
    d = tmp_path / "data"
    (d / "work").mkdir(parents=True)
    (d / "work" / "old.pst").write_bytes(b"!BDN" + b"\0" * 60)
    (d / "Sent").write_bytes(b"From a@b Mon Jan  1 00:00:00 2001\nSubject: x\n\nhi\n")  # mbox, no extension
    (d / "notes.txt").write_text("hello")
    (d / "Exported").mkdir()
    (d / "Exported" / "1.eml").write_bytes(_plain("s", "b").as_bytes())
    (d / ".hidden").mkdir()
    (d / "Export2" / "Inbox").mkdir(parents=True)
    (d / "Export2" / "Inbox" / "1.eml").write_bytes(_plain("s", "b").as_bytes())
    return d


@pytest.fixture()
def client(data, tmp_path):
    app = create_app(manager=ArchiveManager(tmp_path / "cache"), data_dir=data)
    with local_client(app) as c:
        yield c


def test_roots(client, data):
    r = client.get("/api/fs/roots").json()
    kinds = {x["kind"]: x["path"] for x in r["roots"]}
    assert kinds["home"] == str(os.path.realpath(os.path.expanduser("~")))
    assert kinds["data"] == str(data.resolve())
    assert "native_picker_available" in r and isinstance(r["recent"], list)


def test_listing_marks_archives(client, data):
    r = client.get("/api/fs/list", params={"path": str(data)}).json()
    by = {e["name"]: e for e in r["entries"]}
    assert [e["type"] for e in r["entries"]][:4] == ["dir", "dir", "dir", "file"]  # folders first
    assert by["Sent"]["archive"]["format_type"] == "mbox"
    assert by["Exported"]["archive"]["format_type"] == "eml_dir"
    assert by["Export2"]["archive"]["format_type"] == "eml_dir"  # .eml files one level down
    assert by["notes.txt"]["archive"] is None
    assert by["work"]["archive"] is None
    assert ".hidden" not in by
    assert r["parent"] is None  # data/ is a root
    assert r["breadcrumbs"][-1]["path"] == str(data.resolve())

    hidden = client.get("/api/fs/list", params={"path": str(data), "show_hidden": True}).json()
    assert ".hidden" in {e["name"] for e in hidden["entries"]}

    sub = client.get("/api/fs/list", params={"path": str(data / "work")}).json()
    assert sub["entries"][0]["archive"]["format_type"] == "pst"
    assert sub["parent"] == str(data.resolve())
    assert [c["name"] for c in sub["breadcrumbs"]][-1] == "work"


def test_confinement(client, data, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    assert client.get("/api/fs/list", params={"path": str(outside)}).status_code == 403
    assert client.get("/api/fs/list", params={"path": str(data / ".." / "outside")}).status_code == 403
    (data / "escape").symlink_to(outside)
    assert client.get("/api/fs/list", params={"path": str(data / "escape")}).status_code == 403
    assert client.get("/api/fs/list", params={"path": str(data / "missing")}).status_code == 404
    assert client.get("/api/fs/list", params={"path": str(data / "notes.txt")}).status_code == 400


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_subfolder_is_skipped(data):
    locked = data / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        r = FileSystemBrowser(data).list_dir(data)
        assert "locked" not in {e["name"] for e in r["entries"]}
        assert r["skipped"] >= 1
    finally:
        locked.chmod(0o755)


def test_truncation(tmp_path):
    big = tmp_path / "big"
    big.mkdir()
    for i in range(MAX_ENTRIES + 25):
        (big / f"f{i:05d}.txt").touch()
    r = FileSystemBrowser(big).list_dir(big)
    assert r["truncated"] and len(r["entries"]) == MAX_ENTRIES


def test_native_picker_unavailable_without_display(client, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(fsb.sys, "platform", "linux")
    assert fsb.picker_backend() is None
    assert client.get("/api/fs/roots").json()["native_picker_available"] is False
    assert client.post("/api/fs/native-pick", json={}).status_code == 501


@pytest.mark.parametrize("returncode,stdout,expected", [
    (0, "/home/x/Mail/old.pst\n", "/home/x/Mail/old.pst"),
    (1, "", None),  # user cancelled
])
def test_native_picker_kdialog(client, monkeypatch, returncode, stdout, expected):
    calls = []
    monkeypatch.setattr(fsb, "picker_backend", lambda: "kdialog")
    monkeypatch.setattr(api_mod, "picker_backend", lambda: "kdialog")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = client.post("/api/fs/native-pick", json={"mode": "file"})
    assert r.status_code == 200 and r.json() == {"path": expected}
    assert calls[0][0] == "kdialog" and "--getopenfilename" in calls[0]
    client.post("/api/fs/native-pick", json={"mode": "directory"})
    assert "--getexistingdirectory" in calls[1]


def test_native_picker_one_at_a_time(client, monkeypatch):
    monkeypatch.setattr(fsb, "picker_backend", lambda: "kdialog")
    assert fsb._picker_lock.acquire(blocking=False)
    try:
        assert client.post("/api/fs/native-pick", json={}).status_code == 409
    finally:
        fsb._picker_lock.release()
