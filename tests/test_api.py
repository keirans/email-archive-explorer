"""Integration tests for Phase 2: FastAPI Backend REST API (demo archive)."""

import pytest
from conftest import local_client

from email_archive_explorer.api import create_app


@pytest.fixture(scope="session")
def client(tmp_path_factory, demo_eml):
    app = create_app(archive_path=demo_eml, cache_dir=tmp_path_factory.mktemp("api_test"))
    with local_client(app) as test_client:
        yield test_client


def _first_folder_with_messages(nodes):
    for node in nodes:
        if node["message_count"] > 0:
            return node["id"]
        found = _first_folder_with_messages(node["children"])
        if found:
            return found
    return None


def test_api_archive_info(client, demo_counts):
    response = client.get("/api/archive/info")
    assert response.status_code == 200
    data = response.json()
    assert data["total_messages"] == demo_counts["messages"]
    assert data["total_attachments"] == demo_counts["attachments"]
    assert data["total_folders"] >= len(demo_counts["folders"])
    assert data["file_path"].endswith("larkspur-mail")
    assert data["format"] == "EML Directory"


def test_api_folders(client):
    response = client.get("/api/folders")
    assert response.status_code == 200
    tree = response.json()
    assert [f["name"] for f in tree] == ["larkspur-mail"]
    children = {c["name"] for c in tree[0]["children"]}
    assert {"Inbox", "Sent Items", "Projects"} <= children


def test_api_folder_messages(client):
    tree = client.get("/api/folders?include_empty=false").json()
    target_folder_id = _first_folder_with_messages(tree)
    assert target_folder_id is not None

    response = client.get(f"/api/folders/{target_folder_id}/messages?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert data["folder_id"] == target_folder_id
    assert data["total"] > 0
    assert len(data["items"]) <= 10
    msg = data["items"][0]
    assert {"id", "subject", "sender_name", "thread_size"} <= set(msg)


def test_api_message_detail(client):
    folder_id = _first_folder_with_messages(client.get("/api/folders?include_empty=false").json())
    msg_id = client.get(f"/api/folders/{folder_id}/messages?page=1&page_size=1").json()["items"][0]["id"]

    response = client.get(f"/api/messages/{msg_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["id"] == msg_id
    assert detail["body_plain"]
    assert isinstance(detail["recipients"], list)
    assert isinstance(detail["attachments"], list)
    # Conversation signals stay internal
    assert "references" not in detail and "conversation_key" not in detail


def test_api_download_attachment(client):
    results = client.get("/api/search?q=calibration&has_attachments=true&page_size=20").json()["results"]
    target = next(r["id"] for r in results if client.get(f"/api/messages/{r['id']}").json()["attachments"])
    att = client.get(f"/api/messages/{target}").json()["attachments"][0]

    view_resp = client.get(f"/api/messages/{target}/attachments/{att['id']}")
    assert view_resp.status_code == 200
    assert len(view_resp.content) == att["size_bytes"]
    assert "inline" in view_resp.headers.get("content-disposition", "")

    dl_resp = client.get(f"/api/messages/{target}/attachments/{att['id']}?download=1")
    assert dl_resp.status_code == 200
    assert "attachment" in dl_resp.headers.get("content-disposition", "")


def test_api_export_eml(client):
    response = client.get("/api/messages/1/eml")
    assert response.status_code == 200
    assert "message/rfc822" in response.headers.get("content-type", "")
    assert ".eml" in response.headers.get("content-disposition", "")
    assert b"From:" in response.content and b"Subject:" in response.content


def test_api_search(client):
    response = client.get("/api/search?q=Saltmarsh")
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "Saltmarsh"
    assert data["total"] > 40
    res = data["results"][0]
    assert "<mark>" in res["snippet"] or "Saltmarsh" in res["subject"]


def test_api_serve_frontend(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Email Archive Explorer" in response.text
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


def test_api_404_handling(client):
    assert client.get("/api/messages/9999999").status_code == 404
    assert client.get("/api/folders/9999999/messages").status_code == 404
    assert client.get("/api/messages/1/attachments/9999999").status_code == 404


def test_api_no_archive_open(tmp_path):
    """`serve` with no archive: the UI loads and data routes say so clearly."""
    app = create_app(cache_dir=tmp_path)
    with local_client(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/archives").json()["active_archive_id"] is None
        r = c.get("/api/archive/info")
        assert r.status_code == 503 and "No archive" in r.json()["detail"]
