"""
Practice-ice opportunity logic — pure, no Discord / network deps so it's unit-testable.

Practice ice exists wherever fewer than all sheets are in use. Three sources:
  1. A designated practice block on the calendar (occupies 0 sheets itself).
  2. A Learn-to-Curl that doesn't fill all sheets.
  3. A league draw that doesn't fill all sheets.

Unifying rule: during any session, free sheets = TOTAL_SHEETS minus the sheets
occupied by every session overlapping it (including itself). A session is a
practice opportunity when at least one sheet is free.

A "session" is a plain dict:
  {
    "start": datetime, "end": datetime,
    "type": "Practice" | "LTC" | "Private" | "League" | "Reserved Ice",
    "title": str,
    "sheets_used": int | None,   # None = unknown (no registration data yet)
  }
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

TOTAL_SHEETS = 4

# Display rules by session type:
#   ALWAYS_SHOW   — listed even when full (0 free), so members have visibility
#                   (practice blocks and LTCs often free up / have space).
#   SHOW_IF_FREE  — listed only when at least one sheet is open (a booked-solid
#                   league or private event isn't practice ice).
# Free sheets during any session = total minus the sheets used by EVERY
# overlapping session, so concurrent bookings stack correctly (e.g. two private
# events using 2 + 1 sheets at the same time leave 1 free).
ALWAYS_SHOW_TYPES = ("Practice", "LTC", "Private", "Reserved Ice")
SHOW_IF_FREE_TYPES = ("League",)


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and a_end > b_start


def free_sheets_during(session: dict, sessions: list[dict], total: int = TOTAL_SHEETS):
    """
    Free sheets during `session`. Returns (free, has_unknown):
      free        — sheets free assuming unknown sessions use 0
      has_unknown — True if an overlapping session has unknown usage, so `free`
                    is an optimistic upper bound.
    """
    occupied = 0
    has_unknown = False
    for s in sessions:
        if not overlaps(session["start"], session["end"], s["start"], s["end"]):
            continue
        used = s.get("sheets_used")
        if used is None:
            has_unknown = True
        else:
            occupied += used
    return max(0, total - occupied), has_unknown


def practice_opportunities(
    sessions: list[dict],
    total: int = TOTAL_SHEETS,
    always_show: tuple[str, ...] = ALWAYS_SHOW_TYPES,
    show_if_free: tuple[str, ...] = SHOW_IF_FREE_TYPES,
) -> list[dict]:
    """
    Return the sessions worth displaying, each annotated with `free` and
    `has_unknown`, sorted by start time. `always_show` types are listed even at
    0 free; `show_if_free` types only when a sheet is open (or usage is unknown);
    all other types are omitted from the list (but still counted in the math).
    """
    out: list[dict] = []
    for s in sessions:
        free, has_unknown = free_sheets_during(s, sessions, total)
        t = s.get("type")
        show = (t in always_show) or (t in show_if_free and (free >= 1 or has_unknown))
        if show:
            out.append({**s, "free": free, "has_unknown": has_unknown})
    out.sort(key=lambda x: x["start"])
    return out


def format_opportunity(opp: dict, total: int = TOTAL_SHEETS) -> tuple[str, str]:
    """Return (icon, line) for display: time · type · sheets available · date."""
    start: datetime = opp["start"]
    when = f"{start.strftime('%a %b %-d')} · {start.strftime('%-I:%M %p')}"
    free = opp["free"]

    if opp.get("has_unknown"):
        icon = "⚪"
        sheets = f"{free}–{total} sheets free (unconfirmed)"
    elif free == 0:
        icon = "🔴"
        sheets = "0 sheets free"
    elif free == 1:
        icon = "🟡"
        sheets = "1 sheet free"
    else:
        icon = "🟢"
        sheets = f"{free} of {total} sheets free"

    line = f"{icon}  **{when}** · {opp['type']} · {sheets}"
    title = html.unescape(str(opp.get("title", "")))  # decode &amp; &#8211; etc.
    if title:
        line += f"\n    {title}"
    return icon, line
