"""Outlook PST/OST driver using libpff (pypff) for in-place, read-only access."""

from __future__ import annotations

import mimetypes
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pypff
from striprtf.striprtf import rtf_to_text

from email_archive_explorer.config import format_size
from email_archive_explorer.drivers.base import BaseArchiveDriver
from email_archive_explorer.models import (
    AttachmentSummary,
    MessageDetail,
    Recipient,
)
from email_archive_explorer.threads import conversation_key, parse_msgid, parse_msgid_list

PST_MAGIC = b"!BDN"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
EMBEDDED_ITEM_MIME = "application/x-pst-embedded-item"
_MAX_OLE_SNIFF = 32 * 1024 * 1024

# Leading bytes → extension, for attachments Outlook stored without a file name
_MAGIC_EXT = [
    (b"%PDF", "pdf"), (b"PK\x03\x04", "zip"), (b"GIF8", "gif"), (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG", "png"), (b"{\\rtf", "rtf"), (b"BM", "bmp"),
]
# UTF-16 stream names inside an OLE compound file → Office format
_OLE_STREAMS = [
    ("WordDocument", "doc"), ("Workbook", "xls"), ("Book", "xls"),
    ("PowerPoint Document", "ppt"), ("VisioDocument", "vsd"), ("__substg1.0_", "msg"),
]


def decode_bytes(data: bytes | str | None) -> str:
    """Safely decode bytes into a string with encoding fallback."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class PstDriver(BaseArchiveDriver):
    """Thread-safe driver for Microsoft Outlook PST/OST archives (via libpff)."""

    format_type = "pst"
    # v2: unnamed attachments are identified by content (embedded Office docs,
    # attached Outlook items) instead of being called "attachment_N".
    index_version = "3"  # 3: conversation signals (Spec 10)

    @classmethod
    def format_name(cls) -> str:
        return "Outlook PST"

    @classmethod
    def supported_extensions(cls) -> list[str]:
        return [".pst", ".ost"]

    @classmethod
    def probe(cls, file_path: Path) -> bool:
        try:
            if not Path(file_path).is_file():
                return False
            with open(file_path, "rb") as f:
                return f.read(4) == PST_MAGIC
        except OSError:
            return False

    @classmethod
    def read_header(cls, file_path: Path) -> dict[str, object]:
        """Decode the PST/OST file header (magic, client, version → ANSI/Unicode)."""
        with open(file_path, "rb") as f:
            hdr = f.read(16)
        client = hdr[8:10]
        version = int.from_bytes(hdr[10:12], "little") if len(hdr) >= 12 else 0
        if version in (14, 15):
            encoding = "ANSI"
        elif version >= 23:
            encoding = "Unicode"
        else:
            encoding = "Unknown"
        return {
            "kind": "OST" if client == b"SO" else "PST",
            "version": version,
            "encoding": encoding,
            "page_size": 4096 if version == 36 else 512,
        }

    _CRYPT = {0: "None", 1: "Compressible (permutation)", 2: "High (cyclic)", 0x10: "Windows Information Protection"}

    def technical_details(self) -> list[dict]:
        with open(self.file_path, "rb") as f:
            hdr = f.read(1024)
        h = self.read_header(self.file_path)
        ansi = h["encoding"] == "ANSI"
        crypt_off = 461 if ansi else 513
        crypt = hdr[crypt_off] if len(hdr) > crypt_off else None
        items = [
            {"label": "Signature", "value": f"{hdr[:4].hex(' ').upper()}  (\"!BDN\")", "mono": True,
             "hint": "dwMagic: every PST/OST file starts with these 4 bytes"},
            {"label": "Client", "value": f"{hdr[8:10].decode('ascii', 'replace')} — {'Offline Storage (OST)' if h['kind'] == 'OST' else 'Personal Storage (PST)'}",
             "hint": "wMagicClient: SM = PST, SO = OST"},
            {"label": "File version", "value": f"{h['version']} ({h['encoding']})",
             "hint": "wVer: 14/15 = ANSI (32-bit), 23 = Unicode (64-bit), 36 = Unicode with 4 KB pages"},
            {"label": "Client version", "value": str(int.from_bytes(hdr[12:14], "little")), "mono": True, "hint": "wVerClient"},
            {"label": "Era", "value": "Outlook 97–2002" if ansi else "Outlook 2003 or later"},
            {"label": "Page size", "value": f"{h['page_size']} bytes"},
            {"label": "Encryption", "value": self._CRYPT.get(crypt, f"Unknown ({crypt})") if crypt is not None else "Unknown",
             "hint": "bCryptMethod. 'Compressible' is Outlook's default obfuscation, not real security."},
            {"label": "Header CRC", "value": "0x" + hdr[4:8][::-1].hex().upper(), "mono": True, "hint": "dwCRCPartial"},
            {"label": "Format size limit", "value": "2 GB" if ansi else "50 GB (Outlook 2010+ default)"},
            {"label": "Parser", "value": f"libpff {pypff.get_version()}", "mono": True},
        ]
        return [{"title": "PST header", "items": items}]

    def describe(self) -> str:
        try:
            h = self.read_header(self.file_path)
        except OSError:
            return self.format_name()
        era = {"ANSI": "Outlook 97–2002", "Unicode": "Outlook 2003+"}.get(h["encoding"], "")
        detail = ", ".join(x for x in (str(h["encoding"]), era) if x and x != "Unknown")
        return f"Outlook {h['kind']}" + (f" ({detail})" if detail else "")

    def __init__(self, pst_path: str | Path):
        super().__init__(pst_path)
        self.pst_path = self.file_path
        self._pst: Optional[pypff.file] = None
        self._lock = threading.RLock()
        self._path_to_indices: dict[str, tuple[int, ...]] = {}
        self._initialized_paths = False
        # libpff re-reads a folder's message table on every new folder object
        # (~14 ms each), so folder objects and counts are cached per path.
        self._folder_cache: dict[str, tuple[object, int]] = {}

    def open(self) -> None:
        """Opens the PST file in read-only mode."""
        with self._lock:
            if self._pst is None:
                if not self.pst_path.exists():
                    raise FileNotFoundError(f"PST archive not found: {self.pst_path}")
                pst = pypff.file()
                pst.open(str(self.pst_path))
                self._pst = pst
                self._build_folder_index()
                self.is_open = True

    def close(self) -> None:
        """Closes the PST file handle."""
        with self._lock:
            if self._pst is not None:
                try:
                    self._pst.close()
                except Exception:
                    pass
                self._pst = None
                self._folder_cache.clear()
                self._path_to_indices.clear()
                self._initialized_paths = False
            self.is_open = False

    def _ensure_open(self) -> pypff.file:
        if self._pst is None:
            self.open()
        return self._pst

    def _build_folder_index(self) -> None:
        """Recursively maps folder paths to navigation index tuples."""
        self._path_to_indices.clear()
        pst = self._ensure_open()
        root = pst.get_root_folder()

        def _walk(folder, current_path: str, indices: tuple[int, ...]):
            name = folder.get_name() or ""
            new_path = f"{current_path}/{name}" if current_path and name else (name or current_path)
            if new_path:
                self._path_to_indices[new_path] = indices
            num_sub = folder.get_number_of_sub_folders()
            for i in range(num_sub):
                sub = folder.get_sub_folder(i)
                _walk(sub, new_path, indices + (i,))

        _walk(root, "", ())
        self._initialized_paths = True

    def get_folder_paths(self) -> list[str]:
        """Returns all recognized folder paths in the archive (pre-order)."""
        with self._lock:
            self._ensure_open()
            return list(self._path_to_indices.keys())

    def _cached_folder(self, folder_path: str) -> tuple[object, int]:
        with self._lock:
            self._ensure_open()
            hit = self._folder_cache.get(folder_path)
            if hit is not None:
                return hit
            if folder_path not in self._path_to_indices:
                raise KeyError(f"Folder path not found in PST: {folder_path}")
            folder = self._pst.get_root_folder()
            for idx in self._path_to_indices[folder_path]:
                folder = folder.get_sub_folder(idx)
            entry = (folder, folder.get_number_of_sub_messages())
            self._folder_cache[folder_path] = entry
            return entry

    def get_folder_message_count(self, folder_path: str) -> int:
        return self._cached_folder(folder_path)[1]

    def get_folder_by_path(self, folder_path: str):
        """Retrieves a pypff.folder object by its canonical path."""
        return self._cached_folder(folder_path)[0]

    def get_message(self, folder_path: str, message_index: int):
        """Retrieves a message object given folder path and index."""
        with self._lock:
            folder, num_msgs = self._cached_folder(folder_path)
            if message_index < 0 or message_index >= num_msgs:
                raise IndexError(
                    f"Message index {message_index} out of range (folder has {num_msgs} messages)"
                )
            return folder.get_sub_message(message_index)

    def extract_recipients(self, msg) -> list[Recipient]:
        """Extracts parsed recipient list from a message."""
        recipients: list[Recipient] = []
        if not getattr(msg, "recipients", None):
            return recipients
        try:
            num = msg.recipients.get_number_of_recipients()
        except Exception:
            return recipients

        for i in range(num):
            try:
                recip_obj = msg.recipients.get_recipient(i)
                name = ""
                email = ""
                rtype_int = 1
                for e in range(recip_obj.get_number_of_entries()):
                    entry = recip_obj.get_entry(e)
                    etype = entry.get_entry_type()
                    if etype == 0x3001:  # PR_DISPLAY_NAME
                        name = decode_bytes(entry.get_data_as_string())
                    elif etype == 0x3003:  # PR_EMAIL_ADDRESS
                        email = decode_bytes(entry.get_data_as_string())
                    elif etype == 0x0C15:  # PR_RECIPIENT_TYPE
                        rtype_int = entry.get_data_as_integer() or 1
                rtype = "To" if rtype_int == 1 else ("Cc" if rtype_int == 2 else "Bcc")
                recipients.append(Recipient(name=name, email=email, recipient_type=rtype))
            except Exception:
                continue
        return recipients

    def extract_body_plain(self, msg) -> str:
        """Extracts plain text message body with RTF fallback."""
        try:
            raw_plain = msg.get_plain_text_body()
            if raw_plain:
                text = decode_bytes(raw_plain).strip()
                if text:
                    return text
        except Exception:
            pass

        # Fallback to decompressed RTF if plain text was not available
        try:
            raw_rtf = msg.get_rtf_body()
            if raw_rtf:
                decoded_rtf = decode_bytes(raw_rtf)
                extracted = rtf_to_text(decoded_rtf).strip()
                if extracted:
                    return extracted
        except Exception:
            pass

        return ""

    def extract_body_html(self, msg) -> Optional[str]:
        """Extracts HTML body if present."""
        try:
            raw_html = msg.get_html_body()
            if raw_html:
                html_str = decode_bytes(raw_html).strip()
                if html_str:
                    return html_str
        except Exception:
            pass
        return None

    def get_message_detail(
        self, folder_path: str, message_index: int, msg_id: int = 0
    ) -> MessageDetail:
        """Builds a full MessageDetail object from the PST."""
        return self._build_detail(folder_path, message_index, msg_id, full=True)

    def get_message_for_index(self, folder_path: str, message_index: int) -> MessageDetail:
        """Indexing variant: skips HTML body and transport headers (not stored)."""
        return self._build_detail(folder_path, message_index, 0, full=False)

    def _build_detail(
        self, folder_path: str, message_index: int, msg_id: int, full: bool
    ) -> MessageDetail:
        with self._lock:
            msg = self.get_message(folder_path, message_index)
            subject = decode_bytes(msg.get_subject()) or "(No Subject)"
            sender_name = decode_bytes(msg.get_sender_name()) or ""

            # Delivery date
            dt = msg.get_delivery_time() or msg.get_client_submit_time()
            date_iso = dt.isoformat() if isinstance(dt, datetime) else None

            # Attachments
            num_att = msg.get_number_of_attachments()
            attachments: list[AttachmentSummary] = []
            for a in range(num_att):
                att = msg.get_attachment(a)
                fn, mime = self._attachment_name(att, a)
                size = att.get_size() or 0
                attachments.append(
                    AttachmentSummary(
                        id=a + 1,
                        message_id=msg_id,
                        attachment_index=a,
                        filename=fn,
                        size_bytes=size,
                        size_formatted=format_size(size),
                        mime_type=mime or "application/octet-stream",
                        download_url=f"/api/messages/{msg_id}/attachments/{a + 1}?download=1",
                        view_url=f"/api/messages/{msg_id}/attachments/{a + 1}",
                    )
                )

            body_plain = self.extract_body_plain(msg)
            body_html = self.extract_body_html(msg) if full else None
            recipients = self.extract_recipients(msg)
            headers = self._transport_headers(msg)
            ids = self._header_ids(headers)
            try:
                conv_key = conversation_key(msg.get_conversation_index())
            except Exception:
                conv_key = None
            try:
                topic = decode_bytes(msg.get_conversation_topic()) or None
            except Exception:
                topic = None

            return MessageDetail(
                id=msg_id,
                folder_id=0,
                folder_path=folder_path,
                message_index=message_index,
                identifier=msg.get_identifier() or 0,
                subject=subject,
                sender_name=sender_name,
                sender_email="",
                date_iso=date_iso,
                has_attachments=num_att > 0,
                attachment_count=num_att,
                recipients=recipients,
                body_plain=body_plain,
                body_html=body_html,
                attachments=attachments,
                transport_headers=(headers or None) if full else None,
                message_id_hdr=ids[0],
                in_reply_to=ids[1],
                references=ids[2],
                conversation_key=conv_key,
                conversation_topic=topic,
            )

    @staticmethod
    def _transport_headers(msg) -> str:
        getter = getattr(msg, "get_transport_headers", None)
        if getter is None:
            return ""
        try:
            return decode_bytes(getter()) or ""
        except Exception:
            return ""

    @staticmethod
    def _header_ids(headers: str):
        """``(Message-ID, In-Reply-To, References)`` from a transport header block."""
        if not headers:
            return None, None, []
        from email.parser import HeaderParser
        try:
            h = HeaderParser().parsestr(headers)
            return (parse_msgid(h.get("Message-ID")), parse_msgid(h.get("In-Reply-To")),
                    parse_msgid_list(h.get("References")))
        except Exception:
            return None, None, []

    def get_attachment_bytes(
        self, folder_path: str, message_index: int, attachment_index: int
    ) -> tuple[str, bytes]:
        """Extracts raw attachment bytes directly from the PST archive."""
        with self._lock:
            msg = self.get_message(folder_path, message_index)
            num_att = msg.get_number_of_attachments()
            if attachment_index < 0 or attachment_index >= num_att:
                raise IndexError(f"Attachment index {attachment_index} out of range ({num_att})")
            att = msg.get_attachment(attachment_index)
            filename, _ = self._attachment_name(att, attachment_index)
            size = att.get_size() or 0
            data = att.read_buffer(size) if size > 0 else b""
            return filename, data

    @staticmethod
    def _attachment_name(att, index: int) -> tuple[str, Optional[str]]:
        """File name and MIME type; sniffs content when Outlook stored no name."""
        name = att.get_long_filename()
        if name:
            return name, mimetypes.guess_type(name)[0]
        n = index + 1
        size = att.get_size() or 0
        try:
            head = att.read_buffer(min(size, 16)) if size else b""
        except Exception:
            head = b""
        # Attached Outlook item: a raw property-context heap (bSig 0xEC, client 0xBC)
        # that libpff cannot turn back into a message.
        if head[2:4] == b"\xec\xbc":
            return f"Embedded Outlook item {n}", EMBEDDED_ITEM_MIME
        if head.startswith(OLE_MAGIC):
            ext = "ole"
            if size <= _MAX_OLE_SNIFF:
                try:
                    data = att.read_buffer(size)
                    ext = next((e for stream, e in _OLE_STREAMS if stream.encode("utf-16-le") in data), "ole")
                except Exception:
                    pass
            name = f"embedded_object_{n}.{ext}"
            return name, mimetypes.guess_type(name)[0] or "application/x-ole-storage"
        for magic, ext in _MAGIC_EXT:
            if head.startswith(magic):
                name = f"attachment_{n}.{ext}"
                return name, mimetypes.guess_type(name)[0]
        return f"attachment_{n}", None
