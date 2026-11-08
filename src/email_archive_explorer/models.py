"""Data models for Email Archive Explorer."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Recipient(BaseModel):
    name: str = ""
    email: str = ""
    recipient_type: str = "To"  # To, Cc, Bcc


class AttachmentSummary(BaseModel):
    id: int = 0
    message_id: int = 0
    attachment_index: int = 0
    filename: str
    size_bytes: int
    size_formatted: str = ""
    mime_type: str = "application/octet-stream"
    download_url: str = ""
    view_url: str = ""


class MessageSummary(BaseModel):
    id: int
    folder_id: int
    folder_path: str
    message_index: int
    identifier: int = 0
    subject: str = "(No Subject)"
    sender_name: str = ""
    sender_email: str = ""
    date_iso: Optional[str] = None
    has_attachments: bool = False
    attachment_count: int = 0
    body_preview: str = ""
    thread_size: int = 1


class MessageDetail(BaseModel):
    id: int
    folder_id: int
    folder_path: str
    message_index: int
    identifier: int = 0
    subject: str = "(No Subject)"
    sender_name: str = ""
    sender_email: str = ""
    date_iso: Optional[str] = None
    has_attachments: bool = False
    attachment_count: int = 0
    recipients: list[Recipient] = Field(default_factory=list)
    body_plain: str = ""
    body_html: Optional[str] = None
    attachments: list[AttachmentSummary] = Field(default_factory=list)
    transport_headers: Optional[str] = None
    # Conversation signals gathered while indexing (Spec 10); never serialised
    message_id_hdr: Optional[str] = Field(default=None, exclude=True)
    in_reply_to: Optional[str] = Field(default=None, exclude=True)
    references: list[str] = Field(default_factory=list, exclude=True)
    conversation_key: Optional[str] = Field(default=None, exclude=True)
    conversation_topic: Optional[str] = Field(default=None, exclude=True)


class FolderNode(BaseModel):
    id: int
    parent_id: Optional[int] = None
    name: str
    full_path: str
    depth: int = 0
    message_count: int = 0
    children: list[FolderNode] = Field(default_factory=list)


class DateRange(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None


class ArchiveInfo(BaseModel):
    file_path: str
    file_size_bytes: int
    file_size_formatted: str
    format: str = "Unknown"
    total_folders: int = 0
    total_messages: int = 0
    total_attachments: int = 0
    date_range: DateRange = Field(default_factory=DateRange)


class SearchResult(BaseModel):
    id: int
    folder_id: int
    folder_path: str
    subject: str
    sender_name: str
    date_iso: Optional[str] = None
    has_attachments: bool = False
    attachment_count: int = 0
    snippet: str = ""
    thread_size: int = 1


# ---------------------------------------------------------------------------
# Multi-archive manager models (Spec 06)
# ---------------------------------------------------------------------------

class ArchiveEntry(BaseModel):
    id: str
    name: str
    file_path: str
    format_type: str
    file_size_bytes: int
    file_size_formatted: str
    is_active: bool = False
    total_messages: int = 0
    total_folders: int = 0
    last_opened_iso: Optional[str] = None


class ArchiveListResponse(BaseModel):
    active_archive_id: Optional[str]
    archives: list[ArchiveEntry]


class OpenArchiveRequest(BaseModel):
    file_path: str
    wait: bool = False           # True: block until indexed (always 200)
    force_reindex: bool = False


class NativePickRequest(BaseModel):
    mode: str = "file"           # "file" | "directory"


class ScanDirectoryRequest(BaseModel):
    directory: str = "data/"
    recursive: bool = True


class ScanResponse(BaseModel):
    discovered_count: int
    newly_added: list[str]


class ThreadWindow(BaseModel):
    """A window of one conversation around a message (Spec 10 §2.3)."""

    thread_id: int
    size: int
    position: int                 # 1-based position of the requested message
    method: str                   # 'headers' | 'conversation-index' | 'subject' | 'single'
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    offset: int = 0               # index of items[0] within the conversation
    items: list[MessageSummary] = Field(default_factory=list)
