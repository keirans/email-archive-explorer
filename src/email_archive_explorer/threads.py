"""Conversation detection (Spec 10 §2).

Messages are linked by *strong* signals first, then loose replies are attached
by a guarded subject rule:

1. ``In-Reply-To`` / ``References`` pointing at another message's ``Message-ID``
   (and duplicate copies sharing a ``Message-ID``).
2. Outlook's ``PR_CONVERSATION_INDEX``: its first 22 bytes are shared by every
   message in a conversation.
3. Subject fallback, only for messages the strong signals left alone: a message
   whose subject carries a reply/forward prefix joins the nearest earlier
   message (or failing that, a later one) with the same *normalised* subject
   within :data:`SUBJECT_GAP_DAYS`. Generic subjects never group, and a
   subject fallback never merges two strong conversations together.

The module is pure (no database access) so it can be unit-tested and measured
against an existing index without re-indexing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

#: Maximum gap between a reply and the message it is attached to by subject.
SUBJECT_GAP_DAYS = 60

_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|sv|wg|vs|antw|tr|rif|ref|recall)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]|\[[^\]]{1,40}\])\s*",
    re.IGNORECASE,
)
_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]{1,40}\]\s*)*(?:re|fw|fwd|aw|sv|wg|vs|antw|tr|rif|ref)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]",
    re.IGNORECASE,
)
_MSGID_RE = re.compile(r"<([^<>\s]+)>")

#: Subjects too vague to group on, after normalisation.
GENERIC_SUBJECTS = frozenset({
    "(no subject)", "no subject", "hi", "hello", "hey", "hi there", "test", "testing",
    "meeting", "meetings", "update", "updates", "fyi", "question", "questions",
    "quick question", "thanks", "thank you", "info", "information", "help", "lunch",
    "request", "reminder", "notice", "note", "notes", "message", "email", "mail",
    "document", "documents", "file", "files", "attachment", "attachments", "report",
    "status", "draft", "ok", "yes", "no", "urgent", "important", "follow up",
    "follow-up", "followup", "catch up",
})
MIN_SUBJECT_LEN = 4


def normalise_subject(subject: Optional[str]) -> str:
    """Strip every leading reply/forward/list prefix, collapse spaces, lower-case."""
    s = (subject or "").strip()
    while True:
        new = _PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return " ".join(s.split()).lower()


def is_reply_subject(subject: Optional[str]) -> bool:
    return bool(_REPLY_PREFIX_RE.match(subject or ""))


def is_generic_subject(norm: str) -> bool:
    return len(norm) < MIN_SUBJECT_LEN or norm in GENERIC_SUBJECTS


def parse_msgid(value: Optional[str]) -> Optional[str]:
    """``<Abc@Host>`` → ``abc@host``; bare ids are accepted too."""
    if not value:
        return None
    m = _MSGID_RE.search(value)
    v = (m.group(1) if m else value).strip().strip("<>").lower()
    return v or None


def parse_msgid_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    found = [x.lower() for x in _MSGID_RE.findall(value)]
    return found or ([v] if (v := parse_msgid(value)) else [])


def conversation_key(index: Optional[bytes]) -> Optional[str]:
    """Hex of the 22-byte conversation-index header, or None when unusable."""
    if not index or len(index) < 22:
        return None
    head = bytes(index[:22])
    if not any(head[6:22]):           # zero GUID: not a real conversation id
        return None
    return head.hex()


@dataclass
class ThreadSignals:
    """What the indexer knows about one message for threading."""

    id: int
    subject: str = ""
    date_iso: Optional[str] = None
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Sequence[str] = field(default_factory=tuple)
    conversation_key: Optional[str] = None
    topic: Optional[str] = None


@dataclass
class Thread:
    id: int                      # root message id (earliest)
    members: list[int]           # ordered by date, then id
    method: str                  # 'headers' | 'conversation-index' | 'subject' | 'single'
    first_date: Optional[str]
    last_date: Optional[str]
    subject: str


class _DSU:
    def __init__(self, ids: Iterable[int]):
        self.parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        return True


def _ts(date_iso: Optional[str]) -> Optional[float]:
    if not date_iso:
        return None
    try:
        dt = datetime.fromisoformat(date_iso)
    except ValueError:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


_METHOD_RANK = {"headers": 3, "conversation-index": 2, "subject": 1}


def compute_threads(messages: Sequence[ThreadSignals], gap_days: int = SUBJECT_GAP_DAYS) -> list[Thread]:
    """Group messages into conversations. Every message appears in exactly one thread."""
    by_id = {m.id: m for m in messages}
    dsu = _DSU(by_id)
    method_of: dict[int, str] = {}  # root -> strongest link method used

    def link(a: int, b: int, method: str) -> None:
        ra, rb = dsu.find(a), dsu.find(b)
        if ra == rb:
            return
        best = max((method_of.pop(ra, ""), method_of.pop(rb, ""), method),
                   key=lambda x: _METHOD_RANK.get(x, 0))
        dsu.union(ra, rb)
        method_of[dsu.find(ra)] = best

    # 1. Headers: duplicate copies share a Message-ID; replies point at parents
    owner: dict[str, int] = {}
    for m in messages:
        if m.message_id:
            if m.message_id in owner:
                link(owner[m.message_id], m.id, "headers")
            else:
                owner[m.message_id] = m.id
    for m in messages:
        parents = list(m.references or ())
        if m.in_reply_to:
            parents.append(m.in_reply_to)
        for ref in parents:
            target = owner.get(ref)
            if target is not None:
                link(m.id, target, "headers")

    # 2. Outlook conversation index
    conv_owner: dict[str, int] = {}
    for m in messages:
        if m.conversation_key:
            if m.conversation_key in conv_owner:
                link(conv_owner[m.conversation_key], m.id, "conversation-index")
            else:
                conv_owner[m.conversation_key] = m.id

    # Loose = left alone by the strong signals
    linked = {dsu.find(mid) for mid in by_id if dsu.find(mid) != mid} | set(method_of)
    loose = {mid for mid in by_id if dsu.find(mid) == mid and mid not in linked}

    # 3. Guarded subject fallback: a loose *reply* joins the nearest earlier
    # message with the same normalised subject (else the next later one) within
    # the gap. Each loose message links at most once, so two existing
    # conversations can never be bridged by subject.
    gap = gap_days * 86400.0
    buckets: dict[str, list[tuple]] = {}
    for m in messages:
        norm = normalise_subject(m.topic or m.subject)
        if is_generic_subject(norm):
            continue
        ts = _ts(m.date_iso)
        if ts is None:
            continue
        buckets.setdefault(norm, []).append((ts, m.id))
    for items in buckets.values():
        if len(items) < 2:
            continue
        items.sort()
        for k, (ts, mid) in enumerate(items):
            if mid not in loose or not is_reply_subject(by_id[mid].subject):
                continue
            target = None
            if k > 0 and ts - items[k - 1][0] <= gap:
                target = items[k - 1][1]
            elif k + 1 < len(items) and items[k + 1][0] - ts <= gap:
                nxt = items[k + 1][1]
                # only pull forward onto another loose message, never into an
                # existing conversation (that would let one reply merge two)
                if nxt in loose:
                    target = nxt
            if target is not None:
                link(mid, target, "subject")
                loose.discard(mid)

    # Collect threads
    groups: dict[int, list[int]] = {}
    for mid in by_id:
        groups.setdefault(dsu.find(mid), []).append(mid)

    def order_key(mid: int):
        d = by_id[mid].date_iso
        return (d is None, d or "", mid)

    threads: list[Thread] = []
    for root, members in groups.items():
        members.sort(key=order_key)
        dates = [by_id[x].date_iso for x in members if by_id[x].date_iso]
        first = members[0]
        threads.append(Thread(
            id=first,
            members=members,
            method=method_of.get(root, "subject") if len(members) > 1 else "single",
            first_date=min(dates) if dates else None,
            last_date=max(dates) if dates else None,
            subject=by_id[first].subject or "",
        ))
    return threads
