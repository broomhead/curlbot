"""
Practice sign-up pool — pure storage + logic (no Discord / network deps).

People sign up to say "I want to practice during this session." There's no hard
cap (a session has a few sheets but plenty of room), so each session is just an
open pool: we show how many sheets are free and how many people have signed up,
and members sort out the details themselves.

Sessions are keyed by their start minute (e.g. "20260616T1945") so the same
practice slot maps to the same pool across queries and restarts. Metadata
(label, sheets) is refreshed whenever the session is seen; the user pool is
preserved. A session ages out once its start time has passed (+grace), and the people still
signed up for it at that point have that practice confirmed against their streak.
A confirmed practice stays individually addressable for a couple of weeks, so a
member who didn't actually make it can take it back (see editable_practices).

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
    # don't count yet): {"<user_id>": {"name": str, "weeks": ["2026-W24", ...],
    # "practices": [{"key", "label", "when_ts", "week"}, ...]}}. It's never pruned
    # when a streak breaks — a gap just ends the current streak — so the full history
    # survives. See current_streak / streak_leaderboard.
    #
    # `practices` is the per-session detail behind those weeks. A week on its own
    # can't be un-done: "I wasn't actually there on Tuesday" needs to know which
    # Tuesday. Each confirmed practice is kept so remove_practice can drop one and
    # recompute the week from what's left.
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
    for rec in state["attendance"].values():
        _backfill_markers(rec)
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


def _backfill_markers(rec: dict) -> dict:
    """Give every credited week at least one practice record.

    Weeks credited before practices were kept individually are bare strings with
    nothing behind them — there's no date to take back, so such a week would be
    stuck on a streak forever. A keyless marker stands in for "a practice this
    week, date not recorded": it can't be offered as a date, but the week it
    belongs to can be taken back whole (see editable_practices)."""
    rec.setdefault("weeks", [])
    rec.setdefault("practices", [])
    known = {p.get("week") for p in rec["practices"]}
    for w in rec["weeks"]:
        if w not in known:
            rec["practices"].append({"key": "", "label": "", "when_ts": "", "week": w})
    return rec


def _record(state: dict, user_id: int, name: str) -> dict:
    rec = state.setdefault("attendance", {}).setdefault(
        str(user_id), {"name": name, "weeks": [], "practices": []})
    _backfill_markers(rec)
    if name:
        rec["name"] = name
    return rec


def _confirm_practice(state: dict, user_id: int, name: str, key: str,
                      when_ts: str, label: str) -> None:
    """Count one practice that has now happened: add its ISO week to the user's
    streak and keep the practice itself so it stays individually removable."""
    week = _week_of(when_ts)
    if not week:
        return
    rec = _record(state, user_id, name)
    if week not in rec["weeks"]:
        rec["weeks"].append(week)
        rec["weeks"].sort()
    if not any(p.get("key") == key for p in rec["practices"]):
        rec["practices"].append({"key": key, "label": label,
                                 "when_ts": when_ts, "week": week})
        rec["practices"].sort(key=lambda p: p.get("when_ts", ""))


# ── Taking a practice back ───────────────────────────────────────────────────
# Sign-ups get stale in both directions: someone signs up and can't make it, and
# by the time they know, the slot has often already passed and been counted. The
# edit window is this week plus all of last week — long enough to fix a practice
# that had already started before anyone knew, short enough that the streak board
# isn't rewritable history.

def window_start(now: datetime) -> datetime:
    """Midnight on the Sunday of the week containing seven days ago."""
    d = (now - timedelta(days=7)).date()
    d -= timedelta(days=(d.weekday() + 1) % 7)   # weekday(): Mon=0 … Sun=6
    return datetime(d.year, d.month, d.day)


def _in_window(when_ts: str, floor: datetime) -> bool:
    try:
        return datetime.fromisoformat(when_ts) >= floor
    except (ValueError, TypeError):
        return False


WEEK_KEY_PREFIX = "week:"   # can't collide with a session key (%Y%m%dT%H%M)


def _week_label(iso_week: str) -> str:
    monday = _week_monday(iso_week)
    return (f"Week of {monday.strftime('%b %-d')} — date not recorded"
            if monday else iso_week)


def editable_practices(state: dict, user_id: int, now: datetime) -> list[dict]:
    """Every practice this member can still take back, earliest first. Each:
    {key, label, when_ts, counted, whole_week} — `counted` meaning the practice has
    passed and is already part of their streak, as opposed to a slot still coming
    up; `whole_week` meaning the entry stands for a week with no date on record, so
    taking it back takes the whole week.

    Covers every half of the pool because a member shouldn't have to know which one
    a given date is in: live sessions they're signed up for, practices already
    confirmed against their streak, and weeks credited before the dates behind them
    were kept. A key is only ever in one of them — expire() moves it across."""
    floor = window_start(now)
    out = []
    for key, s in state.get("sessions", {}).items():
        if not any(u["user_id"] == user_id for u in s.get("users", [])):
            continue
        when_ts = s.get("when_ts", "")
        if _in_window(when_ts, floor):
            out.append({"key": key, "label": s.get("label", "") or key,
                        "when_ts": when_ts, "counted": False, "whole_week": False})
    rec = state.get("attendance", {}).get(str(user_id)) or {}
    for p in rec.get("practices", []):
        if p.get("key") and _in_window(p.get("when_ts", ""), floor):
            out.append({"key": p["key"], "label": p.get("label", "") or p["key"],
                        "when_ts": p.get("when_ts", ""), "counted": True,
                        "whole_week": False})
    # A week with no date on record is offered as the week itself. Judged on its
    # MONDAY rather than the Sunday floor: with no date to place inside the week,
    # the only honest reading is "this week and last week", and a week whose Monday
    # is in the window is exactly that.
    seen_weeks = set()
    for p in rec.get("practices", []):
        week = p.get("week")
        if p.get("key") or not week or week in seen_weeks:
            continue
        seen_weeks.add(week)
        monday = _week_monday(week)
        if monday is None or datetime(monday.year, monday.month, monday.day) < floor:
            continue
        out.append({"key": WEEK_KEY_PREFIX + week, "label": _week_label(week),
                    "when_ts": monday.isoformat(), "counted": True,
                    "whole_week": True})
    out.sort(key=lambda p: p["when_ts"])
    return out


def remove_practice(state: dict, user_id: int, key: str, now: datetime) -> Optional[dict]:
    """Take one practice back. Returns what was removed — {key, label, when_ts,
    counted, live, week_lost} — or None if it isn't theirs to remove or has aged
    past the window.

    `live` says the slot is still an open session (so the shared board shows it and
    wants a repaint); `week_lost` says this was their last practice that week, so
    the week has come off their streak."""
    target = next((p for p in editable_practices(state, user_id, now)
                   if p["key"] == key), None)
    if target is None:
        return None
    out = {**target, "live": False, "week_lost": False}
    rec = state.get("attendance", {}).get(str(user_id)) or {}
    if target.get("whole_week"):
        week = key[len(WEEK_KEY_PREFIX):]
        # Only the dateless markers go. A dated practice in the same week is its own
        # entry on the menu and stays until it's removed on its own terms.
        rec["practices"] = [p for p in rec["practices"]
                            if p.get("key") or p.get("week") != week]
        if not any(p.get("week") == week for p in rec["practices"]):
            rec["weeks"] = [w for w in rec.get("weeks", []) if w != week]
            out["week_lost"] = True
        return out
    session = state.get("sessions", {}).get(key)
    if session is not None and not target["counted"]:
        session["users"] = [u for u in session["users"] if u["user_id"] != user_id]
        out["live"] = True
        return out
    gone = next((p for p in rec.get("practices", []) if p.get("key") == key), None)
    if gone is None:
        return None
    rec["practices"] = [p for p in rec["practices"] if p.get("key") != key]
    week = gone.get("week")
    # The week only comes off if nothing else that week is still on record — two
    # practices in a week are one week of streak, and taking one back leaves it.
    if week and not any(p.get("week") == week for p in rec["practices"]):
        rec["weeks"] = [w for w in rec.get("weeks", []) if w != week]
        out["week_lost"] = True
    return out


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
            for u in s.get("users", []):
                _confirm_practice(state, u["user_id"], u.get("name", ""), key,
                                  s.get("when_ts", ""), s.get("label", ""))
            dropped.append(key)
            del state["sessions"][key]
    return dropped
