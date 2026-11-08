"""SQLite + FTS5 indexing engine, format-agnostic via archive drivers (Spec 07)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from email_archive_explorer.attachments import ensure_attachment_catalog
from email_archive_explorer.config import DEFAULT_CACHE_DB, format_size
from email_archive_explorer.drivers import detect_driver
from email_archive_explorer.models import (
    ArchiveInfo,
    DateRange,
    FolderNode,
    MessageSummary,
    SearchResult,
    ThreadWindow,
)
from email_archive_explorer.threads import Thread, ThreadSignals, compute_threads


def get_db_connection(db_path: str | Path = DEFAULT_CACHE_DB) -> sqlite3.Connection:
    """Creates a configured SQLite connection with row factories and WAL mode.

    ``check_same_thread=False``: FastAPI may open a request's connection in one
    worker thread (the dependency) and use it in another (the endpoint). Each
    connection still serves a single request at a time, so this is safe.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initializes tables, indexes, and FTS5 virtual tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS archive_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            name TEXT NOT NULL,
            full_path TEXT NOT NULL UNIQUE,
            depth INTEGER NOT NULL,
            message_count INTEGER DEFAULT 0,
            FOREIGN KEY (parent_id) REFERENCES folders(id)
        );

        CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
        CREATE INDEX IF NOT EXISTS idx_folders_path ON folders(full_path);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            folder_path TEXT NOT NULL,
            message_index INTEGER NOT NULL,
            identifier INTEGER NOT NULL,
            subject TEXT,
            sender_name TEXT,
            sender_email TEXT,
            recipients_json TEXT,
            date_iso TEXT,
            has_attachments INTEGER DEFAULT 0,
            attachment_count INTEGER DEFAULT 0,
            body_preview TEXT,
            message_id_hdr TEXT,
            thread_id INTEGER,
            FOREIGN KEY (folder_id) REFERENCES folders(id)
        );

        CREATE TABLE IF NOT EXISTS threads (
            id INTEGER PRIMARY KEY,      -- id of the earliest message
            size INTEGER NOT NULL,
            first_date TEXT,
            last_date TEXT,
            subject TEXT,
            method TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_folder ON messages(folder_id);
        CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_iso DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_name);
        CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, date_iso);

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            attachment_index INTEGER NOT NULL,
            filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mime_type TEXT,
            FOREIGN KEY (message_id) REFERENCES messages(id)
        );

        CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            message_id UNINDEXED,
            subject,
            sender,
            recipients,
            body_plain,
            tokenize = 'porter unicode61'
        );
        """
    )
    conn.commit()


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(conn.execute("SELECT key, value FROM archive_meta").fetchall())
    except sqlite3.Error:
        return {}


def is_cache_valid(archive_path: str | Path, db_path: str | Path = DEFAULT_CACHE_DB) -> bool:
    """Checks if the SQLite cache is up-to-date with the archive (file or directory)."""
    src = Path(archive_path)
    db_p = Path(db_path)
    if not src.exists() or not db_p.exists():
        return False

    try:
        driver = detect_driver(src)
        size, mtime = driver.fingerprint()
        conn = get_db_connection(db_p)
        rows = _read_meta(conn)
        conn.close()

        cached_size = int(rows.get("pst_size", -1))
        cached_mtime = float(rows.get("pst_mtime", -1))
        cached_indexed = rows.get("indexed_complete", "false")

        return (
            cached_indexed == "true"
            and rows.get("driver_index_version", "1") == driver.index_version
            and cached_size == size
            and abs(cached_mtime - mtime) < 1.0
        )
    except Exception:
        return False


def store_threads(conn: sqlite3.Connection, threads: list[Thread]) -> None:
    """Write conversations to ``threads`` and ``messages.thread_id``."""
    conn.execute("DELETE FROM threads")
    conn.executemany(
        "INSERT INTO threads (id, size, first_date, last_date, subject, method) VALUES (?, ?, ?, ?, ?, ?)",
        [(t.id, len(t.members), t.first_date, t.last_date, t.subject, t.method) for t in threads],
    )
    conn.executemany(
        "UPDATE messages SET thread_id = ? WHERE id = ?",
        [(t.id, mid) for t in threads for mid in t.members],
    )
    conn.commit()


def build_index(
    archive_path: str | Path,
    db_path: str | Path = DEFAULT_CACHE_DB,
    force: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """Indexes any supported archive into SQLite + FTS5. Returns indexed message count.

    The format is detected via the driver registry; this function only talks to
    :class:`BaseArchiveDriver`, so new formats need no changes here.
    """
    src = Path(archive_path).resolve()
    db_p = Path(db_path).resolve()

    if not force and is_cache_valid(src, db_p):
        conn = get_db_connection(db_p)
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        return total

    driver = detect_driver(src)
    size, mtime = driver.fingerprint()
    started = time.time()

    # Remove existing db if forcing re-index
    if force and db_p.exists():
        try:
            db_p.unlink()
        except Exception:
            pass

    conn = get_db_connection(db_p)

    # Drop and recreate every table, so caches from an older schema are rebuilt
    # with the current columns rather than just emptied.
    conn.executescript(
        """
        DROP TABLE IF EXISTS attachment_catalog;
        DROP TABLE IF EXISTS attachments;
        DROP TABLE IF EXISTS threads;
        DROP TABLE IF EXISTS messages;
        DROP TABLE IF EXISTS folders;
        DROP TABLE IF EXISTS messages_fts;
        DROP TABLE IF EXISTS archive_meta;
        """
    )
    conn.commit()
    init_db(conn)

    driver.open()
    try:
        # Pass 1: folders. Paths arrive parents-first; the parent is the longest
        # already-seen path that prefixes this one (folder names may contain "/").
        folder_id_map: dict[str, int] = {}
        folder_depth: dict[str, int] = {}
        folder_counts: dict[str, int] = {}
        for path in driver.get_folder_paths():
            if not path or path in folder_id_map:
                continue
            parent = None
            for candidate in folder_id_map:
                if path.startswith(candidate + "/") and (parent is None or len(candidate) > len(parent)):
                    parent = candidate
            name = path[len(parent) + 1:] if parent else path
            depth = folder_depth[parent] + 1 if parent else 1
            try:
                count = driver.get_folder_message_count(path)
            except Exception:
                count = 0
            cursor = conn.execute(
                """
                INSERT INTO folders (parent_id, name, full_path, depth, message_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (folder_id_map.get(parent) if parent else None, name, path, depth, count),
            )
            folder_id_map[path] = cursor.lastrowid
            folder_depth[path] = depth
            folder_counts[path] = count
        conn.commit()

        total_messages = sum(folder_counts.values())
        if progress_callback:
            progress_callback(0, total_messages, "")

        # Pass 2: messages, FTS tokens and attachment metadata
        indexed_count = 0
        signals: list[ThreadSignals] = []
        batch_fts: list[tuple] = []
        batch_attachments: list[tuple] = []

        def _flush() -> None:
            if batch_fts:
                conn.executemany(
                    "INSERT INTO messages_fts (message_id, subject, sender, recipients, body_plain) VALUES (?, ?, ?, ?, ?)",
                    batch_fts,
                )
            if batch_attachments:
                conn.executemany(
                    "INSERT INTO attachments (message_id, attachment_index, filename, size_bytes, mime_type) VALUES (?, ?, ?, ?, ?)",
                    batch_attachments,
                )
            conn.commit()
            batch_fts.clear()
            batch_attachments.clear()

        for folder_path, folder_id in folder_id_map.items():
            for i in range(folder_counts[folder_path]):
                try:
                    detail = driver.get_message_for_index(folder_path, i)
                except Exception:
                    continue

                recipients_json = json.dumps([r.model_dump() for r in detail.recipients])
                recipients_text = " ".join(f"{r.name} {r.email}" for r in detail.recipients)
                body_plain = detail.body_plain or ""
                preview = body_plain[:250].replace("\r", " ").replace("\n", " ").strip()
                sender_text = " ".join(x for x in (detail.sender_name, detail.sender_email) if x)

                cursor = conn.execute(
                    """
                    INSERT INTO messages (
                        folder_id, folder_path, message_index, identifier, subject,
                        sender_name, sender_email, recipients_json, date_iso,
                        has_attachments, attachment_count, body_preview, message_id_hdr
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        folder_id,
                        folder_path,
                        i,
                        detail.identifier or 0,
                        detail.subject,
                        detail.sender_name,
                        detail.sender_email,
                        recipients_json,
                        detail.date_iso,
                        1 if detail.attachments else 0,
                        len(detail.attachments),
                        preview,
                        detail.message_id_hdr,
                    ),
                )
                msg_id = cursor.lastrowid
                signals.append(ThreadSignals(
                    id=msg_id,
                    subject=detail.subject or "",
                    date_iso=detail.date_iso,
                    message_id=detail.message_id_hdr,
                    in_reply_to=detail.in_reply_to,
                    references=tuple(detail.references),
                    conversation_key=detail.conversation_key,
                    topic=detail.conversation_topic,
                ))

                batch_fts.append((msg_id, detail.subject, sender_text, recipients_text, body_plain))
                for att in detail.attachments:
                    batch_attachments.append(
                        (msg_id, att.attachment_index, att.filename, att.size_bytes,
                         att.mime_type or "application/octet-stream")
                    )

                indexed_count += 1
                if progress_callback and indexed_count % 100 == 0:
                    progress_callback(indexed_count, total_messages, detail.subject)

                if len(batch_fts) >= 500:
                    _flush()

        _flush()
        store_threads(conn, compute_threads(signals))

        # Save metadata (pst_* key names kept so existing caches stay valid)
        meta = {
            "pst_path": str(src),
            "pst_size": str(size),
            "pst_mtime": str(mtime),
            "format": driver.describe(),
            "format_type": driver.format_type,
            "driver_index_version": driver.index_version,
            "indexed_at": datetime.now().isoformat(),
            "index_duration_s": f"{time.time() - started:.2f}",
            "indexed_complete": "true",
        }
        conn.executemany(
            "INSERT OR REPLACE INTO archive_meta (key, value) VALUES (?, ?)", list(meta.items())
        )
        conn.commit()
        ensure_attachment_catalog(conn)
    finally:
        conn.close()
        driver.close()

    return indexed_count


def get_archive_info(conn: sqlite3.Connection, archive_path: str | Path) -> ArchiveInfo:
    """Retrieves high-level metadata and aggregate counts."""
    src = Path(archive_path)
    meta = _read_meta(conn)

    if "pst_size" in meta:
        file_size = int(meta["pst_size"])
    else:
        file_size = src.stat().st_size if src.is_file() else 0

    fmt = meta.get("format")
    if not fmt:
        try:
            fmt = detect_driver(src).describe()
        except Exception:
            fmt = "Unknown"

    total_folders = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_attachments = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]

    date_row = conn.execute(
        "SELECT MIN(date_iso), MAX(date_iso) FROM messages WHERE date_iso IS NOT NULL"
    ).fetchone()
    start_date = date_row[0] if date_row else None
    end_date = date_row[1] if date_row else None

    return ArchiveInfo(
        file_path=str(src),
        file_size_bytes=file_size,
        file_size_formatted=format_size(file_size),
        format=fmt,
        total_folders=total_folders,
        total_messages=total_messages,
        total_attachments=total_attachments,
        date_range=DateRange(start=start_date, end=end_date),
    )


def get_message_position(
    conn: sqlite3.Connection,
    message_id: int,
    page_size: int = 50,
    sort_by: str = "date",
    sort_order: str = "desc",
    has_attachments: Optional[bool] = None,
) -> Optional[dict[str, int]]:
    """Which folder page a message falls on, using the same ordering as get_messages."""
    row = conn.execute("SELECT folder_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return None
    sort_col = {"sender": "sender_name", "subject": "subject"}.get(sort_by, "date_iso")
    direction = "ASC" if sort_order.lower() == "asc" else "DESC"
    where, params = "folder_id = ?", [row["folder_id"]]
    if has_attachments is not None:
        where += " AND has_attachments = ?"
        params.append(1 if has_attachments else 0)
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM messages WHERE {where} ORDER BY {sort_col} {direction}, id ASC", params
    )]
    if message_id not in ids:
        return None
    index = ids.index(message_id)
    page_size = max(1, page_size)
    return {"folder_id": row["folder_id"], "index": index, "page": index // page_size + 1}


def get_folder_tree(conn: sqlite3.Connection, include_empty: bool = False) -> list[FolderNode]:
    """Retrieves the full hierarchical folder tree."""
    query = "SELECT id, parent_id, name, full_path, depth, message_count FROM folders"
    if not include_empty:
        query += " WHERE message_count > 0 OR id IN (SELECT DISTINCT parent_id FROM folders WHERE parent_id IS NOT NULL)"
    query += " ORDER BY depth ASC, name ASC"

    rows = conn.execute(query).fetchall()

    nodes: dict[int, FolderNode] = {}
    roots: list[FolderNode] = []

    for r in rows:
        node = FolderNode(
            id=r["id"],
            parent_id=r["parent_id"],
            name=r["name"],
            full_path=r["full_path"],
            depth=r["depth"],
            message_count=r["message_count"],
            children=[],
        )
        nodes[node.id] = node

    for node in nodes.values():
        if node.parent_id and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


def get_messages(
    conn: sqlite3.Connection,
    folder_id: int,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = "date",
    sort_order: str = "desc",
    has_attachments: Optional[bool] = None,
) -> tuple[int, list[MessageSummary]]:
    """Retrieves a paginated list of messages in a given folder."""
    offset = max(0, (page - 1) * page_size)
    limit = min(max(1, page_size), 200)

    where_clauses = ["folder_id = ?"]
    params: list[Any] = [folder_id]

    if has_attachments is not None:
        where_clauses.append("has_attachments = ?")
        params.append(1 if has_attachments else 0)

    where_str = " AND ".join(where_clauses)

    total_query = f"SELECT COUNT(*) FROM messages WHERE {where_str}"
    total = conn.execute(total_query, params).fetchone()[0]

    sort_col = "date_iso"
    if sort_by == "sender":
        sort_col = "sender_name"
    elif sort_by == "subject":
        sort_col = "subject"

    direction = "ASC" if sort_order.lower() == "asc" else "DESC"

    query = f"""
        SELECT m.id, m.folder_id, m.folder_path, m.message_index, m.identifier,
               m.subject, m.sender_name, m.sender_email, m.date_iso,
               m.has_attachments, m.attachment_count, m.body_preview,
               COALESCE(t.size, 1) AS thread_size
        FROM messages m LEFT JOIN threads t ON t.id = m.thread_id
        WHERE {where_str}
        ORDER BY m.{sort_col} {direction}, m.id ASC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(query, params + [limit, offset]).fetchall()

    items = [
        MessageSummary(
            id=r["id"],
            folder_id=r["folder_id"],
            folder_path=r["folder_path"],
            message_index=r["message_index"],
            identifier=r["identifier"],
            subject=r["subject"] or "(No Subject)",
            sender_name=r["sender_name"] or "",
            sender_email=r["sender_email"] or "",
            date_iso=r["date_iso"],
            has_attachments=bool(r["has_attachments"]),
            attachment_count=r["attachment_count"],
            body_preview=r["body_preview"] or "",
            thread_size=r["thread_size"],
        )
        for r in rows
    ]
    return total, items


def search_messages(
    conn: sqlite3.Connection,
    query_str: str,
    folder_id: Optional[int] = None,
    has_attachments: Optional[bool] = None,
    sender: Optional[str] = None,
    attachment_ext: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[int, list[SearchResult]]:
    """Executes a full-text search with highlighted snippets via SQLite FTS5 with multi-criteria filters."""
    clean_query = query_str.strip().replace('"', '""')

    offset = max(0, (page - 1) * page_size)
    limit = min(max(1, page_size), 200)

    where_clauses = []
    params: list[Any] = []

    if clean_query:
        fts_tokens = [f'"{token}"' for token in clean_query.split() if token]
        fts_expr = " AND ".join(fts_tokens) if fts_tokens else clean_query
        where_clauses.append("messages_fts MATCH ?")
        params.append(fts_expr)

    if folder_id is not None:
        where_clauses.append("m.folder_id = ?")
        params.append(folder_id)

    if has_attachments is not None:
        where_clauses.append("m.has_attachments = ?")
        params.append(1 if has_attachments else 0)

    if sender:
        where_clauses.append("m.sender_name LIKE ?")
        params.append(f"%{sender.strip()}%")

    if date_from:
        where_clauses.append("m.date_iso >= ?")
        params.append(date_from)

    if date_to:
        where_clauses.append("m.date_iso <= ?")
        params.append(date_to + "T23:59:59" if "T" not in date_to else date_to)

    if attachment_ext:
        ext_clean = attachment_ext.strip().lstrip(".").lower()
        where_clauses.append("m.id IN (SELECT message_id FROM attachments WHERE LOWER(filename) LIKE ?)")
        params.append(f"%.{ext_clean}")

    if not where_clauses:
        where_clauses.append("1=1")

    where_str = " AND ".join(where_clauses)

    if clean_query:
        count_sql = f"""
            SELECT COUNT(*)
            FROM messages_fts f
            JOIN messages m ON f.message_id = m.id
            WHERE {where_str}
        """
        select_sql = f"""
            SELECT m.id, m.folder_id, m.folder_path, m.subject, m.sender_name,
                   m.date_iso, m.has_attachments, m.attachment_count,
                   snippet(messages_fts, 4, '<mark>', '</mark>', '...', 24) AS snippet,
                   COALESCE(t.size, 1) AS thread_size
            FROM messages_fts f
            JOIN messages m ON f.message_id = m.id
            LEFT JOIN threads t ON t.id = m.thread_id
            WHERE {where_str}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
    else:
        count_sql = f"SELECT COUNT(*) FROM messages m WHERE {where_str}"
        select_sql = f"""
            SELECT m.id, m.folder_id, m.folder_path, m.subject, m.sender_name,
                   m.date_iso, m.has_attachments, m.attachment_count,
                   m.body_preview AS snippet,
                   COALESCE(t.size, 1) AS thread_size
            FROM messages m
            LEFT JOIN threads t ON t.id = m.thread_id
            WHERE {where_str}
            ORDER BY m.date_iso DESC
            LIMIT ? OFFSET ?
        """

    try:
        total = conn.execute(count_sql, params).fetchone()[0]
    except sqlite3.OperationalError:
        # Fallback if complex FTS syntax fails
        if clean_query and params:
            params[0] = f'"{clean_query}"'
        total = conn.execute(count_sql, params).fetchone()[0]

    rows = conn.execute(select_sql, params + [limit, offset]).fetchall()

    results = [
        SearchResult(
            id=r["id"],
            folder_id=r["folder_id"],
            folder_path=r["folder_path"],
            subject=r["subject"] or "(No Subject)",
            sender_name=r["sender_name"] or "",
            date_iso=r["date_iso"],
            has_attachments=bool(r["has_attachments"]),
            attachment_count=r["attachment_count"],
            snippet=r["snippet"] or "",
            thread_size=r["thread_size"],
        )
        for r in rows
    ]
    return total, results


def get_message_thread(
    conn: sqlite3.Connection, message_id: int, limit: int = 50, offset: Optional[int] = None
) -> Optional[ThreadWindow]:
    """A window of ``message_id``'s conversation (Spec 10 §2.3).

    Without ``offset`` the window is centred on the message itself.
    Returns None when the message doesn't exist.
    """
    row = conn.execute("SELECT thread_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return None
    thread_id = row["thread_id"] if row["thread_id"] is not None else message_id
    t = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE thread_id = ? "
        "ORDER BY date_iso IS NULL, date_iso ASC, id ASC", (thread_id,)
    )] or [message_id]
    size = len(ids)
    position = ids.index(message_id) + 1 if message_id in ids else 1
    limit = min(max(1, limit), 200)
    if offset is None:
        offset = max(0, min(position - 1 - limit // 2, size - limit))
    offset = max(0, min(offset, max(0, size - 1)))
    window = ids[offset:offset + limit]

    placeholders = ",".join("?" * len(window))
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT id, folder_id, folder_path, message_index, identifier, subject,
                   sender_name, sender_email, date_iso, has_attachments,
                   attachment_count, body_preview
            FROM messages WHERE id IN ({placeholders})""", window)}
    items = [
        MessageSummary(
            id=r["id"], folder_id=r["folder_id"], folder_path=r["folder_path"],
            message_index=r["message_index"], identifier=r["identifier"],
            subject=r["subject"] or "(No Subject)", sender_name=r["sender_name"] or "",
            sender_email=r["sender_email"] or "", date_iso=r["date_iso"],
            has_attachments=bool(r["has_attachments"]), attachment_count=r["attachment_count"],
            body_preview=r["body_preview"] or "", thread_size=size,
        )
        for r in (rows[i] for i in window if i in rows)
    ]
    return ThreadWindow(
        thread_id=thread_id,
        size=size,
        position=position,
        method=(t["method"] if t else "single") if size > 1 else "single",
        first_date=t["first_date"] if t else None,
        last_date=t["last_date"] if t else None,
        offset=offset,
        items=items,
    )


def get_archive_analytics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Retrieves analytics data: monthly email distribution, top senders, and attachment metrics."""
    # Monthly distribution
    months = conn.execute(
        """
        SELECT SUBSTR(date_iso, 1, 7) as month, COUNT(*) as cnt
        FROM messages
        WHERE date_iso IS NOT NULL
        GROUP BY month
        ORDER BY month ASC
        """
    ).fetchall()

    # Top senders
    senders = conn.execute(
        """
        SELECT sender_name, COUNT(*) as cnt
        FROM messages
        WHERE sender_name IS NOT NULL AND sender_name != ''
        GROUP BY sender_name
        ORDER BY cnt DESC
        LIMIT 10
        """
    ).fetchall()

    # Attachment breakdown
    extensions = conn.execute(
        """
        SELECT
            CASE
                WHEN filename LIKE '%.%' THEN LOWER(SUBSTR(filename, INSTR(filename, '.') + 1))
                ELSE 'no_ext'
            END AS ext,
            COUNT(*) as cnt,
            SUM(size_bytes) as total_bytes
        FROM attachments
        GROUP BY ext
        ORDER BY cnt DESC
        LIMIT 10
        """
    ).fetchall()

    return {
        "monthly": [{"month": r["month"], "count": r["cnt"]} for r in months],
        "top_senders": [{"sender": r["sender_name"], "count": r["cnt"]} for r in senders],
        "top_attachments": [
            {
                "ext": r["ext"],
                "count": r["cnt"],
                "total_bytes": r["total_bytes"],
                "formatted_size": format_size(r["total_bytes"]),
            }
            for r in extensions
        ],
    }

