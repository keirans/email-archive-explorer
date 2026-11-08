"""Spec 08 §3.1: local API hardening."""

from __future__ import annotations

import mailbox

import pytest
from conftest import local_client
from starlette.testclient import TestClient
from test_drivers import MESSAGES

from email_archive_explorer.api import create_app
from email_archive_explorer.manager import ArchiveManager


@pytest.fixture()
def app(tmp_path):
    # Function-scoped: leaving a TestClient context shuts the manager down.
    root = tmp_path
    box = mailbox.mbox(str(root / "Inbox.mbox"))
    for build in MESSAGES:
        box.add(build())
    box.close()
    mgr = ArchiveManager(root / "cache")
    mgr.open_archive(root / "Inbox.mbox")
    return create_app(manager=mgr, data_dir=root)


def test_no_cors_headers(app):
    with local_client(app) as c:
        r = c.get("/api/folders", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers
        pre = c.options("/api/archives/open", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in pre.headers


@pytest.mark.parametrize("host,ok", [
    ("http://127.0.0.1:8000", True),
    ("http://localhost:8000", True),
    ("http://[::1]:8000", True),
    ("http://evil.example", False),        # DNS-rebinding style request
    ("http://127.0.0.1.evil.example", False),
])
def test_host_header_check(app, host, ok):
    with TestClient(app, base_url=host) as c:
        r = c.get("/api/archive/info")
        assert (r.status_code == 200) if ok else (r.status_code == 421)


def test_host_check_can_be_disabled(tmp_path):
    mgr = ArchiveManager(tmp_path)
    open_app = create_app(manager=mgr, allowed_hosts=None)
    with TestClient(open_app, base_url="http://my-laptop.lan") as c:
        assert c.get("/api/archives").status_code == 200


def test_token_required_for_state_changes(app):
    with local_client(app, token=False) as c:
        assert c.post("/api/archives/scan", json={}).status_code == 403
        assert c.post("/api/archives/open", json={"file_path": "/nope"}).status_code == 403
        assert c.post("/api/attachments/zip", json={"ids": [1]}).status_code == 403
        assert c.post("/api/archive/checksum").status_code == 403
        assert c.get("/api/fs/roots").status_code == 403
        assert c.get("/api/fs/list", params={"path": "/"}).status_code == 403
        r = c.post("/api/archives/scan", json={}, headers={"X-EAE-Token": "wrong"})
        assert r.status_code == 403
        # Read-only GETs stay token-free (they're protected by no-CORS + Host check)
        assert c.get("/api/folders").status_code == 200
    with local_client(app) as c:
        assert c.post("/api/archives/scan", json={"directory": "/nonexistent"}).status_code == 200


def test_index_html_carries_token(app):
    with local_client(app, token=False) as c:
        r = c.get("/")
        assert f'<meta name="eae-token" content="{app.state.token}">' in r.text
        assert r.headers["cache-control"] == "no-store"


def test_tokens_differ_per_process(tmp_path):
    a = create_app(manager=ArchiveManager(tmp_path / "a"))
    b = create_app(manager=ArchiveManager(tmp_path / "b"))
    assert a.state.token != b.state.token and len(a.state.token) >= 40


def test_fs_routes_can_be_disabled(tmp_path):
    remote = create_app(manager=ArchiveManager(tmp_path), fs_enabled=False)
    with local_client(remote) as c:
        assert c.get("/api/fs/roots").status_code == 403
        assert c.post("/api/fs/native-pick", json={}).status_code == 403
