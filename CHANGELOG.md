# Changelog

## 0.1.0

First public release.

- Read-only explorer for Outlook PST/OST, MBOX and EML/Maildir archives, with
  format detection by content.
- Mail view: folder tree, message list, reading pane with sandboxed HTML,
  full-text search (SQLite FTS5) with filters, `.eml` and ZIP export, analytics.
- Conversations detected from reply headers, Outlook's conversation index and a
  guarded subject fallback; a compact conversation bar that never crowds out the
  message body.
- Attachments view for finding "hidden gems": noise filtering, duplicate
  collapsing, Discover lenses, previews, ZIP/TAR listing and origin tracing.
- Multiple archives, a confined file browser, background indexing with progress,
  and an Archive Inspector.
- Local-only security model (Host check, session token, no CORS, CSP sandboxing).
- `email-archive demo`: a fictional demo archive for trying, testing and
  screenshots.
