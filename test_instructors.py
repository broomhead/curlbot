"""
Tests for the instructor board. No network, no Google, no Discord gateway.

Run:  python3 test_instructors.py

The fixture mirrors the real sheet's SHAPE exactly (blank spacer rows, ragged
columns, a "(if needed)" name, a CPATH row with no attendee count) because every
parsing bug this thing can have comes from the sheet being hand-maintained. The
names are invented: this repo is public and the real sheet is full of members.
"""
from datetime import date, timedelta as _TD

import ice
import instructor_board as board
import instructor_sheet
from instructor_sheet import Event, parse_events

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


# Pin the sheet id rather than inheriting whatever is in the developer's .env,
# so the footer link and the "no id" guard behave the same for everyone.
instructor_sheet.SHEET_ID = "TEST_SHEET_ID"

CSV = open("fixture_instructor_sheet.csv").read()
TODAY = date(2026, 8, 18)
EVENTS = parse_events(CSV, today=TODAY)


def by_date(d):
    return next(e for e in EVENTS if e.date == date.fromisoformat(d))


# ── Parsing a hand-maintained sheet ──────────────────────────────────────────
check("parse/only upcoming", [e.date.isoformat() for e in EVENTS],
      ["2026-08-25", "2026-08-29", "2026-09-19", "2026-10-17"])
check("parse/blank spacer rows don't end the data",
      by_date("2026-08-25").type, "Private Event")     # sits below two blank rows
check("parse/attendees", by_date("2026-08-25").attendees, 30)
check("parse/time verbatim", by_date("2026-08-25").time, "12:30 - 2:45 pm")
check("parse/instructors", by_date("2026-08-29").instructors, ["Ann Adams"])
check("parse/empty instructor row", by_date("2026-10-17").instructors, [])
check("parse/horizon excludes 11/14", [e for e in EVENTS if e.date.month == 11], [])

# Past events are gone, including one on today's date boundary.
check("parse/today is still upcoming",
      [e.date for e in parse_events(CSV, today=date(2026, 8, 25))][0], date(2026, 8, 25))
check("parse/yesterday is dropped",
      date(2026, 8, 25) in [e.date for e in parse_events(CSV, today=date(2026, 8, 26))], False)

# "Lisa Calder (if needed)" is a maybe. Counting it as filled would hide a real
# shortfall, so it's listed separately and doesn't fill a slot.
tent = parse_events(CSV, today=date(2026, 7, 1))
jul18 = next(e for e in tent if e.date == date(2026, 7, 18))
check("parse/tentative not counted", jul18.filled, 8)
check("parse/tentative listed", jul18.tentative, ["Jo James"])

# Sheets are reordered and renamed by hand; don't be brittle about it.
reordered = "Date,Time,Type of Event,# of Attendees,Instructor1,Instructor2\n" \
            "9/5/26,2 - 4 pm,LTC,16,Ann,Bob\n"
ev = parse_events(reordered, today=TODAY)[0]
check("parse/column order irrelevant", (ev.type, ev.attendees, ev.instructors),
      ("LTC", 16, ["Ann", "Bob"]))
try:
    parse_events("Nope,Nothing\n1,2\n", today=TODAY)
    check("parse/no date column raises", "no error", "RuntimeError")
except RuntimeError as e:
    check("parse/no date column raises", "Date column" in str(e), True)


# ── Staffing: sheets of ice, not a headcount ratio ───────────────────────────
# The sheet count comes from ice.sheets_for_people, the SAME call /sheets uses
# for an LTC, so the report and the board can never disagree about how much ice
# a headcount needs. Two instructors per sheet is the target, one per sheet is
# the floor we can stretch to.
check("staff/uses the shared ice math",
      [ice.sheets_for_people(n) for n in (8, 9, 20, 32, 400)], [1, 2, 3, 4, 4])
def staffing(attendees, filled=0):
    e = Event(type="LTC", date=TODAY, time="", attendees=attendees,
              instructors=[f"P{i}" for i in range(filled)])
    return (e.sheets, e.needed, e.minimum, e.short_by, e.critical)

check("staff/8 attendees is one sheet", staffing(8), (1, 2, 1, 2, True))
check("staff/9 attendees is two sheets", staffing(9), (2, 4, 2, 4, True))
check("staff/20 attendees is three sheets", staffing(20), (3, 6, 3, 6, True))
check("staff/32 attendees is four sheets", staffing(32), (4, 8, 4, 8, True))
check("staff/capped at the facility's sheets", staffing(400)[0], 4)
# Stretch cases: workable but under target, NOT short-handed.
check("staff/3 instructors across 2 sheets", staffing(16, 3), (2, 4, 2, 1, False))
check("staff/3 instructors across 3 sheets", staffing(24, 3), (3, 6, 3, 3, False))
# One under the floor is short-handed.
check("staff/2 across 3 sheets is short-handed", staffing(24, 2)[4], True)
check("staff/at target is covered", staffing(24, 6), (3, 6, 3, 0, False))
check("staff/over target stays covered", staffing(24, 9)[3], 0)

# CPATH events carry no attendee count, so we can't claim a shortfall at all.
cpath = Event(type="CPATH", date=TODAY, time="", attendees=None, instructors=["A"])
check("staff/no attendees means no target", (cpath.sheets, cpath.needed, cpath.minimum),
      (None, None, None))
check("staff/no attendees is never short", (cpath.short_by, cpath.critical), (0, False))
check("staff/cpath note", cpath.note, "no club credit for this one")

# A sheet column, if one is ever added, overrides the computed target.
override = Event(type="LTC", date=TODAY, time="", attendees=32,
                 instructors=["A"], needed_override=3)
check("staff/column overrides the ratio", (override.needed, override.short_by), (3, 2))
check("staff/override reads from the sheet",
      parse_events("Date,# of Attendees,Instructors Needed,Instructor1\n"
                   "9/5/26,32,3,Ann\n", today=TODAY)[0].needed, 3)


# ── The rendered board ───────────────────────────────────────────────────────
# Grouped by URGENCY, not by severity: 8/25 and 8/29 are inside the 14 day
# window and are the only things anyone should be chased about today; 9/19 and
# 10/17 are short too, and can wait.
text = board.render(EVENTS, today=TODAY)
blocks = [b.strip().splitlines() for b in text.split("```")[1::2]]
near_block, later_block = blocks
block = near_block + later_block         # the table lines, both groups
# Rows are the table lines; each is followed by its names on indented lines.
rows = [l for b in blocks for l in b[2:] if not l.startswith(board.NAME_INDENT)]
names = [l for b in blocks for l in b[2:] if l.startswith(board.NAME_INDENT)]
headings = [l for l in text.splitlines() if l.startswith(("🔴", "🟡", "🟢"))]

check("board/two groups, one table each", text.count("```"), 4)
check("board/urgent group first",
      [l.split()[1] for l in near_block[2:] if not l.startswith(board.NAME_INDENT)],
      ["8/25", "8/29"])
check("board/later group after it",
      [l.split()[1] for l in later_block[2:] if not l.startswith(board.NAME_INDENT)],
      ["9/19", "10/17"])
check("board/urgent heading is red",
      headings[0], "🔴  **Needs instructors now (next 14 days)**")
check("board/later heading is amber", headings[1], "🟡  **Coming up later**")
check("board/lights stay out of the code block, where they'd break alignment",
      [l for b in blocks for l in b if any(c in l for c in "🔴🟡🟢")], [])
check("board/header row", near_block[0].split(), ["Date", "Event", "Time", "Have/Need"])
check("board/separator row", set(near_block[1]) <= {"-", " "}, True)
check("board/one row per event", len(rows), len(EVENTS))
check("board/at least one name line per event", len(names) >= len(EVENTS), True)
check("board/chronological within a group",
      [l.split()[1] for l in rows], ["8/25", "8/29", "9/19", "10/17"])
check("board/columns align", len({len(l) - len(l.rsplit("  ", 1)[-1]) for l in near_block[2:]
                                 if not l.startswith(board.NAME_INDENT)}), 1)
check("board/table rows stay narrow", max(len(l) for l in rows) <= 50, True)

# The window itself: 14 days out is still urgent, 15 is not.
edge = [Event(type="LTC", date=TODAY + _TD(days=n), time="2 - 4 pm", attendees=16)
        for n in (14, 15)]
check("board/the boundary day counts as urgent",
      [board.is_urgent(e, TODAY) for e in edge], [True, False])
check("board/a past event on the sheet is urgent, not calm",
      board.is_urgent(Event(type="LTC", date=TODAY - _TD(days=1), time="", attendees=16),
                      TODAY), True)
check("board/one group means one table",
      board.render(edge[:1], today=TODAY).count("```"), 2)
check("board/a quiet fortnight still says so",
      board.render([e for e in EVENTS if e.date.month > 8], today=TODAY).splitlines()[0],
      "**Nothing urgent. 2 later events are still short.**")
check("board/covered urgent group gets a green heading",
      board.render([Event(type="LTC", date=TODAY + _TD(days=3), time="2 - 4 pm",
                          attendees=16, instructors=list("ABCD"))],
                   today=TODAY).splitlines()[2],
      "🟢  **Next 14 days, fully staffed**")

# The four things asked for, on one row, then the names beneath it.
i = next(n for n, l in enumerate(block) if l.startswith("Tue 8/25"))
row, who = block[i], block[i + 1]
check("board/date", row.startswith("Tue 8/25"), True)
check("board/event name, without the noise word", "Private" in row and "Event" not in row, True)
check("board/time", "12:30-2:45 pm" in row, True)
check("board/have vs need", row.split()[-1], "6/8")
# The names sit under their row, wrapped by us at whole names and indented on
# every line: Discord would otherwise put a wrapped continuation flush left,
# where it reads as another table row.
who_lines = []
for l in block[i + 1:]:
    if not l.startswith(board.NAME_INDENT):
        break
    who_lines.append(l)
check("board/names under the row", " ".join(l.strip() for l in who_lines),
      "Ann Adams, Bo Brooks, Cara Cole, Dev Diaz, Eve Ellis, Finn Ford")
check("board/a long name list wraps", len(who_lines) > 1, True)
check("board/every name line is indented",
      all(l.startswith(board.NAME_INDENT) for l in names), True)
check("board/wrapped name lines are no wider than the table",
      max(len(l) for l in names)
      <= max([board.NAME_WRAP] + [len(l) for l in block
                                  if not l.startswith(board.NAME_INDENT)]), True)
check("board/wrapping never splits a name",
      [l for l in board.wrap_names("Ann Adams, Bo Brooks, Cara Cole, Dev Diaz, "
                                   "Eve Ellis, Finn Ford", 44)],
      ["   Ann Adams, Bo Brooks, Cara Cole,", "   Dev Diaz, Eve Ellis, Finn Ford"])
check("board/a short list stays on one line",
      board.wrap_names("Ann Adams", 44), ["   Ann Adams"])
check("board/commas end the line they belong to",
      all(not l.strip().startswith(",") for l in names), True)
check("board/empty roster reads plainly",
      next(l for l in names if "nobody" in l).strip(), "nobody yet")

# No attendee count means no target: show who is in, don't invent a shortfall.
cpath = [Event(type="CPATH", date=date(2026, 9, 5), time="2 - 4 pm", attendees=None,
               instructors=["A", "B"])]
cpath_block = board.render(cpath, today=TODAY).split("```")[1].strip().splitlines()
check("board/no target shows a bare count", cpath_block[2].split()[-1], "2")
check("board/no target is explained", "no target" in board.render(cpath, today=TODAY), True)

# A tentative name is listed with its qualifier, and still not counted.
tent = [Event(type="LTC", date=date(2026, 9, 5), time="2 - 4 pm", attendees=16,
              instructors=["A"], tentative=["B"])]
tent_block = board.render(tent, today=TODAY).split("```")[1].strip().splitlines()
check("board/tentative not counted", tent_block[2].split()[-1], "1/4")
check("board/tentative named with its qualifier", tent_block[3].strip(), "A, B (if needed)")

# Discord rejects an over-long description with a 400, which would mean no board
# at all. A row plus its names runs 120 to 200 characters, so a busy stretch can
# reach the limit; the far end is dropped until it fits.
# Worst realistic case: every event a full LTC with nine long names on it.
many = [Event(type="Private Event", date=date(2026, 9, 1) + _TD(days=2 * i),
              time="12:30 - 2:45 pm", attendees=32,
              instructors=[f"Firstname Lastname{n}" for n in range(9)]) for i in range(60)]
long_text = board.render(many, today=TODAY)
check("board/fits Discord's limit", len(long_text) <= board.DESCRIPTION_LIMIT, True)
check("board/trims only as much as it must",
      len(long_text) > board.DESCRIPTION_LIMIT - 300, True)
check("board/says what it trimmed",
      any(l.startswith("Showing the next ") and "further events are on the sheet." in l
          for l in long_text.splitlines()), True)
check("board/keeps the near events", "Tue 9/1" in long_text, True)
check("board/no trim note when it fits",
      any(l.startswith("Showing the next ") for l in text.splitlines()), False)

# Headline counts the asks; footer links the sheet people actually edit.
check("board/headline counts only the urgent asks", text.splitlines()[0],
      "**2 events in the next 14 days need instructors.**")
check("board/headline when all staffed",
      board.render([Event(type="LTC", date=date(2026, 9, 5), time="2 - 4 pm",
                          attendees=16, instructors=list("ABCD"))],
                   today=TODAY).splitlines()[0],
      "**Every event is fully staffed.**")
check("board/links the sheet",
      "[instructor sheet](https://docs.google.com/spreadsheets/d/TEST_SHEET_ID/edit)" in text,
      True)

# House style: no em dashes or en dashes anywhere in member facing copy.
check("board/no em dash", "—" in text, False)
check("board/no en dash", "–" in text, False)

# Determinism is load-bearing: the text IS the state, so anything time varying
# would make every check look like a change and spam the channel twice a day.
check("board/deterministic",
      board.render(parse_events(CSV, today=TODAY), today=TODAY), text)
check("board/no clock in the output",
      any(w in text.lower() for w in ("as of", "updated", "generated")), False)
check("board/title not in the description", board.BOARD_TITLE in text, False)

check("board/empty sheet says so", "No events" in board.render([], today=TODAY), True)

# Embed colour still signals the worst state, which is not grouping: the rows
# stay in date order either way.
full = [Event(type="LTC", date=date(2026, 9, 5), time="2 - 4 pm", attendees=16,
              instructors=["A", "B", "C", "D"])]
check("board/colour red when something urgent is short",
      board.color(EVENTS, today=TODAY), board.COLOR_SHORT)
check("board/colour green when covered", board.color(full, today=TODAY), board.COLOR_OK)
# The same empty event, near and far: proximity is what makes the bar red, so an
# October LTC with nobody on it is a yellow board, not an emergency.
bare_near = [Event(type="LTC", date=TODAY + _TD(days=5), time="2 - 4 pm", attendees=16)]
bare_far = [Event(type="LTC", date=TODAY + _TD(days=45), time="2 - 4 pm", attendees=16)]
check("board/colour red for a near gap", board.color(bare_near, today=TODAY),
      board.COLOR_SHORT)
check("board/colour amber for the same gap months out",
      board.color(bare_far, today=TODAY), board.COLOR_UNDER)

check("summary/all staffed", board.summary_line(full, today=TODAY),
      "1 upcoming events, all fully staffed")
check("summary/leads with the urgent count", board.summary_line(EVENTS, today=TODAY),
      "4 upcoming events, 4 under target (20 instructor slots to fill), 2 inside 14 days")
check("summary/says when nothing is urgent", board.summary_line(bare_far, today=TODAY),
      "1 upcoming events, 1 under target (4 instructor slots to fill), none inside 14 days")


# ── Fetch guards ─────────────────────────────────────────────────────────────
check("url/basic", instructor_sheet.csv_url("ABC"),
      "https://docs.google.com/spreadsheets/d/ABC/export?format=csv")
check("url/with tab", instructor_sheet.csv_url("ABC", "12345"),
      "https://docs.google.com/spreadsheets/d/ABC/export?format=csv&gid=12345")
_saved, instructor_sheet.SHEET_ID = instructor_sheet.SHEET_ID, ""
try:
    instructor_sheet.csv_url()
    check("url/no id raises", "no error", "RuntimeError")
except RuntimeError:
    check("url/no id raises", True, True)
check("url/no id means no footer link", instructor_sheet.edit_url(), "")
instructor_sheet.SHEET_ID = _saved
check("url/edit link", instructor_sheet.edit_url(),
      "https://docs.google.com/spreadsheets/d/TEST_SHEET_ID/edit")

# The fixture must not carry real member names: this repo is public. Checked by
# SHAPE rather than against a list of the real ones — a denylist of real names in a
# public repo leaks exactly what it is meant to protect. Every fixture instructor is
# alliterative ("Ann Adams", "Bo Brooks"), which no real roster is.
_names = [c.strip() for row in CSV.splitlines()[1:] for c in row.split(",")[5:] if c.strip()]
check("fixture/has names to check at all", len(_names) > 10, True)
def _alliterative(cell: str) -> bool:
    # "Jo James (if needed)" — the sheet carries notes beside names; judge the name.
    parts = cell.split("(")[0].split()
    return len(parts) >= 2 and parts[0][:1].casefold() == parts[1][:1].casefold()


check("fixture/every name is a synthetic alliterative pair",
      [n for n in _names if not _alliterative(n)], [])

print("\n".join("FAIL: " + f for f in FAILS) or f"All checks passed.")
raise SystemExit(1 if FAILS else 0)
