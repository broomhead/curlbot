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
from league_client import get_cached_leagues, draw_to_datetime
import practice_ice as pi
import practice_store as ps
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
# Sheet count varies by facility — configure via NUM_SHEETS (default 4).
TOTAL_SHEETS  = int(os.environ.get("NUM_SHEETS", "4"))
TIMEZONE_OFFSET = -5  # America/Chicago (CST = UTC-5, CDT = UTC-6)

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


async def setup_hook():
    # Register persistent per-request buttons so they keep working across
    # restarts, then load the subs cog (adds the /subs command + board logic).
    bot.add_dynamic_items(
        subs.NewRequestButton,
        subs.AvailableButton,
        subs.ManageButton,
        subs.ConfirmButton,
        subs.DeclineButton,
        JoinPracticeButton,
    )
    await bot.add_cog(subs.Subs(bot))

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
        await cog.startup()  # prune expired requests and refresh the pinned board
    print(f"✅  Logged in as {bot.user}.")


# ── Practice ice: live report + open sign-up pool ───────────────────────────

MAX_SIGNUP_BUTTONS = 20  # leave headroom under Discord's 25-component cap

def session_key(opp: dict) -> str:
    """Stable per-slot key from the session start minute (e.g. '20260616T1945')."""
    return opp["start"].strftime("%Y%m%dT%H%M")


class SheetsError(Exception):
    """Carries a user-facing message for a failed /sheets fetch."""


async def fetch_opps(upcoming: int) -> list[dict]:
    """Fetch the practice-ice opportunities (raises SheetsError with a message)."""
    try:
        practices = await fetch_upcoming_practices(upcoming)
    except Exception:  # noqa: BLE001 — log detail server-side, show a safe message
        log.exception("Calendar fetch failed")
        raise SheetsError("❌  Could not reach the event calendar right now — please try again shortly.")
    if not practices:
        return []
    window_start = now_club()
    window_end   = max(datetime.fromisoformat(p["end_date"]) for p in practices)
    try:
        async with GFClient() as gf:
            sessions = await collect_sessions(practices, window_start, window_end, gf)
    except Exception:  # noqa: BLE001 — never surface raw errors (they can carry the GF URL+creds)
        log.exception("Registration data fetch failed")
        raise SheetsError("❌  Couldn't load registration data right now — please try again shortly.")
    return pi.practice_opportunities(sessions, TOTAL_SHEETS)


async def build_sheets_payload(upcoming: int, user) -> tuple[discord.Embed, discord.ui.View | None]:
    """Build the (ephemeral) practice-ice embed + sign-up view for a member."""
    opps = await fetch_opps(upcoming)

    # Register each session and snapshot sign-up counts under the lock.
    async with _practice_lock:
        ps.expire(_practice_state, now_club())
        keyed = []
        for o in opps[:MAX_SIGNUP_BUTTONS]:
            key = session_key(o)
            label = f"{o['start'].strftime('%a %b %-d')} · {o['start'].strftime('%-I:%M %p')}"
            ps.register_session(_practice_state, key, when_ts=o["start"].isoformat(),
                                label=label, sheets=o.get("free"))
            keyed.append((o, key, ps.count(_practice_state, key),
                          ps.is_signed_up(_practice_state, key, user.id)))
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
        lines.append(line)
        view.add_item(JoinPracticeButton(key, upcoming, o["start"], n, mine))
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Tap a slot to sign up · only you can see this report")
    return embed, view


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
    e.set_footer(text="Use /sheets to join · updates as people sign up")
    return e


async def render_practice_board(target_channel=None):
    """Edit the pinned practice board (creating/moving it to `target_channel` when
    given). Safe to call with no target — it just refreshes an existing board."""
    async with _practice_lock:
        ps.expire(_practice_state, now_club())
        embed = build_practice_board_embed()
        board = _practice_state.get("board")
        ps.save(PRACTICE_STORE_PATH, _practice_state)

    # If the board exists in a different channel than where the action happened,
    # move it (people want it where they're using the bot).
    if board and target_channel is not None and board.get("channel_id") != target_channel.id:
        try:
            ch = bot.get_channel(board["channel_id"]) or await bot.fetch_channel(board["channel_id"])
            old = await ch.fetch_message(board["message_id"])
            await old.delete()
        except discord.HTTPException:
            pass
        board = None

    if board:
        try:
            ch = bot.get_channel(board["channel_id"]) or await bot.fetch_channel(board["channel_id"])
            msg = await ch.fetch_message(board["message_id"])
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            board = None
        except discord.Forbidden as e:
            # 50005 = board authored by another bot identity (e.g. a dev bot reusing
            # the prod data dir). Drop the stale pointer and fall through to repost
            # one we own (when called with a target channel).
            if e.code == 50005:
                log.warning("Practice board %s authored by another bot — clearing stale pointer.",
                            board["message_id"])
                async with _practice_lock:
                    _practice_state["board"] = None
                    ps.save(PRACTICE_STORE_PATH, _practice_state)
                board = None
            else:
                return
        except discord.HTTPException:
            return

    if target_channel is not None:
        try:
            msg = await target_channel.send(embed=embed)
            try:
                await msg.pin()
            except discord.HTTPException:
                pass  # no Manage Messages — board still works unpinned
        except discord.HTTPException:
            return
        async with _practice_lock:
            _practice_state["board"] = {"channel_id": target_channel.id, "message_id": msg.id}
            ps.save(PRACTICE_STORE_PATH, _practice_state)


class JoinPracticeButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"sheet:join:(?P<key>\d{8}T\d{4}):(?P<up>\d+)"):
    def __init__(self, key: str, upcoming: int, start: datetime | None = None,
                 count: int = 0, mine: bool = False):
        self.key = key
        self.upcoming = int(upcoming)
        when = start.strftime('%a %-I:%M%p').lower() if start else key
        label = f"{'Leave' if mine else 'Practice'} {when} ({count})"
        super().__init__(discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.secondary if mine else discord.ButtonStyle.success,
            custom_id=f"sheet:join:{key}:{upcoming}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["key"], int(match["up"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
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

        # Refresh the (private) report in place so counts stay current.
        try:
            embed, view = await build_sheets_payload(self.upcoming, interaction.user)
            await interaction.edit_original_response(embed=embed, view=view)
        except SheetsError:
            pass  # keep the existing message if the refresh fetch hiccups

        if repeat:
            return  # nothing changed — don't re-ping the channel or re-notify

        # Update the shared, pinned practice board in this channel…
        await render_practice_board(interaction.channel)
        # …and ping the channel when someone newly joins, so others can join in.
        if result == "joined" and interaction.channel is not None:
            try:
                await interaction.channel.send(
                    f"🧹  **{interaction.user.display_name}** is in for **{label}** "
                    f"— {n} signed up to practice. Come throw some rocks!")
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            (f"🧹  You're signed up for **{label}** — the channel board's updated." if result == "joined"
             else f"👍  Removed you from practice on **{label}**."),
            ephemeral=True)


@bot.tree.command(name="sheets", description="Check free sheets and sign up to practice (only you see it)")
@app_commands.describe(upcoming="How many upcoming practices to show (1–5, default 1)")
async def sheets_cmd(interaction: discord.Interaction, upcoming: int = 1):
    upcoming = max(1, min(upcoming, 5))
    await interaction.response.defer(ephemeral=True)
    try:
        embed, view = await build_sheets_payload(upcoming, interaction.user)
    except SheetsError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


bot.run(DISCORD_TOKEN)
