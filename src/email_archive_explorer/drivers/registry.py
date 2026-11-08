"""Driver registry: format detection by magic bytes, then by extension (Spec 07)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from email_archive_explorer.drivers.base import BaseArchiveDriver


class ArchiveDriverRegistry:
    """Holds the known drivers in registration order (probe order matters)."""

    _registry: dict[str, type[BaseArchiveDriver]] = {}

    @classmethod
    def register(cls, driver_cls: type[BaseArchiveDriver]) -> type[BaseArchiveDriver]:
        """Register a driver class. Usable as a decorator."""
        cls._registry[driver_cls.format_name()] = driver_cls
        return driver_cls

    @classmethod
    def drivers(cls) -> list[type[BaseArchiveDriver]]:
        return list(cls._registry.values())

    @classmethod
    def get_supported_extensions(cls) -> list[str]:
        exts: list[str] = []
        for d in cls._registry.values():
            for e in d.supported_extensions():
                if e not in exts:
                    exts.append(e)
        return exts

    @classmethod
    def get_by_format_type(cls, format_type: str) -> Optional[type[BaseArchiveDriver]]:
        return next((d for d in cls._registry.values() if d.format_type == format_type), None)

    @classmethod
    def probe(cls, file_path: str | Path) -> Optional[type[BaseArchiveDriver]]:
        """Pass 1 only: the first driver whose magic-byte probe accepts the path."""
        path = Path(file_path)
        for driver_cls in cls._registry.values():
            try:
                if driver_cls.probe(path):
                    return driver_cls
            except Exception:
                continue
        return None

    @classmethod
    def detect_driver_class(cls, file_path: str | Path) -> type[BaseArchiveDriver]:
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Archive not found: {path}")

        # Pass 1: magic byte / structure probing
        found = cls.probe(path)
        if found is not None:
            return found

        # Pass 2: extension fallback (files only)
        if path.is_file():
            ext = path.suffix.lower()
            for driver_cls in cls._registry.values():
                if not driver_cls.is_directory_format and ext in driver_cls.supported_extensions():
                    return driver_cls

        kind = "folder" if path.is_dir() else "file"
        raise ValueError(
            f"Unsupported archive format: {path.name} ({kind}). "
            f"Supported: {', '.join(cls.get_supported_extensions())} or a folder of .eml files"
        )

    @classmethod
    def detect_driver(cls, file_path: str | Path) -> BaseArchiveDriver:
        """Return an (unopened) driver instance for ``file_path``."""
        return cls.detect_driver_class(file_path)(Path(file_path).resolve())


def detect_driver(file_path: str | Path) -> BaseArchiveDriver:
    return ArchiveDriverRegistry.detect_driver(file_path)
