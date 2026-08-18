"""
Reads the club's LTC / event instructor sheet (who is teaching what).

Nothing to do with sheets of ICE, despite the name collision that is
unavoidable in a curling club: see ice.py for those.

The Google Sheet is the ONLY source of truth: there is no local database, no
mirror, no cache file. Every run re-reads the sheet. The sheet is published for
link access, so the CSV export endpoint works with no credentials at all, which
is why this needs no Google API key, no service account and no OAuth dance:

    https://docs.google.com/spreadsheets/d/<id>/export?format=csv

Sheet columns (as maintained by hand):

    Type of Event | Date | Day of Week | Time | # of Attendees | Instructor1..N

Everything in this module except `fetch_csv` is pure, so the parsing and the
"is this event short of instructors" arithmetic are unit-testable offline.
"""

from __future__ import annotations

import csv
import io
import logging

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import aiohttp

from ice import sheets_for_people

log = logging.getLogger(__name__)

SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_GID = os.environ.get("SHEET_GID", "")          # optional: a specific tab
TIMEOUT = aiohttp.ClientTimeout(total=20)

# Staffing comes from SHEETS OF ICE, not from a headcount ratio: ice.py turns
# attendees into sheets (the same call /sheets uses for an LTC), and then two
# instructors per sheet is the target while the club can stretch below it
# ("3 instructors for 2 sheets, or 3 across 3"). So each event has two
# thresholds rather than one number:
#
#     ideal   = 2 per sheet   <- what the board asks toward
#     minimum = 1 per sheet   <- below this the event is genuinely stuck
#
# Between the two it's workable but short, which is where those stretch cases
# land. An "Instructors Needed" column in the sheet, if one is ever added,
# overrides the ideal for that event.
INSTRUCTORS_PER_SHEET = int(os.environ.get("INSTRUCTORS_PER_SHEET", "2"))
MIN_INSTRUCTORS_PER_SHEET = int(os.environ.get("MIN_INSTRUCTORS_PER_SHEET", "1"))
# How far ahead to look.
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "60"))

# Per-type footnotes. CPATH events don't earn club credit, which is worth saying
# every time so nobody signs up expecting it.
TYPE_NOTES = {"cpath": "no club credit for this one"}

# A name like "Lisa Calder (if needed)" is a maybe, not a commitment. We list
# them but don't count them as filling a slot, otherwise a tentative name hides
# a real shortfall.
_TENTATIVE_RE = re.compile(r"\(.*?\)")

# Strict: an instructor slot is "Instructor", "Instructor1", "Instructor 10".
# A loose startswith("instructor") would swallow "Instructors Needed" and count
# the target column itself as a person.
_INSTRUCTOR_COL_RE = re.compile(r"^instructors?\s*\d*$")

_HEADER_ALIASES = {
    "type of event": "type",
    "date": "date",
    "day of week": "weekday",
    "time": "time",
    "# of attendees": "attendees",
    "attendees": "attendees",
    "instructors needed": "needed",     # optional override column
    "# of instructors needed": "needed",
}


@dataclass
class Event:
    type: str
    date: date
    time: str
    attendees: int | None
    instructors: list[str] = field(default_factory=list)   # committed
    tentative: list[str] = field(default_factory=list)     # "(if needed)" etc.
    needed_override: int | None = None

    @property
    def filled(self) -> int:
        return len(self.instructors)

    @property
    def sheets(self) -> int | None:
        """Sheets of ice this event will use, by the same rule /sheets uses for
        LTCs: one sheet per PEOPLE_PER_SHEET attendees, capped at the facility's
        sheet count. None when the sheet has no attendee count (CPATH events)."""
        if self.attendees is None:
            return None
        return sheets_for_people(self.attendees)

    @property
    def needed(self) -> int | None:
        """Instructors wanted in total: two per sheet. None when we have no
        basis to say, which is different from "it needs nobody"."""
        if self.needed_override is not None:
            return self.needed_override
        s = self.sheets
        return None if s is None else s * INSTRUCTORS_PER_SHEET

    @property
    def minimum(self) -> int | None:
        """The stretch floor: one instructor per sheet. Below this the event
        can't really run as intended."""
        s = self.sheets
        return None if s is None else s * MIN_INSTRUCTORS_PER_SHEET

    @property
    def short_by(self) -> int:
        """How many below the ideal. Zero once the event is fully staffed."""
        n = self.needed
        return 0 if n is None else max(0, n - self.filled)

    @property
    def critical(self) -> bool:
        """Short of even the stretch floor."""
        m = self.minimum
        return m is not None and self.filled < m

    @property
    def note(self) -> str:
        return TYPE_NOTES.get(self.type.strip().casefold(), "")

    @property
    def key(self) -> tuple:
        """Identity for comparing one read of the sheet with another."""
        return (self.date, self.time, self.type, self.attendees)


def csv_url(sheet_id: str = "", gid: str = "") -> str:
    sheet_id = sheet_id or SHEET_ID
    if not sheet_id:
        raise RuntimeError("SHEET_ID is not set")
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    gid = gid or SHEET_GID
    return f"{url}&gid={gid}" if gid else url


def edit_url(sheet_id: str = "") -> str:
    """Human-facing link to the sheet, for the board's footer."""
    sheet_id = sheet_id or SHEET_ID
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else ""


async def fetch_csv(session: aiohttp.ClientSession | None = None) -> str:
    """GET the sheet as CSV. Google 302s to a googleusercontent host; aiohttp
    follows that for us."""
    own = session is None
    session = session or aiohttp.ClientSession(timeout=TIMEOUT)
    try:
        async with session.get(csv_url()) as r:
            body = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"sheet fetch failed: HTTP {r.status}")
    finally:
        if own:
            await session.close()
    if body.lstrip().lower().startswith(("<!doctype", "<html")):
        # Sharing was turned off, or the id is wrong: Google hands back a
        # sign-in page with a 200. Say so rather than parsing HTML as CSV.
        raise RuntimeError(
            "sheet fetch returned an HTML page, not CSV. Is link sharing still on?")
    return body


def _parse_date(text: str, *, today: date | None = None) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_int(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_events(csv_text: str, *, today: date | None = None,
                 horizon_days: int = HORIZON_DAYS) -> list[Event]:
    """Every upcoming event in the sheet, soonest first.

    Tolerates what a hand-maintained sheet actually contains: blank spacer rows
    in the middle (they are NOT the end of the data), ragged row lengths, and
    columns being reordered or renamed slightly.
    """
    today = today or date.today()
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return []

    header = [h.strip().casefold() for h in rows[0]]
    cols: dict[str, int] = {}
    instructor_cols: list[int] = []
    for i, h in enumerate(header):
        # Named columns win over the instructor pattern, so a header that reads
        # like both is never counted as a person.
        if h in _HEADER_ALIASES:
            cols.setdefault(_HEADER_ALIASES[h], i)
        elif _INSTRUCTOR_COL_RE.match(h):
            instructor_cols.append(i)

    if "date" not in cols:
        raise RuntimeError(f"sheet has no Date column; found headers: {rows[0]!r}")

    def cell(row: list[str], name: str) -> str:
        i = cols.get(name)
        return (row[i].strip() if i is not None and i < len(row) else "")

    out: list[Event] = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue                              # blank spacer row, keep going
        when = _parse_date(cell(row, "date"), today=today)
        if when is None:
            continue
        if when < today or (when - today).days > horizon_days:
            continue

        committed, tentative = [], []
        for i in instructor_cols:
            name = (row[i].strip() if i < len(row) else "")
            if not name:
                continue
            if _TENTATIVE_RE.search(name):
                tentative.append(_TENTATIVE_RE.sub("", name).strip())
            else:
                committed.append(name)

        out.append(Event(
            type=cell(row, "type") or "Event",
            date=when,
            time=cell(row, "time"),
            attendees=_parse_int(cell(row, "attendees")),
            instructors=committed,
            tentative=tentative,
            needed_override=_parse_int(cell(row, "needed")),
        ))

    out.sort(key=lambda e: (e.date, e.time))
    return out


async def upcoming_events(*, today: date | None = None) -> list[Event]:
    return parse_events(await fetch_csv(), today=today)
