"""
Curling Club Practice-Ice Bot
Slash command: /sheets [weeks] [stats] [show]  (stats:True = streak records, show:True = post board)

Reports practice ice from four sources, in time order: designated practice
blocks, Learn-to-Curls that don't fill the ice, league draws that don't fill the
ice, and facility-reserved curling ice the club hasn't booked over yet (the
rink's public iCal feed, via pond_ice). Sheet counts come from Gravity Forms
(LTC/private events) and the public league pages (leagues, via league_client).
No estimates — unconfirmed sessions are flagged as such.

Configure the target site and club name via environment variables
(SITE_BASE_URL, CLUB_NAME) — see .env.example.
"""

import asyncio
import json
import math
import os
import re
import time
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from gf_client import GFClient
from ice import PEOPLE_PER_SHEET, TOTAL_SHEETS, sheets_for_people
from league_client import get_cached_leagues, draw_to_datetime
import instructors
import practice_ice as pi
import practice_store as ps
import pond_ice
import subs

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
# Dev mode: set DEV_GUILD_ID to your server's ID to sync slash commands to that
# one guild — they appear instantly, instead of the ~1h global propagation.
# Leave unset in production for a normal global sync.
DEV_GUILD_ID  = int(os.environ["DEV_GUILD_ID"]) if os.environ.get("DEV_GUILD_ID") else None
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://example.com")
CLUB_NAME     = os.environ.get("CLUB_NAME", "Curling Club")
WP_API        = f"{SITE_BASE_URL}/wp-json/tribe/events/v1"
# NUM_SHEETS / PEOPLE_PER_SHEET and sheets_for_people() come from ice.py (imported
# above) so the /sheets report and the instructor board can never disagree about
# how much ice a headcount needs.
# How long a session keeps showing in /sheets after it ends, so latecomers still
# see a just-finished slot. Past this grace it drops off (set 0 for a hard cutoff
# at end time). Applies to every source (practice/LTC/league/reserved ice).
SHEETS_GRACE_HOURS = float(os.environ.get("SHEETS_GRACE_HOURS", "1"))
TIMEZONE_OFFSET = -5  # America/Chicago (CST = UTC-5, CDT = UTC-6)

# Facility-reserved curling ice (the rink's own public Google calendar). The
# club can use these blocks but they're not on the club calendar until something
# is booked, so we surface any that nothing club-side overlaps as open practice
# ice. POND_ICS_URLS = comma-separated public iCal URLs (kept in .env, never in
# the repo); POND_MATCH = title keyword that marks a curling block.
POND_ICS_URLS = [u.strip() for u in os.environ.get("POND_ICS_URLS", "").split(",") if u.strip()]
POND_MATCH    = os.environ.get("POND_MATCH", "curl")
POND_CACHE_TTL = int(os.environ.get("POND_CACHE_TTL", "21600"))  # 6h
# /sheets makes several network calls (calendar + Gravity Forms). That data changes
# slowly, so cache the built opportunities and reuse them for SHEETS_CACHE_TTL —
# same 6h cadence as the league cache. In-memory, so it also refetches once per app
# launch. See fetch_opps_cached.
SHEETS_CACHE_TTL = int(os.environ.get("SHEETS_CACHE_TTL", "21600"))  # 6h

# Practice sign-up pool (shared across members; interactions stay private).
PRACTICE_STORE_PATH = os.environ.get("PRACTICE_STORE_PATH", "practice_signups.json")
_practice_state = ps.load(PRACTICE_STORE_PATH)
_practice_lock = asyncio.Lock()

# Debounce impatient double-taps of a practice signup button. ps.toggle is a
# join↔leave toggle, so without this a quick double-click would join then
# instantly leave. (user_id, session_key) -> monotonic time of last click.
CLICK_DEBOUNCE_SECONDS = 3.0
_practice_cooldown: dict[tuple[int, str], float] = {}


def _is_repeat_click(cooldown: dict, key) -> bool:
    """Record this click and report whether it repeats `key` within the debounce
    window; callers treat a repeat as a no-op. Call under _practice_lock."""
    now_m = time.monotonic()
    last = cooldown.get(key)
    cooldown[key] = now_m
    if len(cooldown) > 256:  # opportunistic prune of stale entries
        for k in [k for k, t in cooldown.items() if now_m - t >= CLICK_DEBOUNCE_SECONDS]:
            del cooldown[k]
    return last is not None and now_m - last < CLICK_DEBOUNCE_SECONDS

PRACTICE_SLUG = "practice"

PRIVATE_EVENT_FORM_ID = 116   # "Private Event Registration"
LTC_FORM_ID           = 110   # "Learn to Curl Registration"

PRICE_PER_PERSON = 55   # private event rate per person

# League draws expose a start time but no end; assume a standard draw length
# (typical evening leagues run ~2h15m) for overlap math.
LEAGUE_DRAW_DURATION_MIN = 135


def now_club() -> datetime:
    """Current club-local time (naive), matching calendar/draw datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)

# ── Events API (no auth) ───────────────────────────────────────────────────────

async def fetch_events_on_day(date: datetime) -> list[dict]:
    day_start = date.replace(hour=0,  minute=0,  second=0)
    day_end   = date.replace(hour=23, minute=59, second=59)
    return await fetch_events_in_range(day_start, day_end)


async def fetch_events_in_range(start: datetime, end: datetime) -> list[dict]:
    """All published events overlapping [start, end] in one paged sweep
    (replaces day-by-day calls — a multi-week window is a single request set)."""
    base = {
        "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date":   end.strftime("%Y-%m-%d %H:%M:%S"),
        "per_page": 50, "status": "publish",
    }
    out, page = [], 1
    async with aiohttp.ClientSession() as s:
        while True:
            async with s.get(f"{WP_API}/events", params={**base, "page": page}) as r:
                data = await r.json()
            batch = data.get("events", [])
            out.extend(batch)
            if page >= int(data.get("total_pages", 1) or 1) or not batch:
                break
            page += 1
    return out


async def fetch_practices_within(weeks: int) -> list[dict]:
    """Practice blocks starting within the next `weeks` weeks (the /sheets window)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)
    window_end = now + timedelta(weeks=weeks)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{WP_API}/events", params={
            "categories": PRACTICE_SLUG,
            "start_date": now.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": window_end.strftime("%Y-%m-%d %H:%M:%S"),
            "per_page": 100, "status": "publish",
        }) as r:
            events = (await r.json()).get("events", [])
    # Defensive: keep only blocks that actually start inside the window, in case the
    # calendar API is lenient about end_date.
    return [e for e in events
            if e.get("start_date") and datetime.fromisoformat(e["start_date"]) < window_end]


# ── Sheet lookup via Gravity Forms ─────────────────────────────────────────────

def event_slugs(event: dict) -> set[str]:
    return {c["slug"] for c in event.get("categories", [])}

def is_practice(event: dict) -> bool:
    return PRACTICE_SLUG in event_slugs(event)

# Staff mark an event closed in its title before the inventory cap is reached.
# A closed LTC takes the whole rink regardless of the registered headcount.
FULL_TITLE_MARKERS = ("registration full", "sold out", "wait list", "waitlist")

def event_is_full(event: dict) -> bool:
    title = str(event.get("title", "")).lower()
    return any(m in title for m in FULL_TITLE_MARKERS)

def _people(value) -> int:
    """Parse a 'Number of Entries' (group size) value to an int; 0 if unparseable."""
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return 0

def overlaps(e: dict, practice: dict) -> bool:
    es = datetime.fromisoformat(e["start_date"])
    ee = datetime.fromisoformat(e["end_date"])
    ps = datetime.fromisoformat(practice["start_date"])
    pe = datetime.fromisoformat(practice["end_date"])
    return es < pe and ee > ps


async def entries_for_event(gf: GFClient, form_id: int, event_post_id: int) -> list[dict]:
    """Fetch GF entries whose source_id matches the WP event post ID."""
    return await gf.entries(form_id, params={
        "paging[page_size]": 200,
        "search": '{"field_filters":[{"key":"source_id","value":"' + str(event_post_id) + '"}]}',
    })


def _normalize_title(s: str) -> str:
    """Lowercase, normalise dashes, drop full-markers, collapse to alnum words."""
    s = str(s).lower()
    s = re.sub(r"&#8211;|&#8212;|–|—", "-", s)
    for marker in FULL_TITLE_MARKERS:
        s = s.replace(marker, "")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


async def ltc_entries_for_event(gf: GFClient, event: dict) -> list[dict]:
    """
    LTC registrations for an event, matched by event DATE (form field 31, ISO),
    not source_id — entries for one event are split across several source_ids.
    Field 31 includes the year, so it won't collide across seasons.

    If two LTCs share a date, the pool has more than one Event Name (field 34);
    in that case keep only entries whose Event Name matches this event's title.
    """
    date_iso = datetime.fromisoformat(event["start_date"]).strftime("%Y-%m-%d")
    pool = await gf.entries(LTC_FORM_ID, params={
        "paging[page_size]": 200,
        "search": json.dumps({"field_filters": [
            {"key": "31", "operator": "is", "value": date_iso}
        ]}),
    })
    names = {str(e.get("34", "")) for e in pool}
    if len(names) > 1:  # multiple LTCs that day — disambiguate by Event Name
        target = _normalize_title(event.get("title", ""))
        pool = [e for e in pool if _normalize_title(e.get("34", "")) == target]
    return pool


async def sheets_for_event(event: dict, gf: GFClient) -> int | None:
    """
    Return sheets used by a concurrent event, or None if no GF data exists for this type.
    Raises on GF API errors — caller surfaces them to the user.
    """
    slugs    = event_slugs(event)
    event_id = event["id"]  # WP post ID — matches source_id in GF entries

    # ── Private events: price paid ÷ PRICE_PER_SHEET → sheets ─────────────
    if "privateevents" in slugs:
        entries = await entries_for_event(gf, PRIVATE_EVENT_FORM_ID, event_id)
        if not entries:
            return None
        # Field 14.2 holds the negotiated fee, e.g. "$1,000.00"
        raw = entries[0].get("14.2", "0")
        price = float(str(raw).replace("$", "").replace(",", "").strip() or "0")
        people = price / PRICE_PER_PERSON
        # Uncapped: a big enough booking can imply more ice than the club has,
        # and the caller wants to see that rather than a silent clamp.
        return sheets_for_people(people, cap=False)

    # ── Learn to Curls ────────────────────────────────────────────────────
    if "learn-to-curls" in slugs or "ltc-instructional-leagues" in slugs:
        # Staff-closed LTCs ("REGISTRATION FULL" etc.) take the whole rink,
        # even below the 32-person cap — headcount ÷ 8 can't detect this.
        if event_is_full(event):
            return TOTAL_SHEETS
        # Matched by event date (field 31), not source_id — see ltc_entries_for_event.
        entries = await ltc_entries_for_event(gf, event)
        if not entries:
            return None
        # Field 2 = "Number of Entries" (group size per submission); sum = people.
        total_people = sum(_people(e.get("2")) for e in entries)
        if total_people == 0:
            return None
        return sheets_for_people(total_people)

    # ── Other categories (leagues, bonspiels, etc.): no reliable data ─────
    # Leagues: team vs individual registration mix makes counting unreliable.
    # Bonspiels: no GF form mapped.
    return None


# ── Session collection (all three practice-ice sources) ────────────────────────

async def collect_sessions(practices, window_start, window_end, gf) -> list[dict]:
    """
    Build a list of ice sessions in [window_start, window_end] from all three
    sources: designated practice blocks, LTC/private calendar events, and league
    draws. Each session has start/end/type/title/sheets_used (None = unknown).
    """
    sessions: list[dict] = []

    # 1. Designated practice blocks — occupy no sheets themselves. Skip any that
    #    already ended or start past the window: the calendar API's start_date filter
    #    is day-granular, so it can hand back a block from earlier today/yesterday.
    for p in practices:
        es = datetime.fromisoformat(p["start_date"])
        ee = datetime.fromisoformat(p["end_date"])
        if ee <= window_start or es >= window_end:
            continue
        sessions.append({
            "start": es,
            "end":   ee,
            "type":  "Practice",
            "title": p["title"],
            "sheets_used": 0,
        })

    # 2. LTC / private events on the calendar — one ranged fetch, then look up
    #    every event's sheet count concurrently.
    seen: set = set()
    candidates: list[tuple] = []
    for e in await fetch_events_in_range(window_start, window_end):
        if e["id"] in seen or is_practice(e):
            continue
        es = datetime.fromisoformat(e["start_date"])
        ee = datetime.fromisoformat(e["end_date"])
        if ee <= window_start or es >= window_end:
            continue
        slugs = event_slugs(e)
        if {"learn-to-curls", "ltc-instructional-leagues"} & slugs:
            typ = "LTC"
        elif "privateevents" in slugs:
            typ = "Private"
        else:
            continue  # bonspiels/social/etc. aren't practice-ice sources
        seen.add(e["id"])
        candidates.append((e, es, ee, typ))

    counts = await asyncio.gather(*(sheets_for_event(e, gf) for e, _, _, _ in candidates))
    for (e, es, ee, typ), n in zip(candidates, counts):
        sessions.append({
            "start": es, "end": ee, "type": typ,
            "title": e["title"], "sheets_used": n,
        })

    # 3. League draws (from the cached league pages).
    try:
        leagues = await get_cached_leagues()
    except Exception as ex:  # noqa: BLE001
        log.warning("League data unavailable: %s", ex)
        leagues = []
    for lg in leagues:
        if lg.get("ended"):
            continue
        for d in lg.get("upcoming_draws", []):
            dt = draw_to_datetime(d)
            if dt is None:
                continue
            dend = dt + timedelta(minutes=LEAGUE_DRAW_DURATION_MIN)
            if dend <= window_start or dt >= window_end:
                continue
            sessions.append({
                "start": dt, "end": dend, "type": "League",
                # Same de-noising the subs board uses: "Thursday League – Summer
                # 2026 League 3 – Begins August 6" → "Thursday League". The row
                # already carries this draw's date/time, so no range here.
                "title": subs.league_name(lg.get("title", "")) or "League",
                "sheets_used": d.get("sheets_used"),
            })

    # 4. Facility-reserved curling ice (the rink's public calendar). Surface a
    #    block only when nothing the club already has booked overlaps it — an
    #    overlap means that ice is already represented by another row above.
    if POND_ICS_URLS:
        club_sessions = list(sessions)  # snapshot before adding reserved blocks
        try:
            reserved = await pond_ice.fetch_reserved_curling(
                POND_ICS_URLS, window_start, window_end, POND_CACHE_TTL, POND_MATCH)
        except Exception as ex:  # noqa: BLE001 — a feed hiccup must not break /sheets
            log.warning("Reserved-ice fetch failed: %s", ex)
            reserved = []
        for rv in reserved:
            if any(pi.overlaps(rv["start"], rv["end"], s["start"], s["end"]) for s in club_sessions):
                continue
            sessions.append({
                "start": rv["start"], "end": rv["end"],
                "type": "Reserved Ice", "title": "", "sheets_used": 0,
            })

    return sessions


# ── Discord bot ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
# Slash-command + button bot only — no text-prefix commands, so we don't need the
# privileged Message Content intent. Using when_mentioned (rather than a literal
# "!" prefix) tells discord.py that, which silences the "message content intent is
# missing" warning without enabling a privileged intent we'd never use.
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


async def setup_hook():
    # Register persistent per-request buttons so they keep working across
    # restarts, then load the subs cog (adds the /subs command + board logic).
    bot.add_dynamic_items(
        subs.NewRequestButton,
        subs.AvailableButton,
        subs.FillForButton,
        subs.RemoveButton,
        subs.PageClaimButton,
        JoinPracticeButton,
    )
    await bot.add_cog(subs.Subs(bot))

    # Instructor board (adds /instructors + the scheduled channel post). Entirely
    # optional: with no channel or sheet id configured the cog never loads and
    # nothing else changes.
    if instructors.configured():
        await bot.add_cog(instructors.Instructors(bot))
    else:
        log.info("Instructor board not configured (INSTRUCTOR_CHANNEL_ID / SHEET_ID); "
                 "skipping that cog")

    # Sync slash commands once, here (on_ready can fire repeatedly on reconnect).
    if DEV_GUILD_ID:
        guild = discord.Object(id=DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)        # stage every command onto the dev guild
        synced = await bot.tree.sync(guild=guild)   # replaces the guild's set — instant
        # Flush the GLOBAL set so commands don't also show as a second (global)
        # copy in the dev guild. copy_global_to already captured them above.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print(f"🔧  Dev mode: synced {len(synced)} command(s) to guild {DEV_GUILD_ID} "
              f"(instant) and cleared global commands.")
    else:
        synced = await bot.tree.sync()              # global — up to ~1h to appear
        print(f"🌐  Synced {len(synced)} command(s) globally (allow up to ~1h to appear).")

bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    cog = bot.get_cog("Subs")
    if cog is not None:
        await cog.startup()  # prune expired requests and refresh each server's board
    await restore_practice_board()  # re-render the practice board after a (re)boot
    print(f"✅  Logged in as {bot.user}.")


# ── Practice ice: live report + open sign-up pool ───────────────────────────

MAX_SIGNUP_BUTTONS = 20  # leave headroom under Discord's 25-component cap

def session_key(opp: dict) -> str:
    """Stable per-slot key from the session start minute (e.g. '20260616T1945')."""
    return opp["start"].strftime("%Y%m%dT%H%M")


class SheetsError(Exception):
    """Carries a user-facing message for a failed /sheets fetch."""


async def fetch_opps(weeks: int) -> list[dict]:
    """Fetch the practice-ice opportunities (raises SheetsError with a message)."""
    try:
        practices = await fetch_practices_within(weeks)
    except Exception:  # noqa: BLE001 — log detail server-side, show a safe message
        log.exception("Calendar fetch failed")
        raise SheetsError("❌  Could not reach the event calendar right now — please try again shortly.")
    if not practices:
        return []
    # Drop the "already over" floor back by a grace period so a session that just
    # ended still shows briefly (latecomers can see/join it) — but yesterday's is
    # long gone. The floor gates every source; the window still extends `weeks`
    # ahead of *now*.
    now = now_club()
    window_start = now - timedelta(hours=SHEETS_GRACE_HOURS)
    window_end   = now + timedelta(weeks=weeks)
    try:
        async with GFClient() as gf:
            sessions = await collect_sessions(practices, window_start, window_end, gf)
    except Exception:  # noqa: BLE001 — never surface raw errors (they can carry the GF URL+creds)
        log.exception("Registration data fetch failed")
        raise SheetsError("❌  Couldn't load registration data right now — please try again shortly.")
    return pi.practice_opportunities(sessions, TOTAL_SHEETS)


# Cache the fetched opportunities per `weeks` window so /sheets (and every sign-up
# button re-render) doesn't hit the backend each time. Refetch only when the entry
# is older than SHEETS_CACHE_TTL (or on first use after launch). On a refetch
# failure, serve the stale entry rather than erroring.
_opps_cache: dict[int, tuple[float, list[dict]]] = {}
_opps_cache_lock = asyncio.Lock()


def _fresh_opps(opps: list[dict]) -> list[dict]:
    """Drop opportunities that have already ended, so a long-lived cache never shows a
    clearly-past slot; everything still upcoming (within its window) is kept as-is."""
    cutoff = now_club() - timedelta(hours=SHEETS_GRACE_HOURS)
    return [o for o in opps if o.get("end") is None or o["end"] > cutoff]


async def fetch_opps_cached(weeks: int) -> list[dict]:
    now_m = time.monotonic()
    entry = _opps_cache.get(weeks)
    if entry is None or now_m - entry[0] > SHEETS_CACHE_TTL:
        async with _opps_cache_lock:
            entry = _opps_cache.get(weeks)  # re-check: another task may have refetched
            if entry is None or time.monotonic() - entry[0] > SHEETS_CACHE_TTL:
                try:
                    opps = await fetch_opps(weeks)
                except SheetsError:
                    if entry is not None:
                        log.warning("Sheets refetch failed — serving cached opportunities.")
                        return _fresh_opps(entry[1])
                    raise
                entry = (time.monotonic(), opps)
                _opps_cache[weeks] = entry
    return _fresh_opps(entry[1])


async def build_sheets_payload(weeks: int, user) -> tuple[discord.Embed, discord.ui.View | None]:
    """Build the (ephemeral) practice-ice embed + sign-up view for a member."""
    opps = await fetch_opps_cached(weeks)

    # Register each session and snapshot sign-up counts under the lock. Sessions are
    # keyed by start minute, so two opportunities at the same time share ONE sign-up
    # pool — collapse them to a single row + button (a second button would reuse the
    # custom_id, which Discord rejects with 50035 "custom id cannot be duplicated").
    async with _practice_lock:
        ps.expire(_practice_state, now_club())
        keyed, seen_keys = [], set()
        for o in opps:
            key = session_key(o)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            label = f"{o['start'].strftime('%a %b %-d')} · {o['start'].strftime('%-I:%M %p')}"
            ps.register_session(_practice_state, key, when_ts=o["start"].isoformat(),
                                label=label, sheets=o.get("free"))
            keyed.append((o, key, ps.count(_practice_state, key),
                          ps.is_signed_up(_practice_state, key, user.id)))
            if len(keyed) >= MAX_SIGNUP_BUTTONS:
                break
        ps.save(PRACTICE_STORE_PATH, _practice_state)

    embed = discord.Embed(title=f"🥌  Practice Ice — {CLUB_NAME}", color=0x1a6bb5)
    if not opps:
        embed.description = "🔴  No sheets free during the upcoming practice window."
        embed.set_footer(text="Only you can see this · source: calendar + Gravity Forms + leagues")
        return embed, None

    lines, view = [], discord.ui.View(timeout=None)
    for o, key, n, mine in keyed:
        line = pi.format_opportunity(o, TOTAL_SHEETS)[1]
        line += f"\n    🧹 **{n}** signed up to practice" + (" · you're in" if mine else "")
        # No free ice → nothing to practice on, so don't let people sign up. Someone
        # already signed up keeps a button (to withdraw); everyone else gets none.
        full = o.get("free", 0) <= 0
        if full and not mine:
            line += "\n    ⛔ no open ice — sign-up closed for this slot"
        lines.append(line)
        if mine or not full:
            view.add_item(JoinPracticeButton(key, weeks, o["start"], n, mine))
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Tap a slot to sign up · only you can see this report")
    return embed, (view if view.children else None)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _streak_rows(rows: list[dict], key: str, n: int = 5) -> str:
    """Format a leaderboard as a high-score list: 🥇/🥈/🥉 then numbered.

    Placing is by STREAK LENGTH, not by position in the list — everyone on the same
    number of weeks shares a medal (three people tied on the club record all get 🥇).
    Competition ranking, so the group after a 3-way tie for 1st is 4th — the same
    rule ps.streak_rank uses for the "3rd longest in the club" line on a sign-up.
    The cut-off never splits a tied group: if the 5th and 6th names are level, both
    show — up to HARD_CAP, since early in a season the whole club can be tied on one
    week and an embed field dies past 1024 characters."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    HARD_CAP = 12
    if not rows:
        return "—"
    shown = list(rows[:n])
    cutoff = shown[-1][key]
    tied_rest = [r for r in rows[n:] if r[key] == cutoff]
    shown += tied_rest[: HARD_CAP - len(shown)]
    out = []
    for r in shown:
        w = r[key]
        rank = 1 + sum(1 for x in rows if x[key] > w)   # ties share a rank
        out.append(f"{medals.get(rank, f'`{rank}.`')}  **{r['name']}** — {w} wk{'s' if w != 1 else ''}")
    hidden = len(tied_rest) - (len(shown) - min(len(rows), n))
    if hidden > 0:
        out.append(f"…and {hidden} more tied at {cutoff} wk{'s' if cutoff != 1 else ''}")
    return "\n".join(out)


def build_streak_leaderboard_embed() -> discord.Embed:
    """The /sheets stats:True screen — arcade high-score board: who's hot right now,
    plus the all-time records that stand forever."""
    now = now_club()
    current = ps.streak_leaderboard(_practice_state, now)
    all_time = ps.all_time_leaderboard(_practice_state)
    e = discord.Embed(title=f"🏆  Practice Streak Records — {CLUB_NAME}", color=0xE0632D)
    if not current and not all_time:
        e.description = ("No streaks on the board yet — sign up two weeks running (and show up!) "
                         "to get your name up here. Run **/sheets** to find open ice.")
        return e
    e.add_field(name="🔥  Current streaks", value=_streak_rows(current, "streak"), inline=True)
    e.add_field(name="🏆  All-time records", value=_streak_rows(all_time, "best"), inline=True)
    e.set_footer(text="Consecutive weeks practiced · current resets if you miss a week · records stand forever")
    return e


def build_practice_board_embed() -> discord.Embed:
    """Public, always-current practice sign-up board (built from the store only)."""
    e = discord.Embed(title=f"🧹  Practice Sign-ups — {CLUB_NAME}", color=0x1a6bb5)
    sessions = [s for s in ps.active_sessions(_practice_state) if s.get("users")]
    if not sessions:
        e.description = "No one's signed up to practice yet.\n\nRun **/sheets** to see open ice and join a slot."
    else:
        lines = []
        for s in sessions:
            sheets = s.get("sheets")
            free = f"{sheets} sheet{'s' if sheets != 1 else ''} free · " if sheets is not None else ""
            names = ", ".join(u["name"] for u in s["users"])
            lines.append(f"🧹  **{s.get('label', s['key'])}** · {free}{len(s['users'])} in\n    {names}")
        e.description = "\n\n".join(lines)
    # Current top-5 streaks on the public board (all-time records via /sheets stats:True).
    lb = ps.streak_leaderboard(_practice_state, now_club())
    if lb:
        e.add_field(name="🔥  Current streaks (top 5)", value=_streak_rows(lb, "streak"), inline=False)
    e.set_footer(text="/sheets to join · /sheets stats:True for all-time records")
    return e


async def _post_practice_board(channel):
    """Post a fresh practice board in `channel`, repoint state at it, and delete the
    previous board message. Never pinned — visibility comes from being the newest
    message in the channel (so the bot needs no Manage Messages permission)."""
    async with _practice_lock:
        embed = build_practice_board_embed()
        old = _practice_state.get("board")
    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException:
        return
    async with _practice_lock:
        _practice_state["board"] = {"channel_id": channel.id, "message_id": msg.id}
        ps.save(PRACTICE_STORE_PATH, _practice_state)
    if old:
        try:
            ch = bot.get_channel(old["channel_id"]) or await bot.fetch_channel(old["channel_id"])
            if ch is not None:
                await ch.get_partial_message(old["message_id"]).delete()
        except discord.HTTPException:
            pass


async def bump_practice_board(channel):
    """Publish the updated board by reposting it to the BOTTOM of `channel`, so the
    change is visible to everyone in the chat flow. Used on every sign-up / cancel."""
    if channel is None:
        return
    async with _practice_lock:
        ps.expire(_practice_state, now_club())
        ps.save(PRACTICE_STORE_PATH, _practice_state)
    await _post_practice_board(channel)


async def render_practice_board(target_channel=None):
    """Edit the existing board in place (no repost); if it's gone, or none exists and
    a `target_channel` is given, (re)post one. Used on boot. Sign-ups use
    bump_practice_board so the update surfaces at the bottom of the channel."""
    async with _practice_lock:
        ps.expire(_practice_state, now_club())
        embed = build_practice_board_embed()
        board = _practice_state.get("board")
        ps.save(PRACTICE_STORE_PATH, _practice_state)
    if board:
        try:
            ch = bot.get_channel(board["channel_id"]) or await bot.fetch_channel(board["channel_id"])
            # get_partial_message() makes no API call and editing our own message
            # doesn't need Read Message History; NotFound only if it was deleted.
            await ch.get_partial_message(board["message_id"]).edit(embed=embed)
            return
        except discord.NotFound:
            pass  # deleted while we were down — repost below if we have a target
        except discord.Forbidden as e:
            if e.code == 50005:  # authored by another bot identity — drop stale pointer
                async with _practice_lock:
                    _practice_state["board"] = None
                    ps.save(PRACTICE_STORE_PATH, _practice_state)
            else:
                log.warning("Couldn't edit practice board %s: %s", board["message_id"], e)
                return
        except discord.HTTPException as e:
            log.warning("Couldn't edit practice board %s: %s", board["message_id"], e)
            return
    if target_channel is not None:
        await _post_practice_board(target_channel)


async def restore_practice_board():
    """On (re)boot, re-establish the practice board from its stored pointer: refresh
    its contents in place, reposting it in the same channel if the message was
    deleted while we were down. No-op if no board has been created yet (it's created
    on the first /sheets sign-up). Mirrors the subs cog's startup()."""
    board = _practice_state.get("board")
    if not board:
        return
    try:
        ch = bot.get_channel(board["channel_id"]) or await bot.fetch_channel(board["channel_id"])
    except discord.HTTPException as e:
        log.warning("Couldn't resolve practice board channel on boot: %s", e)
        return
    if ch is not None:
        # Passing the stored channel means render reposts there if the message is
        # gone, rather than silently giving up (it has no target otherwise).
        await render_practice_board(ch)


class JoinPracticeButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"sheet:join:(?P<key>\d{8}T\d{4}):(?P<weeks>\d+)"):
    def __init__(self, key: str, weeks: int, start: datetime | None = None,
                 count: int = 0, mine: bool = False):
        self.key = key
        self.weeks = int(weeks)
        # No type prefix: the report line directly above each button already names
        # the slot type and free sheets, and the button colour signals state. So the
        # button just shows the time (+ current sign-up count), e.g. "6/20 1:30PM (0)".
        # "Leave" stays on the joined state since that affordance isn't obvious from
        # colour alone. Fall back to parsing the key when reconstructed from a
        # custom_id (start isn't encoded; the live label is re-rendered on build).
        when_dt = start
        if when_dt is None:
            try:
                when_dt = datetime.strptime(key, "%Y%m%dT%H%M")
            except ValueError:
                when_dt = None
        when = when_dt.strftime('%-m/%-d %-I:%M%p') if when_dt else key
        label = f"Leave {when} ({count})" if mine else f"{when} ({count})"
        super().__init__(discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.secondary if mine else discord.ButtonStyle.success,
            custom_id=f"sheet:join:{key}:{weeks}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["key"], int(match["weeks"]))

    async def callback(self, interaction: discord.Interaction):
        # Instant "busy" feedback: grey out the report's buttons the moment they tap,
        # so the click visibly registers and an impatient double-tap can't slip a
        # second toggle in before we re-render. Live buttons return with the refresh
        # below. (The time-based debounce stays on as a backstop.)
        try:
            busy = discord.ui.View.from_message(interaction.message)
            for child in busy.children:
                child.disabled = True
            await interaction.response.edit_message(view=busy)
        except (discord.HTTPException, AttributeError):
            try:
                await interaction.response.defer()  # fall back to a plain ack
            except discord.HTTPException:
                pass
        when_ts = ""
        try:
            when_ts = datetime.strptime(self.key, "%Y%m%dT%H%M").isoformat()
        except ValueError:
            pass
        async with _practice_lock:
            # Debounce a rapid re-click of the same session: skip the toggle so an
            # impatient double-tap can't join-then-leave. Checked under the lock so
            # two near-simultaneous clicks can't both pass.
            repeat = _is_repeat_click(_practice_cooldown, (interaction.user.id, self.key))
            if not repeat:
                result = ps.toggle(_practice_state, self.key, interaction.user.id,
                                   interaction.user.display_name, when_ts=when_ts, now=now_club())
                label = _practice_state["sessions"].get(self.key, {}).get("label", self.key)
                n = ps.count(_practice_state, self.key)
                ps.save(PRACTICE_STORE_PATH, _practice_state)

        # Refresh the (private) report in place so counts stay current. Don't let a
        # hiccup here (fetch error, or the interaction message being gone) block the
        # shared-board update below — the board is what everyone else is watching.
        try:
            embed, view = await build_sheets_payload(self.weeks, interaction.user)
            await interaction.edit_original_response(embed=embed, view=view)
        except (SheetsError, discord.HTTPException):
            log.warning("Couldn't refresh the private /sheets report after a toggle.")

        if repeat:
            return  # nothing changed — don't re-publish or re-notify

        # Publish the updated board (reposted to the bottom of this channel)…
        await bump_practice_board(interaction.channel)
        # …and, on a new sign-up, post a one-line note — with their practice streak
        # and club ranking if it's a real streak (> 1 week).
        if result == "joined" and interaction.channel is not None:
            note = f"🧹  **{interaction.user.display_name}** is in for **{label}** — {n} signed up."
            streak = ps.current_streak(_practice_state, interaction.user.id, now_club())
            if streak > 1:
                rank, _total, tied = ps.streak_rank(_practice_state, interaction.user.id, now_club())
                place = f"{'tied for ' if tied else ''}{_ordinal(rank)} longest in the club"
                note += f" That's a **{streak}-week** practice streak — {place}! 🔥"
            try:
                await interaction.channel.send(note)
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            (f"🧹  You're signed up for **{label}** — the channel board's updated." if result == "joined"
             else f"👍  Removed you from practice on **{label}**."),
            ephemeral=True)


# One bare command with optional flags (so bare `/sheets` still works): default is
# the private free-ice report (`weeks` ahead); `stats:True` posts the streak records
# to the channel; `show:True` posts the practice sign-up board to the channel.
@bot.tree.command(name="sheets", description="Free ice + sign-up (private) · stats:True records · show:True post board")
@app_commands.describe(
    weeks="How many weeks ahead to show (1–4, default 1)",
    stats="Post the practice-streak records (current + all-time) to the channel",
    show="Post the practice sign-up board in this channel for everyone")
async def sheets_cmd(interaction: discord.Interaction, weeks: int = 1,
                     stats: bool = False, show: bool = False):
    if stats:
        # Public arcade high-score board — visible to the whole channel.
        await interaction.response.send_message(embed=build_streak_leaderboard_embed())
        return
    if show:
        if interaction.guild_id is None:
            await interaction.response.send_message("Use `show:True` in a server channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await bump_practice_board(interaction.channel)
        await interaction.followup.send("🧹  Posted the practice sign-up board here.", ephemeral=True)
        return
    weeks = max(1, min(weeks, 4))
    await interaction.response.defer(ephemeral=True)
    try:
        embed, view = await build_sheets_payload(weeks, interaction.user)
    except SheetsError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


bot.run(DISCORD_TOKEN)
