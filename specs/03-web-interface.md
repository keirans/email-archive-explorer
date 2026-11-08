# Component Spec 03: Web Interface (UI/UX)

## 1. Scope & Purpose

This component defines the frontend user interface for exploring the email archive.

### Architectural Principles
- **Zero Build Step:** Built with pure modern HTML5, CSS3, and ES6+ JavaScript. No Node.js, npm, or bundlers required.
- **Embedded Static Assets:** Served directly by the Python FastAPI backend from `src/email_archive_explorer/static/`.
- **Fast & Responsive:** Sub-10ms UI interactions, virtual rendering or paginated lists, mobile and desktop responsive layout.
- **Classic 3-Pane Desktop Email Client:** Familiar Outlook / Thunderbird / Apple Mail layout.

---

## 2. Layout & Wireframe Architecture

```text
+----------------------------------------------------------------------------------------------------+
|  [Archive Icon] larkspur-mail (374 msgs)        |  [ Search emails... (Ctrl+/) ]  | [Theme] [Help]   |
+----------------------------------------------------------------------------------------------------+
| FOLDERS (11)                | MESSAGES (Saltmarsh Rig - 77)          | READING PANE                |
|                             | Sort: [ Date (Newest) v ] [📎 Only]    |                             |
| v larkspur-mail             |----------------------------------------| Subject: RE: Saltmarsh rig  |
|   > Admin (13)              | Priya Kestrel             12 Aug 2003  | From: Priya Kestrel         |
|   v Projects                | RE: Saltmarsh rig calibration    📎1   | To: Alex Thornbury          |
|       Saltmarsh Rig (77) <- | The Saltmarsh rig held calibration...  | Date: 12 Aug 2003 14:22     |
|     v Tidewater Sensor (65) |----------------------------------------|-----------------------------|
|         Field Trials (29)   | Sam Whitlow               11 Aug 2003  | [📎 drift chart week 32.png]|
|   > Inbox (40)              | RE: Saltmarsh rig calibration          | [⬇ Download .EML]           |
|   > Sent Items (145)        | Bench 2 torque fixture needs re-se...  |-----------------------------|
|   > Social (1)              |----------------------------------------| Hi Alex,                    |
|   > Suppliers (4)           | Dana Rourke               10 Aug 2003  | The Saltmarsh rig held      |
|                             | RE: Saltmarsh rig calibration          | calibration overnight;      |
|                             | I've re-run the warm-up soak with...   | drift stayed under 3 ppm/h. |
|                             |                                        |                             |
+----------------------------------------------------------------------------------------------------+
| Status: Archive read-only | Local engine                             | Page 1 of 2 (77 messages)   |
+----------------------------------------------------------------------------------------------------+
```

---

## 3. Detailed Component Breakdown

### 3.1. Top Navigation & Global Search
- **Archive Identity:** Shows archive name, total message count, and status badge.
- **Search Input:**
  - Real-time search with debouncing (300ms) or enter key trigger.
  - Queries `/api/search?q={query}`.
  - Highlights matched terms inside message snippets.
  - Pressing `Escape` clears search and restores previous folder view.
- **Filter Bar:**
  - Toggle: "Has Attachments" (`📎`).
  - Sort selector: Newest First, Oldest First, Sender (A-Z), Subject (A-Z).
  - Dark / Light mode toggle (persists in `localStorage`, auto-respects `prefers-color-scheme`).

### 3.2. Left Pane: Folder Tree Navigator
- **Hierarchical Tree View:**
  - Nested folders with collapsible arrows (`>` / `v`).
  - Badge on each folder showing exact message count (`34`).
  - Folders with 0 messages styled slightly dimmed.
  - Selecting a folder triggers `/api/folders/{id}/messages` and resets the message list to page 1.
  - Auto-remembers expanded/collapsed state in session storage.

### 3.3. Middle Pane: Message List
- **Folder Header:** Shows selected folder path and count of matching messages.
- **Message Cards:**
  - Sender display name (prominent).
  - Date (compact formatted: `14 May 2002`).
  - Subject line (single-line truncated with ellipsis).
  - 2-line snippet / preview text.
  - Attachment badge with paperclip (`📎 2`) if attachments exist.
  - Active selection highlight (distinct accent border/background).
- **Pagination Controls:**
  - Compact pager at footer: `[Prev]` `Page X of Y` `[Next]`.

### 3.4. Right Pane: Reading & Attachment Viewer
- **Empty State:** Clean illustration with "Select a message from the list to view its contents".
- **Message Header Box:**
  - Full email subject in large heading.
  - Sender chip with email address copy-to-clipboard button.
  - Recipient chips (`To:`, `Cc:`).
  - Formatted RFC timestamp.
  - **Export Action:** `[⬇ Export .EML]` button (triggers `/api/messages/{id}/eml`).
- **Attachment Tray:**
  - Visually distinct section whenever attachments are present.
  - Displays each attachment as an interactive card:
    - File icon according to extension (PDF, Excel, Word, Image, Archive, Text).
    - File name and formatted size (e.g. `23.0 KB`).
    - Actions: `[View]` (opens inline preview for images/PDFs) and `[Download]` (triggers direct file download).
- **Message Body View:**
  - Clean typographic container with comfortable font size and line height.
  - Automatic URL linkification (`http://...` converted to safe clickable links opening in new tab).
  - Preserves whitespace and ASCII formatting for system logs, trace dumps, and shell outputs.
  - Quote blocks (replies/forwards) styled with subtle left border.
- **Image Lightbox Modal:**
  - Clicking any image attachment (`.jpg`, `.gif`, `.png`, `.bmp`) opens full-screen overlay for instant viewing without leaving the app.

---

## 4. Keyboard Shortcuts

| Key | Action |
|---|---|
| `j` or `ArrowDown` | Move to next message in list |
| `k` or `ArrowUp` | Move to previous message in list |
| `/` or `Ctrl+K` | Focus global search bar |
| `Esc` | Clear search / close preview modals |
| `e` | Export currently selected message as `.eml` |
| `[` and `]` | Navigate previous/next page of messages |

---

## 5. File Structure for Static Assets

```text
src/email_archive_explorer/static/
├── index.html       # Single-page HTML skeleton
├── css/
│   ├── app.css      # Core styles, modern dark/light variables, 3-pane flex layout
│   └── tree.css     # Collapsible tree styling and animations
└── js/
    ├── app.js       # Main controller, state management, router
    ├── api.js       # Fetch client wrapping backend REST endpoints
    ├── tree.js      # Folder tree rendering and collapse state
    └── viewer.js    # Message rendering, linkification, and attachment preview
```

---

## 6. Verification & Acceptance Criteria

1. Loading `http://localhost:8000` renders the complete 3-pane layout without browser console errors.
2. Clicking any folder in the left tree immediately loads its messages in the middle pane in < 50ms.
3. Clicking an email in the middle pane renders subject, sender, recipients, body, and attachment badges in < 20ms.
4. Clicking an attachment download button downloads the exact original binary file.
5. Entering a query in the search bar filters messages in real-time with highlighted text snippets.
