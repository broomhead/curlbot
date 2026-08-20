"""
Practice-ice opportunity logic — pure, no Discord / network deps so it's unit-testable.

Practice ice exists wherever fewer than all sheets are in use. Three sources:
  1. A designated practice block on the calendar (occupies 0 sheets itself).
  2. A Learn-to-Curl that doesn't fill all sheets.
  3. A league draw that doesn't fill all sheets.

...minus a fourth thing that only takes ice away: a manual BLOCK (see
block_store), placed by a member when ice gets reserved off the books. A block
is a session like any other for the arithmetic, but it's in neither display
list, so it never gets a row of its own — it just lowers the free count on the
rows it overlaps, and those rows carry a note naming who blocked and why.

Unifying rule: during any session, free sheets = TOTAL_SHEETS minus the sheets
occupied by every session overlapping it (including itself). A session is a
practice opportunity when at least one sheet is free.

A "session" is a plain dict:
  {
    "start": datetime, "end": datetime,
    "type": "Practice" | "LTC" | "Private" | "League" | "Reserved Ice" | "Blocked",
    "title": str,
    "sheets_used": int | None,   # None = unknown (no registration data yet)
  }

A "Blocked" session carries two extras used only for display:
  "blocked_by": str  — the member who placed the hold
  "reason":     str  — what the ice is for, if they said (may be "")
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

# From ice.py, the one place the club's facility numbers live — a local literal
# here would silently disagree with NUM_SHEETS the moment a club with a different
# facility set it.
from ice import TOTAL_SHEETS  # noqa: E402

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

# Manual holds on ice that no calendar knows about (block_store). Deliberately in
# NEITHER list: a block isn't an event members could attend, so it gets no row —
# it only subtracts sheets from the sessions it overlaps, which the two functions
# below then annotate so the row can explain where the ice went. Keeping the name
# here (rather than in block_store) means the pure math module owns the session
# vocabulary and block_store imports it, not the other way round.
BLOCK_TYPE = "Blocked"
# Most block notes shown on a single row before they're summarised (see block_notes).
MAX_BLOCK_NOTES = 3


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


def blocks_during(session: dict, sessions: list[dict]) -> list[dict]:
    """Manual blocks overlapping `session`, soonest first. Used purely for display:
    the sheets they take are already gone via free_sheets_during."""
    out = [
        s for s in sessions
        if s.get("type") == BLOCK_TYPE
        and s is not session
        and overlaps(session["start"], session["end"], s["start"], s["end"])
    ]
    out.sort(key=lambda b: b["start"])
    return out


def annotate(sessions: list[dict], total: int = TOTAL_SHEETS) -> list[dict]:
    """Every non-block session with `free`, `has_unknown` and `blocks` attached,
    sorted by start time — before any display rule is applied.

    Separate from practice_opportunities because the display rules HIDE rows, and
    callers that need to reason about what a change did to the ice (what's left in
    a slot, who's signed up there) must see the hidden ones too. Filtering first
    and asking questions afterwards means a slot that just dropped off the report
    looks like a slot that never existed."""
    unblocked = [s for s in sessions if s.get("type") != BLOCK_TYPE]
    out: list[dict] = []
    for s in sessions:
        if s.get("type") == BLOCK_TYPE:
            continue          # a block is arithmetic, never a row
        free, has_unknown = free_sheets_during(s, sessions, total)
        # What this row would have had with no manual blocks at all. The gap
        # between the two is the ONLY honest test of whether a block affected
        # this row — merely overlapping it isn't enough. A league already booked
        # solid on its own is at 0 free before and after any block, and tagging
        # it "2 sheets blocked by Darin" would blame him for ice he never took.
        free_open, _ = free_sheets_during(s, unblocked, total)
        took_ice = free < free_open
        out.append({**s, "free": free, "has_unknown": has_unknown,
                    "free_if_unblocked": free_open,
                    "blocks": blocks_during(s, sessions) if took_ice else []})
    out.sort(key=lambda x: x["start"])
    return out


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
    return [o for o in annotate(sessions, total) if should_show(o, always_show, show_if_free)]


def should_show(opp: dict, always_show: tuple[str, ...] = ALWAYS_SHOW_TYPES,
                show_if_free: tuple[str, ...] = SHOW_IF_FREE_TYPES) -> bool:
    """Display rule for one annotated session.

    The last clause is why a league can still appear at 0 free: normally hiding
    it is right (a full league won't free up, so it isn't practice ice), but if a
    BLOCK is what closed it, hiding the row hides the block with it — and the row
    is the only place a block is ever shown, so the ice would vanish with no
    explanation anywhere. The condition is "it had ice and a block took it", not
    "a block happens to overlap": a league that was booked solid on its own stays
    hidden, exactly as before."""
    t = opp.get("type")
    if t in always_show:
        return True
    if t not in show_if_free:
        return False
    return (opp["free"] >= 1 or bool(opp.get("has_unknown"))
            or (bool(opp.get("blocks")) and opp.get("free_if_unblocked", 0) >= 1))


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
    for note in block_notes(opp):
        line += f"\n    {note}"
    return icon, line


def block_notes(opp: dict) -> list[str]:
    """One line per manual block on this row, naming who took the ice and (if they
    said) what for. Without this the sheets would just quietly go missing — the
    whole point of a block is that the reservation is off the books, so the report
    is the only place anyone can find out about it.

    The block's own window is spelled out whenever it differs from the session's,
    since a block often covers only part of a practice slot and the free count is
    the pessimistic whole-window number."""
    notes = []
    held = opp.get("blocks") or []
    for b in held[:MAX_BLOCK_NOTES]:
        n = b.get("sheets_used") or 0
        who = (b.get("blocked_by") or "").strip() or "someone"
        note = f"🚫 {n} sheet{'s' if n != 1 else ''} blocked by {who}"
        reason = html.unescape(str(b.get("reason") or "")).strip()
        if reason:
            note += f" — {reason}"
        if b.get("start") != opp.get("start") or b.get("end") != opp.get("end"):
            note += f" ({_span(b['start'], b['end'])})"
        notes.append(note)
    extra = len(held) - MAX_BLOCK_NOTES
    if extra > 0:
        # Embed descriptions are capped at 4096 characters and a busy day can
        # stack a lot of holds on one slot; the free count above already accounts
        # for every one of them.
        notes.append(f"🚫 …and {extra} more block{'s' if extra != 1 else ''} on this slot")
    return notes


def _span(start: datetime, end: datetime) -> str:
    """'1:30–4:00 PM', collapsing the repeated AM/PM when both ends share one.
    A window crossing midnight names the end's day too, or '10:00 PM–2:00 AM'
    reads as a twenty-hour span running backwards."""
    if start.date() != end.date():
        return f"{start.strftime('%-I:%M %p')}–{end.strftime('%a %-I:%M %p')}"
    same_half = start.strftime("%p") == end.strftime("%p")
    left = start.strftime("%-I:%M") if same_half else start.strftime("%-I:%M %p")
    return f"{left}–{end.strftime('%-I:%M %p')}"
