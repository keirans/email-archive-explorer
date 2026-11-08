"""Unit tests for Phase 1: data model, archive reading and the indexer.

Index tests use the demo EML archive. PST reader tests need a real Outlook file
and run only when ``EAE_TEST_PST`` is set.
"""

import pytest

from email_archive_explorer.drivers.pst_driver import PstDriver
from email_archive_explorer.indexer import (
    build_index,
    get_archive_info,
    get_db_connection,
    get_folder_tree,
    get_messages,
    is_cache_valid,
    search_messages,
)


@pytest.fixture(scope="session")
def indexed_db(demo_eml, demo_counts, tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db_test") / "test_cache.db"
    assert build_index(demo_eml, db_path=db_path) == demo_counts["messages"]
    return db_path


# --- PST reader (opt-in: EAE_TEST_PST) -------------------------------------

def test_pst_reader_open_and_folders(real_pst):
    with PstDriver(real_pst) as reader:
        paths = reader.folder_paths
        assert paths, "a PST always has at least its root folder"
        assert len(set(paths)) == len(paths)


def test_pst_reader_message_and_attachments(real_pst):
    with PstDriver(real_pst) as reader:
        found_msg = found_att = False
        for path in reader.folder_paths:
            for idx in range(min(reader.get_folder_message_count(path), 50)):
                try:
                    detail = reader.get_message_detail(path, idx, msg_id=1)
                except Exception:
                    continue  # non-mail items (contacts, notes)
                found_msg = True
                assert detail.folder_path == path
                if detail.attachments:
                    fn, data = reader.get_attachment_bytes(path, idx, 0)
                    assert fn and len(data) == detail.attachments[0].size_bytes
                    found_att = True
                    break
            if found_att:
                break
        assert found_msg


def test_pst_index_and_threads(real_pst, tmp_path):
    db = tmp_path / "pst.db"
    n = build_index(real_pst, db)
    conn = get_db_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == n
    # every message belongs to exactly one stored conversation
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COALESCE(SUM(size), 0) FROM threads").fetchone()[0] == n
    conn.close()


# --- Indexer (demo archive) ------------------------------------------------

def test_indexer_cache_validation(demo_eml, indexed_db):
    assert is_cache_valid(demo_eml, indexed_db) is True


def test_indexer_archive_info(demo_eml, demo_counts, indexed_db):
    conn = get_db_connection(indexed_db)
    info = get_archive_info(conn, demo_eml)
    conn.close()

    assert info.total_messages == demo_counts["messages"]
    assert info.total_attachments == demo_counts["attachments"]
    assert info.total_folders >= len(demo_counts["folders"])
    assert info.date_range.start.startswith("2001")
    assert info.date_range.end[:4] in {"2005", "2006"}


def test_indexer_folder_tree(indexed_db):
    conn = get_db_connection(indexed_db)
    roots = get_folder_tree(conn, include_empty=True)
    conn.close()

    assert [r.name for r in roots] == ["larkspur-mail"]
    projects = next(c for c in roots[0].children if c.name == "Projects")
    rig = next(c for c in projects.children if c.name == "Saltmarsh Rig")
    assert rig.message_count > 40
    tide = next(c for c in projects.children if c.name == "Tidewater Sensor")
    assert [c.name for c in tide.children] == ["Field Trials"]


def test_indexer_get_messages_and_sorting(indexed_db):
    conn = get_db_connection(indexed_db)
    folder_id = conn.execute("SELECT id FROM folders WHERE message_count > 10 LIMIT 1").fetchone()["id"]

    total, msgs = get_messages(conn, folder_id=folder_id, page=1, page_size=10, sort_order="desc")
    _, asc = get_messages(conn, folder_id=folder_id, page=1, page_size=10, sort_order="asc")
    conn.close()

    assert total > 10 and len(msgs) == 10 and msgs[0].subject
    assert msgs[0].date_iso >= msgs[-1].date_iso
    assert asc[0].date_iso <= asc[-1].date_iso


def test_indexer_fts_search(indexed_db):
    conn = get_db_connection(indexed_db)
    total, results = search_messages(conn, "drift")
    conn.close()

    assert total > 0
    assert any("<mark>drift</mark>" in r.snippet.lower() for r in results)
