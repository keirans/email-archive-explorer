"""CLI runner and entrypoints for Email Archive Explorer."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from email_archive_explorer.config import DEFAULT_CACHE_DIR
from email_archive_explorer.drivers import detect_driver
from email_archive_explorer.indexer import (
    build_index,
    get_archive_info,
    get_db_connection,
)


def _cache_db_for(args: argparse.Namespace, archive_path: Path) -> Path:
    """Explicit --db-path, else the same per-archive cache the web server uses."""
    if getattr(args, "db_path", None):
        return Path(args.db_path).resolve()
    from email_archive_explorer.manager import cache_path_for_archive

    cache_dir = Path(getattr(args, "cache_dir", None) or DEFAULT_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_path_for_archive(archive_path, cache_dir)


def _archive_arg(args: argparse.Namespace) -> Path | None:
    """The archive path given on the command line, resolved, or None."""
    return Path(args.archive_path).expanduser().resolve() if args.archive_path else None


def cmd_stats(args: argparse.Namespace) -> int:
    """Displays archive statistics in terminal."""
    archive_path = _archive_arg(args)
    if archive_path is None or not archive_path.exists():
        print(f"Error: Archive not found at {archive_path}", file=sys.stderr)
        return 1
    db_path = _cache_db_for(args, archive_path)

    print(f"Inspecting archive: {archive_path.name}...")
    build_index(archive_path, db_path, force=False)

    conn = get_db_connection(db_path)
    info = get_archive_info(conn, archive_path)

    top_senders = conn.execute(
        """
        SELECT sender_name, COUNT(*) as cnt
        FROM messages
        WHERE sender_name IS NOT NULL AND sender_name != ''
        GROUP BY sender_name
        ORDER BY cnt DESC
        LIMIT 8
        """
    ).fetchall()

    top_extensions = conn.execute(
        """
        SELECT
            CASE
                WHEN filename LIKE '%.%' THEN LOWER(SUBSTR(filename, INSTR(filename, '.') + 1))
                ELSE 'no_ext'
            END AS ext,
            COUNT(*) as cnt
        FROM attachments
        GROUP BY ext
        ORDER BY cnt DESC
        LIMIT 6
        """
    ).fetchall()

    conn.close()

    print("\n" + "=" * 50)
    print(f"  EMAIL ARCHIVE SUMMARY: {archive_path.name}")
    print("=" * 50)
    print(f"  Path:            {info.file_path}")
    print(f"  File Size:       {info.file_size_formatted}")
    print(f"  Format:          {info.format}")
    print(f"  Total Folders:   {info.total_folders:,}")
    print(f"  Total Messages:  {info.total_messages:,}")
    print(f"  Total Attachments: {info.total_attachments:,}")
    print(f"  Date Range:      {info.date_range.start[:10] if info.date_range.start else 'N/A'} to {info.date_range.end[:10] if info.date_range.end else 'N/A'}")

    print("\n  Top Senders:")
    for row in top_senders:
        print(f"    • {row['sender_name']}: {row['cnt']:,} emails")

    print("\n  Top Attachment Types:")
    for row in top_extensions:
        print(f"    • .{row['ext']}: {row['cnt']:,} files")
    print("=" * 50 + "\n")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Builds or updates the SQLite + FTS5 cache index."""
    archive_path = _archive_arg(args)
    if archive_path is None or not archive_path.exists():
        print(f"Error: Archive not found at {archive_path}", file=sys.stderr)
        return 1
    db_path = _cache_db_for(args, archive_path)

    print(f"Indexing archive: {archive_path}...")
    t0 = time.time()
    total = build_index(archive_path, db_path, force=args.reindex)
    t1 = time.time()

    db_size = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0
    print(f"Indexed {total:,} messages in {t1 - t0:.2f}s.")
    print(f"Cache saved to {db_path} ({db_size:.2f} MB).")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Starts the local web server and opens the browser."""
    archive_path = _archive_arg(args)
    if archive_path is not None and not archive_path.exists():
        print(f"Error: Archive not found at {archive_path}", file=sys.stderr)
        return 1

    from email_archive_explorer.manager import ArchiveManager

    mgr = ArchiveManager(args.cache_dir or DEFAULT_CACHE_DIR)
    # Register archives in the data folder (and next to the requested file)
    mgr.scan_directory("data/", recursive=True)
    url = f"http://{args.host}:{args.port}"
    if archive_path is not None:
        print(f"Indexing {archive_path.name}...")
        mgr.scan_directory(archive_path.parent, recursive=False)
        mgr.open_archive(archive_path, force_reindex=args.reindex)
        conn = get_db_connection(mgr.get_active_db_path())
        info = get_archive_info(conn, archive_path)
        conn.close()
        archive_line = f"{archive_path.name[:45]:<45}"
        counts = f"{info.total_messages:,} emails across {info.total_folders} folders"
    else:
        archive_line = f"{'(none: choose one in the browser)':<45}"
        counts = "Open an archive from the web UI"
    banner = f"""
╭──────────────────────────────────────────────────────────╮
│  Email Archive Explorer                                  │
│  Archive: {archive_line} │
│  {counts:<56}│
│  Web UI:  {url:<45} │
╰──────────────────────────────────────────────────────────╯
"""
    print(banner)

    if not args.no_browser:
        def open_browser():
            time.sleep(1.0)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    from email_archive_explorer.api import create_app
    from email_archive_explorer.security import LOOPBACK_HOSTS, WILDCARD_BINDS, is_loopback

    allowed_hosts = set(LOOPBACK_HOSTS)
    fs_enabled = True
    if not is_loopback(args.host):
        fs_enabled = args.allow_remote_browse
        print(
            f"WARNING: listening on {args.host} — anyone who can reach this port can read your mail.\n"
            f"         File browsing is {'ENABLED (--allow-remote-browse)' if fs_enabled else 'disabled'}.",
            file=sys.stderr,
        )
        # Binding to all interfaces means clients use the machine's own name/IP.
        allowed_hosts = None if args.host in WILDCARD_BINDS else allowed_hosts | {args.host.strip("[]")}
    app_instance = create_app(
        manager=mgr,
        allowed_hosts=allowed_hosts,
        fs_enabled=fs_enabled,
        browse_roots=args.browse_root or (),
    )

    config = uvicorn.Config(
        app_instance,
        host=args.host,
        port=args.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Batch exports messages to standard .eml files."""
    archive_path = _archive_arg(args)
    if archive_path is None or not archive_path.exists():
        print(f"Error: Archive not found at {archive_path}", file=sys.stderr)
        return 1
    db_path = _cache_db_for(args, archive_path)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    build_index(archive_path, db_path, force=False)
    conn = get_db_connection(db_path)

    query = "SELECT id, subject, folder_path, message_index FROM messages"
    params = []
    if args.folder:
        query += " WHERE folder_path LIKE ?"
        params.append(f"%{args.folder}%")
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    print(f"Exporting {len(rows):,} messages to {out_dir}...")
    reader = detect_driver(archive_path)
    reader.open()

    from email.message import EmailMessage

    count = 0
    for r in rows:
        try:
            detail = reader.get_message_detail(r["folder_path"], r["message_index"], msg_id=r["id"])
            safe_name = "".join(c for c in detail.subject if c.isalnum() or c in (" ", "-", "_")).strip()[:40] or f"msg_{r['id']}"
            eml_file = out_dir / f"{r['id']:05d}_{safe_name}.eml"
            raw = reader.get_raw_message(r["folder_path"], r["message_index"])
            if raw is not None:  # MBOX / EML: original message, byte for byte
                eml_file.write_bytes(raw)
                count += 1
                continue
            eml = EmailMessage()
            eml["Subject"] = detail.subject
            eml["From"] = detail.sender_name or "unknown@local"
            if detail.recipients:
                eml["To"] = ", ".join(rec.name or rec.email for rec in detail.recipients if rec.recipient_type == "To")
            eml.set_content(detail.body_plain or "")
            eml_file.write_bytes(eml.as_bytes())
            count += 1
        except Exception:
            continue

    reader.close()
    print(f"Successfully exported {count:,} .eml files to {out_dir}.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Writes the fictional Larkspur Instruments demo archive."""
    from email_archive_explorer.demo import write_demo

    paths = write_demo(args.out, seed=args.seed)
    print("Demo archive written (all people, companies and files are fictional):")
    print(f"  EML folder tree: {paths['eml']}")
    print(f"  MBOX mailbox:    {paths['mbox']}")
    print(f"\nTry it:  uv run email-archive serve {paths['eml']}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="email-archive",
        description="Fast, local web application to explore email archives (Outlook PST/OST, MBOX, EML folders).",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Command: serve (default)
    p_serve = subparsers.add_parser("serve", help="Launch the web explorer")
    p_serve.add_argument("archive_path", nargs="?", default=None,
                         help="Archive to open (.pst/.ost/.mbox file or a folder of .eml files); omit to choose one in the browser")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    p_serve.add_argument("--no-browser", action="store_true", help="Do not open web browser automatically")
    p_serve.add_argument("--reindex", action="store_true", help="Force rebuild of the archive's cache")
    p_serve.add_argument("--allow-remote-browse", action="store_true",
                         help="Keep the file browser enabled when --host is not a loopback address")
    p_serve.add_argument("--browse-root", action="append", metavar="PATH",
                         help="Extra folder the in-app file browser may show (repeatable)")
    p_serve.add_argument("--cache-dir", default=None, help="Directory for the archive catalog and per-archive caches (default: .cache)")
    p_serve.set_defaults(func=cmd_serve)

    # Command: stats
    p_stats = subparsers.add_parser("stats", help="Display archive statistics in terminal")
    p_stats.add_argument("archive_path", help="Path to an archive (.pst/.ost/.mbox file or a folder of .eml files)")
    p_stats.add_argument("--db-path", default=None, help="Path to SQLite cache database (default: per-archive cache in .cache/)")
    p_stats.set_defaults(func=cmd_stats)

    # Command: index
    p_index = subparsers.add_parser("index", help="Build or update metadata index")
    p_index.add_argument("archive_path", help="Path to an archive (.pst/.ost/.mbox file or a folder of .eml files)")
    p_index.add_argument("--reindex", action="store_true", help="Force rebuild of SQLite cache")
    p_index.add_argument("--db-path", default=None, help="Path to SQLite cache database (default: per-archive cache in .cache/)")
    p_index.set_defaults(func=cmd_index)

    # Command: export
    p_export = subparsers.add_parser("export", help="Batch export messages to .eml format")
    p_export.add_argument("archive_path", help="Path to an archive (.pst/.ost/.mbox file or a folder of .eml files)")
    p_export.add_argument("--out", required=True, help="Destination directory for .eml files")
    p_export.add_argument("--folder", default=None, help="Filter export to specific folder path")
    p_export.add_argument("--limit", type=int, default=None, help="Limit number of emails exported")
    p_export.add_argument("--db-path", default=None, help="Path to SQLite cache database (default: per-archive cache in .cache/)")
    p_export.set_defaults(func=cmd_export)

    # Command: demo
    p_demo = subparsers.add_parser("demo", help="Create a fictional demo archive to try the app with")
    p_demo.add_argument("--out", default="data/demo", help="Output directory (default: data/demo)")
    p_demo.add_argument("--seed", type=int, default=7, help="Random seed (same seed, same files)")
    p_demo.set_defaults(func=cmd_demo)

    args = parser.parse_args()
    if not args.command:
        # Default to serve
        sys.argv.insert(1, "serve")
        args = parser.parse_args()

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
