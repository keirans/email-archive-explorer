# Architecture

How Email Archive Explorer works. Diagrams are in [`docs/images/`](./images/) and adapt to light or dark mode. Design detail and acceptance criteria for each part live in [`specs/`](../specs/).

**Contents**

1. [System overview](#1-system-overview)
2. [Archive drivers and format detection](#2-archive-drivers-and-format-detection)
3. [Indexing pipeline](#3-indexing-pipeline)
4. [Storage: what lives in `.cache/`](#4-storage-what-lives-in-cache)
5. [Multiple archives and safe switching](#5-multiple-archives-and-safe-switching)
6. [Attachment Explorer](#6-attachment-explorer)
7. [Security model](#7-security-model)
8. [Performance notes](#8-performance-notes)
9. [Adding a new archive format](#9-adding-a-new-archive-format)
10. [Test map](#10-test-map)

---

## 1. System overview

![System overview: browser UI, FastAPI app, archive manager, indexer, attachment catalog, driver registry, caches and archives](./images/system-overview.svg)

The app runs on your own machine. `uv run email-archive serve` starts a FastAPI server on `127.0.0.1` and opens the browser UI.

| Layer | Module | Responsibility |
|---|---|---|
| Browser UI | `static/index.html`, `js/ui.js`, `js/app.js`, `js/tree.js`, `js/viewer.js`, `js/attachments.js`, `js/filebrowser.js`, `js/inspector.js` | **Mail** view (folders, messages, reader with Text/HTML body, threads, search, filters, analytics) and **Attachments** view (discover, preview, origin), plus the file browser, Archive Inspector, toasts and help dialog. Zero build step: plain HTML, CSS and native ES modules (`app.js` is the entry point; `ui.js` loads first). |
| File browser | `fs_browser.py` | Confined directory listing with archive badges; native OS file dialog (kdialog, zenity, osascript, tkinter) |
| Inspector | `inspector.py` | File, format, header, contents and index facts; on-demand SHA-256 |
| Security | `security.py` | Host-header check, session token |
| API | `api.py` | REST endpoints. A per-request dependency pins the active archive (driver and cache) for the whole request. |
| CLI | `cli.py` | `serve`, `stats`, `index` and `export`. These use the same per-archive caches as the server. |
| Archive manager | `manager.py` | Catalog of known archives (`catalog.db`), scanning `data/`, opening and switching the active archive under a read/write lock. |
| Indexer | `indexer.py` | Builds and validates caches; folder, message, search, thread and analytics queries. |
| Attachment catalog | `attachments.py` | Derived classification of every attachment: category, duplicates, noise. Also discovery queries and peeking inside ZIP/TAR files. |
| Drivers | `drivers/` | Read-only access to one archive format each, behind `BaseArchiveDriver`. |

Two rules hold throughout:
- **Archives are never written to.** Every driver opens files read-only.
- **Caches are disposable.** Everything under `.cache/` can be deleted and is rebuilt the next time an archive is opened.

---

## 2. Archive drivers and format detection

![Format detection: content probe by each driver, then extension fallback, else ValueError](./images/driver-detection.svg)

Everything above the driver layer talks only to [`BaseArchiveDriver`](../src/email_archive_explorer/drivers/base.py). A message is addressed as `(folder_path, message_index)`.

| Driver | Reads | Implementation notes |
|---|---|---|
| `PstDriver` | `.pst`, `.ost` (ANSI and Unicode) | `libpff-python`. Caches libpff folder objects (see §8). Reports the header version, e.g. "Outlook PST (ANSI, Outlook 97–2002)". Identifies unnamed attachments by content: embedded Word/Excel documents and attached Outlook items. |
| `MboxDriver` | `.mbox`, `.mbx`, or extension-less mbox files (Thunderbird) | Its own boundary scanner, opened `rb` only. The standard `mailbox.mbox` opens files read-write, so it isn't used. Undoes `>From ` quoting. |
| `EmlDirDriver` | Folder trees of `.eml` files, Maildir, Maildir++, or a single `.eml` | Each directory becomes a folder. The cache fingerprint is total bytes plus the newest modification time. |

**Required methods:** `format_name`, `supported_extensions`, `probe`, `open`, `close`, `get_folder_paths`, `get_folder_message_count`, `get_message_detail`, `get_attachment_bytes`.

**Optional hooks** (with defaults):

| Hook | Purpose |
|---|---|
| `describe()` | Format label shown in the UI |
| `fingerprint()` | Decides whether a cache is stale |
| `get_message_for_index()` | A cheaper extraction used while indexing |
| `get_raw_message()` | Original RFC 822 bytes, so `.eml` export of MBOX/EML mail is lossless |
| `index_version` | Bumping it rebuilds caches built by an older version of the driver |

RFC 822 parsing (bodies, recipients, attachments, attached emails, HTML-to-text) is shared by the MBOX and EML drivers in `drivers/rfc822.py`.

---

## 3. Indexing pipeline

![Indexing pipeline: cache check, detect driver, folder pass, message pass, archive_meta, attachment catalog](./images/indexing-pipeline.svg)

`build_index(path, db)` is format-agnostic.
1. Folders come back parents first. A folder's parent is the longest already-seen path that prefixes it, so names containing `/` still work.
2. Each message contributes a row to `messages`, rows to `attachments`, and a row to the FTS5 table, which covers subject, sender, recipients and body.
3. The cache is valid only while the archive's fingerprint and the driver's `index_version` match what is stored in `archive_meta`.
4. After the message pass, `threads.compute_threads()` groups messages into conversations and stores them in `threads` and `messages.thread_id` (see below).

### Conversations

Threads are computed once, at index time, by `threads.py` (pure Python, unit-tested on its own). Signals in priority order:

| Signal | Source | Notes |
|---|---|---|
| `In-Reply-To` / `References` → `Message-ID` | RFC 822 headers (MBOX, EML; PST transport headers when present) | Duplicate copies sharing a `Message-ID` also join |
| Outlook conversation index | First 22 bytes of `PR_CONVERSATION_INDEX` (PST) | Shared by every message in an Outlook conversation |
| Subject fallback | Normalised subject (all `Re:`/`Fw:`/`[list]` prefixes stripped) | Only for messages the signals above left on their own, only for replies, within 60 days, never for generic subjects ("Update", "Hi", …), and never bridging two existing conversations |

A union-find links messages along these signals. The reader shows a one-line **conversation bar** (`3 of 12`, Prev/Next, `n`/`p`) that expands (`t`) into a capped, scrolling list fetched 50 at a time from `/api/messages/{id}/thread`. The header zone above the body is capped at 40% of the reading pane, so the message itself always stays readable. Design and measurements: [Spec 10](../specs/10-conversation-threads-and-reading-pane.md).

---

## 4. Storage: what lives in `.cache/`

![Cache layout: catalog.db with known_archives and scan_directories; cache_id.db with archive_meta, folders, messages, threads, attachments, messages_fts and attachment_catalog](./images/cache-layout.svg)

- `catalog.db` backs the **Archives** menu: known archives, their sizes and counts, when each was last opened, and which folders have been scanned.
- `cache_<id>.db` holds one archive's index, named by the first 8 hex characters of the SHA-256 of its absolute path. Archives never share a cache, so they can't pollute each other's tables.
- `attachment_catalog` is **derived**. It is rebuilt whenever `CATALOG_VERSION` or the attachment count changes, so older caches gain the Attachments view without a reindex.

The legacy `archive_cache.db` in the project root predates the per-archive caches and is no longer used; it can be deleted.

---

## 5. Multiple archives and safe switching

![Switching archives: requests hold a shared lock on archive A while B is indexed; a brief exclusive swap; later requests use B; A's driver is closed](./images/archive-switching.svg)

`ArchiveManager.session()` hands each request a consistent snapshot: the driver and the cache path. `open_archive()` indexes the new archive **before** taking the exclusive lock, so the current archive keeps serving while a large one indexes. The lock is held only for the swap itself; after that the old driver is closed.

Covered by `test_concurrent_requests_during_switching` and `test_parallel_requests_share_threadpool`.

---

## 6. Attachment Explorer

![Attachment explorer data flow: classify, group duplicates, noise rules, attachment_catalog, API, and the Attachments view](./images/attachment-explorer.svg)

The **Attachments** tab (`g a`) answers "what's actually in this archive?". It hides signature logos, stationery and mail-server footers by default and collapses forwarded copies into one row with a ×N badge. **Discover** lenses surface the largest files, rare file types, the most-forwarded files, videos, archives, presentations, programs and photos.

For each file, the detail pane shows:
- an inline preview (image, PDF, text, sandboxed HTML, audio/video, or the files inside a ZIP/TAR)
- the **origin message** (sender, recipients, date, folder, opening lines)
- every other email carrying the same file, and who sent it first

**Open email** (`o`) jumps to the Mail view on the right folder, page and message.

The full design is in [Spec 09](../specs/09-attachment-explorer.md).

---

## 7. Security model

| Concern | Current behaviour |
|---|---|
| Archive integrity | Drivers open archives read-only; no code path writes to them |
| Network exposure | Binds to `127.0.0.1` by default. A non-loopback `--host` prints a warning and disables the file browser unless `--allow-remote-browse` is given. |
| Other websites reading the API | No CORS headers are sent, so a page on another origin can't read responses |
| DNS rebinding | `HostCheckMiddleware` answers `421` unless the `Host` header is `127.0.0.1`, `localhost` or `::1` (plus the `--host` name) |
| Cross-site requests | Every state-changing request (`POST` open, scan, ZIP, checksum, native picker) and every `/api/fs/*` route needs the per-process session token. The token is injected into `index.html` and sent as `X-EAE-Token` by a fetch wrapper in `ui.js`. |
| File browser | Confined to Home, `data/`, mounted drives and `--browse-root` folders. Paths are resolved (`..` and symlinks) before the containment check. |
| HTML mail | Rendered in `<iframe sandbox>` (no scripts, no same-origin access) with a CSP that blocks all network loads until you choose **Load remote images** |
| HTML/SVG attachments | Inline responses carry `Content-Security-Policy: sandbox`; previews use `<iframe sandbox>`. Old HTML can't run script against the local API. |
| MIME sniffing | `X-Content-Type-Options: nosniff` on every attachment response |
| ZIP/TAR peeking | Read in memory, never extracted to disk. Member names are reduced to base names; downloads are capped at 200 MB. |

Covered by `tests/test_security.py` and `tests/test_fs_browser.py`.

**Accessibility.** The folder tree and message list are ARIA widgets (`tree`, `listbox`) with roving tabindex, and every dialog is a native `<dialog>`. Colour tokens meet WCAG 2.1 AA in both themes. `scripts/ui_audit.py` runs axe-core in headless Chrome over every view and dialog, in light and dark, and fails on any violation or JavaScript error.

---

## 8. Performance notes

Measured on a 415 MB ANSI PST (4,101 messages, 2,068 attachments); a 26,110-message PST indexes in about 12 s:

| Operation | Time |
|---|---|
| Full index build | ≈ 5 s |
| Opening an archive with a fresh cache | < 1 s |
| Opening one message (PST) | ≈ 3 ms |
| Building the attachment catalog | ≈ 15 ms |
| Attachment list query | ≈ 7 ms |

- **libpff folder cache.** Creating a libpff folder object re-reads that folder's message table, about 14 ms each time. Without caching folder objects per path, a full index took 68 s instead of 5 s.
- **SQLite and threads.** FastAPI may open a request's connection in one worker thread and use it in another, so connections are created with `check_same_thread=False`. Each connection still serves a single request at a time.

---

## 9. Adding a new archive format

1. Create `drivers/<name>_driver.py` with a subclass of `BaseArchiveDriver`:
   - set `format_type` (a short ID stored in the catalog) and, for folder-based formats, `is_directory_format = True`
   - implement the required methods
   - make `probe()` cheap: read a few bytes, never the whole file
2. If the format stores standard RFC 822 messages, reuse `rfc822.message_to_detail()` and `extract_attachment()`, and return the bytes from `get_raw_message()`.
3. Register the driver in `drivers/__init__.py`. Order matters: the most specific probe goes first.
4. If directories of this format should be found when `data/` is scanned, extend `ArchiveManager._discover()`.
5. Add cases to `tests/test_drivers.py`. `test_driver_interface_compliance` picks the new driver up automatically.

Nothing in the indexer, API or UI needs to change.

---

## 10. Test map

Run the suite with `uv run pytest`. Tests run against the fictional demo archive, generated into a temporary folder. Tests that need a real Outlook PST (no library can write one) are skipped unless you point `EAE_TEST_PST` at a `.pst` file.

| File | Tests | Covers |
|---|---|---|
| `test_indexer.py` | 8 | Index build, cache validity, folder tree, sorting, FTS search (demo archive); PST reading and PST threads (opt-in) |
| `test_api.py` | 10 | REST endpoints: folders, messages, attachments, search, `.eml` export, starting with no archive open |
| `test_cli.py` | 4 | `stats`, `index`, missing-archive error, `demo` |
| `test_advanced.py` | 5 | Advanced search filters, thread API window, analytics, ZIP export |
| `test_threads.py` | 10 | Subject normalisation, generic subjects, Message-ID and conversation-index parsing, header chains, subject fallback guards (gap, reply prefix, no bridging), stored threads and windowing |
| `test_archive_manager.py` | 10 | Discovery, switching, concurrent requests while switching, cache isolation, parallel-request threading |
| `test_drivers.py` | 28 | Driver interface compliance, probing, MBOX/EML/Maildir indexing, attachment extraction, scan discovery, API on MBOX/EML, unnamed PST attachment sniffing, `index_version` invalidation |
| `test_security.py` | 11 | No CORS, Host-header check (incl. rebinding names), token on state changes and file routes, token injection into `index.html`, file routes disabled for remote binds |
| `test_fs_browser.py` | 9 | Roots, archive badges (including EML folders one level down), confinement against `..` and symlinks, unreadable folders, truncation, native picker (no display, success, cancel, one at a time) |
| `test_open_jobs.py` | 3 | Background indexing with progress while the old archive keeps serving, 409 on a concurrent open, `wait`, failed jobs |
| `test_archive_details.py` | 4 | PST header facts and SHA-256 (opt-in), MBOX whole-file checksum, MBOX/EML layout details, manifest checksum |
| `scripts/ui_audit.py` | — | Not part of pytest; needs a running server and Chrome. axe-core over the Mail view (conversation collapsed and expanded), Attachments view, File Browser, Help and Inspector in both themes, plus folder-tree keyboard navigation and JavaScript errors. |
| `scripts/capture_screenshots.py` | — | Regenerates the README screenshots from the demo archive in a scratch home folder; refuses to run against anything else. |
| `scripts/reading_pane_check.py` | — | Not part of pytest; needs Chrome. Generates a worst-case archive (2,000-message thread, 300 recipients, 60 attachments) and checks the body starts within the top 45% of the reading pane at 1366×768. |
| `test_attachments.py` | 18 | Classification, noise rules, duplicates, filters, sorting, summary, origin and copies, ZIP/TAR contents, bulk ZIP, security headers, message position, the demo archive's planted gems and noise |
| `test_privacy.py` | 3 | Generic privacy rules (home paths, non-example email addresses, archive files), denylist matching, and a clean scan of the repository |
