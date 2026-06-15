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

def clean_title(title: str) -> str:
    """Decode HTML entities and trim the verbose '– Begins <date>' tail."""
    t = html.unescape(title or "")
    t = re.split(r"\s[–—-]\s*Begins\b", t)[0]
    return t.strip()


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

def build_embed(state: dict) -> discord.Embed:
    reqs = store.requests_sorted(state)
    e = discord.Embed(title=f"🥌  Subs Board — {CLUB_NAME}", color=0x1a6bb5)

    if not reqs:
        e.description = "No open sub requests right now.\n\nPress **➕ Need a sub** to post one."
    else:
        lines = []
        for i, r in enumerate(reqs[:MAX_BUTTON_REQUESTS], start=1):
            opn = store.open_spots(r)
            icon = "🟢" if opn > 0 else "✅"
            head = f"{icon}  **{fmt_when(r['game_ts'])}**"
            bits = []
            if r.get("league"):
                bits.append(_truncate(r["league"], 40))
            if r.get("team"):
                bits.append(f"Team {r['team']}")
            bits.append(f"needs {r['spots_needed']}")
            head += " · " + " · ".join(bits)
            names = [f["name"] for f in r.get("filled", [])]
            names += [f"{p['name']} (pending)" for p in r.get("pending", [])]
            filled = ", ".join(names) or "nobody yet"
            sub = f"    by {r['requester_name']} · filled: {filled}"
            sub += f" · {opn} open" if opn > 0 else " · full"
            lines.append(f"`{i}.` {head}\n{sub}")
        e.description = "\n\n".join(lines)
        if len(reqs) > MAX_BUTTON_REQUESTS:
            e.description += f"\n\n…and {len(reqs) - MAX_BUTTON_REQUESTS} more (older ones fill first)."

    avail = state.get("availability", [])
    if avail:
        rows = []
        for a in avail:
            line = "• " + a["name"]
            if a.get("league"):
                line += f" — {_truncate(a['league'], 40)}"
            games = a.get("games") or []
            if games:
                line += " · " + ", ".join(fmt_when(g) for g in games)
            else:
                line += " · any game"
            if a.get("note"):
                line += f" ({a['note']})"
            rows.append(line)
        e.add_field(name="🙋  Available to sub", value="\n".join(rows)[:1024], inline=False)

    e.set_footer(text="Tap a number to take a spot · ➕ post a request · 🙋 offer to sub")
    return e


def build_view(state: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(NewRequestButton())
    view.add_item(AvailableButton())
    view.add_item(ManageButton())
    for i, r in enumerate(store.requests_sorted(state)[:MAX_BUTTON_REQUESTS], start=1):
        view.add_item(TakeSpotButton(r["id"], index=i, req=r))
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
        await interaction.response.defer(ephemeral=True)
        leagues = await cog.get_leagues()
        if not leagues:
            await interaction.followup.send(
                "Couldn't load the league list just now — try again in a moment.", ephemeral=True)
            return
        view = NeedSubFlowView(leagues)
        view.message = await interaction.followup.send(content=view.prompt(), view=view, ephemeral=True)


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
        await interaction.response.defer(ephemeral=True)
        leagues = await cog.get_leagues()
        if not leagues:
            await interaction.followup.send(
                "Couldn't load the league list just now — try again in a moment.", ephemeral=True)
            return
        view = AvailFlowView(leagues, interaction.user.id)
        view.message = await interaction.followup.send(content=view.prompt(), view=view, ephemeral=True)


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
        mine = [r for r in store.requests_sorted(cog.state) if r["requester_id"] == interaction.user.id]
        if not mine:
            await interaction.response.send_message(
                "You have no open sub requests to manage.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Pick a request to manage:", view=ManagePickView(mine), ephemeral=True)


class TakeSpotButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:take:(?P<rid>[0-9a-f]+)"):
    def __init__(self, rid: str, index: int | None = None, req: dict | None = None):
        self.rid = rid
        if req is not None:
            opn = store.open_spots(req)
            label = f"{index}. {first_name(req['requester_name'])} · {fmt_when_short(req['game_ts'])} ({len(req['filled'])}/{req['spots_needed']})"
            style = discord.ButtonStyle.success if opn > 0 else discord.ButtonStyle.secondary
        else:
            label, style = "Take a spot", discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label[:80], style=style, custom_id=f"sub:take:{rid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        await cog.handle_take(interaction, self.rid)


# ── Shared selects for the league/game flows ────────────────────────────────

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
        super().__init__(placeholder="Choose a league…", min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        self.view.league_id = self.values[0]
        self.view.on_league_change()
        await self.view.refresh(interaction)


class TeamSelect(discord.ui.Select):
    def __init__(self, names: list[str], selected, row: int = 1):
        opts = [
            discord.SelectOption(label=_truncate(n, 100), value=_truncate(n, 100), default=(n == selected))
            for n in names[:25]
        ]
        super().__init__(placeholder="Your team…", min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction):
        self.view.team = self.values[0]
        await self.view.refresh(interaction)


class GameSelect(discord.ui.Select):
    def __init__(self, games: list[dict], selected_isos, *, multi: bool, allow_manual: bool, row: int = 2):
        self.multi = multi
        opts = [
            discord.SelectOption(label=_truncate(g["label"], 100), value=g["iso"], default=(g["iso"] in (selected_isos or [])))
            for g in games[:23]
        ]
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
            if names:
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
        if (lg.get("team_names")) and not self.team:
            return False
        return bool(self.game_iso)

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
        lg = f.league()
        title = clean_title(lg.get("title", "")) if lg else ""
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = await cog.add_request(
            requester=interaction.user,
            league_id=f.league_id or "",
            league=title,
            team=f.team or "",
            game_ts=f.game_iso,
            spots=f.spots,
        )
        # Offer to invite an available sub straight away (optional).
        view = InviteView(req["id"], cog.state)
        await interaction.response.edit_message(
            content=(f"✅  Posted: **{title}** · {f.team or '—'} · {fmt_when(f.game_iso)} · needs {f.spots}.\n"
                     "Invite an available sub now (optional), or just leave it on the board:"),
            view=view,
        )


# ── Available-to-sub flow (ephemeral, league → games) ───────────────────────

class AvailFlowView(discord.ui.View):
    def __init__(self, leagues: list[dict], user_id: int):
        super().__init__(timeout=300)
        self.leagues = leagues
        self.user_id = user_id
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
        self.add_item(LeagueSelect(self.leagues, self.league_id, row=0))
        lg = self.league()
        if lg:
            games = league_games(lg, club_now())
            if games:
                self.add_item(GameSelect(games, self.game_isos, multi=True, allow_manual=False, row=1))
            self.add_item(PostAvailButton(row=2))
            self.add_item(RemoveAvailButton(row=2))
        return self

    def prompt(self) -> str:
        lg = self.league()
        if not lg:
            return "**I can sub** — pick the league you can sub in:"
        s = f"League: **{clean_title(lg.get('title', ''))}**"
        if self.game_isos:
            s += " · games: " + ", ".join(fmt_when(g) for g in self.game_isos)
        return ("**I can sub** — " + s +
                "\nPick the games you can cover (or none for any), then **Post availability**.")

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.prompt(), view=self.build())


class PostAvailButton(discord.ui.Button):
    def __init__(self, row: int = 2):
        super().__init__(label="Post availability", emoji="🙋", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        view: AvailFlowView = self.view
        lg = view.league()
        if not lg:
            await interaction.response.defer()
            return
        title = clean_title(lg.get("title", ""))
        cog: "Subs" = interaction.client.get_cog("Subs")
        await cog.add_availability(user=interaction.user, league_id=view.league_id, league=title, games=view.game_isos)
        gtxt = ", ".join(fmt_when(g) for g in view.game_isos) if view.game_isos else "any game"
        await interaction.response.edit_message(
            content=f"✅  Listed you as available for **{title}** · {gtxt}.", view=None)


class RemoveAvailButton(discord.ui.Button):
    def __init__(self, row: int = 2):
        super().__init__(label="Remove my availability", emoji="🗑️", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        view: AvailFlowView = self.view
        if not view.league_id:
            await interaction.response.defer()
            return
        cog: "Subs" = interaction.client.get_cog("Subs")
        removed = await cog.remove_availability(interaction.user.id, view.league_id)
        msg = ("Removed your availability for this league." if removed
               else "You had no availability listed for this league.")
        await interaction.response.edit_message(content=msg, view=None)


# ── Invite an available sub (requester picks → DM confirmation) ─────────────

def _availability_for_request(state: dict, req: dict | None) -> list[dict]:
    """Availability entries relevant to a request: same league first, else all."""
    if not req:
        return []
    lid = str(req.get("league_id") or "")
    same = [a for a in state.get("availability", []) if a.get("league_id", "") == lid]
    pool = same if same else state.get("availability", [])
    return [a for a in pool if not store.is_involved(req, a["user_id"])]


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
            if a.get("league"):
                desc = f"{a['league']} · {desc}"
            opts.append(discord.SelectOption(
                label=_truncate(a["name"], 100), value=str(a["user_id"]), description=_truncate(desc, 100)))
        disabled = not opts
        if not opts:
            opts = [discord.SelectOption(label="No available subs to invite", value="__none__")]
        super().__init__(placeholder="Invite an available sub…", min_values=1, max_values=1,
                         options=opts, disabled=disabled, row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        uid = int(self.values[0])
        name = self.names.get(uid, "a sub")
        cog: "Subs" = interaction.client.get_cog("Subs")
        result = await cog.invite(self.rid, uid, name, inviter=interaction.user)
        msgs = {
            "invited": f"📨  Invited **{name}** — they'll get a DM to confirm. Pending until they accept.",
            "already": f"**{name}** is already on this request.",
            "full": "No open spots left to invite into.",
            "closed": "That request is no longer available.",
        }
        view = InviteView(self.rid, cog.state) if result in ("invited", "already") else None
        await interaction.response.edit_message(content=msgs.get(result, "Done."), view=view)


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


# ── Manage flow (ephemeral, requester-only) ─────────────────────────────────

class ManagePickView(discord.ui.View):
    def __init__(self, requests: list[dict]):
        super().__init__(timeout=180)
        self.add_item(ManagePickSelect(requests))


class ManagePickSelect(discord.ui.Select):
    def __init__(self, requests: list[dict]):
        options = [
            discord.SelectOption(
                label=fmt_when(r["game_ts"])[:100],
                value=r["id"],
                description=(f"{store.open_spots(r)} open · filled: " +
                             (", ".join(f["name"] for f in r["filled"]) or "nobody"))[:100],
            )
            for r in requests[:25]
        ]
        super().__init__(placeholder="Choose a request…", options=options, min_values=1, max_values=1)

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
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.rid)
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.response.edit_message(content="That request is no longer available.", view=None)
            return
        await cog.close_request(self.rid)
        await interaction.response.edit_message(content="✅  Request closed and removed from the board.", view=None)


class AddSubSelect(discord.ui.UserSelect):
    def __init__(self, rid: str):
        self.rid = rid
        super().__init__(placeholder="Add or remove someone as a sub…", min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        cog: "Subs" = interaction.client.get_cog("Subs")
        req = store.find_request(cog.state, self.rid)
        if not req or req["requester_id"] != interaction.user.id:
            await interaction.response.edit_message(content="That request is no longer available.", view=None)
            return
        member = self.values[0]
        result = await cog.requester_toggle_sub(req, member)
        if result == "added":
            note = f"✅  Added {member.display_name} as a sub."
        elif result == "removed":
            note = f"➖  Removed {member.display_name} from the spots."
        else:  # full
            note = f"⚠️  No open spots — {member.display_name} not added."
        await interaction.response.edit_message(
            content=f"{note}\nManaging **{fmt_when(req['game_ts'])}** ({store.open_spots(req)} open):",
            view=ManageActionView(self.rid, cog.state),
        )


# ── The cog ─────────────────────────────────────────────────────────────────

class Subs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = store.load(STORE_PATH)
        self._lock = __import__("asyncio").Lock()

    # -- lifecycle ----------------------------------------------------------
    async def cog_load(self):
        self.expiry_loop.start()

    async def cog_unload(self):
        self.expiry_loop.cancel()

    async def startup(self):
        """Prune and re-render the board after a (re)connect."""
        async with self._lock:
            store.expire(self.state, club_now(), GRACE_HOURS)
            store.save(STORE_PATH, self.state)
        await self.render_board()

    # -- persistence + board refresh ----------------------------------------
    def _save(self):
        store.save(STORE_PATH, self.state)

    async def render_board(self):
        board = self.state.get("board")
        if not board:
            return
        channel = self.bot.get_channel(board["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(board["channel_id"])
            except discord.HTTPException:
                return
        try:
            msg = await channel.fetch_message(board["message_id"])
        except discord.NotFound:
            self.state["board"] = None
            self._save()
            return
        except discord.HTTPException:
            return
        await msg.edit(embed=build_embed(self.state), view=build_view(self.state))

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
        async with self._lock:
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
        return req

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
        if interaction.user.id != uid:
            await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
            return
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                await interaction.response.edit_message(content="This request is no longer open.", view=None)
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
            await interaction.response.edit_message(content="This invite is no longer valid.", view=None)
            return
        await self.render_board()
        if result == "confirmed":
            await interaction.response.edit_message(content=f"✅  You're in for **{when}** — thanks for subbing!", view=None)
            await self.notify(requester_id,
                              f"🥌 {interaction.user.display_name} confirmed as a sub for your **{when}** game "
                              f"({opn} still open).")
        else:  # declined
            await interaction.response.edit_message(content=f"👍  Thanks for letting us know — declined **{when}**.", view=None)
            await self.notify(requester_id,
                              f"🥌 {interaction.user.display_name} can't sub for your **{when}** game "
                              f"({opn} open again).")

    async def _refresh_clicked(self, interaction: discord.Interaction, note: str | None = None):
        """Refresh the board the click came from (pinned or a personal /subs copy)
        in place. `note` is sent as a quiet ephemeral nudge."""
        try:
            await interaction.response.edit_message(embed=build_embed(self.state), view=build_view(self.state))
            if note:
                await interaction.followup.send(note, ephemeral=True)
        except discord.HTTPException:
            if note:
                try:
                    await interaction.response.send_message(note, ephemeral=True)
                except discord.HTTPException:
                    pass

    async def handle_take(self, interaction: discord.Interaction, rid: str):
        async with self._lock:
            req = store.find_request(self.state, rid)
            if req is None:
                await self._refresh_clicked(interaction, note="That request just closed.")
                return
            result = store.toggle_spot(req, interaction.user.id, interaction.user.display_name, now=club_now())
            requester_id = req["requester_id"]
            when = fmt_when(req["game_ts"])
            opn = store.open_spots(req)
            self._save()

        if result == "full":
            await self._refresh_clicked(interaction, note="All spots are filled for that game.")
            await self.render_board()
            return

        # Update the board they clicked (their private copy or the pinned one)…
        await self._refresh_clicked(interaction)
        # …and keep the shared pinned board in sync.
        await self.render_board()

        if requester_id != interaction.user.id:
            verb = "took" if result == "added" else "dropped"
            await self.notify(
                requester_id,
                f"🥌 {interaction.user.display_name} {verb} a sub spot for your **{when}** game "
                f"({opn} still open).")

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

    async def close_request(self, rid: str):
        async with self._lock:
            store.close_request(self.state, rid)
            self._save()
        await self.render_board()

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
        async with self._lock:
            old = self.state.get("board")
            msg = await interaction.channel.send(embed=build_embed(self.state), view=build_view(self.state))
            pinned, pin_err = True, None
            try:
                await msg.pin()
            except discord.Forbidden:
                pinned = False
                pin_err = "I need the **Manage Messages** permission in this channel to pin."
            except discord.HTTPException as ex:
                pinned = False
                pin_err = f"pinning failed (`{ex}`)."
            self.state["board"] = {"channel_id": interaction.channel_id, "message_id": msg.id}
            self._save()

        # Remove the previous board message (if any) so there's only ever one.
        if old and old.get("message_id") != msg.id:
            try:
                ch = self.bot.get_channel(old["channel_id"]) or await self.bot.fetch_channel(old["channel_id"])
                prev = await ch.fetch_message(old["message_id"])
                await prev.delete()  # also unpins it
            except discord.HTTPException:
                pass

        if pinned:
            await interaction.response.send_message("📌  Shared subs board posted and pinned here.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"✅  Shared subs board posted — but I couldn't pin it: {pin_err}\n"
                "Grant the permission, then run `/subsboard` again to pin.",
                ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Subs(bot))
