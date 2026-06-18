"""
Subs board — an interactive, persistent Discord widget for "I need a sub" /
"I can sub" coordination.

One pinned message per channel acts as a live board. Members interact entirely
through buttons:

  ➕ Need a sub   — opens a modal (date, time, position, spots, notes) and posts
                    a request onto the board.
  Sub for …      — one button per open request; click to take an open spot,
                    click again to drop it. The requester is notified on change.
  🙋 I can sub    — toggle yourself onto the "available subs" list.
  🛠 Manage       — requester-only panel to add a specific person to a spot,
                    remove someone, or close a request early.

Requests auto-expire a few hours after their game time, so played-out games drop
off the board on their own. State lives in a small JSON file (see sub_store).

Notifications try a DM first and fall back to an @-mention in the board channel.

discord.py >= 2.4 is required for DynamicItem (persistent per-request buttons
that survive a bot restart without re-registering each message).
"""

from __future__ import annotations

import os
import re
import html
import time
import logging
from datetime import datetime, date, timedelta, timezone

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
MAX_BUTTON_REQUESTS = 20  # Discord caps a message at 25 components; reserve a row for controls.
# Several buttons act on shared state and can be impatiently double-tapped before
# the first click visibly resolves. We ignore a repeat click (same user, same
# target) within this window so a double-tap is idempotent: a "Take a spot" toggle
# can't take-then-drop, and a Confirm/Decline can't clobber its own result.
CLICK_DEBOUNCE_SECONDS = 3.0

CID_NEW    = "sub:new"
CID_AVAIL  = "sub:avail"
CID_MANAGE = "sub:manage"


def club_now() -> datetime:
    """Current club-local time as a naive datetime (matches stored game_ts)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)


# ── Date/time parsing for the modal ─────────────────────────────────────────

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d", "%b %d", "%B %d", "%m-%d")
_TIME_FORMATS = ("%I:%M %p", "%I %p", "%I:%M%p", "%I%p", "%H:%M")


def parse_when(date_str: str, time_str: str, *, ref: datetime | None = None) -> datetime:
    """
    Parse a date + time the way a member would type it. Accepts e.g.
    date "2026-06-20", "6/20", "Jun 20"; time "7:30 PM", "7pm", "19:30".
    Formats without a year assume the year that keeps the game in the future.
    Raises ValueError if neither field parses.
    """
    ref = ref or club_now()
    date_str = date_str.strip()
    time_str = time_str.strip().upper().replace(".", "")

    d = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(date_str, fmt)
        except ValueError:
            continue
        d = parsed
        if "%Y" not in fmt:  # no year supplied — assume current, roll forward if past
            d = d.replace(year=ref.year)
        break
    if d is None:
        raise ValueError(f"Couldn't read the date “{date_str}”. Try e.g. 2026-06-20 or Jun 20.")

    t = None
    for fmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(time_str, fmt)
            break
        except ValueError:
            continue
    if t is None:
        raise ValueError(f"Couldn't read the time “{time_str}”. Try e.g. 7:30 PM or 19:30.")

    when = d.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    # If a year-less date landed clearly in the past, it's next year's game.
    if when < ref - timedelta(days=1):
        when = when.replace(year=when.year + 1)
    return when


def fmt_when(game_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(game_ts)
    except (ValueError, TypeError):
        return game_ts or "TBD"
    return f"{dt.strftime('%a %b %-d')} · {dt.strftime('%-I:%M %p')}"


def fmt_when_short(game_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(game_ts)
    except (ValueError, TypeError):
        return "TBD"
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


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


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
        dt = draw_to_datetime(d) or datetime.combine(dd, datetime.min.time())
        dt = dt.replace(second=0, microsecond=0)
        out.append({"iso": dt.isoformat(), "label": fmt_when(dt.isoformat()), "dt": dt})
    out.sort(key=lambda g: g["dt"])
    seen, uniq = set(), []
    for g in out:
        if g["iso"] in seen:
            continue
        seen.add(g["iso"])
        uniq.append(g)
    return uniq


# ── Board rendering ─────────────────────────────────────────────────────────

BOARD_TITLE = f"🥌  Subs Board — {CLUB_NAME}"


def _looks_like_board(msg) -> bool:
    """True if a message is one of our pinned subs boards (by embed title)."""
    return any("Subs Board" in (e.title or "") for e in msg.embeds)


def _game_key(iso: str) -> str:
    """Game timestamp normalized to the minute, for matching availability to requests."""
    try:
        return datetime.fromisoformat(iso).replace(second=0, microsecond=0).isoformat()
    except (ValueError, TypeError):
        return iso or ""


def build_embed(state: dict) -> discord.Embed:
    reqs = store.requests_sorted(state)
    e = discord.Embed(title=BOARD_TITLE, color=0x1a6bb5)

    if not reqs:
        e.description = "No open sub requests right now.\n\nPress **➕ Need a sub** to post one."
    else:
        lines = []
        for i, r in enumerate(reqs[:MAX_BUTTON_REQUESTS], start=1):
            opn = store.open_spots(r)
            icon = "🟢" if opn > 0 else "✅"
            # League name is intentionally omitted — it's redundant with the
            # date/time (and the league title itself embeds the season/date).
            bits = []
            if r.get("team"):
                bits.append(f"Team {r['team']}")
            status = f"{len(r.get('filled', []))}/{r['spots_needed']} filled"
            # Name the subs so you can see who you're playing with (pending = invited
            # but not yet confirmed).
            names = [f["name"] for f in r.get("filled", [])]
            names += [f"{p['name']} (pending)" for p in r.get("pending", [])]
            if names:
                status += " — " + ", ".join(names)
            bits.append(status)
            lines.append(f"`{i}.` {icon}  **{fmt_when(r['game_ts'])}** · " + " · ".join(bits))
        e.description = "\n\n".join(lines)
        if len(reqs) > MAX_BUTTON_REQUESTS:
            e.description += f"\n\n…and {len(reqs) - MAX_BUTTON_REQUESTS} more (older ones fill first)."

    avail = state.get("availability", [])
    if avail:
        # Anyone already filled/pending on a request is no longer "available" for that
        # game — hide them from the list (keyed by user + league + game-minute). When a
        # requester later removes them they're un-committed again and reappear here;
        # only a self-drop deletes their availability outright.
        committed = set()
        for r in state.get("requests", []):
            lid_r = str(r.get("league_id") or "")
            gk = _game_key(r.get("game_ts", ""))
            for m in r.get("filled", []) + r.get("pending", []):
                committed.add((m["user_id"], lid_r, gk))
        # Grouped by league → game so the board answers "who can sub THIS game?" at a
        # glance, instead of a per-person list you have to scan for dates. Subs who
        # listed no specific games are shown on an "any game" line for that league.
        groups: dict[str, dict] = {}
        order: list[str] = []
        for a in avail:
            lkey = str(a.get("league_id") or "")
            if lkey not in groups:
                groups[lkey] = {"title": clean_title(a.get("league", "")) or "Other league",
                                "games": {}, "any": []}
                order.append(lkey)
            g = groups[lkey]
            games = a.get("games") or []
            if games:
                for iso in games:
                    if (a["user_id"], lkey, _game_key(iso)) in committed:
                        continue  # already subbing this game — not available for it
                    g["games"].setdefault(iso, []).append(a["name"])
            else:
                g["any"].append(a["name"])
        rows = []
        for lkey in order:
            g = groups[lkey]
            if not g["games"] and not g["any"]:
                continue  # everyone offered here is already committed — nothing to show
            rows.append(f"**{_truncate(g['title'], 40)}**")
            for iso in sorted(g["games"]):
                rows.append(f"{fmt_when(iso)} — {', '.join(sorted(g['games'][iso]))}")
            if g["any"]:
                rows.append(f"any game — {', '.join(sorted(g['any']))}")
        if rows:
            e.add_field(name="🙋  Available to sub", value="\n".join(rows)[:1024], inline=False)

    e.set_footer(text="➕ post a request · 🙋 offer to sub")
    return e


def build_view(state: dict) -> discord.ui.View:
    # Just the three control buttons. Claiming an open spot is done from the
    # "I can sub" flow (multi-select of open requests), not per-request buttons.
    view = discord.ui.View(timeout=None)
    view.add_item(NewRequestButton())
    view.add_item(AvailableButton())
    view.add_item(ManageButton())
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
            label="I can sub", emoji="🙋",
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


class ManageButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:manage"):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Manage", emoji="🛠️",
            style=discord.ButtonStyle.secondary, custom_id=CID_MANAGE, row=0,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        uid = interaction.user.id
        has_avail = any(a.get("user_id") == uid for a in cog.state.get("availability", []))
        if not (_my_requests(cog.state, uid) or _my_spots(cog.state, uid) or has_avail):
            await interaction.response.send_message(
                "Nothing to manage yet — you have no open requests, sub spots, or availability listed.",
                ephemeral=True)
            return
        await interaction.response.send_message(
            "**Manage** — your requests, the spots you're subbing, and your availability:",
            view=ManageHomeView(cog.state, uid), ephemeral=True)


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
        opts = [
            discord.SelectOption(
                label=_truncate(clean_title(l.get("title", "")), 100),
                value=str(l["id"]),
                description=(l.get("day") or None),
                default=(str(l["id"]) == str(selected)),
            )
            for l in leagues[:25]
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


class TeamSelect(discord.ui.Select):
    def __init__(self, names: list[str], selected, row: int = 1):
        opts = _unique_options([
            discord.SelectOption(label=_truncate(n, 100), value=_truncate(n, 100), default=(n == selected))
            for n in names[:24]
        ])
        # Always allow a typed team — some leagues expose no team list, and the
        # requester may want a name that isn't in it.
        opts.append(discord.SelectOption(label="⌨️ Enter team manually…", value="__manual__"))
        placeholder = "Your team…" if names else "Enter your team…"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__manual__":
            await interaction.response.send_modal(TeamModal(self.view))
            return
        self.view.team = self.values[0]
        await self.view.refresh(interaction)


class TeamModal(discord.ui.Modal, title="Enter your team"):
    team_in = discord.ui.TextInput(label="Team name", placeholder="e.g. Simpson", required=True, max_length=60)

    def __init__(self, flow):
        super().__init__()
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        self.flow.team = str(self.team_in).strip()
        await interaction.response.defer()
        try:
            await self.flow.message.edit(content=self.flow.prompt(), view=self.flow.build())
        except discord.HTTPException:
            pass


class GameSelect(discord.ui.Select):
    def __init__(self, games: list[dict], selected_isos, *, multi: bool, allow_manual: bool, row: int = 2):
        self.multi = multi
        # Two draws can share a start time; dedupe so the iso values stay unique.
        opts = _unique_options([
            discord.SelectOption(label=_truncate(g["label"], 100), value=g["iso"], default=(g["iso"] in (selected_isos or [])))
            for g in games[:23]
        ])
        if allow_manual:
            opts.append(discord.SelectOption(label="⌨️ Enter date manually…", value="__manual__"))
        if not opts:
            opts = [discord.SelectOption(label="No games in the next 2 weeks", value="__none__")]
        super().__init__(
            placeholder=("Games you can cover…" if multi else "Which game…"),
            min_values=1, max_values=(len(opts) if multi else 1), options=opts, row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        vals = self.values
        if "__none__" in vals:
            await interaction.response.defer()
            return
        if "__manual__" in vals:
            await interaction.response.send_modal(ManualDateModal(self.view))
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


class ManualDateModal(discord.ui.Modal, title="Enter game date & time"):
    date_in = discord.ui.TextInput(label="Game date", placeholder="2026-06-20  or  Jun 20", required=True, max_length=20)
    time_in = discord.ui.TextInput(label="Game time", placeholder="7:30 PM  or  19:30", required=True, max_length=12)

    def __init__(self, flow):
        super().__init__()
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        try:
            when = parse_when(str(self.date_in), str(self.time_in))
        except ValueError as ex:
            await interaction.response.send_message(f"⚠️  {ex}", ephemeral=True)
            return
        self.flow.game_iso = when.replace(second=0, microsecond=0).isoformat()
        await interaction.response.defer()
        try:
            await self.flow.message.edit(content=self.flow.prompt(), view=self.flow.build())
        except discord.HTTPException:
            pass


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
            # Always offer the team step (dropdown when the league lists teams, plus
            # a manual-entry option) — team is required info for a sub request.
            self.add_item(TeamSelect(names, self.team, row=1))
            self.add_item(GameSelect(
                league_games(lg, club_now()),
                [self.game_iso] if self.game_iso else [],
                multi=False, allow_manual=True, row=2,
            ))
            self.add_item(SpotsSelect(self.spots, row=3))
            self.add_item(PostNeedButton(disabled=not self.ready(), row=4))
        return self

    def ready(self) -> bool:
        lg = self.league()
        if not lg:
            return False
        return bool(self.team) and bool(self.game_iso)

    def prompt(self) -> str:
        lg = self.league()
        if not lg:
            return "**Need a sub** — pick the league:"
        parts = [f"League: **{clean_title(lg.get('title', ''))}**"]
        if self.team:
            parts.append(f"Team: **{self.team}**")
        if self.game_iso:
            parts.append(f"Game: **{fmt_when(self.game_iso)}**")
        parts.append(f"Spots: **{self.spots}**")
        tail = "Press **Post request**." if self.ready() else "Pick team, game, and spots."
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
        title = clean_title(lg.get("title", "")) if lg else ""
        cog: "Subs" = interaction.client.get_cog("Subs")
        status, req = await cog.add_request(
            requester=interaction.user,
            league_id=f.league_id or "",
            league=title,
            team=f.team or "",
            game_ts=f.game_iso,
            spots=f.spots,
        )
        if status == "duplicate":
            # Don't stack an identical request on the board. Keep the flow open so
            # they can tweak the team/game and re-post if it really is different.
            f.posted = False
            await interaction.edit_original_response(
                content=(f"⚠️  There's already an open request for **{f.team}** · "
                         f"{fmt_when(f.game_iso)}. Claim or **Manage** that one instead — "
                         "or change the team/game below and re-post.\n\n" + f.prompt()),
                view=f.build(),
            )
            return
        # Offer to invite an available sub straight away (optional).
        view = InviteView(req["id"], cog.state)
        await interaction.edit_original_response(
            content=(f"✅  Posted: **{title}** · {f.team or '—'} · {fmt_when(f.game_iso)} · needs {f.spots}.\n"
                     "Invite an available sub now (optional), or just leave it on the board:"),
            view=view,
        )


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

    def open_requests(self) -> list[dict]:
        """All open requests (any league) the user could claim — shown first so they
        can grab a spot without first having to pick a league."""
        return _open_requests_for_fill(self.state, None, [], self.user_id)

    def build(self) -> "AvailFlowView":
        self.clear_items()
        row = 0
        # Claim open spots FIRST — always shown (a disabled placeholder when nothing
        # is open), any league, no league pick needed.
        self.add_item(FillOpenRequestSelect(self.open_requests(), row=row))
        row += 1
        # Then the optional general-availability path: pick a league → games → post.
        self.add_item(LeagueSelect(self.leagues, self.league_id, row=row))
        row += 1
        lg = self.league()
        if lg:
            games = league_games(lg, club_now())
            if games:
                self.add_item(GameSelect(games, self.game_isos, multi=True, allow_manual=False, row=row))
                row += 1
            self.add_item(PostAvailButton(row=row))
        return self

    def prompt(self) -> str:
        lines = ["**I can sub**"]
        has_open = bool(self.open_requests())
        if has_open:
            lines.append("🔔  **Claim open spots** — pick any games that need a sub (any league) "
                         "from the top menu.")
        prefix = "Or list " if has_open else "List "
        lg = self.league()
        if lg:
            s = f"**general availability** · {clean_title(lg.get('title', ''))}"
            if self.game_isos:
                s += " · " + ", ".join(fmt_when(g) for g in self.game_isos)
            lines.append(f"{prefix}{s} — choose games (or none for any), then **Post availability**.")
        else:
            lines.append(f"{prefix}**general availability** for other times — pick a league below.")
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
        title = clean_title(lg.get("title", ""))
        cog: "Subs" = interaction.client.get_cog("Subs")
        await cog.add_availability(user=interaction.user, league_id=view.league_id, league=title, games=view.game_isos)
        gtxt = ", ".join(fmt_when(g) for g in view.game_isos) if view.game_isos else "any game"
        await interaction.edit_original_response(
            content=f"✅  Listed you as available for **{title}** · {gtxt}.", view=None)


def _open_requests_for_fill(state: dict, league_id, game_isos, user_id: int) -> list[dict]:
    """Open requests the user could fill from the 'I can sub' flow: in the chosen
    league, matching the chosen game(s) (or any game in the league if none chosen),
    with an open spot, excluding the user's own requests and ones they're already in."""
    lid = str(league_id or "")
    out = []
    for r in store.requests_sorted(state):
        if lid and str(r.get("league_id") or "") != lid:
            continue
        if store.open_spots(r) <= 0:
            continue
        if r.get("requester_id") == user_id or store.is_involved(r, user_id):
            continue
        if game_isos and not any(_same_game(r.get("game_ts", ""), g) for g in game_isos):
            continue
        out.append(r)
    return out


class FillOpenRequestSelect(discord.ui.Select):
    """Surfaced in the 'I can sub' flow: pick one or more open requests (any league)
    to claim their spots directly (each requester is notified). Renders as a disabled
    placeholder when nothing is open, so the option stays visible instead of vanishing."""
    def __init__(self, reqs: list[dict], row: int = 1):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(f"{fmt_when_short(r['game_ts'])}"
                                + (f" · Team {r['team']}" if r.get("team") else ""), 100),
                value=r["id"],
                description=_truncate(f"{store.open_spots(r)} of {r['spots_needed']} spots open", 100),
            )
            for r in reqs[:25]
        ])
        disabled = not opts
        if disabled:
            opts = [discord.SelectOption(label="No open requests to claim right now", value="__none__")]
        super().__init__(
            placeholder=("No open requests right now" if disabled else "Claim open spot(s) that need a sub…"),
            min_values=1, max_values=(1 if disabled else max(1, len(opts))),
            options=opts, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.values and self.values[0] == "__none__":
            return  # disabled placeholder — nothing to claim
        cog: "Subs" = interaction.client.get_cog("Subs")
        filled, already, unavailable = [], [], []
        for rid in self.values:
            result, req = await cog.fill_request_spot(interaction.user, rid)
            when = fmt_when(req["game_ts"]) if req else "that game"
            if result == "added":
                filled.append(when)
            elif result == "already":
                already.append(when)
            else:  # full / closed
                unavailable.append(when)
        parts = []
        if filled:
            parts.append("✅  You're in for: " + ", ".join(filled) + " — requester(s) notified.")
        if already:
            parts.append("Already in for: " + ", ".join(already) + ".")
        if unavailable:
            parts.append("Couldn't claim (filled up or closed): " + ", ".join(unavailable) + ".")
        await interaction.edit_original_response(content="\n".join(parts) or "Nothing to claim.", view=None)


# ── Invite an available sub (requester picks → DM confirmation) ─────────────

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


def _find_open_duplicate(state: dict, league_id, game_ts: str, team: str) -> dict | None:
    """An existing open request for the same league + game + team (case/space
    tolerant), or None. All requests on the board are open, so a match is a dup."""
    lid = str(league_id or "")
    team_norm = (team or "").strip().casefold()
    for r in state.get("requests", []):
        if str(r.get("league_id") or "") != lid:
            continue
        if (r.get("team") or "").strip().casefold() != team_norm:
            continue
        if _same_game(r.get("game_ts", ""), game_ts or ""):
            return r
    return None


def _availability_for_request(state: dict, req: dict | None) -> list[dict]:
    """Subs who can cover THIS request: available in its league AND for its game
    time. An availability with no specific games listed covers any game in that
    league. Deduped by user; the requester-side filtering (anyone already filled or
    pending) is applied so they don't show up as invitable."""
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


class InviteView(discord.ui.View):
    """Ephemeral picker shown after posting or via Manage."""
    def __init__(self, rid: str, state: dict):
        super().__init__(timeout=300)
        req = store.find_request(state, rid)
        self.add_item(InviteSelect(rid, _availability_for_request(state, req)))


class InviteSelect(discord.ui.Select):
    def __init__(self, rid: str, entries: list[dict], row: int | None = None):
        self.rid = rid
        self.names = {int(a["user_id"]): a["name"] for a in entries}
        opts = []
        for a in entries[:25]:
            games = a.get("games") or []
            desc = ", ".join(fmt_when_short(g) for g in games) if games else "any game"
            opts.append(discord.SelectOption(
                label=_truncate(a["name"], 100), value=str(a["user_id"]), description=_truncate(desc, 100)))
        opts = _unique_options(opts)  # belt-and-braces: never emit a duplicate user value
        disabled = not opts
        if not opts:
            opts = [discord.SelectOption(label="No available subs to invite", value="__none__")]
        super().__init__(placeholder="Invite an available sub…", min_values=1, max_values=1,
                         options=opts, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction):
        # Ack immediately (the invite does a DM + board sync that can be slow).
        await interaction.response.defer()
        if self.values[0] == "__none__":
            return
        uid = int(self.values[0])
        name = self.names.get(uid, "a sub")
        cog: "Subs" = interaction.client.get_cog("Subs")
        # invite_sub is idempotent (returns "already" if they're already pending/
        # filled), so a repeated pick can't double-book or double-DM.
        result = await cog.invite(self.rid, uid, name, inviter=interaction.user)
        msgs = {
            "invited": f"📨  Invited **{name}** — they'll get a DM to confirm. Pending until they accept.",
            "already": f"**{name}** is already on this request.",
            "full": "No open spots left to invite into.",
            "closed": "That request is no longer available.",
        }
        view = InviteView(self.rid, cog.state) if result in ("invited", "already") else None
        await interaction.edit_original_response(content=msgs.get(result, "Done."), view=view)


def confirm_view(rid: str, uid) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ConfirmButton(rid, uid))
    v.add_item(DeclineButton(rid, uid))
    return v


class ConfirmButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"sub:confirm:(?P<rid>[0-9a-f]+):(?P<uid>\d+)"):
    def __init__(self, rid: str, uid):
        self.rid = rid
        self.uid = str(uid)
        super().__init__(discord.ui.Button(
            label="Confirm", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"sub:confirm:{rid}:{uid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"], match["uid"])

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        await cog.handle_invite_response(interaction, self.rid, int(self.uid), confirm=True)


class DeclineButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"sub:decline:(?P<rid>[0-9a-f]+):(?P<uid>\d+)"):
    def __init__(self, rid: str, uid):
        self.rid = rid
        self.uid = str(uid)
        super().__init__(discord.ui.Button(
            label="Can't", emoji="❌", style=discord.ButtonStyle.danger,
            custom_id=f"sub:decline:{rid}:{uid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"], match["uid"])

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        await cog.handle_invite_response(interaction, self.rid, int(self.uid), confirm=False)


# ── Manage flow (ephemeral) ─────────────────────────────────────────────────
# Manages both sides of the board for the caller: requests they opened, the spots
# they're subbing (drop), and the availability they've listed (remove).

def _my_requests(state: dict, uid: int) -> list[dict]:
    return [r for r in store.requests_sorted(state) if r.get("requester_id") == uid]


def _my_spots(state: dict, uid: int) -> list[dict]:
    """Requests where the caller is a sub (filled or pending)."""
    return [r for r in store.requests_sorted(state)
            if store.is_filled_by(r, uid) or store.is_pending_by(r, uid)]


class ManageHomeView(discord.ui.View):
    """One ephemeral panel with a select per category the caller has anything in."""
    def __init__(self, state: dict, uid: int):
        super().__init__(timeout=180)
        row = 0
        my_requests = _my_requests(state, uid)
        if my_requests:
            self.add_item(ManagePickSelect(my_requests, row=row))
            row += 1
        my_spots = _my_spots(state, uid)
        if my_spots:
            self.add_item(DropSpotSelect(my_spots, uid, row=row))
            row += 1
        my_avail = [a for a in state.get("availability", []) if a.get("user_id") == uid]
        if my_avail:
            self.add_item(RemoveAvailSelect(my_avail, row=row))
            row += 1


class ManagePickSelect(discord.ui.Select):
    def __init__(self, requests: list[dict], row: int | None = None):
        options = [
            discord.SelectOption(
                label=fmt_when(r["game_ts"])[:100],
                value=r["id"],
                description=(f"{store.open_spots(r)} open · filled: " +
                             (", ".join(f["name"] for f in r["filled"]) or "nobody"))[:100],
            )
            for r in requests[:25]
        ]
        super().__init__(placeholder="Manage a request you opened…",
                         options=options, min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.values[0])
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.response.edit_message(content="That request is no longer available.", view=None)
            return
        await interaction.response.edit_message(
            content=f"Managing **{fmt_when(req['game_ts'])}** ({store.open_spots(req)} open):",
            view=ManageActionView(req["id"], cog.state),
        )


class DropSpotSelect(discord.ui.Select):
    """Drop a spot the caller is subbing (filled or pending). Requester is notified."""
    def __init__(self, spots: list[dict], uid: int, row: int | None = None):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(f"{fmt_when_short(r['game_ts'])}"
                                + (f" · Team {r['team']}" if r.get("team") else ""), 100),
                value=r["id"],
                description=_truncate(
                    "awaiting your confirmation" if store.is_pending_by(r, uid) else "you're in", 100),
            )
            for r in spots[:25]
        ])
        super().__init__(placeholder="Drop a spot you're subbing…",
                         options=opts, min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        rid = self.values[0]
        if cog._is_repeat_click(cog._click_cooldown, ("drop", interaction.user.id, rid)):
            return
        result, req = await cog.drop_sub_spot(interaction.user, rid)
        if result == "removed":
            msg = (f"➖  Dropped your spot for **{fmt_when(req['game_ts'])}** — "
                   "the requester's been notified.")
        elif result == "absent":
            msg = "You weren't in that spot."
        else:  # closed
            msg = "That request is no longer on the board."
        await interaction.edit_original_response(content=msg, view=None)


class RemoveAvailSelect(discord.ui.Select):
    """Remove one of the caller's availability listings (keyed by league)."""
    def __init__(self, entries: list[dict], row: int | None = None):
        opts = _unique_options([
            discord.SelectOption(
                label=_truncate(clean_title(a.get("league", "")) or "League", 100),
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
        removed = await cog.remove_availability(interaction.user.id, league_id)
        msg = "🗑️  Removed that availability listing." if removed else "That listing was already gone."
        await interaction.edit_original_response(content=msg, view=None)


class ManageActionView(discord.ui.View):
    def __init__(self, rid: str, state: dict):
        super().__init__(timeout=180)
        self.rid = rid
        self.add_item(AddSubSelect(rid))                       # row 0 — add/remove a member directly
        req = store.find_request(state, rid)
        self.add_item(InviteSelect(rid, _availability_for_request(state, req), row=1))  # invite (DM confirm)
        # Close button is the decorated method below (row 2).

    @discord.ui.button(label="Close request", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.rid)
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.edit_original_response(content="That request is no longer available.", view=None)
            return
        # close_request is idempotent, but debounce so a double-tap doesn't clobber
        # the "closed" message with "no longer available".
        if cog._is_repeat_click(cog._click_cooldown, ("close", interaction.user.id, self.rid)):
            return
        await cog.close_request(self.rid)
        await interaction.edit_original_response(content="✅  Request closed and removed from the board.", view=None)


class AddSubSelect(discord.ui.UserSelect):
    def __init__(self, rid: str):
        self.rid = rid
        super().__init__(placeholder="Add or remove someone as a sub…", min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.rid)
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.edit_original_response(content="That request is no longer available.", view=None)
            return
        member = self.values[0]
        if member.id == req["requester_id"]:
            await interaction.edit_original_response(
                content=("You can't add yourself as a sub to your own request.\n"
                         f"Managing **{fmt_when(req['game_ts'])}** ({store.open_spots(req)} open):"),
                view=ManageActionView(self.rid, cog.state))
            return
        # requester_toggle_sub is a toggle (add ↔ remove); debounce a rapid repeat
        # of the same member so a double-pick can't add-then-remove them.
        if cog._is_repeat_click(cog._click_cooldown, ("manage_add", interaction.user.id, self.rid, member.id)):
            await interaction.edit_original_response(
                content=f"Managing **{fmt_when(req['game_ts'])}** ({store.open_spots(req)} open):",
                view=ManageActionView(self.rid, cog.state),
            )
            return
        result = await cog.requester_toggle_sub(req, member)
        if result == "added":
            note = f"✅  Added {member.display_name} as a sub."
        elif result == "removed":
            note = f"➖  Removed {member.display_name} from the spots."
        else:  # full
            note = f"⚠️  No open spots — {member.display_name} not added."
        await interaction.edit_original_response(
            content=f"{note}\nManaging **{fmt_when(req['game_ts'])}** ({store.open_spots(req)} open):",
            view=ManageActionView(self.rid, cog.state),
        )


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

    async def cog_unload(self):
        self.expiry_loop.cancel()

    async def startup(self):
        """Prune and re-render the board after a (re)connect, and clean up any stray
        duplicate board pins left by a previous run/deploy."""
        async with self._lock:
            store.expire(self.state, club_now(), GRACE_HOURS)
            store.save(STORE_PATH, self.state)
        await self.render_board()
        board = self.state.get("board")
        if board:
            ch = await self._resolve_channel(board["channel_id"])
            if ch is not None:
                await self._sweep_board_pins(ch, keep_id=board["message_id"])

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

    async def _sweep_board_pins(self, channel, keep_id: int):
        """Unpin & delete any of OUR older board messages still pinned in `channel`
        (leftovers from a past deploy, reset, or a stale pointer), keeping only
        keep_id. Only touches board-looking messages this bot authored, so it can't
        clobber unrelated pins or another bot's board."""
        me = self.bot.user
        if me is None:
            return
        try:
            pins = await channel.pins()
        except discord.HTTPException:
            return
        for m in pins:
            if m.id != keep_id and m.author.id == me.id and _looks_like_board(m):
                try:
                    await m.delete()  # delete also unpins
                except discord.HTTPException:
                    pass

    async def _post_board(self, channel):
        """Post a fresh board in `channel`, pin it, repoint state at it, then remove
        any older board (tracked elsewhere or stray pins). Returns (pinned, pin_err)."""
        old = self.state.get("board")
        msg = await channel.send(embed=build_embed(self.state), view=build_view(self.state))
        pinned, pin_err = True, None
        try:
            await msg.pin()
        except discord.Forbidden:
            pinned, pin_err = False, "I need the **Manage Messages** permission to pin."
        except discord.HTTPException as ex:
            pinned, pin_err = False, f"pinning failed (`{ex}`)."
        async with self._lock:
            self.state["board"] = {"channel_id": channel.id, "message_id": msg.id}
            self._save()
        # One board only: drop stray pins here, and a prior board in another channel.
        await self._sweep_board_pins(channel, keep_id=msg.id)
        if old and old.get("channel_id") != channel.id:
            prev_ch = await self._resolve_channel(old["channel_id"])
            if prev_ch is not None:
                try:
                    await (await prev_ch.fetch_message(old["message_id"])).delete()
                except discord.HTTPException:
                    pass
        return pinned, pin_err

    async def render_board(self):
        board = self.state.get("board")
        if not board:
            return
        channel = await self._resolve_channel(board["channel_id"])
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(board["message_id"])
        except discord.NotFound:
            await self._post_board(channel)  # board was deleted — repost so it stays live
            return
        except discord.HTTPException:
            return
        try:
            await msg.edit(embed=build_embed(self.state), view=build_view(self.state))
        except discord.Forbidden as e:
            # 50005 = "cannot edit a message authored by another user": the stored
            # board belongs to a different bot identity. Repost one we own (and sweep
            # our own strays) rather than leaving a board that never updates.
            if e.code == 50005:
                log.warning("Subs board %s authored by another bot — reposting our own.",
                            board["message_id"])
                await self._post_board(channel)
            else:
                log.warning("Forbidden editing subs board: %s", e)
            return
        except discord.HTTPException as e:
            log.warning("Could not edit subs board: %s", e)
            return
        # The board we just updated must be the one that's actually pinned — otherwise
        # people keep watching a stale, unpinned copy. Re-pin it and clear strays if so.
        if not msg.pinned:
            try:
                await msg.pin()
                await self._sweep_board_pins(channel, keep_id=msg.id)
            except discord.HTTPException as e:
                log.warning("Could not pin the live subs board: %s", e)

    def board_channel(self):
        board = self.state.get("board")
        return self.bot.get_channel(board["channel_id"]) if board else None

    async def notify(self, user_id: int, text: str):
        """DM the user; fall back to an @-mention in the board channel."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(text)
            return
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        channel = self.board_channel()
        if channel is not None:
            try:
                await channel.send(f"<@{user_id}> {text}")
            except discord.HTTPException:
                log.warning("Could not notify user %s", user_id)

    # -- league data --------------------------------------------------------
    async def get_leagues(self) -> list[dict]:
        """Active (non-ended) leagues from the shared league cache."""
        try:
            leagues = await get_cached_leagues()
        except Exception as e:  # noqa: BLE001 — network/cache failure shouldn't crash the button
            log.warning("League fetch failed: %s", e)
            return []
        return [lg for lg in leagues if not lg.get("ended")]

    # -- mutations (called from button/modal callbacks) ---------------------
    async def add_request(self, *, requester, game_ts, spots, league_id="", league="", team=""):
        """Create a request, unless an open one already exists for the same league +
        game + team. Returns (status, req): ("duplicate", existing) or ("created", new).
        The dup check + create happen under one lock so two posts can't both slip in."""
        async with self._lock:
            dup = _find_open_duplicate(self.state, league_id, game_ts, team)
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
                now=club_now(),
            )
            self._save()
        await self.render_board()
        return ("created", req)

    async def invite(self, rid: str, uid: int, name: str, *, inviter) -> str:
        """Requester invites an available sub: reserve a pending spot and DM them."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return "closed"
            result = store.invite_sub(req, uid, name, now=club_now())
            when = fmt_when(req["game_ts"])
            league = req.get("league", "")
            team = req.get("team", "")
            self._save()
        if result != "invited":
            return result
        await self.render_board()
        text = (f"🥌  **{inviter.display_name}** asked you to sub"
                + (f" for **{league}**" if league else "")
                + (f" · Team {team}" if team else "")
                + f" on **{when}**.\nCan you do it?")
        await self._send_confirm(uid, text, rid)
        return result

    async def _send_confirm(self, uid: int, text: str, rid: str):
        """DM the invited user a Confirm/Can't prompt; fall back to a channel ping."""
        view = confirm_view(rid, uid)
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(text, view=view)
            return
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        channel = self.board_channel()
        if channel is not None:
            try:
                await channel.send(f"<@{uid}> {text}", view=view)
            except discord.HTTPException:
                log.warning("Could not send sub confirmation to %s", uid)

    async def handle_invite_response(self, interaction: discord.Interaction, rid: str, uid: int, *, confirm: bool):
        # Ack immediately: confirm/decline does board + DM work that can exceed the
        # 3s response deadline, and the instant feedback stops impatient re-clicks.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass  # already acked (e.g. a duplicate dispatch) — fall through

        if interaction.user.id != uid:
            await interaction.followup.send("This confirmation isn't for you.", ephemeral=True)
            return

        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                await interaction.edit_original_response(content="This request is no longer open.", view=None)
                return
            # Debounce a rapid double-tap so the duplicate can't clobber the result
            # message (e.g. overwrite "You're in!" with "no longer valid").
            if self._is_repeat_click(self._click_cooldown, ("invite_resp", uid, rid)):
                return
            if confirm:
                result = store.confirm_sub(req, uid, interaction.user.display_name, now=club_now())
            else:
                result = store.decline_sub(req, uid)
            requester_id = req["requester_id"]
            when = fmt_when(req["game_ts"])
            opn = store.open_spots(req)
            self._save()

        if result == "absent":
            await interaction.edit_original_response(content="This invite is no longer valid.", view=None)
            return
        await self.render_board()
        if result == "confirmed":
            await interaction.edit_original_response(content=f"✅  You're in for **{when}** — thanks for subbing!", view=None)
            await self.notify(requester_id,
                              f"🥌 {interaction.user.display_name} confirmed as a sub for your **{when}** game "
                              f"({opn} still open).")
        else:  # declined
            await interaction.edit_original_response(content=f"👍  Thanks for letting us know — declined **{when}**.", view=None)
            await self.notify(requester_id,
                              f"🥌 {interaction.user.display_name} can't sub for your **{when}** game "
                              f"({opn} open again).")

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

    async def add_availability(self, *, user, league_id, league, games) -> str:
        async with self._lock:
            result = store.upsert_availability(
                self.state, user_id=user.id, name=user.display_name,
                league_id=league_id, league=league, games=games, now=club_now(),
            )
            self._save()
        await self.render_board()
        return result

    async def remove_availability(self, user_id: int, league_id) -> bool:
        async with self._lock:
            removed = store.remove_availability(self.state, user_id, league_id)
            self._save()
        await self.render_board()
        return removed

    async def requester_toggle_sub(self, req: dict, member) -> str:
        """Requester adds a named member, or removes them if already in."""
        async with self._lock:
            if store.is_filled_by(req, member.id):
                store.remove_sub(req, member.id)
                result = "removed"
            else:
                result = store.add_sub(req, member.id, member.display_name, now=club_now())
            when = fmt_when(req["game_ts"])
            self._save()
        await self.render_board()
        if result == "added":
            await self.notify(member.id, f"🥌 You've been added as a sub for the **{when}** game.")
        elif result == "removed":
            await self.notify(member.id, f"🥌 You've been removed as a sub for the **{when}** game.")
        return result

    async def fill_request_spot(self, user, rid: str) -> tuple[str, dict | None]:
        """A sub self-fills an open request (e.g. from the 'I can sub' flow). Adds
        them to the spot and notifies the requester. Returns (result, req) where
        result is "added" | "already" | "full" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            result = store.add_sub(req, user.id, user.display_name, now=club_now())
            when = fmt_when(req["game_ts"])
            requester_id = req["requester_id"]
            opn = store.open_spots(req)
            self._save()
        await self.render_board()
        if result == "added" and requester_id != user.id:
            await self.notify(
                requester_id,
                f"🥌 {user.display_name} filled a sub spot for your **{when}** game "
                f"({opn} still open).")
        return (result, req)

    async def drop_sub_spot(self, user, rid: str) -> tuple[str, dict | None]:
        """A sub drops a spot they were filling/pending (from Manage). Notifies the
        requester they lost a sub. Returns (result, req) where result is
        "removed" | "absent" | "closed"."""
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                return ("closed", None)
            result = store.remove_sub(req, user.id)  # "removed" | "absent"
            when = fmt_when(req["game_ts"])
            requester_id = req["requester_id"]
            opn = store.open_spots(req)
            if result == "removed":
                # They opted out — also drop that game from their availability so the
                # board doesn't re-offer them for it. (A requester-side removal leaves
                # availability intact, so they reappear as available.)
                store.remove_availability_game(self.state, user.id, req.get("league_id"), req.get("game_ts"))
            self._save()
        await self.render_board()
        if result == "removed" and requester_id != user.id:
            await self.notify(
                requester_id,
                f"🥌 {user.display_name} dropped their sub spot for your **{when}** game "
                f"({opn} now open).")
        return (result, req)

    async def close_request(self, rid: str):
        # Capture the subs (filled + pending) before closing so we can tell them the
        # request they were on is gone — they were removed by someone else (the
        # requester), so they get a heads-up. The requester themselves is skipped.
        async with self._lock:
            req = store.find_request(self.state, rid)
            when, requester_id, subs = "", None, []
            if req is not None:
                when = fmt_when(req["game_ts"])
                requester_id = req["requester_id"]
                subs = [m["user_id"] for m in (req.get("filled", []) + req.get("pending", []))]
            store.close_request(self.state, rid)
            self._save()
        await self.render_board()
        for uid in subs:
            if uid != requester_id:
                await self.notify(
                    uid, f"🥌 The **{when}** game you were subbing was closed by the requester — "
                         "you're no longer needed. Thanks!")

    # -- background expiry --------------------------------------------------
    @tasks.loop(minutes=15)
    async def expiry_loop(self):
        async with self._lock:
            dropped = store.expire(self.state, club_now(), GRACE_HOURS)
            if dropped["requests"] or dropped["availability"]:
                self._save()
                changed = True
            else:
                changed = False
        if changed:
            await self.render_board()

    @expiry_loop.before_loop
    async def _before_expiry(self):
        await self.bot.wait_until_ready()

    # -- slash commands -----------------------------------------------------
    @app_commands.command(name="subs", description="Open your private subs board — only you can see it.")
    async def subs_cmd(self, interaction: discord.Interaction):
        """An ephemeral, interactive copy of the board, visible only to the caller.
        Taking a spot / posting / inviting all update the shared pinned board."""
        await interaction.response.send_message(
            content="Your subs board (only you can see this):",
            embed=build_embed(self.state), view=build_view(self.state), ephemeral=True)

    @app_commands.command(name="subsboard", description="Post & pin the shared subs board in this channel (organizers).")
    @app_commands.default_permissions(manage_messages=True)
    async def subsboard_cmd(self, interaction: discord.Interaction):
        # _post_board posts + pins + repoints state, then sweeps any older/stray board
        # pins (this channel) and a prior board in another channel — so there's only
        # ever one pinned board.
        await interaction.response.defer(ephemeral=True)
        pinned, pin_err = await self._post_board(interaction.channel)
        if pinned:
            await interaction.followup.send("📌  Shared subs board posted and pinned here.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅  Shared subs board posted — but I couldn't pin it: {pin_err}\n"
                "Grant the permission, then run `/subsboard` again to pin.",
                ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Subs(bot))
