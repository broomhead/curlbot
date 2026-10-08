"""
Renders the instructor board as the description of one Discord embed.

One chronological list with a traffic light per EVENT, not one per board. Each
event is in one of three states, the same red/amber/green the subs and practice
boards use:

    red     short by more than it can absorb, and close enough to hurt
    amber   under target, but workable as it stands or still far enough out
    green   fully staffed (or no target to measure against)

It takes both things to make an event red. Severity: a two sheet event with
three of its four instructors runs fine and nobody is chased over it, so it is
amber; the same event with two is a real ask (see `Event.workable`). Proximity:
a thin event eleven days out is a problem someone has to solve this week; the
same gap two months out is not (URGENT_DAYS, 14). A board that shouts about
both teaches people to ignore it.

The list is never reordered by state. Consecutive events in the same state
share one heading and one code block, and a change of state starts the next, so
reading down the board is still reading down the calendar and the lights say
where to look. A fully staffed Saturday between two thin Thursdays gets its own
green heading in between rather than being lumped in with either.

Each block is rows of date, event, time, and how many instructors are signed up
against how many are wanted, with the names on the lines beneath each row.
Discord has no markdown tables, so the rows go in fenced code blocks, which
render monospace and let the columns line up. Every block is laid out to the
same column widths and only the first carries the header row, so together they
read as one table with headings between the rows. That costs bold/italic inside
the block (they don't render in code) and gains a table you can actually scan.
The lights live on the headings, OUTSIDE the block, because an emoji inside a
code block is not one monospace cell wide and would shove the columns of its
own row out of line. A long name list is wrapped here rather than left to
Discord, which would start the continuation hard against the left margin where
it reads as another row.

Two rules shape everything here:

1. NO local state. The rendered text IS the state: the poster compares what it
   just rendered against the board already sitting in the channel, and only
   reposts when they differ. So this output must be a pure function of (sheet,
   today). Nothing finer grained than a date may appear: a clock time, even
   "as of 09:00", would make every check look like a change and spam the channel
   twice a day. Depending on the date is deliberate and costs at most one extra
   post on the day an event crosses the 14 day line, which is exactly the day
   people should see it change colour.
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

# The three states an event can be in. See `state`.
RED, AMBER, GREEN = "red", "amber", "green"

# Traffic lights, same vocabulary as the subs and practice boards.
LIGHTS = {RED: "🔴", AMBER: "🟡", GREEN: "🟢"}
# What each light says. One wording per state: amber covers both "one short but
# it runs" and "short with weeks to go", and the row beneath says which.
HEADINGS = {
    RED: "Needs instructors now",
    AMBER: "Could use more, not urgent",
    GREEN: "Fully staffed",
}

# Colour of the embed's bar: the worst state on the board. Red is reserved for
# an event that is both close and properly short; one instructor under target,
# or an empty LTC two months out, is worth listing and not worth making the
# whole board look on fire.
COLOR_SHORT = 0xE03A3A      # at least one red event
COLOR_UNDER = 0xE6A700      # no red, at least one amber
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


def state(event: Event, today: date) -> str:
    """RED, AMBER or GREEN for one event.

    Green is at target. Red needs BOTH halves: short by more than the event can
    absorb, and inside the window anyone is chased for. Everything else under
    target is amber, which covers the event that is one down and will run fine
    today as well as the empty one that is still weeks away."""
    if event.short_by == 0:
        return GREEN
    if event.understaffed and is_urgent(event, today):
        return RED
    return AMBER


def runs(events: list[Event], today: date) -> list[tuple[str, list[Event]]]:
    """The events in the order given, cut wherever the state changes:
    [(state, [events]), ...]. Nothing is moved, so a state can come up more
    than once and each time gets its own group."""
    out: list[tuple[str, list[Event]]] = []
    for event in events:
        s = state(event, today)
        if out and out[-1][0] == s:
            out[-1][1].append(event)
        else:
            out.append((s, [event]))
    return out


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


def _cells(event: Event) -> list[str]:
    return [fmt_date_short(event.date), fmt_event(event), fmt_time(event),
            fmt_staffing(event)]


def _widths(events: list[Event]) -> list[int]:
    """Column widths for the WHOLE board, so every block lines up with the
    others however the events are split between them."""
    rows = [_cells(e) for e in events]
    return [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADERS)]


def _line(cells: list[str], widths: list[int]) -> str:
    # Last column isn't padded, so no trailing whitespace in the block.
    return "  ".join(c.ljust(w) for c, w in zip(cells[:-1], widths[:-1])) + "  " + cells[-1]


def _table(events: list[Event], widths: list[int], *, header: bool) -> str:
    """One block's rows, each followed by its names. `header` puts the column
    titles on top; only the first block on a board gets them."""
    rule = _line(["-" * w for w in widths], widths)
    out = [_line(list(HEADERS), widths), rule] if header else []
    # The rule is the widest line the table can have, and it is the same for
    # every block, so names wrap at the same place all the way down.
    width = max(NAME_WRAP, len(rule))
    for event in events:
        out.append(_line(_cells(event), widths))
        out.extend(wrap_names(fmt_names(event), width))
    return "\n".join(out)


def color(events: list[Event], today: date | None = None) -> int:
    """Embed bar colour: the worst state of any event on the board."""
    today = today or date.today()
    states = {state(e, today) for e in events}
    if RED in states:
        return COLOR_SHORT
    if AMBER in states:
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


def _headline(events: list[Event], today: date) -> str:
    """The one line someone reads if they read nothing else. It counts only the
    red events, so a board of near misses and far-off gaps says nothing is
    urgent."""
    states = [state(e, today) for e in events]
    n = states.count(RED)
    if n:
        return (f"**{n} {_plural(n, 'event', 'events')} in the next {URGENT_DAYS} days "
                f"{_plural(n, 'needs', 'need')} instructors.**")
    n = states.count(AMBER)
    if n:
        return (f"**Nothing urgent. {n} {_plural(n, 'event', 'events')} could use "
                f"more instructors.**")
    return "**Every event is fully staffed.**"


def _heading(s: str) -> str:
    return f"{LIGHTS[s]}  **{HEADINGS[s]}**"


def _render(all_events: list[Event], shown: int, today: date) -> str:
    if not all_events:
        return "No events on the sheet for the next few weeks."

    events, dropped = all_events[:shown], len(all_events) - shown
    widths = _widths(events)

    parts = [_headline(events, today)]
    for i, (s, group) in enumerate(runs(events, today)):
        parts += ["", _heading(s), "```", _table(group, widths, header=i == 0), "```"]

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
    """One line for logs and for the slash command's private reply. Ends on
    the urgent count, the same thing the board's headline leads with."""
    today = today or date.today()
    short = [e for e in events if e.short_by > 0]
    if not short:
        return f"{len(events)} upcoming events, all fully staffed"
    urgent = sum(1 for e in short if state(e, today) == RED)
    total = sum(e.short_by for e in short)
    return (f"{len(events)} upcoming events, {len(short)} under target "
            f"({total} instructor slots to fill), {urgent or 'none'} urgent")
