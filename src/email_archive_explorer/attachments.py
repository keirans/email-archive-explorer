"""Attachment Explorer (Spec 09): derived catalog, discovery queries, archive peeking."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import tarfile
import threading
import zipfile
from collections import Counter
from datetime import UTC
from pathlib import PurePosixPath
from typing import Any, Optional

from email_archive_explorer.config import format_size

CATALOG_VERSION = "4"

SMALL_IMAGE_BYTES = 10 * 1024
REPEATED_IMAGE_COPIES = 5
ARTIFACT_MAX_BYTES = 50 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000

CATEGORY_LABELS: dict[str, str] = {
    "documents": "Documents",
    "spreadsheets": "Spreadsheets",
    "presentations": "Presentations",
    "images": "Images",
    "video_audio": "Video & Audio",
    "archives": "Archives",
    "email": "Emails",
    "contacts_calendar": "Contacts & Calendar",
    "web_code": "Web, Code & Config",
    "programs": "Programs",
    "drawings": "Drawings",
    "other": "Other",
}

_EXT_CATEGORY: dict[str, str] = {}
for _cat, _exts in {
    "documents": "doc docx docm dot dotx rtf odt pdf wpd wri txt md pages ltt",
    "spreadsheets": "xls xlsx xlsm xlt xltx csv ods",
    "presentations": "ppt pptx pps ppsx pot potx odp key",
    "images": "jpg jpeg jpe png gif bmp tif tiff pcx svg webp heic ico wmf emf",
    "video_audio": "mpg mpeg mpe mp4 m4v avi mov wmv asf flv webm ogv mp3 wav wma mid midi ogg m4a ra rm",
    "archives": "zip rar 7z gz tgz tar bz2 cab arj lzh z",
    "email": "eml msg mht mhtml",
    "contacts_calendar": "vcf ics vcs",
    "web_code": "htm html xml js vbs bat cmd sh ps1 c cpp h py java sql json ini cfg log url css asp php pl",
    "programs": "exe dll msi com scr",
    "drawings": "vsd vdx vss dwg dxf",
}.items():
    for _e in _exts.split():
        _EXT_CATEGORY[_e] = _cat

_MIME_PREFIX_CATEGORY: list[tuple[str, str]] = [
    ("image/", "images"),
    ("video/", "video_audio"),
    ("audio/", "video_audio"),
    ("message/rfc822", "email"),
    ("application/x-pst-embedded-item", "email"),
    ("application/pdf", "documents"),
    ("application/msword", "documents"),
    ("text/plain", "documents"),
    ("application/vnd.ms-excel", "spreadsheets"),
    ("application/vnd.ms-powerpoint", "presentations"),
    ("application/zip", "archives"),
    ("application/x-tar", "archives"),
    ("application/gzip", "archives"),
    ("text/html", "web_code"),
    ("text/xml", "web_code"),
]

_ARTIFACT_RE = re.compile(r"^(att\d+\.(txt|htm|html|c)|winmail\.dat|oledata\.mso)$", re.IGNORECASE)
_DISCLAIMER_RE = re.compile(r"disclaimer.*\.(txt|htm|html)$", re.IGNORECASE)
_OUTLOOK_INLINE_RE = re.compile(r"^image\d{3}\.(png|gif|jpg|jpeg|bmp)$", re.IGNORECASE)

_PREVIEW_BY_EXT: dict[str, str] = {}
for _kind, _exts in {
    "image": "jpg jpeg jpe png gif bmp svg webp ico",
    "pdf": "pdf",
    "text": "txt log csv ini cfg xml json vcf ics vcs url bat cmd sh ps1 c cpp h py java sql md eml css js vbs pl",
    "html": "htm html mht mhtml",
    "video": "mp4 m4v webm ogv",
    "audio": "mp3 wav ogg m4a",
    "archive": "zip tar tgz",
}.items():
    for _e in _exts.split():
        _PREVIEW_BY_EXT[_e] = _kind

_catalog_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def file_ext(filename: str) -> str:
    name = (filename or "").strip().lower()
    if name.endswith(".tar.gz"):
        return "tgz"
    suffix = PurePosixPath(name).suffix
    ext = suffix[1:] if suffix else ""
    # Treat things like "Outlook..bmp" normally, but not random dotted tokens
    return ext if ext and len(ext) <= 12 and ext.isalnum() else ""


def categorize(ext: str, mime_type: Optional[str]) -> str:
    if ext in _EXT_CATEGORY:
        return _EXT_CATEGORY[ext]
    mime = (mime_type or "").lower()
    for prefix, cat in _MIME_PREFIX_CATEGORY:
        if mime.startswith(prefix):
            return cat
    return "other"


def preview_kind(ext: str, mime_type: Optional[str], filename: str = "") -> str:
    if (filename or "").lower().endswith(".tar.gz"):
        return "archive"
    if ext in _PREVIEW_BY_EXT:
        return _PREVIEW_BY_EXT[ext]
    mime = (mime_type or "").lower()
    if mime == "message/rfc822":
        return "text"
    if mime.startswith("image/") and mime not in ("image/tiff", "image/x-pcx"):
        return "image"
    return "none"


def noise_reason(
    filename: str, ext: str, category: str, size: int, copies: int, mime_type: Optional[str] = None
) -> Optional[str]:
    name = (filename or "").strip()
    if mime_type == "application/x-pst-embedded-item":
        return "embedded-item"
    if _ARTIFACT_RE.match(name):
        return "outlook-artifact"
    if _OUTLOOK_INLINE_RE.match(name) and size < ARTIFACT_MAX_BYTES:
        return "outlook-artifact"
    if _DISCLAIMER_RE.search(name) and size < SMALL_IMAGE_BYTES:
        return "mail-footer"
    if category == "images" and ext:
        if size < SMALL_IMAGE_BYTES:
            return "small-image"
        if copies >= REPEATED_IMAGE_COPIES:
            return "repeated-image"
    return None


# ---------------------------------------------------------------------------
# Derived catalog
# ---------------------------------------------------------------------------


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM archive_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _catalog_fresh(conn: sqlite3.Connection) -> bool:
    if _meta(conn, "attachment_catalog_version") != CATALOG_VERSION:
        return False
    try:
        n_cat = conn.execute("SELECT COUNT(*) FROM attachment_catalog").fetchone()[0]
    except sqlite3.Error:
        return False
    n_att = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    return n_cat == n_att and _meta(conn, "attachment_catalog_count") == str(n_att)


def ensure_attachment_catalog(conn: sqlite3.Connection) -> None:
    """Build or refresh the derived ``attachment_catalog`` table if stale."""
    if _catalog_fresh(conn):
        return
    with _catalog_lock:
        if _catalog_fresh(conn):
            return
        rows = conn.execute(
            "SELECT id, filename, size_bytes, mime_type FROM attachments ORDER BY id"
        ).fetchall()

        groups: dict[tuple[str, int], int] = {}
        counts: Counter = Counter()
        for r in rows:
            key = ((r[1] or "").strip().lower(), r[2] or 0)
            groups.setdefault(key, r[0])
            counts[key] += 1

        records = []
        for r in rows:
            att_id, filename, size, mime = r[0], r[1] or "", r[2] or 0, r[3]
            key = (filename.strip().lower(), size)
            ext = file_ext(filename)
            cat = categorize(ext, mime)
            copies = counts[key]
            reason = noise_reason(filename, ext, cat, size, copies, mime)
            records.append((att_id, ext, cat, key[0], groups[key], copies, 1 if reason else 0, reason))

        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")  # one transaction: readers never see a half-built table
        try:
            conn.execute("DROP TABLE IF EXISTS attachment_catalog")
            conn.execute(
                """
                CREATE TABLE attachment_catalog (
                    attachment_id INTEGER PRIMARY KEY,  -- attachments.id (derived; no FK so re-indexing can clear tables)
                    ext           TEXT NOT NULL,
                    category      TEXT NOT NULL,
                    name_key      TEXT NOT NULL,
                    dup_group     INTEGER NOT NULL,
                    copies        INTEGER NOT NULL,
                    is_noise      INTEGER NOT NULL,
                    noise_reason  TEXT
                )
                """
            )
            conn.execute("CREATE INDEX idx_attcat_category ON attachment_catalog(category)")
            conn.execute("CREATE INDEX idx_attcat_group ON attachment_catalog(dup_group)")
            conn.execute("CREATE INDEX idx_attcat_ext ON attachment_catalog(ext)")
            conn.executemany("INSERT INTO attachment_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)", records)
            conn.executemany(
                "INSERT OR REPLACE INTO archive_meta (key, value) VALUES (?, ?)",
                [("attachment_catalog_version", CATALOG_VERSION),
                 ("attachment_catalog_count", str(len(rows)))],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_BASE_SELECT = """
    SELECT a.id, a.message_id, a.attachment_index, a.filename, a.size_bytes, a.mime_type,
           c.ext, c.category, c.dup_group, c.copies, c.is_noise, c.noise_reason,
           m.subject, m.sender_name, m.sender_email, m.date_iso, m.folder_id, m.folder_path
    FROM attachments a
    JOIN attachment_catalog c ON c.attachment_id = a.id
    JOIN messages m ON m.id = a.message_id
"""

_SORTS = {
    "date": "date_iso",
    "size": "size_bytes",
    "name": "filename COLLATE NOCASE",
    "sender": "sender_name COLLATE NOCASE",
    "copies": "copies",
    "type": "ext",
}


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters(
    conn: sqlite3.Connection,
    q: Optional[str] = None,
    category: Optional[str] = None,
    ext: Optional[str] = None,
    year: Optional[str] = None,
    sender: Optional[str] = None,
    folder_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    hide_noise: bool = True,
    rare_only: bool = False,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if q and q.strip():
        where.append("a.filename LIKE ? ESCAPE '\\'")
        params.append(_like(q.strip()))
    if category:
        where.append("c.category = ?")
        params.append(category)
    if ext:
        where.append("c.ext = ?")
        params.append(ext.strip().lstrip(".").lower())
    if year:
        where.append("substr(m.date_iso, 1, 4) = ?")
        params.append(str(year))
    if sender and sender.strip():
        where.append("(m.sender_name LIKE ? ESCAPE '\\' OR m.sender_email LIKE ? ESCAPE '\\')")
        params += [_like(sender.strip())] * 2
    if folder_id is not None:
        row = conn.execute("SELECT full_path FROM folders WHERE id = ?", (folder_id,)).fetchone()
        path = row[0] if row else ""
        where.append("(m.folder_path = ? OR m.folder_path LIKE ? ESCAPE '\\')")
        params += [path, path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"]
    if date_from:
        where.append("m.date_iso >= ?")
        params.append(date_from)
    if date_to:
        where.append("m.date_iso <= ?")
        params.append(date_to + "T23:59:59" if "T" not in date_to else date_to)
    if min_size is not None:
        where.append("a.size_bytes >= ?")
        params.append(min_size)
    if max_size is not None:
        where.append("a.size_bytes <= ?")
        params.append(max_size)
    if hide_noise:
        where.append("c.is_noise = 0")
    if rare_only:
        where.append(
            "c.ext != '' AND c.ext IN (SELECT ext FROM attachment_catalog WHERE is_noise = 0 "
            "GROUP BY ext HAVING COUNT(DISTINCT dup_group) <= 2)"
        )
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _item(r: sqlite3.Row, copies_matching: Optional[int] = None) -> dict[str, Any]:
    d = {
        "id": r["id"],
        "message_id": r["message_id"],
        "attachment_index": r["attachment_index"],
        "filename": r["filename"],
        "ext": r["ext"],
        "category": r["category"],
        "mime_type": r["mime_type"] or "application/octet-stream",
        "size_bytes": r["size_bytes"],
        "size_formatted": format_size(r["size_bytes"]),
        "copies": r["copies"],
        "is_noise": bool(r["is_noise"]),
        "noise_reason": r["noise_reason"],
        "preview": preview_kind(r["ext"], r["mime_type"], r["filename"]),
        "view_url": f"/api/messages/{r['message_id']}/attachments/{r['id']}",
        "download_url": f"/api/messages/{r['message_id']}/attachments/{r['id']}?download=1",
        "message": {
            "id": r["message_id"],
            "subject": r["subject"] or "(No Subject)",
            "sender_name": r["sender_name"] or "",
            "sender_email": r["sender_email"] or "",
            "date_iso": r["date_iso"],
            "folder_id": r["folder_id"],
            "folder_path": r["folder_path"],
        },
    }
    if copies_matching is not None:
        d["copies_matching"] = copies_matching
    return d


def list_attachments(
    conn: sqlite3.Connection,
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    page_size: int = 60,
    collapse: bool = True,
    **filters: Any,
) -> dict[str, Any]:
    ensure_attachment_catalog(conn)
    where, params = _filters(conn, **filters)
    sort_col = _SORTS.get(sort, "date_iso")
    direction = "ASC" if order == "asc" else "DESC"
    # Undated messages always sort last
    order_by = f"(date_iso IS NULL), {sort_col} {direction}, id ASC" if sort == "date" \
        else f"{sort_col} {direction}, (date_iso IS NULL), date_iso ASC, id ASC"

    base = _BASE_SELECT + where
    if collapse:
        source = f"""
            SELECT * FROM (
                SELECT b.*,
                       ROW_NUMBER() OVER (PARTITION BY dup_group
                                          ORDER BY (date_iso IS NULL), date_iso, id) AS rn,
                       COUNT(*) OVER (PARTITION BY dup_group) AS copies_matching
                FROM ({base}) b
            ) WHERE rn = 1
        """
    else:
        source = f"SELECT b.*, 1 AS copies_matching FROM ({base}) b"

    total, total_bytes = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM ({source})", params
    ).fetchone()

    page_size = max(1, min(page_size, 200))
    offset = (max(page, 1) - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM ({source}) ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": total,
        "total_bytes": total_bytes,
        "total_bytes_formatted": format_size(total_bytes),
        "page": max(page, 1),
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": [_item(r, r["copies_matching"]) for r in rows],
    }


def attachment_summary(conn: sqlite3.Connection, hide_noise: bool = True) -> dict[str, Any]:
    ensure_attachment_catalog(conn)
    noise_clause = "WHERE c.is_noise = 0" if hide_noise else ""
    join = "FROM attachments a JOIN attachment_catalog c ON c.attachment_id = a.id JOIN messages m ON m.id = a.message_id"

    tot = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(a.size_bytes), 0), COUNT(DISTINCT c.dup_group),
                   SUM(c.is_noise)
            FROM attachments a JOIN attachment_catalog c ON c.attachment_id = a.id"""
    ).fetchone()
    visible = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(a.size_bytes), 0), COUNT(DISTINCT c.dup_group) {join} {noise_clause}"
    ).fetchone()

    categories = [
        {
            "category": r[0],
            "label": CATEGORY_LABELS.get(r[0], r[0]),
            "count": r[1],
            "unique": r[2],
            "bytes": r[3],
            "size_formatted": format_size(r[3]),
        }
        for r in conn.execute(
            f"""SELECT c.category, COUNT(*), COUNT(DISTINCT c.dup_group), COALESCE(SUM(a.size_bytes), 0)
                {join} {noise_clause} GROUP BY c.category ORDER BY COUNT(*) DESC"""
        )
    ]
    extensions = [
        {"ext": r[0], "count": r[1], "unique": r[2], "bytes": r[3], "size_formatted": format_size(r[3])}
        for r in conn.execute(
            f"""SELECT c.ext, COUNT(*), COUNT(DISTINCT c.dup_group), COALESCE(SUM(a.size_bytes), 0)
                {join} {noise_clause} GROUP BY c.ext ORDER BY COUNT(*) DESC LIMIT 40"""
        )
    ]
    years = [
        {"year": r[0], "count": r[1]}
        for r in conn.execute(
            f"""SELECT substr(m.date_iso, 1, 4) y, COUNT(*) {join}
                {noise_clause + ' AND' if noise_clause else 'WHERE'} m.date_iso IS NOT NULL
                GROUP BY y ORDER BY y"""
        )
    ]
    senders = [
        {"sender": r[0], "count": r[1], "bytes": r[2], "size_formatted": format_size(r[2])}
        for r in conn.execute(
            f"""SELECT COALESCE(NULLIF(m.sender_name, ''), m.sender_email) s, COUNT(*),
                       COALESCE(SUM(a.size_bytes), 0)
                {join} {noise_clause + ' AND' if noise_clause else 'WHERE'} s IS NOT NULL AND s != ''
                GROUP BY s ORDER BY COUNT(*) DESC LIMIT 10"""
        )
    ]
    rare = [
        {"ext": r[0], "count": r[1], "category": r[2]}
        for r in conn.execute(
            """SELECT ext, COUNT(DISTINCT dup_group) n, MIN(category) FROM attachment_catalog
               WHERE is_noise = 0 AND ext != '' GROUP BY ext HAVING n <= 2 ORDER BY n, ext LIMIT 24"""
        )
    ]
    noise = [
        {"reason": r[0], "count": r[1]}
        for r in conn.execute(
            "SELECT noise_reason, COUNT(*) FROM attachment_catalog WHERE is_noise = 1 "
            "GROUP BY noise_reason ORDER BY COUNT(*) DESC"
        )
    ]

    return {
        "total_count": tot[0],
        "total_bytes": tot[1],
        "total_size_formatted": format_size(tot[1]),
        "unique_count": tot[2],
        "noise_count": tot[3] or 0,
        "visible_count": visible[0],
        "visible_bytes": visible[1],
        "visible_size_formatted": format_size(visible[1]),
        "visible_unique": visible[2],
        "categories": categories,
        "extensions": extensions,
        "years": years,
        "top_senders": senders,
        "rare_types": rare,
        "noise": noise,
        "largest": list_attachments(conn, sort="size", page_size=8, hide_noise=hide_noise)["items"],
        "most_forwarded": [
            i for i in list_attachments(conn, sort="copies", page_size=8, hide_noise=hide_noise)["items"]
            if i["copies"] > 1
        ],
    }


def attachment_detail(conn: sqlite3.Connection, attachment_id: int) -> Optional[dict[str, Any]]:
    ensure_attachment_catalog(conn)
    row = conn.execute(_BASE_SELECT + " WHERE a.id = ?", (attachment_id,)).fetchone()
    if row is None:
        return None
    item = _item(row)

    msg = conn.execute(
        """SELECT m.id, m.subject, m.sender_name, m.sender_email, m.date_iso, m.folder_id,
                  m.folder_path, m.recipients_json, m.body_preview, m.attachment_count,
                  f.name AS folder_name
           FROM messages m LEFT JOIN folders f ON f.id = m.folder_id WHERE m.id = ?""",
        (row["message_id"],),
    ).fetchone()
    try:
        recipients = json.loads(msg["recipients_json"] or "[]")
    except ValueError:
        recipients = []
    item["origin"] = {
        "id": msg["id"],
        "subject": msg["subject"] or "(No Subject)",
        "sender_name": msg["sender_name"] or "",
        "sender_email": msg["sender_email"] or "",
        "date_iso": msg["date_iso"],
        "folder_id": msg["folder_id"],
        "folder_name": msg["folder_name"] or "",
        "folder_path": msg["folder_path"],
        "recipients": recipients,
        "body_preview": msg["body_preview"] or "",
        "attachment_count": msg["attachment_count"],
    }

    copies = conn.execute(
        _BASE_SELECT + " WHERE c.dup_group = ? ORDER BY (m.date_iso IS NULL), m.date_iso, a.id",
        (row["dup_group"],),
    ).fetchall()
    item["copies_list"] = [_item(c) for c in copies]
    item["first_seen"] = item["copies_list"][0] if item["copies_list"] else None
    item["category_label"] = CATEGORY_LABELS.get(item["category"], item["category"])
    return item


# ---------------------------------------------------------------------------
# Peeking inside ZIP / TAR attachments
# ---------------------------------------------------------------------------


class ArchiveContentError(ValueError):
    pass


def _open_container(data: bytes):
    buf = io.BytesIO(data)
    if zipfile.is_zipfile(buf):
        buf.seek(0)
        return "zip", zipfile.ZipFile(buf)
    buf.seek(0)
    try:
        return "tar", tarfile.open(fileobj=buf, mode="r:*")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ArchiveContentError("Not a readable ZIP or TAR archive") from exc


def list_archive_contents(data: bytes) -> dict[str, Any]:
    kind, container = _open_container(data)
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        if kind == "zip":
            infos = container.infolist()
            for i, info in enumerate(infos):
                if i >= MAX_ARCHIVE_ENTRIES:
                    truncated = True
                    break
                y, mo, d, h, mi, s = info.date_time
                entries.append({
                    "index": i,
                    "name": info.filename,
                    "is_dir": info.is_dir(),
                    "size_bytes": info.file_size,
                    "size_formatted": format_size(info.file_size),
                    "compressed_bytes": info.compress_size,
                    "modified_iso": f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}",
                    "encrypted": bool(info.flag_bits & 0x1),
                })
        else:
            from datetime import datetime

            for i, info in enumerate(container.getmembers()):
                if i >= MAX_ARCHIVE_ENTRIES:
                    truncated = True
                    break
                entries.append({
                    "index": i,
                    "name": info.name,
                    "is_dir": info.isdir(),
                    "size_bytes": info.size,
                    "size_formatted": format_size(info.size),
                    "compressed_bytes": None,
                    "modified_iso": datetime.fromtimestamp(info.mtime, UTC)
                    .replace(tzinfo=None).isoformat() if info.mtime else None,
                    "encrypted": False,
                })
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as exc:
        raise ArchiveContentError(f"Archive is damaged: {exc}") from exc
    finally:
        container.close()

    files = [e for e in entries if not e["is_dir"]]
    return {
        "kind": kind,
        "file_count": len(files),
        "total_bytes": sum(e["size_bytes"] for e in files),
        "total_size_formatted": format_size(sum(e["size_bytes"] for e in files)),
        "entries": entries,
        "truncated": truncated,
    }


def extract_archive_member(data: bytes, index: int) -> tuple[str, bytes]:
    kind, container = _open_container(data)
    try:
        if kind == "zip":
            infos = container.infolist()
            if index < 0 or index >= len(infos) or infos[index].is_dir():
                raise IndexError(index)
            info = infos[index]
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ArchiveContentError("File is too large to extract here")
            if info.flag_bits & 0x1:
                raise ArchiveContentError("File is password-protected")
            payload = container.read(info)
            name = info.filename
        else:
            members = container.getmembers()
            if index < 0 or index >= len(members) or not members[index].isfile():
                raise IndexError(index)
            info = members[index]
            if info.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ArchiveContentError("File is too large to extract here")
            fh = container.extractfile(info)
            payload = fh.read() if fh else b""
            name = info.name
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, RuntimeError) as exc:
        raise ArchiveContentError(f"Could not extract: {exc}") from exc
    finally:
        container.close()
    # Only the base name is ever used (no extraction to disk → no traversal)
    return PurePosixPath(name.replace("\\", "/")).name or f"file_{index + 1}", payload


def unique_zip_name(name: str, used: set) -> str:
    safe = PurePosixPath((name or "attachment").replace("\\", "/")).name or "attachment"
    if safe.lower() not in used:
        used.add(safe.lower())
        return safe
    stem, dot, ext = safe.rpartition(".")
    if not dot:
        stem, ext = safe, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        if candidate.lower() not in used:
            used.add(candidate.lower())
            return candidate
        n += 1
