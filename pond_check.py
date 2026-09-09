"""
Health check for the rink's reserved-ice feeds — the source /sheets can't see
going wrong.

Reserved ice is the one /sheets source the club doesn't control: the facility
publishes its curling blocks on ITS calendars, and the bot only reads the feed
URLs it was configured with. Two ways that goes quiet, both silent today:

  1. The rink re-creates the weekly series every season rather than extending it
     (each carries an UNTIL), and the new one may land on a DIFFERENT calendar of
     the several its schedule page overlays. Incident 2026-09-09: the Saturday
     block moved to another calendar at the summer→fall rollover, so /sheets
     showed no ice on a Saturday the club actually had.
  2. A feed stops resolving — a calendar is unshared, an id is mistyped, the URL
     rots. `fetch_reserved_curling` degrades quietly by design so /sheets keeps
     working, which is right for the members and invisible to the operator.

So the detector is coverage, not emptiness: a feed can be perfectly healthy and
still have lost the day the club cares about. This compares which WEEKDAYS carry
reserved ice in the recent past against which carry it ahead, and reports a
weekday that has vanished. Comparing whole weekdays across ALL feeds at once is
deliberate: a block MOVING between calendars is normal and must not alert, while
a block disappearing from every calendar is the thing worth waking someone for.
Times are ignored for the same reason — a season that shifts Saturday by an hour
hasn't lost Saturday.

Pure except for `collect`, which is the only part that touches the network.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pond_ice

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday")

# How far back to look for the weekdays the club "normally" has, and how far
# ahead to expect them to continue. Four weeks back is long enough to have seen
# every weekly slot at least once (and to ride out a holiday EXDATE); four ahead
# matches the longest window /sheets will show.
WEEKS_BACK = int(os.environ.get("POND_CHECK_WEEKS_BACK", "4"))
WEEKS_AHEAD = int(os.environ.get("POND_CHECK_WEEKS_AHEAD", "4"))


def now_local() -> datetime:
    """Wall-clock time at the rink, matching what the feeds are parsed into."""
    return datetime.now(pond_ice.LOCAL_TZ).replace(tzinfo=None)


# ── pure: findings ──────────────────────────────────────────────────────────

def _finding(key: str, text: str) -> dict:
    return {"key": key, "text": text}


def findings(feeds: list[dict], past: list[dict], ahead: list[dict],
             weeks_back: int = WEEKS_BACK, weeks_ahead: int = WEEKS_AHEAD) -> list[dict]:
    """What's wrong with the reserved-ice picture, worst first.

    `feeds` are the fetch results (pond_ice.fetch_feeds), `past` and `ahead` the
    curling occurrences behind and in front of now. Each finding has a stable
    `key` so the same problem on consecutive runs is recognisably the same one.
    """
    out: list[dict] = []

    for f in feeds:
        if f.get("ok"):
            continue
        stale = " (still using the last copy that worked)" if f.get("text") else ""
        out.append(_finding(
            f"feed_unreadable:{f['label']}",
            f"Couldn't read the rink calendar feed `{f['label']}`{stale}."))

    if not ahead:
        out.append(_finding(
            "no_ice",
            f"No reserved curling ice at all in the next {weeks_ahead} weeks. "
            f"Either the rink hasn't published the new season yet, or none of the "
            f"configured calendars is the one carrying it."))
        return out

    lost = sorted(pond_ice.weekdays_covered(past) - pond_ice.weekdays_covered(ahead))
    for wd in lost:
        last = max((o["start"] for o in past if o["start"].weekday() == wd), default=None)
        when = f" (last one {last:%a %b %-d})" if last else ""
        out.append(_finding(
            f"weekday_gone:{wd}",
            f"No {WEEKDAY_NAMES[wd]} reserved ice in the next {weeks_ahead} weeks, "
            f"though there was some in the last {weeks_back}{when}. At a season "
            f"rollover the rink usually re-creates the series on a different "
            f"calendar — if so, add that feed to POND_ICS_URLS."))

    for o in ahead:
        if pond_ice.is_tentative(o["title"]):
            out.append(_finding(
                f"tentative:{o['start']:%Y%m%dT%H%M}",
                f"{o['start']:%a %b %-d, %-I:%M%p} is a provisional hold "
                f"(“{o['title']}”) but /sheets is offering it as free ice."))

    # The same block can appear on two calendars; one problem, one finding.
    seen, unique = set(), []
    for f in out:
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        unique.append(f)
    return unique


def report(feeds: list[dict], past: list[dict], ahead: list[dict],
           weeks_back: int = WEEKS_BACK, weeks_ahead: int = WEEKS_AHEAD) -> str:
    """The full picture, for a human reading the run output."""
    lines = [f"Reserved ice — {len(feeds)} feed(s), "
             f"{weeks_back}w back / {weeks_ahead}w ahead:"]
    for f in feeds:
        mine = [o for o in ahead if o["feed"] == f["label"]]
        state = "" if f["ok"] else "  ⚠ UNREADABLE"
        lines.append(f"  • {f['label']}: {len(mine)} block(s) ahead{state}")
        for slot in sorted({(o["start"].weekday(), o["start"].strftime("%-I:%M%p"))
                            for o in mine}):
            n = sum(1 for o in mine
                    if o["start"].weekday() == slot[0]
                    and o["start"].strftime("%-I:%M%p") == slot[1])
            lines.append(f"      {WEEKDAY_NAMES[slot[0]][:3]} {slot[1]} ×{n}")

    def days(occ):
        return " ".join(WEEKDAY_NAMES[d][:3]
                        for d in sorted(pond_ice.weekdays_covered(occ))) or "none"

    lines.append(f"  weekdays covered: past [{days(past)}] → ahead [{days(ahead)}]")
    return "\n".join(lines)


def alert_text(found: list[dict]) -> str:
    """The Discord message for a set of findings."""
    head = "⚠️ **Reserved ice check**" if len(found) == 1 else \
           f"⚠️ **Reserved ice check** — {len(found)} things to look at"
    return "\n".join([head] + [f"• {f['text']}" for f in found])


# ── I/O ─────────────────────────────────────────────────────────────────────

async def collect(urls, weeks_back: int = WEEKS_BACK, weeks_ahead: int = WEEKS_AHEAD,
                  match: str = "curl", ttl: int = 0):
    """Fetch every feed (bypassing the cache by default) and expand it twice:
    the recent past and the window ahead. Returns (feeds, past, ahead); each
    occurrence carries the `feed` label it came from."""
    feeds = await pond_ice.fetch_feeds(urls, ttl=ttl, force=True)
    now = now_local()

    def expand(start, end):
        rows = []
        for f in feeds:
            if not f["text"]:
                continue
            for o in pond_ice.reserved_curling_sessions([f["text"]], start, end, match):
                rows.append({**o, "feed": f["label"]})
        rows.sort(key=lambda o: o["start"])
        return rows

    past = expand(now - timedelta(weeks=weeks_back), now)
    ahead = expand(now, now + timedelta(weeks=weeks_ahead))
    return feeds, past, ahead
