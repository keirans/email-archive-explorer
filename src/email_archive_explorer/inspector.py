"""Archive Inspector data (Spec 08 §3.5): technical facts about the open archive."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from email_archive_explorer.config import format_size
from email_archive_explorer.drivers import BaseArchiveDriver
from email_archive_explorer.indexer import is_cache_valid

_CHUNK = 1024 * 1024


def _item(label: str, value: Any, mono: bool = False, hint: Optional[str] = None) -> dict[str, Any]:
    d: dict[str, Any] = {"label": label, "value": str(value), "mono": mono}
    if hint:
        d["hint"] = hint
    return d


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM archive_meta").fetchall())


def _owner(st: os.stat_result) -> str:
    try:
        import pwd
        return pwd.getpwuid(st.st_uid).pw_name
    except (ImportError, KeyError):
        return str(st.st_uid)


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%-d %b %Y")
    except ValueError:
        return iso[:10]


def archive_details(
    conn: sqlite3.Connection, file_path: Path, db_path: Path, driver: BaseArchiveDriver
) -> dict[str, Any]:
    meta = _meta(conn)
    st = file_path.stat()
    size = int(meta.get("pst_size", st.st_size if file_path.is_file() else 0))

    file_items = [
        _item("Path", file_path, mono=True),
        _item("Kind", "Folder" if file_path.is_dir() else "File"),
        _item("Size", f"{format_size(size)} ({size:,} bytes)"),
        _item("Modified", _fmt_dt(st.st_mtime)),
        _item("Permissions", f"{stat.filemode(st.st_mode)}  {_owner(st)}", mono=True),
        _item("Opened", "Read-only — this app never writes to the archive"),
    ]

    format_items = [
        _item("Format", meta.get("format") or driver.describe()),
        _item("Driver", f"{type(driver).__name__} (index v{driver.index_version})", mono=True),
    ]

    totals = conn.execute(
        """SELECT (SELECT COUNT(*) FROM folders), (SELECT COUNT(*) FROM messages),
                  (SELECT COUNT(*) FROM attachments), (SELECT COALESCE(SUM(size_bytes), 0) FROM attachments),
                  (SELECT COUNT(*) FROM messages WHERE date_iso IS NULL),
                  (SELECT MIN(date_iso) FROM messages), (SELECT MAX(date_iso) FROM messages)"""
    ).fetchone()
    largest = conn.execute(
        "SELECT name, full_path, message_count FROM folders ORDER BY message_count DESC LIMIT 1"
    ).fetchone()
    senders = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(NULLIF(sender_email, ''), sender_name)) FROM messages"
    ).fetchone()[0]
    contents_items = [
        _item("Folders", f"{totals[0]:,}"),
        _item("Messages", f"{totals[1]:,}"),
        _item("Attachments", f"{totals[2]:,} ({format_size(totals[3])})"),
        _item("Date range", f"{_fmt_date(totals[5])} – {_fmt_date(totals[6])}"),
        _item("Messages without a date", f"{totals[4]:,}"),
        _item("Distinct senders", f"{senders:,}"),
    ]
    if largest:
        contents_items.append(_item("Largest folder", f"{largest[0]} ({largest[2]:,})", hint=largest[1]))

    cache_bytes = sum(
        p.stat().st_size for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")) if p.exists()
    )
    indexed_at = meta.get("indexed_at")
    duration = meta.get("index_duration_s")
    index_items = [
        _item("Cache file", db_path, mono=True),
        _item("Cache size", format_size(cache_bytes)),
        _item("Indexed", (indexed_at[:16].replace("T", " ") if indexed_at else "—")
              + (f" ({float(duration):.1f} s)" if duration else "")),
        _item("Status", "Up to date" if is_cache_valid(file_path, db_path) else "Stale — will rebuild on next open"),
        _item("Full-text search", "SQLite FTS5 (porter, unicode61)"),
    ]

    sections: list[dict[str, Any]] = [
        {"title": "File", "items": file_items},
        {"title": "Format", "items": format_items},
    ]
    try:
        sections += driver.technical_details()
    except Exception as exc:  # never let one driver's quirk break the panel
        sections.append({"title": "Format details", "items": [_item("Unavailable", exc)]})
    sections += [
        {"title": "Contents", "items": contents_items},
        {"title": "Search index", "items": index_items},
    ]

    checksum = None
    if meta.get("sha256") and meta.get("sha256_fingerprint") == f"{meta.get('pst_size')}:{meta.get('pst_mtime')}":
        checksum = {"algorithm": "SHA-256", "value": meta["sha256"], "scope": meta.get("sha256_scope", "file")}
    return {"sections": sections, "checksum": checksum}


def compute_checksum(conn: sqlite3.Connection, file_path: Path) -> dict[str, str]:
    """SHA-256 of the archive (streamed). For folders: hash of a sorted manifest
    of ``relative/path  sha256`` lines. Cached against the current fingerprint."""
    h = hashlib.sha256()
    if file_path.is_file():
        scope = "file"
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    else:
        scope = "manifest"
        for dirpath, dirnames, filenames in os.walk(file_path):
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                p = Path(dirpath) / name
                fh = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(_CHUNK), b""):
                        fh.update(chunk)
                h.update(f"{p.relative_to(file_path).as_posix()}  {fh.hexdigest()}\n".encode())
    digest = h.hexdigest()
    meta = _meta(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO archive_meta (key, value) VALUES (?, ?)",
        [("sha256", digest), ("sha256_scope", scope),
         ("sha256_fingerprint", f"{meta.get('pst_size')}:{meta.get('pst_mtime')}")],
    )
    conn.commit()
    return {"algorithm": "SHA-256", "value": digest, "scope": scope}
