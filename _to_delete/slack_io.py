"""
The only Slack calls this bot makes, and the trick that lets it hold no state.

There is no database and no state file: the board already sitting in the channel
IS the record of what was last reported. Each check renders the board from the
sheet and compares it with the text of the last board we posted. Same text means
nothing changed, so we stay quiet. Different text means the sheet moved, so the
old board is deleted and a fresh one posted at the bottom of the channel, where
people will actually see it (the same pattern curlbot uses for its practice
board).

Needs a bot token, not just an incoming webhook: a webhook can post but cannot
read history or delete, and both are required for the above.

Scopes: chat:write, channels:history (or groups:history for a private channel).
Add commands only if you want the /instructors slash command.
"""

from __future__ import annotations

import logging

from slack_sdk.web.async_client import AsyncWebClient

from instructor_board import BOARD_HEADING

log = logging.getLogger(__name__)

# How far back to look for our own previous board. Generous: the board only
# reposts when the sheet changes, so it can sit a long way up a busy channel.
HISTORY_LIMIT = 200


async def whoami(client: AsyncWebClient) -> str:
    resp = await client.auth_test()
    return resp["user_id"]


async def find_board(client: AsyncWebClient, channel: str, me: str) -> dict | None:
    """Our most recent board message in the channel, or None.

    Identified by author plus the heading line, so a human quoting the board
    back into the channel can't be mistaken for the board itself.
    """
    resp = await client.conversations_history(channel=channel, limit=HISTORY_LIMIT)
    for msg in resp.get("messages", []):        # newest first
        if msg.get("user") != me and msg.get("bot_id") is None:
            continue
        text = msg.get("text", "")
        if text.startswith(BOARD_HEADING):
            return {"ts": msg["ts"], "text": text}
    return None


async def replace_board(client: AsyncWebClient, channel: str, text: str,
                        previous: dict | None) -> str:
    """Post the board, retiring the previous one. Returns the new message ts.

    Post first, then delete: if the delete fails (someone revoked a scope, say)
    the channel is left with a duplicate rather than with nothing.
    """
    resp = await client.chat_postMessage(channel=channel, text=text,
                                         unfurl_links=False, unfurl_media=False)
    if previous:
        try:
            await client.chat_delete(channel=channel, ts=previous["ts"])
        except Exception as e:      # noqa: BLE001 - a stale board is not fatal
            log.warning("Could not delete the previous board (%s); leaving it in place", e)
    return resp["ts"]


async def post_ephemeral(client: AsyncWebClient, channel: str, user: str, text: str) -> None:
    """A reply only the person who ran the command sees."""
    try:
        await client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not send the ephemeral reply: %s", e)
