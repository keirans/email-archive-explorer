"""Server-side file browser and native OS file dialog (Spec 08 §3.2–3.3).

A browser can't reveal the real path of a file the user picks (and uploading
multi-GB archives makes no sense), so the local server browses the disk on the
user's behalf, confined to a set of roots.
"""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from email_archive_explorer.config import format_size
from email_archive_explorer.drivers import ArchiveDriverRegistry, EmlDirDriver

MAX_ENTRIES = 2000
_EML_SCAN_LIMIT = 500
ARCHIVE_GLOBS = "*.pst *.ost *.mbox *.mbx *.eml"


class BrowseError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _is_mail_folder_fast(d: Path, depth: int = 1) -> bool:
    """Cheap version of ``EmlDirDriver.probe``: stops at the first .eml it sees and
    looks at most one level down (e.g. Export/Inbox/*.eml)."""
    try:
        if (d / "cur").is_dir() and (d / "new").is_dir():
            return True
        with os.scandir(d) as it:
            for i, entry in enumerate(it):
                if i >= _EML_SCAN_LIMIT:
                    break
                if entry.name.lower().endswith(".eml") and not entry.name.startswith("."):
                    return True
        if depth > 0:
            with os.scandir(d) as it:
                subdirs = [e for e in it if not e.name.startswith(".") and e.is_dir()][:50]
            return any(_is_mail_folder_fast(Path(e.path), depth - 1) for e in subdirs)
    except OSError:
        return False
    return False


def _mount_points() -> list[Path]:
    user = getpass.getuser()
    candidates: list[Path] = []
    system = platform.system()
    if system == "Darwin":
        candidates += [p for p in Path("/Volumes").glob("*")]
    elif system == "Windows":
        import string
        candidates += [Path(f"{c}:\\") for c in string.ascii_uppercase if Path(f"{c}:\\").exists()]
    else:
        for base in (Path("/run/media") / user, Path("/media") / user, Path("/mnt")):
            try:
                candidates += [p for p in base.iterdir() if p.is_dir()]
            except OSError:
                continue
    out = []
    for p in candidates:
        try:
            if p.is_dir() and os.access(p, os.R_OK | os.X_OK):
                out.append(p)
        except OSError:
            continue
    return out


class FileSystemBrowser:
    """Lists directories under a fixed set of roots; nothing outside them is reachable."""

    def __init__(self, data_dir: Path | str = "data", extra_roots: Iterable[Path | str] = ()):
        self.data_dir = Path(data_dir).resolve()
        self.extra_roots = [Path(p).expanduser().resolve() for p in extra_roots]

    def root_entries(self) -> list[dict[str, Any]]:
        roots: list[dict[str, Any]] = [{"label": "Home", "path": str(Path.home().resolve()), "kind": "home"}]
        if self.data_dir.is_dir():
            roots.append({"label": "Project data", "path": str(self.data_dir), "kind": "data"})
        for p in self.extra_roots:
            if p.is_dir():
                roots.append({"label": p.name or str(p), "path": str(p), "kind": "custom"})
        for p in _mount_points():
            roots.append({"label": p.name or str(p), "path": str(p.resolve()), "kind": "mount"})
        seen, unique = set(), []
        for r in roots:
            if r["path"] not in seen:
                seen.add(r["path"])
                unique.append(r)
        return unique

    def _root_paths(self) -> list[Path]:
        return [Path(r["path"]) for r in self.root_entries()]

    def resolve_allowed(self, path: str | Path) -> Path:
        """Resolve symlinks and ``..``, then require the result to sit inside a root."""
        try:
            p = Path(path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise BrowseError(400, f"Invalid path: {exc}") from exc
        for root in self._root_paths():
            if p == root or root in p.parents:
                return p
        raise BrowseError(403, "That location is outside the folders this app may browse")

    def list_dir(self, path: str | Path, show_hidden: bool = False) -> dict[str, Any]:
        p = self.resolve_allowed(path)
        if not p.exists():
            raise BrowseError(404, f"Not found: {p}")
        if not p.is_dir():
            raise BrowseError(400, f"Not a folder: {p}")

        roots = self._root_paths()
        root = max((r for r in roots if p == r or r in p.parents), key=lambda r: len(r.parts))
        crumbs = [{"name": root.name or str(root), "path": str(root)}]
        for part in p.relative_to(root).parts:
            crumbs.append({"name": part, "path": str(Path(crumbs[-1]["path"]) / part)})

        entries: list[dict[str, Any]] = []
        skipped = 0
        truncated = False
        try:
            with os.scandir(p) as it:
                raw = sorted(it, key=lambda e: e.name.lower())
        except PermissionError as exc:
            raise BrowseError(403, f"Permission denied: {p}") from exc

        dirs, files = [], []
        for e in raw:
            if not show_hidden and e.name.startswith("."):
                continue
            try:
                (dirs if e.is_dir() else files).append(e)
            except OSError:
                skipped += 1

        for e in dirs + files:
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            ep = Path(e.path)
            try:
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                skipped += 1
                continue
            archive = None
            if is_dir:
                if not os.access(ep, os.R_OK | os.X_OK):
                    skipped += 1
                    continue
                if _is_mail_folder_fast(ep):
                    archive = {"format_type": EmlDirDriver.format_type, "format_name": EmlDirDriver.format_name()}
            else:
                drv = ArchiveDriverRegistry.probe(ep)
                if drv is None:
                    ext = ep.suffix.lower()
                    drv = next((d for d in ArchiveDriverRegistry.drivers()
                                if not d.is_directory_format and ext in d.supported_extensions()), None)
                if drv is not None and drv is not EmlDirDriver:
                    archive = {"format_type": drv.format_type, "format_name": drv.format_name()}
            entries.append({
                "name": e.name,
                "path": str(ep),
                "type": "dir" if is_dir else "file",
                "size_bytes": None if is_dir else st.st_size,
                "size_formatted": None if is_dir else format_size(st.st_size),
                "modified_iso": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "archive": archive,
            })

        parent = None
        if p not in roots:
            parent = str(p.parent)
        return {
            "path": str(p),
            "parent": parent,
            "breadcrumbs": crumbs,
            "entries": entries,
            "truncated": truncated,
            "skipped": skipped,
        }


# ---------------------------------------------------------------------------
# Native OS file dialog
# ---------------------------------------------------------------------------

_picker_lock = threading.Lock()
PICKER_TIMEOUT_S = 600


class PickerBusy(Exception):
    pass


def _has_display() -> bool:
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def picker_backend() -> Optional[str]:
    """Which native dialog can be shown here, if any."""
    if not _has_display() or os.environ.get("SSH_CONNECTION") and not os.environ.get("DISPLAY"):
        return None
    if sys.platform == "darwin":
        return "osascript" if shutil.which("osascript") else None
    if sys.platform == "win32":
        try:
            import tkinter  # noqa: F401
            return "tkinter"
        except ImportError:
            return None
    for tool in ("kdialog", "zenity"):
        if shutil.which(tool):
            return tool
    return None


def _command(backend: str, mode: str, start: str) -> list[str]:
    if backend == "kdialog":
        if mode == "directory":
            return ["kdialog", "--title", "Open archive folder", "--getexistingdirectory", start]
        return ["kdialog", "--title", "Open archive", "--getopenfilename", start,
                f"Email archives ({ARCHIVE_GLOBS})|All files (*)"]
    if backend == "zenity":
        cmd = ["zenity", "--file-selection", "--title=Open archive", f"--filename={start}/"]
        if mode == "directory":
            return cmd + ["--directory"]
        return cmd + [f"--file-filter=Email archives | {ARCHIVE_GLOBS}", "--file-filter=All files | *"]
    if backend == "osascript":
        what = "choose folder" if mode == "directory" else "choose file"
        return ["osascript", "-e", f'POSIX path of ({what} with prompt "Open archive")']
    raise ValueError(backend)


def native_pick(mode: str = "file", start: Optional[str] = None) -> Optional[str]:
    """Show the OS dialog and return the chosen path, or ``None`` if cancelled."""
    backend = picker_backend()
    if backend is None:
        raise RuntimeError("No native file dialog is available on this machine")
    if not _picker_lock.acquire(blocking=False):
        raise PickerBusy()
    try:
        start = start or str(Path.home())
        if backend == "tkinter":
            from tkinter import Tk, filedialog
            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(initialdir=start) if mode == "directory" \
                else filedialog.askopenfilename(initialdir=start)
            root.destroy()
            return chosen or None
        proc = subprocess.run(
            _command(backend, mode, start), capture_output=True, text=True, timeout=PICKER_TIMEOUT_S
        )
        if proc.returncode != 0:
            return None  # cancelled (kdialog/zenity/osascript all exit non-zero)
        chosen = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
        return chosen or None
    except subprocess.TimeoutExpired:
        return None
    finally:
        _picker_lock.release()
