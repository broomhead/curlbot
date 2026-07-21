#!/usr/bin/env python3
"""
Seed dummy practice sign-ups + streaks for testing the board and /sheets stats.

Writes fake data to the practice store (PRACTICE_STORE_PATH). It is OPT-IN and is
never run by the bot itself — run it as a one-off, then restart the bot so it
picks up the file:

    docker compose -f docker-compose.prod.yml -p curlbot run --rm curlbot python seed_practice.py
    docker compose -f docker-compose.prod.yml -p curlbot restart

Creates:
  • Two upcoming practice slots — one with 2 sign-ups, one with 1.
  • A third upcoming slot with nobody signed up (the public board only lists slots
    that have sign-ups, so this one stays off the board until someone joins).
  • Streak history so the leaderboard has something to show: a 3-week, a 2-week,
    and a 1-week active streak.

Safety: refuses to overwrite a non-empty store unless SEED_FORCE=1, so you can't
wipe real sign-ups by accident. Point PRACTICE_STORE_PATH at a test file/volume.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import practice_store as ps

PATH = os.environ.get("PRACTICE_STORE_PATH", "practice_signups.json")

# Fake, clearly-labelled users (IDs in a high range so they can't collide with real
# Discord snowflakes in use). These aren't real accounts — DMs/mentions won't work,
# but the board and streak leaderboard render fine.
ANN = (900000000000000001, "Ann (test)")
BO  = (900000000000000002, "Bo (test)")
CY  = (900000000000000003, "Cy (test)")


def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _slot(dt: datetime, users, sheets: int) -> dict:
    return {
        "when_ts": dt.isoformat(),
        "label": f"{dt.strftime('%a %b %-d')} · {dt.strftime('%-I:%M %p')}",
        "sheets": sheets,
        "users": [{"user_id": uid, "name": name, "ts": dt.isoformat()} for uid, name in users],
    }


def build_state(now: datetime) -> dict:
    day1 = (now + timedelta(days=2)).replace(hour=19, minute=45, second=0, microsecond=0)
    day2 = (now + timedelta(days=4)).replace(hour=19, minute=45, second=0, microsecond=0)
    day3 = (now + timedelta(days=6)).replace(hour=19, minute=45, second=0, microsecond=0)

    def wk(back: int) -> str:
        return _iso_week(now - timedelta(weeks=back))

    return {
        "board": None,
        "sessions": {
            day1.strftime("%Y%m%dT%H%M"): _slot(day1, [ANN, BO], sheets=2),  # 2 signed up
            day2.strftime("%Y%m%dT%H%M"): _slot(day2, [CY], sheets=1),       # 1 signed up
            day3.strftime("%Y%m%dT%H%M"): _slot(day3, [], sheets=3),         # empty slot
        },
        # Attendance = weeks whose practice has already PASSED (streaks count only
        # those). The upcoming sign-ups above don't count until they happen.
        "attendance": {
            str(ANN[0]): {"name": ANN[1], "weeks": [wk(3), wk(2), wk(1)]},   # 3-week streak
            str(BO[0]):  {"name": BO[1],  "weeks": [wk(2), wk(1)]},          # 2-week streak
            str(CY[0]):  {"name": CY[1],  "weeks": [wk(1)]},                 # 1-week streak
        },
    }


def main() -> int:
    existing = ps.load(PATH)
    has_data = bool(existing.get("sessions") or existing.get("attendance"))
    if has_data and os.environ.get("SEED_FORCE") != "1":
        print(f"⚠️  {PATH} already has data — refusing to overwrite.\n"
              f"    Set SEED_FORCE=1 to overwrite, or point PRACTICE_STORE_PATH at a test file.",
              file=sys.stderr)
        return 1

    state = build_state(datetime.now())
    ps.save(PATH, state)
    n_users = sum(len(s["users"]) for s in state["sessions"].values())
    print(f"✅  Seeded {len(state['sessions'])} practice slots ({n_users} sign-ups) and "
          f"{len(state['attendance'])} streaks to {PATH}.")
    print("    Restart the bot to load it: docker compose -p curlbot restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
