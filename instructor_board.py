"""
Renders the instructor board as the description of one Discord embed.

One chronological table, no grouping: date, event, time, and how many
instructors are signed up against how many are wanted, with the names on the
line beneath each row. Discord has no markdown tables, so it goes in a fenced
code block, which renders monospace and lets the columns line up. That costs
bold/italic inside the block (they don't render in code) and gains a table you
can actually scan. A long name list wraps within its own line, which leaves the
rows below it still aligned.

Two rules shape everything here:

1. NO local state. The rendered text IS the state: the poster compares what it
   just rendered against the board already sitting in the channel, and only
   reposts when they differ. So this output must be a pure function of the
   sheet. Nothing time-dependent (no "as of 09:00") may appear, or every check
   would look like a change and the channel would get spammed twice a day.
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
# Colour of the embed's bar, by worst state on the board. Not a grouping: the
# rows stay in date order, this is just the at-a-glance signal.
COLOR_SHORT = 0xE03A3A      # someone is below the floor
COLOR_UNDER = 0xE6A700      # under target but workable
COLOR_OK = 0x2FA84F         # everything covered

# Discord's hard cap on an embed description. A row plus its names runs 120 to
# 200 characters depending on how full the roster is, so a busy couple of months
# can reach this. Going over is a 400 and no board at all, so we drop events off
# the far end until it fits.
DESCRIPTION_LIMIT = 4096

HEADERS = ("Date", "Event", "Time", "Have/Need")
# Names sit under their row, indented enough to read as a continuation.
NAME_INDENT = "   "


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


def _table(events: list[Event]) -> str:
    rows = [[fmt_date_short(e.date), fmt_event(e), fmt_time(e), fmt_staffing(e)]
            for e in events]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADERS)]

    def line(cells: list[str]) -> str:
        # Last column isn't padded, so no trailing whitespace in the block.
        return "  ".join(c.ljust(w) for c, w in zip(cells[:-1], widths[:-1])) + "  " + cells[-1]

    out = [line(list(HEADERS)), line(["-" * w for w in widths])]
    for event, row in zip(events, rows):
        out.append(line(row))
        out.append(f"{NAME_INDENT}{fmt_names(event)}")
    return "\n".join(out)


def color(events: list[Event]) -> int:
    """Embed bar colour: red if anything is below the floor, amber if anything is
    under target, green otherwise."""
    if any(e.critical for e in events):
        return COLOR_SHORT
    if any(e.short_by > 0 for e in events):
        return COLOR_UNDER
    return COLOR_OK


def render(all_events: list[Event]) -> str:
    """The whole board, as an embed description. Deterministic for a given
    sheet: no timestamps, because this text is what we diff against."""
    # Trim from the far end until it fits: the near events are the ones anybody
    # can still act on.
    shown = len(all_events)
    while shown > 1 and len(_render(all_events, shown)) > DESCRIPTION_LIMIT:
        shown -= 1
    return _render(all_events, shown)


def _render(all_events: list[Event], shown: int) -> str:
    if not all_events:
        return "No events on the sheet for the next few weeks."

    events, dropped = all_events[:shown], len(all_events) - shown

    short = [e for e in events if e.short_by > 0]
    if short:
        head = (f"**{len(short)} event{'s' if len(short) != 1 else ''} still "
                f"{'need' if len(short) != 1 else 'needs'} instructors.**")
    else:
        head = "**Every event is fully staffed.**"

    parts = [head, "", "```", _table(events), "```"]
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


def summary_line(events: list[Event]) -> str:
    """One line for logs and for the slash command's private reply."""
    short = [e for e in events if e.short_by > 0]
    if not short:
        return f"{len(events)} upcoming events, all fully staffed"
    total = sum(e.short_by for e in short)
    critical = sum(1 for e in short if e.critical)
    tail = f", {critical} short-handed" if critical else ""
    return (f"{len(events)} upcoming events, {len(short)} under target "
            f"({total} instructor slots to fill){tail}")
