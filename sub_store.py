"""
Sub-request storage and domain logic — pure, no Discord / network deps so it's
unit-testable (mirrors practice_ice.py).

Two small lists, persisted to one JSON file:

  requests      — "I need a sub" posts. Each has a game date/time, how many
                  spots are needed, and who has filled them. A request auto-
                  expires once its game time has passed (plus a grace window).
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
        "game_ts": "2026-06-20T19:30:00",   # ISO, club-local naive
        "position": str,                       # e.g. "Lead", "Vice" (optional)
        "notes": str,                          # free text (optional)
        "spots_needed": int,
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


# ── Persistence ─────────────────────────────────────────────────────────────

def empty_state() -> dict:
    return {"board": None, "requests": [], "availability": []}


def load(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state()
    # Tolerate older/partial files.
    state.setdefault("board", None)
    state.setdefault("requests", [])
    state.setdefault("availability", [])
    for r in state["requests"]:  # tolerate stores written before these fields existed
        r.setdefault("filled", [])
        r.setdefault("pending", [])
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
    game_ts: str,
    spots_needed: int,
    league_id="",
    league: str = "",
    team: str = "",
    position: str = "",
    notes: str = "",
    kind: str = "sub",
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now()
    req = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "requester_id": requester_id,
        "requester_name": requester_name,
        "league_id": str(league_id) if league_id not in (None, "") else "",
        "league": league.strip(),
        "team": team.strip(),
        "game_ts": game_ts,
        "position": position.strip(),
        "notes": notes.strip(),
        "spots_needed": max(1, int(spots_needed)),
        "filled": [],
        "pending": [],
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


# ── Invitations (requester invites an available sub; they confirm via DM) ────

def invite_sub(req: dict, user_id: int, name: str, now: Optional[datetime] = None) -> str:
    """Reserve a spot for an invited sub, pending their confirmation.
    Returns "invited" | "already" (filled or already pending) | "full"."""
    if is_involved(req, user_id):
        return "already"
    if open_spots(req) <= 0:
        return "full"
    req.setdefault("pending", []).append(
        {"user_id": user_id, "name": name, "ts": _now_iso(now or datetime.now())})
    return "invited"


def confirm_sub(req: dict, user_id: int, name: str, now: Optional[datetime] = None) -> str:
    """Invited sub accepts: move them from pending to filled. Returns "confirmed" | "absent"."""
    if not is_pending_by(req, user_id):
        return "absent"
    req["pending"] = [p for p in req["pending"] if p["user_id"] != user_id]
    req.setdefault("filled", []).append(
        {"user_id": user_id, "name": name, "ts": _now_iso(now or datetime.now())})
    return "confirmed"


def decline_sub(req: dict, user_id: int) -> str:
    """Invited sub declines: free the reserved spot. Returns "declined" | "absent"."""
    if not is_pending_by(req, user_id):
        return "absent"
    req["pending"] = [p for p in req["pending"] if p["user_id"] != user_id]
    return "declined"


def close_request(state: dict, rid: str) -> bool:
    before = len(state["requests"])
    state["requests"] = [r for r in state["requests"] if r["id"] != rid]
    return len(state["requests"]) < before


def requests_sorted(state: dict) -> list[dict]:
    """Open requests, soonest game first; unparseable game_ts sinks to the end."""
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


def availability_for_user(state: dict, user_id: int) -> list[dict]:
    return [a for a in state["availability"] if a["user_id"] == user_id]


# ── Expiry ──────────────────────────────────────────────────────────────────

def expire(
    state: dict,
    now: datetime,
    grace_hours: int = DEFAULT_GRACE_HOURS,
    avail_days: int = DEFAULT_AVAIL_DAYS,
) -> dict:
    """
    Drop played-out requests and stale availability sign-ups. Mutates `state`
    and returns the removed items: {"requests": [...], "availability": [...]}.
    A request whose game_ts can't be parsed is kept (never silently lost).
    """
    cutoff = now - timedelta(hours=grace_hours)
    kept_reqs, dropped_reqs = [], []
    for r in state["requests"]:
        try:
            game = datetime.fromisoformat(r["game_ts"])
        except (ValueError, KeyError):
            kept_reqs.append(r)
            continue
        (dropped_reqs if game < cutoff else kept_reqs).append(r)
    state["requests"] = kept_reqs

    # Availability: if it names specific games, drop it once the LAST game has
    # passed (+grace). Otherwise (whole-league offer) age it out by created date.
    acutoff = now - timedelta(days=avail_days)
    kept_av, dropped_av = [], []
    for a in state["availability"]:
        games = a.get("games") or []
        if games:
            try:
                last = max(datetime.fromisoformat(g) for g in games)
            except (ValueError, TypeError):
                kept_av.append(a)
                continue
            (dropped_av if last < cutoff else kept_av).append(a)
        else:
            try:
                created = datetime.fromisoformat(a["created_ts"])
            except (ValueError, KeyError):
                kept_av.append(a)
                continue
            (dropped_av if created < acutoff else kept_av).append(a)
    state["availability"] = kept_av

    return {"requests": dropped_reqs, "availability": dropped_av}
