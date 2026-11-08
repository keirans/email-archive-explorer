"""Shared RFC 822 / MIME parsing for drivers that store standard messages (MBOX, EML)."""

from __future__ import annotations

import html
import mimetypes
import re
from datetime import UTC
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Optional

from email_archive_explorer.config import format_size
from email_archive_explorer.models import AttachmentSummary, MessageDetail, Recipient
from email_archive_explorer.threads import parse_msgid, parse_msgid_list

_RFC822_HEADER_RE = re.compile(
    rb"^(date|from|subject|to|mime-version|message-id|received|return-path|delivered-to):",
    re.IGNORECASE | re.MULTILINE,
)


def looks_like_rfc822(head: bytes) -> bool:
    """Heuristic: at least two well-known header names at line starts."""
    found = {m.group(1).lower() for m in _RFC822_HEADER_RE.finditer(head)}
    return len(found) >= 2


def parse_message(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def _header_str(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:
        return ""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _addresses(msg: Message, name: str) -> list[tuple[str, str]]:
    try:
        values = [str(v) for v in msg.get_all(name, [])]
    except Exception:
        return []
    return [(n.strip(), e.strip()) for n, e in getaddresses(values) if n or e]


def _date_iso(msg: Message) -> Optional[str]:
    raw = _header_str(msg, "Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None:
        return None
    # Store naive UTC, matching the PST driver, so sorting/filtering is consistent.
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat()


def _part_text(part: Message) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("latin-1", errors="replace")


_TAG_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.IGNORECASE)
_DROP_BLOCK_RE = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    text = _DROP_BLOCK_RE.sub(" ", markup)
    text = _TAG_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _bodies(msg: EmailMessage) -> tuple[Optional[Message], Optional[Message]]:
    try:
        plain = msg.get_body(preferencelist=("plain",))
    except Exception:
        plain = None
    try:
        html_part = msg.get_body(preferencelist=("html",))
    except Exception:
        html_part = None
    return plain, html_part


def attachment_parts(msg: EmailMessage) -> list[Message]:
    """Attachment parts in a stable order. Attached emails count as one attachment."""
    plain, html_part = _bodies(msg)
    body_ids = {id(p) for p in (plain, html_part) if p is not None}
    out: list[Message] = []

    def _walk(part: Message, is_root: bool) -> None:
        ctype = part.get_content_type()
        if ctype == "message/rfc822" and not is_root:
            out.append(part)
            return
        if part.is_multipart():
            for sub in part.iter_parts() if hasattr(part, "iter_parts") else part.get_payload():
                _walk(sub, False)
            return
        if id(part) in body_ids:
            return
        disposition = part.get_content_disposition()
        if (
            disposition == "attachment"
            or part.get_filename()
            or part.get_content_maintype() not in ("text", "multipart")
        ):
            out.append(part)

    _walk(msg, True)
    return out


def attachment_payload(part: Message, index: int) -> tuple[str, bytes, str]:
    """``(filename, bytes, mime_type)`` for an attachment part."""
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        inner = part.get_payload(0) if part.is_multipart() else part.get_payload()
        data = inner.as_bytes() if isinstance(inner, Message) else b""
        subject = _header_str(inner, "Subject") if isinstance(inner, Message) else ""
        name = part.get_filename() or f"{(subject or f'attached_message_{index + 1}')[:60]}.eml"
        return name, data, ctype

    data = part.get_payload(decode=True) or b""
    name = part.get_filename()
    if not name:
        ext = mimetypes.guess_extension(ctype) or ".bin"
        name = f"attachment_{index + 1}{ext}"
    return name, data, ctype


def _header_block(raw: bytes) -> Optional[str]:
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = raw.find(sep)
        if idx != -1:
            return raw[:idx].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace") if raw else None


def message_to_detail(
    raw: bytes,
    folder_path: str,
    message_index: int,
    msg_id: int = 0,
    identifier: int = 0,
    include_html: bool = True,
) -> MessageDetail:
    msg = parse_message(raw)

    subject = _header_str(msg, "Subject") or "(No Subject)"
    sender_name, sender_email = parseaddr(_header_str(msg, "From"))
    if not sender_name and not sender_email:
        sender_name, sender_email = parseaddr(_header_str(msg, "Sender"))

    recipients: list[Recipient] = []
    for header, rtype in (("To", "To"), ("Cc", "Cc"), ("Bcc", "Bcc")):
        recipients.extend(
            Recipient(name=n, email=e, recipient_type=rtype) for n, e in _addresses(msg, header)
        )

    plain, html_part = _bodies(msg)
    body_html = _part_text(html_part) if html_part is not None else None
    if plain is not None:
        body_plain = _part_text(plain).strip()
    elif body_html:
        body_plain = html_to_text(body_html)
    else:
        body_plain = ""

    attachments: list[AttachmentSummary] = []
    for i, part in enumerate(attachment_parts(msg)):
        name, data, mime = attachment_payload(part, i)
        attachments.append(
            AttachmentSummary(
                id=i + 1,
                message_id=msg_id,
                attachment_index=i,
                filename=name,
                size_bytes=len(data),
                size_formatted=format_size(len(data)),
                mime_type=mime or "application/octet-stream",
                download_url=f"/api/messages/{msg_id}/attachments/{i + 1}?download=1",
                view_url=f"/api/messages/{msg_id}/attachments/{i + 1}",
            )
        )

    return MessageDetail(
        id=msg_id,
        folder_id=0,
        folder_path=folder_path,
        message_index=message_index,
        identifier=identifier,
        subject=subject,
        sender_name=sender_name or sender_email,
        sender_email=sender_email,
        date_iso=_date_iso(msg),
        has_attachments=bool(attachments),
        attachment_count=len(attachments),
        recipients=recipients,
        body_plain=body_plain,
        body_html=body_html if include_html else None,
        attachments=attachments,
        transport_headers=_header_block(raw) if include_html else None,
        message_id_hdr=parse_msgid(_header_str(msg, "Message-ID")),
        in_reply_to=parse_msgid(_header_str(msg, "In-Reply-To")),
        references=parse_msgid_list(_header_str(msg, "References")),
    )


def extract_attachment(raw: bytes, attachment_index: int) -> tuple[str, bytes]:
    parts = attachment_parts(parse_message(raw))
    if attachment_index < 0 or attachment_index >= len(parts):
        raise IndexError(f"Attachment index {attachment_index} out of range ({len(parts)})")
    name, data, _ = attachment_payload(parts[attachment_index], attachment_index)
    return name, data
