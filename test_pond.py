"""Unit tests for reserved ice: feed parsing, the coverage check, and alerting.

Run:  python3 test_pond.py

The case these are built around is the one that reached members: the facility
ends a weekly series (UNTIL) at the season rollover and re-creates it on a
DIFFERENT calendar, so a day the club still has ice quietly vanishes from
/sheets. Nothing was broken — the bot was reading one of several calendars — so
the tests that matter here are about noticing, not about parsing.

Also exercises the wiring, not just the pure functions: a check that never gets
called from the scheduled job is a check that doesn't exist.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("POND_ICS_URLS", "https://example.invalid/a.ics")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import alerts                      # noqa: E402
import pond_check as pc            # noqa: E402
import pond_ice                    # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


def check_true(name, got):
    if not got:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: truthy")


def ics(*vevents: str) -> str:
    return "BEGIN:VCALENDAR\n" + "\n".join(vevents) + "\nEND:VCALENDAR\n"


def vevent(summary, start, end, rrule="", exdate=""):
    lines = [
        "BEGIN:VEVENT",
        f"SUMMARY:{summary}",
        f"DTSTART;TZID=America/Chicago:{start}",
        f"DTEND;TZID=America/Chicago:{end}",
    ]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if exdate:
        lines.append(f"EXDATE;TZID=America/Chicago:{exdate}")
    lines.append("END:VEVENT")
    return "\n".join(lines)


# The two shapes of the same Saturday slot across a season rollover: a summer
# series that stops at the end of August, and a fall series that picks it up in
# September at a slightly different hour.
SUMMER_SAT = vevent("Curling (Summer Hours)", "20260606T133000", "20260606T154500",
                    "FREQ=WEEKLY;WKST=SU;UNTIL=20260901T045959Z;BYDAY=SA")
FALL_SAT = vevent("Curling (Fall Hours)", "20260912T141500", "20260912T163000",
                  "FREQ=WEEKLY;WKST=SU;UNTIL=20270601T045959Z;BYDAY=SA")
FALL_THU = vevent("Curling (Fall Hours)", "20260903T123000", "20260903T144500",
                  "FREQ=WEEKLY;WKST=SU;UNTIL=20270601T045959Z;BYDAY=TH")

NOW = datetime(2026, 9, 9, 12, 0)          # a Wednesday, mid-rollover
BACK, AHEAD = NOW - timedelta(weeks=4), NOW + timedelta(weeks=4)


# ── 1. Parsing across the rollover ───────────────────────────────────────────

past_sat = pond_ice.reserved_curling_sessions([ics(SUMMER_SAT)], BACK, NOW)
check("expired series stops at its UNTIL",
      max(o["start"] for o in past_sat), datetime(2026, 8, 29, 13, 30))
check_true("expired series contributes nothing ahead",
           not pond_ice.reserved_curling_sessions([ics(SUMMER_SAT)], NOW, AHEAD))

fall_sat = pond_ice.reserved_curling_sessions([ics(FALL_SAT)], NOW, AHEAD)
check("new series starts on its first date",
      min(o["start"] for o in fall_sat), datetime(2026, 9, 12, 14, 15))
check("new series keeps its duration",
      fall_sat[0]["end"] - fall_sat[0]["start"], timedelta(hours=2, minutes=15))

# An EXDATE for one holiday must not read as "the weekday is gone".
holiday = vevent("Curling (Fall Hours)", "20260903T123000", "20260903T144500",
                 "FREQ=WEEKLY;WKST=SU;UNTIL=20270601T045959Z;BYDAY=TH",
                 exdate="20260917T123000")
thu = pond_ice.reserved_curling_sessions([ics(holiday)], NOW, AHEAD)
check_true("EXDATE removes only its own week",
           datetime(2026, 9, 17, 12, 30) not in [o["start"] for o in thu] and len(thu) >= 2)

check("feed_label strips the calendar domain",
      pond_ice.feed_label(
          "https://calendar.google.com/calendar/ical/abc123%40group.calendar.google.com"
          "/public/basic.ics"), "abc123")
check("is_tentative flags a hold", pond_ice.is_tentative("CURLING Tentative Hold"), True)
check("is_tentative leaves a confirmed block alone",
      pond_ice.is_tentative("Curling (Fall Hours 2026-2027)"), False)


# ── 2. The coverage check ────────────────────────────────────────────────────

def occ(feed, *starts):
    return [{"start": s, "end": s + timedelta(hours=2), "title": "Curling", "feed": feed}
            for s in starts]


FEED_A = {"url": "a", "label": "a", "text": "x", "ok": True}
FEED_B = {"url": "b", "label": "b", "text": "x", "ok": True}

# The incident: Saturdays for the last month, none ahead — while other days
# carry on, so no feed is empty and nothing else looks wrong.
sat_past = occ("a", datetime(2026, 8, 22, 13, 30), datetime(2026, 8, 29, 13, 30))
thu_both = occ("a", datetime(2026, 9, 3, 12, 30)), occ("a", datetime(2026, 9, 10, 12, 30))
found = pc.findings([FEED_A], sat_past + list(thu_both[0]), list(thu_both[1]))
check("a vanished weekday is reported", [f["key"] for f in found], ["weekday_gone:5"])
check_true("and it says which day", "Saturday" in found[0]["text"])
check_true("and when it last ran", "Aug 29" in found[0]["text"])

# The same block moving to another calendar is normal operation, not a problem.
moved = pc.findings([FEED_A, FEED_B], sat_past,
                    occ("b", datetime(2026, 9, 12, 14, 15)))
check("a block moving between feeds is silent", moved, [])

# A feed that won't load is worth saying, even when the ice looks fine.
dead = dict(FEED_B, ok=False, text=None)
found = pc.findings([FEED_A, dead], sat_past, occ("a", datetime(2026, 9, 12, 14, 15)))
check("an unreadable feed is reported", [f["key"] for f in found], ["feed_unreadable:b"])
stale = pc.findings([dict(dead, text="old")], sat_past,
                    occ("a", datetime(2026, 9, 12, 14, 15)))
check_true("and says when it's serving a stale copy",
           "last copy" in stale[0]["text"])

check("no ice at all is its own finding",
      [f["key"] for f in pc.findings([FEED_A], sat_past, [])], ["no_ice"])
check("nothing wrong reports nothing",
      pc.findings([FEED_A], sat_past, occ("a", datetime(2026, 9, 12, 14, 15))), [])

hold = occ("a", datetime(2026, 9, 12, 14, 15))
hold[0]["title"] = "Curling Temp Hold"
check("a provisional hold offered as free ice is flagged",
      [f["key"] for f in pc.findings([FEED_A], sat_past, hold)],
      ["tentative:20260912T1415"])

# The same slot listed on two calendars is one problem, not two.
dupe = hold + [dict(hold[0], feed="b")]
check("duplicate findings collapse by key",
      len(pc.findings([FEED_A, FEED_B], sat_past, dupe)), 1)

text = pc.alert_text(pc.findings([FEED_A], sat_past, list(thu_both[1])))
check_true("the alert names the day", "Saturday" in text)
check_true("the alert says what to do", "POND_ICS_URLS" in text)

report = pc.report([FEED_A], sat_past, occ("a", datetime(2026, 9, 12, 14, 15)))
check_true("the printed report lists each feed", "a: 1 block(s) ahead" in report)
check_true("the printed report shows coverage", "past [Sat]" in report)


# ── 3. Saying it once ────────────────────────────────────────────────────────

f1 = {"key": "weekday_gone:5", "text": "no Saturday"}
f2 = {"key": "feed_unreadable:b", "text": "feed down"}
t0 = datetime(2026, 9, 9, 4, 30)

send, resolved, state = alerts.due(alerts.empty_state(), [f1], t0)
check("a new finding sends", [f["key"] for f in send], ["weekday_gone:5"])
check("and is remembered", list(state["open"]), ["weekday_gone:5"])

send, resolved, state2 = alerts.due(state, [f1], t0 + timedelta(days=1))
check("the same finding tomorrow is silent", send, [])
check("and doesn't re-stamp itself", state2["open"], state["open"])

send, _, _ = alerts.due(state, [f1], t0 + timedelta(days=8))
check("a week later it says so again", [f["key"] for f in send], ["weekday_gone:5"])

send, resolved, state3 = alerts.due(state, [], t0 + timedelta(days=1))
check("a cleared finding is reported once", resolved, ["weekday_gone:5"])
check("and then forgotten", state3["open"], {})

send, _, _ = alerts.due(state, [f1, f2], t0 + timedelta(days=1))
check("a NEW problem alongside an old one still sends",
      [f["key"] for f in send], ["feed_unreadable:b"])

send, _, _ = alerts.due({"open": {"weekday_gone:5": "not-a-date"}}, [f1], t0)
check("an unreadable timestamp errs towards sending",
      [f["key"] for f in send], ["weekday_gone:5"])

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "alert_state.json")
    check("a missing state file is empty, not an error", alerts.load(path), {"open": {}})
    alerts.save({"open": {"k": "2026-09-09T04:30:00"}}, path)
    check("state round-trips", alerts.load(path)["open"]["k"], "2026-09-09T04:30:00")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    check("a corrupt state file doesn't stop the run", alerts.load(path), {"open": {}})
    saved_channel = os.environ.pop("ALERT_CHANNEL_ID", None)
    try:
        check_true("no channel configured means nothing is sent",
                   asyncio.run(alerts.send("hi")) is False)
    finally:
        if saved_channel is not None:
            os.environ["ALERT_CHANNEL_ID"] = saved_channel


# ── 4. Wiring: the scheduled job actually runs the check ─────────────────────

import refresh_leagues as job              # noqa: E402


class Recorder:
    def __init__(self):
        self.sent = []
        self.saved = []

    async def send(self, text):
        self.sent.append(text)
        return True

    def save(self, state, *a, **k):
        self.saved.append(state)


def run_job(*, leagues, collect_result, send_ok=True, no_alert=False):
    rec = Recorder()

    async def fake_leagues(*a, **k):
        if isinstance(leagues, Exception):
            raise leagues
        return leagues

    async def fake_collect(*a, **k):
        return collect_result

    async def fake_send(text):
        rec.sent.append(text)
        return send_ok

    job.get_cached_leagues = fake_leagues
    job.pond_check.collect = fake_collect
    job.alerts.send = fake_send
    job.alerts.load = lambda *a, **k: alerts.empty_state()
    job.alerts.save = rec.save

    args = job.main.__globals__["argparse"].Namespace(
        weeks_ahead=4, weeks_back=4, no_alert=no_alert)
    code = asyncio.run(job.amain(args))
    return code, rec


LIVE_LEAGUE = [{"day": "Thursday", "teams": 8, "ended": False, "next_draw": None}]
GOOD = ([FEED_A], sat_past, occ("a", datetime(2026, 9, 12, 14, 15)))
BAD = ([FEED_A], sat_past, list(thu_both[1]))          # Saturday has vanished

code, rec = run_job(leagues=LIVE_LEAGUE, collect_result=GOOD)
check("a healthy run sends nothing", rec.sent, [])
check("and exits clean", code, 0)

code, rec = run_job(leagues=LIVE_LEAGUE, collect_result=BAD)
check("the job alerts on what the check found", len(rec.sent), 1)
check_true("with the finding in it", "Saturday" in rec.sent[0])
check("and records that it did", len(rec.saved), 1)

code, rec = run_job(leagues=LIVE_LEAGUE, collect_result=BAD, send_ok=False)
check_true("a failed send is not recorded as sent", rec.saved == [])

code, rec = run_job(leagues=LIVE_LEAGUE, collect_result=BAD, no_alert=True)
check("--no-alert messages nobody", rec.sent, [])
check("--no-alert records nothing either", rec.saved, [])

code, rec = run_job(leagues=RuntimeError("site down"), collect_result=GOOD)
check("a failed league refresh alerts too", len(rec.sent), 1)
check_true("and doesn't leak the error text", "site down" not in rec.sent[0])

code, rec = run_job(leagues=[], collect_result=GOOD)
check_true("no active leagues is worth a word", "league" in rec.sent[0].lower())


# ── Report ───────────────────────────────────────────────────────────────────

if FAILS:
    print(f"\n❌  {len(FAILS)} failure(s):\n")
    for f in FAILS:
        print(" - " + f)
    raise SystemExit(1)
print("✅  all reserved-ice + alerting checks passed")
