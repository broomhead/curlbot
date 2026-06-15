"""
Practice sign-up pool — pure storage + logic (no Discord / network deps).

People sign up to say "I want to practice during this session." There's no hard
cap (a session has a few sheets but plenty of room), so each session is just an
open pool: we show how many sheets are free and how many people have signed up,
and members sort out the details themselves.

Sessions are keyed by their start minute (e.g. "20260616T1945") so the same
practice slot maps to the same pool across queries and restarts. Metadata
(label, sheets) is refreshed whenever the session is seen; the user pool is
preserved. A session ages out once its start time has passed (+grace).

State shape:
  {
    "sessions": {
      "20260616T1945": {
        "when_ts": "2026-06-16T19:45:00",
        "label": "Wed Jun 16 · 7:45 PM",
        "sheets": 2,                      # free sheets when last seen (display only)
        "users": [{"user_id": int, "name": str, "ts": "..."}]
      }, ...
    }
  }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Optional

DEFAULT_GRACE_HOURS = 3


def empty_state() -> dict:
    return {"sessions": {}, "board": None}


def load(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state()
    state.setdefault("sessions", {})
    state.setdefault("board", None)
    for s in state["sessions"].values():
        s.setdefault("users", [])
    return state


def active_sessions(state: dict) -> list[dict]:
    """Sessions sorted by start time, each annotated with its key."""
    out = [{**s, "key": k} for k, s in state["sessions"].items()]
    out.sort(key=lambda s: s.get("when_ts", ""))
    return out


def save(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _now_iso(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()


def register_session(state: dict, key: str, *, when_ts: str, label: str = "", sheets=None) -> dict:
    """Upsert a session's display metadata without touching its user pool."""
    s = state["sessions"].get(key)
    if s is None:
        s = {"when_ts": when_ts, "label": label, "sheets": sheets, "users": []}
        state["sessions"][key] = s
    else:
        s["when_ts"] = when_ts or s.get("when_ts")
        if label:
            s["label"] = label
        if sheets is not None:
            s["sheets"] = sheets
    return s


def is_signed_up(state: dict, key: str, user_id: int) -> bool:
    s = state["sessions"].get(key)
    return bool(s and any(u["user_id"] == user_id for u in s["users"]))


def count(state: dict, key: str) -> int:
    s = state["sessions"].get(key)
    return len(s["users"]) if s else 0


def signups(state: dict, key: str) -> list[dict]:
    s = state["sessions"].get(key)
    return list(s["users"]) if s else []


def toggle(
    state: dict,
    key: str,
    user_id: int,
    name: str,
    *,
    when_ts: str = "",
    label: str = "",
    sheets=None,
    now: Optional[datetime] = None,
) -> str:
    """Join or leave a session's practice pool. Returns "joined" | "left"."""
    s = register_session(state, key, when_ts=when_ts, label=label, sheets=sheets)
    if any(u["user_id"] == user_id for u in s["users"]):
        s["users"] = [u for u in s["users"] if u["user_id"] != user_id]
        return "left"
    s["users"].append({"user_id": user_id, "name": name, "ts": _now_iso(now or datetime.now())})
    return "joined"


def expire(state: dict, now: datetime, grace_hours: int = DEFAULT_GRACE_HOURS) -> list[str]:
    """Drop sessions whose start time has passed (+grace). Returns removed keys."""
    cutoff = now - timedelta(hours=grace_hours)
    dropped = []
    for key, s in list(state["sessions"].items()):
        try:
            when = datetime.fromisoformat(s["when_ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if when < cutoff:
            dropped.append(key)
            del state["sessions"][key]
    return dropped
