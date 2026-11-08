# Component Spec 04: CLI Runner, Packaging & Execution

## 1. Scope & Purpose

This component specifies:
1. The project structure and packaging configuration (`pyproject.toml`) compliant with modern Python packaging standards (PEP 621) and `uv`.
2. The command-line interface (CLI) for running the server, building the index, inspecting statistics, and exporting archives.
3. Automated startup behavior (auto-indexing, browser opening, graceful shutdown).

---

## 2. Directory & Package Structure

```text
email-archive-explorer/
├── pyproject.toml                     # Project metadata & uv dependencies
├── README.md                          # User documentation & quickstart
├── specs/                             # Component specifications
│   ├── 00-system-overview-and-roadmap.md
│   ├── 01-data-model-and-indexer.md
│   ├── 02-backend-api.md
│   ├── 03-web-interface.md
│   ├── 04-cli-and-runner.md
│   └── AI-TOOL-EXECUTION-GUIDE.md
├── src/
│   └── email_archive_explorer/
│       ├── __init__.py
│       ├── cli.py                     # CLI entrypoint
│       ├── config.py                  # App configuration & path defaults
│       ├── reader.py                  # pypff PST reader abstraction
│       ├── models.py                  # Pydantic & database data models
│       ├── indexer.py                 # SQLite + FTS5 indexing engine
│       ├── api.py                     # FastAPI routes & lifespan
│       └── static/                    # Frontend assets
│           ├── index.html
│           ├── css/
│           └── js/
├── tests/
│   ├── test_reader.py
│   ├── test_indexer.py
│   └── test_api.py
└── data/
    └── mail/
        └── archive-2003.pst             # Original read-only archive (git-ignored)
```

---

## 3. Project Configuration (`pyproject.toml`)

```toml
[project]
name = "email-archive-explorer"
version = "0.1.0"
description = "Fast, web-based email archive explorer for legacy Outlook PST files"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "libpff-python>=20231122",
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.28.0",
    "pydantic>=2.6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "httpx>=0.27.0",
]

[project.scripts]
email-archive = "email_archive_explorer.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## 4. CLI Commands & Parameters (`cli.py`)

The application provides a unified CLI tool:

### 4.1. `email-archive serve` (Default Command)
Launches the web application.

```bash
uv run email-archive serve [PST_PATH] [OPTIONS]
```

**Options:**
- `PST_PATH`: Path to `.pst` file (default: auto-detects `data/mail/archive-2003.pst` or any `.pst` in current directory).
- `--host TEXT`: Bind host (default: `127.0.0.1`).
- `--port INTEGER`: Bind port (default: `8000`).
- `--no-browser`: Do not automatically open the web browser.
- `--reindex`: Force a full rebuild of the SQLite cache even if valid.

**Startup Workflow:**
1. Validates the existence and readability of the PST file.
2. Checks if `archive_cache.db` exists and matches the PST file's size/mtime.
3. If missing or invalid, runs the indexer with a progress indicator (taking ~3-4 seconds).
4. Starts `uvicorn` serving the FastAPI application.
5. Prints terminal banner with local URL:
   ```text
   ╭──────────────────────────────────────────────────────────╮
   │  Email Archive Explorer                                  │
   │  Archive: data/mail/archive-2003.pst (4,101 messages)   │
   │  Web UI:  http://127.0.0.1:8000                          │
   ╰──────────────────────────────────────────────────────────╯
   ```
6. Automatically opens the default browser tab to `http://127.0.0.1:8000`.
7. Catches `SIGINT` (Ctrl+C) to gracefully close file handles and terminate.

---

### 4.2. `email-archive index`
Explicitly builds or updates the metadata index without starting the web server.

```bash
uv run email-archive index [PST_PATH] [--reindex]
```

**Output:**
```text
Inspecting archive: data/mail/archive-2003.pst...
Found 135 folders, indexing messages...
Indexed 4,101 messages and 2,068 attachments in 3.42s.
Cache saved to archive_cache.db (7.8 MB).
```

---

### 4.3. `email-archive stats`
Prints a formatted summary of the archive to standard output.

```bash
uv run email-archive stats [PST_PATH]
```

**Output:**
```text
Archive: archive-2003.pst (415.41 MB)
Format: Outlook ANSI (<= 2002, 32-bit)
Total Folders: 135
Total Messages: 4,101
Total Attachments: 2,068 (332.77 MB)
Date Range: 2000-10-31 to 2003-08-08
Top Senders:
  - KESTREL,PRIYA (657)
  - WHITLOW,SAM (486)
  - ASHGROVE,JORDAN (433)
  - THORNBURY,ALEX (193)
```

---

### 4.4. `email-archive export`
Batch exports messages or folders to standard `.eml` files.

```bash
uv run email-archive export [PST_PATH] --out ./exported_emls [--folder "Projects"]
```

---

## 5. Verification & Acceptance Criteria

1. Running `uv run email-archive stats data/mail/archive-2003.pst` executes without warnings and prints archive statistics in < 4 seconds.
2. Running `uv run email-archive index` successfully generates `archive_cache.db`.
3. Running `uv run email-archive serve --no-browser` boots the server on port 8000 and responds to HTTP requests.
4. Pressing Ctrl+C exits cleanly with return code 0.
