"""Abstract driver interface that every archive format driver implements (Spec 07)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar, Optional

from email_archive_explorer.models import MessageDetail


class BaseArchiveDriver(ABC):
    """Read-only access to one email archive (a file or a directory).

    Messages are addressed by ``(folder_path, message_index)``. Folder paths
    are ``/``-separated and returned in pre-order (parents before children),
    which is what the indexer relies on to rebuild the tree.
    """

    #: Short, stable identifier stored in the catalog (e.g. ``"pst"``).
    format_type: ClassVar[str] = "unknown"
    #: True when the archive is a directory rather than a single file.
    is_directory_format: ClassVar[bool] = False
    #: Bump when the driver extracts index data differently; caches built by an
    #: older version are then rebuilt automatically.
    index_version: ClassVar[str] = "1"

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path).resolve()
        self.is_open = False

    # ------------------------------------------------------------------
    # Format identity & detection
    # ------------------------------------------------------------------

    @classmethod
    @abstractmethod
    def format_name(cls) -> str:
        """Human-readable format name (e.g. 'Outlook PST', 'MBOX Mailbox')."""

    @classmethod
    @abstractmethod
    def supported_extensions(cls) -> list[str]:
        """Lower-case file extensions (e.g. ['.pst', '.ost'])."""

    @classmethod
    @abstractmethod
    def probe(cls, file_path: Path) -> bool:
        """Inspect magic bytes / structure to confirm this driver can read the path."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Open the archive read-only."""

    @abstractmethod
    def close(self) -> None:
        """Release underlying file handles."""

    def __enter__(self) -> BaseArchiveDriver:
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Content access
    # ------------------------------------------------------------------

    @abstractmethod
    def get_folder_paths(self) -> list[str]:
        """All canonical folder paths, parents before children."""

    @abstractmethod
    def get_folder_message_count(self, folder_path: str) -> int:
        """Number of messages directly inside ``folder_path``."""

    @abstractmethod
    def get_message_detail(
        self, folder_path: str, message_index: int, msg_id: int = 0
    ) -> MessageDetail:
        """Full message: headers, bodies, recipients and attachment metadata."""

    @abstractmethod
    def get_attachment_bytes(
        self, folder_path: str, message_index: int, attachment_index: int
    ) -> tuple[str, bytes]:
        """``(filename, payload)`` for one attachment, extracted on demand."""

    # ------------------------------------------------------------------
    # Optional hooks (sensible defaults; override for speed or fidelity)
    # ------------------------------------------------------------------

    @property
    def folder_paths(self) -> list[str]:
        return self.get_folder_paths()

    def technical_details(self) -> list[dict]:
        """Format-specific facts for the Archive Inspector (Spec 08 §3.5).

        Returns sections: ``[{"title": str, "items": [{"label", "value", "mono"?, "hint"?}]}]``.
        """
        return []

    def describe(self) -> str:
        """Format description shown in the UI (may include version detail)."""
        return self.format_name()

    def fingerprint(self) -> tuple[int, float]:
        """``(size_bytes, mtime)`` used to decide whether a cache is stale.

        Must work without :meth:`open` being called.
        """
        st = self.file_path.stat()
        return st.st_size, st.st_mtime

    def get_message_for_index(self, folder_path: str, message_index: int) -> MessageDetail:
        """Message data needed for indexing. Drivers may skip HTML/headers here."""
        return self.get_message_detail(folder_path, message_index)

    def get_raw_message(self, folder_path: str, message_index: int) -> Optional[bytes]:
        """Original RFC 822 bytes, when the format stores them (MBOX, EML).

        Returns ``None`` when the message must be synthesised (PST).
        """
        return None
