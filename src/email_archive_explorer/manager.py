"""ArchiveManager: multi-archive lifecycle controller for Email Archive Explorer."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from email_archive_explorer.config import DEFAULT_CACHE_DIR, format_size
from email_archive_explorer.drivers import ArchiveDriverRegistry, BaseArchiveDriver, EmlDirDriver
from email_archive_explorer.drivers.eml_dir_driver import is_mail_folder
from email_archive_explorer.indexer import build_index, get_archive_info, get_db_connection, is_cache_valid
from email_archive_explorer.models import ArchiveEntry


def _archive_id(path: Path) -> str:
    """Deterministic 8-character ID derived from the absolute file path."""
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]


def _cache_path_for(archive_id: str, cache_dir: Path) -> Path:
    return cache_dir / f"cache_{archive_id}.db"


def cache_path_for_archive(archive_path: str | Path, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Path:
    """Per-archive cache file used by both the web server and the CLI."""
    return _cache_path_for(_archive_id(Path(archive_path)), Path(cache_dir).resolve())


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        try:
            return EmlDirDriver(path).fingerprint()[0]
        except OSError:
            return 0
    return 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _ReadWriteLock:
    """Writer-preferring readers/writer lock.

    Not tied to thread identity, so a request may acquire it in one worker
    thread and release it in another (as FastAPI does with yield-dependencies).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._writers_waiting:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()


@dataclass(frozen=True)
class ActiveArchive:
    """Consistent snapshot of the active archive, valid while a session is held."""

    archive_id: str
    file_path: Path
    db_path: Path
    reader: BaseArchiveDriver


@dataclass
class OpenJob:
    """Progress of a background ``open_archive`` call."""

    id: str
    file_path: str
    name: str
    format_type: str
    state: str = "indexing"          # indexing | done | error
    indexed: int = 0
    total: int = 0
    current_subject: str = ""
    archive: Optional[ArchiveEntry] = None
    error: Optional[str] = None
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "job_id": self.id,
            "status_url": f"/api/archives/jobs/{self.id}",
            "state": self.state,
            "file_path": self.file_path,
            "name": self.name,
            "format_type": self.format_type,
            "indexed": self.indexed,
            "total": self.total,
            "current_subject": self.current_subject,
            "archive": self.archive.model_dump() if self.archive else None,
            "error": self.error,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
        }


class JobConflict(Exception):
    def __init__(self, job: OpenJob):
        super().__init__(f"Already opening {job.name}")
        self.job = job


class ArchiveManager:
    """
    Manages the lifecycle of multiple archive files.

    * Maintains a catalog SQLite database of known archives.
    * Opens and indexes archives on demand, each into its own cache file.
    * Keeps exactly one archive active at a time.
    * Thread-safe: requests hold a shared lock on the active archive via
      :meth:`session`; switching takes an exclusive lock only for the brief
      swap, so in-flight requests never see a closed reader.
    """

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> None:
        self._cache_dir = Path(cache_dir).resolve()
        self._catalog_path = self._cache_dir / "catalog.db"

        self._rw = _ReadWriteLock()          # guards the active archive
        self._open_lock = threading.Lock()   # serialises open_archive calls
        self._catalog_lock = threading.Lock()

        self._active: Optional[ActiveArchive] = None
        self._jobs: dict[str, OpenJob] = {}
        self._jobs_lock = threading.Lock()

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._init_catalog()

    # ------------------------------------------------------------------
    # Catalog helpers
    # ------------------------------------------------------------------

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def _init_catalog(self) -> None:
        """Create the catalog schema if it does not exist."""
        conn = sqlite3.connect(str(self._catalog_path))
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS known_archives (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                file_path       TEXT NOT NULL UNIQUE,
                format_type     TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                total_messages  INTEGER NOT NULL DEFAULT 0,
                total_folders   INTEGER NOT NULL DEFAULT 0,
                last_opened_iso TEXT,
                last_scanned_iso TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_directories (
                path      TEXT PRIMARY KEY,
                recursive INTEGER DEFAULT 1
            );
            """
        )
        conn.commit()
        conn.close()

    def _catalog_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._catalog_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _upsert_archive(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        file_path: Path,
        total_messages: int = 0,
        total_folders: int = 0,
        last_opened_iso: Optional[str] = None,
        is_scan: bool = False,
        format_type: str = "unknown",
        file_size: Optional[int] = None,
    ) -> None:
        if file_size is None:
            file_size = _path_size(file_path)

        conn.execute(
            """
            INSERT INTO known_archives
                (id, name, file_path, format_type, file_size_bytes,
                 total_messages, total_folders, last_opened_iso, last_scanned_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                file_path        = excluded.file_path,
                format_type      = excluded.format_type,
                file_size_bytes  = excluded.file_size_bytes,
                total_messages   = CASE WHEN excluded.total_messages > 0
                                        THEN excluded.total_messages
                                        ELSE known_archives.total_messages END,
                total_folders    = CASE WHEN excluded.total_folders > 0
                                        THEN excluded.total_folders
                                        ELSE known_archives.total_folders END,
                last_opened_iso  = CASE WHEN excluded.last_opened_iso IS NOT NULL
                                        THEN excluded.last_opened_iso
                                        ELSE known_archives.last_opened_iso END,
                last_scanned_iso = CASE WHEN ? THEN excluded.last_scanned_iso
                                        ELSE known_archives.last_scanned_iso END
            """,
            (
                archive_id,
                file_path.name,
                str(file_path.resolve()),
                format_type,
                file_size,
                total_messages,
                total_folders,
                last_opened_iso,
                _now_iso(),  # last_scanned_iso
                is_scan,     # used in CASE
            ),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_known_archives(self) -> list[ArchiveEntry]:
        """Return all known archives that still exist on disk; marks the active one."""
        active_id = self.active_archive_id
        with self._catalog_lock:
            conn = self._catalog_conn()
            rows = conn.execute(
                """
                SELECT id, name, file_path, format_type,
                       file_size_bytes, total_messages, total_folders, last_opened_iso
                FROM known_archives
                ORDER BY last_opened_iso DESC NULLS LAST, name ASC
                """
            ).fetchall()
            conn.close()

        return [
            ArchiveEntry(
                id=row["id"],
                name=row["name"],
                file_path=row["file_path"],
                format_type=row["format_type"],
                file_size_bytes=row["file_size_bytes"],
                file_size_formatted=format_size(row["file_size_bytes"]),
                is_active=(row["id"] == active_id),
                total_messages=row["total_messages"],
                total_folders=row["total_folders"],
                last_opened_iso=row["last_opened_iso"],
            )
            for row in rows
            if row["id"] == active_id or Path(row["file_path"]).exists()
        ]

    @staticmethod
    def _discover(base: Path, recursive: bool) -> list[tuple]:
        """Find archives under ``base`` as ``(path, format_type)`` pairs.

        Files are identified by magic bytes or extension. A directory holding
        ``.eml`` files or a Maildir is one archive, and its contents are not
        scanned further. Hidden files and directories are skipped.
        """
        found: list[tuple] = []

        def _file_archive(fp: Path) -> Optional[str]:
            if fp.name.startswith(".") or fp.suffix.lower() == ".eml":
                return None
            drv = ArchiveDriverRegistry.probe(fp)
            if drv is None:
                ext = fp.suffix.lower()
                drv = next(
                    (d for d in ArchiveDriverRegistry.drivers()
                     if not d.is_directory_format and ext in d.supported_extensions()),
                    None,
                )
            return drv.format_type if drv is not None else None

        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            if is_mail_folder(d):
                found.append((d, EmlDirDriver.format_type))
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                fmt = _file_archive(d / name)
                if fmt:
                    found.append((d / name, fmt))
            dirnames[:] = sorted(n for n in dirnames if not n.startswith(".")) if recursive else []
        return found

    def scan_directory(
        self,
        directory: str | Path = "data/",
        recursive: bool = True,
    ) -> dict:
        """
        Discover archives (any registered format) under ``directory`` and
        register them in the catalog.  Returns a dict with ``discovered_count``
        and ``newly_added`` (list of absolute path strings).
        """
        base = Path(directory).resolve()
        if not base.is_dir():
            return {"discovered_count": 0, "newly_added": []}

        found = sorted({(p.resolve(), fmt) for p, fmt in self._discover(base, recursive)})

        newly_added: list[str] = []
        with self._catalog_lock:
            conn = self._catalog_conn()
            existing = {
                row["file_path"]
                for row in conn.execute("SELECT file_path FROM known_archives").fetchall()
            }
            for fp, fmt in found:
                self._upsert_archive(conn, _archive_id(fp), fp, is_scan=True, format_type=fmt)
                if str(fp) not in existing:
                    newly_added.append(str(fp))
            conn.execute(
                "INSERT OR REPLACE INTO scan_directories (path, recursive) VALUES (?, ?)",
                (str(base), int(recursive)),
            )
            conn.commit()
            conn.close()

        return {"discovered_count": len(found), "newly_added": newly_added}

    @staticmethod
    def validate_archive(file_path: str | Path):
        """Resolve and check a path; returns ``(path, driver_class)`` or raises
        FileNotFoundError / PermissionError / ValueError (unsupported format)."""
        fp = Path(file_path).expanduser().resolve()
        if not fp.exists():
            raise FileNotFoundError(f"Archive not found: {fp}")
        if not os.access(fp, os.R_OK | (os.X_OK if fp.is_dir() else 0)):
            raise PermissionError(f"Cannot read archive: {fp}")
        try:
            driver_cls = ArchiveDriverRegistry.detect_driver_class(fp)
        except PermissionError:
            raise
        except OSError as exc:
            raise PermissionError(f"Cannot read archive: {fp} ({exc})") from exc
        return fp, driver_cls

    def needs_indexing(self, file_path: str | Path, force_reindex: bool = False) -> bool:
        """True if opening this archive would (re)build its cache."""
        fp, _ = self.validate_archive(file_path)
        if force_reindex:
            return True
        return not is_cache_valid(fp, _cache_path_for(_archive_id(fp), self._cache_dir))

    def cache_path(self, file_path: str | Path) -> Path:
        return _cache_path_for(_archive_id(Path(file_path).resolve()), self._cache_dir)

    # ------------------------------------------------------------------
    # Background open jobs (Spec 08 §3.4)
    # ------------------------------------------------------------------

    def start_open_job(self, file_path: str | Path, force_reindex: bool = False) -> OpenJob:
        """Validate now, then index + activate in a background thread."""
        fp, driver_cls = self.validate_archive(file_path)
        with self._jobs_lock:
            running = next((j for j in self._jobs.values() if j.state == "indexing"), None)
            if running is not None:
                raise JobConflict(running)
            job = OpenJob(id=uuid.uuid4().hex[:12], file_path=str(fp), name=fp.name,
                          format_type=driver_cls.format_type)
            self._jobs[job.id] = job

        def _progress(done: int, total: int, subject: str) -> None:
            job.indexed, job.total, job.current_subject = done, total, (subject or "")[:120]

        def _run() -> None:
            try:
                job.archive = self.open_archive(fp, force_reindex=force_reindex, progress=_progress)
                job.indexed = job.total = job.archive.total_messages
                job.state = "done"
            except Exception as exc:  # surfaced to the UI via the job status
                job.error = str(exc)
                job.state = "error"
            finally:
                job.finished = time.time()

        threading.Thread(target=_run, name=f"open-{job.id}", daemon=True).start()
        return job

    def get_job(self, job_id: str) -> Optional[OpenJob]:
        with self._jobs_lock:
            # Forget finished jobs after 10 minutes
            cutoff = time.time() - 600
            for jid in [j.id for j in self._jobs.values() if j.finished and j.finished < cutoff]:
                self._jobs.pop(jid, None)
            return self._jobs.get(job_id)

    def open_archive(
        self,
        file_path: str | Path,
        force_reindex: bool = False,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> ArchiveEntry:
        """
        Make ``file_path`` the active archive:

        1. Validate the path exists, is readable, and has a supported format
           (detected by the driver registry: magic bytes, then extension).
        2. Build / load the per-archive SQLite cache (outside the swap lock, so
           the current archive keeps serving requests while indexing runs).
        3. Open a fresh driver for the archive.
        4. Swap it in under an exclusive lock, closing the previous reader.
        5. Update catalog timestamps and message counts.

        Returns the activated ``ArchiveEntry``.
        """
        fp, driver_cls = self.validate_archive(file_path)
        fmt = driver_cls.format_type

        aid = _archive_id(fp)
        db_p = _cache_path_for(aid, self._cache_dir)

        with self._open_lock:
            current = self._active
            already_active = current is not None and current.archive_id == aid

            if not already_active or force_reindex:
                if already_active:
                    # Rebuilding the live cache: block readers for the duration.
                    self._rw.acquire_write()
                    try:
                        build_index(fp, db_p, force=True, progress_callback=progress)
                    finally:
                        self._rw.release_write()
                else:
                    build_index(fp, db_p, force=force_reindex, progress_callback=progress)

            cache_conn = get_db_connection(db_p)
            try:
                info = get_archive_info(cache_conn, fp)
            finally:
                cache_conn.close()

            if not already_active:
                reader = driver_cls(fp)
                reader.open()
                new_active = ActiveArchive(archive_id=aid, file_path=fp, db_path=db_p, reader=reader)

                self._rw.acquire_write()
                try:
                    old = self._active
                    self._active = new_active
                finally:
                    self._rw.release_write()

                if old is not None:
                    try:
                        old.reader.close()
                    except Exception:
                        pass

            now_iso = _now_iso()
            with self._catalog_lock:
                cat_conn = self._catalog_conn()
                self._upsert_archive(
                    cat_conn,
                    aid,
                    fp,
                    total_messages=info.total_messages,
                    total_folders=info.total_folders,
                    last_opened_iso=now_iso,
                    format_type=fmt,
                    file_size=info.file_size_bytes,
                )
                cat_conn.commit()
                cat_conn.close()

        size = info.file_size_bytes
        return ArchiveEntry(
            id=aid,
            name=fp.name,
            file_path=str(fp),
            format_type=fmt,
            file_size_bytes=size,
            file_size_formatted=format_size(size),
            is_active=True,
            total_messages=info.total_messages,
            total_folders=info.total_folders,
            last_opened_iso=now_iso,
        )

    @contextmanager
    def session(self) -> Iterator[ActiveArchive]:
        """Hold a shared lock on the active archive for the duration of a request.

        The yielded snapshot (reader + cache path) is guaranteed to stay open
        and consistent until the context exits, even if another thread
        switches archives in the meantime.
        """
        self._rw.acquire_read()
        try:
            if self._active is None:
                raise RuntimeError("No archive is currently open.")
            yield self._active
        finally:
            self._rw.release_read()

    def get_active_reader(self) -> BaseArchiveDriver:
        """Return the currently active archive driver (raises if none open).

        Prefer :meth:`session` inside request handlers; this accessor does not
        protect against a concurrent switch.
        """
        active = self._active
        if active is None:
            raise RuntimeError("No archive is currently open.")
        return active.reader

    def get_active_db_path(self) -> Path:
        """Return the SQLite cache path for the currently active archive."""
        active = self._active
        if active is None:
            raise RuntimeError("No archive is currently open.")
        return active.db_path

    def get_active_file_path(self) -> Path:
        """Return the archive file path for the currently active archive."""
        active = self._active
        if active is None:
            raise RuntimeError("No archive is currently open.")
        return active.file_path

    @property
    def active_archive_id(self) -> Optional[str]:
        active = self._active
        return active.archive_id if active else None

    def close(self) -> None:
        """Shut down cleanly, waiting for in-flight requests to finish."""
        with self._open_lock:
            self._rw.acquire_write()
            try:
                old, self._active = self._active, None
            finally:
                self._rw.release_write()
            if old is not None:
                try:
                    old.reader.close()
                except Exception:
                    pass
