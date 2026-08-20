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
from datetime import datetime, date, timedelta
from typing import Optional

DEFAULT_GRACE_HOURS = 3


def empty_state() -> dict:
    # `attendance` is a PERSISTENT per-user record of the ISO weeks a member's
    # practice has been CONFIRMED — i.e. a session they were signed up for that has
    # since passed (weeks are added in expire(), never at sign-up, so future sign-ups
    # don't count yet): {"<user_id>": {"name": str, "weeks": ["2026-W24", ...]}}. It's
    # never pruned when a streak breaks — a gap just ends the current streak — so the
    # full history survives. See current_streak / streak_leaderboard.
    return {"sessions": {}, "board": None, "attendance": {}}


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
    state.setdefault("attendance", {})
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
    """Join or leave a session's practice pool. Returns "joined" | "left". Streak
    attendance is NOT touched here — a week is only counted once its session has
    PASSED (see expire), so a future sign-up doesn't inflate a streak until the
    practice actually happens (and only if the member hasn't left by then)."""
    s = register_session(state, key, when_ts=when_ts, label=label, sheets=sheets)
    if any(u["user_id"] == user_id for u in s["users"]):
        s["users"] = [u for u in s["users"] if u["user_id"] != user_id]
        return "left"
    s["users"].append({"user_id": user_id, "name": name, "ts": _now_iso(now or datetime.now())})
    return "joined"


# ── Weekly practice streaks ──────────────────────────────────────────────────
# A "week" is an ISO week string "GGGG-Www". A user's attendance holds only weeks
# whose practice has already PASSED and that the user was still signed up for (weeks
# are added in expire(), never at sign-up). A streak is the run of consecutive such
# weeks ending at their most recent one; it stays "active" until a completed week
# goes by with no practice (then it's over, but the historical weeks are kept).

def _week_of(ts: str) -> Optional[str]:
    try:
        y, w, _ = datetime.fromisoformat(ts).isocalendar()
    except (ValueError, TypeError):
        return None
    return f"{y:04d}-W{w:02d}"


def _week_monday(iso_week: str) -> Optional[date]:
    try:
        y, w = iso_week.split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, TypeError, AttributeError):
        return None


def _add_week(state: dict, user_id: int, name: str, week: Optional[str]) -> None:
    if not week:
        return
    rec = state.setdefault("attendance", {}).setdefault(str(user_id), {"name": name, "weeks": []})
    if name:
        rec["name"] = name
    if week not in rec["weeks"]:
        rec["weeks"].append(week)
        rec["weeks"].sort()


def current_streak(state: dict, user_id: int, now: datetime) -> int:
    """Consecutive PASSED weeks of practice ending at the user's latest one. Returns 0
    once a completed week has been missed (streak broken). Future sign-ups don't count
    — attendance only holds weeks whose practice has already happened."""
    rec = state.get("attendance", {}).get(str(user_id))
    if not rec or not rec.get("weeks"):
        return 0
    mondays = sorted({m for m in (_week_monday(w) for w in rec["weeks"]) if m})
    if not mondays:
        return 0
    this_week = _week_monday(_week_of(now.isoformat()))
    # Broken if they skipped a whole completed week (latest is >1 week behind now).
    if this_week is not None and mondays[-1] < this_week - timedelta(days=7):
        return 0
    n = 1
    for i in range(len(mondays) - 1, 0, -1):
        if (mondays[i] - mondays[i - 1]).days == 7:
            n += 1
        else:
            break
    return n


def streak_leaderboard(state: dict, now: datetime) -> list[dict]:
    """Members with an active streak, longest first. Each: {user_id, name, streak}."""
    out = []
    for uid_str, rec in state.get("attendance", {}).items():
        try:
            uid = int(uid_str)
        except (ValueError, TypeError):
            continue
        s = current_streak(state, uid, now)
        if s >= 1:
            out.append({"user_id": uid, "name": rec.get("name", ""), "streak": s})
    out.sort(key=lambda x: (-x["streak"], (x["name"] or "").casefold()))
    return out


def best_streak(state: dict, user_id: int) -> int:
    """All-time record: the longest run of consecutive practiced weeks anywhere in the
    user's history (not just the current one). Derived from the kept attendance."""
    rec = state.get("attendance", {}).get(str(user_id))
    if not rec or not rec.get("weeks"):
        return 0
    mondays = sorted({m for m in (_week_monday(w) for w in rec["weeks"]) if m})
    if not mondays:
        return 0
    best = run = 1
    for i in range(1, len(mondays)):
        run = run + 1 if (mondays[i] - mondays[i - 1]).days == 7 else 1
        best = max(best, run)
    return best


def all_time_leaderboard(state: dict) -> list[dict]:
    """Every member's best-ever streak, longest first. Each: {user_id, name, best}.
    Unlike the current leaderboard this includes broken streaks — records stand."""
    out = []
    for uid_str, rec in state.get("attendance", {}).items():
        try:
            uid = int(uid_str)
        except (ValueError, TypeError):
            continue
        b = best_streak(state, uid)
        if b >= 1:
            out.append({"user_id": uid, "name": rec.get("name", ""), "best": b})
    out.sort(key=lambda x: (-x["best"], (x["name"] or "").casefold()))
    return out


def streak_rank(state: dict, user_id: int, now: datetime) -> tuple[int, int, bool]:
    """(rank, total_active, tied) for a user among active streaks; rank 1 = longest.
    rank 0 means no active streak.

    DENSE ranking — the rank counts distinct streak LENGTHS above this one, not
    people. So the group below a three-way tie for first is 2nd, not 4th. This has
    to match how the leaderboard groups its lines (bot._streak_rows), because the
    sign-up ping says "2nd longest in the club" about the very board the member is
    about to look at; counting people made the two disagree."""
    lb = streak_leaderboard(state, now)
    me = next((e for e in lb if e["user_id"] == user_id), None)
    if me is None:
        return (0, len(lb), False)
    higher = len({e["streak"] for e in lb if e["streak"] > me["streak"]})
    tied = sum(1 for e in lb if e["streak"] == me["streak"]) > 1
    return (higher + 1, len(lb), tied)


def expire(state: dict, now: datetime, grace_hours: int = DEFAULT_GRACE_HOURS) -> list[str]:
    """Drop sessions whose start time has passed (+grace), and — since the practice
    has now happened — confirm streak attendance for everyone who was still signed up
    for it (this is the ONLY place a week is added to a streak). Returns removed keys."""
    cutoff = now - timedelta(hours=grace_hours)
    dropped = []
    for key, s in list(state["sessions"].items()):
        try:
            when = datetime.fromisoformat(s["when_ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if when < cutoff:
            week = _week_of(s.get("when_ts", ""))
            if week:
                for u in s.get("users", []):
                    _add_week(state, u["user_id"], u.get("name", ""), week)
            dropped.append(key)
            del state["sessions"][key]
    return dropped
