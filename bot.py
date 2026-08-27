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
import block_store as bs
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
# slowly, so cache the fetched sessions and reuse them for SHEETS_CACHE_TTL —
# same 6h cadence as the league cache. In-memory, so it also refetches once per app
# launch. See fetch_sessions_cached / current_opportunities.
SHEETS_CACHE_TTL = int(os.environ.get("SHEETS_CACHE_TTL", "21600"))  # 6h
# How soon to retry after a failed refetch (we keep serving the stale data until
# then). Short enough to recover quickly, long enough that a down backend isn't
# re-dialled once per button press.
SHEETS_RETRY_SECONDS = int(os.environ.get("SHEETS_RETRY_SECONDS", "120"))

# Practice sign-up pool (shared across members; interactions stay private).
PRACTICE_STORE_PATH = os.environ.get("PRACTICE_STORE_PATH", "practice_signups.json")
_practice_state = ps.load(PRACTICE_STORE_PATH)
_practice_lock = asyncio.Lock()

# Ad-hoc sheet blocks: ice reserved off the books, held by hand from /sheets so
# the report stops advertising sheets that aren't actually there. Kept in its own
# store (and its own lock) because it's written from the block flow while a
# sign-up may be writing the practice pool. See block_store.
BLOCK_STORE_PATH = os.environ.get("BLOCK_STORE_PATH", "sheet_blocks.json")
BLOCK_GRACE_HOURS = float(os.environ.get("BLOCK_GRACE_HOURS", "1"))
_block_state = bs.load(BLOCK_STORE_PATH)
_block_lock = asyncio.Lock()

# ── Where state actually lives ────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))


# Secrets the bot cannot work without. DISCORD_TOKEN is read at import and fails
# immediately, but the Gravity Forms pair is only touched when GFClient() is
# constructed — deep inside the first /sheets call. That meant a missing key
# booted a perfectly healthy-looking bot that then threw KeyError at the first
# member who used the command.
REQUIRED_SECRETS = ("DISCORD_TOKEN", "GF_CONSUMER_KEY", "GF_CONSUMER_SECRET")


def _check_required_env() -> None:
    """Fail at boot, loudly and by name, if a required secret is missing.

    Learned the hard way (2026-08-20): prod never declared the Gravity Forms
    credentials. It worked anyway because `COPY . .` had baked the developer's
    .env into the image and load_dotenv() read it from /app/.env — so production
    was quietly running on dev's keys. Adding a .dockerignore removed that file
    and the real gap surfaced as a KeyError mid-command. A missing secret should
    stop the bot at startup where the operator sees it, not ambush a member."""
    missing = [k for k in REQUIRED_SECRETS if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "FATAL: missing required environment variable(s): " + ", ".join(missing)
            + ".\nThese are secrets: put them in the env file the deployment ships"
              " (.env.prod -> .env on the box), NOT in docker-compose. Note that a"
              " .env inside the image is no longer read — .dockerignore excludes it"
              " on purpose.")


def _audit_store_paths() -> None:
    """Log where every persistent store resolved to, and shout if one landed inside
    the app directory.

    A store written next to the code is a store written INSIDE THE DOCKER IMAGE.
    It looks like it works — the bot reads and writes it all session — but the
    container is replaced on every deploy, so the file reverts to whatever was in
    the build context on the developer's laptop. That is exactly how a sheet block
    placed in dev turned up in production while production's own block vanished
    (2026-08-20): BLOCK_STORE_PATH wasn't set in the compose files, so it fell back
    to the bare default in the working directory instead of the /data volume.

    Silent data loss found by a human noticing wrong numbers is the worst way to
    find this, so it goes in the boot log where it can't hide."""
    stores = (("BLOCK_STORE_PATH", BLOCK_STORE_PATH),
              ("PRACTICE_STORE_PATH", PRACTICE_STORE_PATH),
              ("SUBS_STORE_PATH", subs.STORE_PATH))
    for label, path in stores:
        resolved = os.path.abspath(path)
        try:
            inside_app = os.path.commonpath([resolved, APP_DIR]) == APP_DIR
        except ValueError:            # different drives — can't be inside
            inside_app = False
        if inside_app:
            log.warning(
                "%s resolves to %s, inside the app directory — this file is part of "
                "the image and WILL BE RESET on the next deploy. Point it at the "
                "durable volume (e.g. /data/%s).",
                label, resolved, os.path.basename(resolved))
        else:
            log.info("%s -> %s%s", label, resolved,
                     "" if os.path.exists(resolved) else " (new)")

# Debounce impatient double-taps of a practice signup button. ps.toggle is a
# join↔leave toggle, so without this a quick double-click would join then
# instantly leave. (user_id, session_key) -> monotonic time of last click.
CLICK_DEBOUNCE_SECONDS = 3.0
_practice_cooldown: dict[tuple[int, str], float] = {}
# Same guard for placing a sheet block — see finish_block.
_block_cooldown: dict[tuple, float] = {}


def _is_repeat_click(cooldown: dict, key) -> bool:
    """Record this click and report whether it repeats `key` within the debounce
    window; callers treat a repeat as a no-op. Call under whichever lock guards
    the passed dict — _practice_lock for _practice_cooldown, _block_lock for
    _block_cooldown."""
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
    # First thing in the boot log: prove the config is complete, and say where the
    # durable state actually lives. Called here rather than at import time because
    # discord.py installs the log handlers inside run(), so anything logged
    # earlier goes nowhere.
    _check_required_env()
    _audit_store_paths()

    # Register persistent per-request buttons so they keep working across
    # restarts, then load the subs cog (adds the /subs command + board logic).
    bot.add_dynamic_items(
        subs.NewRequestButton,
        subs.AvailableButton,
        subs.FillForButton,
        subs.RemoveButton,
        subs.ShowAllButton,
        subs.PageClaimButton,
        subs.SeriesClaimButton,
        subs.RunPickButton,
        # Buttons that live in a DM: the assignment handshake and the "your league has
        # teams now" nudge. These outlive the message they were sent on, so they must
        # be registered like any board button or they go dead on the next restart.
        subs.ConfirmAutoButton,
        subs.DropAutoButton,
        subs.SetTeamButton,
        JoinPracticeButton,
        BlockSheetsButton,
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
# Discord's embed-description limit is 4096; stop short so the trailing
# "…and N more" line always fits.
MAX_EMBED_DESCRIPTION = 3900

def session_key(opp: dict) -> str:
    """Stable per-slot key from the session start minute (e.g. '20260616T1945')."""
    return opp["start"].strftime("%Y%m%dT%H%M")


class SheetsError(Exception):
    """Carries a user-facing message for a failed /sheets fetch."""


async def fetch_sessions(weeks: int) -> list[dict]:
    """Fetch the raw ice sessions in the window (raises SheetsError with a message).

    Stops short of practice_opportunities on purpose: what's cached below has to be
    only the slow, network-derived half. Blocks live in a local file and change the
    instant someone places one, so they're folded in per-render instead — caching
    finished opportunities would have meant a block quietly not applying until the
    6-hour TTL rolled over."""
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
    return sessions


# Cache the fetched sessions per `weeks` window so /sheets (and every sign-up
# button re-render) doesn't hit the backend each time. Refetch only when the entry
# is older than SHEETS_CACHE_TTL (or on first use after launch). On a refetch
# failure, serve the stale entry rather than erroring.
_sessions_cache: dict[int, tuple[float, list[dict]]] = {}
_sessions_cache_lock = asyncio.Lock()


def _fresh_sessions(sessions: list[dict]) -> list[dict]:
    """Drop sessions that have already ended, so a long-lived cache never shows a
    clearly-past slot; everything still upcoming (within its window) is kept as-is."""
    cutoff = now_club() - timedelta(hours=SHEETS_GRACE_HOURS)
    return [s for s in sessions if s.get("end") is None or s["end"] > cutoff]


async def fetch_sessions_cached(weeks: int) -> list[dict]:
    now_m = time.monotonic()
    entry = _sessions_cache.get(weeks)
    if entry is None or now_m - entry[0] > SHEETS_CACHE_TTL:
        async with _sessions_cache_lock:
            entry = _sessions_cache.get(weeks)  # re-check: another task may have refetched
            if entry is None or time.monotonic() - entry[0] > SHEETS_CACHE_TTL:
                try:
                    sessions = await fetch_sessions(weeks)
                except SheetsError:
                    if entry is not None:
                        # Re-stamp the stale entry to a short retry instead of
                        # leaving it expired: otherwise every /sheets and every 🚫
                        # press re-dials a dead host while holding this lock, and
                        # the whole club queues behind one HTTP timeout.
                        log.warning("Sheets refetch failed — serving cached sessions.")
                        _sessions_cache[weeks] = (
                            time.monotonic() - SHEETS_CACHE_TTL + SHEETS_RETRY_SECONDS,
                            entry[1])
                        return _fresh_sessions(entry[1])
                    raise
                entry = (time.monotonic(), sessions)
                _sessions_cache[weeks] = entry
    return _fresh_sessions(entry[1])


async def current_opportunities(weeks: int) -> list[dict]:
    """The practice-ice rows as they stand right now: cached sessions from the
    calendar/forms/leagues, plus every live manual block, run through the same
    overlap arithmetic. Blocks are applied here (not inside the cache) so placing
    one shows up on the very next render."""
    return [o for o in await current_sessions_annotated(weeks) if pi.should_show(o)]


async def current_sessions_annotated(weeks: int) -> list[dict]:
    """Every session in the window with its free-sheet count attached, INCLUDING
    the ones the display rules hide. current_opportunities is the subset members
    see; this is the subset the bot has to reason about."""
    sessions = await fetch_sessions_cached(weeks)
    async with _block_lock:
        if bs.expire(_block_state, now_club(), BLOCK_GRACE_HOURS):
            bs.save(BLOCK_STORE_PATH, _block_state)
        blocks = bs.as_sessions(_block_state)
    return pi.annotate(sessions + blocks, TOTAL_SHEETS)


async def current_sessions_annotated_safe(weeks: int) -> list[dict]:
    try:
        return await current_sessions_annotated(weeks)
    except SheetsError:
        log.warning("Couldn't recompute sessions after a block change.")
        return []


async def current_opportunities_safe(weeks: int) -> list[dict]:
    """current_opportunities, but a fetch failure yields [] instead of raising —
    for the paths (block announcements, board refreshes) where a hiccup upstream
    must not take down the action the member just performed."""
    try:
        return await current_opportunities(weeks)
    except SheetsError:
        log.warning("Couldn't recompute opportunities after a block change.")
        return []


async def build_sheets_payload(weeks: int, user) -> tuple[discord.Embed, discord.ui.View | None]:
    """Build the (ephemeral) practice-ice embed + sign-up view for a member."""
    opps = await current_opportunities(weeks)

    # Register each session and snapshot sign-up counts under the lock. Sessions are
    # keyed by start minute, so two opportunities at the same time share ONE sign-up
    # pool — collapse them to a single row + button (a second button would reuse the
    # custom_id, which Discord rejects with 50035 "custom id cannot be duplicated").
    async with _practice_lock:
        ps.expire(_practice_state, now_club())
        keyed, seen_keys, capped = [], set(), 0
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
                capped = len({session_key(x) for x in opps}) - len(keyed)
                break
        ps.save(PRACTICE_STORE_PATH, _practice_state)

    embed = discord.Embed(title=f"🥌  Practice Ice — {CLUB_NAME}", color=0x1a6bb5)
    if not opps:
        embed.description = "🔴  No sheets free during the upcoming practice window."
        embed.set_footer(text="Only you can see this · source: calendar + Gravity Forms + leagues")
        # Still offer the block menu: with nothing listed, the manual-entry path is
        # the only way to record ice that got reserved off the calendar — and the
        # release menu is how a block placed in error gets undone.
        empty = discord.ui.View(timeout=None)
        empty.add_item(BlockSheetsButton(weeks))
        return embed, empty

    lines, view = [], discord.ui.View(timeout=None)
    used, dropped = 0, 0
    for o, key, n, mine in keyed:
        line = pi.format_opportunity(o, TOTAL_SHEETS)[1]
        line += f"\n    **{n}** signed up to practice" + (" · you're in" if mine else "")
        # No free ice → nothing to practice on, so don't let people sign up. Someone
        # already signed up keeps a button (to withdraw); everyone else gets none.
        full = o.get("free", 0) <= 0
        if full and not mine:
            line += "\n    ⛔ no open ice — sign-up closed for this slot"
        # Discord hard-rejects an embed description over 4096 characters, and
        # discord.py doesn't check — it just 400s, leaving the member on a
        # permanent "thinking…" with the 🚫 menu (the only way to UNDO a block)
        # stuck behind the failed render. Blocks add lines per row, so the
        # headroom that used to be comfortable no longer is. Drop whole slots
        # rather than risk it, and say so.
        if used + len(line) + 2 > MAX_EMBED_DESCRIPTION:
            dropped += 1
            continue
        used += len(line) + 2
        lines.append(line)
        if mine or not full:
            view.add_item(JoinPracticeButton(key, weeks, o["start"], n, mine))
    # Slots left out for either reason — the component cap or the description
    # budget — are reported together; two separate tallies would each under-count.
    dropped += capped
    if dropped:
        lines.append(f"…and {dropped} more slot{'s' if dropped != 1 else ''} — "
                     "run `/sheets` with a smaller `weeks` to see them.")
    embed.description = "\n\n".join(lines)
    # Sits last, after the slot buttons (20 of those at most, so this stays inside
    # Discord's 25-component cap).
    view.add_item(BlockSheetsButton(weeks))
    embed.set_footer(text="Tap a slot to sign up · 🚫 to hold ice that's booked off the calendar")
    return embed, view


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Embed fields die past 1024 characters, and a leaderboard line is unbounded (it
# carries every name in a tied group). Leave room for the "…and N others" tail.
STREAK_FIELD_BUDGET = 900
# Names on one line before the rest are summarised, so one huge tie can't push the
# groups below it off the board.
MAX_NAMES_PER_GROUP = 10


def streak_groups(rows: list[dict], key: str) -> list[tuple]:
    """Collapse a sorted leaderboard into [(weeks, [names…]), …], longest first.

    One entry per distinct streak LENGTH, not per person — everyone level on three
    weeks is one group. `rows` arrives sorted by (-streak, name), so a group is just
    a run of equal values and the names inside it come out alphabetical."""
    groups: list[tuple] = []
    for r in rows:
        weeks = r[key]
        if groups and groups[-1][0] == weeks:
            groups[-1][1].append(r["name"])
        else:
            groups.append((weeks, [r["name"]]))
    return groups


def _streak_rows(rows: list[dict], key: str, n: int = 5) -> str:
    """Format a leaderboard as up to `n` lines — ONE PER STREAK LENGTH, with every
    name on that line.

    Grouping is the whole point. Listing one person per line repeated the same medal
    down the board (three people tied on the club record each got their own 🥇 row)
    and then jumped to a bare "4." for the next pair, which read as a numbering
    glitch rather than as fourth place. A line per group says the same thing without
    the repetition: place is the group's position, so the group after a three-way
    tie for first is second, and gets 🥈.

    That's dense ranking, and ps.streak_rank matches it — the "2nd longest in the
    club" line on a sign-up has to agree with the board the member is looking at."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    if not rows:
        return "—"
    groups = streak_groups(rows, key)
    out, used, shown_groups = [], 0, 0
    for place, (weeks, names) in enumerate(groups[:n], start=1):
        listed = names[:MAX_NAMES_PER_GROUP]
        rest = len(names) - len(listed)
        who = ", ".join(listed) + (f" +{rest} more" if rest else "")
        line = (f"{medals.get(place, f'`{place}.`')}  **{weeks} wk"
                f"{'s' if weeks != 1 else ''}** — {who}")
        if used + len(line) + 1 > STREAK_FIELD_BUDGET:
            break
        out.append(line)
        used += len(line) + 1
        shown_groups += 1
    # Never drop people silently: say how many are still on the board below the cut.
    hidden = sum(len(names) for _w, names in groups[shown_groups:])
    if hidden:
        out.append(f"…and {hidden} more with shorter streaks")
    return "\n".join(out) if out else "—"


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
            # No emoji per date. The broom lives in the board title only — repeated
            # down every row (and on every sign-up message) it was pure noise. Row
            # emoji are reserved for ones that carry meaning: the 🟢/🟡/🔴 free-sheet
            # status on /sheets, ⛔ for a closed slot, 🚫 for a manual block.
            lines.append(f"**{s.get('label', s['key'])}** · {free}{len(s['users'])} in\n    {names}")
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
    a `target_channel` is given, (re)post one. Used on boot, and by the block flow
    (which passes the board's OWN channel — see _board_channel — so a block never
    relocates it). Sign-ups use bump_practice_board so the update surfaces at the
    bottom of the channel."""
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
            note = f"**{interaction.user.display_name}** is in for **{label}** — {n} signed up."
            streak = ps.current_streak(_practice_state, interaction.user.id, now_club())
            if streak > 1:
                rank, _total, tied = ps.streak_rank(_practice_state, interaction.user.id, now_club())
                place = f"{'tied for ' if tied else ''}{_ordinal(rank)} longest in the club"
                note += f" That's a **{streak}-week** practice streak — {place}! 🔥"
            try:
                await interaction.channel.send(
                    note, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            (f"You're signed up for **{label}** — the channel board's updated." if result == "joined"
             else f"👍  Removed you from practice on **{label}**."),
            ephemeral=True)


# ── Blocking sheets ───────────────────────────────────────────────────────────
# Ice gets reserved off the books: a board member books a Learn-to-Curl by hand,
# a group reserves sheets at the rink, money changes hands and nothing lands on
# the club calendar or in Gravity Forms. /sheets keeps reporting that ice as free
# and members turn up to sheets that are already taken.
#
# A block is the manual patch: "hold N sheets during this window, and here's who
# did it and what for". Anyone in the server can place one — the club runs on
# trust and whoever's at the rink is usually the one who knows — so every block
# is announced in the channel with its owner's name, and any of them can be
# released from the same menu. Blocks age out on their own once the ice is past.

BLOCK_OTHER = "__other__"


def _releasable_blocks() -> list[dict]:
    """Blocks a member can still let go of. Deliberately matched to what
    current_opportunities still SUBTRACTS — which is anything inside
    BLOCK_GRACE_HOURS of ending, not merely anything still running. Using
    `now` here instead left a window where a just-ended block was still eating
    sheets on the report with nothing in the menu to release. Call under
    _block_lock."""
    return [b for b in bs.active(_block_state, now_club() - timedelta(hours=BLOCK_GRACE_HOURS))
            if b.get("id")]   # no id, no select value — see block_store.load


def _opt_text(text: str, limit: int = 100) -> str:
    """Discord rejects a select option whose label or description runs past 100
    characters, which would fail the whole message render."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slot_label(opp: dict) -> str:
    """'Sun Aug 23 · 1:30 PM · Practice' — the row as the block menu names it."""
    return (f"{opp['start'].strftime('%a %b %-d')} · "
            f"{opp['start'].strftime('%-I:%M %p')} · {opp.get('type', 'Ice')}")


class BlockSlotSelect(discord.ui.Select):
    def __init__(self, opps: list[dict], selected, row: int = 0):
        seen, opts = set(), []
        for o in opps:
            key = session_key(o)
            if key in seen:   # two rows can share a start minute; one option each
                continue
            seen.add(key)
            free = o.get("free", 0)
            opts.append(discord.SelectOption(
                label=_opt_text(slot_label(o)),
                value=key,
                description=_opt_text(f"{free} of {TOTAL_SHEETS} sheets free"
                                      + (f" · {o['title']}" if o.get("title") else "")),
                default=(key == selected),
            ))
            if len(opts) >= 24:   # leave room for the manual-entry option below
                break
        opts.append(discord.SelectOption(
            label="Other date/time…", value=BLOCK_OTHER, emoji="🗓",
            description="Ice that isn't listed above",
            default=(selected == BLOCK_OTHER),
        ))
        super().__init__(placeholder="Which ice is being taken?…", min_values=1,
                         max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        flow: "BlockFlowView" = self.view
        if self.values[0] == BLOCK_OTHER:
            # Straight to the modal — a slot we don't know about has no free count
            # to reason about, so there's nothing else to pick first.
            # The picker is deliberately left LIVE: View.stop() deregisters it, so
            # anyone who hits Cancel on the modal (common) would come back to a
            # menu whose every control answers "This interaction failed".
            await interaction.response.send_modal(ManualBlockModal(flow.weeks, flow))
            return
        flow.slot_key = self.values[0]
        await flow.refresh(interaction)


class BlockCountSelect(discord.ui.Select):
    def __init__(self, selected: int, opp: dict | None, row: int = 1):
        free = (opp or {}).get("free", TOTAL_SHEETS)
        opts = []
        for n in range(1, min(TOTAL_SHEETS, 25) + 1):   # 25 = Discord's option cap
            left = max(0, free - n)
            opts.append(discord.SelectOption(
                label=f"Block {n} sheet{'s' if n > 1 else ''}",
                value=str(n),
                description=(f"leaves {left} free" if n <= free
                             else "more than that slot has free"),
                default=(n == selected),
            ))
        super().__init__(placeholder="How many sheets…", min_values=1, max_values=1,
                         options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.sheets = int(self.values[0])
        await self.view.refresh(interaction)


class BlockConfirmButton(discord.ui.Button):
    def __init__(self, row: int = 2):
        super().__init__(label="Block it", emoji="🚫",
                         style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        flow: "BlockFlowView" = self.view
        opp = flow.slot()
        if opp is None:
            await interaction.response.send_message(
                "That slot isn't on the report any more — reopen 🚫 Block sheets.",
                ephemeral=True)
            return
        await interaction.response.send_modal(
            BlockReasonModal(flow.weeks, opp["start"], opp["end"], flow.sheets, flow))


class ReleaseBlockSelect(discord.ui.Select):
    def __init__(self, blocks: list[dict], row: int = 3):
        opts = [
            discord.SelectOption(
                label=_opt_text(bs.describe(b, with_who=False)),
                value=b["id"],
                description=_opt_text(f"blocked by {b.get('name') or 'someone'}"),
            )
            for b in blocks[:25] if b.get("id")
        ]
        super().__init__(placeholder="…or release a block", min_values=1,
                         max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        flow: "BlockFlowView" = self.view
        block_id = self.values[0]
        async with _block_lock:
            block = bs.release(_block_state, block_id)
            if block is not None:
                bs.save(BLOCK_STORE_PATH, _block_state)
            flow.blocks = _releasable_blocks()
        flow.status = ("That block was already gone." if block is None
                       else f"♻️  Released: {bs.describe(block)}")
        # Answer by editing the picker itself, so it stays live and its release
        # list is immediately correct. Announcing afterwards is fine — the
        # interaction is already acknowledged, and followups have 15 minutes.
        await interaction.response.edit_message(content=flow.prompt(), view=flow.build())
        if block is None:
            return
        who = (block.get("name") or "someone").strip()
        mine = block.get("user_id") == interaction.user.id
        whose = "their own block" if mine else f"**{who}**'s block"
        n = int(block.get("sheets", 0))
        note = (f"♻️  **{interaction.user.display_name}** released {whose} — "
                f"**{n} sheet{'s' if n != 1 else ''}** back on "
                f"**{bs.window_text(block)}**.")
        await _publish_block_change(interaction, flow.weeks, note)


class BlockFlowView(discord.ui.View):
    """Ephemeral picker: which slot → how many sheets → why. Plus a release menu
    when anything is currently blocked, so one 🚫 button covers both directions."""

    def __init__(self, weeks: int, opps: list[dict], blocks: list[dict]):
        # Long enough to open the modal, type a reason and submit. A modal itself
        # is untimed, so retire()/on_timeout have to cope with it outlasting this.
        super().__init__(timeout=300)
        self.weeks = weeks
        self.opps = opps
        self.blocks = blocks
        self.slot_key = None
        self.sheets = 1
        self.message = None
        self.status = ""
        self.build()

    def slot(self) -> dict | None:
        return next((o for o in self.opps if session_key(o) == self.slot_key), None)

    def build(self) -> "BlockFlowView":
        self.clear_items()
        self.add_item(BlockSlotSelect(self.opps, self.slot_key, row=0))
        if self.slot() is not None:
            self.add_item(BlockCountSelect(self.sheets, self.slot(), row=1))
            self.add_item(BlockConfirmButton(row=2))
        if self.blocks:
            self.add_item(ReleaseBlockSelect(self.blocks, row=3))
        return self

    def prompt(self) -> str:
        lines = ["🚫  **Block out sheets** — hold ice that's booked outside the calendar."]
        opp = self.slot()
        if opp is None:
            lines.append("Pick the session the ice comes out of, or **Other date/time…** "
                         "if it isn't listed.")
        else:
            free = opp.get("free", 0)
            left = max(0, free - self.sheets)
            lines.append(f"Slot: **{slot_label(opp)}** — {free} free now")
            lines.append(f"Blocking **{self.sheets}** → **{left}** sheet"
                         f"{'s' if left != 1 else ''} left for practice.")
            lines.append("Press **Block it** to say what it's for.")
        if self.blocks:
            lines.append("\nCurrently blocked:")
            lines += [f"· {bs.describe(b)}" for b in self.blocks[:5]]
        if self.status:
            lines.append(f"\n{self.status}")
        return "\n".join(lines)

    async def refresh(self, interaction: discord.Interaction):
        self.status = ""   # a one-shot note about the last action, not a fixture
        await interaction.response.edit_message(content=self.prompt(), view=self.build())

    async def retire(self, status: str) -> None:
        """Re-render the picker after a block landed: clear the chosen slot (so the
        confirm button goes away), refresh the release list, and say what happened.

        A modal has no timeout of its own, so a member can sit on it past this
        view's 300 seconds. By then on_timeout has greyed the controls out and the
        view is deregistered — rebuilding here would put fresh, ENABLED controls on
        a message Discord will no longer route, which is exactly the dead-but-
        live-looking menu the timeout handler exists to prevent."""
        if self.is_finished():
            return
        self.slot_key = None
        self.status = status
        async with _block_lock:
            self.blocks = _releasable_blocks()
        self.build()   # rebuild first: the view's own state must be right even if
        if self.message is None:   # we never got a message handle to push it to
            return
        try:
            await self.message.edit(content=self.prompt(), view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        """Grey the controls out rather than leaving a menu that looks alive and
        answers every click with "This interaction failed"."""
        if self.message is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content=self.prompt() +
                                    "\n\n_Timed out — press 🚫 Block sheets again._",
                                    view=self)
        except discord.HTTPException:
            pass


class BlockReasonModal(discord.ui.Modal, title="Block out sheets"):
    """Second half of the slot path: the when/how-many are already settled, so the
    only thing left to ask is what the ice is for."""

    reason = discord.ui.TextInput(
        label="What's the ice for?", required=False, max_length=100,
        placeholder="e.g. Learn-to-Curl (booked off the calendar)")

    def __init__(self, weeks: int, start: datetime, end: datetime, sheets: int,
                 flow: "BlockFlowView | None" = None):
        super().__init__()
        self.weeks, self.start, self.end, self.sheets = weeks, start, end, sheets
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await finish_block(interaction, weeks=self.weeks, start=self.start,
                           end=self.end, sheets=self.sheets, reason=str(self.reason),
                           flow=self.flow)


class ManualBlockModal(discord.ui.Modal, title="Block out sheets"):
    """The off-calendar path: ice that no /sheets row covers. Five fields is
    Discord's hard cap on a modal, which is exactly what this needs."""

    date = discord.ui.TextInput(label="Date", max_length=30, placeholder="Aug 23 · 8/23 · tomorrow")
    start = discord.ui.TextInput(label="Start time", max_length=20, placeholder="1:30 PM")
    length = discord.ui.TextInput(label="How long?", max_length=20, placeholder="2h · 2.5 · 90m")
    count = discord.ui.TextInput(label="How many sheets?", max_length=4, placeholder="2")
    reason = discord.ui.TextInput(label="What's the ice for?", required=False,
                                  max_length=100, placeholder="e.g. Learn-to-Curl")

    def __init__(self, weeks: int, flow: "BlockFlowView | None" = None):
        super().__init__()
        self.weeks = weeks
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            start, end, sheets = bs.parse_manual(
                str(self.date), str(self.start), str(self.length), str(self.count),
                now=now_club(), total=TOTAL_SHEETS)
        except ValueError as ex:
            # Self-authored message about their own typing — safe to echo.
            await interaction.edit_original_response(content=f"⚠️  {ex}")
            return
        await finish_block(interaction, weeks=self.weeks, start=start, end=end,
                           sheets=sheets, reason=str(self.reason), flow=self.flow)


async def finish_block(interaction: discord.Interaction, *, weeks: int, start: datetime,
                       end: datetime, sheets: int, reason: str,
                       flow: "BlockFlowView | None" = None) -> None:
    """Commit a block, tell the channel, and refresh what everyone else is looking at.
    Both entry paths (slot picker and manual entry) land here."""
    now = now_club()
    block, error, repeat = None, None, False
    # Placing a block isn't idempotent — two presses take two lots of ice. The
    # picker is retired below once a block lands, but a member can open a second
    # modal before the first submits, so debounce the identical block the way
    # sign-ups debounce a double-tap. Keyed on the whole window, not just the
    # start, so two same-start blocks of different lengths don't collide.
    key = (interaction.user.id, start.isoformat(), end.isoformat(), sheets)
    async with _block_lock:
        repeat = _is_repeat_click(_block_cooldown, key)
        if not repeat:
            bs.expire(_block_state, now, BLOCK_GRACE_HOURS)
            try:
                block = bs.add(_block_state, start=start, end=end, sheets=sheets,
                               user_id=interaction.user.id,
                               name=interaction.user.display_name,
                               reason=reason, now=now)
            except ValueError as ex:
                error = str(ex)
                # The cooldown is a guard against duplicate WORK, not against
                # retrying: a member who fixes a rejected block and resubmits
                # within three seconds must not be told it already exists.
                _block_cooldown.pop(key, None)
            # Saved either way: the expire() above mutated the store, and dropping
            # that because the new block was rejected would resurrect stale holds.
            bs.save(BLOCK_STORE_PATH, _block_state)

    # Every reply below is an HTTP round trip, so all of them happen OUTSIDE the
    # lock. Holding it across one would stall the next member's release — whose
    # callback answers without deferring and so has only three seconds to ack.
    if repeat:
        await interaction.edit_original_response(
            content="👍  Already blocked that — nothing else to do.")
        return
    if error is not None:
        await interaction.edit_original_response(content=f"⚠️  {error}")
        return

    reason_txt = (reason or "").strip()
    note = (f"🚫  **{interaction.user.display_name}** blocked "
            f"**{sheets} sheet{'s' if sheets != 1 else ''}** — "
            f"**{bs.fmt_window(start, end)}**"
            + (f" · {reason_txt}" if reason_txt else "") + ".")
    tail, mentions = await _block_impact(weeks, start, end, sheets)
    note += tail
    if mentions:
        note += ("\n" + " ".join(f"<@{uid}>" for uid in mentions)
                 + " — heads up, you're signed up to practice then.")
    await _publish_block_change(interaction, weeks, note)
    await interaction.edit_original_response(
        content=f"🚫  Blocked {sheets} sheet{'s' if sheets != 1 else ''} for "
                f"{bs.fmt_window(start, end)}. Everyone's /sheets report updates right "
                "away — reopen 🚫 Block sheets to release it.")
    # Retiring the picker is cosmetic — without it the "Block it" button sits
    # there live, showing pre-block numbers — so it goes AFTER the confirmation
    # the member is actually waiting on, and can't strand them if it fails.
    if flow is not None:
        await flow.retire(f"🚫  Blocked: {bs.describe(block, with_who=False)}")


async def _block_impact(weeks: int, start: datetime, end: datetime,
                        sheets: int) -> tuple[str, list[int]]:
    """What the block did to the sessions it landed on: a sentence about the ice
    left, plus the ids of anyone signed up to a session the block took to zero (the
    one case where someone's plans just changed and they deserve a ping).

    Measured over pi.annotate, NOT the displayed rows: a league draw that a block
    pushes to 0 free used to drop out of the display entirely, so the block looked
    like it had hit nothing and the people it stranded were never told."""
    rows = await current_sessions_annotated_safe(weeks)
    # Only sessions a block actually took ice from. Overlap alone isn't enough:
    # a league already booked solid is at 0 free with or without the block, and
    # counting it here made the bot announce "no free ice" (and ping that slot's
    # sign-ups) for a slot the member's block never touched.
    hit = [o for o in rows
           if pi.overlaps(start, end, o["start"], o["end"])
           and o.get("free", 0) < o.get("free_if_unblocked", 0)]
    if not hit:
        return "", []
    free = min(o.get("free", 0) for o in hit)
    # Ping only the people on sessions THIS block pushed to zero. Two filters:
    # a long block can span a full slot and a half-empty one (the half-empty
    # one's sign-ups don't need a "no free ice" alarm), and a slot someone else
    # had already blocked solid was at zero before this block arrived — its
    # sign-ups were told the first time and don't need telling again.
    zeroed = [o for o in hit
              if o.get("free", 0) <= 0 and o.get("free", 0) + sheets >= 1]
    mentions: list[int] = []
    if zeroed:
        async with _practice_lock:
            for o in zeroed:
                for u in ps.signups(_practice_state, session_key(o)):
                    if u["user_id"] not in mentions:
                        mentions.append(u["user_id"])
    if free > 0:
        return f" {free} sheet{'s' if free != 1 else ''} still free there.", mentions
    if not zeroed:
        return " That slot had no free ice left anyway.", mentions
    return " That leaves **no free ice** in that slot.", mentions


async def _publish_block_change(interaction: discord.Interaction, weeks: int, note: str) -> None:
    """Announce a block/release in the channel and resync the shared practice board.
    A block silently changing everyone's numbers is the failure mode to avoid — the
    whole point is that off-calendar ice becomes visible."""
    await _resync_practice_sheets(weeks)
    if interaction.channel is not None:
        try:
            # users=True and nothing else: the note deliberately @-mentions the
            # people a block stranded, but the reason field is member-typed free
            # text and must never be able to fire @everyone or a role ping.
            await interaction.channel.send(
                note, allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True))
        except discord.HTTPException:
            log.warning("Couldn't announce a block change in the channel.")
    # Edit the board where it already lives rather than bump_practice_board, which
    # REPOSTS it into whatever channel this interaction came from. Blocking is
    # pitched as "do it from wherever you are", so moving the club's board to
    # #general because someone blocked ice from their phone is the normal case,
    # not an edge one. Passing the board's OWN channel (not this interaction's)
    # lets it repost there if the message was deleted, instead of silently
    # no-op-ing on every block from then on.
    await render_practice_board(await _board_channel())


async def _board_channel():
    """The channel the practice board already lives in, or None if there's no board
    yet. Used so a board refresh can heal a deleted message without relocating it."""
    board = _practice_state.get("board")
    if not board:
        return None
    try:
        return bot.get_channel(board["channel_id"]) or await bot.fetch_channel(board["channel_id"])
    except (discord.HTTPException, KeyError, TypeError):
        return None


async def _resync_practice_sheets(weeks: int) -> None:
    """Refresh each pooled session's cached free-sheet count. The shared board reads
    those numbers straight from the store, so without this it would keep advertising
    sheets a block just took until someone happened to run /sheets."""
    rows = await current_sessions_annotated_safe(weeks)
    if not rows:
        return
    # Annotated sessions, not displayed rows: a slot the display rules now hide
    # (a league blocked down to 0) still has a sign-up pool whose cached count
    # would otherwise keep advertising ice that's gone.
    # Two sessions can share a start minute and therefore a sign-up pool.
    # build_sheets_payload registers the pool from the FIRST DISPLAYED row, so
    # resolve the collision the same way here — last-wins would let a hidden
    # 0-free row overwrite the shown row's real count on the public board.
    by_key: dict[str, tuple] = {}
    for o in rows:
        key = session_key(o)
        shown = pi.should_show(o)
        prev = by_key.get(key)
        if prev is None or (shown and not prev[1]):
            by_key[key] = (o.get("free"), shown)
    async with _practice_lock:
        for key, (free, _shown) in by_key.items():
            session = _practice_state["sessions"].get(key)
            if session is not None:
                session["sheets"] = free
        ps.save(PRACTICE_STORE_PATH, _practice_state)


class BlockSheetsButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"sheet:block:(?P<weeks>\d+)"):
    """The 🚫 entry point on every /sheets report. Persistent, so it keeps working
    on a report someone left open across a restart."""

    def __init__(self, weeks: int):
        self.weeks = int(weeks)
        super().__init__(discord.ui.Button(
            label="Block sheets", emoji="🚫",
            style=discord.ButtonStyle.secondary,
            custom_id=f"sheet:block:{int(weeks)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["weeks"]))

    async def callback(self, interaction: discord.Interaction):
        # New ephemeral message rather than editing the report in place — the report
        # is the thing they're reading while they decide what to block.
        await interaction.response.defer(thinking=True, ephemeral=True)
        opps = await current_opportunities_safe(self.weeks)
        async with _block_lock:
            blocks = _releasable_blocks()
        flow = BlockFlowView(self.weeks, opps, blocks)
        flow.message = await interaction.edit_original_response(
            content=flow.prompt(), view=flow)


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
        await interaction.followup.send("Posted the practice sign-up board here.", ephemeral=True)
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
