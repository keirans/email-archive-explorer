# Component Spec 02: Backend REST API

## 1. Scope & Purpose

This component specifies the REST API built with **FastAPI**. It serves:
1. Archive metadata and folder tree navigation.
2. Paginated and sortable message listings per folder.
3. Full message details, rendering plain text and formatted bodies.
4. On-demand streaming of attachment files with proper MIME types.
5. Instant full-text search with keyword highlight snippets via SQLite FTS5.
6. Dynamic RFC 822 `.eml` export for any message in the archive.
7. Hosting static assets for the single-page frontend.

---

## 2. Server Configuration & Lifespan

- **Framework:** `fastapi` with `uvicorn` (standard ASGI).
- **Execution:** `uv run uvicorn email_archive_explorer.api:app --host 127.0.0.1 --port 8000`
- **Lifespan Manager:**
  - On startup: Verifies PST existence, checks/builds the SQLite index, opens a persistent read-only `PstReader` instance.
  - On shutdown: Closes SQLite connections and cleanly releases PST file handles.

---

## 3. API Endpoints Specification

### 3.1. Archive Overview
`GET /api/archive/info`

**Response (`200 OK`):**
```json
{
  "file_path": "data/mail/archive-2003.pst",
  "file_size_bytes": 435585024,
  "file_size_formatted": "415.41 MB",
  "format": "Outlook ANSI (<= 2002, 32-bit)",
  "total_folders": 135,
  "total_messages": 4101,
  "total_attachments": 2068,
  "date_range": {
    "start": "2000-10-31T01:36:51",
    "end": "2003-08-08T05:20:44"
  }
}
```

---

### 3.2. Folder Tree Hierarchy
`GET /api/folders`

**Query Parameters:**
- `include_empty` (boolean, optional, default `false`): Whether to include folders with 0 messages.

**Response (`200 OK`):**
```json
[
  {
    "id": 1,
    "parent_id": null,
    "name": "Top of Personal Folders",
    "full_path": "Top of Personal Folders",
    "depth": 1,
    "message_count": 0,
    "children": [
      {
        "id": 4,
        "parent_id": 1,
        "name": "Projects",
        "full_path": "Top of Personal Folders/Projects",
        "depth": 2,
        "message_count": 34,
        "children": [ ... ]
      }
    ]
  }
]
```

---

### 3.3. Folder Messages List
`GET /api/folders/{folder_id}/messages`

**Path Parameters:**
- `folder_id` (integer, required): Target folder ID.

**Query Parameters:**
- `page` (integer, default `1`): 1-indexed page number.
- `page_size` (integer, default `50`, max `200`): Items per page.
- `sort_by` (string, enum: `date`, `sender`, `subject`, default `date`).
- `sort_order` (string, enum: `asc`, `desc`, default `desc`).
- `has_attachments` (boolean, optional): Filter by attachment presence.

**Response (`200 OK`):**
```json
{
  "folder_id": 4,
  "folder_name": "Projects",
  "total": 34,
  "page": 1,
  "page_size": 50,
  "total_pages": 1,
  "items": [
    {
      "id": 1024,
      "message_index": 12,
      "subject": "FW: Saltmarsh rig calibration - batch 0412",
      "sender_name": "THORNBURY,ALEX (Field Engineering)",
      "sender_email": "alex.thornbury@larkspur.example",
      "date_iso": "2001-03-19T09:42:10",
      "has_attachments": true,
      "attachment_count": 2,
      "body_preview": "Hi all, calibration results for batch 0412 are attached..."
    }
  ]
}
```

---

### 3.4. Message Detail
`GET /api/messages/{message_id}`

**Path Parameters:**
- `message_id` (integer, required): Unique message ID from database.

**Response (`200 OK`):**
```json
{
  "id": 1024,
  "folder_id": 4,
  "folder_path": "Top of Personal Folders/Projects",
  "subject": "FW: Saltmarsh rig calibration - batch 0412",
  "sender_name": "THORNBURY,ALEX (Field Engineering)",
  "sender_email": "alex.thornbury@larkspur.example",
  "date_iso": "2001-03-19T09:42:10",
  "recipients": [
    {
      "name": "Rourke, Dana",
      "email": "dana.rourke@marrowbay.example",
      "type": "To"
    }
  ],
  "body_plain": "Hi all,\n\nPlease find attached the report for this week.\n\nRegards,\nAlex",
  "body_html": null,
  "attachments": [
    {
      "id": 88,
      "attachment_index": 0,
      "filename": "torque readings 2003-08.csv",
      "size_bytes": 23040,
      "size_formatted": "22.5 KB",
      "mime_type": "application/vnd.ms-excel",
      "download_url": "/api/messages/1024/attachments/88?download=1",
      "view_url": "/api/messages/1024/attachments/88"
    }
  ],
  "transport_headers": "From: THORNBURY,ALEX...\nTo: Rourke, Dana..."
}
```

---

### 3.5. Attachment Streaming & Download
`GET /api/messages/{message_id}/attachments/{attachment_id}`

**Path Parameters:**
- `message_id` (integer, required)
- `attachment_id` (integer, required)

**Query Parameters:**
- `download` (boolean, optional, default `false`): If `true`, sets `Content-Disposition: attachment`. If `false`, sets `Content-Disposition: inline` (enabling browser viewing for images, PDFs, and text).

**Behavior:**
1. Looks up `folder_path`, `message_index`, and `attachment_index`.
2. Reads raw attachment buffer from `PstReader`.
3. Streams bytes with headers:
   - `Content-Type: <mime_type>` (e.g. `image/jpeg`, `application/pdf`)
   - `Content-Disposition: [attachment|inline]; filename="<sanitized_filename>"`
   - `Content-Length: <size_bytes>`

---

### 3.6. Full-Text Search
`GET /api/search`

**Query Parameters:**
- `q` (string, required): Search query string.
- `folder_id` (integer, optional): Scope search to specific folder.
- `has_attachments` (boolean, optional): Restrict to messages with attachments.
- `page` (integer, default `1`)
- `page_size` (integer, default `50`)

**Implementation:**
Queries `messages_fts` virtual table using FTS5:
```sql
SELECT
    m.id, m.folder_id, m.folder_path, m.subject, m.sender_name, m.date_iso,
    m.has_attachments, m.attachment_count,
    snippet(messages_fts, 4, '<mark>', '</mark>', '...', 24) AS snippet
FROM messages_fts f
JOIN messages m ON f.message_id = m.id
WHERE messages_fts MATCH :query
ORDER BY rank
LIMIT :limit OFFSET :offset;
```

**Response (`200 OK`):**
```json
{
  "query": "Saltmarsh",
  "total": 42,
  "page": 1,
  "page_size": 50,
  "results": [
    {
      "id": 512,
      "subject": "Heron firmware 2.4.1 release",
      "sender_name": "KESTREL,PRIYA (Quality)",
      "date_iso": "2002-05-14T14:22:00",
      "folder_path": "Top of Personal Folders/Projects/Saltmarsh Rig",
      "snippet": "...re-test the <mark>Saltmarsh</mark> rig units after flashing..."
    }
  ]
}
```

---

### 3.7. RFC 822 (.eml) Export
`GET /api/messages/{message_id}/eml`

**Behavior:**
1. Loads message headers, recipients, body, and all attachment buffers.
2. Uses standard library `email.message.EmailMessage` to build a MIME multipart message.
3. Returns `Content-Type: message/rfc822` with `Content-Disposition: attachment; filename="message_<id>.eml"`.
4. Compatible with Thunderbird, Apple Mail, Gmail import, and any standard email client.

---

## 4. Verification & Automated Test Suite

Create `tests/test_api.py` using `pytest` and `httpx`:
```bash
uv run pytest tests/test_api.py -v
```

Required test assertions:
- `GET /api/archive/info` returns 200 with 4,101 total messages.
- `GET /api/folders` returns valid JSON tree with root node.
- `GET /api/folders/4/messages` returns list with pagination metadata.
- `GET /api/messages/1` returns valid subject and sender.
- `GET /api/messages/{id}/attachments/{att_id}` returns binary bytes with correct MIME type.
- `GET /api/search?q=Saltmarsh` returns search results with snippets.
- `GET /api/messages/1/eml` returns valid RFC 822 format bytes.
