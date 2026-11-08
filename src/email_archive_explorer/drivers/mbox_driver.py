"""MBOX driver (Unix mailboxes, Thunderbird, Gmail Takeout exports)."""

from __future__ import annotations

import re
import threading
import zlib
from pathlib import Path
from typing import BinaryIO, Optional

from email_archive_explorer.drivers.base import BaseArchiveDriver
from email_archive_explorer.drivers.rfc822 import extract_attachment, message_to_detail
from email_archive_explorer.models import MessageDetail

# "From sender@example.com Mon Jan  1 00:00:00 2001" — require a 4-digit year so
# arbitrary text files that happen to start with "From " are not mistaken for mbox.
_ENVELOPE_RE = re.compile(rb"^From \S*.*\b\d{4}\b")
_ESCAPED_FROM_RE = re.compile(rb"^>(>*From )", re.MULTILINE)


class MboxDriver(BaseArchiveDriver):
    """Single-folder driver for one mbox file.

    Implemented without :mod:`mailbox` because ``mailbox.mbox`` opens the file
    read-write; this driver only ever opens the archive with ``"rb"``.
    """

    format_type = "mbox"
    index_version = "2"  # 2: conversation signals (Spec 10)

    @classmethod
    def format_name(cls) -> str:
        return "MBOX Archive"

    @classmethod
    def supported_extensions(cls) -> list[str]:
        return [".mbox", ".mbx"]

    @classmethod
    def probe(cls, file_path: Path) -> bool:
        try:
            if not Path(file_path).is_file():
                return False
            with open(file_path, "rb") as f:
                first_line = f.readline(512)
            return bool(_ENVELOPE_RE.match(first_line))
        except OSError:
            return False

    def __init__(self, file_path: str | Path):
        super().__init__(file_path)
        self._fh: Optional[BinaryIO] = None
        self._spans: list[tuple[int, int]] = []  # (body_start, end) per message
        self._envelopes: list[int] = []         # offset of each "From " line
        self._lock = threading.RLock()
        stem = self.file_path.stem if self.file_path.suffix.lower() in self.supported_extensions() else self.file_path.name
        self._folder = stem or "Inbox"

    # ------------------------------------------------------------------

    def open(self) -> None:
        with self._lock:
            if self._fh is not None:
                return
            self._fh = open(self.file_path, "rb")
            self._spans, self._envelopes = self._scan(self._fh)
            self.is_open = True

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            self._spans = []
            self.is_open = False

    @staticmethod
    def _scan(fh: BinaryIO) -> tuple[list[tuple[int, int]], list[int]]:
        """Locate messages: a ``From `` line at file start or after a blank line."""
        fh.seek(0)
        starts: list[tuple[int, int]] = []  # (from_line_start, body_start)
        pos = 0
        prev_blank = True
        for line in fh:
            if prev_blank and line.startswith(b"From "):
                starts.append((pos, pos + len(line)))
            prev_blank = line in (b"\n", b"\r\n")
            pos += len(line)
        spans = []
        for i, (_, body_start) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else pos
            spans.append((body_start, end))
        return spans, [s for s, _ in starts]

    def technical_details(self) -> list[dict]:
        with self._lock:
            self._ensure_open()
            fh = self._fh
            fh.seek(0)
            sample = fh.read(1024 * 1024)

            def _line(off: int) -> str:
                fh.seek(off)
                return fh.readline(300).decode("utf-8", "replace").rstrip()

            first = _line(self._envelopes[0]) if self._envelopes else "—"
            last = _line(self._envelopes[-1]) if self._envelopes else "—"
            count = len(self._spans)
        crlf = sample.count(b"\r\n")
        lf = sample.count(b"\n") - crlf
        endings = "CRLF (Windows)" if crlf and not lf else "LF (Unix)" if lf and not crlf else "Mixed" if crlf else "—"
        quoted = len(re.findall(rb"^>+From ", sample, re.MULTILINE))
        deep = len(re.findall(rb"^>>+From ", sample, re.MULTILINE))
        variant = "mboxrd (nested quoting seen)" if deep else ("mboxo / mboxrd (>From quoting)" if quoted else "Unquoted (no >From lines seen)")
        return [{"title": "MBOX structure", "items": [
            {"label": "Messages", "value": f"{count:,}"},
            {"label": "Line endings", "value": endings, "hint": "Measured on the first 1 MB"},
            {"label": "From_ quoting", "value": variant, "hint": "How body lines starting with 'From ' were escaped"},
            {"label": "First envelope", "value": first, "mono": True},
            {"label": "Last envelope", "value": last, "mono": True},
            {"label": "Access", "value": "Opened read-only (rb); boundaries indexed in memory"},
        ]}]

    def _ensure_open(self) -> None:
        if self._fh is None:
            self.open()

    def _check_folder(self, folder_path: str) -> None:
        if folder_path != self._folder:
            raise KeyError(f"Folder path not found in mbox: {folder_path}")

    # ------------------------------------------------------------------

    def get_folder_paths(self) -> list[str]:
        return [self._folder]

    def get_folder_message_count(self, folder_path: str) -> int:
        self._check_folder(folder_path)
        with self._lock:
            self._ensure_open()
            return len(self._spans)

    def get_raw_message(self, folder_path: str, message_index: int) -> bytes:
        self._check_folder(folder_path)
        with self._lock:
            self._ensure_open()
            if message_index < 0 or message_index >= len(self._spans):
                raise IndexError(
                    f"Message index {message_index} out of range (mbox has {len(self._spans)} messages)"
                )
            start, end = self._spans[message_index]
            self._fh.seek(start)
            data = self._fh.read(end - start)
        # Drop the blank separator line before the next envelope, then undo
        # ">From " quoting (mboxrd / mboxo).
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        return _ESCAPED_FROM_RE.sub(rb"\1", data)

    def _identifier(self, message_index: int) -> int:
        return zlib.crc32(f"{self.file_path}:{message_index}".encode())

    def get_message_detail(
        self, folder_path: str, message_index: int, msg_id: int = 0
    ) -> MessageDetail:
        raw = self.get_raw_message(folder_path, message_index)
        return message_to_detail(
            raw, folder_path, message_index, msg_id, identifier=self._identifier(message_index)
        )

    def get_message_for_index(self, folder_path: str, message_index: int) -> MessageDetail:
        raw = self.get_raw_message(folder_path, message_index)
        return message_to_detail(
            raw, folder_path, message_index, 0,
            identifier=self._identifier(message_index), include_html=False,
        )

    def get_attachment_bytes(
        self, folder_path: str, message_index: int, attachment_index: int
    ) -> tuple[str, bytes]:
        return extract_attachment(self.get_raw_message(folder_path, message_index), attachment_index)
