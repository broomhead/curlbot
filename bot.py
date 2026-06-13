"""
Curling Club Practice-Ice Bot
Slash command: /sheets [upcoming]

Reports practice ice from three sources, in time order: designated practice
blocks, Learn-to-Curls that don't fill the ice, and league draws that don't
fill the ice. Sheet counts come from Gravity Forms (LTC/private events) and the
public league pages (leagues, via league_client). No estimates — unconfirmed
sessions are flagged as such.

Configure the target site and club name via environment variables
(SITE_BASE_URL, CLUB_NAME) — see .env.example.
"""

import asyncio
import json
import math
import os
import re
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from gf_client import GFClient
from league_client import get_cached_leagues, draw_to_datetime
import practice_ice as pi

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://example.com")
CLUB_NAME     = os.environ.get("CLUB_NAME", "Curling Club")
WP_API        = f"{SITE_BASE_URL}/wp-json/tribe/events/v1"
TOTAL_SHEETS  = 4
TIMEZONE_OFFSET = -5  # America/Chicago (CST = UTC-5, CDT = UTC-6)

PRACTICE_SLUG = "practice"

PRIVATE_EVENT_FORM_ID = 116   # "Private Event Registration"
LTC_FORM_ID           = 110   # "Learn to Curl Registration"

PRICE_PER_PERSON = 55   # private event rate per person
PEOPLE_PER_SHEET = 8    # max people per sheet

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


async def fetch_upcoming_practices(count: int) -> list[dict]:
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{WP_API}/events", params={
            "categories": PRACTICE_SLUG,
            "start_date": now.strftime("%Y-%m-%d %H:%M:%S"),
            "per_page": count, "status": "publish",
        }) as r:
            return (await r.json()).get("events", [])


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
        return max(1, math.ceil(people / PEOPLE_PER_SHEET))

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
        return max(1, min(TOTAL_SHEETS, math.ceil(total_people / PEOPLE_PER_SHEET)))

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

    # 1. Designated practice blocks — occupy no sheets themselves.
    for p in practices:
        sessions.append({
            "start": datetime.fromisoformat(p["start_date"]),
            "end":   datetime.fromisoformat(p["end_date"]),
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
                "title": lg.get("title", "League"),
                "sheets_used": d.get("sheets_used"),
            })

    return sessions


# ── Discord bot ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅  Logged in as {bot.user} — slash commands synced.")


@bot.tree.command(name="sheets", description="Check free sheets during upcoming practice ice")
@app_commands.describe(upcoming="How many upcoming practices to show (1–5, default 1)")
async def sheets_cmd(interaction: discord.Interaction, upcoming: int = 1):
    upcoming = max(1, min(upcoming, 5))
    await interaction.response.defer()

    try:
        practices = await fetch_upcoming_practices(upcoming)
    except Exception as e:
        await interaction.followup.send(f"❌  Could not reach the event calendar: `{e}`")
        return

    if not practices:
        await interaction.followup.send("⚠️  No upcoming practice events found on the calendar.")
        return

    # The N practice blocks define the lookahead window; LTCs and league draws
    # between now and the last block surface as additional practice ice.
    window_start = now_club()
    window_end   = max(datetime.fromisoformat(p["end_date"]) for p in practices)

    try:
        async with GFClient() as gf:
            sessions = await collect_sessions(practices, window_start, window_end, gf)
    except Exception as ex:
        await interaction.followup.send(f"❌  Registration data error: `{ex}`")
        return

    opps = pi.practice_opportunities(sessions, TOTAL_SHEETS)

    embed = discord.Embed(title=f"🥌  Practice Ice — {CLUB_NAME}", color=0x1a6bb5)
    if not opps:
        embed.description = "🔴  No sheets free during the upcoming practice window."
    else:
        embed.description = "\n\n".join(pi.format_opportunity(o, TOTAL_SHEETS)[1] for o in opps)
    embed.set_footer(text="Source: event calendar + Gravity Forms + league pages")
    await interaction.followup.send(embed=embed)


bot.run(DISCORD_TOKEN)
