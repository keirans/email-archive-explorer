"""Tests for Phase 7: Pluggable Archive Driver Architecture (Spec 07)."""

from __future__ import annotations

import inspect
import mailbox
from email.message import EmailMessage
from pathlib import Path

import pytest
from conftest import local_client

from email_archive_explorer.api import create_app
from email_archive_explorer.drivers import (
    ArchiveDriverRegistry,
    BaseArchiveDriver,
    EmlDirDriver,
    MboxDriver,
    PstDriver,
    detect_driver,
)
from email_archive_explorer.indexer import (
    build_index,
    get_archive_info,
    get_db_connection,
    get_folder_tree,
    is_cache_valid,
    search_messages,
)
from email_archive_explorer.manager import ArchiveManager

PDF_BYTES = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ---------------------------------------------------------------------------
# Synthetic archive builders
# ---------------------------------------------------------------------------


def _plain(subject: str, body: str, sender="Alice Example <alice@example.com>", date="Mon, 03 Mar 2003 10:00:00 +1100") -> EmailMessage:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "Bob Builder <bob@example.com>, carol@example.com"
    m["Cc"] = "Dave <dave@example.com>"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    m.set_content(body)
    return m


def _with_attachment() -> EmailMessage:
    m = _plain("Quarterly report", "Report attached.\nFrom the finance team.")
    m.add_attachment(PDF_BYTES, maintype="application", subtype="pdf", filename="report Q1.pdf")
    m.add_attachment(_plain("Original thread", "Inner message body"), filename=None)
    return m


def _html_only() -> EmailMessage:
    m = EmailMessage()
    m["From"] = "news@example.org"
    m["To"] = "bob@example.com"
    m["Subject"] = "=?utf-8?q?Caf=C3=A9_newsletter?="
    m["Date"] = "Tue, 04 Mar 2003 08:30:00 -0500"
    m.set_content("<html><body><p>Hello <b>reader</b>&amp; friends</p><style>p{}</style></body></html>", subtype="html")
    return m


def _alternative_with_image() -> EmailMessage:
    m = _plain("Photo from site visit", "Plain version of the photo email")
    m.add_alternative("<p>HTML version of the photo email</p>", subtype="html")
    m.add_attachment(PNG_BYTES, maintype="image", subtype="png", filename="site.png")
    return m


MESSAGES = [_with_attachment, _html_only, _alternative_with_image]


@pytest.fixture()
def mbox_file(tmp_path) -> Path:
    path = tmp_path / "Inbox.mbox"
    box = mailbox.mbox(str(path))
    for build in MESSAGES:
        box.add(build())
    box.flush()
    box.close()
    return path


@pytest.fixture()
def eml_dir(tmp_path) -> Path:
    root = tmp_path / "Exported Mail"
    (root / "Inbox").mkdir(parents=True)
    (root / "Projects" / "2003").mkdir(parents=True)
    (root / "Inbox" / "001.eml").write_bytes(_with_attachment().as_bytes())
    (root / "Inbox" / "002.eml").write_bytes(_html_only().as_bytes())
    (root / "Projects" / "2003" / "a.eml").write_bytes(_alternative_with_image().as_bytes())
    (root / "Inbox" / "notes.txt").write_text("ignored")
    return root


@pytest.fixture()
def maildir(tmp_path) -> Path:
    root = tmp_path / "Maildir"
    md = mailbox.Maildir(str(root))
    md.add(_plain("Maildir inbox message", "hello from cur"))
    sent = md.add_folder("Sent")
    sent.add(_plain("Maildir sent message", "hello from sent"))
    md.close()
    return root


# ---------------------------------------------------------------------------
# 1. Interface compliance
# ---------------------------------------------------------------------------


def test_builtin_drivers_registered_in_probe_order():
    assert ArchiveDriverRegistry.drivers()[:3] == [PstDriver, MboxDriver, EmlDirDriver]


@pytest.mark.parametrize("driver_cls", ArchiveDriverRegistry.drivers())
def test_driver_interface_compliance(driver_cls):
    assert issubclass(driver_cls, BaseArchiveDriver)
    assert not inspect.isabstract(driver_cls), driver_cls.__abstractmethods__
    for name in BaseArchiveDriver.__abstractmethods__:
        owner = next(k for k in driver_cls.__mro__ if name in k.__dict__)
        assert owner is not BaseArchiveDriver, f"{driver_cls.__name__} does not implement {name}"
    assert driver_cls.format_name()
    assert driver_cls.format_type != "unknown"
    assert all(e.startswith(".") and e == e.lower() for e in driver_cls.supported_extensions())


# ---------------------------------------------------------------------------
# 2. Probing accuracy
# ---------------------------------------------------------------------------


def test_probe_real_pst(real_pst):
    drv = detect_driver(real_pst)
    assert isinstance(drv, PstDriver)
    assert drv.describe().startswith("Outlook PST (")


def test_probe_mbox(mbox_file, tmp_path):
    assert isinstance(detect_driver(mbox_file), MboxDriver)
    # Thunderbird-style mbox with no extension is found by magic bytes alone
    bare = tmp_path / "Sent"
    bare.write_bytes(mbox_file.read_bytes())
    assert isinstance(detect_driver(bare), MboxDriver)


def test_probe_eml_dir_and_maildir(eml_dir, maildir):
    assert isinstance(detect_driver(eml_dir), EmlDirDriver)
    assert isinstance(detect_driver(eml_dir / "Inbox"), EmlDirDriver)
    assert isinstance(detect_driver(eml_dir / "Inbox" / "001.eml"), EmlDirDriver)
    assert isinstance(detect_driver(maildir), EmlDirDriver)


def test_probe_magic_beats_extension(mbox_file, tmp_path):
    misnamed = tmp_path / "actually_mbox.pst"
    misnamed.write_bytes(mbox_file.read_bytes())
    assert isinstance(detect_driver(misnamed), MboxDriver)


def test_probe_unsupported_raises_value_error(tmp_path):
    png = tmp_path / "picture.png"
    png.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="Unsupported archive format"):
        detect_driver(png)

    text = tmp_path / "letter.txt"
    text.write_text("From the desk of the CEO: hello")  # "From " but no envelope
    with pytest.raises(ValueError):
        detect_driver(text)

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ValueError):
        detect_driver(empty_dir)

    with pytest.raises(FileNotFoundError):
        detect_driver(tmp_path / "missing.mbox")


# ---------------------------------------------------------------------------
# 3. Pluggable indexing
# ---------------------------------------------------------------------------


def test_index_mbox(mbox_file, tmp_path):
    db = tmp_path / "mbox.db"
    assert build_index(mbox_file, db) == 3
    assert is_cache_valid(mbox_file, db)

    conn = get_db_connection(db)
    info = get_archive_info(conn, mbox_file)
    assert info.format == "MBOX Archive"
    assert info.total_folders == 1
    assert info.total_messages == 3
    assert info.total_attachments == 3  # pdf + attached email + png
    assert info.file_size_bytes == mbox_file.stat().st_size

    tree = get_folder_tree(conn, include_empty=True)
    assert [n.name for n in tree] == ["Inbox"]

    rows = conn.execute(
        "SELECT subject, sender_name, sender_email, date_iso, attachment_count, body_preview "
        "FROM messages ORDER BY message_index"
    ).fetchall()
    assert rows[0]["subject"] == "Quarterly report"
    assert rows[0]["sender_name"] == "Alice Example"
    assert rows[0]["sender_email"] == "alice@example.com"
    assert rows[0]["date_iso"] == "2003-03-02T23:00:00"  # normalised to UTC
    assert rows[0]["attachment_count"] == 2
    assert "From the finance team" in rows[0]["body_preview"]  # ">From" unescaped
    assert rows[1]["subject"] == "Café newsletter"
    assert rows[1]["sender_name"] == "news@example.org"
    assert "Hello reader& friends" in rows[1]["body_preview"]

    total, results = search_messages(conn, "newsletter")
    assert total == 1 and results[0].subject == "Café newsletter"
    total, _ = search_messages(conn, "", attachment_ext="pdf")
    assert total == 1
    conn.close()

    # Changing the archive invalidates the cache
    with open(mbox_file, "ab") as f:
        f.write(b"\nFrom x@y Thu Jan  1 00:00:00 2004\nSubject: late\n\nlate body\n")
    assert not is_cache_valid(mbox_file, db)
    assert build_index(mbox_file, db) == 4


def test_index_eml_dir(eml_dir, tmp_path):
    db = tmp_path / "eml.db"
    assert build_index(eml_dir, db) == 3

    conn = get_db_connection(db)
    tree = get_folder_tree(conn, include_empty=True)
    assert len(tree) == 1 and tree[0].name == "Exported Mail"
    children = {c.name: c for c in tree[0].children}
    assert set(children) == {"Inbox", "Projects"}
    assert children["Inbox"].message_count == 2
    assert children["Projects"].children[0].name == "2003"
    assert children["Projects"].children[0].full_path == "Exported Mail/Projects/2003"
    assert get_archive_info(conn, eml_dir).format == "EML Directory"
    conn.close()

    assert is_cache_valid(eml_dir, db)
    (eml_dir / "Inbox" / "003.eml").write_bytes(_plain("New", "new").as_bytes())
    assert not is_cache_valid(eml_dir, db)


def test_index_maildir(maildir, tmp_path):
    db = tmp_path / "md.db"
    assert build_index(maildir, db) == 2
    conn = get_db_connection(db)
    paths = {r[0]: r[1] for r in conn.execute("SELECT full_path, message_count FROM folders")}
    conn.close()
    assert paths == {"Maildir": 1, "Maildir/Sent": 1}


# ---------------------------------------------------------------------------
# 4. Attachment extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["mbox", "eml"])
def test_attachment_extraction(kind, mbox_file, eml_dir):
    if kind == "mbox":
        drv, folder = MboxDriver(mbox_file), "Inbox"
    else:
        drv, folder = EmlDirDriver(eml_dir), "Exported Mail/Inbox"

    with drv:
        detail = drv.get_message_detail(folder, 0, msg_id=7)
        assert [a.filename for a in detail.attachments] == ["report Q1.pdf", "Original thread.eml"]
        assert [a.mime_type for a in detail.attachments] == ["application/pdf", "message/rfc822"]
        assert detail.attachments[0].size_bytes == len(PDF_BYTES)
        assert detail.attachments[0].download_url == "/api/messages/7/attachments/1?download=1"

        name, data = drv.get_attachment_bytes(folder, 0, 0)
        assert (name, data) == ("report Q1.pdf", PDF_BYTES)

        name, data = drv.get_attachment_bytes(folder, 0, 1)
        assert name == "Original thread.eml"
        assert b"Inner message body" in data and b"Subject: Original thread" in data

        with pytest.raises(IndexError):
            drv.get_attachment_bytes(folder, 0, 5)

        assert detail.transport_headers and "Subject: Quarterly report" in detail.transport_headers
        assert {(r.email, r.recipient_type) for r in detail.recipients} == {
            ("bob@example.com", "To"), ("carol@example.com", "To"), ("dave@example.com", "Cc"),
        }


def test_alternative_bodies_and_inline_image(mbox_file):
    with MboxDriver(mbox_file) as drv:
        d = drv.get_message_detail("Inbox", 2)
    assert d.body_plain.startswith("Plain version")
    assert "HTML version" in d.body_html
    assert [(a.filename, a.mime_type) for a in d.attachments] == [("site.png", "image/png")]


def test_pst_attachment_extraction(real_pst):
    with PstDriver(real_pst) as drv:
        for folder in drv.get_folder_paths():
            for i in range(drv.get_folder_message_count(folder)):
                try:
                    d = drv.get_message_detail(folder, i)
                except Exception:
                    continue  # non-mail items
                if d.attachments:
                    name, data = drv.get_attachment_bytes(folder, i, 0)
                    assert name == d.attachments[0].filename
                    assert len(data) == d.attachments[0].size_bytes > 0
                    assert drv.get_raw_message(folder, i) is None
                    return
    pytest.skip("this PST has no attachments")


def test_mbox_driver_never_opens_for_writing(mbox_file):
    mbox_file.chmod(0o444)
    try:
        with MboxDriver(mbox_file) as drv:
            assert drv.get_folder_message_count("Inbox") == 3
    finally:
        mbox_file.chmod(0o644)


# ---------------------------------------------------------------------------
# Manager & API integration
# ---------------------------------------------------------------------------


def test_scan_discovers_all_formats(tmp_path, mbox_file, eml_dir, maildir):
    data = tmp_path / "data"
    (data / "mail").mkdir(parents=True)
    (data / "mail" / "Inbox.mbox").write_bytes(mbox_file.read_bytes())
    (data / "mail" / "Thunderbird_Sent").write_bytes(mbox_file.read_bytes())  # no extension
    (data / "mail" / "Inbox.mbox.msf").write_text("thunderbird index, not an archive")
    (data / "work").mkdir()
    (data / "work" / "fake.pst").write_bytes(b"!BDN" + b"\x00" * 60)
    eml_dir.rename(data / "Exported Mail")
    maildir.rename(data / "Maildir")
    (data / ".hidden").mkdir()
    (data / ".hidden" / "x.mbox").write_bytes(mbox_file.read_bytes())

    mgr = ArchiveManager(tmp_path / "cache")
    result = mgr.scan_directory(data)
    found = {Path(p).relative_to(data).as_posix() for p in result["newly_added"]}
    assert found == {
        "mail/Inbox.mbox",
        "mail/Thunderbird_Sent",
        "work/fake.pst",
        # EML folders are registered once each; their sub-folders are not
        "Exported Mail/Inbox",
        "Exported Mail/Projects/2003",
        "Maildir",
    }
    types = {a.name: a.format_type for a in mgr.get_known_archives()}
    assert types["Inbox.mbox"] == "mbox"
    assert types["fake.pst"] == "pst"
    assert types["Maildir"] == "eml_dir"


def test_api_serves_mbox_archive(mbox_file, tmp_path):
    cache = tmp_path / "cache"
    mgr = ArchiveManager(cache)
    mgr.open_archive(mbox_file)
    app = create_app(manager=mgr)
    with local_client(app) as client:
        info = client.get("/api/archive/info").json()
        assert info["format"] == "MBOX Archive"
        assert info["total_messages"] == 3

        archives = client.get("/api/archives").json()["archives"]
        assert archives[0]["format_type"] == "mbox"

        folder = client.get("/api/folders").json()[0]
        msgs = client.get(f"/api/folders/{folder['id']}/messages?sort_by=subject&sort_order=asc").json()
        target = next(m for m in msgs["items"] if m["subject"] == "Quarterly report")

        detail = client.get(f"/api/messages/{target['id']}").json()
        assert detail["sender_email"] == "alice@example.com"
        att = detail["attachments"][0]
        resp = client.get(att["download_url"])
        assert resp.status_code == 200
        assert resp.content == PDF_BYTES
        assert resp.headers["content-type"] == "application/pdf"

        # .eml export returns the original message bytes
        eml = client.get(f"/api/messages/{target['id']}/eml")
        assert eml.status_code == 200
        assert b"Subject: Quarterly report" in eml.content
        assert b"report Q1.pdf" in eml.content

        # Switching to an EML directory through the API also works
        eml_root = tmp_path / "eml_export"
        (eml_root / "Inbox").mkdir(parents=True)
        (eml_root / "Inbox" / "1.eml").write_bytes(_html_only().as_bytes())
        resp = client.post("/api/archives/open", json={"file_path": str(eml_root), "wait": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["format_type"] == "eml_dir"
        assert client.get("/api/archive/info").json()["total_messages"] == 1

        png = tmp_path / "p.png"
        png.write_bytes(PNG_BYTES)
        resp = client.post("/api/archives/open", json={"file_path": str(png)})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Unnamed PST attachments are identified by content (index_version 2)
# ---------------------------------------------------------------------------


class _FakeAtt:
    def __init__(self, data: bytes, name=None):
        self.data, self.name = data, name

    def get_long_filename(self):
        return self.name

    def get_size(self):
        return len(self.data)

    def read_buffer(self, n):
        return self.data[:n]


@pytest.mark.parametrize("data,name,expected", [
    (b"\x00\x00\xec\xbc" + b"\x00" * 60, None, ("Embedded Outlook item 3", "application/x-pst-embedded-item")),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "WordDocument".encode("utf-16-le"), None, ("embedded_object_3.doc", "application/msword")),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "Workbook".encode("utf-16-le"), None, ("embedded_object_3.xls", "application/vnd.ms-excel")),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, None, ("embedded_object_3.ole", "application/x-ole-storage")),
    (b"%PDF-1.3 ...", None, ("attachment_3.pdf", "application/pdf")),
    (b"\xff\xd8\xff\xe0JFIF", None, ("attachment_3.jpg", "image/jpeg")),
    (b"random bytes", None, ("attachment_3", None)),
    (b"%PDF", "Real Name.pdf", ("Real Name.pdf", "application/pdf")),
])
def test_pst_unnamed_attachment_sniffing(data, name, expected):
    assert PstDriver._attachment_name(_FakeAtt(data, name), 2) == expected


def test_driver_index_version_invalidates_cache(mbox_file, tmp_path, monkeypatch):
    db = tmp_path / "v.db"
    build_index(mbox_file, db)
    assert is_cache_valid(mbox_file, db)
    monkeypatch.setattr(MboxDriver, "index_version", "99")
    assert not is_cache_valid(mbox_file, db)
    build_index(mbox_file, db)
    assert is_cache_valid(mbox_file, db)
