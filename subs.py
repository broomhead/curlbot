"""
Subs board — an interactive, persistent Discord widget for "I need a sub" /
"I can sub" coordination.

Requests and availability are SHARED across every server the bot is in; each
server renders its own board of that same data. A change made anywhere updates the
shared data and reposts the board on the SERVER WHERE IT HAPPENED (so testing on
one server doesn't spam another) — other servers pick up the change the next time
someone acts there. `/subs` shows a private copy; `/subs show:True` (re)posts a
server's public board in the current channel. The board is never pinned — it
reposts fresh at the bottom on each change. It groups open games by date with a
🔴/🟡/🟢 status per spot and lists who's available for each. Interaction is on the
board:

  ➕ Need a sub      — league → team → date(s) → spots (picked from the system).
                       The TEAM is optional — chairs often don't set teams until a
                       day or two before the first draw. The DATE never is: when you
                       need a sub is the point. A league with no schedule posted
                       still offers real dates, projected weekly from its title's
                       start date onto its own night.
                       Tick SEVERAL dates and each is posted as its own ordinary
                       request, sharing a series_id. That id is a BULK-INPUT TAG and
                       nothing more: from the moment they're posted the dates are
                       independent sub opportunities — separately claimable,
                       droppable, lockable and expiring — so one person pulling out
                       of one date changes that date and nothing else. There is
                       deliberately no "cancel them all".
                       Re-running the flow for a date you already have open is not
                       a dead end any more — it becomes an EDIT of that request's
                       spot count ("we found one, now we need three"), so nobody
                       already on it gets bumped.
  🙋 I'm free        — list your availability so you get tagged for matching games.
  ➕ Fill for someone — mark another member into an open spot (offline sync). Covers
                       several dates of one posting at once, with the same picker,
                       and ends on an explicit submit button — picking a name records
                       nothing, because a mis-tap would otherwise put the wrong person
                       on someone else's game with no undo.
                       Also sits on every alert, pre-aimed at that posting: since
                       posting no longer asks who's covering the dates, this is where
                       "Ben's got Tuesdays" gets recorded, one tap from the ask.
  ➖ Remove          — take a sub off a game, cancel a request you opened, or clear
                       your own availability. All three CONFIRM first: in this cog a
                       select never mutates shared state, only a button does.
  🙋 <game>          — one one-tap button per open date, matching the text line for
                       line. NOT one per multi-date posting: posting several dates at
                       once is a bulk input convenience, not a standing arrangement —
                       the moment someone can't make week 3, "their run" is a fiction
                       and the other dates carry on. Bulk lives where it belongs: the
                       posting form, the alert for a fresh post, and Fill for someone.
  📋 Show all        — the board lists only the next SUBS_BOARD_DAYS (14) days; a
                       Discord embed can't scroll, and a 16-date season would bury the
                       games people can still act on. Everything past that condenses to
                       one line, and this button sends the whole board privately,
                       buttons and all. Nothing is hidden, only moved out of the way.

When a request is posted (and again ~24h before an unfilled game), the bot posts a
public alert that @-mentions the members available for that game, each carrying an
"I'll take it" button. The only DM the bot sends is to a request's owner, letting
them know their game just gained or lost a sub; everything else is in the channel.

Sub rosters freeze LOCK_MINUTES before game time. Requests auto-expire a few hours
after their game. (Requests can no longer be posted without a date, but ones made
while that was allowed still render and age out after SUBS_UNDATED_DAYS.)
State lives in a small JSON file (see sub_store).

discord.py >= 2.4 is required for DynamicItem (persistent buttons that survive a
bot restart without re-registering each message).
"""

from __future__ import annotations

import os
import re
import asyncio
import html
import time
import uuid
import logging
# NB: `time` (the stdlib module, imported above) is used for monotonic clocks in
# the click debounce. Import datetime's time CLASS under another name — plain
# `from datetime import time` shadows the module and turns time.monotonic() into
# an AttributeError at the first button click.
from datetime import datetime, date, timedelta, timezone
from datetime import time as clock_time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import sub_store as store
from league_client import get_cached_leagues, draw_to_datetime

log = logging.getLogger(__name__)

STORE_PATH      = os.environ.get("SUBS_STORE_PATH", "subs_store.json")
CLUB_NAME       = os.environ.get("CLUB_NAME", "Curling Club")
TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "-5"))  # America/Chicago default
GRACE_HOURS     = int(os.environ.get("SUBS_GRACE_HOURS", str(store.DEFAULT_GRACE_HOURS)))
# A request with no game date has nothing to expire against — it ages out this
# many days after it was posted instead.
UNDATED_DAYS    = int(os.environ.get("SUBS_UNDATED_DAYS", str(store.DEFAULT_UNDATED_DAYS)))
# How close to game time an unfilled request gets an automatic re-alert (once).
REMINDER_HOURS  = int(os.environ.get("SUBS_REMINDER_HOURS", "24"))
# Sub rosters freeze this many minutes before tip-off — no more adds/removes/claims.
LOCK_MINUTES    = int(os.environ.get("SUBS_LOCK_MINUTES", "30"))
# How close to game time an AUTO-ASSIGNED super sub who still hasn't confirmed gets
# chased (once). Longer than REMINDER_HOURS on purpose: an unconfirmed assignment
# is the one case where the board looks covered and might not be, so it needs to
# surface while there is still time to find someone else.
ACK_HOURS       = int(os.environ.get("SUBS_ACK_HOURS", "48"))
# How long outbound notices to ONE person are held so they arrive as one message.
# A chair posting nine dates one at a time is nine separate assignments, but it is
# ONE thing that happened to the super sub — and nine DMs (or nine @-mentions on
# nine alerts) is how a person mutes the bot. Everything inside this window folds
# into a single message, rebuilt from the store when it goes out. 0 = send at once.
NOTIFY_WINDOW   = int(os.environ.get("SUBS_NOTIFY_WINDOW", "180"))
# Discord caps a message at 25 components: row 0 holds the four verbs, leaving 20
# slots for the game buttons below. A RUN spends only one of them (see open_units),
# so these two caps are no longer the same number — the embed can list far more
# nights than there are buttons, which is exactly what a multi-week run needs.
MAX_BOARD_BUTTONS = 20
MAX_BOARD_GROUPS  = 40   # embed date groups; DESC_BUDGET is the real limit
# How far ahead the board shows in full. A Discord message cannot scroll — an embed is
# a fixed block of text — so the only lever on a season-long run is showing less of it.
# Two weeks is the window in which a sub is actually arranged; everything past it is
# summarised into one line and reachable through "Show all", which sends the whole
# board privately (nothing is hidden, only moved out of the channel's way).
BOARD_HORIZON_DAYS = int(os.environ.get("SUBS_BOARD_DAYS", "14"))
# Several buttons act on shared state and can be impatiently double-tapped before
# the first click visibly resolves. We ignore a repeat click (same user, same
# target) within this window so a double-tap is idempotent: a "Take a spot" toggle
# can't take-then-drop, and a Confirm/Decline can't clobber its own result.
CLICK_DEBOUNCE_SECONDS = 3.0

CID_NEW     = "sub:new"
CID_AVAIL   = "sub:avail"
CID_FILLFOR = "sub:fillfor"
CID_REMOVE  = "sub:remove"
CID_SHOWALL = "sub:showall"
# Per-request one-tap claim/hand-raise button (alert page + board): "sub:take:<rid>".
CID_TAKE_PREFIX = "sub:take:"
# One-tap "I'll cover the whole run" on a multi-week series: "sub:takerun:<series_id>".
CID_TAKE_RUN_PREFIX = "sub:takerun:"
# A run's button that opens the night picker (board + alert page): "sub:pickrun:<sid>".
CID_PICK_RUN_PREFIX = "sub:pickrun:"
# A super sub acknowledging or dropping the dates they were auto-assigned. Keyed on
# the PERSON, not on a batch: their notices are folded into one message, so the
# buttons have to cover everything that message lists.
CID_AUTO_OK   = "sub:autook"
CID_AUTO_DROP = "sub:autodrop"
# "Your league has teams now — which one is your spot on?", keyed by league id.
CID_SET_TEAM_PREFIX  = "sub:setteam:"


def club_now() -> datetime:
    """Current club-local time as a naive datetime (matches stored game_ts)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)


def is_locked(req: dict, *, now: datetime | None = None) -> bool:
    """True once a game is within LOCK_MINUTES of starting (or already underway) — its
    sub roster is frozen and can no longer be changed. Unparseable times never lock."""
    now = now or club_now()
    try:
        return datetime.fromisoformat(req["game_ts"]) <= now + timedelta(minutes=LOCK_MINUTES)
    except (ValueError, KeyError, TypeError):
        return False


# ── Date/time formatting ────────────────────────────────────────────────────


# A league night whose start time the club hasn't settled yet. Expiry, locking
# and sorting all key off a real timestamp, so we park these at the very end of
# their day: the request then lives through the whole draw day instead of dying
# at midnight, and never locks early. No draw starts at 23:59, so the value
# doubles as the marker that the time is still to be confirmed.
TIME_TBC = clock_time(23, 59)


def fmt_when(game_ts: str) -> str:
    """A game's date/time for display. Two non-obvious cases are normal, not
    errors: an empty timestamp (a legacy request posted with no date at all) and
    TIME_TBC (date known, start time not announced yet)."""
    try:
        dt = datetime.fromisoformat(game_ts)
    except (ValueError, TypeError):
        return game_ts or "date TBD"
    if dt.time() == TIME_TBC:
        return f"{dt.strftime('%a %b %-d')} · time TBC"
    return f"{dt.strftime('%a %b %-d')} · {dt.strftime('%-I:%M %p')}"


def fmt_when_short(game_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(game_ts)
    except (ValueError, TypeError):
        return "TBD"
    if dt.time() == TIME_TBC:
        return f"{dt.strftime('%a %-m/%-d')} TBC"
    h = dt.strftime('%-I:%M%p').lower().replace(":00", "")
    return f"{dt.strftime('%a %-m/%-d')} {h}"


def first_name(name: str) -> str:
    return (name or "").split()[0] if name else name


# ── League / game helpers ───────────────────────────────────────────────────

# Admins embed scheduling noise in league titles (e.g. "– Summer 2026 League 2 –
# Begins July 5"). We strip the date/time-ish tokens so the name doesn't echo the
# game date/time we already display. Best-effort across formats — weekday names
# (Sunday/Tuesday/…) are deliberately NOT stripped since they're part of the name.
_MONTHS = (r"(?:January|February|March|April|May|June|July|August|September|October|"
           r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)")
_TITLE_NOISE = [
    re.compile(r"\bBegins\b.*$", re.I),                                    # "Begins July 5" tail
    re.compile(r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?", re.I),               # 9:00 AM, 19:30
    re.compile(r"\b\d{1,2}\s*[ap]\.?m\.?\b", re.I),                        # 9am, 7 pm
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                                  # 2026-07-05
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),                       # 7/5, 07/05/26
    re.compile(rf"\b{_MONTHS}\b\.?\s*\d{{0,2}}(?:st|nd|rd|th)?", re.I),    # July 5, Jul
    re.compile(r"\b(?:Spring|Summer|Fall|Autumn|Winter)\b", re.I),        # season
    re.compile(r"\b(?:19|20)\d{2}\b"),                                    # 2026
]


def clean_title(title: str) -> str:
    """Decode HTML entities and strip admin-embedded date/time noise (seasons,
    years, month-dates, clock times, "Begins …" tails) plus orphaned punctuation,
    so the league name doesn't repeat the game date/time we already show."""
    t = html.unescape(title or "")
    for pat in _TITLE_NOISE:
        t = pat.sub(" ", t)
    t = re.sub(r"[(\[]\s*[)\]]", " ", t)               # drop emptied ()/[] pairs
    t = re.sub(r"\s+", " ", t)                          # collapse whitespace
    t = re.sub(r"(?:\s*[–—\-·,]\s*){2,}", " – ", t)    # collapse separator runs
    return t.strip(" –—-·,")


# "Summer 2026 League 2" tells a sub nothing — after clean_title strips the season
# and year, the bare sequence number ("League 2", "League #3") is pure noise too.
_LEAGUE_SEQ = re.compile(r"[\s–—\-·,]*\bLeagues?\s*#?\s*\d+\s*$", re.I)


def league_name(title: str) -> str:
    """Human league name: clean_title minus a trailing sequence number
    ("Thursday League – Summer 2026 League 3 – Begins August 6" → "Thursday League").
    Never returns empty — falls back to the cleaned title if stripping ate it all."""
    base = clean_title(title)
    stripped = _LEAGUE_SEQ.sub("", base).strip(" –—-·,")
    return stripped or base


def _draw_dates(league: dict) -> list[date]:
    out = []
    for d in league.get("draws", []) or []:
        try:
            out.append(date.fromisoformat(d["date"]))
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(set(out))


# Every league title at this club ends "– Begins September 6" / "– Begins Sept 4".
# clean_title() strips that as noise for DISPLAY, but it's the only machine-readable
# start date a league has before its schedule is posted — which is exactly when we
# need one. Parsed off the RAW title, before clean_title eats it.
_BEGINS_RE = re.compile(rf"\bBegins\b\s*:?\s*({_MONTHS})\.?\s+(\d{{1,2}})", re.I)
_TITLE_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_MONTH_NUM = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def title_start_date(title: str, *, today: date | None = None) -> date | None:
    """The date in a league title's "Begins …" tail, or None.

    The year comes from the season in the same title ("Fall 2026") when it's
    there; otherwise we take whichever year puts the date nearest to now, so a
    January league read in December lands next year rather than eleven months
    ago."""
    raw = html.unescape(title or "")
    m = _BEGINS_RE.search(raw)
    if not m:
        return None
    month = _MONTH_NUM.get(m.group(1)[:3].casefold())
    if not month:
        return None
    day = int(m.group(2))
    ym = _TITLE_YEAR_RE.search(raw)
    today = today or date.today()
    years = [int(ym.group(1))] if ym else [today.year - 1, today.year, today.year + 1]
    best = None
    for y in years:
        try:
            cand = date(y, month, day)
        except ValueError:
            continue                    # e.g. "Begins February 30"
        if best is None or abs((cand - today).days) < abs((best - today).days):
            best = cand
    return best


def league_start_date(league: dict, *, today: date | None = None) -> date | None:
    """When this league starts: its first scheduled draw, else the date in its
    title. The title is all we have for a league whose schedule isn't posted."""
    dates = _draw_dates(league)
    if dates:
        return dates[0]
    return title_start_date(league.get("title", ""), today=today)


def league_date_range(league: dict) -> str:
    """"8/2 – 8/30" across a league's scheduled draws. With no schedule posted,
    falls back to "from 9/6" off the title, so a Fall league is still identifiable
    in a picker that lists several leagues on the same night."""
    dates = _draw_dates(league)
    if not dates:
        start = league_start_date(league)
        return f"from {start.month}/{start.day}" if start else ""
    first, last = dates[0], dates[-1]
    a = f"{first.month}/{first.day}"
    if first == last:
        return a
    return f"{a} – {last.month}/{last.day}"


def league_label(league: dict) -> str:
    """What a league is called everywhere a human reads it: name + run dates, e.g.
    "Sunday Rise & Shine League 8/2 – 8/30". The dates are what tell someone on the
    sub board WHICH Sunday league this is; the admin's "League 2" never did."""
    name = league_name(league.get("title", ""))
    rng = league_date_range(league)
    return f"{name} {rng}".strip() if rng else name


_WEEKDAY_ORDER = {d: i for i, d in enumerate(
    ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"))}


def league_weekday_index(league: dict) -> int:
    """0 = Sunday … 6 = Saturday; 7 when the day can't be determined (sorts last).
    Prefers the league's own `day`, then a draw's parsed weekday, then the first
    draw's date."""
    d = (league.get("day") or "").strip().casefold()
    if d in _WEEKDAY_ORDER:
        return _WEEKDAY_ORDER[d]
    for dr in league.get("draws", []) or []:
        wd = (dr.get("weekday") or "").strip().casefold()
        if wd in _WEEKDAY_ORDER:
            return _WEEKDAY_ORDER[wd]
    start = league_start_date(league)
    if start:
        return (start.weekday() + 1) % 7      # date.weekday() is Mon=0; we want Sun=0
    return 7


def league_sort_key(league: dict):
    """Sort order for every league list a member sees: day of week Sun→Sat, then
    start date within that day, then name. Leagues on the same night land together,
    earliest-starting first — so "which Sunday league is this?" is answered by
    position as well as by the label."""
    start = league_start_date(league)
    return (league_weekday_index(league),
            start or date.max,
            league_name(league.get("title", "")).casefold())


def league_sub_label(league: dict) -> str:
    """Secondary line for a league picker: when it's played."""
    bits = [x for x in ((league.get("day") or ""), (league.get("time") or "")) if x]
    return " · ".join(bits)


def stored_league(text: str) -> str:
    """Display a league name that was already labelled when it was stored. Do NOT
    run clean_title over these — it would strip the very dates league_label added."""
    return html.unescape(text or "").strip()


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.I)


def _parse_clock(text: str) -> clock_time | None:
    """'7:45 pm' / '9am' / '9:00 a.m.' -> time. None if it isn't a clock time."""
    m = _CLOCK_RE.match(text or "")
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    return clock_time(hour, int(m.group(2) or 0))


def _league_time(league: dict) -> clock_time | None:
    """The league's start time, from its `time` field or, failing that, whatever
    time its known draws are at. None if we can't tell."""
    t = _parse_clock(league.get("time") or "")
    if t is not None:
        return t
    for d in league.get("draws", []) or []:
        t = _parse_clock(d.get("time") or "")
        if t is not None:
            return t
    return None


def league_games(league: dict, now: datetime) -> list[dict]:
    """
    All upcoming draws for a league (from today onward). Each item:
    {iso, label, dt}. De-duped and sorted by time.
    """
    today = now.date()
    out: list[dict] = []
    for d in league.get("draws", []):
        try:
            dd = date.fromisoformat(d["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if dd < today:
            continue
        dt = draw_to_datetime(d)
        if dt is None or (dt.hour == 0 and dt.minute == 0):
            # draw_to_datetime falls back to midnight when a row's time is
            # missing or unparseable. No club draws at midnight, so read that as
            # "time unknown" and use the league's start time instead — otherwise
            # the picker offers "12:00 AM" and the request expires a day early.
            dt = datetime.combine(dd, _league_time(league) or time(0, 0))
        dt = dt.replace(second=0, microsecond=0)
        out.append({"iso": dt.isoformat(), "label": fmt_when(dt.isoformat()), "dt": dt})
    out.sort(key=lambda g: g["dt"])
    seen, uniq = set(), []
    for g in out:
        if g["iso"] in seen:
            continue
        seen.add(g["iso"])
        g["projected"] = False
        uniq.append(g)
    return uniq


# How many nights to offer when a league's schedule isn't published yet. Discord
# caps a select at 25 options; 8 weeks is a season and leaves room to spare.
PROJECTED_NIGHTS = 8

def projected_games(league: dict, now: datetime, *, start: date | None = None,
                    count: int = PROJECTED_NIGHTS) -> list[dict]:
    """Upcoming *league nights* worked out from the league's day and start date,
    for the stretch before the chair posts a schedule. When you need a sub is the
    whole point of a request, so an unscheduled league still has to offer real
    dates: weekly on its own night, starting from the league's own start date, so
    a date that isn't a league night can't be picked.

    The START TIME may legitimately be unknown — the club itself sometimes hasn't
    settled it ("either 6pm or 7pm", per the Fall over/under league page). We
    don't guess it (a neighbouring league's time would be flat wrong: Sunday
    morning is 9am, Sunday night is not) and we don't let it block the date,
    which is the part people actually need. Those entries carry TIME_TBC."""
    idx = league_weekday_index(league)
    if idx > 6:
        return []                      # no idea what night this league plays
    t = _league_time(league)
    time_known = t is not None
    target = (idx - 1) % 7             # our Sun=0…Sat=6 → date.weekday()'s Mon=0…Sun=6
    begins = start if start is not None else league_start_date(league, today=now.date())
    d = max(begins or now.date(), now.date())
    d += timedelta(days=(target - d.weekday()) % 7)
    out: list[dict] = []
    while len(out) < count:
        dt = datetime.combine(d, t or TIME_TBC)
        d += timedelta(days=7)
        if dt <= now:
            continue                   # tonight's draw already started
        out.append({"iso": dt.isoformat(), "label": fmt_when(dt.isoformat()),
                    "dt": dt, "projected": True, "time_known": time_known})
    return out


def league_is_over(league: dict, now: datetime) -> bool:
    """True when every draw this league has is in the past. Finished seasons sit
    in the cache for weeks without an `ended` flag; they're dead ends in a picker
    (nothing left to sub for), so we hide them. A league with NO draws is not
    over — that's a season whose schedule simply hasn't been posted."""
    dates = _draw_dates(league)
    return bool(dates) and dates[-1] < now.date()


def game_options(league: dict, now: datetime, *, cap: int = 25) -> list[dict]:
    """What the game picker offers.

    A posted schedule always wins — we never invent dates that contradict one,
    even to extend past its last draw. Projected nights are strictly the
    no-schedule-yet case, which is the one that used to leave the picker empty
    and the request unpostable. A league whose draws have ALL been played is a
    finished season, not an unscheduled one: it gets nothing, so old leagues
    lingering in the cache without an `ended` flag can't be picked."""
    real = league_games(league, now)
    if real:
        return real[:cap]
    if _draw_dates(league) or league.get("fetch_failed"):
        # Either the schedule exists and it's all in the past, or we couldn't read
        # the league's page at all. "No draws" only means "not scheduled yet" when
        # we actually managed to look — otherwise a site outage would have us
        # inventing league nights out of nothing.
        return []
    return projected_games(league, now)[:cap]


# ── Long-term ("Ben has Tuesdays for 8 weeks") sub runs ─────────────────────
# A run is NOT a new kind of record. It is N ordinary requests, one per league
# night, sharing a series_id — so each night keeps its own status, claim button,
# roster, lock and expiry, and week 3 can fall through without disturbing the
# other seven. The series_id buys exactly two things: one alert for the whole run
# instead of one per night, and a single tap that covers all of it.

def fmt_run(isos: list[str]) -> str:
    """Several dates in one line. One reads as itself. Several read as a span, a count
    and ONE start time — a multi-date posting is usually the same slot each week, so
    repeating "7:45 pm" at both ends is noise, and the count ("8 dates") is the part
    someone actually needs to check before agreeing to it.

    "dates", never "nights": plenty of league draws are daytime hours."""
    if not isos:
        return "no dates"
    if len(isos) == 1:
        return fmt_when(isos[0])
    try:
        a = datetime.fromisoformat(isos[0])
        b = datetime.fromisoformat(isos[-1])
    except (ValueError, TypeError):
        return f"{fmt_when(isos[0])} → {fmt_when(isos[-1])} · {len(isos)} dates"
    span = f"{a.strftime('%a %-m/%-d')} → {b.strftime('%a %-m/%-d')} · {len(isos)} dates"
    # Nights can carry different times (a run laid over a rescheduled draw), so only
    # claim a single start time when every night really shares one.
    times = {i[11:] for i in isos}
    if len(times) > 1:
        return span
    if a.time() == TIME_TBC:
        return f"{span} · time TBC"
    return f"{span} · {a.strftime('%-I:%M %p')}"


# ── Board rendering ─────────────────────────────────────────────────────────

BOARD_TITLE = f"Subs Board — {CLUB_NAME}"
# Discord's embed-description cap is 4096; leave headroom for the overflow line.
DESC_BUDGET = 3800


def _game_key(iso: str) -> str:
    """Game timestamp normalized to the minute, for matching availability to requests."""
    try:
        return datetime.fromisoformat(iso).replace(second=0, microsecond=0).isoformat()
    except (ValueError, TypeError):
        return iso or ""


def _before(iso: str, floor: datetime) -> bool:
    """True if `iso` is a real timestamp earlier than `floor`. An empty or
    unparseable value is NOT "before" — undated items are handled on their own
    terms and unreadable ones are never silently dropped."""
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso) < floor
    except (ValueError, TypeError):
        return False


def _req_icon(req: dict) -> str:
    """Traffic light by urgency: 🔴 nobody yet, 🟡 partly covered, 🟢 fully covered."""
    needed = int(req["spots_needed"])
    covered = needed - store.open_spots(req)   # filled + pending
    if covered <= 0:
        return "🔴"
    if covered < needed:
        return "🟡"
    return "🟢"


def _embed_color(reqs: list[dict]) -> int:
    """Bar color reflects the most urgent open request (red beats yellow beats green)."""
    icons = {_req_icon(r) for r in reqs}
    if "🔴" in icons:
        return 0xE03A3A
    if "🟡" in icons:
        return 0xE6A700
    if reqs:
        return 0x2FA84F
    return 0x1A6BB5


INDENT = "\u00a0\u00a0\u00a0"  # non-breaking spaces: Discord keeps these, so lines indent under the date


def _req_for(req: dict) -> str:
    """Who the spot is for: the team when one is named, otherwise the person who
    asked (teams often aren't set until a day or two before the first draw)."""
    if req.get("team"):
        return f"Team {req['team']}"
    who = first_name(req.get("requester_name", ""))
    return f"{who}'s spot" if who else "Sub"


def _req_status_line(req: dict) -> str:
    needed = int(req["spots_needed"])
    covered = needed - store.open_spots(req)
    # An auto-assigned super sub who hasn't acknowledged yet is shown as such: the
    # spot IS covered, but nobody has heard from them, and a board that hides that
    # is how a team turns up three-handed.
    names = [f["name"] + (" (unconfirmed)" if f.get("auto") and not f.get("confirmed") else "")
             for f in req.get("filled", [])]
    names += [f"{p['name']} (pending)" for p in req.get("pending", [])]
    who = ", ".join(names) if names else "nobody yet"
    return f"{INDENT}{_req_icon(req)} {_req_for(req)} — {covered}/{needed} · {who}"


def _available_for_group(state: dict, grp: dict, key: str) -> list[str]:
    """Who's genuinely free for this time slot: anyone whose availability covers the
    game and who isn't already tied up in it. Being tied up means subbing one of the
    slot's requests (filled or pending) — or having opened one, since a requester
    needs a sub precisely because they can't play. Assigned subs drop off this list
    and show by name on their spot line instead."""
    tied_up = set()
    for r in grp["reqs"]:
        tied_up.add(r.get("requester_id"))
        for m in r.get("filled", []) + r.get("pending", []):
            tied_up.add(m["user_id"])

    names = []
    for a in state.get("availability", []):
        if a["user_id"] in tied_up:
            continue
        games = a.get("games") or []
        lid = str(a.get("league_id") or "")
        if grp["reqs"]:
            # Covers the slot if it matches any request here: same league, and either
            # this specific game or an "any game in this league" offer.
            covers = any(
                (not lid or str(r.get("league_id") or "") == lid)
                and (not games or any(_same_game(r.get("game_ts", ""), g) for g in games))
                for r in grp["reqs"]
            )
        else:
            # No request opened for this game yet — only an explicit game listing counts.
            covers = any(_game_key(g) == key for g in games)
        if covers:
            names.append(a["name"])
    return sorted(set(names))


def board_horizon(horizon_days: int | None) -> datetime | None:
    """The last moment the board shows in full, or None for "show everything"."""
    if horizon_days is None:
        return None
    return store.day_floor(club_now()) + timedelta(days=horizon_days)


def _beyond(iso: str, horizon: datetime | None) -> bool:
    """True if this dated item falls past the board's horizon. Undated and
    unparseable items are never "beyond" — they're handled on their own terms and
    must never be silently dropped."""
    if horizon is None or not iso:
        return False
    try:
        return datetime.fromisoformat(iso) > horizon
    except (ValueError, TypeError):
        return False


def build_embed(state: dict, *, horizon_days: int | None = BOARD_HORIZON_DAYS) -> discord.Embed:
    """One combined, date-ordered board. A game appears if it has a request OR if
    anyone is available for it. Under each date: the sub spots (traffic-light status,
    with the names of whoever is in), then the available subs not yet assigned.
    General (any-time) availability is summarized at the bottom.

    Only the next `horizon_days` are listed. A season-long run is 16 nights, and a
    Discord embed cannot scroll — so the channel copy stays the size of a decision
    people can act on this fortnight, and everything past it condenses to one line
    pointing at **Show all** (`horizon_days=None`, sent privately)."""
    reqs = store.requests_sorted(state)
    horizon = board_horizon(horizon_days)
    e = discord.Embed(title=BOARD_TITLE, color=_embed_color(reqs))

    # Date groups come from requests AND from game-specific availability, so a game
    # with willing subs shows up even before anyone opens a request for it.
    groups: dict[str, dict] = {}
    for r in reqs:
        ts = r.get("game_ts", "")
        if ts:
            k = _game_key(ts)
            groups.setdefault(k, {"iso": ts, "label": fmt_when(ts), "reqs": []})["reqs"].append(r)
        else:
            # Undated requests group per league rather than into one anonymous
            # "date TBD" pile — the league is the only context they carry, and
            # it's what tells a would-be sub whether it's their night.
            k = f"tbd:{r.get('league_id') or ''}"
            lg = stored_league(r.get("league", ""))
            groups.setdefault(k, {"iso": "", "reqs": [],
                                  "label": "Date TBD" + (f" · {lg}" if lg else "")})["reqs"].append(r)
    for a in state.get("availability", []):
        for iso in (a.get("games") or []):
            groups.setdefault(_game_key(iso), {"iso": iso, "label": fmt_when(iso), "reqs": []})

    # Hard today-forward floor. store.expire() already prunes past dates, but it
    # only runs every 15 minutes and only mutates what it can parse — this makes
    # the board itself incapable of showing yesterday. Undated ("Date TBD") groups
    # carry no date to be behind, so they're never floored out.
    floor = store.day_floor(club_now())
    for k in [k for k, g in groups.items() if _before(g.get("iso"), floor)]:
        del groups[k]

    def _sort_key(k: str):
        try:
            return (0, datetime.fromisoformat(groups[k]["iso"]), "")
        except (ValueError, TypeError):
            return (1, datetime.max, k)   # undated sinks below every real date

    order = sorted(groups, key=_sort_key)
    # Past the horizon isn't dropped, it's deferred: counted, dated, and one tap away.
    later = [k for k in order if _beyond(groups[k].get("iso"), horizon)]
    order = [k for k in order if k not in set(later)]

    blocks, used, hidden = [], 0, max(0, len(order) - MAX_BOARD_GROUPS)
    for k in order[:MAX_BOARD_GROUPS]:
        grp = groups[k]
        lines = [f"**{grp['label']}**"]
        for r in grp["reqs"]:
            lines.append(_req_status_line(r))
        free = _available_for_group(state, grp, k)
        if free:
            lines.append(f"{INDENT}available: {', '.join(free)}")
        block = "\n".join(lines)
        # Discord hard-rejects an embed description over 4096 chars, and a single
        # 8-week run can carry the board to twenty date groups. Trim from the
        # BOTTOM (the furthest-out nights) and say so, rather than letting the whole
        # board fail to render — the soonest games are the ones people need.
        if used + len(block) + 2 > DESC_BUDGET:
            hidden += 1
            continue
        blocks.append(block)
        used += len(block) + 2

    # One tail line covers both reasons something isn't listed — past the horizon, or
    # past what Discord will render — so the count is always the true remainder.
    rest = hidden + len(later)
    if rest:
        last = groups[(later or order)[-1]].get("iso")
        through = f" through {fmt_when_short(last)}" if last else ""
        tail = f"…and **{rest}** more game{'s' if rest != 1 else ''}{through}"
        blocks.append(f"{tail} — tap **Show all**." if later else f"{tail}.")

    if blocks:
        e.description = "\n\n".join(blocks)
    else:
        # No dated games — but an any-time roster below may still have people on it.
        e.description = ("No sub requests right now.\n\nUse **Need a sub** to post one, "
                         "or **I'm free** to list your availability.")

    # Bottom: people available with no specific game (any time), grouped by league.
    anytime: dict[str, dict] = {}
    aorder: list[str] = []
    for a in state.get("availability", []):
        if a.get("games"):
            continue  # game-specific availability shows on the "available:" lines above
        lkey = str(a.get("league_id") or "")
        if lkey not in anytime:
            anytime[lkey] = {"title": stored_league(a.get("league", "")) or "Any league", "names": []}
            aorder.append(lkey)
        anytime[lkey]["names"].append(a["name"])
    if aorder:
        rows = [f"**{_truncate(anytime[l]['title'], 40)}** — {', '.join(sorted(anytime[l]['names']))}"
                for l in aorder]
        e.add_field(name="Available any time", value="\n".join(rows)[:1024], inline=False)

    foot = "🔴 none · 🟡 partial · 🟢 filled — tap a game button below to take a spot"
    if horizon_days is None:
        foot += " · showing everything"
    elif later:
        foot += f" · showing the next {horizon_days} days"
    e.set_footer(text=foot)
    return e


def open_units(state: dict, *, now: datetime | None = None) -> list[dict]:
    """Open nights grouped by the posting they came from — ONLY for the BULK pickers
    (Fill-for, and the alert page for a fresh post), never for the board.

    A group here is a convenience for doing one thing to several nights at once
    ("Ben said he'd cover the rest of Tuesdays"), NOT a standing arrangement: nothing
    is stored per-group, and the group is recomputed from whatever is still open every
    single time. Drop a sub from one night and it simply rejoins the group as open;
    fill the rest and the group shrinks to a single game (kind flips to "one").
    Each unit is {kind: "run"|"one", sid, reqs}, ordered by its soonest open night.
    Locked and filled nights are already excluded, so a unit is always actionable."""
    open_reqs = [r for r in store.requests_sorted(state)
                 if store.open_spots(r) > 0 and not is_locked(r, now=now)]
    units, seen = [], set()
    for r in open_reqs:
        sid = str(r.get("series_id") or "")
        if not sid:
            units.append({"kind": "one", "sid": "", "reqs": [r]})
            continue
        if sid in seen:
            continue
        seen.add(sid)
        run = [x for x in open_reqs if str(x.get("series_id") or "") == sid]
        # A run whose other nights have all filled or locked is just a single game now.
        units.append({"kind": "run" if len(run) > 1 else "one", "sid": sid, "reqs": run})
    return units


def unit_label(u: dict) -> str:
    """Button/option text for a unit. A run leads with WHO, since its nights are the
    same slot every week and the dates are right there in the board text above."""
    r = u["reqs"][0]
    who = r["team"] if r.get("team") else first_name(r.get("requester_name", ""))
    if u["kind"] == "run":
        return _truncate(f"{who} · {len(u['reqs'])} dates", 80)
    when = fmt_when_short(r["game_ts"]) if r.get("game_ts") else "TBD"
    return _truncate(f"{when} {who}", 80)


def build_view(state: dict, *, horizon_days: int | None = BOARD_HORIZON_DAYS) -> discord.ui.View:
    """Row 0 = the verbs; below that, ONE ONE-TAP BUTTON PER OPEN NIGHT, matching the
    text above it line for line.

    Deliberately not per-run. A multi-night post is a bulk INPUT convenience, not a
    standing arrangement: the moment Bruce can't make week 3, "his run" is a fiction and
    the other four nights carry on unchanged. Nothing downstream — dropping a sub,
    cancelling a night, expiry — works on runs, so the board shouldn't either. The
    horizon, not grouping, is what keeps this list short.

    Buttons obey the same horizon as the text: a game the board isn't listing must not
    have a button, or people would be claiming a night they can't see."""
    view = discord.ui.View(timeout=None)
    horizon = board_horizon(horizon_days)
    open_reqs = [r for r in store.requests_sorted(state)
                 if store.open_spots(r) > 0 and not is_locked(r)]
    shown = [r for r in open_reqs if not _beyond(r.get("game_ts", ""), horizon)]
    view.add_item(NewRequestButton())   # ➕ Need a sub
    view.add_item(AvailableButton())    # 🙋 I'm free
    view.add_item(FillForButton())      # ✍️ Fill for someone
    view.add_item(RemoveButton())       # ✖️ Remove
    # Only offered when something is actually out of sight — a button that reveals
    # nothing new is worse than no button.
    if len(shown) < len(open_reqs) or _has_later(state, horizon):
        view.add_item(ShowAllButton())
    for i, r in enumerate(shown[:MAX_BOARD_BUTTONS]):   # rows 1–4, 5 each
        who = r["team"] if r.get("team") else first_name(r.get("requester_name", ""))
        when = fmt_when_short(r["game_ts"]) if r.get("game_ts") else "TBD"
        view.add_item(PageClaimButton(r["id"], label=_truncate(f"{when} {who}", 80),
                                      style=discord.ButtonStyle.success, row=1 + i // 5))
    return view


def _has_later(state: dict, horizon: datetime | None) -> bool:
    """Anything dated past the horizon — a request OR a game someone has listed
    availability for. Availability counts: a night with willing subs and no request yet
    is a real row of the full board."""
    if horizon is None:
        return False
    if any(_beyond(r.get("game_ts", ""), horizon) for r in state.get("requests", [])):
        return True
    return any(_beyond(g, horizon)
               for a in state.get("availability", []) for g in (a.get("games") or []))


# ── Persistent buttons (DynamicItem — survive restarts) ─────────────────────

class NewRequestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:new"):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Need a sub", emoji="➕",
            style=discord.ButtonStyle.success, custom_id=CID_NEW, row=0,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        # thinking=True shows an ephemeral "curlbot is thinking…" right away while we
        # load leagues, then we edit that placeholder into the flow.
        await interaction.response.defer(thinking=True, ephemeral=True)
        leagues = await cog.get_leagues()
        if not leagues:
            await interaction.edit_original_response(
                content="Couldn't load the league list just now — try again in a moment.")
            return
        view = NeedSubFlowView(leagues, cog.state, interaction.user.id)
        view.message = await interaction.edit_original_response(content=view.prompt(), view=view)


class AvailableButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:avail"):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="I'm free", emoji="🙋",
            style=discord.ButtonStyle.primary, custom_id=CID_AVAIL, row=0,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        # thinking=True shows an ephemeral "curlbot is thinking…" right away while we
        # load leagues, then we edit that placeholder into the flow.
        await interaction.response.defer(thinking=True, ephemeral=True)
        leagues = await cog.get_leagues()
        if not leagues:
            await interaction.edit_original_response(
                content="Couldn't load the league list just now — try again in a moment.")
            return
        view = AvailFlowView(leagues, interaction.user.id, cog.state)
        view.message = await interaction.edit_original_response(content=view.prompt(), view=view)


class ShowAllButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:showall"):
    """The channel board lists only the next couple of weeks — a Discord embed cannot
    scroll, and a season of league nights would bury the games people can still do
    something about. This sends the WHOLE board privately, buttons and all, so nothing
    is ever out of reach, only out of the channel's way."""

    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Show all", emoji="📋",
            style=discord.ButtonStyle.secondary, custom_id=CID_SHOWALL, row=0))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        await interaction.response.send_message(
            content="The whole board, every date (only you can see this):",
            embed=build_embed(cog.state, horizon_days=None),
            view=build_view(cog.state, horizon_days=None),
            ephemeral=True)


class FillForButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"sub:fillfor(?::(?P<kind>run|one):(?P<ident>[0-9a-f]+))?"):
    """Mark someone ELSE into an open spot (offline sync) — they told you they'd cover
    it, so you record it for them. Covers several dates of one posting in a single go:
    "Ben said he'll do the rest of Tuesdays" is one thing that happened.

    Two placements, one class. On the BOARD it's bare and you pick the game. On an
    ALERT it carries that posting's id in its custom_id and opens pre-aimed at it —
    the alert is already about one specific ask, so making someone re-find it in a
    dropdown is a step for nothing. This is the only way to name a sub up front now
    that posting no longer asks: post the dates, then say who's covering them."""

    def __init__(self, key: str = "", *, style: discord.ButtonStyle = discord.ButtonStyle.success):
        self.key = key or ""
        suffix = f":{self.key}" if self.key else ""
        super().__init__(discord.ui.Button(
            label="Fill for someone", emoji="➕",
            style=style, custom_id=f"{CID_FILLFOR}{suffix}", row=0))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        kind, ident = match["kind"], match["ident"]
        return cls(f"{kind}:{ident}" if kind and ident else "")

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        view = FillForView(cog.state, key=self.key)
        if not view.units():
            await interaction.response.send_message("No open spots to fill right now.", ephemeral=True)
            return
        await interaction.response.send_message(view.prompt(), view=view, ephemeral=True)


def unit_key(u: dict) -> str:
    """Stable select-option value for a unit — the series for a run, the request for a
    single game. Prefixed so the two id spaces can never collide."""
    return f"run:{u['sid']}" if u["kind"] == "run" else f"one:{u['reqs'][0]['id']}"


class FillForView(discord.ui.View):
    """Pick the game (or run) → for a run, which nights → who's covering it."""

    def __init__(self, state: dict, key: str = ""):
        super().__init__(timeout=300)
        self.state = state
        # Pre-aimed when opened from an alert; a key that no longer matches anything
        # open (the spot filled while the alert sat there) just falls back to the picker.
        self.key: str | None = key or None
        self.rids: list[str] = []         # dates to fill (all of the posting, by default)
        self.member = None                # who's covering them; committed by the button
        self.submitted = False            # one-shot guard on the submit button
        self.on_unit_change()
        self.build()

    def units(self) -> list[dict]:
        return open_units(self.state)

    def unit(self) -> dict | None:
        return next((u for u in self.units() if unit_key(u) == self.key), None)

    def on_unit_change(self):
        u = self.unit()
        # Default to EVERY date of the posting — the same "all ticked" default the
        # self-serve picker uses, so both paths behave identically.
        self.rids = [r["id"] for r in u["reqs"]] if u else []

    def build(self) -> "FillForView":
        self.clear_items()
        self.add_item(FillForPick(self.units(), self.key, row=0))
        u = self.unit()
        if u:
            if u["kind"] == "run":
                self.add_item(NightSelect(u["reqs"], self.rids, row=1,
                                          placeholder="Dates they're covering…"))
            self.add_item(FillForMemberSelect(self.member, row=2))
            self.add_item(FillForSubmitButton(self.member, len(self.rids), row=3))
        return self

    def prompt(self) -> str:
        u = self.unit()
        if not u:
            return "**Fill for someone** — pick the game, then choose the teammate to mark in:"
        who = f"**{self.member.display_name}**" if self.member is not None else "them"
        if u["kind"] == "run":
            total, picked = len(u["reqs"]), len(self.rids)
            # Track the actual selection — claiming "all 4 are ticked" after two were
            # unticked is the kind of small lie that makes people distrust the form.
            which = (f"All {total} dates are ticked — untick any they can't make"
                     if picked == total else
                     f"**{picked} of {total}** dates ticked")
            head = (f"**Fill for someone** · {_req_for(u['reqs'][0])} · "
                    f"{fmt_run([r['game_ts'] for r in u['reqs']])}\n"
                    f"{which}, then choose who's covering them.")
        else:
            head = (f"**Fill for someone** · {fmt_when(u['reqs'][0]['game_ts'])} — "
                    "choose the teammate to mark in.")
        # Nothing happens until the button: picking a name by mistake used to BE the
        # action, and there is no undo for it beyond ➖ Remove.
        if self.member is None:
            return head + "\nNothing is recorded until you press the green button."
        n = len(self.rids)
        return head + (f"\nReady: {who} on **{n} date{'s' if n != 1 else ''}**. "
                       "Press the green button to record it.")

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class FillForPick(discord.ui.Select):
    """One option per open UNIT, so an eight-week run is one line here rather than
    eight near-identical ones burying every other game."""

    def __init__(self, units: list[dict], selected, row: int = 0):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(unit_label(u), 100),
                value=unit_key(u),
                description=_truncate(
                    (f"{fmt_run([r['game_ts'] for r in u['reqs']])} · "
                     f"{sum(store.open_spots(r) for r in u['reqs'])} open")
                    if u["kind"] == "run"
                    else f"{store.open_spots(u['reqs'][0])} open", 100),
                default=(unit_key(u) == selected),
            )
            for u in units[:25]
        ])
        if not opts:
            opts = [discord.SelectOption(label="No open spots right now", value="__none__")]
        super().__init__(placeholder="Which game…", min_values=1, max_values=1,
                         options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        self.view.key = self.values[0]
        self.view.on_unit_change()
        await self.view.refresh(interaction)


class FillForMemberSelect(discord.ui.UserSelect):
    """Picks WHO — and only records the choice. Committing is the button below.

    This select used to fire the fill the instant a name was touched, so a mis-tap
    put the wrong person on someone else's game with no undo short of ➖ Remove. Every
    other flow in this cog ends on an explicit button; this one now matches."""

    def __init__(self, selected=None, row: int = 2):
        # Show who's currently picked, so a rebuilt view doesn't look empty and invite
        # a second, accidental pick. (discord.py >= 2.4.)
        defaults = ([discord.SelectDefaultValue.from_user(selected)]
                    if selected is not None else [])
        super().__init__(
            placeholder=("Change who's covering it…" if selected is not None
                         else "Choose the teammate to mark in…"),
            min_values=1, max_values=1, row=row, default_values=defaults)

    async def callback(self, interaction: discord.Interaction):
        self.view.member = self.values[0]
        await self.view.refresh(interaction)


class FillForSubmitButton(discord.ui.Button):
    def __init__(self, member=None, count: int = 0, row: int = 3):
        if member is None:
            label = "Mark them in"
        else:
            who = first_name(member.display_name) or member.display_name
            label = (f"Mark {who} in for {count} dates" if count > 1
                     else f"Mark {who} in")
        super().__init__(label=_truncate(label, 80), emoji="✅",
                         style=discord.ButtonStyle.success, row=row,
                         disabled=member is None)

    async def callback(self, interaction: discord.Interaction):
        view: "FillForView" = self.view
        # One-shot guard, same as PostNeedButton: filling isn't idempotent and an
        # impatient double-tap must not run twice. Set it before the first await.
        if view.submitted:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        view.submitted = True
        await interaction.response.defer()

        cog: "Subs" = interaction.client.get_cog("Subs")
        member, rids = view.member, list(view.rids)
        if member is None or not rids:
            view.submitted = False
            await interaction.edit_original_response(
                content=("Pick a date and a person first.\n\n" + view.prompt()),
                view=view.build())
            return
        if cog._is_repeat_click(cog._click_cooldown,
                                ("fillfor", interaction.user.id, tuple(sorted(rids)), member.id)):
            return
        filled, skipped, why = await cog.fill_nights_for(
            interaction.user, rids, member, interaction.channel)
        if filled:
            tail = (f" ({skipped} skipped — already covered, locked, or their own request.)"
                    if skipped else "")
            msg = (f"✅  Marked **{member.display_name}** in for **{fmt_run(filled)}** — "
                   f"they and the requester were notified.{tail}")
        else:
            msg = {
                "already": f"**{member.display_name}** is already on all of that.",
                "requester": "That's the requester — they can't sub their own request.",
                "full": "No open spots left there.",
                "locked": "That starts too soon — the roster's locked.",
                "closed": "That's no longer on the board.",
            }.get(why, "Nothing there to fill.")
        await interaction.edit_original_response(content=msg, view=None)


# ── Remove (click a name → confirm; cancel a request; clear availability) ─────

def _all_committed_subs(state: dict) -> list[tuple]:
    """Every listed sub across all requests as (rid, req, member-dict). Skips games
    whose roster has locked (within LOCK_MINUTES of start) — those can't be changed."""
    out = []
    for r in store.requests_sorted(state):
        if is_locked(r):
            continue
        for m in r.get("filled", []) + r.get("pending", []):
            out.append((r["id"], r, m))
    return out


class RemoveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:remove"):
    """Remove a sub (click a name → confirm), cancel a request you opened, or clear
    your own availability — all in one place."""
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Remove", emoji="➖",
            style=discord.ButtonStyle.danger, custom_id=CID_REMOVE, row=0))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        view = RemoveHomeView(cog.state, interaction.user.id)
        if not view.children:
            await interaction.response.send_message(
                "Nothing to remove right now — no subs, requests, or availability listed.",
                ephemeral=True)
            return
        await interaction.response.send_message(
            "**Remove** — cancel a sub (you'll confirm), cancel a request you opened, "
            "or clear your availability:", view=view, ephemeral=True)


class RemoveHomeView(discord.ui.View):
    def __init__(self, state: dict, uid: int):
        super().__init__(timeout=180)
        row = 0
        subs = _all_committed_subs(state)
        if subs:
            self.add_item(RemoveSubSelect(subs, row=row)); row += 1
        my_reqs = _my_requests(state, uid)
        if my_reqs:
            self.add_item(CancelRequestSelect(my_reqs, row=row)); row += 1
        my_avail = [a for a in state.get("availability", []) if a.get("user_id") == uid]
        if my_avail:
            self.add_item(RemoveAvailSelect(my_avail, row=row)); row += 1


class RemoveSubSelect(discord.ui.Select):
    def __init__(self, subs: list[tuple], row: int = 0):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(m["name"], 100),
                value=f"{rid}:{m['user_id']}",
                description=_truncate(f"{fmt_when_short(r['game_ts'])} · {_req_for(r)}", 100),
            )
            for (rid, r, m) in sorted(subs, key=lambda t: (t[2]["name"] or "").casefold())[:25]
        ])
        super().__init__(placeholder="Remove a sub (click a name)…",
                         min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        rid, _, uid_s = self.values[0].partition(":")
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, rid)
        if not req:
            await interaction.response.edit_message(content="That request is no longer on the board.", view=None)
            return
        target = int(uid_s)
        name = next((m["name"] for m in (req.get("filled", []) + req.get("pending", []))
                     if m["user_id"] == target), "this sub")
        await interaction.response.edit_message(
            content=f"Remove **{name}** from **{fmt_when(req['game_ts'])}**?",
            view=ConfirmRemoveSubView(rid, target, name))


class ConfirmRemoveSubView(discord.ui.View):
    def __init__(self, rid: str, target: int, name: str):
        super().__init__(timeout=120)
        self.rid = rid
        self.target = target
        self.name = name

    @discord.ui.button(label="Remove", emoji="✖️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown, ("rmsub", interaction.user.id, self.rid, self.target)):
            return
        result, req = await cog.remove_sub_by_anyone(interaction.user, self.rid, self.target, interaction.channel)
        when = fmt_when(req["game_ts"]) if req else "that game"
        if result == "removed":
            msg = f"✖️  Removed **{self.name}** from **{when}** — they and the requester were notified."
        elif result == "absent":
            msg = "They were already off that spot."
        elif result == "locked":
            msg = f"**{when}** starts too soon — the roster's locked."
        else:
            msg = "That request is no longer on the board."
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Okay — left them on.", view=None)


class CancelRequestSelect(discord.ui.Select):
    def __init__(self, reqs: list[dict], row: int = 1):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(f"{fmt_when_short(r['game_ts'])} · {_req_for(r)}", 100),
                value=r["id"],
                description=_truncate(f"{store.open_spots(r)} open · cancel this request", 100),
            )
            for r in reqs[:25]
        ])
        super().__init__(placeholder="Cancel a request you opened…",
                         min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.values[0])
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.response.edit_message(content="That request is no longer yours to cancel.", view=None)
            return
        await interaction.response.edit_message(
            content=f"Cancel your request for **{fmt_when(req['game_ts'])}**? Any subs on it will be told.",
            view=ConfirmCancelView(req["id"]))


class ConfirmCancelView(discord.ui.View):
    def __init__(self, rid: str):
        super().__init__(timeout=120)
        self.rid = rid

    @discord.ui.button(label="Cancel request", emoji="✖️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.rid)
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.edit_original_response(content="That request is no longer yours to cancel.", view=None)
            return
        if cog._is_repeat_click(cog._click_cooldown, ("cancelreq", interaction.user.id, self.rid)):
            return
        await cog.close_request(self.rid, interaction.channel)
        await interaction.edit_original_response(content="✖️  Request cancelled and removed from the board.", view=None)

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Okay — kept the request.", view=None)


# ── Shared selects for the league/game flows ────────────────────────────────

def _unique_options(opts: list[discord.SelectOption]) -> list[discord.SelectOption]:
    """Drop options whose value repeats, keeping the first. Discord rejects a
    Select whose options share a value (error 50035: "option value is already
    used"), which would otherwise fail the whole message render."""
    seen, out = set(), []
    for o in opts:
        if o.value in seen:
            continue
        seen.add(o.value)
        out.append(o)
    return out


class LeagueSelect(discord.ui.Select):
    def __init__(self, leagues: list[dict], selected, row: int = 0):
        ordered = sorted(leagues, key=league_sort_key)
        opts = [
            discord.SelectOption(
                label=_truncate(league_label(l), 100),
                value=str(l["id"]),
                description=(league_sub_label(l) or None),
                default=(str(l["id"]) == str(selected)),
            )
            for l in ordered[:25]
        ] or [discord.SelectOption(label="No active leagues", value="__none__")]
        super().__init__(placeholder="Choose a league…", min_values=1, max_values=1,
                         options=_unique_options(opts), row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        self.view.league_id = self.values[0]
        self.view.on_league_change()
        await self.view.refresh(interaction)


NO_TEAM = "__noteam__"


class TeamSelect(discord.ui.Select):
    """The team whose spot needs filling.

    Required WHENEVER the league has teams, and optional only while it doesn't. A
    chair often hasn't drafted teams a week before the first draw, and people line up
    week-1 subs before that — so a teamless request stays possible, but strictly as
    the answer to "there is nothing to pick yet". Once the teams are up, offering
    "no team" would just keep minting records that can't be matched against anything:
    two requests for the same team on the same draw look like two different asks, and
    two subs turn up. (Teamless ones already posted are reconciled by the nudge in
    Subs.team_reconcile_loop.)"""

    def __init__(self, names: list[str], selected, row: int = 1):
        # Only teams the system knows about — no free-typing. Alphabetized.
        opts = _unique_options([
            discord.SelectOption(label=_truncate(n, 100), value=_truncate(n, 100), default=(n == selected))
            for n in sorted(names, key=str.casefold)[:24]
        ])
        no_teams_yet = not opts
        if no_teams_yet:
            opts.append(discord.SelectOption(
                label="Teams aren't set yet — post without one",
                value=NO_TEAM,
                description="Posts as “needs a sub”, no team named",
                default=(selected == ""),
            ))
        super().__init__(
            placeholder=("Teams aren't set yet — optional" if no_teams_yet else "Which team…"),
            min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.team = "" if self.values[0] == NO_TEAM else self.values[0]
        await self.view.refresh(interaction)


class GameSelect(discord.ui.Select):
    """The night picker. `placeholder` is the CALLER's to set: this select serves two
    opposite people — someone saying when they NEED a sub and someone saying when they
    can BE one — and keying the wording off `multi` instead put "Games you can cover…"
    in front of a requester the moment the need-a-sub flow went multi-select."""

    def __init__(self, games: list[dict], selected_isos, *, multi: bool, row: int = 2,
                 placeholder: str | None = None):
        self.multi = multi
        # Real draws, plus the league's own upcoming nights where the schedule
        # doesn't reach yet (flagged in the description so nobody mistakes a
        # projected night for a posted one). Still a pick-list, never free text:
        # every option is a real league night at the league's start time.
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(g["label"], 100),
                value=g["iso"],
                description=("not on the schedule yet" if g.get("projected") else None),
                default=(g["iso"] in (selected_isos or [])),
            )
            for g in games[:25]
        ])
        if not opts:
            # Only reachable for a league with no schedule, no start date in its
            # title AND no known night — nothing to build a date from.
            opts = [discord.SelectOption(
                label="No dates available for this league", value="__none__",
                description="No schedule and no start date posted yet")]
        super().__init__(
            placeholder=(placeholder or ("Which dates…" if multi else "Which game…")),
            min_values=1, max_values=(len(opts) if multi else 1), options=opts, row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        vals = self.values
        if "__none__" in vals:
            await interaction.response.defer()
            return
        if self.multi:
            self.view.game_isos = list(vals)
        else:
            self.view.game_iso = vals[0]
        await self.view.refresh(interaction)


class SpotsSelect(discord.ui.Select):
    """How many subs this game needs.

    In EDIT mode — an open request already exists for this league + team + night —
    the number is the new TOTAL, not an increment. "We had one, we need three now"
    is answered by picking 3, and the sub already on it keeps their spot."""

    def __init__(self, selected: int, row: int = 3, *, editing: bool = False, covered: int = 0):
        opts = [
            discord.SelectOption(
                label=f"{n} spot{'s' if n > 1 else ''} needed"
                      + (" — current" if editing and n == selected else ""),
                value=str(n),
                description=("fewer than the subs already on it" if n < covered else None),
                default=(n == selected))
            for n in range(1, 5)
        ]
        super().__init__(placeholder=("Total spots needed…" if editing else "How many subs needed…"),
                         min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.spots = int(self.values[0])
        self.view.spots_touched = True
        await self.view.refresh(interaction)


# ── Need-a-sub flow (ephemeral, league → team → date(s) → spots) ────────────
# One flow, three outcomes:
#   • one date, nothing open for it   → post a request
#   • one date, a request ALREADY open for that league+team+date → EDIT its spot
#     count. This used to be a dead end ("already open, change something and
#     re-post"), which left a team that found one sub and then needed two more with
#     no move but cancelling — losing the sub they had.
#   • several dates ticked → one request PER DATE sharing a series_id. That id is a
#     bulk-input tag and nothing more: each date is an ordinary, independent sub
#     opportunity from the moment it's posted. Naming who'll cover them is a separate
#     step (Fill for someone, offered right on the alert), not part of posting.


class NeedSubFlowView(discord.ui.View):
    def __init__(self, leagues: list[dict], state: dict | None = None, requester_id=None):
        super().__init__(timeout=300)
        self.leagues = leagues
        # The live store, for spotting a request that already covers the chosen date.
        self.state = state if state is not None else {"requests": []}
        self.requester_id = requester_id
        self.league_id = None
        self.team = None
        self.game_isos: list[str] = []
        self.spots = 1
        self.spots_touched = False   # once they pick a number, stop defaulting it
        self.message = None
        self.posted = False  # one-shot guard: a posted flow can't post again
        self.build()

    # -- selection state ----------------------------------------------------
    def league(self) -> dict | None:
        return next((l for l in self.leagues if str(l["id"]) == str(self.league_id)), None)

    def on_league_change(self):
        self.team = None
        self.game_isos = []

    def games(self) -> list[dict]:
        lg = self.league()
        return game_options(lg, club_now()) if lg else []

    def dates(self) -> list[str]:
        """Exactly the dates ticked — nothing is inferred or extended. Whoever posts
        chooses each date on purpose."""
        return sorted(set(self.game_isos))

    def existing(self) -> dict | None:
        """The open request this post would collide with. Only meaningful for a single
        date — a multi-date post lays over whatever is already there rather than
        editing it."""
        d = self.dates()
        if len(d) != 1:
            return None
        return _find_open_duplicate(self.state, self.league_id, d[0], self.team or "",
                                    requester_id=self.requester_id)

    def league_has_teams(self) -> bool:
        lg = self.league()
        return bool(lg and (lg.get("team_names") or []))

    def ready(self) -> bool:
        # League + at least one date. WHEN you need a sub is the point of the request,
        # so a date is never optional — for an unscheduled league the picker projects
        # real league dates off the title's start date rather than letting the request
        # go out dateless (see projected_games).
        #
        # The team is required too, EXCEPT while the league has no teams to pick from
        # (see TeamSelect). That exception is what keeps week-1 subbing possible; it is
        # not a general "team optional" rule.
        lg = self.league()
        if lg is None or not self.dates():
            return False
        return bool(self.team) or not self.league_has_teams()

    # -- rendering ----------------------------------------------------------
    def build(self) -> "NeedSubFlowView":
        self.clear_items()
        self.add_item(LeagueSelect(self.leagues, self.league_id, row=0))
        lg = self.league()
        if lg:
            # Team is picked from the league's roster only (no free-typing).
            self.add_item(TeamSelect(lg.get("team_names") or [], self.team, row=1))
            # Multi-select: ticking several dates posts each one as its own request.
            self.add_item(GameSelect(
                self.games(), self.game_isos, multi=True, row=2,
                placeholder="Which date — tick as many as you need…"))
            ex = self.existing()
            shown = self.spots
            if ex is not None and not self.spots_touched:
                shown = int(ex["spots_needed"])   # start from what the request asks now
                self.spots = shown
            self.add_item(SpotsSelect(shown, row=3, editing=ex is not None,
                                      covered=store.covered(ex) if ex else 0))
            self.add_item(PostNeedButton(disabled=not self.ready(), editing=ex is not None,
                                         count=len(self.dates()), row=4))
        return self

    def prompt(self) -> str:
        lg = self.league()
        if not lg:
            return "**Need a sub** — pick the league:"
        d = self.dates()
        ex = self.existing()
        parts = [f"League: **{league_label(lg)}**"]
        if self.team:
            parts.append(f"Team: **{self.team}**")
        else:
            parts.append("Team: **not posted yet**" if not self.league_has_teams()
                         else "Team: **not set**")
        parts.append(f"When: **{fmt_run(d)}**" if d else "When: **not set**")
        parts.append(f"Spots: **{self.spots}**" + (" a date" if len(d) > 1 else ""))
        head = "**Need a sub** — " + " · ".join(parts)

        if ex is not None:
            cov = store.covered(ex)
            return (head + f"\n⚠️  There's already an open request for that date — "
                           f"**{cov}/{ex['spots_needed']}** covered. Set the **total** number "
                           f"of spots you need and press **Update spots**; whoever's already "
                           f"on it keeps their place.")
        if not self.ready():
            if self.league_has_teams() and not self.team:
                return head + "\nPick your team and the date — tick as many dates as you need."
            return (head + "\nPick the date — tick as many as you need. This league's teams "
                           "aren't posted yet, so the team can wait.")
        if len(d) > 1:
            return head + f"\nPress **Post {len(d)} dates** — each one goes up as its own request."
        return head + "\nPress **Post request**."

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class PostNeedButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, editing: bool = False, count: int = 1, row: int = 4):
        if editing:
            label, emoji = "Update spots", "✏️"
        elif count > 1:
            label, emoji = f"Post {count} dates", "✅"
        else:
            label, emoji = "Post request", "✅"
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.success,
                         row=row, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        f: NeedSubFlowView = self.view
        # One-shot guard: posting isn't idempotent (each call appends requests), so an
        # impatient double-tap would post twice. Set the flag synchronously — before
        # any await — so a second callback (which can only run once this one yields)
        # sees it and bails. Belt-and-braces with the button being removed on success.
        if f.posted:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        f.posted = True

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        lg = f.league()
        title = league_label(lg) if lg else ""
        cog: "Subs" = interaction.client.get_cog("Subs")
        dates = f.dates()
        ex = f.existing()

        # ── Edit: change the spot count on the request that's already up ──────
        if ex is not None:
            result, req = await cog.set_request_spots(
                interaction.user, ex["id"], f.spots, channel=interaction.channel)
            if result == "ok":
                opn = store.open_spots(req) if req else 0
                await interaction.edit_original_response(
                    content=(f"✏️  **{fmt_when(dates[0])}** now needs **{f.spots}** — "
                             f"{opn} still open. The board and the alert are updated, and "
                             "anyone available has been re-tagged."),
                    view=None)
                return
            msgs = {
                "unchanged": f"That request already asks for **{f.spots}**.",
                "too_low": ("Can't drop below the people already on it — take someone off "
                            "with **➖ Remove** first. (Only the person who posted it can "
                            "lower the count.)"),
                "locked": "That game starts too soon — its roster is locked.",
                "closed": "That request is no longer on the board.",
            }
            f.posted = False
            await interaction.edit_original_response(
                content=msgs.get(result, "Nothing changed.") + "\n\n" + f.prompt(),
                view=f.build())
            return

        # ── Post: one request per date, sharing a series_id when there's more ──
        made, skipped, filled = await cog.add_series(
            requester=interaction.user, league_id=f.league_id or "", league=title,
            team=f.team or "", game_isos=dates, spots=f.spots,
            channel=interaction.channel)

        if not made:
            f.posted = False
            who = f"**{f.team}**" if f.team else "**you**"
            await interaction.edit_original_response(
                content=(f"⚠️  There's already an open request for {who} on every date you "
                         f"picked. Pick a single date to raise its spot count, or use "
                         f"**➖ Remove** on the existing one.\n\n" + f.prompt()),
                view=f.build())
            return

        bits = [f"✅  Posted **{title}** · {f.team or 'no team named'} · {fmt_run(dates)} · "
                f"{f.spots} spot{'s' if f.spots != 1 else ''}"
                f"{' a date' if len(dates) > 1 else ''}."]
        if skipped:
            bits.append(f"({skipped} date{'s' if skipped != 1 else ''} already had an open "
                        f"request — left alone.)")
        if filled:
            # The team has a super sub, so nothing was asked of the room.
            goto = store.standing_for(cog.state, f.league_id or "", f.team or "")
            who = first_name(goto[0]["name"]) if goto else "your team's super sub"
            bits.append(f"**{who}** is the super sub for {f.team} and has been put on "
                        f"{filled} of {'them' if len(dates) > 1 else 'it'} — "
                        f"they've been asked to confirm.")
        if filled < len(dates):
            bits.append("Anyone available has been tagged. If you already know who's covering "
                        "them, hit **Fill for someone** on the alert."
                        if len(dates) > 1 else
                        "It's on the board — anyone available has been tagged and can hit "
                        "**I'll take it**.")
        await interaction.edit_original_response(content=" ".join(bits), view=None)


# ── Available-to-sub flow (ephemeral, league → games) ───────────────────────

class AvailFlowView(discord.ui.View):
    def __init__(self, leagues: list[dict], user_id: int, state: dict | None = None):
        super().__init__(timeout=300)
        self.leagues = leagues
        self.user_id = user_id
        self.state = state or {}
        self.league_id = None
        self.game_isos: list[str] = []
        self.message = None
        self.build()

    def league(self) -> dict | None:
        return next((l for l in self.leagues if str(l["id"]) == str(self.league_id)), None)

    def on_league_change(self):
        self.game_isos = []


    def build(self) -> "AvailFlowView":
        self.clear_items()
        # Availability-only: pick a league → games (or none for any) → post. Claiming
        # an open spot is now a one-tap 🙋 hand-raise button on the board itself.
        row = 0
        self.add_item(LeagueSelect(self.leagues, self.league_id, row=row))
        row += 1
        lg = self.league()
        if lg:
            games = game_options(lg, club_now())
            if games:
                self.add_item(GameSelect(games, self.game_isos, multi=True, row=row,
                                         placeholder="Games you can cover…"))
                row += 1
            self.add_item(PostAvailButton(row=row))
        return self

    def prompt(self) -> str:
        lines = ["**I'm free to sub** — list your availability so you get pinged when a "
                 "matching game needs someone."]
        lg = self.league()
        if lg:
            s = f"League: **{league_label(lg)}**"
            if self.game_isos:
                s += " · " + ", ".join(fmt_when(g) for g in self.game_isos)
            lines.append(f"{s} — choose games (or none for any), then **Post availability**.")
        else:
            lines.append("Pick a league below (then optionally the games you can cover).")
        return "\n".join(lines)

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class PostAvailButton(discord.ui.Button):
    def __init__(self, row: int = 2):
        super().__init__(label="Post availability", emoji="🙋", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        # Ack immediately (feedback + dodge the 3s deadline before the board sync).
        await interaction.response.defer()
        view: AvailFlowView = self.view
        lg = view.league()
        if not lg:
            return  # leave the flow open so they can pick a league
        title = league_label(lg)
        cog: "Subs" = interaction.client.get_cog("Subs")
        await cog.add_availability(user=interaction.user, league_id=view.league_id, league=title,
                                   games=view.game_isos, channel=interaction.channel)
        gtxt = ", ".join(fmt_when(g) for g in view.game_isos) if view.game_isos else "any game"
        await interaction.edit_original_response(
            content=f"✅  Listed you as available for **{title}** · {gtxt}.", view=None)


# ── Request/availability matching helpers ───────────────────────────────────

def _same_game(a_iso: str, b_iso: str) -> bool:
    """True if two game timestamps refer to the same draw, compared to the minute
    and tolerant of minor ISO formatting differences."""
    if a_iso == b_iso:
        return True
    try:
        return (datetime.fromisoformat(a_iso).replace(second=0, microsecond=0)
                == datetime.fromisoformat(b_iso).replace(second=0, microsecond=0))
    except (ValueError, TypeError):
        return False


def _find_open_duplicate(state: dict, league_id, game_ts: str, team: str,
                         requester_id=None) -> dict | None:
    """An existing open request for the same league + game + team (case/space
    tolerant), or None. All requests on the board are open, so a match is a dup.

    With NO team named, "same team" can't be the test — two different members can
    each legitimately need a sub for the same draw. So a team-less request is only
    a duplicate of another team-less request BY THE SAME PERSON (a double-tap)."""
    lid = str(league_id or "")
    team_norm = (team or "").strip().casefold()
    for r in state.get("requests", []):
        if str(r.get("league_id") or "") != lid:
            continue
        if (r.get("team") or "").strip().casefold() != team_norm:
            continue
        if not team_norm and r.get("requester_id") != requester_id:
            continue
        if _same_game(r.get("game_ts", ""), game_ts or ""):
            return r
    return None


def _availability_for_request(state: dict, req: dict | None) -> list[dict]:
    """Subs who can cover THIS request: available in its league AND for its game
    time. An availability with no specific games listed covers any game in that
    league. Deduped by user; anyone already on the request is excluded. Used to
    @-mention the right people on the alert page."""
    if not req:
        return []
    lid = str(req.get("league_id") or "")
    game = req.get("game_ts") or ""
    out, seen = [], set()
    for a in state.get("availability", []):
        uid = a["user_id"]
        # Skip the requester themselves (you can't sub your own request) and anyone
        # already filled/pending on it.
        if uid in seen or uid == req.get("requester_id") or store.is_involved(req, uid):
            continue
        if lid and str(a.get("league_id") or "") != lid:
            continue  # different league
        games = a.get("games") or []
        if games and game and not any(_same_game(game, g) for g in games):
            continue  # available in this league, but not for this game time
        seen.add(uid)
        out.append(a)
    return out


def _standing_for_request(state: dict, req: dict | None) -> list[dict]:
    """The super subs for THIS request's league + team, first up first — the people an
    alert should tag ahead of the general availability list.

    Excludes anyone already on it (the first super sub is normally auto-assigned, so an
    alert usually only exists because they dropped it or the team needs more than
    one), the requester, and anyone who has already declined this particular date."""
    if not req:
        return []
    declined = set(req.get("auto_declined") or [])
    return [g for g in store.standing_for(state, req.get("league_id", ""), req.get("team", ""))
            if g["user_id"] not in declined
            and g["user_id"] != req.get("requester_id")
            and not store.is_involved(req, g["user_id"])]


def _my_requests(state: dict, uid: int) -> list[dict]:
    return [r for r in store.requests_sorted(state) if r.get("requester_id") == uid]


def _avail_label(a: dict) -> str:
    """How one availability listing reads in a picker/confirm: the league, plus the
    games it covers (or "any game")."""
    league = stored_league(a.get("league", "")) or "League"
    games = ", ".join(fmt_when_short(g) for g in (a.get("games") or [])) or "any game"
    return f"{league} · {games}"


class RemoveAvailSelect(discord.ui.Select):
    """Pick one of the caller's availability listings to clear. Picking only OPENS the
    confirm — the other two selects in this menu (remove a sub, cancel a request) both
    confirm before acting, and a stray tap here silently un-volunteers you from a game
    people may already be counting on you for."""

    def __init__(self, entries: list[dict], row: int | None = None):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(stored_league(a.get("league", "")) or "League", 100),
                value=(str(a.get("league_id")) if a.get("league_id") else "__nolg__"),
                description=_truncate(
                    ", ".join(fmt_when_short(g) for g in (a.get("games") or [])) or "any game", 100),
            )
            for a in entries[:25]
        ])
        super().__init__(placeholder="Remove an availability listing…",
                         options=opts, min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        league_id = "" if self.values[0] == "__nolg__" else self.values[0]
        entry = store.find_availability(cog.state, interaction.user.id, league_id)
        if entry is None:
            await interaction.response.edit_message(
                content="That listing is already gone.", view=None)
            return
        await interaction.response.edit_message(
            content=f"Clear your availability for **{_avail_label(entry)}**?",
            view=ConfirmRemoveAvailView(league_id, _avail_label(entry)))


class ConfirmRemoveAvailView(discord.ui.View):
    def __init__(self, league_id: str, label: str):
        super().__init__(timeout=120)
        self.league_id = league_id
        self.label = label

    @discord.ui.button(label="Clear it", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown,
                                ("rmavail", interaction.user.id, self.league_id)):
            return
        removed = await cog.remove_availability(
            interaction.user.id, self.league_id, interaction.channel)
        msg = (f"🗑️  Cleared your availability for **{self.label}**." if removed
               else "That listing was already gone.")
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Okay — you're still listed as available.", view=None)


# ── Alert page (public "sub needed" message with a one-tap claim button) ──────

class PageView(discord.ui.View):
    """The buttons on an alert page.

    One date: the plain hand-raise. Several: take them all, or take some — and either
    way, **Fill for someone**, pre-aimed at this posting. That last one matters more
    than it looks: posting no longer asks who's covering the dates, so this alert is
    where "Ben's got Tuesdays" actually gets recorded, one tap from the ask."""

    def __init__(self, rid: str, series_id: str = "", dates: int = 0):
        super().__init__(timeout=None)
        if series_id:
            self.add_item(SeriesClaimButton(
                series_id, label=f"Take {dates} dates" if dates else "Take them all"))
            self.add_item(RunPickButton(series_id, label="I'll take some…",
                                        style=discord.ButtonStyle.secondary))
            # Secondary: on an alert the green button is the ask ("take it"), and
            # recording it for someone else is the quieter, less common answer.
            self.add_item(FillForButton(f"run:{series_id}",
                                        style=discord.ButtonStyle.secondary))
        else:
            self.add_item(PageClaimButton(rid))
            self.add_item(FillForButton(f"one:{rid}",
                                        style=discord.ButtonStyle.secondary))


class PageClaimButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"sub:take:(?P<rid>[0-9a-f]+)"):
    """The '🙋 I'll take it' hand-raise button. Claims the spot in one tap — carried
    both by an alert page and by each open request on the board itself (with a
    per-request label). No form: tapping fills the clicker straight in."""
    def __init__(self, rid: str, *, label: str = "I'll take it", emoji: str = "🙋",
                 style: discord.ButtonStyle = discord.ButtonStyle.success, row: int | None = None):
        self.rid = rid
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style, row=row,
            custom_id=f"{CID_TAKE_PREFIX}{rid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown, ("take", interaction.user.id, self.rid)):
            return
        result, req = await cog.claim_from_page(interaction.user, self.rid, interaction.channel)
        when = fmt_when(req["game_ts"]) if req else "that game"
        msgs = {
            "added": f"✅  You're in for **{when}** — thanks for subbing! The requester's been notified.",
            "already": f"You're already down for **{when}**.",
            "requester": "That's your own request — you can't sub yourself.",
            "full": "That spot just filled up — thanks anyway!",
            "locked": f"**{when}** starts too soon — the roster's locked. Sort it out in person.",
            "closed": "That request is no longer on the board.",
        }
        await interaction.followup.send(msgs.get(result, "Done."), ephemeral=True)


class NightSelect(discord.ui.Select):
    """Which nights of a run to act on.

    Values are REQUEST ids, not timestamps: a run is N separate requests and every
    mutation downstream takes a request id, so resolving it here means nothing has to
    match dates back to records later. Only open, unlocked nights are ever offered."""

    def __init__(self, reqs: list[dict], selected_rids, row: int = 0, *,
                 placeholder: str, description_of=None):
        # `placeholder` and `description_of` are the CALLER's: this select now serves
        # someone taking dates, someone dropping dates they were assigned, and someone
        # attaching a team to dates they posted. "3 open" is only true of the first.
        describe = description_of or (lambda r: f"{store.open_spots(r)} open")
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(fmt_when(r["game_ts"]), 100),
                value=r["id"],
                description=_truncate(describe(r) or "", 100) or None,
                default=(r["id"] in (selected_rids or [])),
            )
            for r in reqs[:25]
        ])
        super().__init__(placeholder=placeholder, min_values=1,
                         max_values=len(opts), options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.rids = list(self.values)
        await self.view.refresh(interaction)


class TakeNightsButton(discord.ui.Button):
    def __init__(self, count: int, row: int = 1):
        super().__init__(label=f"Take {count} date{'s' if count != 1 else ''}",
                         emoji="🙋", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view: "NightPickView" = self.view
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown,
                                ("takenights", interaction.user.id, tuple(sorted(view.rids)))):
            return
        took, skipped = await cog.claim_nights(interaction.user, view.rids, interaction.channel)
        if not took:
            await interaction.edit_original_response(
                content="Nothing there to take any more — covered, locked, or your own request.",
                view=None)
            return
        tail = (f" ({skipped} of the ones you picked got taken or locked first.)"
                if skipped else "")
        await interaction.edit_original_response(
            content=f"✅  You're in for **{fmt_run(took)}** — thanks for covering!{tail}",
            view=None)


class NightPickView(discord.ui.View):
    """Pick which nights of a run to take. Every open night starts TICKED: covering the
    whole run is the common case and shouldn't cost eight taps, while unticking is the
    only way "I can do the first two but not the rest" can be expressed at all."""

    def __init__(self, state: dict, sid: str):
        super().__init__(timeout=300)
        self.state = state
        self.sid = sid
        self.rids = [r["id"] for r in self.nights()]
        self.build()

    def nights(self) -> list[dict]:
        return [r for r in store.series_requests(self.state, self.sid)
                if store.open_spots(r) > 0 and not is_locked(r)]

    def build(self) -> "NightPickView":
        self.clear_items()
        ns = self.nights()
        if ns:
            self.add_item(NightSelect(ns, self.rids, row=0,
                                      placeholder="Which dates…"))
            self.add_item(TakeNightsButton(len(self.rids), row=1))
        return self

    def prompt(self) -> str:
        ns = self.nights()
        if not ns:
            return "Nothing open on those dates any more — someone got there first."
        head = f"**{_req_for(ns[0])}** · {fmt_run([r['game_ts'] for r in ns])}\n"
        if len(ns) > 25:
            # Discord caps a select at 25 options. The button still takes ALL of them
            # (rids starts from the full list), so the cap only limits *unticking* —
            # say so rather than letting the list look complete.
            return (head + f"All {len(ns)} are ticked and the button takes every one. "
                           f"Only the first 25 can be unticked here — for a finer split, "
                           f"take these and drop what you can't make with **➖ Remove**.")
        return head + f"All {len(ns)} are ticked. Untick any you can't make, then press the button."

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class RunPickButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"sub:pickrun:(?P<sid>[0-9a-f]+)"):
    """A run's single button — on the board, and beside the one-tap button on an alert
    page. Opens the night picker rather than acting, because a run is the one case
    where "all of it" and "some of it" are both ordinary answers."""

    def __init__(self, sid: str, *, label: str = "I'll take some…", emoji: str = "🙋",
                 style: discord.ButtonStyle = discord.ButtonStyle.success,
                 row: int | None = None):
        self.sid = sid
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style, row=row,
            custom_id=f"{CID_PICK_RUN_PREFIX}{sid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        view = NightPickView(cog.state, self.sid)
        await interaction.response.send_message(content=view.prompt(),
                                                view=view if view.nights() else None,
                                                ephemeral=True)


class SeriesClaimButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"sub:takerun:(?P<sid>[0-9a-f]+)"):
    """One tap covers every open night of a run. Saying "I'll take Tuesdays" is a
    single decision, so it should cost a single click — the nights stay separate
    requests underneath, so any one of them can still be dropped later."""

    def __init__(self, sid: str, *, label: str = "I'll take them all"):
        self.sid = sid
        super().__init__(discord.ui.Button(
            label=label, emoji="🙋",
            style=discord.ButtonStyle.success,
            custom_id=f"{CID_TAKE_RUN_PREFIX}{sid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown, ("takerun", interaction.user.id, self.sid)):
            return
        took, skipped = await cog.claim_series(interaction.user, self.sid, interaction.channel)
        if not took:
            await interaction.followup.send(
                "Nothing left to take there — it's covered, locked, or yours. "
                "Thanks anyway!", ephemeral=True)
            return
        tail = (f" ({skipped} date{'s' if skipped != 1 else ''} were already covered or "
                f"too close to start.)" if skipped else "")
        await interaction.followup.send(
            f"✅  You're in for **{fmt_run(took)}** — thanks for covering!{tail}",
            ephemeral=True)


# ── The cog ─────────────────────────────────────────────────────────────────

# ── Super subs: the auto-assignment handshake ───────────────────────────────
# A super sub doesn't claim a spot, they're put on it. That's the point — the team
# gets coverage the moment they ask, without waiting for anyone to notice an alert.
# The cost is that a name lands on a game its owner hasn't seen, so every assignment
# is followed by a DM they can answer in one tap: Confirm, or drop the dates they
# can't make. Dropping reopens exactly those dates and alerts the room, and is
# remembered per date so nothing quietly puts them back on.


class AutoAssignView(discord.ui.View):
    """What an auto-assigned super sub gets in their DM."""

    def __init__(self, count: int = 1):
        super().__init__(timeout=None)
        self.add_item(ConfirmAutoButton(count=count))
        self.add_item(DropAutoButton(count=count))


class ConfirmAutoButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"sub:autook"):
    """Acknowledge your assignments. Confirming doesn't change who's on the spot —
    they're already on it — it only stops the T-minus chase, which is the whole
    difference between "covered" and "we think it's covered".

    Keyed on the PERSON, not on the batch it was sent for. Notices are folded into
    one message per person (see Subs._flush_notice), so a button tied to one batch
    would confirm part of what its own message lists. "Confirm" means "yes to what
    I'm down for", and the reply names exactly what that turned out to be."""

    def __init__(self, *, count: int = 1):
        super().__init__(discord.ui.Button(
            label=("Confirm" if count <= 1 else f"Confirm all {count}"),
            emoji="\u2705", style=discord.ButtonStyle.success,
            custom_id=CID_AUTO_OK))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog: "Subs" = interaction.client.get_cog("Subs")
        confirmed = await cog.confirm_auto(interaction.user)
        if not confirmed:
            await interaction.followup.send(
                "Nothing left to confirm there — those dates have already been "
                "confirmed, dropped or played.", ephemeral=True)
            return
        await interaction.followup.send(
            f"\u2705  Confirmed \u2014 you're down for **{fmt_run(confirmed)}**. "
            "If something changes, hit **I can't make some** or use **\u2796 Remove** "
            "on the board.", ephemeral=True)


class DropAutoButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"sub:autodrop"):
    """Opens the picker for dropping assigned dates. Never drops on the tap itself:
    an arrangement is many dates and "I can't make one of them" is the common case.
    Person-keyed for the same reason as Confirm — and it usefully means this button
    reaches every date they're down for, not just the ones in one message."""

    def __init__(self, *, count: int = 1):
        super().__init__(discord.ui.Button(
            label=("I can't make it" if count <= 1 else "I can't make some\u2026"),
            style=discord.ButtonStyle.secondary,
            custom_id=CID_AUTO_DROP))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        view = AutoDropView(cog.state, interaction.user.id)
        await interaction.response.send_message(
            content=view.prompt(), view=view if view.dates() else None, ephemeral=True)


class AutoDropView(discord.ui.View):
    """Which of the dates you were assigned you can't make. Nothing starts ticked —
    the default answer to "can you still do these" is yes."""

    def __init__(self, state: dict, user_id: int):
        super().__init__(timeout=300)
        self.state = state
        self.user_id = user_id
        self.rids: list[str] = []
        self.build()

    def dates(self) -> list[dict]:
        return [r for r in store.auto_requests(self.state, self.user_id)
                if not is_locked(r)]

    def build(self) -> "AutoDropView":
        self.clear_items()
        ds = self.dates()
        if ds:
            self.add_item(NightSelect(ds, self.rids, row=0,
                                      placeholder="Dates you can't make\u2026",
                                      description_of=lambda r: _req_for(r)))
            self.add_item(AutoDropSubmit(len(self.rids), row=1))
        return self

    def prompt(self) -> str:
        ds = self.dates()
        if not ds:
            return ("Nothing there to drop \u2014 those dates have already been dropped, "
                    "played, or are too close to game time to change.")
        head = f"**{_req_for(ds[0])}** \u00b7 you're assigned to {fmt_run([r['game_ts'] for r in ds])}\n"
        return head + ("Tick the ones you can't make, then press the button. Each one "
                       "reopens on its own \u2014 the rest stay yours.")

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class AutoDropSubmit(discord.ui.Button):
    def __init__(self, count: int, row: int = 1):
        super().__init__(label=(f"Drop {count} date{'s' if count != 1 else ''}"
                                if count else "Drop"),
                         emoji="\u21a9\ufe0f", style=discord.ButtonStyle.danger,
                         row=row, disabled=not count)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view: "AutoDropView" = self.view
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown,
                                ("autodrop", interaction.user.id, tuple(sorted(view.rids)))):
            return
        dropped = await cog.drop_auto(interaction.user, view.rids)
        if not dropped:
            await interaction.edit_original_response(
                content="Nothing changed \u2014 those dates were already off your plate.",
                view=None)
            return
        await interaction.edit_original_response(
            content=(f"\u21a9\ufe0f  Dropped **{fmt_run(dropped)}**. Those dates are back on "
                     "the board and the room has been alerted \u2014 you won't be put on them "
                     "again. Everything else is still yours."),
            view=None)


# ── "Your league has teams now" \u2014 attaching a team after the fact ────────────


class SetTeamButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"sub:setteam:(?P<lid>[0-9]+)"):
    """From the nudge DM. Resolves the caller's teamless requests in that league at
    CLICK time rather than from the id, so anything they posted since the nudge went
    out is included and anything since covered or played is not."""

    def __init__(self, lid: str, *, count: int = 0):
        self.lid = lid
        super().__init__(discord.ui.Button(
            label=("Set my team" if count <= 1 else f"Set my team on {count} dates"),
            emoji="\U0001f3f7\ufe0f", style=discord.ButtonStyle.primary,
            custom_id=f"{CID_SET_TEAM_PREFIX}{lid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["lid"])

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        league = next((l for l in await cog.get_leagues()
                       if str(l["id"]) == str(self.lid)), None)
        view = SetTeamView(cog.state, league, interaction.user.id)
        await interaction.response.send_message(
            content=view.prompt(), view=view if view.pending() else None, ephemeral=True)


class SetTeamView(discord.ui.View):
    """Attach a team to requests posted before the chair set the teams. All of the
    caller's teamless dates in that league start ticked \u2014 they're nearly always the
    same spot on the same team \u2014 and any that aren't can be unticked and done after."""

    def __init__(self, state: dict, league: dict | None, user_id: int):
        super().__init__(timeout=300)
        self.state = state
        self.league = league
        self.user_id = user_id
        self.team = None
        self.rids = [r["id"] for r in self.pending()]
        self.build()

    def pending(self) -> list[dict]:
        lid = str((self.league or {}).get("id") or "")
        if not lid:
            return []
        return [r for r in store.requests_sorted(self.state)
                if r.get("requester_id") == self.user_id
                and not (r.get("team") or "").strip()
                and str(r.get("league_id") or "") == lid
                and not is_locked(r)]

    def teams(self) -> list[str]:
        return (self.league or {}).get("team_names") or []

    def build(self) -> "SetTeamView":
        self.clear_items()
        ps = self.pending()
        if ps and self.teams():
            self.add_item(TeamSelect(self.teams(), self.team, row=0))
            self.add_item(NightSelect(ps, self.rids, row=1,
                                      placeholder="Which of your dates\u2026",
                                      description_of=lambda r: f"{store.open_spots(r)} open"))
            self.add_item(SetTeamSubmit(self.team, len(self.rids), row=2))
        return self

    def prompt(self) -> str:
        ps = self.pending()
        if not ps:
            return "Those requests are already sorted \u2014 nothing here needs a team."
        if not self.teams():
            return "That league still has no teams posted, so there's nothing to pick yet."
        head = (f"**{league_label(self.league)}** \u2014 the teams are up now. You have "
                f"**{len(ps)}** request{'s' if len(ps) != 1 else ''} with no team on "
                f"{fmt_run([r['game_ts'] for r in ps])}.\n")
        if not self.team:
            return head + "Pick the team the spot is on. All your dates are ticked \u2014 untick any that are a different team and do those after."
        return head + f"Press the button to put **{self.team}** on the ticked dates."

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class SetTeamSubmit(discord.ui.Button):
    def __init__(self, team, count: int, row: int = 2):
        label = (f"Set {team} on {count} date{'s' if count != 1 else ''}"
                 if team else "Pick a team first")
        super().__init__(label=_truncate(label, 80), emoji="\u2705",
                         style=discord.ButtonStyle.success, row=row,
                         disabled=not (team and count))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view: "SetTeamView" = self.view
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown,
                                ("setteam", interaction.user.id, tuple(sorted(view.rids)))):
            return
        done, clashes, assigned = await cog.set_team_for(
            interaction.user, view.rids, view.team, channel=interaction.channel)
        if not done:
            await interaction.edit_original_response(
                content="Nothing changed \u2014 those requests already have a team, or have "
                        "gone off the board.", view=None)
            return
        bits = [f"\u2705  **{view.team}** is now on **{fmt_run(done)}**."]
        if assigned:
            bits.append(f"{assigned} of them went straight to your team's super sub.")
        if clashes:
            bits.append(f"\u26a0\ufe0f  Heads up \u2014 **{view.team}** already had a sub request on "
                        f"**{fmt_run(clashes)}** from someone else. If that's the same spot, "
                        f"one of you should drop it with **\u2796 Remove**, or two subs will "
                        f"turn up.")
        await interaction.edit_original_response(content=" ".join(bits), view=None)


# ── Managing super subs ─────────────────────────────────────────────────────


def leagues_with_teams(leagues: list[dict]) -> list[dict]:
    """The only leagues a super sub arrangement can be made on. A standing arrangement is
    per TEAM, and a team you can't name is a team nothing can be matched to \u2014 so
    unlike an ordinary request, this has no "teams aren't up yet" escape hatch."""
    return [l for l in leagues if (l.get("team_names") or [])]


def standing_summary(state: dict) -> str:
    """Every super sub arrangement, grouped by league and team, in priority order."""
    groups: dict[tuple, list[dict]] = {}
    for g in store.standing_sorted(state):
        groups.setdefault((str(g.get("league_id") or ""), g.get("team", "")), []).append(g)
    if not groups:
        return "_No super subs set up yet._"
    lines = []
    for (_lid, team), members in groups.items():
        league = stored_league(members[0].get("league", "")) or "League"
        who = " \u00b7 ".join(f"{i}. {m['name']}" for i, m in enumerate(members, 1))
        lines.append(f"\u2022 **{league}** \u00b7 {team} \u2014 {who}")
    return "\n".join(lines)


def build_notice(news: list[tuple], groups: dict) -> str:
    """The one message a super sub gets: `news` is arrangements just made
    [(league, team, actor)], `groups` is dates they haven't been told about yet
    {(league, team): [iso]}.

    These two arrive together more often than you'd think — making an arrangement
    sweeps up whatever that team already has open, and a notice that was lost to a
    restart is picked up by the next flush. Concatenating two announcements is how
    one message reads as two unrelated ones, so a new arrangement and the dates are
    joined by "You're also down for" rather than stacked."""
    lines = []
    for league, team, actor in news:
        lines.append(f"\u2b50  {actor} made you the **super sub** for **{team}** in "
                     f"**{league}**.")
    if news:
        lines.append(
            f"Whenever {'that team needs' if len(news) == 1 else 'those teams need'} a "
            "sub, you'll be put on it automatically and DM'd to confirm \u2014 you can "
            "drop any date you can't make, and it goes back on the board. An organiser "
            "can end the arrangement any time.")
    rest = list(groups.items())
    if rest:
        new_keys = {(lg, tm) for lg, tm, _a in news}
        if news and len(rest) == 1 and rest[0][0] in new_keys:
            # The arrangement swept up its own team's open dates — don't say the team
            # and league again three lines after naming them.
            lines += ["", f"**You're down for:** {fmt_run(sorted(rest[0][1]))}"]
        elif news:
            head = ("**You're also down for:**"
                    if any(k not in new_keys for k, _ in rest) else "**You're down for:**")
            lines += ["", head]
            lines += [f"\u00b7 **{tm}** \u00b7 {lg} \u2014 **{fmt_run(sorted(iso))}**"
                      for (lg, tm), iso in rest]
        elif len(rest) == 1:
            (lg, tm), iso = rest[0]
            lines.append(f"\u2b50  You're the super sub for **{tm}** in **{lg}** \u2014 "
                         f"you're down for **{fmt_run(sorted(iso))}**.")
        else:
            lines.append("\u2b50  You're down for:")
            lines += [f"\u00b7 **{tm}** \u00b7 {lg} \u2014 **{fmt_run(sorted(iso))}**"
                      for (lg, tm), iso in rest]
        lines.append("Confirm so the team knows it's sorted, or tell me which dates you "
                     "can't make \u2014 those go straight back on the board.")
    return "\n".join(lines)


class StandingHomeView(discord.ui.View):
    def __init__(self, state: dict, leagues: list[dict]):
        super().__init__(timeout=300)
        self.state = state
        self.leagues = leagues
        self.add_item(StandingAddButton(disabled=not leagues_with_teams(leagues)))
        if store.standing_sorted(state):
            self.add_item(StandingRemoveButton())


class StandingAddButton(discord.ui.Button):
    def __init__(self, *, disabled: bool = False):
        super().__init__(label="Add a super sub", emoji="\u2795",
                         style=discord.ButtonStyle.success, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        home: "StandingHomeView" = self.view
        view = StandingAddView(leagues_with_teams(home.leagues), home.state)
        await interaction.response.edit_message(content=view.prompt(), view=view)


class StandingRemoveButton(discord.ui.Button):
    def __init__(self):
        # ➖ and danger, to match Remove on the board: taking something off looks
        # the same wherever it happens.
        super().__init__(label="Remove one", emoji="\u2796",
                         style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        home: "StandingHomeView" = self.view
        entries = store.standing_sorted(home.state)
        if not entries:
            await interaction.response.edit_message(
                content="Nothing to remove \u2014 there are no super subs set up.", view=None)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(StandingRemoveSelect(entries))
        await interaction.response.edit_message(
            content="Which arrangement should end?", view=view)


class StandingAddView(discord.ui.View):
    """league \u2192 team \u2192 person, ending on an explicit button. The order matters: the
    team list comes from the league, and naming someone the super sub for a team is a
    commitment made on their behalf, so it never fires off a select."""

    def __init__(self, leagues: list[dict], state: dict):
        super().__init__(timeout=300)
        self.leagues = leagues
        self.state = state
        self.league_id = None
        self.team = None
        self.member = None
        self.submitted = False
        self.build()

    def league(self) -> dict | None:
        return next((l for l in self.leagues if str(l["id"]) == str(self.league_id)), None)

    def on_league_change(self):
        self.team = None

    def ready(self) -> bool:
        return bool(self.league() and self.team and self.member)

    def build(self) -> "StandingAddView":
        self.clear_items()
        self.add_item(LeagueSelect(self.leagues, self.league_id, row=0))
        lg = self.league()
        if lg:
            self.add_item(TeamSelect(lg.get("team_names") or [], self.team, row=1))
            self.add_item(StandingMemberSelect(self.member, row=2))
            self.add_item(StandingSubmitButton(self.member, self.team,
                                               disabled=not self.ready(), row=3))
        return self

    def prompt(self) -> str:
        lg = self.league()
        if not lg:
            return ("**Super sub** \u2014 pick the league. Only leagues with teams posted "
                    "can have one: the arrangement is per team.")
        parts = [f"League: **{league_label(lg)}**",
                 f"Team: **{self.team}**" if self.team else "Team: **not set**",
                 f"Sub: **{self.member.display_name}**" if self.member else "Sub: **not set**"]
        head = "**Super sub** \u2014 " + " \u00b7 ".join(parts)
        if not self.ready():
            return head + "\nPick the team and the person."
        current = store.standing_for(self.state, self.league_id, self.team)
        rank = len(current) + 1
        if rank == 1:
            return (head + f"\n{self.member.display_name} will be **auto-assigned** whenever "
                           f"{self.team} needs a sub in this league, and DM'd to confirm.")
        return (head + f"\n{self.member.display_name} would be **#{rank}** for {self.team} \u2014 "
                       f"{current[0]['name']} is auto-assigned first; the rest get tagged "
                       f"ahead of everyone else on the alert.")

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class StandingMemberSelect(discord.ui.UserSelect):
    """Records the pick only \u2014 the button commits (see the cog's UI invariants)."""

    def __init__(self, selected=None, row: int = 2):
        defaults = ([discord.SelectDefaultValue.from_user(selected)]
                    if selected is not None else [])
        super().__init__(
            placeholder=("Change who the super sub is\u2026" if selected is not None
                         else "Who's the super sub\u2026"),
            min_values=1, max_values=1, row=row, default_values=defaults)

    async def callback(self, interaction: discord.Interaction):
        self.view.member = self.values[0]
        await self.view.refresh(interaction)


class StandingSubmitButton(discord.ui.Button):
    def __init__(self, member=None, team=None, *, disabled: bool, row: int = 3):
        label = (f"Make {first_name(member.display_name)} the super sub for {team}"
                 if member is not None and team else "Make them the super sub")
        super().__init__(label=_truncate(label, 80), emoji="\u2b50",
                         style=discord.ButtonStyle.success, row=row, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        view: "StandingAddView" = self.view
        if view.submitted:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        view.submitted = True
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        lg = view.league()
        result, assigned = await cog.add_standing(
            actor=interaction.user, member=view.member, league_id=view.league_id or "",
            league=league_label(lg) if lg else "", team=view.team or "",
            channel=interaction.channel)
        if result == "already":
            view.submitted = False
            await interaction.edit_original_response(
                content=(f"{view.member.display_name} is already a super sub for "
                         f"**{view.team}** in that league.\n\n" + view.prompt()),
                view=view.build())
            return
        bits = [f"\u2b50  **{view.member.display_name}** is the super sub for **{view.team}** "
                f"in **{league_label(lg) if lg else 'that league'}**."]
        bits.append("They've been DM'd." if result == "added" else "")
        if assigned:
            bits.append(f"**{len(assigned)}** open request{'s' if len(assigned) != 1 else ''} "
                        f"for that team ({fmt_run(assigned)}) went to them right away.")
        await interaction.edit_original_response(
            content=" ".join(b for b in bits if b), view=None)


class StandingRemoveSelect(discord.ui.Select):
    """Picking only OPENS a confirm \u2014 ending someone's arrangement is not a select."""

    def __init__(self, entries: list[dict], row: int = 0):
        self.entries = entries
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(f"{g['name']} \u2014 {g.get('team','')}", 100),
                value=f"{g['user_id']}|{g.get('league_id','')}|{g.get('team','')}"[:100],
                description=_truncate(stored_league(g.get("league", "")), 100) or None,
            )
            for g in entries[:25]
        ])
        super().__init__(placeholder="Which super sub arrangement\u2026", min_values=1,
                         max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        uid, lid, team = self.values[0].split("|", 2)
        entry = next((g for g in self.entries
                      if str(g["user_id"]) == uid
                      and str(g.get("league_id", "")) == lid
                      and g.get("team", "") == team), None)
        if entry is None:
            await interaction.response.edit_message(
                content="That arrangement is already gone.", view=None)
            return
        await interaction.response.edit_message(
            content=(f"End **{entry['name']}**'s super sub arrangement for **{team}** in "
                     f"**{stored_league(entry.get('league',''))}**? Dates they're already "
                     f"on stay theirs \u2014 this only stops future ones."),
            view=ConfirmRemoveStandingView(int(uid), lid, team, entry["name"]))


class ConfirmRemoveStandingView(discord.ui.View):
    def __init__(self, user_id: int, league_id: str, team: str, name: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.league_id = league_id
        self.team = team
        self.name = name

    @discord.ui.button(label="End it", emoji="\U0001f5d1\ufe0f", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        if cog._is_repeat_click(cog._click_cooldown,
                                ("rmstanding", interaction.user.id, self.user_id,
                                 self.league_id, self.team)):
            return
        removed = await cog.remove_standing(self.user_id, self.league_id, self.team,
                                            actor=interaction.user)
        msg = (f"\U0001f5d1\ufe0f  **{self.name}** is no longer the super sub for **{self.team}**."
               if removed else "That arrangement was already gone.")
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Okay \u2014 nothing changed.", view=None)


class Subs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = store.load(STORE_PATH)
        self._lock = __import__("asyncio").Lock()
        # namespaced-key -> monotonic time of last click, for debouncing impatient
        # double-taps across all buttons. Keys are tagged tuples, e.g.
        # ("take", user_id, rid) or ("manage_add", user_id, rid, member_id).
        self._click_cooldown: dict[tuple, float] = {}
        # user id -> {"preambles": [...], "task": Task}: assignment news waiting to be
        # folded into one DM. See _queue_sub_notice.
        self._notices: dict[int, dict] = {}
        # user id -> monotonic time we last @-mentioned them on an alert, so a burst
        # of separate postings pings each person once instead of once per posting.
        self._mention_cooldown: dict[int, float] = {}

    # -- lifecycle ----------------------------------------------------------
    async def cog_load(self):
        self.expiry_loop.start()
        self.reminder_loop.start()
        self.team_reconcile_loop.start()

    async def cog_unload(self):
        self.expiry_loop.cancel()
        self.reminder_loop.cancel()
        self.team_reconcile_loop.cancel()
        await self._flush_all_notices()

    async def startup(self):
        """Prune expired requests and re-render every server's board after a (re)connect."""
        async with self._lock:
            store.expire(self.state, club_now(), GRACE_HOURS, undated_days=UNDATED_DAYS)
            store.save(STORE_PATH, self.state)
        await self.render_all_boards()
        self._requeue_unnotified()

    def _requeue_unnotified(self):
        """Anyone still owed an assignment notice gets queued on (re)connect.

        `notified` is in the store precisely so this survives a restart. Without it a
        notice lost mid-window was never retried — and then rode along with the next
        unrelated flush hours later, which is how one member got told about a team he'd
        just been assigned to AND a different team's date in the same breath."""
        owed = {f["user_id"] for r in self.state["requests"]
                for f in store.auto_entries(r) if not f.get("notified")}
        for uid in owed:
            self._queue_sub_notice(uid)
        if owed:
            log.info("Re-queued super sub notices for %d member(s)", len(owed))

    # -- persistence + board refresh ----------------------------------------
    def _save(self):
        store.save(STORE_PATH, self.state)

    async def _resolve_channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    @staticmethod
    def _guild_id(channel) -> int | None:
        g = getattr(channel, "guild", None)
        return g.id if g is not None else None

    def _board_ptr(self, guild_id):
        return self.state.get("boards", {}).get(str(guild_id)) if guild_id is not None else None

    async def _post_board(self, channel):
        """Post a fresh board for `channel`'s SERVER and repoint that server's pointer
        at it, deleting that server's previous board message. Requests/availability
        are shared across servers; each server just renders its own board of the same
        data. Not pinned — visibility comes from being the newest message (so the bot
        needs no Manage Messages permission). Returns the message (or None in a DM)."""
        gid = self._guild_id(channel)
        if gid is None:
            return None
        key = str(gid)
        old = self.state.get("boards", {}).get(key)
        msg = await channel.send(embed=build_embed(self.state), view=build_view(self.state))
        async with self._lock:
            self.state.setdefault("boards", {})[key] = {"channel_id": channel.id, "message_id": msg.id}
            self._save()
        # Delete this server's previous board so it doesn't duplicate as it hops down.
        if old:
            prev_ch = await self._resolve_channel(old["channel_id"])
            if prev_ch is not None:
                try:
                    await prev_ch.get_partial_message(old["message_id"]).delete()
                except discord.HTTPException:
                    pass
        return msg

    async def bump_board(self, guild_id, fallback_channel=None):
        """Repost ONE server's board at the bottom of its channel so that server sees
        the change. Only the acting server's board moves; other servers keep theirs
        (they refresh the shared data the next time someone acts there). Falls back to
        `fallback_channel` when that server has no board yet."""
        if guild_id is None:
            return
        ptr = self._board_ptr(guild_id)
        channel = await self._resolve_channel(ptr["channel_id"]) if ptr else None
        if channel is None:
            channel = fallback_channel
        if channel is None:
            return
        await self._post_board(channel)

    async def render_board(self, guild_id, fallback_channel=None):
        """Edit one server's board in place (no repost). Lazily create it in
        `fallback_channel` if that server has none yet."""
        if guild_id is None:
            return
        ptr = self._board_ptr(guild_id)
        if not ptr:
            if fallback_channel is not None:
                await self._post_board(fallback_channel)
            return
        channel = await self._resolve_channel(ptr["channel_id"])
        if channel is None:
            return
        try:
            # get_partial_message() makes no API call and editing our own message
            # doesn't need Read Message History; NotFound only if it was deleted.
            partial = channel.get_partial_message(ptr["message_id"])
            await partial.edit(embed=build_embed(self.state), view=build_view(self.state))
        except discord.NotFound:
            await self._post_board(channel)  # board was deleted — repost so it stays live
        except discord.Forbidden as e:
            if e.code == 50005:  # message authored by a different bot identity
                log.warning("Subs board %s authored by another bot — reposting our own.",
                            ptr["message_id"])
                await self._post_board(channel)
            else:
                log.warning("Forbidden editing subs board: %s", e)
        except discord.HTTPException as e:
            log.warning("Could not edit subs board: %s", e)

    async def render_all_boards(self):
        """Refresh every server's board in place — used on startup and after the
        background expiry prune, where there's no single acting server."""
        for key in list(self.state.get("boards", {}).keys()):
            try:
                await self.render_board(int(key))
            except (ValueError, TypeError):
                continue


    # -- alert pages ("sub needed" pings + one-tap claim) -------------------
    def _series_open(self, req: dict, reason: str = "new") -> list[dict]:
        """The still-claimable nights of `req`'s run — the unit a "new"/"bump" alert
        speaks about, so an 8-week arrangement pings the room ONCE instead of eight
        times. A REMINDER is always about the single night that's a few hours away,
        so it never aggregates. Returns [] for anything that isn't a live run."""
        if reason == "reminder":
            return []
        run = [r for r in store.series_requests(self.state, req.get("series_id", ""))
               if store.open_spots(r) > 0 and not is_locked(r)]
        return run if len(run) > 1 else []

    def _page_view(self, req: dict, reason: str = "new") -> discord.ui.View:
        run = self._series_open(req, reason)
        if not run:
            return PageView(req["id"])
        return PageView(run[0]["id"], series_id=str(req.get("series_id") or ""),
                        dates=len(run))

    def _alert_recipients(self, req: dict) -> tuple[list[dict], list[dict]]:
        """(super subs, everyone else listed as available) for this request, with
        nobody counted twice."""
        goto = _standing_for_request(self.state, req)
        goto_ids = {g["user_id"] for g in goto}
        subs = [a for a in _availability_for_request(self.state, req)
                if a["user_id"] not in goto_ids]
        return goto, subs

    def _quiet_recipients(self, req: dict) -> set:
        """Who to name WITHOUT an @ on this alert, because we pinged them within the
        last NOTIFY_WINDOW seconds.

        A chair posting nine dates one at a time puts up nine alerts, and each one
        tagging the same two available members is nine phone buzzes each — the thing
        people actually complain about. They're still named, so the alert reads the
        same; they just aren't pinged nine times about the same league."""
        if NOTIFY_WINDOW <= 0:
            return set()
        now_m = time.monotonic()
        goto, subs = self._alert_recipients(req)
        quiet = set()
        for who in [g["user_id"] for g in goto] + [a["user_id"] for a in subs]:
            last = self._mention_cooldown.get(who)
            if last is not None and now_m - last < NOTIFY_WINDOW:
                quiet.add(who)
            else:
                self._mention_cooldown[who] = now_m
        if len(self._mention_cooldown) > 256:
            for k in [k for k, t in self._mention_cooldown.items()
                      if now_m - t >= NOTIFY_WINDOW]:
                del self._mention_cooldown[k]
        return quiet

    def _page_body(self, req: dict, *, reason: str, quiet=frozenset()) -> str:
        heads = {
            "new":      "🆘 **Sub needed**",
            "bump":     "🔔 **Still need a sub**",
            "reminder": "⏰ **Game soon — still need a sub**",
        }
        run = self._series_open(req, reason)
        league = stored_league(req.get("league", ""))
        if run:
            when = fmt_run([r["game_ts"] for r in run])
            opn = sum(store.open_spots(r) for r in run)
            spots_txt = f"{opn} spot{'s' if opn != 1 else ''} open in total"
        else:
            when = fmt_when(req["game_ts"])
            opn = store.open_spots(req)
            spots_txt = f"{opn} spot{'s' if opn != 1 else ''} open"
        detail = " · ".join(x for x in [league or None, _req_for(req), when, spots_txt] if x)
        # Super subs are tagged FIRST and named as such — that ordering is the whole
        # of "priority" as far as an alert is concerned. Anyone who is both a super sub
        # and listed as available is only tagged once, on the super sub line. Anyone in
        # `quiet` was pinged moments ago and is named in plain text instead.
        goto, subs = self._alert_recipients(req)
        verb = "take any or all of them" if run else "grab it"

        def tag(people, note):
            loud = [p for p in people if p["user_id"] not in quiet]
            hushed = [p for p in people if p["user_id"] in quiet]
            out = []
            if loud:
                out.append(" ".join(f"<@{p['user_id']}>" for p in loud) + f" — {note}")
            if hushed:
                out.append(", ".join(first_name(p["name"]) for p in hushed)
                           + f" — {note} (tagged a moment ago)")
            return out

        lines = []
        if goto:
            lines += tag(goto, "you're the super sub for this team.")
        if subs:
            lines += tag(subs, "you're listed as available.")
        if lines:
            tail = "\n".join(lines) + f"\nTap to {verb}:"
        else:
            tail = ("_No one's listed as available yet — first to tap takes "
                    f"{'them' if run else 'it'}:_")
        return f"{heads.get(reason, heads['new'])}\n{detail}\n{tail}"

    async def _delete_page(self, req: dict):
        """Delete a request's live alert message (if any) and clear its pointer."""
        alert = req.get("alert") or {}
        mid = alert.get("message_id")
        if not mid:
            return
        ch = await self._resolve_channel(alert["channel_id"])
        if ch is not None:
            try:
                await ch.get_partial_message(mid).delete()
            except discord.HTTPException:
                pass
        req["alert"] = {"channel_id": None, "message_id": None}

    async def post_page(self, req: dict, *, reason: str = "new", channel=None):
        """Post (or repost) the alert page for `req`: @-mention the members listed as
        available for its league/game and attach a one-tap 'I'll take it' button.
        Retires any earlier page for the same request so alerts don't stack."""
        if store.open_spots(req) <= 0:
            return
        # Alerts go to the request's origin channel (where it was posted), so a
        # request made on one server pings on that server even though the data
        # is shared.
        ch = channel or await self._resolve_channel(req.get("channel_id"))
        if ch is None:
            return
        await self._delete_page(req)
        body = self._page_body(req, reason=reason, quiet=self._quiet_recipients(req))
        try:
            msg = await ch.send(body, view=self._page_view(req, reason),
                                allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e:
            log.warning("Could not post sub alert page: %s", e)
            return
        async with self._lock:
            live = store.find_request(self.state, req["id"])
            if live is not None:
                live["alert"] = {"channel_id": ch.id, "message_id": msg.id}
            self._save()

    async def refresh_page(self, req: dict):
        """Keep a request's alert page in sync after a fill/removal: retire it once the
        request is full or gone; otherwise refresh the open-spot count."""
        alert = req.get("alert") or {}
        if not alert.get("message_id"):
            return
        ch = await self._resolve_channel(alert["channel_id"])
        if ch is None:
            return
        partial = ch.get_partial_message(alert["message_id"])
        # A run's page stays live while ANY of its nights is still open, even once the
        # night it was posted for has filled — otherwise covering week 1 would retire
        # the ask for the other seven.
        run = self._series_open(req)
        live = store.find_request(self.state, req["id"]) is not None
        still_open = live and (bool(run) or store.open_spots(req) > 0)
        try:
            if still_open:
                await partial.edit(content=self._page_body(req, reason="new"),
                                   view=self._page_view(req))
                return
            covered_what = ("every date" if req.get("series_id")
                            else fmt_when(req["game_ts"]))
            await partial.edit(content=f"✅  Covered — thanks! ({covered_what})", view=None)
        except discord.HTTPException:
            pass
        async with self._lock:
            live = store.find_request(self.state, req["id"])
            if live is not None:
                live["alert"] = {"channel_id": None, "message_id": None}
                self._save()


    async def claim_from_page(self, user, rid: str, channel=None) -> tuple[str, dict | None]:
        """One-tap claim from an alert page. Returns (result, req) where result is
        "added" | "already" | "requester" | "full" | "locked" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            if user.id == req.get("requester_id"):
                return ("requester", req)
        return await self.fill_request_spot(user, rid, channel)  # board bump + page refresh + DM

    async def remove_sub_by_anyone(self, actor, rid: str, target_uid: int, channel=None) -> tuple[str, dict | None]:
        """Anyone removes a listed sub from a request (offline sync). DMs the requester
        that a spot opened back up, and reposts the acting server's board. Returns
        (result, req) where result is "removed" | "absent" | "locked" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            if is_locked(req):
                return ("locked", req)
            name = next((m["name"] for m in (req.get("filled", []) + req.get("pending", []))
                         if m["user_id"] == target_uid), None)
            result = store.remove_sub(req, target_uid)  # "removed" | "absent"
            when = fmt_when(req["game_ts"])
            requester_id = req["requester_id"]
            opn = store.open_spots(req)
            self._save()
        if result != "removed":
            return (result, req)
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        await self.refresh_page(req)
        if requester_id != actor.id:
            await self._dm_requester(
                requester_id,
                f"🥌 {actor.display_name} removed {name or 'a sub'} from your {when} game "
                f"— {opn} now open.")
        return (result, req)

    # -- league data --------------------------------------------------------
    async def get_leagues(self) -> list[dict]:
        """Leagues you could still need a sub for: not flagged ended, and not a
        finished season sitting in the cache unflagged (see league_is_over)."""
        try:
            leagues = await get_cached_leagues()
        except Exception as e:  # noqa: BLE001 — network/cache failure shouldn't crash the button
            log.warning("League fetch failed: %s", e)
            return []
        now = club_now()
        return [lg for lg in leagues
                if not lg.get("ended") and not league_is_over(lg, now)]

    # -- mutations (called from button/modal callbacks) ---------------------
    async def add_series(self, *, requester, league_id, league, team, game_isos, spots,
                         channel=None) -> tuple[int, int, int]:
        """Post one or more nights as ordinary requests, sharing a series_id when
        there's more than one. A single date is just a posting of one and goes through
        here too, so there's exactly one create path.

        A date that ALREADY has an open request is left alone — dates laid over one
        someone posted by hand must not double-book it.

        The whole posting goes out under ONE alert: eight pings for eight Tuesdays is
        how a useful board becomes a muted one. Naming who covers them is a separate
        step (Fill for someone, offered on that alert).

        A team with a super sub never reaches the room at all: the arrangement fills
        the spot as the posting is made, and the alert is simply not posted. `filled`
        is how many dates went that way (it was dead weight before this).

        Returns (created, skipped, filled)."""
        isos = list(dict.fromkeys(game_isos))          # dedupe, keep order
        sid = uuid.uuid4().hex[:8] if len(isos) > 1 else ""
        created, skipped, filled = [], 0, 0
        async with self._lock:
            for iso in isos:
                dup = _find_open_duplicate(self.state, league_id, iso, team,
                                           requester_id=requester.id)
                if dup is not None:
                    skipped += 1
                    continue
                req = store.new_request(
                    self.state,
                    requester_id=requester.id,
                    requester_name=requester.display_name,
                    game_ts=iso,
                    spots_needed=spots,
                    league_id=league_id,
                    league=league,
                    team=team,
                    series_id=sid,
                    guild_id=self._guild_id(channel),
                    channel_id=getattr(channel, "id", None),
                    now=club_now(),
                )
                created.append(req)
            if created or filled:
                self._save()
        if not created:
            return (0, skipped, 0)
        # Before anyone is asked: whoever the team has arranged as its super sub is put
        # on these dates. Whatever they cover is never alerted — asking the room to
        # cover a spot that is already covered is how a board gets muted.
        assigned = await self._auto_assign(created, actor_id=requester.id, channel=channel)
        filled = sum(len(b["isos"]) for b in assigned.values())
        await self.render_board(self._guild_id(channel), fallback_channel=channel)
        # One alert for the whole run, anchored on its soonest still-open night.
        anchor = next((r for r in created
                       if store.open_spots(r) > 0 and not is_locked(r)), None)
        if anchor is not None:
            await self.post_page(anchor, reason="new", channel=channel)
        return (len(created), skipped, filled)

    async def set_request_spots(self, actor, rid: str, spots: int,
                                channel=None) -> tuple[str, dict | None]:
        """Change how many subs a live request needs — the "we found one, now we need
        two more" case, which used to have no answer but cancelling and re-posting
        (losing the sub already on it).

        ANYONE may RAISE the count: the team, not the poster, is who lost players, and
        whoever is at the keyboard isn't always whoever posted. Only the requester may
        LOWER it — that's the same call as cancelling part of their own ask.

        Returns (result, req): "ok" | "unchanged" | "too_low" | "locked" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            if is_locked(req):
                return ("locked", req)
            n = int(spots)
            if n < int(req["spots_needed"]) and actor.id != req.get("requester_id"):
                return ("too_low", req)
            before_open = store.open_spots(req)
            result = store.set_spots(req, n)
            if result == "ok":
                # Spots that just opened deserve their own shout. The pre-game
                # reminder has to be re-armed too, or a request that already fired one
                # while it was covered would never chase these new spots.
                if store.open_spots(req) > before_open:
                    req["reminded"] = False
                self._save()
            requester_id = req["requester_id"]
            when = fmt_when(req["game_ts"])
            opened = store.open_spots(req) - before_open
            opn = store.open_spots(req)
        if result != "ok":
            return (result, req)
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        if opened > 0 and opn > 0:
            # Re-ping: nobody re-reads an alert that already said "covered".
            await self.post_page(req, reason="bump", channel=channel)
        else:
            await self.refresh_page(req)
        if requester_id != actor.id:
            await self._dm_requester(
                requester_id,
                f"🥌 {actor.display_name} set your {when} game to {n} sub"
                f"{'s' if n != 1 else ''} needed — {opn} open.")
        return (result, req)

    async def _refresh_pages_for(self, rids, sids) -> None:
        """Refresh whichever affected requests carry a live alert page. A run's page
        lives on ONE of its nights, and that needn't be one of the nights just acted on
        — so refresh the whole series, not only what changed, or the page keeps
        advertising spots that are gone."""
        targets, seen = [], set()
        for sid in {str(x or "") for x in sids} - {""}:
            targets += store.series_requests(self.state, sid)
        for rid in rids:
            r = store.find_request(self.state, rid)
            if r is not None:
                targets.append(r)
        for r in targets:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            await self.refresh_page(r)

    async def claim_nights(self, user, rids, channel=None) -> tuple[list[str], int]:
        """Fill `user` into each of these nights (request ids) as ONE action — the night
        picker's "take these five of eight", and the whole-run one-tap underneath.

        Skips nights they're already on, ones that are full, ones whose roster has
        locked, and any they opened themselves: a partial cover is a good outcome, so
        nothing here is all-or-nothing. One board repost and one DM for the lot, not one
        per night. Returns (nights taken, nights skipped)."""
        took: list[str] = []
        skipped = 0
        sids, requester_id = set(), None
        async with self._lock:
            for rid in rids:
                r = store.find_request(self.state, rid)
                if r is None:
                    skipped += 1
                    continue
                if requester_id is None:
                    requester_id = r.get("requester_id")
                if (is_locked(r) or store.open_spots(r) <= 0
                        or user.id == r.get("requester_id")
                        or store.is_involved(r, user.id)):
                    skipped += 1
                    continue
                if store.add_sub(r, user.id, user.display_name, now=club_now()) == "added":
                    took.append(r["game_ts"])
                    sids.add(r.get("series_id") or "")
                else:
                    skipped += 1
            if took:
                self._save()
        if not took:
            return (took, skipped)
        took.sort()
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        await self._refresh_pages_for(rids, sids)
        if requester_id is not None and requester_id != user.id:
            await self._dm_requester(
                requester_id,
                f"🥌 {user.display_name} is covering {len(took)} date"
                f"{'s' if len(took) != 1 else ''} for you — {fmt_run(took)}.")
        return (took, skipped)

    async def claim_series(self, user, sid: str, channel=None) -> tuple[list[str], int]:
        """One-tap "I'll take the whole run" from an alert page. Every night of the run
        goes through claim_nights, which skips whatever isn't actually takeable."""
        async with self._lock:
            rids = [r["id"] for r in store.series_requests(self.state, sid)]
        return await self.claim_nights(user, rids, channel)

    async def fill_nights_for(self, actor, rids, member, channel=None) -> tuple[list[str], int, str]:
        """Mark `member` into these nights (offline sync — they told someone they'd
        cover it). The same one-or-all power as claiming for yourself, because "Ben says
        he'll do the rest of Tuesdays" is one thing that happened, not eight.

        Returns (nights filled, nights skipped, dominant skip reason) — the reason lets
        the single-night case keep its precise wording instead of a vague count."""
        filled: list[str] = []
        reasons: dict[str, int] = {}
        sids, requester_id = set(), None

        def note(k):
            reasons[k] = reasons.get(k, 0) + 1

        async with self._lock:
            for rid in rids:
                r = store.find_request(self.state, rid)
                if r is None:
                    note("closed")
                    continue
                if requester_id is None:
                    requester_id = r.get("requester_id")
                if is_locked(r):
                    note("locked")
                elif member.id == r.get("requester_id"):
                    note("requester")          # can't sub your own request
                elif store.is_involved(r, member.id):
                    note("already")
                elif store.add_sub(r, member.id, member.display_name, now=club_now()) == "added":
                    filled.append(r["game_ts"])
                    sids.add(r.get("series_id") or "")
                else:
                    note("full")
            if filled:
                self._save()
        skipped = sum(reasons.values())
        top = max(reasons, key=reasons.get) if reasons else ""
        if not filled:
            return ([], skipped, top)
        filled.sort()
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        await self._refresh_pages_for(rids, sids)
        if requester_id is not None and requester_id != actor.id:
            await self._dm_requester(
                requester_id,
                f"🥌 {actor.display_name} put {member.display_name} on {len(filled)} date"
                f"{'s' if len(filled) != 1 else ''} for you — {fmt_run(filled)}.")
        return (filled, skipped, top)

    @staticmethod
    def _is_repeat_click(cooldown: dict, key) -> bool:
        """Record this click and report whether it's a repeat of `key` within the
        debounce window. Caller should treat a repeat as a no-op. Call under the
        lock so two near-simultaneous clicks can't both pass."""
        now_m = time.monotonic()
        last = cooldown.get(key)
        cooldown[key] = now_m
        if len(cooldown) > 256:  # opportunistic prune of stale entries
            for k in [k for k, t in cooldown.items() if now_m - t >= CLICK_DEBOUNCE_SECONDS]:
                del cooldown[k]
        return last is not None and now_m - last < CLICK_DEBOUNCE_SECONDS

    async def add_availability(self, *, user, league_id, league, games, channel=None) -> str:
        async with self._lock:
            result = store.upsert_availability(
                self.state, user_id=user.id, name=user.display_name,
                league_id=league_id, league=league, games=games, now=club_now(),
            )
            self._save()
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        return result

    async def remove_availability(self, user_id: int, league_id, channel=None) -> bool:
        async with self._lock:
            removed = store.remove_availability(self.state, user_id, league_id)
            self._save()
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        return removed


    async def _dm(self, user_id: int, text: str, view: discord.ui.View | None = None) -> bool:
        """Best-effort DM. No channel fallback: if their DMs are closed, the reposted
        board still shows the change. Returns whether it landed.

        A DM may carry buttons — an auto-assignment is the one thing that happens to
        someone without them tapping anything, so the answer has to be one tap away in
        the same message. Those buttons are DynamicItems, so they keep working after a
        restart even though the DM is long gone from the bot's memory."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if view is not None:
                await user.send(text, view=view)
            else:
                await user.send(text)
            return True
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return False

    async def _dm_requester(self, user_id: int, text: str):
        """Tell a request's owner their game just gained or lost a sub."""
        await self._dm(user_id, text)

    async def fill_request_spot(self, user, rid: str, channel=None) -> tuple[str, dict | None]:
        """A sub self-fills an open request (hand-raise / alert-page claim). Adds them
        to the spot and DMs the requester. Returns (result, req) where result is
        "added" | "already" | "full" | "locked" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            if is_locked(req):
                return ("locked", req)
            result = store.add_sub(req, user.id, user.display_name, now=club_now())
            when = fmt_when(req["game_ts"])
            requester_id = req["requester_id"]
            opn = store.open_spots(req)
            self._save()
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        await self.refresh_page(req)
        if result == "added" and requester_id != user.id:
            await self._dm_requester(
                requester_id,
                f"🥌 {user.display_name} took a sub spot for your {when} game — {opn} still open.")
        return (result, req)

    async def close_request(self, rid: str, channel=None):
        async with self._lock:
            req = store.find_request(self.state, rid)
            alert = req.get("alert") if req is not None else None
            store.close_request(self.state, rid)
            self._save()
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        # Retire the alert page for the closed request.
        if alert and alert.get("message_id"):
            ch = await self._resolve_channel(alert["channel_id"])
            if ch is not None:
                try:
                    await ch.get_partial_message(alert["message_id"]).delete()
                except discord.HTTPException:
                    pass

    # -- super subs ---------------------------------------------------------
    async def _auto_assign(self, reqs: list[dict], *, actor_id=None, channel=None) -> dict:
        """Put each request's super sub(s) on it, then tell them.

        Called from every path that can create an unfilled spot on a team that has an
        arrangement: a fresh posting, a team being attached after the fact, and the
        moment an arrangement is made. Assignment is the ONLY thing that happens here
        without the sub having tapped anything, which is why every batch ends in a DM
        they can answer in one tap.

        A whole batch shares one assign_id so eight Thursdays are one DM, not eight —
        but the assignment itself is still per date (see the no-runs rule): dropping
        one leaves the others alone.

        Telling them is QUEUED, not sent here (see _queue_sub_notice): nine dates
        posted one at a time would otherwise be nine DMs.

        Returns {user_id: {"name", "isos"}}. Never holds the lock across a DM."""
        if not reqs:
            return {}
        aid = uuid.uuid4().hex[:8]
        batches: dict[int, dict] = {}
        told: dict[int, list] = {}       # requester_id -> dates covered for them
        async with self._lock:
            for r in reqs:
                live = store.find_request(self.state, r["id"])
                if live is None or is_locked(live) or store.open_spots(live) <= 0:
                    continue
                for g in store.standing_for(self.state, live.get("league_id", ""),
                                            live.get("team", "")):
                    # Priority order, and as many as the request has spots for: the
                    # second super sub isn't a spare, they're the second spot's answer.
                    if store.assign_auto(live, g["user_id"], g["name"], aid,
                                         now=club_now()) != "assigned":
                        continue
                    b = batches.setdefault(g["user_id"], {
                        "name": g["name"], "isos": [],
                        "team": live.get("team", ""), "league": live.get("league", "")})
                    b["isos"].append(live["game_ts"])
                    rid_owner = live.get("requester_id")
                    if rid_owner != g["user_id"]:
                        told.setdefault(rid_owner, []).append(live["game_ts"])
            if batches:
                self._save()
        for uid in batches:
            self._queue_sub_notice(uid)
        for requester_id, isos in told.items():
            if requester_id == actor_id or requester_id is None:
                continue
            names = ", ".join(sorted({b["name"] for b in batches.values()}))
            await self._dm(
                requester_id,
                f"\u2b50  {names} is your team's super sub and has been assigned to your "
                f"{fmt_run(isos)} game{'s' if len(isos) != 1 else ''}. "
                "You'll see it on the board \u2014 they've been asked to confirm.")
        return {uid: {"name": b["name"], "isos": b["isos"]} for uid, b in batches.items()}

    def _queue_sub_notice(self, user_id: int, new_arrangement: tuple | None = None):
        """Hold this person's assignment news briefly so it arrives as ONE message.

        Nine dates posted one at a time are nine assignments, but to the person on the
        other end it's one thing that happened. The message is built from the STORE at
        flush time, not from whatever triggered the queueing, so anything that lands
        during the window is simply included.

        A message lost to a restart is NOT lost for good: `notified` lives in the
        store, so startup re-queues anyone still owed one (see _requeue_unnotified).
        Without that, an assignment made moments before a restart went unannounced and
        then turned up glued to an unrelated message hours later, which is exactly how
        one message ends up reading as two."""
        slot = self._notices.setdefault(user_id, {"new": [], "task": None})
        if new_arrangement is not None and new_arrangement not in slot["new"]:
            slot["new"].append(new_arrangement)
        task = slot.get("task")
        if task is None or task.done():
            slot["task"] = asyncio.create_task(self._flush_notice_later(user_id))

    async def _flush_notice_later(self, user_id: int):
        try:
            if NOTIFY_WINDOW > 0:
                await asyncio.sleep(NOTIFY_WINDOW)
            await self._flush_notice(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad notice must not kill the task silently
            log.exception("Could not send a super sub notice")

    async def _flush_notice(self, user_id: int):
        """Send one person everything they haven't been told about yet."""
        slot = self._notices.pop(user_id, None)
        if slot is None:
            return
        reqs = store.auto_requests(self.state, user_id, only_unnotified=True)
        news = slot.get("new") or []
        if not reqs and not news:
            return
        groups: dict[tuple, list[str]] = {}
        for r in reqs:
            key = (stored_league(r.get("league", "")), r.get("team", ""))
            groups.setdefault(key, []).append(r["game_ts"])
        await self._dm(user_id, build_notice(news, groups),
                       view=AutoAssignView(len(reqs)) if reqs else None)
        # Marked whether or not the DM landed: someone with DMs closed can't be reached
        # by re-queuing forever, and the board and the chase both still cover them.
        async with self._lock:
            if store.mark_notified(self.state, user_id, reqs):
                self._save()

    async def _flush_all_notices(self):
        for uid in list(self._notices):
            slot = self._notices.get(uid) or {}
            task = slot.get("task")
            if task is not None and not task.done():
                task.cancel()
            await self._flush_notice(uid)

    async def add_standing(self, *, actor, member, league_id, league, team,
                           channel=None) -> tuple[str, list[str]]:
        """Make someone the super sub for a league + team. Returns (result, isos) where
        isos are open dates for that team they were assigned on the spot \u2014 an
        arrangement made mid-season should cover what's already asking."""
        async with self._lock:
            result = store.add_standing(
                self.state, user_id=member.id, name=member.display_name,
                league_id=league_id, league=league, team=team,
                created_by=actor.id, now=club_now())
            if result == "added":
                self._save()
        if result != "added":
            return (result, [])
        # The arrangement is made ON someone's behalf, so they hear it from the bot
        # rather than finding their name on a game. Queued, not sent: if it also sweeps
        # up dates that are already open, that news belongs in the SAME message.
        self._queue_sub_notice(
            member.id, (stored_league(league), team.strip(), actor.display_name))
        tk = (team or "").strip().casefold()
        targets = [r for r in store.requests_sorted(self.state)
                   if str(r.get("league_id") or "") == str(league_id or "")
                   and (r.get("team") or "").strip().casefold() == tk
                   and store.open_spots(r) > 0 and not is_locked(r)]
        assigned = await self._auto_assign(targets, actor_id=actor.id, channel=channel)
        if assigned:
            await self.render_board(self._guild_id(channel), fallback_channel=channel)
            for r in targets:
                await self.refresh_page(r)
        return (result, assigned.get(member.id, {}).get("isos", []))

    async def remove_standing(self, user_id: int, league_id, team: str, *, actor=None) -> bool:
        """End an arrangement. Dates already assigned STAY assigned — someone is
        expecting those to be covered, and un-assigning silently is how a team ends up
        short without anyone noticing. Take them off individually if that's the intent."""
        async with self._lock:
            removed = store.remove_standing(self.state, user_id, league_id, team)
            if removed:
                self._save()
        if removed and actor is not None and actor.id != user_id:
            await self._dm(
                user_id,
                f"\u2139\ufe0f  {actor.display_name} ended your super sub arrangement for "
                f"**{team}**. Any dates you're already on are still yours.")
        return removed

    async def confirm_auto(self, user) -> list[str]:
        """Acknowledge everything they're down for. Returns the dates confirmed — the
        caller names them back, so a date that arrived after the message they tapped is
        still something they see rather than something they silently agreed to."""
        done = []
        async with self._lock:
            for r in store.auto_requests(self.state, user.id, only_unconfirmed=True):
                if store.confirm_auto(r, user.id) == "confirmed":
                    done.append(r["game_ts"])
            if done:
                self._save()
        if done:
            await self.render_all_boards()
        return done

    async def drop_auto(self, user, rids, channel=None) -> list[str]:
        """A super sub drops dates they were assigned. Each one reopens as an ordinary
        request and re-alerts its own channel — the arrangement itself stands."""
        dropped, reopened = [], []
        async with self._lock:
            for rid in rids:
                req = store.find_request(self.state, rid)
                if req is None or is_locked(req):
                    continue
                if store.decline_auto(req, user.id) == "removed":
                    dropped.append(req["game_ts"])
                    reopened.append(req)
            if dropped:
                self._save()
        if not dropped:
            return []
        await self.render_all_boards()
        for req in reopened:
            await self._dm_requester(
                req["requester_id"],
                f"\u21a9\ufe0f  {user.display_name} can't make your {fmt_when(req['game_ts'])} "
                f"game after all \u2014 it's back on the board and the room has been alerted.")
            await self.post_page(req, reason="bump")
        return dropped

    async def set_team_for(self, user, rids, team: str,
                           channel=None) -> tuple[list[str], list[str], int]:
        """Attach a team to the caller's own teamless requests. Returns
        (dates set, dates that now collide with someone else's request for the same
        team, how many went to a super sub).

        The collision check runs BEFORE the team is written, so it finds other
        people's requests rather than this one. It warns and never merges: two open
        requests for one team on one draw might be two genuinely missing players."""
        team = (team or "").strip()
        if not team:
            return ([], [], 0)
        done, clashes, changed = [], [], []
        async with self._lock:
            for rid in rids:
                req = store.find_request(self.state, rid)
                if req is None or is_locked(req):
                    continue
                if req.get("requester_id") != user.id or (req.get("team") or "").strip():
                    continue
                clash = _find_open_duplicate(self.state, req.get("league_id", ""),
                                             req.get("game_ts", ""), team)
                req["team"] = team
                done.append(req["game_ts"])
                changed.append(req)
                if clash is not None and clash["id"] != req["id"]:
                    clashes.append(req["game_ts"])
            if done:
                self._save()
        if not done:
            return ([], [], 0)
        assigned = await self._auto_assign(changed, actor_id=user.id, channel=channel)
        await self.render_all_boards()
        for req in changed:
            await self.refresh_page(req)
        return (done, clashes, sum(len(b["isos"]) for b in assigned.values()))

    # -- background expiry --------------------------------------------------
    @tasks.loop(minutes=15)
    async def expiry_loop(self):
        async with self._lock:
            dropped = store.expire(self.state, club_now(), GRACE_HOURS,
                                   undated_days=UNDATED_DAYS)
            changed = bool(dropped["requests"] or dropped["availability"] or dropped["games"])
            if changed:
                self._save()
        # Retire alert pages for played-out requests.
        for r in dropped["requests"]:
            alert = r.get("alert") or {}
            if alert.get("message_id"):
                ch = await self._resolve_channel(alert["channel_id"])
                if ch is not None:
                    try:
                        await ch.get_partial_message(alert["message_id"]).delete()
                    except discord.HTTPException:
                        pass
        # A run's alert lives on its soonest open night. When that night is played and
        # pruned, its page goes with it — so hand the alert to the next open night
        # instead of letting the remaining weeks of the run go quiet.
        for sid in {str(r.get("series_id") or "") for r in dropped["requests"]} - {""}:
            nxt = next((r for r in store.series_requests(self.state, sid)
                        if store.open_spots(r) > 0 and not is_locked(r)), None)
            if nxt is not None and not (nxt.get("alert") or {}).get("message_id"):
                await self.post_page(nxt, reason="bump")
        if changed:
            await self.render_all_boards()

    @expiry_loop.before_loop
    async def _before_expiry(self):
        await self.bot.wait_until_ready()

    # -- unfilled-game reminder ---------------------------------------------
    @tasks.loop(minutes=10)
    async def reminder_loop(self):
        """Once per request, re-alert the room when a still-open game draws within
        REMINDER_HOURS of tip-off."""
        now = club_now()
        window = timedelta(hours=REMINDER_HOURS)
        due: list[str] = []
        async with self._lock:
            for r in self.state["requests"]:
                if store.open_spots(r) <= 0 or r.get("reminded"):
                    continue
                try:
                    game = datetime.fromisoformat(r["game_ts"])
                except (ValueError, KeyError, TypeError):
                    continue
                if now <= game <= now + window:
                    r["reminded"] = True
                    due.append(r["id"])
            if due:
                self._save()
        for rid in due:
            req = store.find_request(self.state, rid)
            if req and store.open_spots(req) > 0:
                await self.post_page(req, reason="reminder")
        await self._chase_unconfirmed(now)

    async def _chase_unconfirmed(self, now: datetime):
        """Once per request: a super sub was put on this date and has never answered,
        and the game is now inside ACK_HOURS.

        This is the one hole auto-assignment opens. Every other unfilled game is
        visibly unfilled; this one reads as covered on the board and might not be. So
        it gets said out loud, in the channel the request came from, while there is
        still time to find somebody else. The spot is NOT reopened — they are still
        the sub until they say otherwise."""
        window = timedelta(hours=ACK_HOURS)
        due = []
        async with self._lock:
            for r in self.state["requests"]:
                if r.get("ack_nudged") or not store.unconfirmed_auto(r):
                    continue
                try:
                    game = datetime.fromisoformat(r["game_ts"])
                except (ValueError, KeyError, TypeError):
                    continue
                if now <= game <= now + window:
                    r["ack_nudged"] = True
                    due.append(r["id"])
            if due:
                self._save()
        # Grouped by channel, and said ONCE: three unconfirmed dates in the same room
        # is one message naming all three, not three messages and three DMs on top.
        # The @-mention IS the notification, so there is no separate DM.
        by_channel: dict[object, list[dict]] = {}
        homeless: list[dict] = []
        for rid in due:
            req = store.find_request(self.state, rid)
            if req is None or not store.unconfirmed_auto(req):
                continue
            (by_channel.setdefault(req.get("channel_id"), [])
             if req.get("channel_id") is not None else homeless).append(req)
        for cid, reqs in by_channel.items():
            ch = await self._resolve_channel(cid)
            if ch is None:
                homeless += reqs
                continue
            people, rows = [], []
            for req in reqs:
                waiting = store.unconfirmed_auto(req)
                people += [f["user_id"] for f in waiting]
                rows.append(f"\u00b7 {stored_league(req.get('league',''))} \u00b7 "
                            f"{_req_for(req)} \u00b7 **{fmt_when(req['game_ts'])}**")
            mentions = " ".join(f"<@{u}>" for u in dict.fromkeys(people))
            head = ("\u23f0  **Not confirmed yet** \u2014 covered on paper, unconfirmed by "
                    "the sub:" if len(rows) > 1 else "\u23f0  **Not confirmed yet**")
            body = (f"{head}\n" + "\n".join(rows) + f"\n{mentions} \u2014 you're the super sub "
                    "for these but haven't confirmed. Tap **Confirm**, or drop what you "
                    "can't make so someone else can pick it up:")
            try:
                await ch.send(body, view=AutoAssignView(len(rows)),
                              allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException as e:
                log.warning("Could not post unconfirmed-assignment nudge: %s", e)
        for req in homeless:   # no channel to speak in — fall back to a DM
            for f in store.unconfirmed_auto(req):
                await self._dm(
                    f["user_id"],
                    f"\u23f0  Quick check \u2014 you're down as the sub for "
                    f"**{_req_for(req)}** on **{fmt_when(req['game_ts'])}** and haven't "
                    f"confirmed. Confirm below, or drop it if you can't make it.",
                    view=AutoAssignView(1))

    # -- teams posted after the fact ----------------------------------------
    @tasks.loop(hours=1)
    async def team_reconcile_loop(self):
        """Ask people to attach a team to requests they posted before the chair set
        the teams.

        A teamless request can't be matched against anything: the duplicate guard has
        no team to compare, so the same spot posted twice reads as two different asks
        and two subs turn up. The picker stops offering "no team" the moment a league
        has teams — this is what closes the ones already out there. Runs off the league
        cache, so it catches up within the hour of a refresh."""
        try:
            leagues = await self.get_leagues()
        except Exception as e:  # noqa: BLE001 — a cache miss must not kill the loop
            log.warning("Team reconcile: league fetch failed: %s", e)
            return
        teams = {str(l["id"]): (l.get("team_names") or []) for l in leagues}
        groups: dict[tuple, list[str]] = {}
        async with self._lock:
            for r in self.state["requests"]:
                if (r.get("team") or "").strip() or r.get("team_prompted"):
                    continue
                lid = str(r.get("league_id") or "")
                if not teams.get(lid) or is_locked(r):
                    continue
                # Marked whether or not the DM lands: someone with DMs closed can't be
                # reached by retrying every hour for a season, and their request is
                # still visible on the board either way.
                r["team_prompted"] = True
                groups.setdefault((r["requester_id"], lid), []).append(r["id"])
            if groups:
                self._save()
        for (uid, lid), rids in groups.items():
            league = next((l for l in leagues if str(l["id"]) == lid), None)
            view = discord.ui.View(timeout=None)
            view.add_item(SetTeamButton(lid, count=len(rids)))
            n = len(rids)
            await self._dm(
                uid,
                f"\U0001f3f7\ufe0f  **{league_label(league) if league else 'Your league'}** has "
                f"its teams posted now.\n"
                f"You have **{n}** sub request{'s' if n != 1 else ''} on it with no team "
                f"named. Naming the team means nobody can post a second request for the "
                f"same spot \u2014 and your team's super sub, if it has one, can pick it up.",
                view=view)

    @team_reconcile_loop.before_loop
    async def _before_team_reconcile(self):
        await self.bot.wait_until_ready()

    @reminder_loop.before_loop
    async def _before_reminder(self):
        await self.bot.wait_until_ready()

    # -- slash command ------------------------------------------------------
    # One bare command with an optional flag: `/subs` shows a private copy; add
    # `show:True` to instead post this server's shared board in the channel for all.
    @app_commands.command(
        name="subs",
        description="Show your subs board (private) — or post the shared board with show:True")
    @app_commands.describe(
        show="Post this server's shared board here for everyone (default: private, only you)")
    async def subs_cmd(self, interaction: discord.Interaction, show: bool = False):
        """Bare `/subs`: a private, ephemeral copy (only the caller sees it). `show:True`:
        (re)posts this server's shared board in the current channel, visible to all —
        that becomes the server's board. Data is shared across servers; each shows its
        own board."""
        if not show:
            await interaction.response.send_message(
                content="Your subs board (only you can see this):",
                embed=build_embed(self.state), view=build_view(self.state), ephemeral=True)
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Use `show:True` in a server channel to post the board.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._post_board(interaction.channel)
        await interaction.followup.send(
            "🥌  Posted the subs board here. It refreshes in place, and hops to the "
            "bottom whenever something changes on this server.", ephemeral=True)

    @app_commands.command(
        name="supersub",
        description="See or set the super subs — who gets auto-assigned when a team needs one")
    async def supersub_cmd(self, interaction: discord.Interaction):
        """Private by design: an arrangement is club information, but changing one is
        a quiet administrative act, not a channel event. The person being made a super sub
        sub always hears about it by DM, which is the notification that matters."""
        leagues = await self.get_leagues()
        body = ("\u2b50  **Super subs** \u2014 when a team needs a sub, its super sub is put on it "
                "straight away and DM'd to confirm.\n\n" + standing_summary(self.state))
        if leagues and not leagues_with_teams(leagues):
            body += ("\n\n_No league has its teams posted yet, so there's nothing to attach "
                     "an arrangement to. It's per team \u2014 come back once the chair sets "
                     "them._")
        await interaction.response.send_message(
            content=body, view=StandingHomeView(self.state, leagues), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Subs(bot))
