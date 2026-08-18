"""
Instructor board cog: posts who is teaching upcoming LTCs into a Discord channel.

A coordinator keeps a Google Sheet of upcoming LTCs, private events and CPATH
events with the instructors signed up for each, then chases people by hand when
an event is short. This does the chasing.

  * The SHEET is the only source of truth. No database, no state file, nothing
    to back up. Every check re-reads it.
  * The board message already in the channel is the record of what was last
    reported, so there is nothing to persist: each check renders the board and
    compares it with the embed already there. Same text means the sheet hasn't
    moved, so we stay quiet. Different means repost, at the bottom of the
    channel where people will see it (the same pattern the practice board uses).
  * `/instructors` forces a post at any time.
  * The whole cog is optional. With no channel or sheet configured,
    `configured()` is False, bot.py never loads it, and nothing else changes.

Scheduling uses discord.py's tasks.loop, same as the subs board's expiry and
reminder loops, so there's one scheduler style in the codebase.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import instructor_board as board
import instructor_sheet

log = logging.getLogger(__name__)

# Channel the board lives in. Right click the channel in Discord with developer
# mode on, Copy Channel ID.
INSTRUCTOR_CHANNEL_ID = int(os.environ.get("INSTRUCTOR_CHANNEL_ID") or 0)
# Club-local check times, 24h, comma separated. Morning and late afternoon by
# default: the sheet gets edited during the day, so a later check catches it.
CHECK_TIMES = os.environ.get("CHECK_TIMES", "09:00,16:00")
TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "-5"))
# How far back to look for our own previous board. Generous: the board only
# reposts when the sheet changes, so it can sit a long way up a busy channel.
HISTORY_LIMIT = 200


def configured() -> bool:
    """Everything the board needs, checked before we try to load it."""
    return bool(INSTRUCTOR_CHANNEL_ID and instructor_sheet.SHEET_ID)


def club_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)


def parse_times(spec: str) -> list[tuple[int, int]]:
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, mm = chunk.split(":")
            out.append((int(hh), int(mm)))
        except ValueError:
            log.warning("Ignoring unparseable CHECK_TIMES entry %r", chunk)
    return sorted(set(out))


async def render() -> tuple[str, str, int]:
    """(board text, one-line summary, embed colour), straight from the sheet.

    The club's date, not UTC's and not the box's: the board groups events by how
    close they are, so which side of midnight we think it is decides what counts
    as urgent."""
    today = club_now().date()
    events = instructor_sheet.parse_events(await instructor_sheet.fetch_csv(), today=today)
    return (board.render(events, today=today),
            board.summary_line(events, today=today),
            board.color(events, today=today))


def build_embed(text: str, colour: int) -> discord.Embed:
    return discord.Embed(title=board.BOARD_TITLE, description=text, color=colour)


class Instructors(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_fired: tuple | None = None

    async def cog_load(self):
        self.check_loop.start()
        log.info("Instructor board: channel %s, checks at %s (club local)",
                 INSTRUCTOR_CHANNEL_ID,
                 ", ".join(f"{h:02d}:{m:02d}" for h, m in parse_times(CHECK_TIMES)) or "never")

    async def cog_unload(self):
        self.check_loop.cancel()

    # -- finding our own board ----------------------------------------------
    async def _find_board(self, channel) -> discord.Message | None:
        """Our most recent board in the channel, or None.

        Matched on author plus the embed title, so a member quoting the board
        back into the channel can't be mistaken for the board itself.
        """
        async for msg in channel.history(limit=HISTORY_LIMIT):
            if msg.author.id != self.bot.user.id or not msg.embeds:
                continue
            if msg.embeds[0].title == board.BOARD_TITLE:
                return msg
        return None

    # -- the one operation --------------------------------------------------
    async def check(self, *, force: bool = False) -> str:
        """Render from the sheet, compare with the board in the channel, and
        repost only if it moved (or if forced)."""
        channel = self.bot.get_channel(INSTRUCTOR_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(INSTRUCTOR_CHANNEL_ID)
            except discord.HTTPException as e:
                log.warning("Instructor channel %s unreachable: %s", INSTRUCTOR_CHANNEL_ID, e)
                return "Could not reach the instructor channel."

        text, summary, colour = await render()
        previous = await self._find_board(channel)
        unchanged = previous is not None and (previous.embeds[0].description or "") == text

        if unchanged and not force:
            log.info("Instructor board unchanged (%s)", summary)
            return f"No change. {summary}"

        # Post first, then delete: if the delete fails the channel keeps a
        # duplicate rather than losing the board entirely.
        await channel.send(embed=build_embed(text, colour))
        if previous is not None:
            try:
                await previous.delete()
            except discord.HTTPException as e:
                log.warning("Could not delete the previous board (%s); leaving it", e)

        why = "forced" if unchanged else "sheet changed"
        log.info("Instructor board posted (%s): %s", why, summary)
        return f"Board posted ({why}). {summary}"

    # -- schedule -----------------------------------------------------------
    @tasks.loop(seconds=30)
    async def check_loop(self):
        """Fire once when the clock passes one of CHECK_TIMES.

        A restart can miss a slot it was down for; the next scheduled check
        picks up whatever changed, and /instructors covers anything urgent.
        """
        now = club_now()
        slot = (now.year, now.month, now.day, now.hour, now.minute)
        if (now.hour, now.minute) not in parse_times(CHECK_TIMES) or slot == self._last_fired:
            return
        self._last_fired = slot
        try:
            await self.check()
        except Exception:                       # noqa: BLE001 - never kill the loop
            log.exception("Scheduled instructor check failed; will retry next slot")

    @check_loop.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()

    # -- /instructors -------------------------------------------------------
    @app_commands.command(
        name="instructors",
        description="Post the instructor board now (who's teaching, what still needs help)")
    async def instructors_cmd(self, interaction: discord.Interaction):
        """Force a fresh board into the instructor channel. The reply is private;
        the board itself goes to the channel it always lives in."""
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.check(force=True)
        except RuntimeError as e:
            # Sheet unreadable (sharing revoked, bad id). Say which, don't dump
            # a traceback at a member.
            await interaction.followup.send(f"Could not read the sheet: {e}", ephemeral=True)
            return
        except Exception:                       # noqa: BLE001
            log.exception("Forced instructor refresh failed")
            await interaction.followup.send(
                "Something went wrong posting the board; try again shortly.", ephemeral=True)
            return
        await interaction.followup.send(result, ephemeral=True)
