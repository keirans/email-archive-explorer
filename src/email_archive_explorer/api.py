"""FastAPI backend application for Email Archive Explorer."""

from __future__ import annotations

import io
import mimetypes
import urllib.parse
import zipfile
from collections.abc import Iterable
from contextlib import asynccontextmanager
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from email_archive_explorer.attachments import (
    ArchiveContentError,
    attachment_detail,
    attachment_summary,
    extract_archive_member,
    list_archive_contents,
    list_attachments,
    unique_zip_name,
)
from email_archive_explorer.config import DEFAULT_CACHE_DIR, format_size
from email_archive_explorer.drivers import BaseArchiveDriver
from email_archive_explorer.fs_browser import BrowseError, FileSystemBrowser, PickerBusy, native_pick, picker_backend
from email_archive_explorer.indexer import (
    get_archive_analytics,
    get_archive_info,
    get_db_connection,
    get_folder_tree,
    get_message_position,
    get_message_thread,
    get_messages,
    search_messages,
)
from email_archive_explorer.inspector import archive_details, compute_checksum
from email_archive_explorer.manager import ActiveArchive, ArchiveManager, JobConflict
from email_archive_explorer.models import (
    ArchiveInfo,
    ArchiveListResponse,
    AttachmentSummary,
    FolderNode,
    MessageDetail,
    MessageSummary,
    NativePickRequest,
    OpenArchiveRequest,
    ScanDirectoryRequest,
    ScanResponse,
    SearchResult,
    ThreadWindow,
)
from email_archive_explorer.security import (
    LOOPBACK_HOSTS,
    HostCheckMiddleware,
    new_token,
    require_token,
)


class MessageListResponse(BaseModel):
    folder_id: int
    folder_name: str
    total: int
    page: int
    page_size: int
    total_pages: int
    items: list[MessageSummary]


class AttachmentZipRequest(BaseModel):
    ids: list[int]


class SearchResponse(BaseModel):
    query: str
    total: int
    page: int
    page_size: int
    total_pages: int
    results: list[SearchResult]


def create_app(
    archive_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    manager: ArchiveManager | None = None,
    allowed_hosts: Optional[Iterable[str]] = tuple(LOOPBACK_HOSTS),
    fs_enabled: bool = True,
    data_dir: str | Path = "data",
    browse_roots: Iterable[str | Path] = (),
) -> FastAPI:
    """Application factory for Email Archive Explorer.

    Pass an already-constructed ``ArchiveManager`` via *manager* (as the CLI
    does).  When *manager* is ``None``, one is created using *cache_dir*
    (default ``.cache/``) and the archive at *archive_path*, if given, is
    opened on startup. With no archive the UI starts on its "Open archive" screen.

    Security (Spec 08 §3.1): requests must carry an allowed ``Host`` header
    (``allowed_hosts=None`` disables the check), and state-changing or
    file-system routes need the per-process session token. *fs_enabled*
    switches the file-browser routes off (the CLI does this for non-loopback
    binds unless ``--allow-remote-browse`` is given).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Build / reuse ArchiveManager
        if manager is not None:
            mgr = manager
        else:
            mgr = ArchiveManager(cache_dir or DEFAULT_CACHE_DIR)
            if archive_path is not None:
                # Register & open the initial archive
                path = Path(archive_path).resolve()
                mgr.scan_directory(path.parent, recursive=False)
                mgr.open_archive(path)

        app.state.manager = mgr

        yield

        # Shutdown
        app.state.manager.close()

    app = FastAPI(
        title="Email Archive Explorer API",
        version="0.1.0",
        description="REST API for exploring, searching, and exporting email archives (PST, MBOX, EML).",
        lifespan=lifespan,
    )

    # No CORS middleware on purpose: the UI is same-origin, and other websites
    # must not be able to read this API. Host check blocks DNS rebinding.
    app.add_middleware(HostCheckMiddleware, allowed_hosts=allowed_hosts)
    app.state.token = new_token()
    app.state.fs_enabled = fs_enabled
    app.state.fs = FileSystemBrowser(data_dir, browse_roots)
    token_required = [Depends(require_token)]

    def get_active():
        """Pin the active archive for the whole request (blocks switches mid-request)."""
        try:
            with app.state.manager.session() as active:
                yield active
        except RuntimeError as exc:
            if "No archive" in str(exc):
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            raise

    def get_db(active: ActiveArchive = Depends(get_active)):
        conn = get_db_connection(active.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def get_reader(active: ActiveArchive = Depends(get_active)) -> BaseArchiveDriver:
        return active.reader

    # ==========================================
    # API Endpoints
    # ==========================================

    @app.get("/api/archive/info", response_model=ArchiveInfo)
    def api_archive_info(
        active: ActiveArchive = Depends(get_active),
        db=Depends(get_db),
    ):
        """Returns metadata, file size, folder and message counts."""
        return get_archive_info(db, active.file_path)

    @app.get("/api/folders", response_model=list[FolderNode])
    def api_folders(
        include_empty: bool = Query(
            False, description="Include folders with 0 messages"
        ),
        db=Depends(get_db),
    ):
        """Returns the hierarchical folder tree."""
        return get_folder_tree(db, include_empty=include_empty)

    @app.get("/api/folders/{folder_id}/messages", response_model=MessageListResponse)
    def api_folder_messages(
        folder_id: int,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        sort_by: str = Query("date", pattern="^(date|sender|subject)$"),
        sort_order: str = Query("desc", pattern="^(asc|desc)$"),
        has_attachments: Optional[bool] = Query(None),
        db=Depends(get_db),
    ):
        """Returns a paginated list of messages in a given folder."""
        folder = db.execute(
            "SELECT id, name FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")

        total, items = get_messages(
            db,
            folder_id=folder_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            has_attachments=has_attachments,
        )

        total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1

        return MessageListResponse(
            folder_id=folder["id"],
            folder_name=folder["name"],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            items=items,
        )

    @app.get("/api/messages/{message_id}", response_model=MessageDetail)
    def api_message_detail(
        message_id: int,
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Retrieves full email details, body, and attachment metadata."""
        msg_row = db.execute(
            """
            SELECT id, folder_id, folder_path, message_index, identifier,
                   subject, sender_name, sender_email, recipients_json, date_iso,
                   has_attachments, attachment_count
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()

        if not msg_row:
            raise HTTPException(status_code=404, detail="Message not found")

        # Extract live body and details directly from the archive
        detail = reader.get_message_detail(
            msg_row["folder_path"], msg_row["message_index"], msg_id=message_id
        )
        detail.folder_id = msg_row["folder_id"]

        # Fetch attachment IDs from database for consistent URLs
        att_rows = db.execute(
            """
            SELECT id, attachment_index, filename, size_bytes, mime_type
            FROM attachments
            WHERE message_id = ?
            ORDER BY attachment_index ASC
            """,
            (message_id,),
        ).fetchall()

        attachments = [
            AttachmentSummary(
                id=att["id"],
                message_id=message_id,
                attachment_index=att["attachment_index"],
                filename=att["filename"],
                size_bytes=att["size_bytes"],
                size_formatted=format_size(att["size_bytes"]),
                mime_type=att["mime_type"],
                download_url=f"/api/messages/{message_id}/attachments/{att['id']}?download=1",
                view_url=f"/api/messages/{message_id}/attachments/{att['id']}",
            )
            for att in att_rows
        ]
        detail.attachments = attachments
        return detail

    @app.get("/api/messages/{message_id}/attachments/{attachment_id}")
    def api_download_attachment(
        message_id: int,
        attachment_id: int,
        download: bool = Query(False),
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Streams raw attachment bytes directly from the archive."""
        att_row = db.execute(
            """
            SELECT a.attachment_index, a.filename, a.size_bytes, a.mime_type,
                   m.folder_path, m.message_index
            FROM attachments a
            JOIN messages m ON a.message_id = m.id
            WHERE a.id = ? AND a.message_id = ?
            """,
            (attachment_id, message_id),
        ).fetchone()

        if not att_row:
            raise HTTPException(status_code=404, detail="Attachment not found")

        filename, data = reader.get_attachment_bytes(
            att_row["folder_path"],
            att_row["message_index"],
            att_row["attachment_index"],
        )

        return _file_response(
            filename, data, att_row["mime_type"] or "application/octet-stream", download
        )

    # Types a browser would execute if opened inline from our origin.
    _ACTIVE_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
                     "application/xml", "message/rfc822", "multipart/related")

    def _file_response(filename: str, data: bytes, mime_type: str, download: bool) -> Response:
        disp_type = "attachment" if download else "inline"
        safe_fn = filename.encode("ascii", "ignore").decode().replace('"', "") or "attachment"
        encoded_fn = urllib.parse.quote(filename)
        headers = {
            "Content-Disposition": f'{disp_type}; filename="{safe_fn}"; filename*=UTF-8\'\'{encoded_fn}',
            "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff",
        }
        if mime_type.split(";")[0].strip().lower() in _ACTIVE_TYPES:
            # An HTML/SVG attachment must never run script against the local API.
            headers["Content-Security-Policy"] = "sandbox"
        return Response(content=data, media_type=mime_type, headers=headers)

    def _safe_name(subject: str, message_id: int) -> str:
        return (
            "".join(c for c in (subject or "") if c.isalnum() or c in (" ", "-", "_"))
            .strip()
            .replace(" ", "_")
        )[:40] or f"message_{message_id}"

    def _build_eml_data(reader: BaseArchiveDriver, msg_row: Any, db: Any) -> tuple[str, bytes]:
        message_id = msg_row["id"]

        # Formats that store real RFC 822 messages (MBOX, EML) export losslessly.
        raw = reader.get_raw_message(msg_row["folder_path"], msg_row["message_index"])
        if raw is not None:
            return _safe_name(msg_row["subject"], message_id), raw

        detail = reader.get_message_detail(
            msg_row["folder_path"], msg_row["message_index"], msg_id=message_id
        )

        eml = EmailMessage()
        eml["Subject"] = detail.subject
        eml["From"] = detail.sender_name or detail.sender_email or "unknown@local"

        to_list = [
            f"{r.name} <{r.email}>" if r.email else r.name
            for r in detail.recipients
            if r.recipient_type == "To"
        ]
        if to_list:
            eml["To"] = ", ".join(to_list)

        cc_list = [
            f"{r.name} <{r.email}>" if r.email else r.name
            for r in detail.recipients
            if r.recipient_type == "Cc"
        ]
        if cc_list:
            eml["Cc"] = ", ".join(cc_list)

        if detail.date_iso:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(detail.date_iso)
                eml["Date"] = formatdate(dt.timestamp())
            except Exception:
                pass

        eml.set_content(detail.body_plain or "")

        att_rows = db.execute(
            "SELECT attachment_index, filename, mime_type FROM attachments WHERE message_id = ?",
            (message_id,),
        ).fetchall()

        for a_row in att_rows:
            try:
                _, a_data = reader.get_attachment_bytes(
                    msg_row["folder_path"],
                    msg_row["message_index"],
                    a_row["attachment_index"],
                )
                m_type = a_row["mime_type"] or "application/octet-stream"
                maintype, subtype = (
                    m_type.split("/", 1) if "/" in m_type else ("application", "octet-stream")
                )
                eml.add_attachment(
                    a_data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=a_row["filename"],
                )
            except Exception:
                continue

        return _safe_name(detail.subject, message_id), eml.as_bytes()

    @app.get("/api/messages/{message_id}/eml")
    def api_export_eml(
        message_id: int,
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Synthesizes a valid standard RFC 822 / RFC 2822 .eml file."""
        msg_row = db.execute(
            """
            SELECT id, folder_path, message_index, subject, sender_name,
                   sender_email, recipients_json, date_iso
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()

        if not msg_row:
            raise HTTPException(status_code=404, detail="Message not found")

        safe_subject, eml_bytes = _build_eml_data(reader, msg_row, db)
        headers = {
            "Content-Disposition": f'attachment; filename="{safe_subject}.eml"',
        }
        return Response(
            content=eml_bytes,
            media_type="message/rfc822",
            headers=headers,
        )

    @app.get("/api/messages/{message_id}/position")
    def api_message_position(
        message_id: int,
        page_size: int = Query(50, ge=1, le=200),
        sort_by: str = Query("date", pattern="^(date|sender|subject)$"),
        sort_order: str = Query("desc", pattern="^(asc|desc)$"),
        has_attachments: Optional[bool] = Query(None),
        db=Depends(get_db),
    ):
        """Folder and page on which a message appears in the folder listing."""
        pos = get_message_position(db, message_id, page_size, sort_by, sort_order, has_attachments)
        if pos is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return pos

    @app.get("/api/messages/{message_id}/thread", response_model=ThreadWindow)
    def api_message_thread(
        message_id: int,
        limit: int = Query(50, ge=1, le=200),
        offset: Optional[int] = Query(None, ge=0),
        db=Depends(get_db),
    ):
        """A window of the message's conversation, oldest first (Spec 10 §2.3)."""
        thread = get_message_thread(db, message_id, limit=limit, offset=offset)
        if thread is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return thread

    @app.get("/api/archive/analytics")
    def api_archive_analytics(db=Depends(get_db)):
        """Returns monthly email activity, sender ranking, and attachment metrics."""
        return get_archive_analytics(db)

    @app.get("/api/folders/{folder_id}/zip")
    def api_export_folder_zip(
        folder_id: int,
        limit: int = Query(500, le=2000),
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Exports all emails in a folder into a streamed ZIP archive of .eml files."""
        folder = db.execute("SELECT id, name FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")

        msg_rows = db.execute(
            """
            SELECT id, folder_path, message_index, subject, sender_name,
                   sender_email, recipients_json, date_iso
            FROM messages
            WHERE folder_id = ?
            ORDER BY date_iso ASC
            LIMIT ?
            """,
            (folder_id, limit),
        ).fetchall()

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, row in enumerate(msg_rows):
                try:
                    safe_sub, eml_bytes = _build_eml_data(reader, row, db)
                    fn = f"{idx + 1:04d}_{row['id']}_{safe_sub}.eml"
                    zf.writestr(fn, eml_bytes)
                except Exception:
                    continue

        safe_folder_name = "".join(c for c in folder["name"] if c.isalnum() or c in (" ", "-", "_")).strip() or "folder"
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_folder_name}.zip"',
            },
        )

    @app.get("/api/search", response_model=SearchResponse)
    def api_search(
        q: str = Query("", description="Full-text query string"),
        folder_id: Optional[int] = Query(None),
        has_attachments: Optional[bool] = Query(None),
        sender: Optional[str] = Query(None),
        attachment_ext: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        db=Depends(get_db),
    ):
        """Executes full-text search across subject, sender, and body with advanced filters."""
        total, results = search_messages(
            db,
            query_str=q,
            folder_id=folder_id,
            has_attachments=has_attachments,
            sender=sender,
            attachment_ext=attachment_ext,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )
        total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1

        return SearchResponse(
            query=q,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            results=results,
        )

    @app.get("/api/search/zip")
    def api_export_search_zip(
        q: str = Query("", description="Full-text query string"),
        folder_id: Optional[int] = Query(None),
        has_attachments: Optional[bool] = Query(None),
        sender: Optional[str] = Query(None),
        attachment_ext: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        limit: int = Query(200, le=500),
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Exports matching search results into a streamed ZIP archive of .eml files."""
        total, results = search_messages(
            db,
            query_str=q,
            folder_id=folder_id,
            has_attachments=has_attachments,
            sender=sender,
            attachment_ext=attachment_ext,
            date_from=date_from,
            date_to=date_to,
            page=1,
            page_size=limit,
        )

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, res in enumerate(results):
                msg_row = db.execute(
                    "SELECT id, folder_path, message_index, subject, sender_name, sender_email, recipients_json, date_iso FROM messages WHERE id = ?",
                    (res.id,),
                ).fetchone()
                if msg_row:
                    try:
                        safe_sub, eml_bytes = _build_eml_data(reader, msg_row, db)
                        fn = f"{idx + 1:04d}_{res.id}_{safe_sub}.eml"
                        zf.writestr(fn, eml_bytes)
                    except Exception:
                        continue

        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="search_results.zip"',
            },
        )

    # ==========================================
    # Attachment Explorer Endpoints (Spec 09)
    # ==========================================

    def _attachment_filters(
        q: Optional[str] = Query(None, description="Filename contains"),
        category: Optional[str] = Query(None),
        ext: Optional[str] = Query(None),
        year: Optional[str] = Query(None, pattern=r"^\d{4}$"),
        sender: Optional[str] = Query(None),
        folder_id: Optional[int] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        min_size: Optional[int] = Query(None, ge=0),
        max_size: Optional[int] = Query(None, ge=0),
        hide_noise: bool = Query(True),
        rare_only: bool = Query(False),
    ) -> dict[str, Any]:
        return dict(q=q, category=category, ext=ext, year=year, sender=sender,
                    folder_id=folder_id, date_from=date_from, date_to=date_to,
                    min_size=min_size, max_size=max_size, hide_noise=hide_noise,
                    rare_only=rare_only)

    @app.get("/api/attachments")
    def api_list_attachments(
        filters: dict[str, Any] = Depends(_attachment_filters),
        collapse: bool = Query(True, description="One row per duplicate group"),
        sort: str = Query("date", pattern="^(date|size|name|sender|copies|type)$"),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(60, ge=1, le=200),
        db=Depends(get_db),
    ):
        """Lists attachments across the whole archive with their origin messages."""
        return list_attachments(db, sort=sort, order=order, page=page,
                                page_size=page_size, collapse=collapse, **filters)

    @app.get("/api/attachments/summary")
    def api_attachment_summary(hide_noise: bool = Query(True), db=Depends(get_db)):
        """Discover dashboard: categories, years, rare types, largest and most-forwarded files."""
        return attachment_summary(db, hide_noise=hide_noise)

    @app.post("/api/attachments/zip", dependencies=token_required)
    def api_attachments_zip(
        body: AttachmentZipRequest,
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Bundles the selected attachments into one ZIP download."""
        ids = list(dict.fromkeys(body.ids))
        if not ids:
            raise HTTPException(status_code=400, detail="No attachments selected")
        if len(ids) > 500:
            raise HTTPException(status_code=400, detail="At most 500 attachments per ZIP")

        marks = ",".join("?" * len(ids))
        rows = db.execute(
            f"""SELECT a.id, a.attachment_index, m.folder_path, m.message_index
                FROM attachments a JOIN messages m ON m.id = a.message_id
                WHERE a.id IN ({marks})""",
            ids,
        ).fetchall()
        by_id = {r["id"]: r for r in rows}

        used: set = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for att_id in ids:
                r = by_id.get(att_id)
                if r is None:
                    continue
                try:
                    name, data = reader.get_attachment_bytes(
                        r["folder_path"], r["message_index"], r["attachment_index"]
                    )
                except Exception:
                    continue
                zf.writestr(unique_zip_name(name, used), data)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="attachments.zip"'},
        )

    def _attachment_bytes(attachment_id: int, db: Any, reader: BaseArchiveDriver) -> tuple[str, bytes]:
        row = db.execute(
            """SELECT a.attachment_index, m.folder_path, m.message_index
               FROM attachments a JOIN messages m ON m.id = a.message_id WHERE a.id = ?""",
            (attachment_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
        return reader.get_attachment_bytes(row["folder_path"], row["message_index"], row["attachment_index"])

    @app.get("/api/attachments/{attachment_id}")
    def api_attachment_detail(attachment_id: int, db=Depends(get_db)):
        """One attachment with its origin message and every other copy in the archive."""
        detail = attachment_detail(db, attachment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
        return detail

    @app.get("/api/attachments/{attachment_id}/contents")
    def api_attachment_contents(
        attachment_id: int,
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Lists the files inside a ZIP or TAR attachment."""
        _, data = _attachment_bytes(attachment_id, db, reader)
        try:
            return list_archive_contents(data)
        except ArchiveContentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/attachments/{attachment_id}/contents/{index}")
    def api_attachment_member(
        attachment_id: int,
        index: int,
        db=Depends(get_db),
        reader: BaseArchiveDriver = Depends(get_reader),
    ):
        """Downloads one file from inside a ZIP or TAR attachment."""
        _, data = _attachment_bytes(attachment_id, db, reader)
        try:
            name, payload = extract_archive_member(data, index)
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="No such file in archive") from exc
        except ArchiveContentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return _file_response(name, payload, mime, download=True)

    # ==========================================
    # Archive Management Endpoints (Spec 06)
    # ==========================================

    @app.get("/api/archives", response_model=ArchiveListResponse)
    def api_list_archives():
        """Lists all known archives and identifies the currently active one."""
        mgr: ArchiveManager = app.state.manager
        return ArchiveListResponse(
            active_archive_id=mgr.active_archive_id,
            archives=mgr.get_known_archives(),
        )

    @app.post("/api/archives/open", dependencies=token_required,
              responses={202: {"description": "Indexing started; poll status_url"}})
    def api_open_archive(body: OpenArchiveRequest):
        """Switches the active archive.

        Returns 200 with the ``ArchiveEntry`` when the archive's cache is
        already valid (or ``wait`` is true); otherwise 202 with a job to poll
        while the current archive keeps serving requests.
        """
        mgr: ArchiveManager = app.state.manager
        try:
            if not body.wait and mgr.needs_indexing(body.file_path, body.force_reindex):
                job = mgr.start_open_job(body.file_path, force_reindex=body.force_reindex)
                return JSONResponse(status_code=202, content=job.as_dict())
            entry = mgr.open_archive(body.file_path, force_reindex=body.force_reindex)
        except JobConflict as exc:
            return JSONResponse(status_code=409, content={
                "detail": f"Already opening {exc.job.name}", "job": exc.job.as_dict()})
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to open archive: {exc}") from exc
        return entry

    @app.get("/api/archives/jobs/{job_id}")
    def api_open_job(job_id: str):
        """Progress of a background archive open (Spec 08 §3.4)."""
        job = app.state.manager.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job.as_dict()

    # ==========================================
    # File browser & native dialog (Spec 08 §3.2–3.3)
    # ==========================================

    def _fs_guard() -> None:
        if not app.state.fs_enabled:
            raise HTTPException(status_code=403, detail="File browsing is disabled for remote access")

    fs_deps = [Depends(require_token), Depends(_fs_guard)]

    @app.get("/api/fs/roots", dependencies=fs_deps)
    def api_fs_roots():
        """Places the file browser may show, plus recently opened archives."""
        recent = [
            {"name": a.name, "path": a.file_path, "format_type": a.format_type,
             "last_opened_iso": a.last_opened_iso}
            for a in app.state.manager.get_known_archives() if a.last_opened_iso
        ][:12]
        return {"roots": app.state.fs.root_entries(), "recent": recent,
                "native_picker_available": picker_backend() is not None}

    @app.get("/api/fs/list", dependencies=fs_deps)
    def api_fs_list(path: str = Query(...), show_hidden: bool = Query(False)):
        """Lists one folder (confined to the roots), flagging recognised archives."""
        try:
            return app.state.fs.list_dir(path, show_hidden=show_hidden)
        except BrowseError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.post("/api/fs/native-pick", dependencies=fs_deps)
    def api_fs_native_pick(body: Optional[NativePickRequest] = None):
        """Shows the operating system's own file dialog; blocks until it closes."""
        mode = body.mode if body and body.mode in ("file", "directory") else "file"
        try:
            return {"path": native_pick(mode)}
        except PickerBusy as exc:
            raise HTTPException(status_code=409, detail="A file dialog is already open") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

    # ==========================================
    # Archive Inspector (Spec 08 §3.5)
    # ==========================================

    @app.get("/api/archive/details")
    def api_archive_details(active: ActiveArchive = Depends(get_active), db=Depends(get_db)):
        """Technical detail about the open archive: file, format header, contents, index."""
        return archive_details(db, active.file_path, active.db_path, active.reader)

    @app.post("/api/archive/checksum", dependencies=token_required)
    def api_archive_checksum(active: ActiveArchive = Depends(get_active), db=Depends(get_db)):
        """SHA-256 of the archive, computed on demand and cached."""
        return compute_checksum(db, active.file_path)

    @app.post("/api/archives/scan", response_model=ScanResponse, dependencies=token_required)
    def api_scan_archives(body: Optional[ScanDirectoryRequest] = None):
        """Scans a directory for supported archive files and registers new ones."""
        mgr: ArchiveManager = app.state.manager
        directory = (body.directory if body else "data/")
        recursive = (body.recursive if body else True)
        result = mgr.scan_directory(directory, recursive=recursive)
        return ScanResponse(
            discovered_count=result["discovered_count"],
            newly_added=result["newly_added"],
        )

    # Static assets fallback
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_root():
        index_html = static_dir / "index.html"
        if index_html.exists():
            # The session token reaches the UI only through this same-origin page.
            page = index_html.read_text(encoding="utf-8").replace(
                '<meta name="eae-token" content="">',
                f'<meta name="eae-token" content="{app.state.token}">',
            )
            return HTMLResponse(page, headers={"Cache-Control": "no-store"})
        return {
            "message": "Email Archive Explorer API is running.",
            "docs": "/docs",
            "archive_info": "/api/archive/info",
        }

    return app


# Default singleton instance for uvicorn
app = create_app()
