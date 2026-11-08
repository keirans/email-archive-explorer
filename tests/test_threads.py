"""Conversation detection (Spec 10 §2)."""

from email.message import EmailMessage

import pytest

from email_archive_explorer.indexer import build_index, get_db_connection, get_message_thread
from email_archive_explorer.threads import (
    ThreadSignals,
    compute_threads,
    conversation_key,
    is_generic_subject,
    is_reply_subject,
    normalise_subject,
    parse_msgid,
    parse_msgid_list,
)


def _groups(threads):
    return sorted(sorted(t.members) for t in threads)


def test_normalise_subject_strips_repeated_prefixes():
    assert normalise_subject("RE: FW: Re[2]:  Budget   2002") == "budget 2002"
    assert normalise_subject("[proj-list] Re: Budget") == "budget"
    assert normalise_subject("AW: SV: Antw: Budget") == "budget"
    assert normalise_subject("Budget") == "budget"
    assert is_reply_subject("Fwd: Budget") and is_reply_subject("[list] RE: x")
    assert not is_reply_subject("Budget re: costs")


def test_generic_subjects():
    assert is_generic_subject(normalise_subject("RE: Update"))
    assert is_generic_subject("") and is_generic_subject("abc")
    assert not is_generic_subject("budget 2002")


def test_msgid_parsing_and_conversation_key():
    assert parse_msgid(" <Abc.123@Host.Example> ") == "abc.123@host.example"
    assert parse_msgid_list("<a@x> <b@x>\n <c@x>") == ["a@x", "b@x", "c@x"]
    assert parse_msgid(None) is None
    assert conversation_key(b"\x01" + b"\x02" * 21 + b"\x03" * 5) == (b"\x01" + b"\x02" * 21).hex()
    assert conversation_key(b"\x01" * 6 + b"\x00" * 16) is None   # zero GUID
    assert conversation_key(b"\x01" * 10) is None


def test_reference_chain_forms_one_thread():
    msgs = [ThreadSignals(id=1, subject="Kickoff", date_iso="2001-03-01T09:00:00", message_id="m1")]
    for i in range(2, 6):
        msgs.append(ThreadSignals(
            id=i, subject="Re: Kickoff", date_iso=f"2001-03-0{i}T09:00:00", message_id=f"m{i}",
            in_reply_to=f"m{i - 1}", references=tuple(f"m{j}" for j in range(1, i)),
        ))
    (t,) = compute_threads(msgs)
    assert t.members == [1, 2, 3, 4, 5] and t.method == "headers" and t.id == 1


def test_generic_subjects_never_group():
    msgs = [
        ThreadSignals(id=1, subject="Update", date_iso="2001-03-01T09:00:00"),
        ThreadSignals(id=2, subject="RE: Update", date_iso="2001-03-02T09:00:00"),
    ]
    assert _groups(compute_threads(msgs)) == [[1], [2]]


def test_subject_fallback_groups_replies_within_gap():
    msgs = [
        ThreadSignals(id=1, subject="Budget", date_iso="2002-01-01T09:00:00"),
        ThreadSignals(id=2, subject="Re: Fw: Budget", date_iso="2002-01-05T09:00:00"),
        ThreadSignals(id=3, subject="RE: Budget", date_iso="2002-01-06T09:00:00"),
    ]
    (t,) = compute_threads(msgs)
    assert t.members == [1, 2, 3] and t.method == "subject"


def test_subject_fallback_respects_gap_and_needs_reply_prefix():
    msgs = [
        ThreadSignals(id=1, subject="Budget", date_iso="2002-01-01T09:00:00"),
        ThreadSignals(id=2, subject="Re: Budget", date_iso="2004-01-01T09:00:00"),  # 2 years later
        ThreadSignals(id=3, subject="Budget", date_iso="2004-01-02T09:00:00"),      # new, not a reply
    ]
    # 2 is too far from 1, so it attaches forward to the loose message 3
    assert _groups(compute_threads(msgs)) == [[1], [2, 3]]


def test_conversation_index_groups_and_subject_never_bridges():
    msgs = [
        # two separate Outlook conversations with the same subject
        ThreadSignals(id=1, subject="Budget", date_iso="2002-01-01T09:00:00", conversation_key="aa"),
        ThreadSignals(id=2, subject="RE: Budget", date_iso="2002-01-02T09:00:00", conversation_key="aa"),
        ThreadSignals(id=3, subject="Budget", date_iso="2002-01-10T09:00:00", conversation_key="bb"),
        ThreadSignals(id=4, subject="RE: Budget", date_iso="2002-01-11T09:00:00", conversation_key="bb"),
        # a loose reply between them attaches to exactly one
        ThreadSignals(id=5, subject="Re: Budget", date_iso="2002-01-03T09:00:00"),
    ]
    threads = compute_threads(msgs)
    assert _groups(threads) == [[1, 2, 5], [3, 4]]
    assert {t.method for t in threads} == {"conversation-index"}


def test_duplicate_message_ids_join():
    msgs = [
        ThreadSignals(id=1, subject="Notes", date_iso="2002-01-01T09:00:00", message_id="x"),
        ThreadSignals(id=2, subject="Notes", date_iso="2002-01-01T09:00:00", message_id="x"),
    ]
    assert _groups(compute_threads(msgs)) == [[1, 2]]


def _eml(subject, msgid, date, irt=None, refs=None):
    m = EmailMessage()
    m["From"] = "Alex Doe <alex@example.com>"
    m["To"] = "Sam Roe <sam@example.com>"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = f"<{msgid}@example.com>"
    if irt:
        m["In-Reply-To"] = f"<{irt}@example.com>"
    if refs:
        m["References"] = " ".join(f"<{r}@example.com>" for r in refs)
    m.set_content(f"Body of {subject}")
    return m.as_bytes()


@pytest.fixture
def eml_archive(tmp_path):
    root = tmp_path / "mail"
    (root / "Inbox").mkdir(parents=True)
    files = [
        _eml("Kickoff", "a", "Thu, 01 Mar 2001 09:00:00 +0000"),
        _eml("Re: Kickoff", "b", "Fri, 02 Mar 2001 09:00:00 +0000", irt="a", refs=["a"]),
        _eml("Re: Kickoff", "c", "Sat, 03 Mar 2001 09:00:00 +0000", irt="b", refs=["a", "b"]),
        _eml("Update", "d", "Sun, 04 Mar 2001 09:00:00 +0000"),
        _eml("RE: Update", "e", "Mon, 05 Mar 2001 09:00:00 +0000"),
    ]
    for i, raw in enumerate(files):
        (root / "Inbox" / f"{i:03d}.eml").write_bytes(raw)
    db = tmp_path / "cache.db"
    build_index(root, db)
    return db


def test_index_stores_threads(eml_archive):
    conn = get_db_connection(eml_archive)
    by_subject = {r["subject"]: r["id"] for r in conn.execute("SELECT id, subject FROM messages")}
    kick = conn.execute("SELECT id FROM messages WHERE subject = 'Kickoff'").fetchone()["id"]
    w = get_message_thread(conn, kick)
    assert w.size == 3 and w.method == "headers" and w.position == 1
    assert [i.subject for i in w.items] == ["Kickoff", "Re: Kickoff", "Re: Kickoff"]
    assert all(i.thread_size == 3 for i in w.items)
    upd = get_message_thread(conn, by_subject["RE: Update"])
    assert upd.size == 1 and upd.method == "single"
    # window centred on the requested message
    last = w.items[-1].id
    w1 = get_message_thread(conn, last, limit=1)
    assert [i.id for i in w1.items] == [last] and w1.position == 3 and w1.offset == 2
    assert get_message_thread(conn, 999999) is None
    conn.close()
