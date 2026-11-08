"""Fictional demo archive generator (Spec 11 §3).

Builds mail for an invented company, *Larkspur Instruments*, between 2001 and
2006, so the app can be tried, tested and screenshotted without anyone's real
mail. Everything is made up: people, companies, projects and files. All
addresses use reserved ``.example`` domains (RFC 2606).

Output (deterministic for a given seed, standard library only)::

    <out>/larkspur-mail/          EML folder tree (Inbox, Sent Items, Projects/…)
    <out>/larkspur-newsletters.mbox

Run ``email-archive demo`` (default ``data/demo``) and then
``email-archive serve data/demo/larkspur-mail``.
"""

from __future__ import annotations

import io
import random
import struct
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Optional

DOMAIN = "larkspur.example"
EML_DIR = "larkspur-mail"
MBOX_FILE = "larkspur-newsletters.mbox"


# ---------------------------------------------------------------------------
# People (all invented)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Person:
    name: str
    email: str
    title: str = ""

    @property
    def first(self) -> str:
        return self.name.split()[0]

    def addr(self) -> str:
        return f"{self.name} <{self.email}>"


ME = Person("Alex Thornbury", f"alex.thornbury@{DOMAIN}", "Field Engineering Lead")
PRIYA = Person("Priya Kestrel", f"priya.kestrel@{DOMAIN}", "Quality Manager")
SAM = Person("Sam Whitlow", f"sam.whitlow@{DOMAIN}", "Firmware Engineer")
JORDAN = Person("Jordan Ashgrove", f"jordan.ashgrove@{DOMAIN}", "Sales Director")
MORGAN = Person("Morgan Fenwick", f"morgan.fenwick@{DOMAIN}", "Managing Director")
CASEY = Person("Casey Larch", f"casey.larch@{DOMAIN}", "IT & Office Manager")
DANA = Person("Dana Rourke", "dana.rourke@marrowbay.example", "Marrow Bay Test Labs")
LEE = Person("Lee Corrigan", "lee.corrigan@quillfield.example", "Quillfield Components")
SOCIAL = Person("Larkspur Social Club", f"social@{DOMAIN}")
SUPPLIER_NEWS = Person("Quillfield Components News", "news@quillfield.example")
TEAM = [PRIYA, SAM, JORDAN, MORGAN, CASEY]


# ---------------------------------------------------------------------------
# Tiny file-format writers (valid PNG, GIF, PDF, ZIP)
# ---------------------------------------------------------------------------

def _png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixel(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def logo_png() -> bytes:
    """Small signature logo: a larkspur-blue bar with three 'petals'."""
    def px(x, y):
        if 6 <= y <= 33 and any((x - cx) ** 2 + (y - 20) ** 2 < 70 for cx in (14, 26, 38)):
            return (88, 86, 214)
        if 48 <= x <= 116 and 16 <= y <= 24:
            return (60, 64, 92)
        return (255, 255, 255)
    return _png(120, 40, px)


def chart_png(seed: int, bars: int = 12, title_hue: int = 0) -> bytes:
    """A bar chart of made-up readings (with a tolerance band)."""
    rnd = random.Random(seed)
    w, h = 480, 280
    values = [rnd.uniform(0.25, 0.9) for _ in range(bars)]
    colours = [(37, 99, 235), (13, 148, 136), (217, 119, 6)]
    bar_col = colours[title_hue % len(colours)]

    def px(x, y):
        if y in (40, 250) or x in (40,):
            return (148, 163, 184)
        if 40 < x < 460 and 40 < y < 250:
            band = 250 - int(0.7 * 210) <= y <= 250 - int(0.55 * 210)
            i = (x - 46) // 34
            if 0 <= i < bars and (x - 46) % 34 < 24 and y >= 250 - int(values[i] * 210):
                return bar_col
            return (236, 253, 245) if band else (255, 255, 255)
        return (255, 255, 255)
    return _png(w, h, px)


def photo_png(seed: int) -> bytes:
    """A procedural 'photo': sky, hills and a lake, different per seed."""
    rnd = random.Random(seed)
    w, h = 640, 420
    sky_top = (rnd.randint(60, 120), rnd.randint(120, 170), rnd.randint(200, 240))
    hill = (rnd.randint(40, 90), rnd.randint(100, 150), rnd.randint(50, 90))
    phase = rnd.uniform(0, 6.28)
    import math

    def px(x, y):
        ridge = 210 + int(40 * math.sin(x / 70 + phase) + 18 * math.sin(x / 23 + phase * 2))
        if y < ridge:
            t = y / ridge
            return tuple(int(c + (250 - c) * t * 0.6) for c in sky_top)
        if y < 320:
            shade = 1 - (y - ridge) / 300
            return tuple(int(c * shade) for c in hill)
        ripple = 8 * math.sin(x / 9 + y / 3)
        return (40 + int(ripple), 90 + int(ripple), 140 + int(ripple))
    return _png(w, h, px)


SPACER_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00"
              b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def pdf_doc(title: str, lines: Sequence[str], pages: int = 1) -> bytes:
    """A small, valid, text-only PDF."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs: list[bytes] = []
    page_ids = []
    n_fixed = 3  # catalog, pages, font
    for p in range(pages):
        content = [f"BT /F1 18 Tf 56 780 Td ({esc(title)}) Tj ET".encode()]
        y = 740
        for line in lines:
            content.append(f"BT /F1 11 Tf 56 {y} Td ({esc(line)}) Tj ET".encode())
            y -= 18
        content.append(f"BT /F1 9 Tf 56 40 Td (Larkspur Instruments - page {p + 1} of {pages}) Tj ET".encode())
        stream = b"\n".join(content)
        page_ids.append(n_fixed + 2 * p + 1)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {n_fixed + 2 * p + 2} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    head = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    all_objs = head + objs
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(all_objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(all_objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(all_objs) + 1, xref))
    return out.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2003, 9, 1, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


def csv_readings(seed: int, rows: int = 400) -> bytes:
    rnd = random.Random(seed)
    lines = ["timestamp,channel,torque_nm,temp_c,drift_ppm"]
    t = datetime(2003, 8, 4, 8, 0)
    for i in range(rows):
        lines.append(f"{(t + timedelta(minutes=5 * i)).isoformat()},{i % 4 + 1},"
                     f"{rnd.uniform(11.8, 12.4):.3f},{rnd.uniform(18, 26):.1f},{rnd.gauss(0, 4):.2f}")
    return ("\n".join(lines) + "\n").encode()


def firmware_blob(seed: int, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


# ---------------------------------------------------------------------------
# Attachments catalogue
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    filename: Optional[str]
    data: bytes
    maintype: str
    subtype: str


def _att(name: Optional[str], data: bytes, mime: str) -> Attachment:
    main, sub = mime.split("/")
    return Attachment(name, data, main, sub)


def _files() -> dict[str, Attachment]:
    report_lines = [
        "Rig: Saltmarsh calibration rig, bench 2",
        "Reference: Tidewater TS-40 pressure sensor, serials 0412-0447",
        "Result: 34 of 36 units within +/- 0.05% full scale after warm-up",
        "Drift: mean 2.1 ppm/h over 6 h soak (limit 5 ppm/h)",
        "Torque fixture re-seated after unit 0431; see appendix B",
        "Action: re-test 0433 and 0440 after firmware 2.4.1",
    ]
    spec_lines = [
        "Range: 0-40 bar, accuracy 0.05% FS",
        "Output: 4-20 mA and RS-485 (Modbus RTU)",
        "Housing: 316L stainless, IP68 to 30 m",
        "Operating temperature: -20 to +70 C",
        "Calibration interval: 24 months",
    ]
    trial = {
        f"field-trial/site-{s}/readings-2003-08.csv": csv_readings(100 + i, 900)
        for i, s in enumerate(("estuary-north", "estuary-south", "harbour-wall", "pump-house"))
    }
    trial["field-trial/README.txt"] = b"Tidewater TS-40 field trial, August 2003. One CSV per site, 5-minute samples.\n"
    trial["field-trial/site-map.png"] = photo_png(77)
    return {
        "logo": _att("larkspur-logo.png", logo_png(), "image/png"),
        "spacer": _att("spacer.gif", SPACER_GIF, "image/gif"),
        "cal_report": _att("Saltmarsh rig calibration report Q3-2003.pdf",
                           pdf_doc("Saltmarsh Rig - Calibration Report Q3 2003", report_lines, pages=3), "application/pdf"),
        "spec_sheet": _att("Tidewater TS-40 spec sheet v3.pdf",
                           pdf_doc("Tidewater TS-40 Pressure Sensor", spec_lines), "application/pdf"),
        "launch_deck": _att("Tidewater launch plan.pdf",
                            pdf_doc("Tidewater TS-40 - Launch Plan", [
                                "1. Field trial sign-off (Sept 2003)",
                                "2. Trade show: Coastal Instrumentation Expo",
                                "3. First distributor shipments (Nov 2003)",
                                "4. Pricing: list 1,450 / distributor 1,020",
                            ], pages=6), "application/pdf"),
        "trial_zip": _att("Tidewater field trial data.zip", zip_bytes(trial), "application/zip"),
        "firmware": _att("heron-firmware-2.4.1.zip", zip_bytes({
            "heron-2.4.1/heron.bin": firmware_blob(9, 900_000),
            "heron-2.4.1/RELEASE-NOTES.txt": b"Heron firmware 2.4.1\n- Fix drift compensation at low temperature\n- Faster Modbus scan\n",
            "heron-2.4.1/flash-tool.cfg": b"port=COM1\nbaud=115200\nverify=yes\n",
        }), "application/zip"),
        "torque_csv": _att("torque readings 2003-08.csv", csv_readings(5), "text/csv"),
        "drift_chart": _att("drift chart week 32.png", chart_png(32, title_hue=0), "image/png"),
        "yield_chart": _att("production yield 2004.png", chart_png(2004, bars=12, title_hue=1), "image/png"),
        "rig_photo": _att("Saltmarsh rig - bench setup.png", photo_png(3), "image/png"),
        "picnic1": _att("picnic-2004-01.png", photo_png(11), "image/png"),
        "picnic2": _att("picnic-2004-02.png", photo_png(12), "image/png"),
        "quote": _att("Quillfield quote Q-5531.pdf",
                      pdf_doc("Quotation Q-5531", ["500 x TS-40 diaphragm assemblies", "Unit price 38.20",
                                                   "Lead time 6 weeks", "Valid 30 days"]), "application/pdf"),
        "minutes": _att("board minutes 2005-03.txt",
                        b"Larkspur Instruments - board meeting, March 2005\n\n1. Tidewater sales ahead of plan.\n"
                        b"2. Second calibration rig approved.\n3. Office move to Unit 4 confirmed for July.\n", "text/plain"),
        "unnamed": _att(None, csv_readings(77, 40), "application/vnd.ms-excel"),
        "rotation": _att("on-call rota 2005.csv",
                         b"week,primary,backup\n1,Sam,Alex\n2,Alex,Priya\n3,Priya,Sam\n4,Sam,Alex\n", "text/csv"),
    }


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@dataclass
class Msg:
    sender: Person
    to: list[Person]
    subject: str
    date: datetime
    body: str
    folder: str
    cc: list[Person] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    html: Optional[str] = None
    msgid: str = ""
    in_reply_to: Optional[str] = None
    references: list[str] = field(default_factory=list)


SIGNATURE = {
    p.email: f"\n\n--\n{p.name}\n{p.title}, Larkspur Instruments" for p in [ME, PRIYA, SAM, JORDAN, MORGAN, CASEY]
}

PHRASES = {
    "rig": [
        "The Saltmarsh rig held calibration overnight; drift stayed under 3 ppm/h.",
        "Bench 2 torque fixture needs re-seating before the next batch.",
        "I've re-run the warm-up soak with the new reference gauge.",
        "Two units failed the 0.05% check at the top of the range.",
        "Can we get the rig booked for Thursday afternoon?",
        "The calibration report is attached; appendix B covers the fixture issue.",
        "Temperature in the lab swung 4 degrees, which explains the scatter.",
    ],
    "sensor": [
        "Field trial units are reporting every five minutes from all four sites.",
        "The harbour-wall site lost power for six hours on Tuesday.",
        "Customer feedback on the TS-40 housing has been positive.",
        "We need the RS-485 noise issue closed before the launch.",
        "Spec sheet v3 includes the new IP68 rating.",
        "Distributor pricing is in the launch plan.",
    ],
    "firmware": [
        "Heron 2.4.1 fixes the low-temperature drift compensation.",
        "Flashing takes about 40 seconds per unit with the new tool.",
        "I can't reproduce the Modbus timeout on the bench.",
        "Release notes are in the zip.",
    ],
    "office": [
        "Please make sure laptops are on the docking stations before Friday.",
        "The server will be down from 6pm for backups.",
        "Reminder that timesheets are due by Monday.",
        "The office move to Unit 4 is confirmed for July.",
    ],
}


class _Builder:
    def __init__(self, seed: int):
        self.rnd = random.Random(seed)
        self.msgs: list[Msg] = []
        self.files = _files()
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"{self._n:05d}.{self.rnd.randrange(16**8):08x}@{DOMAIN}"

    def body(self, topic: str, greeting: str, sender: Person, n: int = 3) -> str:
        lines = self.rnd.sample(PHRASES[topic], k=min(n, len(PHRASES[topic])))
        return f"Hi {greeting},\n\n" + "\n\n".join(lines) + SIGNATURE.get(sender.email, f"\n\n{sender.name}")

    def folder_for(self, sender: Person, project: Optional[str]) -> str:
        if sender == ME:
            return "Sent Items"
        return project or "Inbox"

    def thread(self, subject: str, people: list[Person], start: datetime, replies: int, topic: str,
               project: Optional[str] = None, attach: Optional[dict[int, list[str]]] = None,
               gap_hours: tuple[int, int] = (1, 30), forward_at: Optional[int] = None) -> None:
        """A conversation linked by Message-ID / In-Reply-To / References."""
        attach = attach or {}
        refs: list[str] = []
        when = start
        sender = people[0]
        for i in range(replies + 1):
            if i:
                when += timedelta(hours=self.rnd.randint(*gap_hours), minutes=self.rnd.randint(0, 59))
                sender = self.rnd.choice([p for p in people if p != sender])
            recipients = [p for p in people if p != sender]
            prefix = "" if i == 0 else ("FW: " if i == forward_at else "RE: ")
            m = Msg(
                sender=sender, to=recipients[:1], cc=recipients[1:], subject=prefix + subject,
                date=when, body=self.body(topic, recipients[0].first if recipients else "all", sender),
                folder=self.folder_for(sender, project), msgid=self._id(),
                in_reply_to=refs[-1] if refs else None, references=list(refs[-6:]),
                attachments=[self.files[k] for k in attach.get(i, [])],
            )
            if sender == JORDAN:
                m.attachments.append(self.files["logo"])  # signature logo: repeated-image noise
            self.msgs.append(m)
            refs.append(m.msgid)

    def single(self, sender: Person, to: list[Person], subject: str, when: datetime, topic: str,
               folder: Optional[str] = None, attach: Sequence[str] = (), html: Optional[str] = None,
               body: Optional[str] = None) -> None:
        m = Msg(sender=sender, to=to, subject=subject, date=when,
                body=body or self.body(topic, to[0].first if to else "all", sender),
                folder=folder or self.folder_for(sender, None), msgid=self._id(),
                attachments=[self.files[k] for k in attach], html=html)
        if sender == JORDAN:
            m.attachments.append(self.files["logo"])
        self.msgs.append(m)


def _dt(y, mo, d, h=9, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=10)))


def build_messages(seed: int = 7) -> tuple[list[Msg], list[Msg]]:
    """``(eml_messages, mbox_messages)`` for the demo archive."""
    b = _Builder(seed)
    rig, tide, trials = "Projects/Saltmarsh Rig", "Projects/Tidewater Sensor", "Projects/Tidewater Sensor/Field Trials"

    # The long one: a 40-message rig calibration conversation
    b.thread("Saltmarsh rig calibration - batch 0412", [ME, PRIYA, SAM, DANA], _dt(2003, 8, 4), 39, "rig",
             project=rig, attach={0: ["torque_csv"], 7: ["drift_chart"], 18: ["rig_photo"], 39: ["cal_report"]},
             gap_hours=(2, 20))
    b.thread("Tidewater field trial results", [ME, JORDAN, PRIYA, DANA], _dt(2003, 8, 25), 14, "sensor",
             project=trials, attach={3: ["trial_zip"], 9: ["drift_chart"]})
    b.thread("Heron firmware 2.4.1 release", [SAM, ME, PRIYA], _dt(2003, 9, 8), 8, "firmware",
             project=tide, attach={0: ["firmware"]})
    b.thread("Tidewater launch plan", [JORDAN, MORGAN, ME, PRIYA], _dt(2003, 9, 15), 11, "sensor",
             project=tide, attach={0: ["launch_deck"], 6: ["spec_sheet"]})
    b.thread("TS-40 diaphragm supply", [LEE, ME, PRIYA], _dt(2003, 10, 2), 6, "sensor",
             project="Suppliers", attach={0: ["quote"]})
    b.thread("RS-485 noise on long cable runs", [SAM, ME, DANA], _dt(2003, 10, 20), 9, "firmware",
             project=tide, attach={2: ["unnamed"]})
    # The spec sheet gets forwarded around (duplicate attachment, same name and size)
    for i, (who, to) in enumerate([(JORDAN, [MORGAN]), (ME, [DANA]), (PRIYA, [LEE]), (JORDAN, [ME])]):
        b.single(who, to, "FW: Tidewater TS-40 spec sheet v3", _dt(2003, 11, 3 + 4 * i), "sensor",
                 attach=["spec_sheet"])

    # Shorter conversations spread over the years
    topics = [
        ("Bench 2 torque fixture", "rig", rig), ("Reference gauge recertification", "rig", rig),
        ("Lab temperature logging", "rig", rig), ("Second calibration rig", "rig", rig),
        ("TS-40 housing feedback", "sensor", tide), ("Distributor pricing", "sensor", tide),
        ("Modbus timeout on site", "firmware", tide), ("Flash tool update", "firmware", tide),
        ("Estuary north site access", "sensor", trials), ("Pump house logger swap", "sensor", trials),
        ("Laptop refresh", "office", None), ("Office move to Unit 4", "office", None),
        ("Backup tapes", "office", None), ("Expo stand booking", "sensor", None),
    ]
    start = _dt(2001, 3, 5)
    for i, (subject, topic, project) in enumerate(topics * 4):
        when = start + timedelta(days=37 * i + b.rnd.randint(0, 20))
        if when.year > 2006:
            break
        people = [ME] + b.rnd.sample(TEAM + [DANA], k=b.rnd.randint(1, 3))
        b.thread(subject, people, when, b.rnd.randint(1, 6), topic, project=project)

    # Generic one-offs that must NOT be grouped into one conversation
    for i, who in enumerate([CASEY, MORGAN, CASEY, PRIYA, CASEY, SAM]):
        b.single(who, [ME], "Update", _dt(2002 + i % 4, 1 + 2 * i, 10), "office", folder="Admin/IT Notices")
        b.single(who, [ME], "RE: Update" if i % 2 else "Reminder", _dt(2002 + i % 4, 2 + 2 * i, 3), "office",
                 folder="Admin/IT Notices")

    b.single(MORGAN, [ME, PRIYA, SAM, JORDAN, CASEY], "Board minutes March 2005", _dt(2005, 3, 30), "office",
             attach=["minutes"])
    b.single(CASEY, [ME, PRIYA, SAM], "On-call rota 2005", _dt(2005, 1, 4), "office", attach=["rotation"],
             folder="Admin/IT Notices")
    b.single(MORGAN, [ME, PRIYA, SAM, JORDAN, CASEY], "Production yield 2004", _dt(2005, 1, 20), "sensor",
             attach=["yield_chart"])
    b.single(PRIYA, [ME, SAM, JORDAN, MORGAN, CASEY], "Picnic photos!", _dt(2004, 2, 2), "office",
             folder="Social", attach=["picnic1", "picnic2"],
             body="Hi all,\n\nA few photos from Saturday at the lake. Thanks to everyone who came!\n\nPriya")

    eml = b.msgs

    # MBOX: HTML newsletters with spacer images (noise) and a mail-server footer
    nb = _Builder(seed + 1)
    nb.files = b.files
    for i in range(24):
        when = _dt(2002 + i // 6, 1 + (i % 6) * 2, 15)
        who = SOCIAL if i % 2 == 0 else SUPPLIER_NEWS
        title = (f"Social Club news - {when:%B %Y}" if who == SOCIAL else f"Quillfield Components update {when:%m/%Y}")
        items = (["Quiz night winners: Firmware team (again)", "Bike-to-work week starts Monday", "Lost property: one blue umbrella"]
                 if who == SOCIAL else ["New 316L diaphragm line", "Lead times back to 6 weeks", "Holiday shipping dates"])
        html = ("<html><body style='font-family:Arial'><table width='100%'><tr><td>"
                f"<img src='cid:spacer' width='1' height='1'><h2 style='color:#5856d6'>{title}</h2><ul>"
                + "".join(f"<li>{x}</li>" for x in items)
                + "</ul><p style='color:#666;font-size:11px'>You are receiving this because you subscribed.</p></td></tr></table></body></html>")
        nb.single(who, [ME], title, when, "office", folder="Newsletters", attach=["spacer"], html=html,
                  body=title + "\n\n" + "\n".join(f"* {x}" for x in items))
    return eml, nb.msgs


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def to_email(m: Msg) -> EmailMessage:
    e = EmailMessage()
    e["From"] = m.sender.addr()
    e["To"] = ", ".join(p.addr() for p in m.to)
    if m.cc:
        e["Cc"] = ", ".join(p.addr() for p in m.cc)
    e["Subject"] = m.subject
    e["Date"] = format_datetime(m.date)
    e["Message-ID"] = f"<{m.msgid}>"
    if m.in_reply_to:
        e["In-Reply-To"] = f"<{m.in_reply_to}>"
    if m.references:
        e["References"] = " ".join(f"<{r}>" for r in m.references)
    e["X-Mailer"] = "Larkspur Demo Generator"
    e.set_content(m.body)
    if m.html:
        e.add_alternative(m.html, subtype="html")
    for a in m.attachments:
        kwargs = {"filename": a.filename} if a.filename else {}
        if a.maintype == "text":
            e.add_attachment(a.data.decode(), subtype=a.subtype, **kwargs)
        else:
            e.add_attachment(a.data, maintype=a.maintype, subtype=a.subtype, **kwargs)
    # Deterministic MIME boundaries, so the same seed gives byte-identical files
    tag = zlib.crc32(m.msgid.encode())
    for i, part in enumerate(e.walk()):
        if part.is_multipart():
            part.set_boundary(f"=_larkspur_{tag:08x}_{i}")
    return e


def write_demo(out: str | Path = "data/demo", seed: int = 7) -> dict[str, Path]:
    """Write the demo archive; returns the paths of the EML tree and the MBOX."""
    out = Path(out)
    eml_msgs, mbox_msgs = build_messages(seed)
    root = out / EML_DIR
    if root.exists():
        for f in sorted(root.rglob("*.eml")):
            f.unlink()
    counters: dict[str, int] = {}
    for m in sorted(eml_msgs, key=lambda x: x.date):
        folder = root / m.folder
        folder.mkdir(parents=True, exist_ok=True)
        counters[m.folder] = counters.get(m.folder, 0) + 1
        slug = "".join(c if c.isalnum() else "-" for c in m.subject.lower())[:40].strip("-")
        (folder / f"{m.date:%Y%m%d-%H%M}-{counters[m.folder]:03d}-{slug}.eml").write_bytes(to_email(m).as_bytes())
    mbox = out / MBOX_FILE
    with mbox.open("wb") as f:
        for m in sorted(mbox_msgs, key=lambda x: x.date):
            raw = to_email(m).as_bytes().replace(b"\nFrom ", b"\n>From ")
            f.write(f"From {m.sender.email} {m.date:%a %b %d %H:%M:%S %Y}\n".encode())
            f.write(raw.rstrip(b"\n") + b"\n\n")
    return {"eml": root, "mbox": mbox}
