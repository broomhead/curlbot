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

  ➕ Need a sub      — league → team → game → spots (picked from the system). The
                       TEAM is optional — chairs often don't set teams until a day
                       or two before the first draw. The GAME never is: when you
                       need a sub is the point. A league with no schedule posted
                       still offers real dates, projected weekly from its title's
                       start date onto its own night.
  🙋 I'm free        — list your availability so you get tagged for matching games.
  ➕ Fill for someone — mark another member into an open spot (offline sync).
  ➖ Remove          — cancel a sub (click a name → confirm), cancel a request you
                       opened, or clear your availability.
  🙋 <game>          — one hand-raise button per open game; one tap takes the spot.

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
import html
import time
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
MAX_BUTTON_REQUESTS = 20  # Discord caps a message at 25 components; reserve a row for controls.
# Several buttons act on shared state and can be impatiently double-tapped before
# the first click visibly resolves. We ignore a repeat click (same user, same
# target) within this window so a double-tap is idempotent: a "Take a spot" toggle
# can't take-then-drop, and a Confirm/Decline can't clobber its own result.
CLICK_DEBOUNCE_SECONDS = 3.0

CID_NEW     = "sub:new"
CID_AVAIL   = "sub:avail"
CID_FILLFOR = "sub:fillfor"
CID_REMOVE  = "sub:remove"
# Per-request one-tap claim/hand-raise button (alert page + board): "sub:take:<rid>".
CID_TAKE_PREFIX = "sub:take:"


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


# ── Board rendering ─────────────────────────────────────────────────────────

BOARD_TITLE = f"Subs Board — {CLUB_NAME}"


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
    names = [f["name"] for f in req.get("filled", [])]
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


def build_embed(state: dict) -> discord.Embed:
    """One combined, date-ordered board. A game appears if it has a request OR if
    anyone is available for it. Under each date: the sub spots (traffic-light status,
    with the names of whoever is in), then the available subs not yet assigned.
    General (any-time) availability is summarized at the bottom."""
    reqs = store.requests_sorted(state)
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

    blocks = []
    for k in order[:MAX_BUTTON_REQUESTS]:
        grp = groups[k]
        lines = [f"**{grp['label']}**"]
        for r in grp["reqs"]:
            lines.append(_req_status_line(r))
        free = _available_for_group(state, grp, k)
        if free:
            lines.append(f"{INDENT}available: {', '.join(free)}")
        blocks.append("\n".join(lines))
    if len(order) > MAX_BUTTON_REQUESTS:
        blocks.append(f"…and {len(order) - MAX_BUTTON_REQUESTS} more game(s).")

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

    e.set_footer(text="🔴 none · 🟡 partial · 🟢 filled — tap a game button below to take a spot")
    return e


def build_view(state: dict) -> discord.ui.View:
    """Row 0 = the four verbs; below that, one 🙋 hand-raise button per open game so
    claiming a spot is a single tap (no form)."""
    view = discord.ui.View(timeout=None)
    view.add_item(NewRequestButton())   # ➕ Need a sub
    view.add_item(AvailableButton())    # 🙋 I'm free
    view.add_item(FillForButton())      # ✍️ Fill for someone
    view.add_item(RemoveButton())       # ✖️ Remove
    # A game within LOCK_MINUTES of tip-off is frozen — no hand-raise button for it.
    open_reqs = [r for r in store.requests_sorted(state)
                 if store.open_spots(r) > 0 and not is_locked(r)]
    for i, r in enumerate(open_reqs[:20]):   # rows 1–4, 5 buttons each
        who = r["team"] if r.get("team") else first_name(r.get("requester_name", ""))
        when = fmt_when_short(r["game_ts"]) if r.get("game_ts") else "TBD"
        label = _truncate(f"{when} {who}", 80)
        view.add_item(PageClaimButton(r["id"], label=label,
                                      style=discord.ButtonStyle.success, row=1 + i // 5))
    return view


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
        view = NeedSubFlowView(leagues)
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


class FillForButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:fillfor"):
    """Mark someone ELSE into an open spot (offline sync) — they told you they'd cover
    it, so you record it for them."""
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Fill for someone", emoji="➕",
            style=discord.ButtonStyle.success, custom_id=CID_FILLFOR, row=0))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        open_reqs = [r for r in store.requests_sorted(cog.state)
                     if store.open_spots(r) > 0 and not is_locked(r)]
        if not open_reqs:
            await interaction.response.send_message("No open spots to fill right now.", ephemeral=True)
            return
        await interaction.response.send_message(
            "**Fill for someone** — pick the game, then choose the teammate to mark in:",
            view=FillForView(cog.state), ephemeral=True)


class FillForView(discord.ui.View):
    def __init__(self, state: dict):
        super().__init__(timeout=300)
        self.state = state
        self.rid: str | None = None
        self.build()

    def build(self) -> "FillForView":
        self.clear_items()
        open_reqs = [r for r in store.requests_sorted(self.state)
                     if store.open_spots(r) > 0 and not is_locked(r)]
        self.add_item(FillForPick(open_reqs, self.rid, row=0))
        if self.rid and store.find_request(self.state, self.rid):
            self.add_item(FillForMemberSelect(self.rid, row=1))
        return self

    def prompt(self) -> str:
        if not self.rid:
            return "**Fill for someone** — pick the game, then choose the teammate to mark in:"
        req = store.find_request(self.state, self.rid)
        when = fmt_when(req["game_ts"]) if req else "that game"
        return f"**Fill for someone** · {when} — choose the teammate to mark in:"


class FillForPick(discord.ui.Select):
    def __init__(self, reqs: list[dict], selected, row: int = 0):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(f"{fmt_when_short(r['game_ts'])} · {_req_for(r)}", 100),
                value=r["id"],
                description=_truncate(f"{store.open_spots(r)} open", 100),
                default=(r["id"] == selected),
            )
            for r in reqs[:25]
        ])
        if not opts:
            opts = [discord.SelectOption(label="No open spots right now", value="__none__")]
        super().__init__(placeholder="Which game…", min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        self.view.rid = self.values[0]
        await interaction.response.edit_message(content=self.view.prompt(), view=self.view.build())


class FillForMemberSelect(discord.ui.UserSelect):
    def __init__(self, rid: str, row: int = 1):
        self.rid = rid
        super().__init__(placeholder="Choose the teammate to mark in…", min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        member = self.values[0]
        if cog._is_repeat_click(cog._click_cooldown, ("fillfor", interaction.user.id, self.rid, member.id)):
            return
        result, req = await cog.fill_spot_for(interaction.user, self.rid, member, interaction.channel)
        when = fmt_when(req["game_ts"]) if req else "that game"
        msgs = {
            "added": f"✅  Marked **{member.display_name}** in for **{when}** — they and the requester were notified.",
            "already": f"**{member.display_name}** is already on that request.",
            "requester": "That's the requester — they can't sub their own request.",
            "full": "No open spots left on that request.",
            "locked": f"**{when}** starts too soon — the roster's locked.",
            "closed": "That request is no longer on the board.",
        }
        await interaction.edit_original_response(content=msgs.get(result, "Done."), view=None)


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
    """Team is OPTIONAL. Chairs often don't lock in teams until a day or two before
    the first draw, but people need to line up subs before that — so a request can
    always be posted as "this person needs a sub" with no team attached."""

    def __init__(self, names: list[str], selected, row: int = 1):
        # Only teams the system knows about — no free-typing. Alphabetized.
        opts = _unique_options([
            discord.SelectOption(label=_truncate(n, 100), value=_truncate(n, 100), default=(n == selected))
            for n in sorted(names, key=str.casefold)[:24]
        ])
        no_teams_yet = not opts
        opts.append(discord.SelectOption(
            label=("Teams aren't set yet — post without one" if no_teams_yet else "No team / not sure"),
            value=NO_TEAM,
            description="Posts as “needs a sub”, no team named",
            default=(selected == ""),
        ))
        super().__init__(
            placeholder=("Teams aren't set yet — optional" if no_teams_yet else "Your team… (optional)"),
            min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.team = "" if self.values[0] == NO_TEAM else self.values[0]
        await self.view.refresh(interaction)


class GameSelect(discord.ui.Select):
    def __init__(self, games: list[dict], selected_isos, *, multi: bool, row: int = 2):
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
            placeholder=("Games you can cover…" if multi else "Which game…"),
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
    def __init__(self, selected: int, row: int = 3):
        opts = [
            discord.SelectOption(label=f"{n} spot{'s' if n > 1 else ''} needed", value=str(n), default=(n == selected))
            for n in range(1, 5)
        ]
        super().__init__(placeholder="How many subs needed…", min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.spots = int(self.values[0])
        await self.view.refresh(interaction)


# ── Need-a-sub flow (ephemeral, league → team → game → spots → details) ──────

class NeedSubFlowView(discord.ui.View):
    def __init__(self, leagues: list[dict]):
        super().__init__(timeout=300)
        self.leagues = leagues
        self.league_id = None
        self.team = None
        self.game_iso = None
        self.spots = 1
        self.message = None
        self.posted = False  # one-shot guard: a posted flow can't post again
        self.build()

    def league(self) -> dict | None:
        return next((l for l in self.leagues if str(l["id"]) == str(self.league_id)), None)

    def on_league_change(self):
        self.team = None
        self.game_iso = None

    def build(self) -> "NeedSubFlowView":
        self.clear_items()
        self.add_item(LeagueSelect(self.leagues, self.league_id, row=0))
        lg = self.league()
        if lg:
            names = lg.get("team_names") or []
            # Team is picked from the league's roster only (no free-typing).
            self.add_item(TeamSelect(names, self.team, row=1))
            self.add_item(GameSelect(
                game_options(lg, club_now()),
                [self.game_iso] if self.game_iso else [],
                multi=False, row=2,
            ))
            self.add_item(SpotsSelect(self.spots, row=3))
            self.add_item(PostNeedButton(disabled=not self.ready(), row=4))
        return self

    def ready(self) -> bool:
        # League + game. WHEN you need a sub is the point of the request, so a
        # date is never optional — for an unscheduled league the picker projects
        # real league nights off the title's start date rather than letting the
        # request go out dateless (see projected_games). Only the TEAM is
        # optional, since chairs set teams late.
        return self.league() is not None and bool(self.game_iso)

    def prompt(self) -> str:
        lg = self.league()
        if not lg:
            return "**Need a sub** — pick the league:"
        parts = [f"League: **{league_label(lg)}**"]
        parts.append(f"Team: **{self.team}**" if self.team else "Team: **not set**")
        parts.append(f"Game: **{fmt_when(self.game_iso)}**" if self.game_iso
                     else "Game: **not set**")
        parts.append(f"Spots: **{self.spots}**")
        tail = ("Press **Post request**." if self.ready()
                else "Pick which game — the team is optional if it isn't set yet.")
        return "**Need a sub** — " + " · ".join(parts) + f"\n{tail}"

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class PostNeedButton(discord.ui.Button):
    def __init__(self, *, disabled: bool, row: int = 4):
        super().__init__(label="Post request", emoji="✅", style=discord.ButtonStyle.success, row=row, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        f: NeedSubFlowView = self.view
        # One-shot guard: posting isn't idempotent (each call appends a new
        # request), so an impatient double-tap would post twice. Set the flag
        # synchronously — before any await — so a second callback (which can only
        # run once this one yields) sees it and bails. Belt-and-braces with the
        # button being removed from the view on success below.
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
        status, req = await cog.add_request(
            requester=interaction.user,
            league_id=f.league_id or "",
            league=title,
            team=f.team or "",
            game_ts=f.game_iso,
            spots=f.spots,
            channel=interaction.channel,
        )
        if status == "duplicate":
            # Don't stack an identical request on the board. Keep the flow open so
            # they can tweak the team/game and re-post if it really is different.
            f.posted = False
            who = f"**{f.team}**" if f.team else "**you**"
            await interaction.edit_original_response(
                content=(f"⚠️  There's already an open request for {who} · "
                         f"{fmt_when(f.game_iso)}. Claim or **Remove** that one instead — "
                         "or change the team/game below and re-post.\n\n" + f.prompt()),
                view=f.build(),
            )
            return
        # No invites/DMs — anyone available is tagged on the board's alert and can
        # just hit "I'll take it".
        await interaction.edit_original_response(
            content=(f"✅  Posted: **{title}** · {f.team or 'no team named'} · "
                     f"{fmt_when(f.game_iso)} · needs {f.spots}.\n"
                     "It's on the board — anyone available has been tagged and can hit **I'll take it**."),
            view=None)


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
                self.add_item(GameSelect(games, self.game_isos, multi=True, row=row))
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


def _my_requests(state: dict, uid: int) -> list[dict]:
    return [r for r in store.requests_sorted(state) if r.get("requester_id") == uid]


class RemoveAvailSelect(discord.ui.Select):
    """Remove one of the caller's availability listings (keyed by league)."""
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
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        league_id = "" if self.values[0] == "__nolg__" else self.values[0]
        removed = await cog.remove_availability(interaction.user.id, league_id, interaction.channel)
        msg = "🗑️  Removed that availability listing." if removed else "That listing was already gone."
        await interaction.edit_original_response(content=msg, view=None)


# ── Alert page (public "sub needed" message with a one-tap claim button) ──────

class PageView(discord.ui.View):
    def __init__(self, rid: str):
        super().__init__(timeout=None)
        self.add_item(PageClaimButton(rid))


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


# ── The cog ─────────────────────────────────────────────────────────────────

class Subs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = store.load(STORE_PATH)
        self._lock = __import__("asyncio").Lock()
        # namespaced-key -> monotonic time of last click, for debouncing impatient
        # double-taps across all buttons. Keys are tagged tuples, e.g.
        # ("take", user_id, rid) or ("manage_add", user_id, rid, member_id).
        self._click_cooldown: dict[tuple, float] = {}

    # -- lifecycle ----------------------------------------------------------
    async def cog_load(self):
        self.expiry_loop.start()
        self.reminder_loop.start()

    async def cog_unload(self):
        self.expiry_loop.cancel()
        self.reminder_loop.cancel()

    async def startup(self):
        """Prune expired requests and re-render every server's board after a (re)connect."""
        async with self._lock:
            store.expire(self.state, club_now(), GRACE_HOURS, undated_days=UNDATED_DAYS)
            store.save(STORE_PATH, self.state)
        await self.render_all_boards()

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
    def _page_body(self, req: dict, *, reason: str) -> str:
        heads = {
            "new":      "🆘 **Sub needed**",
            "bump":     "🔔 **Still need a sub**",
            "reminder": "⏰ **Game soon — still need a sub**",
        }
        league = stored_league(req.get("league", ""))
        when = fmt_when(req["game_ts"])
        opn = store.open_spots(req)
        detail = " · ".join(x for x in [
            league or None, _req_for(req), when,
            f"{opn} spot{'s' if opn != 1 else ''} open",
        ] if x)
        subs = _availability_for_request(self.state, req)
        mentions = " ".join(f"<@{a['user_id']}>" for a in subs)
        if mentions:
            tail = f"{mentions} — you're listed as available. Tap to grab it:"
        else:
            tail = "_No one's listed as available yet — first to tap grabs it:_"
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
        # request made on LSCC pings on LSCC even though the data is shared.
        ch = channel or await self._resolve_channel(req.get("channel_id"))
        if ch is None:
            return
        await self._delete_page(req)
        body = self._page_body(req, reason=reason)
        try:
            msg = await ch.send(body, view=PageView(req["id"]),
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
        still_open = store.find_request(self.state, req["id"]) is not None and store.open_spots(req) > 0
        try:
            if still_open:
                await partial.edit(content=self._page_body(req, reason="new"), view=PageView(req["id"]))
                return
            await partial.edit(content=f"✅  Covered — thanks! ({fmt_when(req['game_ts'])})", view=None)
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
    async def add_request(self, *, requester, spots, game_ts="", league_id="", league="",
                          team="", channel=None):
        """Create a request, unless an open one already exists for the same league +
        game + team. Returns (status, req): ("duplicate", existing) or ("created", new).
        The dup check + create happen under one lock so two posts can't both slip in.
        `game_ts` and `team` are both optional — a request may be no more than
        "<name> needs a sub in <league>"."""
        async with self._lock:
            dup = _find_open_duplicate(self.state, league_id, game_ts, team,
                                       requester_id=requester.id)
            if dup is not None:
                return ("duplicate", dup)
            req = store.new_request(
                self.state,
                requester_id=requester.id,
                requester_name=requester.display_name,
                game_ts=game_ts,
                spots_needed=spots,
                league_id=league_id,
                league=league,
                team=team,
                guild_id=self._guild_id(channel),
                channel_id=getattr(channel, "id", None),
                now=club_now(),
            )
            self._save()
        # Refresh this server's board (create it here if it has none yet), then post
        # the public alert page in this channel — it pings the members available for
        # this game and carries a one-tap "I'll take it" button.
        await self.render_board(self._guild_id(channel), fallback_channel=channel)
        await self.post_page(req, reason="new", channel=channel)
        return ("created", req)


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


    async def _dm_requester(self, user_id: int, text: str):
        """Best-effort DM to a request's owner — the only DM the bot still sends, to
        tell them their game just gained or lost a sub. No channel fallback: if their
        DMs are closed, the reposted board still shows the change."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(text)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

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

    async def fill_spot_for(self, actor, rid: str, member, channel=None) -> tuple[str, dict | None]:
        """Any member marks `member` into an open spot on request `rid` (offline sync).
        Direct fill — no confirmation. DMs the requester that their game got a sub, and
        reposts the acting server's board. Returns (result, req):
        "added" | "already" | "requester" | "full" | "locked" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            if is_locked(req):
                return ("locked", req)
            if member.id == req.get("requester_id"):
                return ("requester", req)  # can't sub your own request
            result = store.add_sub(req, member.id, member.display_name, now=club_now())
            when = fmt_when(req["game_ts"])
            requester_id = req["requester_id"]
            opn = store.open_spots(req)
            self._save()
        if result != "added":
            return (result, req)
        await self.bump_board(self._guild_id(channel), fallback_channel=channel)
        await self.refresh_page(req)
        if requester_id != actor.id:
            await self._dm_requester(
                requester_id,
                f"🥌 {actor.display_name} added {member.display_name} as a sub for your {when} game "
                f"— {opn} still open.")
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Subs(bot))
