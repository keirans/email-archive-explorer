"""Driver for a directory tree of ``.eml`` files and/or Maildir folders."""

from __future__ import annotations

import os
import threading
import zlib
from pathlib import Path

from email_archive_explorer.drivers.base import BaseArchiveDriver
from email_archive_explorer.drivers.rfc822 import (
    extract_attachment,
    looks_like_rfc822,
    message_to_detail,
)
from email_archive_explorer.models import MessageDetail

_MAILDIR_SUBDIRS = ("cur", "new")


def _is_maildir(d: Path) -> bool:
    return all((d / s).is_dir() for s in _MAILDIR_SUBDIRS)


def _eml_files(d: Path) -> list[Path]:
    try:
        return sorted(
            p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() == ".eml" and not p.name.startswith(".")
        )
    except OSError:
        return []


def _maildir_files(d: Path) -> list[Path]:
    out: list[Path] = []
    for sub in _MAILDIR_SUBDIRS:
        try:
            out.extend(p for p in (d / sub).iterdir() if p.is_file() and not p.name.startswith("."))
        except OSError:
            continue
    return sorted(out, key=lambda p: (p.name, p.parent.name))


def is_mail_folder(d: Path) -> bool:
    """A directory that directly holds messages: ``.eml`` files or a Maildir."""
    return d.is_dir() and (_is_maildir(d) or bool(_eml_files(d)))


class EmlDirDriver(BaseArchiveDriver):
    """Each directory becomes a folder; each ``.eml`` / Maildir file a message.

    A single ``.eml`` file is also accepted (one folder, one message).
    """

    format_type = "eml_dir"
    is_directory_format = True
    index_version = "2"  # 2: conversation signals (Spec 10)

    @classmethod
    def format_name(cls) -> str:
        return "EML Directory"

    @classmethod
    def supported_extensions(cls) -> list[str]:
        return [".eml"]

    @classmethod
    def probe(cls, file_path: Path) -> bool:
        path = Path(file_path)
        if path.is_dir():
            if is_mail_folder(path):
                return True
            # Allow one level of nesting, e.g. Export/Inbox/*.eml, Export/Sent/*.eml
            try:
                return any(is_mail_folder(c) for c in path.iterdir() if c.is_dir())
            except OSError:
                return False
        if path.is_file() and path.suffix.lower() == ".eml":
            try:
                with open(path, "rb") as f:
                    return looks_like_rfc822(f.read(4096))
            except OSError:
                return False
        return False

    def __init__(self, file_path: str | Path):
        super().__init__(file_path)
        self._lock = threading.RLock()
        self._folders: dict[str, list[Path]] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------------

    def _discover(self) -> tuple[list[str], dict[str, list[Path]]]:
        root = self.file_path
        if root.is_file():
            return [root.stem], {root.stem: [root]}

        root_name = root.name or "Archive"
        found: dict[tuple[str, ...], list[Path]] = {}

        for dirpath, dirnames, _ in os.walk(root):
            d = Path(dirpath)
            rel = d.relative_to(root).parts
            if _is_maildir(d):
                files = _maildir_files(d) + _eml_files(d)
                # Descend only into Maildir++ sub-folders (".Sent", ".Archive.2003")
                dirnames[:] = sorted(n for n in dirnames if n.startswith(".") and (d / n).is_dir())
            else:
                files = _eml_files(d)
                dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
            if files or not rel:
                found[rel] = files

        # Ensure every ancestor exists so the tree is connected.
        for rel in list(found):
            for i in range(1, len(rel)):
                found.setdefault(rel[:i], [])

        def _label(part: str) -> str:
            return part[1:] if part.startswith(".") and len(part) > 1 else part

        order: list[str] = []
        folders: dict[str, list[Path]] = {}
        for rel in sorted(found, key=lambda r: [p.lower() for p in r]):
            path = "/".join([root_name, *(_label(p) for p in rel)])
            if path in folders:  # label collision (e.g. "Sent" and ".Sent")
                folders[path].extend(found[rel])
                continue
            order.append(path)
            folders[path] = found[rel]
        return order, folders

    def open(self) -> None:
        with self._lock:
            if self.is_open:
                return
            if not self.file_path.exists():
                raise FileNotFoundError(f"Archive not found: {self.file_path}")
            self._order, self._folders = self._discover()
            self.is_open = True

    def close(self) -> None:
        with self._lock:
            self._order, self._folders = [], {}
            self.is_open = False

    def technical_details(self) -> list[dict]:
        with self._lock:
            self._ensure_open()
            folders = dict(self._folders)
        if self.file_path.is_file():
            layout = "Single .eml file"
        elif _is_maildir(self.file_path):
            layout = "Maildir++ (with sub-folders)" if len(folders) > 1 else "Maildir"
        else:
            layout = "Folder tree of .eml files"
        n_files = sum(len(v) for v in folders.values())
        maildir_folders = sum(1 for files in folders.values() if any(f.parent.name in _MAILDIR_SUBDIRS for f in files))
        return [{"title": "EML layout", "items": [
            {"label": "Layout", "value": layout},
            {"label": "Message files", "value": f"{n_files:,}"},
            {"label": "Folders", "value": f"{len(folders):,}"},
            {"label": "Maildir folders", "value": f"{maildir_folders:,}"},
            {"label": "Access", "value": "Each message file is read on demand; nothing is written"},
        ]}]

    def fingerprint(self) -> tuple[int, float]:
        """Total message bytes and the newest mtime of any message or directory."""
        if self.file_path.is_file():
            return super().fingerprint()
        total, newest = 0, self.file_path.stat().st_mtime
        for dirpath, _dirnames, filenames in os.walk(self.file_path):
            try:
                newest = max(newest, os.stat(dirpath).st_mtime)
            except OSError:
                pass
            for name in filenames:
                if name.startswith("."):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                total += st.st_size
                newest = max(newest, st.st_mtime)
        return total, newest

    # ------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if not self.is_open:
            self.open()

    def _file_for(self, folder_path: str, message_index: int) -> Path:
        with self._lock:
            self._ensure_open()
            if folder_path not in self._folders:
                raise KeyError(f"Folder path not found: {folder_path}")
            files = self._folders[folder_path]
            if message_index < 0 or message_index >= len(files):
                raise IndexError(
                    f"Message index {message_index} out of range (folder has {len(files)} messages)"
                )
            return files[message_index]

    def get_folder_paths(self) -> list[str]:
        with self._lock:
            self._ensure_open()
            return list(self._order)

    def get_folder_message_count(self, folder_path: str) -> int:
        with self._lock:
            self._ensure_open()
            if folder_path not in self._folders:
                raise KeyError(f"Folder path not found: {folder_path}")
            return len(self._folders[folder_path])

    def get_raw_message(self, folder_path: str, message_index: int) -> bytes:
        return self._file_for(folder_path, message_index).read_bytes()

    def _identifier(self, path: Path) -> int:
        return zlib.crc32(str(path).encode())

    def get_message_detail(
        self, folder_path: str, message_index: int, msg_id: int = 0
    ) -> MessageDetail:
        path = self._file_for(folder_path, message_index)
        return message_to_detail(
            path.read_bytes(), folder_path, message_index, msg_id, identifier=self._identifier(path)
        )

    def get_message_for_index(self, folder_path: str, message_index: int) -> MessageDetail:
        path = self._file_for(folder_path, message_index)
        return message_to_detail(
            path.read_bytes(), folder_path, message_index, 0,
            identifier=self._identifier(path), include_html=False,
        )

    def get_attachment_bytes(
        self, folder_path: str, message_index: int, attachment_index: int
    ) -> tuple[str, bytes]:
        return extract_attachment(self.get_raw_message(folder_path, message_index), attachment_index)
