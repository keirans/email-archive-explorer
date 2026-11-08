"""Unit & Integration tests for Phase 5: Advanced Search, Threading, Zip Export & Analytics."""

import io
import zipfile

import pytest
from conftest import local_client

from email_archive_explorer.api import create_app
from email_archive_explorer.indexer import (
    get_db_connection,
    search_messages,
)


@pytest.fixture(scope="session")
def client(tmp_path_factory, demo_eml):
    temp_dir = tmp_path_factory.mktemp("adv_test")

    app = create_app(archive_path=demo_eml, cache_dir=temp_dir)
    with local_client(app) as test_client:
        yield test_client


def test_indexer_advanced_search_filters(client):
    conn = get_db_connection(client.app.state.manager.get_active_db_path())

    # Date filter test
    total, results = search_messages(conn, "", date_from="2003-01-01", date_to="2003-12-31")
    assert total > 0
    assert all("2003" in r.date_iso for r in results if r.date_iso)

    # Sender filter test
    total_sender, results_sender = search_messages(conn, "", sender="Kestrel")
    assert total_sender > 0
    assert all("KESTREL" in r.sender_name.upper() for r in results_sender)

    # Attachment extension filter test
    total_pdf, results_pdf = search_messages(conn, "", attachment_ext="pdf")
    assert total_pdf > 0
    assert all(r.has_attachments for r in results_pdf)

    conn.close()


def test_api_message_thread(client):
    # A message in a multi-message conversation
    conn = get_db_connection(client.app.state.manager.get_active_db_path())
    sample = conn.execute("SELECT id, size FROM threads WHERE size > 1 ORDER BY size DESC LIMIT 1").fetchone()
    conn.close()

    assert sample is not None and sample["size"] == 40   # the demo's long rig conversation
    msg_id = sample["id"]

    resp = client.get(f"/api/messages/{msg_id}/thread")
    assert resp.status_code == 200
    thread = resp.json()
    assert thread["size"] > 1 and thread["position"] == 1
    assert thread["method"] in {"headers", "conversation-index", "subject"}
    assert len(thread["items"]) == min(thread["size"], 50)
    # Chronological ordering
    dates = [m["date_iso"] for m in thread["items"] if m["date_iso"]]
    assert dates == sorted(dates)
    # Windowing
    small = client.get(f"/api/messages/{msg_id}/thread?limit=1").json()
    assert len(small["items"]) == 1 and small["items"][0]["id"] == msg_id


def test_api_archive_analytics(client):
    resp = client.get("/api/archive/analytics")
    assert resp.status_code == 200
    data = resp.json()
    assert "monthly" in data
    assert "top_senders" in data
    assert "top_attachments" in data
    assert len(data["monthly"]) >= 12
    assert len(data["top_senders"]) >= 5


def test_api_folder_zip_export(client):
    # Find a folder with a few messages
    conn = get_db_connection(client.app.state.manager.get_active_db_path())
    folder = conn.execute("SELECT id, message_count FROM folders WHERE message_count > 0 AND message_count <= 20 LIMIT 1").fetchone()
    conn.close()

    assert folder is not None
    folder_id = folder["id"]

    resp = client.get(f"/api/folders/{folder_id}/zip?limit=5")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    # Verify zip content
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    entries = zf.namelist()
    assert len(entries) > 0
    assert all(e.endswith(".eml") for e in entries)


def test_api_search_zip_export(client):
    resp = client.get("/api/search/zip?q=Saltmarsh&limit=5")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    entries = zf.namelist()
    assert len(entries) > 0
    assert all(e.endswith(".eml") for e in entries)
