"""
Sub-request storage and domain logic — pure, no Discord / network deps so it's
unit-testable (mirrors practice_ice.py).

Two small lists, persisted to one JSON file:

  requests      — "I need a sub" posts: a game date/time, how many spots are
                  needed, and who has filled them. A request auto-expires once
                  its game time has passed (plus a grace window).
                  `game_ts` may be "" on records created while the UI briefly
                  allowed dateless requests; those are still rendered and age out
                  `undated_days` after posting. Nothing creates them any more.
  availability  — "I can sub" sign-ups: members offering to fill in. These age
                  out after a fixed number of days.

The store is deliberately generic: every request carries a `kind` field (default
"sub") so the same board machinery can later back pickup games, team-building,
etc. without a schema change.

State shape:
  {
    "board": {"channel_id": int, "message_id": int} | null,
    "requests": [
      {
        "id": "a1b2c3d4",
        "kind": "sub",
        "requester_id": int,
        "requester_name": str,
        "game_ts": "2026-06-20T19:30:00",   # ISO, club-local naive; "" = date TBD
        "position": str,                       # e.g. "Lead", "Vice" (optional)
        "notes": str,                          # free text (optional)
        "spots_needed": int,
        "series_id": "9f8e7d6c",              # "" = standalone; shared by a run
        "filled": [{"user_id": int, "name": str, "ts": "..."}],
        "created_ts": "..."
      }, ...
    ],
    "availability": [
      {"user_id": int, "name": str, "note": str, "created_ts": "..."}, ...
    ]
  }
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

# How long after game start a request lingers before it's pruned. Covers a
# typical draw so a still-running game's card doesn't vanish mid-game.
DEFAULT_GRACE_HOURS = 3
# How long an "I can sub" sign-up stays on the board before aging out.
DEFAULT_AVAIL_DAYS = 14
# How long a legacy request with NO game date lingers. A dated request expires off
# the back of its game; an undated one has nothing to expire against, so it would
# sit on the board forever without this. (The UI no longer creates these.)
DEFAULT_UNDATED_DAYS = 14


# ── Persistence ─────────────────────────────────────────────────────────────

def empty_state() -> dict:
    # `boards` maps a Discord guild id (str) -> that server's board pointer
    # {"channel_id", "message_id"}. Requests and availability are GLOBAL (shared by
    # every server the bot is in); each server just renders its own board of the
    # same shared data. See new_request for the per-request origin guild/channel.
    return {"boards": {}, "requests": [], "availability": []}


def load(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state()
    # Tolerate older/partial files.
    state.setdefault("boards", {})
    # Migrate the old single-server pointer: we can't key the legacy board by guild
    # (it only stored a channel_id), so drop it. Each server posts a fresh board on
    # its next action; the stale legacy message, if any, is just left in place.
    state.pop("board", None)
    state.setdefault("requests", [])
    state.setdefault("availability", [])
    for r in state["requests"]:  # tolerate stores written before these fields existed
        r.setdefault("filled", [])
        r.setdefault("pending", [])
        r.setdefault("alert", {"channel_id": None, "message_id": None})
        r.setdefault("reminded", False)
        r.setdefault("series_id", "")     # "" = standalone (pre-series stores)
        r.setdefault("guild_id", None)    # server the request was posted from
        r.setdefault("channel_id", None)  # channel it was posted in (for alerts)
    return state


def save(path: str, state: dict) -> None:
    """Atomic write so a crash mid-save can't corrupt the store."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()


def find_request(state: dict, rid: str) -> Optional[dict]:
    return next((r for r in state["requests"] if r["id"] == rid), None)


def open_spots(req: dict) -> int:
    """Spots not yet filled. Pending (invited, unconfirmed) subs reserve a spot."""
    return max(0, int(req["spots_needed"]) - len(req.get("filled", [])) - len(req.get("pending", [])))


def is_filled_by(req: dict, user_id: int) -> bool:
    return any(f["user_id"] == user_id for f in req.get("filled", []))


def is_pending_by(req: dict, user_id: int) -> bool:
    return any(p["user_id"] == user_id for p in req.get("pending", []))


def is_involved(req: dict, user_id: int) -> bool:
    return is_filled_by(req, user_id) or is_pending_by(req, user_id)


# ── Requests ────────────────────────────────────────────────────────────────

def new_request(
    state: dict,
    *,
    requester_id: int,
    requester_name: str,
    spots_needed: int,
    game_ts: str = "",
    league_id="",
    league: str = "",
    team: str = "",
    position: str = "",
    notes: str = "",
    kind: str = "sub",
    series_id: str = "",
    guild_id=None,
    channel_id=None,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now()
    req = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "requester_id": requester_id,
        "requester_name": requester_name,
        # Origin server + channel: alerts/reminders for this request go here, so a
        # request posted on one server pings in that server even though the
        # data is shared.
        "guild_id": int(guild_id) if guild_id is not None else None,
        "channel_id": int(channel_id) if channel_id is not None else None,
        "league_id": str(league_id) if league_id not in (None, "") else "",
        "league": league.strip(),
        "team": team.strip(),
        "game_ts": (game_ts or "").strip(),
        "position": position.strip(),
        "notes": notes.strip(),
        "spots_needed": max(1, int(spots_needed)),
        # A long-term arrangement ("Ben subs Tuesdays for 8 weeks") is stored as one
        # ordinary request PER NIGHT sharing a series_id — never as one multi-date
        # record. Every night then keeps its own status, claim button, lock, expiry
        # and roster, so dropping week 3 touches nothing else. The id exists only so
        # the run can be alerted once and claimed in one tap.
        "series_id": str(series_id or ""),
        "filled": [],
        "pending": [],
        # The live "sub needed" alert message we posted for this request (so we can
        # edit/replace/retire it), and whether the pre-game reminder has fired.
        "alert": {"channel_id": None, "message_id": None},
        "reminded": False,
        "created_ts": _now_iso(now),
    }
    state["requests"].append(req)
    return req


def toggle_spot(req: dict, user_id: int, name: str, now: Optional[datetime] = None) -> str:
    """
    Self-service take/leave. Returns one of:
      "removed" — caller was filling a spot and is now out
      "added"   — caller took an open spot
      "full"    — no open spots and caller wasn't already in
    """
    if is_filled_by(req, user_id):
        req["filled"] = [f for f in req["filled"] if f["user_id"] != user_id]
        return "removed"
    if open_spots(req) <= 0:
        return "full"
    req["filled"].append({"user_id": user_id, "name": name, "ts": _now_iso(now or datetime.now())})
    return "added"


def add_sub(req: dict, user_id: int, name: str, now: Optional[datetime] = None) -> str:
    """Requester adds a named person. Returns "added" | "already" | "full"."""
    if is_filled_by(req, user_id):
        return "already"
    if open_spots(req) <= 0:
        return "full"
    req["filled"].append({"user_id": user_id, "name": name, "ts": _now_iso(now or datetime.now())})
    return "added"


def remove_sub(req: dict, user_id: int) -> str:
    """Requester removes a named person (filled or pending). Returns "removed" | "absent"."""
    if is_filled_by(req, user_id):
        req["filled"] = [f for f in req["filled"] if f["user_id"] != user_id]
        return "removed"
    if is_pending_by(req, user_id):
        req["pending"] = [p for p in req["pending"] if p["user_id"] != user_id]
        return "removed"
    return "absent"


def covered(req: dict) -> int:
    """Spots already accounted for — filled plus invited-but-unconfirmed."""
    return len(req.get("filled", [])) + len(req.get("pending", []))


def set_spots(req: dict, spots: int) -> str:
    """Change how many subs a live request needs.

    The common case is a team that asked for one, found one, and then lost two more
    players: they need the SAME request to say 3, not a second request competing with
    it for the same night. Returns:
      "ok"        — spots_needed changed
      "unchanged" — already that many
      "too_low"   — fewer than the people already on it; nobody is silently bumped,
                    the caller removes a sub first.
    """
    n = int(spots)
    if n < 1:
        return "too_low"
    if n < covered(req):
        return "too_low"
    if n == int(req["spots_needed"]):
        return "unchanged"
    req["spots_needed"] = n
    return "ok"


def series_requests(state: dict, series_id: str) -> list[dict]:
    """Every live request in a run, soonest first. Empty series_id matches nothing —
    standalone requests all carry "" and are not a series."""
    sid = str(series_id or "")
    if not sid:
        return []
    return [r for r in requests_sorted(state) if str(r.get("series_id") or "") == sid]


def close_request(state: dict, rid: str) -> bool:
    before = len(state["requests"])
    state["requests"] = [r for r in state["requests"] if r["id"] != rid]
    return len(state["requests"]) < before


def requests_sorted(state: dict) -> list[dict]:
    """Open requests, soonest game first; undated (and unparseable) sink to the end."""
    def key(r):
        try:
            return (0, datetime.fromisoformat(r["game_ts"]))
        except (ValueError, KeyError):
            return (1, datetime.max)
    return sorted(state["requests"], key=key)


# ── Availability ────────────────────────────────────────────────────────────
# An availability entry is "I can sub in <league>, for these games". Keyed by
# (user_id, league_id) so one person can offer in several leagues. `games` is a
# list of ISO datetimes (the draws they can cover); empty = the whole league.

def find_availability(state: dict, user_id: int, league_id) -> Optional[dict]:
    league_id = str(league_id) if league_id not in (None, "") else ""
    return next(
        (a for a in state["availability"]
         if a["user_id"] == user_id and a.get("league_id", "") == league_id),
        None,
    )


def upsert_availability(
    state: dict,
    *,
    user_id: int,
    name: str,
    league_id="",
    league: str = "",
    games: Optional[list[str]] = None,
    note: str = "",
    now: Optional[datetime] = None,
) -> str:
    """Add or update this user's offer to sub in a league. Returns "added"|"updated"."""
    payload = {
        "user_id": user_id,
        "name": name,
        "league_id": str(league_id) if league_id not in (None, "") else "",
        "league": league.strip(),
        "games": list(games or []),
        "note": note.strip(),
        "created_ts": _now_iso(now or datetime.now()),
    }
    existing = find_availability(state, user_id, league_id)
    if existing:
        existing.update(payload)
        return "updated"
    state["availability"].append(payload)
    return "added"


def remove_availability(state: dict, user_id: int, league_id) -> bool:
    league_id = str(league_id) if league_id not in (None, "") else ""
    before = len(state["availability"])
    state["availability"] = [
        a for a in state["availability"]
        if not (a["user_id"] == user_id and a.get("league_id", "") == league_id)
    ]
    return len(state["availability"]) < before


def _norm_min(iso: str) -> str:
    """ISO timestamp normalized to the minute (for game equality), or the raw value."""
    try:
        return datetime.fromisoformat(iso).replace(second=0, microsecond=0).isoformat()
    except (ValueError, TypeError):
        return iso or ""


def remove_availability_game(state: dict, user_id: int, league_id, game_ts: str) -> bool:
    """Remove a single game from a user's availability for a league (e.g. they dropped
    a spot they'd offered). Deletes the whole entry if no specific games remain. No-op
    for "any game" entries (empty games list). Returns True if something changed."""
    a = find_availability(state, user_id, league_id)
    if not a:
        return False
    games = a.get("games") or []
    if not games:
        return False  # "any game" — nothing game-specific to drop
    target = _norm_min(game_ts)
    kept = [g for g in games if _norm_min(g) != target]
    if len(kept) == len(games):
        return False
    if kept:
        a["games"] = kept
    else:
        state["availability"] = [x for x in state["availability"] if x is not a]
    return True


def availability_for_user(state: dict, user_id: int) -> list[dict]:
    return [a for a in state["availability"] if a["user_id"] == user_id]


# ── Expiry ──────────────────────────────────────────────────────────────────

def day_floor(now: datetime) -> datetime:
    """Midnight at the start of today. The board is today-and-forward, full stop —
    nothing dated before this may survive expiry or be rendered, whatever it is."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def board_cutoff(now: datetime, grace_hours: float = DEFAULT_GRACE_HOURS) -> datetime:
    """The oldest a dated item may be and still belong on the board.

    Two rules, whichever bites harder: a game drops `grace_hours` after it starts
    (so a draw underway is still visible to latecomers), and NOTHING from before
    today survives regardless. The day floor is what stops a late game from
    hanging around after midnight — with grace alone, an 11pm draw was still
    "within 3 hours" at 1am the next morning and stayed on the board."""
    return max(now - timedelta(hours=grace_hours), day_floor(now))


def expire(
    state: dict,
    now: datetime,
    grace_hours: int = DEFAULT_GRACE_HOURS,
    avail_days: int = DEFAULT_AVAIL_DAYS,
    undated_days: int = DEFAULT_UNDATED_DAYS,
) -> dict:
    """
    Drop played-out requests and stale availability sign-ups. Mutates `state`
    and returns the removed items: {"requests": [...], "availability": [...]}.
    A request with no game date ages out `undated_days` after it was posted; one
    whose timestamps can't be parsed at all is kept (never silently lost).
    """
    cutoff = board_cutoff(now, grace_hours)
    undated_cutoff = now - timedelta(days=undated_days)
    kept_reqs, dropped_reqs = [], []
    for r in state["requests"]:
        if not (r.get("game_ts") or "").strip():
            # "Someone needs a sub, date TBD" — no game to expire against, so age
            # it out from when it was posted or it sits on the board forever.
            try:
                created = datetime.fromisoformat(r["created_ts"])
            except (ValueError, KeyError, TypeError):
                kept_reqs.append(r)
                continue
            (dropped_reqs if created < undated_cutoff else kept_reqs).append(r)
            continue
        try:
            game = datetime.fromisoformat(r["game_ts"])
        except (ValueError, KeyError):
            kept_reqs.append(r)
            continue
        (dropped_reqs if game < cutoff else kept_reqs).append(r)
    state["requests"] = kept_reqs

    # Availability: prune the entry's games ONE AT A TIME, then drop the entry once
    # none are left. Expiring only on the LAST game (what this used to do) left every
    # earlier game in the list live: someone free for both Aug 16 and Aug 23 kept
    # putting Aug 16 on the board all the way through the 23rd, because the entry as
    # a whole hadn't expired yet. Each game now stands or falls on its own date.
    # Whole-league offers ("any time") carry no date, so they still age out by
    # created date.
    acutoff = now - timedelta(days=avail_days)
    kept_av, dropped_av, dropped_games = [], [], []
    for a in state["availability"]:
        games = a.get("games") or []
        if games:
            live, stale, unparsed = [], [], []
            for g in games:
                try:
                    dt = datetime.fromisoformat(g)
                except (ValueError, TypeError):
                    unparsed.append(g)   # keep — never silently lose a sign-up
                    continue
                (live if dt >= cutoff else stale).append(g)
            if stale:
                dropped_games.append({"user_id": a.get("user_id"), "name": a.get("name", ""),
                                      "league_id": a.get("league_id", ""), "games": stale})
                a["games"] = live + unparsed
            if not (live or unparsed):
                dropped_av.append(a)     # every game they offered has been played
            else:
                kept_av.append(a)
        else:
            try:
                created = datetime.fromisoformat(a["created_ts"])
            except (ValueError, KeyError):
                kept_av.append(a)
                continue
            (dropped_av if created < acutoff else kept_av).append(a)
    state["availability"] = kept_av

    # `games` lists games pruned from entries that SURVIVED — callers must treat a
    # non-empty value as a change worth saving, or the prune is lost on restart.
    return {"requests": dropped_reqs, "availability": dropped_av, "games": dropped_games}
