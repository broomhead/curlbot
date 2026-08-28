"""
Ad-hoc sheet blocks — ice that's spoken for but isn't on any calendar.

Ice sometimes gets reserved outside every system the bot can see: someone
schedules a Learn-to-Curl by hand, a group books sheets at the rink directly,
money changes hands and nothing lands on the club calendar or in Gravity Forms.
To /sheets that ice still looks free, so members turn up expecting sheets that
aren't there.

A block is a manual "hold N sheets for this window" that anyone in the server
can place from the /sheets menu. It records who placed it and (optionally) what
for, so the report can say *why* the ice went away and members know who to ask.

Blocks feed the same overlap arithmetic as every other session: a block becomes
a session of type `practice_ice.BLOCK_TYPE` with `sheets_used = N`. That type is
in neither ALWAYS_SHOW_TYPES nor SHOW_IF_FREE_TYPES, so a block never gets a row
of its own — it reduces the free count on every row it overlaps, and
practice_ice annotates those rows with the block's details.

Pure module: no discord, no network. State shape:
  {
    "blocks": {
      "20260823T1330-1": {
        "id": "20260823T1330-1",
        "start": "2026-08-23T13:30:00",
        "end":   "2026-08-23T16:00:00",
        "sheets": 2,
        "reason": "LTC - off the books",
        "user_id": 123456,
        "name": "Dana",
        "ts": "2026-08-20T09:14:00"
      }, ...
    }
  }
"""

from __future__ import annotations

import json
import os
import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Optional

from ice import TOTAL_SHEETS
from practice_ice import BLOCK_TYPE

# A block lingers this long past its end time before it's swept, mirroring the
# grace /sheets gives a just-finished session (a block on ice that ran late
# shouldn't vanish while people are still looking at the row).
DEFAULT_GRACE_HOURS = 1.0
# Longest a TYPED block may run. This is a typo guard on the manual-entry modal
# ("2" meaning hours vs "2" fat-fingered into the wrong field), not a rule about
# ice: a block placed against a slot picked off the report inherits that session's
# real window, and an all-day practice block on the calendar is a legitimate 14
# hours long. So the cap lives in parse_duration, which only the typed path uses,
# and add() doesn't enforce it.
MAX_DURATION_HOURS = 12.0
# A bare "Aug 23" with no year means the coming Aug 23. Rolling to NEXT year is
# only right when the date is long past (typing "Jan 4" in December); a date a
# couple of weeks back is a typo, and silently booking it 11 months out would
# park a phantom block in the store that nobody notices. Past this many days
# behind, assume they mean next year; inside it, let parse_manual reject it.
YEAR_ROLL_DAYS = 60
# Blocks are for ice that's coming up. Anything further out than this is a
# mis-typed year, not a plan.
MAX_LEAD_DAYS = 180


def empty_state() -> dict:
    return {"blocks": {}}


def load(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state()
    state.setdefault("blocks", {})
    # Hand-editing this file is the documented way out of a bad block, so don't
    # assume every entry came from add(): the dict key is the id, and the release
    # menu reads `id` off the value.
    for key, block in state["blocks"].items():
        if isinstance(block, dict):
            block.setdefault("id", key)
    return state


def save(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _dt(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ── Creating / releasing ─────────────────────────────────────────────────────

def _next_id(state: dict, start: datetime) -> str:
    """Ids are `<start minute>-<n>`, so two blocks on the same slot never collide
    and the id sorts and reads sensibly in the JSON file."""
    stem = start.strftime("%Y%m%dT%H%M")
    n = 1
    while f"{stem}-{n}" in state["blocks"]:
        n += 1
    return f"{stem}-{n}"


def add(
    state: dict,
    *,
    start: datetime,
    end: datetime,
    sheets: int,
    user_id: int,
    name: str,
    reason: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Place a block. Raises ValueError (message is user-facing) on bad input."""
    if end <= start:
        raise ValueError("The block has to end after it starts.")
    sheets = int(sheets)
    if not 1 <= sheets <= TOTAL_SHEETS:
        raise ValueError(f"You can block between 1 and {TOTAL_SHEETS} sheets.")
    block = {
        "id": _next_id(state, start),
        "start": _iso(start),
        "end": _iso(end),
        "sheets": sheets,
        "reason": (reason or "").strip()[:120],
        "user_id": int(user_id),
        "name": name or "",
        "ts": _iso(now or datetime.now()),
    }
    state["blocks"][block["id"]] = block
    return block


def get(state: dict, block_id: str) -> Optional[dict]:
    return state.get("blocks", {}).get(block_id)


def release(state: dict, block_id: str) -> Optional[dict]:
    """Remove a block, returning it (or None if it was already gone)."""
    return state.get("blocks", {}).pop(block_id, None)


def active(state: dict, now: Optional[datetime] = None) -> list[dict]:
    """Blocks that haven't finished yet (or all of them, if `now` is None), soonest
    first. Sorted by start so the release menu reads in calendar order."""
    out = []
    for b in state.get("blocks", {}).values():
        end = _dt(b.get("end"))
        if now is not None and end is not None and end <= now:
            continue
        out.append(b)
    out.sort(key=lambda b: (b.get("start", ""), b.get("id", "")))
    return out


def expire(state: dict, now: datetime, grace_hours: float = DEFAULT_GRACE_HOURS) -> list[str]:
    """Drop blocks whose window has passed (+grace). Returns the removed ids.

    A block with an unparseable end date is left alone rather than silently
    dropped — better a stale hold someone can release by hand than ice that
    quietly reopens."""
    cutoff = now - timedelta(hours=grace_hours)
    dropped = []
    for bid, b in list(state.get("blocks", {}).items()):
        end = _dt(b.get("end"))
        if end is not None and end < cutoff:
            dropped.append(bid)
            del state["blocks"][bid]
    return dropped


# ── Feeding the sheet math ───────────────────────────────────────────────────

def as_sessions(
    state: dict,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> list[dict]:
    """Blocks as session dicts for practice_ice. `blocked_by` / `reason` / `block_id`
    ride along so a row can name who placed the hold; `title` stays the reason so
    generic session formatting has something sensible to show."""
    out = []
    for b in active(state):
        start, end = _dt(b.get("start")), _dt(b.get("end"))
        if start is None or end is None:
            continue
        if window_start is not None and end <= window_start:
            continue
        if window_end is not None and start >= window_end:
            continue
        out.append({
            "start": start,
            "end": end,
            "type": BLOCK_TYPE,
            "title": b.get("reason", ""),
            "sheets_used": int(b.get("sheets", 0)),
            "block_id": b.get("id"),
            "blocked_by": b.get("name", ""),
            "reason": b.get("reason", ""),
        })
    return out


def describe(block: dict, *, with_who: bool = True) -> str:
    """One-line human summary, e.g.
    'Sun Aug 23 · 1:30–4:00 PM · 2 sheets — LTC (Dana)'."""
    start, end = _dt(block.get("start")), _dt(block.get("end"))
    n = int(block.get("sheets", 0))
    when = fmt_window(start, end) if start else block.get("start", "")
    out = f"{when} · {n} sheet{'s' if n != 1 else ''}"
    reason = (block.get("reason") or "").strip()
    if reason:
        out += f" — {reason}"
    who = (block.get("name") or "").strip()
    if with_who and who:
        out += f" ({who})"
    return out


def window_text(block: dict) -> str:
    """A block's window in display form, straight from its stored ISO strings."""
    start, end = _dt(block.get("start")), _dt(block.get("end"))
    return fmt_window(start, end) if start else str(block.get("start", ""))


def fmt_window(start: datetime, end: Optional[datetime] = None) -> str:
    """'Sun Aug 23 · 1:30–4:00 PM' (or just the start when there's no end)."""
    day = start.strftime("%a %b %-d")
    if end is None:
        return f"{day} · {start.strftime('%-I:%M %p')}"
    if end.date() != start.date():
        # A slot picked off the calendar can span midnight (or a multi-day
        # entry). Naming only the start day would read as a two-minute block.
        return (f"{day} {start.strftime('%-I:%M %p')} – "
                f"{end.strftime('%a %b %-d')} {end.strftime('%-I:%M %p')}")
    # Drop the redundant AM/PM on the start when both ends share it.
    same_half = start.strftime("%p") == end.strftime("%p")
    s = start.strftime("%-I:%M") if same_half else start.strftime("%-I:%M %p")
    return f"{day} · {s}–{end.strftime('%-I:%M %p')}"


# ── Parsing the manual-entry modal ───────────────────────────────────────────
# Everything below turns a member's free text into datetimes. It lives here (not
# in the Discord layer) so it can be unit-tested without a gateway connection.

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_WEEKDAYS = {d: i for i, d in enumerate(
    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


def parse_date(text: str, *, today: date_cls) -> date_cls:
    """Accepts 'today', 'tomorrow', a weekday name ('sunday' = the next one),
    'Aug 23', '23 Aug', '8/23', '8/23/26' or '2026-08-23'. A date with no year
    resolves to the next occurrence, so blocking 'Jan 4' in December works."""
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("I need a date for the block.")
    if t in ("today", "tonight"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t[:3] in _WEEKDAYS and t.replace(".", "").isalpha():
        ahead = (_WEEKDAYS[t[:3]] - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _make_date(y, mo, d)

    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t)
    if m:
        mo, d, y = int(m[1]), int(m[2]), m[3]
        if y is None:
            return _next_occurrence(today, mo, d)
        y = int(y)
        return _make_date(y + 2000 if y < 100 else y, mo, d)

    m = re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?", t)
    if m and m[1][:3] in _MONTHS:
        mo, d, y = _MONTHS[m[1][:3]], int(m[2]), m[3]
        return _make_date(int(y), mo, d) if y else _next_occurrence(today, mo, d)

    m = re.fullmatch(r"(\d{1,2})\s+([a-z]{3,9})\.?(?:\s*,?\s*(\d{4}))?", t)
    if m and m[2][:3] in _MONTHS:
        d, mo, y = int(m[1]), _MONTHS[m[2][:3]], m[3]
        return _make_date(int(y), mo, d) if y else _next_occurrence(today, mo, d)

    raise ValueError(f"I couldn't read “{text}” as a date — try something like "
                     "“Aug 23”, “8/23” or “tomorrow”.")


def _make_date(year: int, month: int, day: int) -> date_cls:
    try:
        return date_cls(year, month, day)
    except ValueError:
        raise ValueError(f"{year}-{month:02d}-{day:02d} isn't a real date.") from None


def _next_occurrence(today: date_cls, month: int, day: int) -> date_cls:
    """This year's month/day, or next year's if it's a long way past — 'Jan 4' typed
    in December means the coming January. See YEAR_ROLL_DAYS for why the threshold
    isn't tighter than that."""
    d = _make_date(today.year, month, day)
    if d < today - timedelta(days=YEAR_ROLL_DAYS):
        d = _make_date(today.year + 1, month, day)
    return d


def parse_clock(text: str) -> tuple[int, int]:
    """'1:30 PM', '1:30pm', '1 pm', '13:30', '7' (assumed PM for rink hours)."""
    t = (text or "").strip().lower().replace(".", "")
    if not t:
        raise ValueError("I need a start time for the block.")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise ValueError(f"I couldn't read “{text}” as a time — try “1:30 PM” or “13:30”.")
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if minute > 59:
        raise ValueError(f"“{text}” has more than 59 minutes in it.")
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(f"“{text}” isn't a real time.")
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif hour > 23:
        raise ValueError(f"“{text}” isn't a real time.")
    elif hour < 7:
        # No 24h marker and an hour the rink is shut: they mean the evening.
        # 7-12 stay as typed (morning draws are real); 1-6 become 13-18.
        hour += 12
    return hour, minute


def parse_duration(text: str) -> timedelta:
    """'2', '2h', '2.5', '90m', '2:30', '1 hour 30' → a timedelta."""
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("I need to know how long the ice is blocked for.")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        total = timedelta(hours=int(m[1]), minutes=int(m[2]))
    elif re.fullmatch(r"\d{1,4}\s*m(in(ute)?s?)?", t):
        total = timedelta(minutes=int(re.match(r"\d+", t)[0]))
    else:
        # The hour suffix is optional and every spelling is allowed: "2", "2h",
        # "2hr", "2 hrs", "2 hours" — plus an optional trailing minutes part.
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:h(?:ou)?r?s?)?(?:\s+(\d{1,2})\s*m\w*)?", t)
        if not m:
            raise ValueError(f"I couldn't read “{text}” as a length — try “2”, “2.5” or “90m”.")
        try:
            total = timedelta(hours=float(m[1]), minutes=int(m[2] or 0))
        except (OverflowError, ValueError):
            # A digit string long enough to overflow a C int is still just a typo.
            raise ValueError(f"“{text}” isn't a length I can use — try “2”, “2.5” "
                             "or “90m”.") from None
    if total <= timedelta(0):
        raise ValueError("The block needs to last longer than zero minutes.")
    if total != total:  # NaN from a value like "nan" slipping through float()
        raise ValueError(f"I couldn't read “{text}” as a length.")
    if total > timedelta(hours=MAX_DURATION_HOURS):
        raise ValueError(f"That's longer than {MAX_DURATION_HOURS:g} hours — put ice "
                         "that's booked that long on the club calendar instead.")
    return total


def parse_sheets(text: str, total: int = TOTAL_SHEETS) -> int:
    t = (text or "").strip()
    m = re.search(r"\d+", t)
    if not m:
        raise ValueError(f"How many sheets? Enter a number from 1 to {total}.")
    n = int(m[0])
    if not 1 <= n <= total:
        raise ValueError(f"The club has {total} sheets — block between 1 and {total}.")
    return n


def parse_manual(
    date_text: str, time_text: str, duration_text: str, sheets_text: str,
    *, now: datetime, total: int = TOTAL_SHEETS,
) -> tuple[datetime, datetime, int]:
    """Turn the four modal fields into (start, end, sheets). Raises ValueError with a
    message meant for the member."""
    day = parse_date(date_text, today=now.date())
    hour, minute = parse_clock(time_text)
    start = datetime(day.year, day.month, day.day, hour, minute)
    duration = parse_duration(duration_text)
    try:
        end = start + duration
    except OverflowError:
        # e.g. year 9999 plus any length. Everything reaching this point parsed
        # cleanly on its own; it's the combination that isn't a real time.
        raise ValueError(f"{start:%b %-d, %Y} plus that long isn't a real time — "
                         "check the date.") from None
    sheets = parse_sheets(sheets_text, total)
    if end < now - timedelta(hours=6):
        raise ValueError(f"{fmt_window(start, end)} is in the past — blocks are for "
                         "upcoming ice.")
    if start > now + timedelta(days=MAX_LEAD_DAYS):
        # Almost always a year typo. Say the full date back, year included, since
        # that's the part they can't see in the short format.
        raise ValueError(f"{start:%a %b %-d, %Y} is more than {MAX_LEAD_DAYS} days "
                         "out — check the date?")
    return start, end, sheets
