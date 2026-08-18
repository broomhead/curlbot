"""
Renders the instructor board as the description of one Discord embed.

Grouped by URGENCY, not by how short an event is. An LTC eleven days out with
half its instructors is a problem someone has to solve this week; the same gap
in October is not, and a board that shouts about both teaches people to ignore
it. So events split at URGENT_DAYS (14) into "needs instructors now" and
"coming up later", each with a traffic light on its heading, the same
red/amber/green the subs and practice boards use.

Inside a group it is one chronological table: date, event, time, and how many
instructors are signed up against how many are wanted, with the names on the
lines beneath each row. Discord has no markdown tables, so each goes in a fenced
code block, which renders monospace and lets the columns line up. That costs
bold/italic inside the block (they don't render in code) and gains a table you
can actually scan. The lights live on the headings, OUTSIDE the block, because
an emoji inside a code block is not one monospace cell wide and would shove the
columns of its own row out of line. A long name list is wrapped here rather than
left to Discord, which would start the continuation hard against the left margin
where it reads as another row.

Two rules shape everything here:

1. NO local state. The rendered text IS the state: the poster compares what it
   just rendered against the board already sitting in the channel, and only
   reposts when they differ. So this output must be a pure function of (sheet,
   today). Nothing finer grained than a date may appear: a clock time, even
   "as of 09:00", would make every check look like a change and spam the channel
   twice a day. Depending on the date is deliberate and costs at most one extra
   post on the day an event crosses the 14 day line, which is exactly the day
   people should see it move.
2. Club house style: no em dashes and no en dash ranges anywhere in member
   facing copy. Plain hyphens, commas and parentheses only.

Pure module: no discord import, so the whole thing stays unit-testable. The cog
wraps the string below in an Embed.
"""

from __future__ import annotations

import os
import re
from datetime import date

from instructor_sheet import Event, edit_url

# The embed title of every board we post. Used to recognise our own previous
# board in the channel, so keep it stable; changing it orphans the board that is
# already there (it will be left behind rather than replaced).
BOARD_TITLE = "🥌  Instructor board"

# How close an event has to be before a gap in it counts as urgent.
URGENT_DAYS = int(os.environ.get("URGENT_DAYS", "14") or 14)

# Traffic lights, same vocabulary as the subs and practice boards.
LIGHT_NOW = "🔴"        # short of instructors, and close enough to hurt
LIGHT_LATER = "🟡"      # short, but there is still time
LIGHT_OK = "🟢"         # covered

# Colour of the embed's bar. Red is reserved for the urgent window: a bare
# October LTC is worth listing, not worth making the whole board look on fire.
COLOR_SHORT = 0xE03A3A      # something inside URGENT_DAYS is short
COLOR_UNDER = 0xE6A700      # only later events are short
COLOR_OK = 0x2FA84F         # everything covered

# Discord's hard cap on an embed description. A row plus its names runs 120 to
# 200 characters depending on how full the roster is, so a busy couple of months
# can reach this. Going over is a 400 and no board at all, so we drop events off
# the far end until it fits.
DESCRIPTION_LIMIT = 4096

HEADERS = ("Date", "Event", "Time", "Have/Need")
# Names sit under their row, indented enough to read as a continuation.
NAME_INDENT = "   "
# Where a long name list wraps. Discord wraps an over-long line inside a code
# block at the window edge and puts the continuation flush left, which looks
# like a new table row, so we wrap it ourselves and indent every line the same.
# The table itself widens this when it is wider: names never make the block
# wider than the rows above them.
NAME_WRAP = 44


def is_urgent(event: Event, today: date) -> bool:
    """Inside the window we actually chase people for. Something already past
    (the sheet is hand-maintained, it happens) counts as urgent, not as calm."""
    return (event.date - today).days <= URGENT_DAYS


def fmt_date_short(d: date) -> str:
    """"Sat 8/29" - the table needs a fixed, narrow date."""
    return f"{d.strftime('%a')} {d.month}/{d.day}"


def fmt_event(event: Event) -> str:
    """"Private Event" is the widest thing on the sheet and the word "Event"
    carries nothing here, so drop it."""
    return re.sub(r"\s*events?\s*$", "", event.type.strip(), flags=re.I) or event.type.strip()


def fmt_time(event: Event) -> str:
    """Sheet text, whitespace normalised and the range hyphen closed up
    ("12:30 - 2:45 pm" -> "12:30-2:45 pm"). Two characters of table width per
    row, and it reads the same."""
    t = " ".join((event.time or "").split())
    return re.sub(r"\s*-\s*", "-", t)


def fmt_staffing(event: Event) -> str:
    """"6/8", or just "5" when the event has no target we can compute (a CPATH
    with no attendee count)."""
    need = event.needed
    return f"{event.filled}/{need}" if need is not None else str(event.filled)


def fmt_names(event: Event) -> str:
    """Who is signed up, in sheet order. A tentative name keeps its "(if needed)"
    qualifier so the list explains itself against the count, which excludes it."""
    names = list(event.instructors) + [f"{n} (if needed)" for n in event.tentative]
    return ", ".join(names) if names else "nobody yet"


def wrap_names(names: str, width: int) -> list[str]:
    """The names line, split at ", " boundaries so a name is never broken across
    lines, with every line carrying NAME_INDENT. A single name longer than the
    width keeps its own line rather than being chopped."""
    limit = max(width - len(NAME_INDENT), 12)
    parts = names.split(", ")
    # Carry each comma with the name it follows, so a line that ends on a comma
    # is still measured with it and can't run one character past the width.
    parts = [f"{n}," for n in parts[:-1]] + parts[-1:]
    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current} {part}" if current else part
        if current and len(candidate) > limit:
            lines.append(current)
            current = part
        else:
            current = candidate
    lines.append(current)
    return [f"{NAME_INDENT}{line}" for line in lines]


def _table(events: list[Event]) -> str:
    rows = [[fmt_date_short(e.date), fmt_event(e), fmt_time(e), fmt_staffing(e)]
            for e in events]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADERS)]

    def line(cells: list[str]) -> str:
        # Last column isn't padded, so no trailing whitespace in the block.
        return "  ".join(c.ljust(w) for c, w in zip(cells[:-1], widths[:-1])) + "  " + cells[-1]

    out = [line(list(HEADERS)), line(["-" * w for w in widths])]
    width = max([NAME_WRAP] + [len(l) for l in out])
    for event, row in zip(events, rows):
        out.append(line(row))
        out.extend(wrap_names(fmt_names(event), width))
    return "\n".join(out)


def split_by_urgency(events: list[Event], today: date) -> tuple[list[Event], list[Event]]:
    """(needs help now, coming up later). Both keep the sheet's date order."""
    return ([e for e in events if is_urgent(e, today)],
            [e for e in events if not is_urgent(e, today)])


def color(events: list[Event], today: date | None = None) -> int:
    """Embed bar colour: red only when something inside the urgent window is
    short, amber when the only gaps are further out, green when nothing is
    short. Proximity, not severity: a below-floor event in October is a yellow
    board, because nobody needs to drop what they're doing today over it."""
    today = today or date.today()
    near, later = split_by_urgency(events, today)
    if any(e.short_by > 0 for e in near):
        return COLOR_SHORT
    if any(e.short_by > 0 for e in later):
        return COLOR_UNDER
    return COLOR_OK


def render(all_events: list[Event], today: date | None = None) -> str:
    """The whole board, as an embed description. Deterministic for a given sheet
    and date: no timestamps, because this text is what we diff against."""
    today = today or date.today()
    # Trim from the far end until it fits: the near events are the ones anybody
    # can still act on.
    shown = len(all_events)
    while shown > 1 and len(_render(all_events, shown, today)) > DESCRIPTION_LIMIT:
        shown -= 1
    return _render(all_events, shown, today)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _headline(near: list[Event], later: list[Event]) -> str:
    """The one line someone reads if they read nothing else. It counts only the
    urgent gaps, so a quiet fortnight says so even with October wide open."""
    near_short = [e for e in near if e.short_by > 0]
    later_short = [e for e in later if e.short_by > 0]
    if near_short:
        n = len(near_short)
        return (f"**{n} {_plural(n, 'event', 'events')} in the next {URGENT_DAYS} days "
                f"{_plural(n, 'needs', 'need')} instructors.**")
    if later_short:
        n = len(later_short)
        return (f"**Nothing urgent. {n} later {_plural(n, 'event', 'events')} "
                f"{_plural(n, 'is', 'are')} still short.**")
    return "**Every event is fully staffed.**"


def _heading(events: list[Event], *, urgent: bool) -> str:
    short = any(e.short_by > 0 for e in events)
    if urgent:
        if short:
            return f"{LIGHT_NOW}  **Needs instructors now (next {URGENT_DAYS} days)**"
        return f"{LIGHT_OK}  **Next {URGENT_DAYS} days, fully staffed**"
    if short:
        return f"{LIGHT_LATER}  **Coming up later**"
    return f"{LIGHT_OK}  **Coming up later, fully staffed**"


def _render(all_events: list[Event], shown: int, today: date) -> str:
    if not all_events:
        return "No events on the sheet for the next few weeks."

    events, dropped = all_events[:shown], len(all_events) - shown
    near, later = split_by_urgency(events, today)

    parts = [_headline(near, later)]
    for group, urgent in ((near, True), (later, False)):
        if group:
            parts += ["", _heading(group, urgent=urgent), "```", _table(group), "```"]

    if dropped:
        parts.append(f"Showing the next {len(events)}; {dropped} further "
                     f"event{'s are' if dropped != 1 else ' is'} on the sheet.")

    if any(e.needed is None for e in events):
        parts.append("No attendee count on the sheet means no target, "
                     "so those show just who is signed up.")

    footer = os.environ.get("BOARD_FOOTER") or _default_footer()
    if footer:
        parts.append(footer)
    return "\n".join(parts)


def _default_footer() -> str:
    url = edit_url()
    return f"Add yourself on the [instructor sheet]({url})." if url else ""


def summary_line(events: list[Event], today: date | None = None) -> str:
    """One line for logs and for the slash command's private reply. Leads with
    the urgent count, same as the board does."""
    today = today or date.today()
    short = [e for e in events if e.short_by > 0]
    if not short:
        return f"{len(events)} upcoming events, all fully staffed"
    urgent = [e for e in short if is_urgent(e, today)]
    total = sum(e.short_by for e in short)
    tail = (f", {len(urgent)} inside {URGENT_DAYS} days" if urgent
            else f", none inside {URGENT_DAYS} days")
    return (f"{len(events)} upcoming events, {len(short)} under target "
            f"({total} instructor slots to fill){tail}")
