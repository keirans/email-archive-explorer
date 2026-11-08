# Component Spec 09: Attachment Explorer ("Hidden Gems")

> **Status: implemented.** Code: `attachments.py`, the `/api/attachments*` routes in `api.py`, `static/js/attachments.js` and `static/css/attachments.css`. Tests: `tests/test_attachments.py`.

## 1. Scope & Purpose

Old archives hold files that no longer exist anywhere else: training decks, firmware notes, photos, home videos, zipped toolkits. In the mail view they're buried one message at a time. This phase adds a dedicated **Attachments** view to work through every attachment in the open archive quickly, to **surface the interesting ones**, and to show where each one **came from** (its origin message, sender, folder, and other places the same file appears).

Works for every driver from Spec 07 (PST, MBOX, EML). No change to the core index schema: it adds a **derived table** that rebuilds itself, so existing caches don't need reindexing.

### 1.1. What a Real Archive Shows (an early-2000s corporate PST used during development, 2,068 attachments)

| Finding | Numbers | Design consequence |
|---|---|---|
| Signature logos, spacer GIFs, Outlook stationery dominate | 684 images under 10 KB; one corporate signature logo ×237, `clip_image001.gif` ×142, `Outlook..bmp` (937 KB stationery) ×19 | **Noise filter on by default**, with the reason shown and one click to reveal |
| The same file was forwarded many times | 148 duplicate groups | **Collapse duplicates** by default; show "×N copies" and the first time each was seen |
| Real finds are a small minority | a 13 MB zip of slide decks, a 7 MB PDF, `.mpg`/`.avi` videos, `.ppt` decks, `.exe` tools | A **Discover** summary: largest files, rare file types, most-forwarded, by category and year |
| 180 attachments have no extension | e.g. `attachment_2` | Fall back to MIME type for the category; never hide these as noise |
| ZIPs hide their contents | 26 zips, 4 tars, 23 MB | **List files inside archives** without downloading them |

---

## 2. Backend

### 2.1. Derived Attachment Catalog (`attachments.py`)

This table is stored in each archive's own cache DB and rebuilt automatically when the index changes or `CATALOG_VERSION` is bumped:

```sql
CREATE TABLE IF NOT EXISTS attachment_catalog (
    attachment_id INTEGER PRIMARY KEY,  -- = attachments.id (no FK: derived, dropped on re-index)
    ext           TEXT NOT NULL,      -- lower-case, no dot; '' if none
    category      TEXT NOT NULL,      -- see §2.2
    name_key      TEXT NOT NULL,      -- lower-case filename
    dup_group     INTEGER NOT NULL,   -- smallest attachment id sharing (name_key, size)
    copies        INTEGER NOT NULL,   -- size of that group
    is_noise      INTEGER NOT NULL,
    noise_reason  TEXT                -- 'small-image' | 'repeated-image' | 'outlook-artifact'
);
```
- `ensure_attachment_catalog(conn)` checks `archive_meta.attachment_catalog_version` and the row count against `attachments`, and rebuilds inside a single transaction when either is stale. A module-level lock prevents two simultaneous rebuilds.
- `build_index()` calls it at the end, so new archives are ready immediately. Older caches build it on first use (≈20 ms for 2k attachments).
- **Duplicates** are matched by `(lower(filename), size_bytes)`. This is a metadata-only heuristic that needs no extra reads from the archive. Content hashing is a future option (§5).

### 2.2. Categories

| Category | Extensions (MIME fallback when no extension) |
|---|---|
| `documents` | doc docx rtf odt pdf wpd wri txt md pages (`application/msword`, `application/pdf`, `text/plain`) |
| `spreadsheets` | xls xlsx xlsm xlt csv ods |
| `presentations` | ppt pptx pps ppsx pot odp key |
| `images` | jpg jpeg png gif bmp tif tiff pcx svg webp heic ico (`image/*`) |
| `video_audio` | mpg mpeg mpe mp4 avi mov wmv asf mp3 wav wma mid midi (`video/*`, `audio/*`) |
| `archives` | zip rar 7z gz tgz tar bz2 cab arj lzh (`application/zip`, …) |
| `email` | eml msg mht (`message/rfc822`) |
| `contacts_calendar` | vcf ics vcs |
| `web_code` | htm html xml js vbs bat cmd sh ps1 c cpp h py java sql json ini cfg log url (`text/html`, `text/xml`) |
| `programs` | exe dll msi com scr |
| `drawings` | vsd vdx dwg dxf |
| `other` | everything else |

### 2.3. Noise Rules (all reversible in the UI)

1. **`small-image`:** a category `images` file smaller than 10 KB (signatures, bullets, spacers).
2. **`repeated-image`:** a category `images` file with 5 or more copies (corporate logos, Outlook stationery backgrounds such as `Outlook..bmp`, `Glacier Bkgrd.jpg`).
3. **`outlook-artifact`:** `winmail.dat`, `oledata.mso`, `ATT00001.htm`-style footers (`^att\d+\.(txt|htm|html|c)$`), `image001.png`-style names under 50 KB.
4. **`mail-footer`:** mail-server disclaimer files (`*disclaimer*.txt/htm`) under 10 KB, e.g. `InterScan_Disclaimer.txt`.
5. **`embedded-item`:** an attached Outlook item that the PST driver can't extract as a file (see below).

No other file without an extension is treated as noise.

#### Unnamed PST attachments (driver change, PST `index_version` 2)

Outlook stores some attachments with no file name. Previously these all appeared as `attachment_N`. In the development archive, 180 attachments had no name. The PST driver now identifies them from their content:

| Content | Detected by | Shown as |
|---|---|---|
| Attached Outlook item (a property-context heap, `bSig 0xEC / 0xBC`) | bytes 2–3 = `EC BC` | `Embedded Outlook item N` (MIME `application/x-pst-embedded-item`, category `email`, noise `embedded-item`). libpff can't rebuild the message, so the UI explains this instead of offering a broken download. |
| OLE compound file (`D0 CF 11 E0 …`) | Stream names inside (`WordDocument`, `Workbook`/`Book`, `PowerPoint Document`, `VisioDocument`) | `embedded_object_N.doc/.xls/.ppt/.vsd`, or `.ole` when unknown |
| PDF, ZIP, JPEG, PNG, GIF, BMP, RTF | Leading magic bytes | `attachment_N.<ext>` |

In the development archive this identifies 177 of the 180 unnamed attachments: 133 attached Outlook items, 3 Word and 6 Excel documents, 17 other OLE objects, and a few images.

`BaseArchiveDriver.index_version` (Spec 07) is stored in `archive_meta.driver_index_version`. When a driver's version changes, `is_cache_valid()` returns false, so existing caches are rebuilt automatically the next time the archive is opened.

### 2.4. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/attachments` | Filtered, sorted, paginated list (one row per attachment, or per duplicate group when collapsed) |
| `GET` | `/api/attachments/summary` | Discover dashboard data |
| `GET` | `/api/attachments/{id}` | One attachment, its origin message, every copy, and its preview type |
| `GET` | `/api/attachments/{id}/contents` | Files inside a ZIP/TAR attachment |
| `GET` | `/api/attachments/{id}/contents/{index}` | Download one file from inside that ZIP/TAR |
| `POST` | `/api/attachments/zip` | Download selected attachments as one ZIP (`{"ids": [...]}`, max 500) |

**`GET /api/attachments` query parameters:**
- `q` (filename contains), `category`, `ext`, `year`, `sender`, `folder_id` (includes sub-folders)
- `date_from`, `date_to`, `min_size`, `max_size`
- `hide_noise=true`, `collapse=true`
- `sort=date|size|name|sender|copies|type`, `order=asc|desc`, `page`, `page_size` (≤ 200)

**Response:**
```json
{
  "total": 1012, "total_bytes": 290112233, "page": 1, "page_size": 60, "total_pages": 17,
  "items": [{
    "id": 812, "message_id": 2210, "attachment_index": 0,
    "filename": "heron-firmware-2.4.1.zip", "ext": "zip", "category": "archives",
    "mime_type": "application/zip", "size_bytes": 900792, "size_formatted": "879.68 KB",
    "copies": 1, "is_noise": false, "noise_reason": null, "preview": "archive",
    "view_url": "/api/messages/2210/attachments/812",
    "download_url": "/api/messages/2210/attachments/812?download=1",
    "message": {"id": 2210, "subject": "Heron firmware 2.4.1 release", "sender_name": "…",
                "sender_email": "", "date_iso": "2003-09-08T09:00:00", "folder_id": 41,
                "folder_path": "Top of Personal Folders/Projects/Tidewater Sensor"}
  }]
}
```
- When `collapse=true`, the row shown for each duplicate group is its **earliest-dated copy**, the likely original.
- Filters apply before collapsing, so `copies_matching` reports how many copies matched the filters, while `copies` is the total across the archive.

**`GET /api/attachments/{id}`** adds:
- `origin`: subject, sender, recipients, date, folder, body preview, attachment count
- `copies`: every attachment in the group with its message summary, oldest first
- `first_seen`: the oldest copy, i.e. who first sent this file and when

**Previews** (`preview` field):

| Value | Applies to | How the UI shows it |
|---|---|---|
| `image` | gif, jpg, png, bmp, svg, … | Inline `<img>` |
| `pdf` | pdf | Browser PDF viewer in an `<iframe>` |
| `text` | txt, log, csv, ini, xml, json, vcf, url, bat, eml, … | First 256 KB as text |
| `html` | htm, html, mht | `<iframe sandbox>`: no scripts, own origin |
| `video` / `audio` | mp4, webm, ogg, mp3, wav | `<video>` / `<audio>` (legacy mpg/avi/wmv show "download to play") |
| `archive` | zip, tar, tgz, tar.gz | Listing of the files inside |
| `none` | everything else | Download only |

### 2.5. Security

- Inline responses from the existing attachment endpoint get `X-Content-Type-Options: nosniff`.
- Responses for active types (`text/html`, `image/svg+xml`, XML) also get `Content-Security-Policy: sandbox`. An HTML attachment from 2002 opened in a new tab can't run script against the local API.
- Listing archive contents is metadata only. Member downloads are capped at 200 MB and file names are sanitised, so there is no path traversal (we never extract to disk).

---

## 3. Frontend

### 3.1. Placement

There are **Mail | Attachments** view tabs in the header, with `g m` / `g a` keyboard shortcuts. The Attachments view replaces the three mail panes but keeps the header, search and archive switcher:

```text
+-----------------------------------------------------------------------------------------------+
| 📬 Explorer   [ Mail | Attachments ]   📁 larkspur-mail ▾                         [📊] [☀️]   |
+------------------+-------------------------------------------------+--------------------------+
| FIND             | DISCOVER  17 files · 1.07 MB (36 noise hidden)  | heron-firmware-2.4.1.zip |
| [🔍 filename   ] | [Largest] [Rare types] [Most forwarded] [Videos] | archives · 879.7 KB      |
| ☑ Hide noise     | ─────────────────────────────────────────────── | ┌──────────────────────┐ |
| ☑ Collapse dupes | ☐ ▤ / ▦   Sort: Largest ▾     ⬇ ZIP selected (3) | │ 3 files inside:      │ |
| CATEGORY         | ☐ 🗜 heron-firmware-2.4.1.zip   880 KB  Sep 2003 | │  heron-2.4.1/heron…  │ |
|  Documents     9 | ☐ 🗜 Tidewater field trial…      70 KB  Aug 2003 | │  …                   │ |
|  Images        6 | ☐ 🖼 picnic-2004-02.png          41 KB  Feb 2004 | └──────────────────────┘ |
|  Spreadsheets  3 | ☐ 📕 Tidewater TS-40 spec sh…   1 KB   ×5       | ORIGIN MESSAGE           |
|  Archives      2 | …                                               | "Heron firmware 2.4.1…"  |
| YEAR             |                                                 | From Sam Whitlow         |
|  2003 █ 2004 ▂   |                                                 | 8 Sep 2003 · Tidewater … |
|  2005 ▃          |                                                 | [Open email] [⬇ Download]|
| SIZE  [Any ▾]    |                                                 | COPIES (1) first seen …  |
+------------------+-------------------------------------------------+--------------------------+
|  Page 1 of 1 · ‹ ›                                    j/k move · space preview · o open email |
+-----------------------------------------------------------------------------------------------+
```

### 3.2. Behaviour

1. **Discover strip:** one-click lenses that set filters and sort:
   - **Largest** (size desc)
   - **Rare types** (extensions seen at most twice)
   - **Most forwarded** (copies desc)
   - **Oldest** (date asc)
   - one chip per notable category (Videos, Archives, Presentations, Programs)

   Totals show what's hidden (`684 noise hidden · show`).
2. **Facets sidebar:**
   - filename search (debounced)
   - Hide noise and Collapse duplicates toggles
   - category list with counts
   - year bars (click to filter)
   - size filter (Any / > 100 KB / > 1 MB / > 10 MB)
   - sender filter
   - active filters shown as removable chips
3. **Results:**
   - **List mode:** icon, name, size, date, sender, subject, copies badge, noise badge
   - **Grid mode:** thumbnails for images (lazy-loaded `<img loading="lazy">`), type tiles for everything else
   - checkboxes for bulk ZIP download
4. **Detail pane:**
   - a preview based on the `preview` type (§2.4)
   - metadata
   - the **origin message** card, with "Open email", which jumps to the Mail view with the folder and message selected
   - the **copies** list, where each entry can be clicked to open its email, and a "first seen" line
   - actions: download, copy name, open in a new tab (only for preview types the browser can show inline)
5. **Keyboard:**
   - `j`/`k` or ↓/↑ moves through results; `[`/`]` changes page
   - space toggles a larger preview; `o` opens the origin email
   - `d` downloads; `x` toggles selection
   - `/` focuses filename search
6. **Scrolling and paging:** the result list scrolls on its own, under the mouse wheel, while the facets and the detail pane stay in view. This is the fix for a reported bug: grid rows default to `min-height: auto`, so the list grew instead of scrolling, which also hid the pager. Pages can be changed from the toolbar pager or the bottom pager, with `[` `]` or `PgUp`/`PgDn`, or by moving past the last row. Page size is 25, 60, 100 or 200 and is remembered; changing it keeps your place. Each new page scrolls to the top and selects its first file (or its last, when paging backwards with `k`), so the detail pane always matches.
7. Switching archives resets the view. The view and its filters persist across reloads in `localStorage`, guarded with try/catch.

### 3.3. Spec 08 Alignment

The code lives in `static/js/attachments.js` and `static/css/attachments.css`, self-contained with no new globals beyond `window.AttachmentExplorer`. It becomes `ui/attachments.js` under Spec 08 §4.6, and its emoji icons are swapped for the Spec 08 sprite.

---

## 4. Acceptance Criteria & Test Plan (`tests/test_attachments.py`)

1. **Catalog:** builds on first use for an existing cache, and rebuilds when the attachment count or version changes. Categories and extensions are correct for a synthetic archive; MIME fallback applies when there is no extension.
2. **Noise:**
   - small and repeated images are flagged with the right reason
   - Outlook artifacts are flagged
   - a 12 MB zip and an extension-less file are never noise
   - on the demo archive, the repeated `larkspur-logo.png` signature logo is flagged `repeated-image`
3. **Listing:**
   - each filter narrows results correctly; `folder_id` includes sub-folders
   - `collapse` returns one row per group, using the earliest copy
   - sorts work in both directions
   - `total_bytes` matches the sum
4. **Detail:**
   - `origin` matches the message
   - `copies` lists every message carrying the file, oldest first
   - `first_seen` is the oldest copy
5. **Archive contents:**
   - ZIP and TAR listings return names and sizes; member download returns exact bytes
   - a corrupt or non-archive file → 422
   - an out-of-range index → 404
6. **Bulk ZIP:** returns every requested file; duplicate names become `name (2).ext`; more than 500 IDs → 400.
7. **Security:**
   - an inline `text/html` attachment response carries `Content-Security-Policy: sandbox` and `nosniff`
   - a PDF response carries `nosniff` but not the sandbox header (it would break the browser's PDF viewer)
8. **Manual:** on the demo archive (`email-archive demo`), the Discover → Largest lens shows `heron-firmware-2.4.1.zip` first. Its contents list. "Open email" lands on the right message in the Mail view.

---

## 5. Future Work (not in this phase)

- **Unwrap OLE packages** (`embedded_object_N.ole`, an `Ole10Native` stream holding the original file name and bytes) with `olefile`, so the remaining OLE objects show their real files.
- **Recover attached Outlook items** by parsing the property-context heap directly. libpff doesn't expose these as sub-items for this archive.

- **Text extraction from attachments** (PDF, legacy `.doc`/`.xls`/`.ppt`) into a separate FTS table, so file contents are searchable. This is the strongest "hidden gems" feature, but it needs new dependencies and a background extraction job.
- **Content-hash duplicates** (SHA-1 of bytes), to catch the same file saved under different names (`image001.jpg` vs `image002.jpg`).
- **Starring and tagging** gems, stored in `catalog.db` so marks survive reindexing.
- Thumbnails for PDF first pages and Office documents.
