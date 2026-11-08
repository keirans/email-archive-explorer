"""Configuration defaults and path constants for Email Archive Explorer."""

from pathlib import Path

DEFAULT_CACHE_DB = Path("archive_cache.db")
# Catalog + per-archive cache files for the multi-archive manager (Spec 06)
DEFAULT_CACHE_DIR = Path(".cache")


def format_size(bytes_size: int | float | None) -> str:
    """Format bytes into a human readable string (KB, MB, GB)."""
    if bytes_size is None or bytes_size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(bytes_size)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}"
