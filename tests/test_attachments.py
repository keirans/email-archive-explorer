"""Tests for Phase 9: Attachment Explorer (Spec 09)."""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest
from conftest import local_client

from email_archive_explorer.api import create_app
from email_archive_explorer.attachments import (
    CATALOG_VERSION,
    categorize,
    ensure_attachment_catalog,
    file_ext,
    noise_reason,
    unique_zip_name,
)
from email_archive_explorer.indexer import build_index, get_db_connection
from email_archive_explorer.manager import ArchiveManager

PDF = b"%PDF-1.4\n" + os.urandom(300 * 1024)
GIF_SMALL = b"GIF89a" + b"\x00" * 2000
BMP_STATIONERY = b"BM" + b"\x11" * (20 * 1024)
BLOB = os.urandom(64 * 1024)
HTML_EVIL = b"<html><body><script>fetch('/api/archives')</script>hi</body></html>"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("toolkit/", "")
        zf.writestr("toolkit/readme.txt", "Install notes for the toolkit")
        zf.writestr("toolkit/../../evil.sh", "echo should never be written to disk")
    return buf.getvalue()


def _tar_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"col1,col2\n1,2\n"
        info = tarfile.TarInfo("export/data.csv")
        info.size = len(data)
        info.mtime = 1_000_000_000
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ZIP = _zip_bytes()
TGZ = _tar_bytes()


def _msg(subject: str, date: str, sender: str, attachments) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "Sam Roe <sam@example.com>"
    m["Subject"] = subject
    m["Date"] = date
    m.set_content(f"Body of {subject}")
    for name, data, mime in attachments:
        maintype, subtype = mime.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


@pytest.fixture(scope="module")
def archive(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("att") / "Mail"
    inbox, projects, sent = root / "Inbox", root / "Inbox" / "Projects", root / "Sent"
    for d in (inbox, projects, sent):
        d.mkdir(parents=True)

    stationery = ("Glacier Bkgrd.bmp", BMP_STATIONERY, "image/bmp")
    (inbox / "01.eml").write_bytes(_msg(
        "Quarterly report", "Thu, 01 Feb 2001 09:00:00 +0000", "Dana Rourke <dana@example.com>",
        [("report.pdf", PDF, "application/pdf"),
         ("logo.gif", GIF_SMALL, "image/gif"),
         ("Toolkit.zip", ZIP, "application/zip"),
         stationery]))
    (sent / "02.eml").write_bytes(_msg(
        "FW: Quarterly report", "Mon, 04 Mar 2002 09:00:00 +0000", "Sam Roe <sam@example.com>",
        [("REPORT.pdf", PDF, "application/pdf"),
         ("InterScan_Disclaimer.txt", b"This email was scanned", "text/plain"),
         ("ATT00001.htm", b"<p>footer</p>", "text/html"),
         stationery]))
    (projects / "03.eml").write_bytes(_msg(
        "Project data", "Tue, 05 Aug 2003 09:00:00 +0000", "Paul <paul@example.com>",
        [("data.tar.gz", TGZ, "application/gzip"),
         ("page.html", HTML_EVIL, "text/html"),
         ("blob", BLOB, "application/octet-stream"),
         ("scan", PDF[:5000], "application/pdf"),
         ("broken.zip", b"PK\x03\x04 definitely not a zip", "application/zip"),
         stationery]))
    for i in range(4, 6):
        (inbox / f"0{i}.eml").write_bytes(_msg(
            f"Newsletter {i}", f"Wed, 0{i} Sep 2003 09:00:00 +0000", "news@example.org", [stationery]))
    return root


@pytest.fixture(scope="module")
def client(archive, tmp_path_factory):
    mgr = ArchiveManager(tmp_path_factory.mktemp("att_cache"))
    mgr.open_archive(archive)
    with local_client(create_app(manager=mgr)) as c:
        yield c


def _all(client, **params):
    params.setdefault("page_size", 200)
    resp = client.get("/api/attachments", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_name(items):
    return {i["filename"]: i for i in items}


# ---------------------------------------------------------------------------
# 1. Classification & catalog
# ---------------------------------------------------------------------------


def test_classification_helpers():
    assert file_ext("Report.PDF") == "pdf"
    assert file_ext("data.tar.gz") == "tgz"
    assert file_ext("Outlook..bmp") == "bmp"
    assert file_ext("attachment_2") == ""
    assert categorize("ppt", None) == "presentations"
    assert categorize("", "application/pdf") == "documents"
    assert categorize("", "application/octet-stream") == "other"
    assert noise_reason("logo.gif", "gif", "images", 2000, 1) == "small-image"
    assert noise_reason("Outlook..bmp", "bmp", "images", 960054, 19) == "repeated-image"
    assert noise_reason("ATT00001.htm", "htm", "web_code", 300, 1) == "outlook-artifact"
    assert noise_reason("winmail.dat", "dat", "other", 90000, 1) == "outlook-artifact"
    assert noise_reason("image001.png", "png", "images", 30000, 1) == "outlook-artifact"
    assert noise_reason("InterScan_Disclaimer.txt", "txt", "documents", 400, 4) == "mail-footer"
    assert noise_reason("Field trial data.zip", "zip", "archives", 12959696, 1) is None
    assert noise_reason("attachment_2", "", "other", 100, 6) is None


def test_catalog_categories_noise_and_duplicates(client):
    items = _by_name(_all(client, hide_noise=False, collapse=False)["items"])
    assert len(items) == 16 - 4  # 16 attachments; the 5 stationery copies share one name
    assert items["report.pdf"]["category"] == "documents"
    assert items["Toolkit.zip"]["category"] == "archives"
    assert items["scan"]["category"] == "documents"  # MIME fallback
    assert items["blob"]["category"] == "other" and not items["blob"]["is_noise"]

    assert items["logo.gif"]["noise_reason"] == "small-image"
    assert items["Glacier Bkgrd.bmp"]["noise_reason"] == "repeated-image"
    assert items["Glacier Bkgrd.bmp"]["copies"] == 5
    assert items["ATT00001.htm"]["noise_reason"] == "outlook-artifact"
    assert items["InterScan_Disclaimer.txt"]["noise_reason"] == "mail-footer"
    assert not items["Toolkit.zip"]["is_noise"]

    # Duplicate matching ignores filename case
    assert items["report.pdf"]["copies"] == 2


def test_catalog_rebuilds_for_existing_caches(archive, tmp_path):
    db = tmp_path / "c.db"
    build_index(archive, db)
    conn = get_db_connection(db)
    conn.execute("DROP TABLE attachment_catalog")
    conn.execute("DELETE FROM archive_meta WHERE key LIKE 'attachment_catalog%'")
    conn.commit()
    ensure_attachment_catalog(conn)  # old cache → built on first use
    n = conn.execute("SELECT COUNT(*) FROM attachment_catalog").fetchone()[0]
    assert n == conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    assert conn.execute(
        "SELECT value FROM archive_meta WHERE key = 'attachment_catalog_version'"
    ).fetchone()[0] == CATALOG_VERSION

    conn.execute("UPDATE archive_meta SET value = '0' WHERE key = 'attachment_catalog_version'")
    conn.execute("DELETE FROM attachment_catalog WHERE attachment_id = 1")
    conn.commit()
    ensure_attachment_catalog(conn)  # stale version → rebuilt
    assert conn.execute("SELECT COUNT(*) FROM attachment_catalog").fetchone()[0] == n
    conn.close()


# ---------------------------------------------------------------------------
# 2. Listing, filters, collapse, sort
# ---------------------------------------------------------------------------


def test_default_listing_hides_noise_and_collapses(client):
    data = _all(client)
    names = [i["filename"] for i in data["items"]]
    assert "logo.gif" not in names and "Glacier Bkgrd.bmp" not in names
    # One row for the two report copies: the earliest (2001) original
    reports = [i for i in data["items"] if i["filename"].lower() == "report.pdf"]
    assert len(reports) == 1
    assert reports[0]["filename"] == "report.pdf"
    assert reports[0]["message"]["subject"] == "Quarterly report"
    assert reports[0]["copies"] == 2 and reports[0]["copies_matching"] == 2
    assert data["total"] == len(data["items"]) == 7
    assert data["total_bytes"] == sum(i["size_bytes"] for i in data["items"])


def test_filters(client, archive):
    assert {i["filename"] for i in _all(client, category="archives")["items"]} == {"Toolkit.zip", "data.tar.gz", "broken.zip"}
    assert [i["filename"] for i in _all(client, ext="tgz")["items"]] == ["data.tar.gz"]
    assert {i["filename"] for i in _all(client, q="tool")["items"]} == {"Toolkit.zip"}
    assert {i["filename"] for i in _all(client, q="%")["items"]} == set()  # LIKE wildcards escaped
    assert {i["message"]["subject"] for i in _all(client, year="2003")["items"]} == {"Project data"}
    assert {i["filename"] for i in _all(client, sender="dana")["items"]} == {"report.pdf", "Toolkit.zip"}
    assert {i["filename"] for i in _all(client, min_size=100_000)["items"]} == {"report.pdf"}
    assert all(i["message"]["date_iso"] <= "2002-12-31T23:59:59" for i in _all(client, date_to="2002-12-31")["items"])

    # folder_id includes sub-folders: Inbox covers Inbox/Projects
    folders = {f["full_path"]: f["id"] for f in _flatten(client.get("/api/folders?include_empty=true").json())}
    inbox = _all(client, folder_id=folders["Mail/Inbox"], hide_noise=False, collapse=False)["items"]
    assert {i["message"]["folder_path"] for i in inbox} == {"Mail/Inbox", "Mail/Inbox/Projects"}
    sent = _all(client, folder_id=folders["Mail/Sent"])["items"]
    # Filtering before collapsing: the Sent copy is the representative within Sent
    assert [i["filename"] for i in sent] == ["REPORT.pdf"]
    assert sent[0]["copies"] == 2 and sent[0]["copies_matching"] == 1


def _flatten(nodes):
    for n in nodes:
        yield n
        yield from _flatten(n["children"])


def test_sorting(client):
    by_size = [i["size_bytes"] for i in _all(client, sort="size", order="desc")["items"]]
    assert by_size == sorted(by_size, reverse=True)
    by_name = [i["filename"].lower() for i in _all(client, sort="name", order="asc")["items"]]
    assert by_name == sorted(by_name)
    by_copies = [i["copies"] for i in _all(client, sort="copies", order="desc", hide_noise=False)["items"]]
    assert by_copies[0] == 5
    dates = [i["message"]["date_iso"] for i in _all(client, sort="date", order="asc")["items"]]
    assert dates == sorted(dates)
    assert client.get("/api/attachments?sort=bogus").status_code == 422


def test_pagination(client):
    p1 = _all(client, page_size=3, page=1, sort="name", order="asc")
    p3 = _all(client, page_size=3, page=3, sort="name", order="asc")
    assert p1["total_pages"] == 3 and len(p1["items"]) == 3 and len(p3["items"]) == 1


def test_rare_only(client):
    exts = {i["ext"] for i in _all(client, rare_only=True)["items"]}
    assert "zip" in exts  # 2 zips → still "rare" (≤ 2 unique)
    assert "" not in exts  # extension-less files are not a "type"


# ---------------------------------------------------------------------------
# 3. Summary & detail
# ---------------------------------------------------------------------------


def test_summary(client):
    s = client.get("/api/attachments/summary").json()
    assert s["total_count"] == 16
    assert s["noise_count"] == 8
    reasons = {n["reason"]: n["count"] for n in s["noise"]}
    assert reasons == {"repeated-image": 5, "outlook-artifact": 1, "mail-footer": 1, "small-image": 1}
    assert s["visible_count"] == s["total_count"] - s["noise_count"]
    assert s["largest"][0]["filename"] == "report.pdf"
    assert s["most_forwarded"][0]["copies"] == 2
    assert {y["year"] for y in s["years"]} == {"2001", "2002", "2003"}
    cats = {c["category"]: c["count"] for c in s["categories"]}
    assert cats["archives"] == 3
    with_noise = client.get("/api/attachments/summary?hide_noise=false").json()
    assert with_noise["visible_count"] == with_noise["total_count"]


def test_detail_origin_and_copies(client):
    sent_copy = _by_name(_all(client, hide_noise=False, collapse=False)["items"])["REPORT.pdf"]
    d = client.get(f"/api/attachments/{sent_copy['id']}").json()
    assert d["origin"]["subject"] == "FW: Quarterly report"
    assert d["origin"]["folder_path"] == "Mail/Sent"
    assert d["origin"]["recipients"][0]["email"] == "sam@example.com"
    assert d["origin"]["attachment_count"] == 4
    assert [c["message"]["subject"] for c in d["copies_list"]] == ["Quarterly report", "FW: Quarterly report"]
    assert d["first_seen"]["message"]["sender_name"] == "Dana Rourke"
    assert d["preview"] == "pdf"
    assert client.get("/api/attachments/999999").status_code == 404


# ---------------------------------------------------------------------------
# 4. Archive contents
# ---------------------------------------------------------------------------


def test_zip_contents_and_member_download(client):
    z = _by_name(_all(client)["items"])["Toolkit.zip"]
    assert z["preview"] == "archive"
    listing = client.get(f"/api/attachments/{z['id']}/contents").json()
    assert listing["kind"] == "zip" and listing["file_count"] == 2
    names = [e["name"] for e in listing["entries"]]
    assert "toolkit/readme.txt" in names
    readme = next(e for e in listing["entries"] if e["name"] == "toolkit/readme.txt")
    resp = client.get(f"/api/attachments/{z['id']}/contents/{readme['index']}")
    assert resp.status_code == 200
    assert resp.content == b"Install notes for the toolkit"
    assert 'filename="readme.txt"' in resp.headers["content-disposition"]

    # A traversal-style member name is reduced to its base name, never a path
    evil = next(e for e in listing["entries"] if "evil" in e["name"])
    resp = client.get(f"/api/attachments/{z['id']}/contents/{evil['index']}")
    assert 'filename="evil.sh"' in resp.headers["content-disposition"]

    directory = next(e for e in listing["entries"] if e["is_dir"])
    assert client.get(f"/api/attachments/{z['id']}/contents/{directory['index']}").status_code == 404
    assert client.get(f"/api/attachments/{z['id']}/contents/99").status_code == 404


def test_tar_contents(client):
    t = _by_name(_all(client)["items"])["data.tar.gz"]
    listing = client.get(f"/api/attachments/{t['id']}/contents").json()
    assert listing["kind"] == "tar"
    assert listing["entries"][0]["name"] == "export/data.csv"
    assert listing["entries"][0]["modified_iso"].startswith("2001-09-09")
    assert client.get(f"/api/attachments/{t['id']}/contents/0").content == b"col1,col2\n1,2\n"


def test_contents_of_non_archive(client):
    items = _by_name(_all(client)["items"])
    assert client.get(f"/api/attachments/{items['broken.zip']['id']}/contents").status_code == 422
    assert client.get(f"/api/attachments/{items['blob']['id']}/contents").status_code == 422


# ---------------------------------------------------------------------------
# 5. Bulk ZIP
# ---------------------------------------------------------------------------


def test_bulk_zip(client):
    items = _by_name(_all(client, collapse=False)["items"])
    ids = [items["report.pdf"]["id"], items["REPORT.pdf"]["id"], items["Toolkit.zip"]["id"]]
    resp = client.post("/api/attachments/zip", json={"ids": ids})
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.namelist() == ["report.pdf", "REPORT (2).pdf", "Toolkit.zip"]
    assert zf.read("report.pdf") == PDF
    assert zf.read("Toolkit.zip") == ZIP

    assert client.post("/api/attachments/zip", json={"ids": []}).status_code == 400
    assert client.post("/api/attachments/zip", json={"ids": list(range(1, 502))}).status_code == 400


def test_unique_zip_name():
    used: set = set()
    assert [unique_zip_name(n, used) for n in ["a.txt", "A.txt", "a.txt", "noext", "noext", "../x/y.doc"]] == \
        ["a.txt", "A (2).txt", "a (3).txt", "noext", "noext (2)", "y.doc"]


# ---------------------------------------------------------------------------
# 6. Security headers & navigation helper
# ---------------------------------------------------------------------------


def test_inline_html_attachment_is_sandboxed(client):
    items = _by_name(_all(client)["items"])
    html = client.get(items["page.html"]["view_url"])
    assert html.headers["content-security-policy"] == "sandbox"
    assert html.headers["x-content-type-options"] == "nosniff"
    assert items["page.html"]["preview"] == "html"

    pdf = client.get(items["report.pdf"]["view_url"])
    assert pdf.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in pdf.headers  # would break the browser PDF viewer


def test_message_position(client):
    folders = {f["full_path"]: f["id"] for f in _flatten(client.get("/api/folders?include_empty=true").json())}
    listing = client.get(f"/api/folders/{folders['Mail/Inbox']}/messages?page_size=1&sort_by=date&sort_order=desc").json()
    for page in range(1, listing["total_pages"] + 1):
        msg = client.get(
            f"/api/folders/{folders['Mail/Inbox']}/messages?page_size=1&page={page}&sort_by=date&sort_order=desc"
        ).json()["items"][0]
        pos = client.get(f"/api/messages/{msg['id']}/position?page_size=1&sort_by=date&sort_order=desc").json()
        assert pos == {"folder_id": folders["Mail/Inbox"], "index": page - 1, "page": page}
    assert client.get("/api/messages/999999/position").status_code == 404


# ---------------------------------------------------------------------------
# 7. Demo archive: the planted gems and noise are found
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo_client(demo_eml, tmp_path_factory):
    app = create_app(archive_path=demo_eml, cache_dir=tmp_path_factory.mktemp("demo_att"))
    with local_client(app) as c:
        yield c


def test_demo_archive_gems(demo_client, demo_counts):
    s = demo_client.get("/api/attachments/summary").json()
    assert s["total_count"] == demo_counts["attachments"]
    assert s["largest"][0]["filename"] == "heron-firmware-2.4.1.zip"
    assert s["most_forwarded"][0]["filename"] == "Tidewater TS-40 spec sheet v3.pdf"

    # The repeated signature logo is noise, hidden by default
    noisy = _all(demo_client, q="larkspur-logo", hide_noise=False, collapse=False)["items"]
    assert len(noisy) >= 5 and all(i["is_noise"] for i in noisy)
    assert not _all(demo_client, q="larkspur-logo")["items"]

    z = next(i for i in s["largest"] if i["filename"] == "Tidewater field trial data.zip")
    listing = demo_client.get(f"/api/attachments/{z['id']}/contents").json()
    assert listing["kind"] == "zip" and listing["file_count"] == 6

    d = demo_client.get(f"/api/attachments/{z['id']}").json()
    assert d["origin"]["folder_path"].endswith("Projects/Tidewater Sensor/Field Trials")
