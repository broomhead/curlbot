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

# Between target and floor sits the line that decides whether anyone is chased:
# an average of 1.5 instructors a sheet, in whole people. One down on two or
# three sheets and two down on a full rink are workable; one sheet has no slack.
def bare(attendees, **kw):
    return Event(type="LTC", date=TODAY, time="", attendees=attendees, **kw)

check("staff/workable is 1.5 a sheet, rounded up",
      [bare(n).workable for n in (8, 16, 24, 32)], [2, 3, 5, 6])
check("staff/3 of 4 is workable, 2 of 4 is not",
      [bare(16, instructors=["A"] * n).understaffed for n in (4, 3, 2)],
      [False, False, True])
check("staff/6 of 8 is workable, 5 of 8 is not",
      [bare(32, instructors=["A"] * n).understaffed for n in (6, 5)], [False, True])
check("staff/an overridden target gets the same proportional slack",
      [bare(32, needed_override=n).workable for n in (1, 2, 3, 4, 6, 8)],
      [1, 2, 3, 3, 5, 6])
# The line is configurable, and pinned between the other two whatever it is set to.
_saved_ok = instructor_sheet.OK_INSTRUCTORS_PER_SHEET
instructor_sheet.OK_INSTRUCTORS_PER_SHEET = 1.0
check("staff/workable can be lowered to the floor", bare(32).workable, bare(32).minimum)
instructor_sheet.OK_INSTRUCTORS_PER_SHEET = 5.0
check("staff/workable never exceeds the target", bare(32).workable, bare(32).needed)
instructor_sheet.OK_INSTRUCTORS_PER_SHEET = _saved_ok

# CPATH events carry no attendee count, so we can't claim a shortfall at all.
cpath = Event(type="CPATH", date=TODAY, time="", attendees=None, instructors=["A"])
check("staff/no attendees means no target", (cpath.sheets, cpath.needed, cpath.minimum),
      (None, None, None))
check("staff/no attendees is never short", (cpath.short_by, cpath.critical), (0, False))
check("staff/no attendees means no workable line either",
      (cpath.workable, cpath.understaffed), (None, False))
check("staff/cpath note", cpath.note, "no club credit for this one")

# A sheet column, if one is ever added, overrides the computed target.
override = Event(type="LTC", date=TODAY, time="", attendees=32,
                 instructors=["A"], needed_override=3)
check("staff/column overrides the ratio", (override.needed, override.short_by), (3, 2))
check("staff/override reads from the sheet",
      parse_events("Date,# of Attendees,Instructors Needed,Instructor1\n"
                   "9/5/26,32,3,Ann\n", today=TODAY)[0].needed, 3)


# ── One light per event ──────────────────────────────────────────────────────
# Red takes two things at once: short by more than the event can absorb, AND
# close enough to matter. Either one alone is amber.
def staffed(days, attendees, filled, **kw):
    return Event(type="LTC", date=TODAY + _TD(days=days), time="2 - 4 pm",
                 attendees=attendees, instructors=[f"P{i}" for i in range(filled)], **kw)


def lights(rendered):
    return [l.split()[0] for l in rendered.splitlines() if l.startswith(("🔴", "🟡", "🟢"))]


def table_blocks(rendered):
    return [b.strip().splitlines() for b in rendered.split("```")[1::2]]


check("state/at target is green", board.state(staffed(3, 16, 4), TODAY), board.GREEN)
check("state/one down on two sheets is amber, even on the day",
      board.state(staffed(0, 16, 3), TODAY), board.AMBER)
check("state/two down on two sheets is red", board.state(staffed(3, 16, 2), TODAY), board.RED)
check("state/two down on a full rink is amber",
      board.state(staffed(3, 32, 6), TODAY), board.AMBER)
check("state/three down on a full rink is red",
      board.state(staffed(3, 32, 5), TODAY), board.RED)
check("state/one sheet has no slack to give",
      board.state(staffed(3, 8, 1), TODAY), board.RED)
check("state/the same empty event weeks out is amber",
      [board.state(staffed(n, 16, 0), TODAY) for n in (5, 45)], [board.RED, board.AMBER])
check("state/no target is green, never an invented gap",
      board.state(Event(type="CPATH", date=TODAY, time="", attendees=None), TODAY),
      board.GREEN)

# The window itself: 14 days out is still urgent, 15 is not.
edge = [staffed(n, 16, 0) for n in (14, 15)]
check("state/the boundary day counts as urgent",
      [board.is_urgent(e, TODAY) for e in edge], [True, False])
check("state/and that is the day it changes colour",
      [board.state(e, TODAY) for e in edge], [board.RED, board.AMBER])
check("state/a past event on the sheet is urgent, not calm",
      board.is_urgent(staffed(-1, 16, 0), TODAY), True)

# ── Grouping: runs of the same light, never reordered ────────────────────────
# The shape this exists for: a weekday event today that is one down and will
# run fine, a full Saturday, a weekday next week that is properly short, and
# another full Saturday. Four events, four lights, and only one of them red.
week = [staffed(0, 16, 3), staffed(2, 32, 8), staffed(7, 16, 2), staffed(9, 32, 8)]
week_text = board.render(week, today=TODAY)
check("runs/one group per change of state",
      [(s, len(g)) for s, g in board.runs(week, TODAY)],
      [(board.AMBER, 1), (board.GREEN, 1), (board.RED, 1), (board.GREEN, 1)])
check("runs/headings come in calendar order", lights(week_text), ["🟡", "🟢", "🔴", "🟢"])
check("runs/a state that comes round again gets its own block",
      len(table_blocks(week_text)), 4)
check("runs/headline counts the one red event", week_text.splitlines()[0],
      "**1 event in the next 14 days needs instructors.**")
check("runs/the bar is red while anything is", board.color(week, today=TODAY),
      board.COLOR_SHORT)
# One more instructor on the short one and nothing is red any more.
eased = week[:2] + [staffed(7, 16, 3)] + week[3:]
check("runs/the bar drops to amber when the red event becomes workable",
      board.color(eased, today=TODAY), board.COLOR_UNDER)
check("runs/and the headline stops asking",
      board.render(eased, today=TODAY).splitlines()[0],
      "**Nothing urgent. 2 events could use more instructors.**")

# Neighbours in the same state share a heading and a block.
pair = [staffed(1, 16, 4), staffed(2, 32, 8), staffed(3, 16, 2)]
check("runs/consecutive events in one state are grouped",
      [(s, len(g)) for s, g in board.runs(pair, TODAY)], [(board.GREEN, 2), (board.RED, 1)])
check("runs/one heading for the pair", lights(board.render(pair, today=TODAY)), ["🟢", "🔴"])
# Sheet order in, sheet order out: grouping only ever cuts the list.
for label, sample in (("week", week), ("pair", pair), ("fixture", EVENTS)):
    check(f"runs/never reorders ({label})",
          [e.date for _, g in board.runs(sample, TODAY) for e in g],
          [e.date for e in sample])
check("runs/no events, no groups", board.runs([], TODAY), [])

# Every block is laid out to the same widths, so the split doesn't show in the
# columns, and only the first one spends two lines on the header.
week_blocks = table_blocks(week_text)
week_rows = [l for b in week_blocks for l in b
             if not l.startswith(board.NAME_INDENT)][2:]
check("runs/only the first block carries the header",
      [b[0].split() == list(board.HEADERS) for b in week_blocks],
      [True, False, False, False])
check("runs/columns line up across blocks",
      len({len(l) - len(l.rsplit("  ", 1)[-1]) for l in week_rows}), 1)
check("runs/one row per event across blocks",
      [l.split()[-1] for l in week_rows], ["3/4", "8/8", "2/4", "8/8"])


# ── The rendered board ───────────────────────────────────────────────────────
# The fixture at TODAY: 8/25 is two down on a full rink (workable, amber), 8/29
# has one instructor for three sheets (red), and 9/19 and 10/17 are thin but
# weeks away (amber, and next to each other, so one block between them).
text = board.render(EVENTS, today=TODAY)
blocks = table_blocks(text)
block = [l for b in blocks for l in b]          # every table line, in order
header = block[:2]
# Rows are the table lines; each is followed by its names on indented lines.
rows = [l for l in block[2:] if not l.startswith(board.NAME_INDENT)]
names = [l for l in block[2:] if l.startswith(board.NAME_INDENT)]
headings = [l for l in text.splitlines() if l.startswith(("🔴", "🟡", "🟢"))]


def block_dates(i):
    lines = blocks[i] if i < len(blocks) else []
    return [l.split()[1] for l in lines
            if l not in header and not l.startswith(board.NAME_INDENT)]


check("board/three runs, one table each", text.count("```"), 6)
check("board/first run", block_dates(0), ["8/25"])
check("board/second run", block_dates(1), ["8/29"])
check("board/third run holds both later events", block_dates(2), ["9/19", "10/17"])
check("board/headings", headings,
      ["🟡  **Could use more, not urgent**", "🔴  **Needs instructors now**",
       "🟡  **Could use more, not urgent**"])
check("board/lights stay out of the code block, where they'd break alignment",
      [l for l in block if any(c in l for c in "🔴🟡🟢")], [])
check("board/header row", header[0].split(), ["Date", "Event", "Time", "Have/Need"])
check("board/separator row", set(header[1]) <= {"-", " "}, True)
check("board/one row per event", len(rows), len(EVENTS))
check("board/at least one name line per event", len(names) >= len(EVENTS), True)
check("board/chronological top to bottom",
      [l.split()[1] for l in rows], ["8/25", "8/29", "9/19", "10/17"])
check("board/columns align", len({len(l) - len(l.rsplit("  ", 1)[-1]) for l in rows}), 1)
check("board/table rows stay narrow", max(len(l) for l in rows) <= 50, True)

check("board/one group means one table",
      board.render(edge[:1], today=TODAY).count("```"), 2)
check("board/a quiet fortnight still says so",
      board.render([e for e in EVENTS if e.date.month > 8], today=TODAY).splitlines()[0],
      "**Nothing urgent. 2 events could use more instructors.**")
check("board/a covered event gets a green heading",
      board.render([staffed(3, 16, 4)], today=TODAY).splitlines()[2],
      "🟢  **Fully staffed**")

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
check("board/headline counts only the red events", text.splitlines()[0],
      "**1 event in the next 14 days needs instructors.**")
check("board/headline plural",
      board.render([staffed(1, 16, 0), staffed(2, 16, 4), staffed(3, 16, 1)],
                   today=TODAY).splitlines()[0],
      "**2 events in the next 14 days need instructors.**")
check("board/headline singular when one event is merely under target",
      board.render([staffed(1, 16, 3)], today=TODAY).splitlines()[0],
      "**Nothing urgent. 1 event could use more instructors.**")
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

# Embed colour is the worst state on the board, which is not grouping: the rows
# stay in date order either way.
full = [Event(type="LTC", date=date(2026, 9, 5), time="2 - 4 pm", attendees=16,
              instructors=["A", "B", "C", "D"])]
check("board/colour red when something urgent is properly short",
      board.color(EVENTS, today=TODAY), board.COLOR_SHORT)
check("board/colour green when covered", board.color(full, today=TODAY), board.COLOR_OK)
check("board/colour green for an empty board", board.color([], today=TODAY), board.COLOR_OK)
# The same empty event, near and far: it takes proximity as well as a real gap
# to make the bar red, so a far-off LTC with nobody on it is an amber board.
bare_near = [staffed(5, 16, 0)]
bare_far = [staffed(45, 16, 0)]
check("board/colour red for a near gap", board.color(bare_near, today=TODAY),
      board.COLOR_SHORT)
check("board/colour amber for the same gap months out",
      board.color(bare_far, today=TODAY), board.COLOR_UNDER)
# And the other half: right on top of us but only one down is amber too.
check("board/colour amber for a near event that is one down",
      board.color([staffed(0, 16, 3)], today=TODAY), board.COLOR_UNDER)

check("summary/all staffed", board.summary_line(full, today=TODAY),
      "1 upcoming events, all fully staffed")
check("summary/ends on the urgent count", board.summary_line(EVENTS, today=TODAY),
      "4 upcoming events, 4 under target (20 instructor slots to fill), 1 urgent")
check("summary/says when nothing is urgent", board.summary_line(bare_far, today=TODAY),
      "1 upcoming events, 1 under target (4 instructor slots to fill), none urgent")
check("summary/one down today is under target but not urgent",
      board.summary_line([staffed(0, 16, 3)], today=TODAY),
      "1 upcoming events, 1 under target (1 instructor slots to fill), none urgent")


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
