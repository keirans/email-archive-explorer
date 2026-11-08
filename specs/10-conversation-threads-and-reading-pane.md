# Component Spec 10: Conversation Threads & Reading Pane Usability

> **Status: implemented** with the recommended answers to §8: a collapsible bar in the header zone, collapsed by default, a 60-day subject gap, and the compact-header toggle.
>
> | Slice | As built |
> |---|---|
> | 10.1 | `threads.py`: `normalise_subject`, generic stop-list, `compute_threads` (union-find). `tests/test_threads.py` |
> | 10.2 | Signals on `MessageDetail` (excluded from JSON). PST reads `conversation_index`, `conversation_topic` and transport headers (≈0.01 ms per message). `index_version`: PST 3, MBOX 2, EML 2. Rebuilds drop and recreate tables, so old schemas migrate. |
> | 10.3 | `ThreadWindow` from `GET /api/messages/{id}/thread?limit=&offset=`; `thread_size` on list and search rows |
> | 10.4 | `.reading-header` zone (max 40%, scrolls itself); body min 240 px; To/Cc show 8 chips and attachments 6 before `+N more` |
> | 10.5 | Conversation bar with Prev/Next, expand list (6 rows, windowed 50 at a time, ≤ 200 in the DOM), `n`/`p`/`t`/`Shift+H` |
> | 10.6 | List badge (click opens with the list expanded), help dialog, docs, axe audit |
>
> **Differences from the plan:**
> - **Topic and subject merged.** The PST conversation topic simply replaces the subject when grouping, so there is no separate `topic` method.
> - **Subject fallback narrowed.** It only attaches *replies* (messages with a `Re:`/`Fw:` prefix) and never merges two existing conversations. Without these guards, real data still produced 1,963-message "threads" spanning 355 subjects.
> - **Attachments strip.** The "Show in Attachments view" link was left out.
>
> **Results:**
>
> | Check | Before | After |
> |---|---|---|
> | Largest thread, archive A (26,110 messages) | 18,632 | 282 (a genuine single-subject reply-all chain) |
> | Messages in threads of more than 50, archive A | 12,500 | 6,355 in 76 conversations (52 with a single subject; the rest have 2–5 subjects, linked by headers or the same Outlook conversation index) |
> | Largest thread, archive B (4,101 messages) | 1,168 | 70 |
> | Thread computation, 26k messages | — | 0.06 s |
> | Indexing, archive A | 13.1 s | 11.3–13.4 s (within budget) |
>
> - **Layout** (`scripts/reading_pane_check.py`, 1366×768): the body starts at 22–40% of the pane in every worst case, collapsed and expanded, and renders in under 70 ms.
> - **Thread API:** 200 items in 4 ms.
> - **Accessibility:** axe reports 0 violations, including with the conversation expanded, in both themes.

## 1. Scope & Purpose

When a message belongs to a large "conversation", the reading pane renders one pill per message in a wrapping block above the body. With hundreds or thousands of pills, that block fills the whole pane and pushes the message body off the bottom of the window, so the message can't be read.

This spec fixes the problem at both ends:

1. **Backend:** threads are matched wrongly, so they are far bigger than real conversations. This phase replaces the matching with real conversation detection, stored at index time.
2. **Frontend:** the reading pane gets a layout that **always leaves the body readable**, however large the thread, recipient list or attachment list is.

### 1.1. Definitions

| Term | Definition |
|---|---|
| **Conversation (thread)** | A set of messages that reply to or forward each other, identified by `thread_id` (§2.2). |
| **Thread root** | The earliest message in a conversation. |
| **Thread position** | A message's 1-based index in its conversation, ordered by date (`3 of 12`). |
| **Normalised subject** | The subject with every leading reply/forward prefix stripped, repeatedly (`Re:`, `RE[2]:`, `Fw:`, `Fwd:`, `AW:`, `SV:`, `WG:`, `Antw:`, `Recall:`, `[list-tag]`), whitespace collapsed and lower-cased. |
| **Generic subject** | A normalised subject too vague to group on: empty, under 4 characters, or on a short stop-list (`hi`, `hello`, `test`, `meeting`, `update`, `fyi`, `question`, `thanks`, `(no subject)`, …). |
| **Header zone** | Everything in the reading pane above the body: subject, From/To/Cc/Date, conversation bar and attachments strip. |
| **Body zone** | The scrollable message body (`.view-body`). |

### 1.2. What the Real Data Shows

Measured on two local PST archives, using the current `get_message_thread()` logic. These are counts only, with no message content.

| Archive | Messages | Median "thread" | 95th percentile | Largest | Messages whose thread has more than 50 entries |
|---|---|---|---|---|---|
| A | 26,110 | 47 | 1,624 | **18,632** | 12,500 (48%) |
| B | 4,101 | 1 | 19 | 1,168 | 134 |

**Root cause 1: matching.** `get_message_thread()` (`indexer.py:602`) does the following:
- runs `WHERE subject LIKE '%<cleaned>%'`, a **substring** match, so a short subject such as "Update" matches every message whose subject *contains* "update"
- strips only one prefix, so `RE: FW: x` keeps `FW:`
- ignores dates and headers completely

**Root cause 2: layout.**
- `.thread-section` is `flex-shrink: 0`, and `.thread-list` is `flex-wrap: wrap` with no height cap, so 18,632 pills wrap into thousands of rows above the body.
- The same thing can happen with the To/Cc chips (mailing-list messages) and the attachments strip.

**Root cause 3: payload.** `/api/messages/{id}/thread` returns every match with no limit, and the viewer builds all of them in one `innerHTML`.

---

## 2. Backend: Correct Conversation Detection

### 2.1. Signals, in Priority Order

| # | Signal | Source | Applies to |
|---|---|---|---|
| 1 | `In-Reply-To` and `References` headers linked to `Message-ID` | RFC 822 headers | MBOX, EML, and PST when transport headers exist |
| 2 | `PR_CONVERSATION_INDEX`: the first 22 bytes (header block, the same for every message in an Outlook conversation) | `pypff.message.conversation_index` | PST |
| 3 | `PR_CONVERSATION_TOPIC` combined with the time window from row 4 | `pypff.message.conversation_topic` | PST |
| 4 | Normalised-subject **equality** (never substring), only for non-generic subjects, split wherever there is a gap of more than **60 days** between consecutive messages | `messages.subject`, `date_iso` | all |

Messages are linked with a union-find over signals 1 and 2. Messages that the first two signals leave unlinked are then grouped by signal 3 or 4. A message with no usable signal is its own conversation of one.

### 2.2. Index Schema Changes (`indexer.py`)

```sql
ALTER TABLE messages ADD COLUMN message_id_hdr TEXT;   -- normalised <id>, lower-case, no brackets
ALTER TABLE messages ADD COLUMN thread_id INTEGER;     -- = threads.id
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, date_iso);

CREATE TABLE IF NOT EXISTS threads (
    id            INTEGER PRIMARY KEY,   -- = id of the root message
    size          INTEGER NOT NULL,
    first_date    TEXT,
    last_date     TEXT,
    subject       TEXT,                  -- subject of the root
    method        TEXT NOT NULL          -- 'headers' | 'conversation-index' | 'topic' | 'subject' | 'single'
);
```
- Threads are computed in one pass at the end of `build_index()`, inside the existing transaction, by a new module `threads.py` (`compute_threads(conn, signals)`).
- Driver changes:
  - `MessageSummary` and the driver hooks gain optional `message_id_hdr`, `in_reply_to`, `references` and `conversation_key`.
  - The MBOX and EML drivers already parse headers during indexing.
  - The PST driver reads `conversation_index` and `conversation_topic`, which are cheap MAPI properties. **Transport headers are not read during indexing**, because that is costly per message.
- The `References` and `In-Reply-To` lists are kept in a temporary table during the build and are not stored.
- **Bump `index_version`** on all three drivers (PST "2" to "3", MBOX and EML "1" to "2"). The existing cache-validity check then re-indexes old caches automatically on next open, with the progress toast from Spec 08.
- **Performance budget:** index time increases by at most **15%**. Baseline: archive B takes about 5 s, archive A about 13 s. Thread computation for 26k messages must take under 1 s.

### 2.3. API

`GET /api/messages/{id}/thread?limit=50` → `ThreadWindow`:
```json
{
  "thread_id": 812,
  "size": 12,
  "position": 3,
  "method": "headers",
  "first_date": "2001-03-02T09:14:00",
  "last_date": "2001-03-19T16:40:00",
  "offset": 0,
  "items": [ /* MessageSummary × ≤ limit: a window centred on the current message */ ]
}
```
- `limit` is between 1 and 200 (default 50), and `offset` pages through the conversation. Without an `offset`, the window is centred on the requested message.
- `GET /api/messages` and the search results gain `thread_size` per message, so the list can show a small conversation badge.
- Breaking change: the response was previously a bare list. The only client is the bundled UI, which is updated in the same change.

---

## 3. Frontend: Reading Pane Layout

### 3.1. Rule: The Body Is Always Readable

- The reading container becomes a two-row grid: `grid-template-rows: minmax(0, auto) minmax(240px, 1fr)`.
- The **header zone has `max-height: 40%`** of the pane and scrolls internally if its content is taller.
- The **body zone always gets at least 60%** of the pane, and at least 240 px.

### 3.2. Conversation Bar (Replaces the Pills)

**Collapsed (default):** a single line.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 💬 Conversation · 3 of 12 · 2 Mar – 19 Mar 2001     [‹ Prev] [Next ›] [▾] │
└──────────────────────────────────────────────────────────────────────────┘
```

**Expanded:** a vertical list, **capped at 6 rows (about 200 px)**, that scrolls internally, with the current message highlighted and scrolled into view.

```
│ 💬 Conversation · 3 of 12                            [‹ Prev] [Next ›] [▴] │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │   1  Dana R.       2 Mar 2001  Re: project kickoff — notes attached 📎 │ │
│ │   2  Alex P.      2 Mar 2001  Re: project kickoff                     │ │
│ │ ▶ 3  You          4 Mar 2001  Re: project kickoff — updated plan      │ │
│ │   4  …                                                                │ │
│ └───────────────────────────────── Load more (9) ──────────────────────┘ │
```
- Each row shows the position, sender, date, the subject (or a body-preview snippet when the subject is unchanged) and an attachment marker. It is a `role=listbox`, operable by keyboard.
- **Windowed loading:** only the API window (50 messages) is rendered. Scrolling to either end loads the neighbouring window. No message count in a single DOM render exceeds 200.
- The expanded or collapsed state is remembered (`eae-thread-open`). Conversations of 1 show no bar.
- The `method` is shown as a hint. When `subject` is the method, the hint reads "grouped by subject"; when the grouping is less certain, the label reads "Related by subject".

### 3.3. Other Header-Zone Caps

| Element | Collapsed form | Expand |
|---|---|---|
| To / Cc chips | One line; overflow shows `+N more` | Click to show all (inside the 40% cap) |
| Attachments strip | One row of chips plus `+N more` | Click to show all, plus a link to "Show in Attachments view" (Spec 09) |
| Subject | Up to 2 lines, then ellipsis | Full text in the `title` attribute |

There is also a **compact header** toggle (`Shift+H`) that hides the meta grid and leaves only the subject, the sender and the conversation line. The setting is remembered.

### 3.4. Message List Badge

When `thread_size > 1`, the message list shows a small `💬 12` badge on each row. Clicking it opens the message with the conversation bar expanded. Full "group by conversation" in the list is out of scope (§7).

### 3.5. Keyboard

| Key | Action |
|---|---|
| `n` / `p` | Next / previous message in the conversation |
| `t` | Expand / collapse the conversation bar |
| `Shift+H` | Compact header on / off |

`[` and `]` stay as the list-page shortcuts. The `?` help dialog is updated to show the new keys.

---

## 4. Implementation Slices

| Slice | Content |
|---|---|
| 10.1 | `threads.py`: normalisation, the generic stop-list and union-find, with unit tests on synthetic messages |
| 10.2 | Driver signals (headers, PST conversation index and topic), schema and `index_version` bumps, timing check against the budget |
| 10.3 | `ThreadWindow` API, `thread_size` on the list and search endpoints |
| 10.4 | Reading-pane grid and header-zone caps (CSS plus `viewer.js`) |
| 10.5 | Conversation bar: collapsed and expanded states, windowed loading, keyboard |
| 10.6 | List badge, help dialog, docs (`architecture.md`, README), axe re-audit |

## 5. Acceptance Criteria

1. **Correct grouping** (synthetic fixtures):
   - a 5-message `References` chain forms one thread with method `headers`
   - two unrelated messages titled "Update" do **not** group
   - `Re: Fw: Budget` and `Budget` group
   - `Budget` messages 2 years apart do **not** group
2. **Real archives:**
   - the largest thread in archive A falls from 18,632 to **under 300**
   - no subject-method thread contains a generic subject
   - spot-check 10 random multi-message threads by hand: all are real conversations
3. **Layout:** at a 1366×768 viewport, the body zone's top edge sits **within the top 45% of the reading pane** for each of these test messages:
   - a 2,000-message thread (expanded and collapsed)
   - a message with 300 recipients
   - a message with 60 attachments

   This is checked by an automated Playwright script.
4. **Performance:**
   - the thread endpoint returns ≤ 200 items in under 50 ms
   - opening a message in a 2,000-message thread renders in under 100 ms
   - index time stays within budget (§2.2)
5. **No regressions:** all existing tests pass, and axe reports 0 violations on the Mail view with the bar collapsed and expanded, in both themes.
6. **Migration:** an old cache is re-indexed automatically on first open after upgrade, with the progress toast. No manual step is needed.

## 6. Risks

| Risk | Mitigation |
|---|---|
| PST messages often lack transport headers, so `References` is unavailable | Conversation index and topic cover Outlook-to-Outlook mail. The subject method is the last resort, with guards. |
| Some Outlook clients rewrite `PR_CONVERSATION_INDEX` | Union-find merges anyway when headers link the messages. Test on both local archives. |
| The stop-list is English-centric | Keep it as a data constant, and make the length and gap rules language-neutral. |
| The re-index on upgrade takes time on big archives | It is a one-off, uses the existing background job, and browsing resumes afterwards. |

## 7. Out of Scope

- A conversation view in the message list (collapse a thread into one row, Gmail-style)
- A threaded reply tree (indentation by reply depth)
- Quoted-text folding inside message bodies

These are candidates for a later spec once threads are trustworthy.

## 8. Decisions Needed Before Implementation

1. **Conversation bar placement:**
   - **(a, recommended)** a collapsible bar in the header zone, as in §3.2
   - (b) a right-hand rail beside the body when the pane is wider than 1,100 px, falling back to (a)
2. **Default state:**
   - **collapsed always (recommended)**
   - or expanded when the conversation has 8 messages or fewer
3. **Subject-fallback gap:** **60 days (recommended)**, or another value.
4. **Compact-header toggle:** **include (recommended)** or drop.
