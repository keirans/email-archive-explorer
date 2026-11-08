"""Tests for Phase 6: Multi-Archive Manager & File Open Menu (Spec 06)."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from conftest import local_client

from email_archive_explorer.api import create_app
from email_archive_explorer.indexer import get_db_connection
from email_archive_explorer.manager import ArchiveManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def two_archives(demo_mbox, tmp_path_factory):
    """Two distinct archive files: the demo MBOX and a copy at another path
    (distinct path → distinct archive ID and cache)."""
    work = tmp_path_factory.mktemp("two_archives")
    a = work / "First_Archive.mbox"
    b = work / "Second_Archive.mbox"
    shutil.copy2(demo_mbox, a)
    shutil.copy2(demo_mbox, b)
    return a.resolve(), b.resolve()


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("archive_mgr_cache")


@pytest.fixture(scope="module")
def client(two_archives, cache_dir):
    a, _ = two_archives
    app = create_app(archive_path=a, cache_dir=cache_dir)
    with local_client(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Discovery
# ---------------------------------------------------------------------------


def test_scan_discovers_archives(tmp_path):
    data = tmp_path / "data"
    (data / "work").mkdir(parents=True)
    (data / "personal" / "old").mkdir(parents=True)
    (data / "work" / "one.pst").write_bytes(b"!BDN")
    (data / "personal" / "old" / "TWO.PST").write_bytes(b"!BDN")
    (data / "notes.txt").write_text("not an archive")

    mgr = ArchiveManager(tmp_path / "cache")

    result = mgr.scan_directory(data, recursive=True)
    assert result["discovered_count"] == 2
    assert sorted(Path(p).name for p in result["newly_added"]) == ["TWO.PST", "one.pst"]

    # Re-scan finds the same files, but nothing new
    again = mgr.scan_directory(data, recursive=True)
    assert again["discovered_count"] == 2
    assert again["newly_added"] == []

    # Non-recursive scan only sees the top level
    assert mgr.scan_directory(data / "work", recursive=False)["discovered_count"] == 1

    known = {a.name for a in mgr.get_known_archives()}
    assert known == {"one.pst", "TWO.PST"}
    assert all(not a.is_active for a in mgr.get_known_archives())

    # Scanned directories are remembered in the catalog
    conn = sqlite3.connect(tmp_path / "cache" / "catalog.db")
    dirs = {row[0] for row in conn.execute("SELECT path FROM scan_directories")}
    conn.close()
    assert str(data.resolve()) in dirs


def test_scan_missing_directory(tmp_path):
    mgr = ArchiveManager(tmp_path / "cache")
    assert mgr.scan_directory(tmp_path / "nope") == {"discovered_count": 0, "newly_added": []}


def test_api_scan_endpoint(client, tmp_path):
    (tmp_path / "x.pst").write_bytes(b"!BDN")
    resp = client.post("/api/archives/scan", json={"directory": str(tmp_path), "recursive": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["discovered_count"] == 1
    assert body["newly_added"] == [str((tmp_path / "x.pst").resolve())]

    names = [a["name"] for a in client.get("/api/archives").json()["archives"]]
    assert "x.pst" in names


# ---------------------------------------------------------------------------
# 2. Switching
# ---------------------------------------------------------------------------


def test_list_archives_marks_active(client, two_archives):
    a, _ = two_archives
    data = client.get("/api/archives").json()
    active = [x for x in data["archives"] if x["is_active"]]
    assert len(active) == 1
    assert active[0]["id"] == data["active_archive_id"]
    assert active[0]["file_path"] == str(a)
    assert active[0]["total_messages"] > 0
    assert active[0]["last_opened_iso"]


def test_switch_archive(client, two_archives):
    a, b = two_archives
    original_id = client.get("/api/archives").json()["active_archive_id"]

    resp = client.post("/api/archives/open", json={"file_path": str(b), "wait": True})
    assert resp.status_code == 200, resp.text
    entry = resp.json()
    assert entry["is_active"] is True
    assert entry["name"] == "Second_Archive.mbox"
    assert entry["id"] != original_id

    listing = client.get("/api/archives").json()
    assert listing["active_archive_id"] == entry["id"]
    assert [x["file_path"] for x in listing["archives"] if x["is_active"]] == [str(b)]

    info = client.get("/api/archive/info").json()
    assert info["file_path"] == str(b)
    assert info["total_messages"] == entry["total_messages"]

    folders = client.get("/api/folders")
    assert folders.status_code == 200
    assert len(folders.json()) >= 1

    # Switch back
    resp = client.post("/api/archives/open", json={"file_path": str(a)})
    assert resp.status_code == 200
    assert client.get("/api/archive/info").json()["file_path"] == str(a)


def test_reopen_active_archive_is_noop(client, two_archives):
    a, _ = two_archives
    before = client.get("/api/archives").json()["active_archive_id"]
    resp = client.post("/api/archives/open", json={"file_path": str(a)})
    assert resp.status_code == 200
    assert resp.json()["id"] == before
    assert client.get("/api/folders").status_code == 200


def test_open_archive_errors(client, tmp_path):
    before = client.get("/api/archives").json()["active_archive_id"]

    resp = client.post("/api/archives/open", json={"file_path": str(tmp_path / "missing.pst")})
    assert resp.status_code == 404

    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    resp = client.post("/api/archives/open", json={"file_path": str(txt)})
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]

    resp = client.post("/api/archives/open", json={"file_path": str(tmp_path)})
    assert resp.status_code == 400

    # A failed open leaves the active archive untouched
    assert client.get("/api/archives").json()["active_archive_id"] == before
    assert client.get("/api/folders").status_code == 200


# ---------------------------------------------------------------------------
# 3. Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_requests_during_switching(two_archives, cache_dir):
    """Readers hammer the active archive while another thread keeps switching."""
    a, b = two_archives
    mgr = ArchiveManager(cache_dir)
    mgr.open_archive(a)
    mgr.open_archive(b)  # both caches now built, so switches are fast
    mgr.open_archive(a)

    errors: list[BaseException] = []
    stop = threading.Event()
    reads = [0]

    def reader_loop():
        while not stop.is_set():
            try:
                with mgr.session() as active:
                    conn = get_db_connection(active.db_path)
                    row = conn.execute(
                        "SELECT id, folder_path, message_index FROM messages "
                        "ORDER BY id LIMIT 1 OFFSET ?",
                        (reads[0] % 20,),
                    ).fetchone()
                    conn.close()
                    detail = active.reader.get_message_detail(
                        row["folder_path"], row["message_index"], msg_id=row["id"]
                    )
                    assert detail.subject is not None
                    reads[0] += 1
            except BaseException as exc:  # noqa: BLE001 - surface anything
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader_loop) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        for i in range(10):
            mgr.open_archive(b if i % 2 == 0 else a)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)
        mgr.close()

    assert not errors, errors
    assert reads[0] > 0


# ---------------------------------------------------------------------------
# 4. Cache isolation
# ---------------------------------------------------------------------------


def test_cache_isolation(two_archives, tmp_path):
    a, b = two_archives
    cache = tmp_path / "cache"
    mgr = ArchiveManager(cache)

    entry_a = mgr.open_archive(a)
    db_a = mgr.get_active_db_path()
    entry_b = mgr.open_archive(b)
    db_b = mgr.get_active_db_path()
    mgr.close()

    assert entry_a.id != entry_b.id
    assert db_a != db_b
    assert db_a.name == f"cache_{entry_a.id}.db"
    assert db_b.name == f"cache_{entry_b.id}.db"
    assert db_a.parent == db_b.parent == cache.resolve()
    assert (cache / "catalog.db").exists()

    # Each cache holds exactly one archive's worth of data
    for db in (db_a, db_b):
        conn = get_db_connection(db)
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == entry_a.total_messages

    # Catalog has no message tables, and each archive is recorded once
    conn = sqlite3.connect(cache / "catalog.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = conn.execute("SELECT id FROM known_archives").fetchall()
    conn.close()
    assert "messages" not in tables
    assert sorted(r[0] for r in rows) == sorted([entry_a.id, entry_b.id])


def test_parallel_requests_share_threadpool(client):
    """Overlapping requests must not trip sqlite3's same-thread check (dependency and
    endpoint can run on different worker threads)."""
    from concurrent.futures import ThreadPoolExecutor

    urls = ["/api/folders", "/api/archive/info", "/api/attachments/summary", "/api/attachments?page_size=5"] * 6
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert codes == [200] * len(urls)
