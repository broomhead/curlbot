"""
Operator alerts — how a background job tells a human something needs attention.

A scheduled job that only writes to its own log tells nobody anything: the
overnight refresh runs unattended, and nothing on this host mails or watches its
output. So findings go to Discord instead, posted to a channel (ALERT_CHANNEL_ID)
where whoever looks after the bot will see them.

Two rules shape this module:

* **Say it once.** A problem that persists — a feed that stays unreadable — must
  not send the same message every single night at 4am, or the alerts get muted
  and the next real one is lost too. Each finding carries a stable key; a key is
  re-sent only after ALERT_REPEAT_DAYS, and when it clears, one short line says
  so. Silence therefore means healthy, which is what makes the noise meaningful.
* **Nothing raw in the message.** Exceptions and request URLs can carry
  credentials (the forms API passes its key in the query string), so alerts are
  built from text this codebase wrote, never from an exception or a URL.

The state functions are pure; only `send` touches the network.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"

STATE_PATH = os.environ.get("ALERT_STATE_PATH", "alert_state.json")
# How long a still-unfixed problem waits before it says so again. Weekly: long
# enough not to be noise, short enough that a forgotten one resurfaces.
REPEAT_DAYS = float(os.environ.get("ALERT_REPEAT_DAYS", "7"))


# ── pure: what to send, and when ────────────────────────────────────────────

def empty_state() -> dict:
    return {"open": {}}


def load(path: str = STATE_PATH) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state()
    state.setdefault("open", {})
    return state


def save(state: dict, path: str = STATE_PATH) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def due(state: dict, found: list[dict], now: datetime,
        repeat_days: float = REPEAT_DAYS) -> tuple[list[dict], list[str], dict]:
    """Split current findings into what to send now and what has cleared.

    Returns (to_send, resolved_keys, new_state). A finding is sent when it's new,
    or when its last send was more than `repeat_days` ago. A key that was open
    and isn't in `found` any more is reported as resolved and forgotten.
    """
    open_keys = dict(state.get("open") or {})
    current = {f["key"]: f for f in found}

    to_send = []
    for key, finding in current.items():
        last = open_keys.get(key)
        if last is not None:
            try:
                if now - datetime.fromisoformat(last) < timedelta(days=repeat_days):
                    continue
            except (ValueError, TypeError):
                pass  # unparseable timestamp: treat as overdue rather than never
        to_send.append(finding)

    resolved = [k for k in open_keys if k not in current]

    new_open = {k: open_keys[k] for k in open_keys if k in current}
    stamp = now.replace(microsecond=0).isoformat()
    for f in to_send:
        new_open[f["key"]] = stamp
    return to_send, resolved, {"open": new_open}


def resolved_text(keys: list[str]) -> str:
    n = len(keys)
    what = "issue" if n == 1 else "issues"
    return f"✅ Reserved ice check: the previously reported {what} ({n}) no longer applies."


# ── I/O ─────────────────────────────────────────────────────────────────────

def configured() -> bool:
    """True if there's a token and a channel to post to."""
    return bool(os.environ.get("DISCORD_TOKEN") and os.environ.get("ALERT_CHANNEL_ID"))


async def send(text: str) -> bool:
    """Post to ALERT_CHANNEL_ID.

    Returns True if Discord accepted the message. Never raises: an alert that
    fails must not take down the refresh job it's reporting on. Posted through
    the REST API rather than the gateway because the caller is a one-shot script,
    not the running bot — no connection to wait for, nothing to log in to.
    """
    token = os.environ.get("DISCORD_TOKEN", "")
    channel_id = os.environ.get("ALERT_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        log.info("No alert channel configured (ALERT_CHANNEL_ID); not sending.")
        return False

    import aiohttp

    # Findings are assembled from our own strings, but a feed title reaches this
    # text, so suppress every mention rather than trust what a calendar says.
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as s:
            async with s.post(f"{API}/channels/{channel_id}/messages", json=payload,
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status not in (200, 201):
                    # 403 here usually means the bot can't see or post in that
                    # channel — a channel-level permission override, not the
                    # server-wide role. 404 means the id is wrong.
                    log.warning("Alert POST failed (HTTP %s)", r.status)
                    return False
    except Exception as ex:  # noqa: BLE001 — alerting is best-effort by design
        log.warning("Alert send failed: %s", type(ex).__name__)
        return False
    return True
