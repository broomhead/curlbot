"""Unit tests for the sub-board / streak fixes.

Run:  python3 test_fixes.py
Covers: league labelling, league ordering (day of week then start date), the
game picker (real draws vs projected league nights, finished seasons),
team- and game-optional sub requests, tied streak medals, and
recovering league JSON from a site that injects markup into its API.
"""
from datetime import datetime, timedelta

import os

import discord

# bot.py calls bot.run() at import time; neuter it so we can import the module.
discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import subs
import bot as botmod
import sub_store as store
import league_client as lc

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


# ── 1. League names ──────────────────────────────────────────────────────────
SUNAM = {
    "id": 26095,
    "title": "Sunday Rise &amp; Shine League &#8211; Summer 2026 League 3 &#8211; Begins August 2",
    "day": "Sunday", "time": "9:00 am",
    "draws": [{"date": d, "weekday": "Sunday", "time": "9:00 am"} for d in
              ("2026-08-02", "2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30")],
}
THURS = {
    "id": 26410,
    "title": "Thursday League &#8211; Summer 2026 League 3 &#8211; Begins August 6",
    "day": "Thursday", "time": "7:45 pm",
    "draws": [{"date": d, "weekday": "Thursday", "time": "7:45 pm"} for d in
              ("2026-08-06", "2026-08-13", "2026-08-20", "2026-08-27")],
}
NODRAWS = {"id": 1, "title": "Tuesday Night League – Fall 2026 League 1", "draws": []}
ONEDRAW = {"id": 2, "title": "Learn to Curl League 2", "draws": [{"date": "2026-09-05"}]}
JUNK = {"id": 3, "title": "League 2", "draws": [{"date": "2026-09-05"}]}

check("name/sunam", subs.league_name(SUNAM["title"]), "Sunday Rise & Shine League")
check("name/thurs", subs.league_name(THURS["title"]), "Thursday League")
check("name/nodraws", subs.league_name(NODRAWS["title"]), "Tuesday Night League")
check("name/junk-not-emptied", subs.league_name(JUNK["title"]), "League 2")
check("label/sunam", subs.league_label(SUNAM), "Sunday Rise & Shine League 8/2 – 8/30")
check("label/thurs", subs.league_label(THURS), "Thursday League 8/6 – 8/27")
check("label/nodraws", subs.league_label(NODRAWS), "Tuesday Night League")
check("label/onedraw", subs.league_label(ONEDRAW), "Learn to Curl 9/5")
check("sublabel", subs.league_sub_label(THURS), "Thursday · 7:45 pm")
# Dates in a stored label must survive display (clean_title would have eaten them).
check("stored_league keeps dates", subs.stored_league(subs.league_label(THURS)),
      "Thursday League 8/6 – 8/27")
# Unsorted draws still give the true first/last.
check("label/unordered", subs.league_label({"title": "Friday League 1", "draws": [
    {"date": "2026-08-20"}, {"date": "2026-08-06"}, {"date": "bogus"}]}),
    "Friday 8/6 – 8/20")

# The picker option Discord actually renders — Sunday sorts ahead of Thursday.
sel = subs.LeagueSelect([THURS, SUNAM], selected=None)
check("select labels", [o.label for o in sel.options],
      ["Sunday Rise & Shine League 8/2 – 8/30", "Thursday League 8/6 – 8/27"])
check("select descs", [o.description for o in sel.options],
      ["Sunday · 9:00 am", "Thursday · 7:45 pm"])


# ── 1b. League ordering: day of week Sun→Sat, then start date ────────────────
def lg(name, day, start, *, drawdays=3, weekday_field=True):
    from datetime import timedelta as _td
    d0 = datetime.fromisoformat(start).date()
    return {"id": name, "title": name, "day": (day if weekday_field else ""),
            "draws": [{"date": (d0 + _td(days=7 * i)).isoformat(),
                       "weekday": day} for i in range(drawdays)]}


order = [subs.league_name(l["title"]) for l in sorted([
    lg("Saturday Doubles", "Saturday", "2026-08-01"),
    lg("Sunday Late", "Sunday", "2026-09-06"),      # same day, later start
    lg("Sunday Rise", "Sunday", "2026-08-02"),
    lg("Thursday Night", "Thursday", "2026-08-06"),
    lg("Monday Open", "Monday", "2026-08-03"),
], key=subs.league_sort_key)]
check("sort/day then start", order,
      ["Sunday Rise", "Sunday Late", "Monday Open", "Thursday Night", "Saturday Doubles"])
# Sunday is index 0 and Saturday index 6, whichever way the day is known.
check("sort/weekday index from day", subs.league_weekday_index({"day": "SUNDAY"}), 0)
check("sort/weekday index from draw weekday",
      subs.league_weekday_index({"draws": [{"date": "2026-08-01", "weekday": "Saturday"}]}), 6)
check("sort/weekday index from date alone",
      subs.league_weekday_index({"draws": [{"date": "2026-08-06"}]}), 4)   # a Thursday
check("sort/unknown day sorts last", subs.league_weekday_index({"title": "Mystery"}), 7)
# A league with no draws at all still sorts (no crash) and lands after dated ones.
mystery = {"id": 99, "title": "Mystery League 1", "draws": []}
check("sort/no draws last", [l["title"] for l in sorted(
    [mystery, lg("Sunday Rise", "Sunday", "2026-08-02")], key=subs.league_sort_key)],
    ["Sunday Rise", "Mystery League 1"])


# ── 1c. Game picker: league nights when the schedule isn't posted ────────────
NOW = datetime(2026, 8, 16, 12, 0)          # a Sunday, mid-season

# A posted schedule wins outright — only its real remaining draws, never
# invented dates past the end of it.
opts = subs.game_options(THURS, NOW)
check("games/schedule wins", [o["label"] for o in opts],
      ["Thu Aug 20 · 7:45 PM", "Thu Aug 27 · 7:45 PM"])
check("games/real not flagged", [o["projected"] for o in opts], [False, False])
check("games/strictly increasing", [o["dt"] for o in opts] == sorted(o["dt"] for o in opts), True)
# A league whose draws have all been played is a finished season, not an
# unscheduled one. Old leagues linger in the cache without an `ended` flag, and
# projecting for them would put ghost leagues back in the picker.
check("games/finished season offers nothing", subs.game_options(
    {"day": "Sunday", "time": "9:00 am",
     "draws": [{"date": "2026-07-05", "time": "9:00 am"},
               {"date": "2026-07-26", "time": "9:00 am"}]}, NOW), [])
# ...and it's dropped from the league picker entirely, so you can't walk into
# that dead end. A league with no draws at all is NOT over — that's the
# schedule-not-posted case and it must survive the filter.
JULY = {"day": "Sunday", "time": "9:00 am", "title": "Sunday Rise League 2",
        "draws": [{"date": "2026-07-05", "time": "9:00 am"},
                  {"date": "2026-07-26", "time": "9:00 am"}]}
check("over/finished season", subs.league_is_over(JULY, NOW), True)
check("over/running season", subs.league_is_over(THURS, NOW), False)
check("over/no schedule yet is not over",
      subs.league_is_over({"title": "Fall League 1", "day": "Sunday", "draws": []}, NOW), False)
check("over/last draw today still counts as live",
      subs.league_is_over({"draws": [{"date": NOW.date().isoformat()}]}, NOW), False)

# The reported case: teams AND schedule not set yet — the picker still offers
# dates, and every one of them is the league's own night at its start time.
UNSCHEDULED = {"id": 7, "title": "Sunday Rise & Shine League 4", "day": "Sunday",
               "time": "9:00 am", "draws": []}
opts = subs.game_options(UNSCHEDULED, NOW)
check("games/unscheduled still offers dates", len(opts), subs.PROJECTED_NIGHTS)
check("games/all projected", all(o["projected"] for o in opts), True)
check("games/locked to league night", {o["dt"].strftime("%A") for o in opts}, {"Sunday"})
check("games/locked to league time", {o["dt"].strftime("%H:%M") for o in opts}, {"09:00"})
check("games/first is the next one", opts[0]["label"], "Sun Aug 23 · 9:00 AM")
check("games/runs weeks ahead", opts[-1]["label"], "Sun Oct 11 · 9:00 AM")

# Today's draw is skipped once it has started, but offered before it does.
SUN_TODAY = dict(UNSCHEDULED, time="7:45 pm")
check("games/today still ahead is offered",
      subs.game_options(SUN_TODAY, NOW)[0]["dt"].date(), NOW.date())
check("games/today already started is skipped",
      subs.game_options(SUN_TODAY, datetime(2026, 8, 16, 20, 0))[0]["dt"].date(),
      datetime(2026, 8, 23).date())

# Times parse from the league's own field, or fall back to a known draw.
check("games/time from field", subs._league_time({"time": "7:45 pm"}).strftime("%H:%M"), "19:45")
check("games/noon and midnight", (subs._league_time({"time": "12:00 pm"}).hour,
                                  subs._league_time({"time": "12:30 am"}).hour), (12, 0))
check("games/time falls back to a draw",
      subs._league_time({"draws": [{"date": "2026-08-06", "time": "6:15 pm"}]}).strftime("%H:%M"),
      "18:15")
# No idea when it plays -> no invented dates (better empty than wrong).
check("games/unknown league is empty", subs.game_options({"title": "Mystery", "draws": []}, NOW), [])
# A draw row whose own time is missing/garbled inherits the league's start time —
# midnight would show as "12:00 AM" and expire the request on the wrong day.
check("games/draw without a time", subs.game_options(
    {"day": "Thursday", "time": "7:45 pm", "draws": [{"date": "2026-08-20"}]}, NOW)[0]["label"],
    "Thu Aug 20 · 7:45 PM")

# What Discord renders: projected nights are labelled as such, real ones aren't.
check("games/select marks real draws",
      [o.description for o in subs.GameSelect(subs.game_options(THURS, NOW), [], multi=False).options],
      [None, None])
check("games/select marks projected nights",
      {o.description for o in subs.GameSelect(subs.game_options(UNSCHEDULED, NOW), [], multi=False).options},
      {"not on the schedule yet"})

# ── 2. Sub requests without teams ────────────────────────────────────────────
# Posting without a team is the answer to "the chair hasn't drafted yet", NOT a
# general opt-out. So the option exists ONLY while the league lists no teams: once
# it has them, a teamless record could never be matched against anything, and the
# same spot posted twice would read as two asks and pull in two subs.
ts_empty = subs.TeamSelect([], None)
check("teamselect/no-teams enabled", ts_empty.disabled, False)
check("teamselect/no-teams values", [o.value for o in ts_empty.options], [subs.NO_TEAM])
ts_full = subs.TeamSelect(["Smith", "Alvarez"], None)
check("teamselect/values", [o.value for o in ts_full.options], ["Alvarez", "Smith"])
check("teamselect/no opt-out once teams exist", subs.NO_TEAM in [o.value for o in ts_full.options], False)
check("teamselect/placeholder is not optional once teams exist",
      "optional" in (ts_full.placeholder or "").casefold(), False)

# A league with no teams posted: the flow is ready without one.
flow = subs.NeedSubFlowView([THURS])
check("flow/no league not ready", flow.ready(), False)
flow.league_id = "26410"
flow.game_isos = ["2026-08-20T19:45:00"]
check("flow/teamless IS ready", flow.ready(), True)
check("flow/prompt says not posted", "Team: **not posted yet**" in flow.prompt(), True)

# The same league once its teams are up: the team is now required.
THURS_TEAMED = dict(THURS, team_names=["Smith", "Alvarez"])
tflow = subs.NeedSubFlowView([THURS_TEAMED])
tflow.league_id = "26410"
tflow.game_isos = ["2026-08-20T19:45:00"]
check("flow/teamed not ready without a team", tflow.ready(), False)
check("flow/teamed prompt asks for the team", "Pick your team" in tflow.prompt(), True)
tflow.team = "Smith"
check("flow/teamed ready with a team", tflow.ready(), True)
flow.team = "Smith"
check("flow/with team ready", flow.ready(), True)
# A team select still renders when the league lists no teams.
flow2 = subs.NeedSubFlowView([NODRAWS])
flow2.league_id = "1"
kinds = [type(i).__name__ for i in flow2.build().children]
check("flow/teamselect always present", "TeamSelect" in kinds, True)

# Duplicate guard: same team = dup; teamless dups only against the SAME requester.
# The board is today-forward, so the two render checks below need a game that is
# still in the future WHENEVER the suite runs — a hardcoded 2026 date quietly
# turned these green-then-red as the calendar caught up with it.
# Inside the board's 14-day horizon as well as in the future — the render checks
# below go through build_embed, which now lists only the near term.
GAME = (datetime.now().replace(hour=19, minute=45, second=0, microsecond=0)
        + timedelta(days=7)).isoformat()
LATER = (datetime.now().replace(hour=19, minute=45, second=0, microsecond=0)
         + timedelta(days=21)).isoformat()
st = {"requests": []}
now = datetime(2026, 8, 10, 12, 0)
store.new_request(st, requester_id=1, requester_name="Ann Lee", game_ts=GAME,
                  spots_needed=1, league_id="26410", league="Thursday League 8/6 – 8/27",
                  team="", now=now)
check("dup/other person teamless is not a dup",
      subs._find_open_duplicate(st, "26410", GAME, "", requester_id=2), None)
check("dup/same person teamless is a dup",
      subs._find_open_duplicate(st, "26410", GAME, "", requester_id=1) is not None,
      True)
store.new_request(st, requester_id=2, requester_name="Bob Ray", game_ts=GAME,
                  spots_needed=1, league_id="26410", league="Thursday League 8/6 – 8/27",
                  team="Smith", now=now)
check("dup/same team any person is a dup",
      subs._find_open_duplicate(st, "26410", GAME, "Smith", requester_id=9) is not None,
      True)
check("dup/different game is not a dup",
      subs._find_open_duplicate(st, "26410", LATER, "Smith", requester_id=2), None)

# Rendering a teamless request names the person instead of a team.
teamless, teamed = st["requests"][0], st["requests"][1]
check("render/teamless", subs._req_for(teamless), "Ann's spot")
check("render/teamed", subs._req_for(teamed), "Team Smith")
check("render/status line", subs._req_status_line(teamless),
      f"{subs.INDENT}🔴 Ann's spot — 0/1 · nobody yet")
labels = [c.item.label for c in subs.build_view(st).children
          if isinstance(c, subs.PageClaimButton)]
short = subs.fmt_when_short(GAME)
check("render/button labels", labels, [f"{short} Ann", f"{short} Smith"])
check("render/board embed has both",
      all(x in subs.build_embed(st).description for x in ("Ann's spot", "Team Smith")), True)


# ── 2b. A date is always required ────────────────────────────────────────────
# Reversed after testing: "WHEN you need a sub is a critical detail." The team
# stays optional; the game never is. An unscheduled league gets projected dates
# rather than a dateless request (see 2d).
flow = subs.NeedSubFlowView([THURS])
check("date/needs a league", flow.ready(), False)
flow.league_id = "26410"
check("date/league alone is not enough", flow.ready(), False)
flow.game_isos = ["2026-08-20T19:45:00"]
check("date/league + game is enough", flow.ready(), True)
check("date/team still optional", flow.team, None)
check("date/no opt-out in the picker",
      [o.value for o in subs.GameSelect(subs.game_options(THURS, NOW), [],
                                        multi=False).options],
      [g["iso"] for g in subs.game_options(THURS, NOW)])
check("date/placeholder is not 'optional'",
      subs.GameSelect(subs.game_options(THURS, NOW), [], multi=False).placeholder,
      "Which game…")
# A league with nothing to build a date from says so rather than offering a blank.
check("date/dead-end message",
      subs.GameSelect([], [], multi=False).options[0].label,
      "No dates available for this league")

# Legacy dateless records (posted while it was briefly allowed) still render and
# still age out — they just can't be created any more.
st2 = {"requests": [], "availability": []}
store.new_request(st2, requester_id=1, requester_name="Ann Lee", spots_needed=1,
                  league_id="26410", league="Thursday League 8/6 – 8/27", now=now)
check("date/legacy renders", subs.fmt_when(st2["requests"][0]["game_ts"]), "date TBD")
check("date/legacy grouped on the board",
      "Date TBD · Thursday League 8/6 – 8/27" in subs.build_embed(st2).description, True)
from datetime import timedelta as _td
store.expire(st2, now + _td(days=15), undated_days=14)
check("date/legacy ages out", st2["requests"], [])


# ── 2d. The real unscheduled league: Sunday Night Over/Under ─────────────────
# Fall 2026. No teams, no schedule, and the club hasn't even settled the start
# time ("either 6pm or 7pm" on the league page). The day and the start date are
# knowable, so the picker must still produce real dates.
OU = {"id": 26713, "day": "Sunday", "time": None, "team_names": [], "draws": [],
      "title": "Sunday Night Over/Under &#8211; Fall 2026 League 1 &#8211; Begins September 6"}

check("ou/start date from the title", subs.league_start_date(OU, today=NOW.date()),
      datetime(2026, 9, 6).date())
check("ou/label carries it", subs.league_label(OU), "Sunday Night Over/Under from 9/6")
opts = subs.game_options(OU, NOW)
check("ou/offers dates", len(opts), subs.PROJECTED_NIGHTS)
check("ou/starts at the season start, not today", opts[0]["dt"].date(),
      datetime(2026, 9, 6).date())
check("ou/every one is a Sunday", {o["dt"].strftime("%A") for o in opts}, {"Sunday"})
check("ou/weekly", [(o["dt"].date() - opts[0]["dt"].date()).days for o in opts],
      [0, 7, 14, 21, 28, 35, 42, 49])
check("ou/8 weeks reaches the season end", opts[-1]["dt"].date(),
      datetime(2026, 10, 25).date())
# The time is unknown and we say so rather than borrowing 9am off the Sunday
# MORNING league — that would be flat wrong for a Sunday night league.
check("ou/time flagged unknown", {o["time_known"] for o in opts}, {False})
check("ou/label says so", opts[0]["label"], "Sun Sep 6 · time TBC")
check("ou/short label too", subs.fmt_when_short(opts[0]["iso"]), "Sun 9/6 TBC")
# TIME_TBC parks the draw at the end of its day: the request survives the whole
# day it's needed instead of expiring at midnight, and never locks early.
check("ou/parked at end of day", opts[0]["dt"].time(), subs.TIME_TBC)
check("ou/not locked days before", subs.is_locked({"game_ts": opts[0]["iso"]},
                                                  now=datetime(2026, 9, 6, 12, 0)), False)
check("ou/survives its own draw day",
      store.expire({"requests": [{"id": "x", "game_ts": opts[0]["iso"],
                                  "created_ts": now.isoformat()}], "availability": []},
                   datetime(2026, 9, 6, 20, 0))["requests"], [])
# A known start time is still used verbatim — TBC is only for genuinely unknown.
OU_TIMED = dict(OU, time="6:00 pm")
timed = subs.game_options(OU_TIMED, NOW)
check("ou/known time used", timed[0]["label"], "Sun Sep 6 · 6:00 PM")
check("ou/known time flagged", timed[0]["time_known"], True)
# Once the schedule IS posted, the real draws take over completely.
OU_SCHEDULED = dict(OU, draws=[{"date": "2026-09-06", "weekday": "Sunday", "time": "6:00 pm"},
                               {"date": "2026-09-13", "weekday": "Sunday", "time": "6:00 pm"}])
check("ou/schedule takes over",
      [o["label"] for o in subs.game_options(OU_SCHEDULED, NOW)],
      ["Sun Sep 6 · 6:00 PM", "Sun Sep 13 · 6:00 PM"])

# A game's timestamp is written once, when it's posted, so a date picked while the
# start time was unknown stays parked at TIME_TBC for good — refreshing the league
# cache feeds the PICKER and never reaches what's already on the board. Re-timing
# walks the store instead, once the league page finally states a time.
_tbc_iso = opts[0]["iso"]                       # Sun Sep 6, parked at 23:59
_st = {"requests": [{"id": "r1", "league_id": "26713", "game_ts": _tbc_iso},
                    {"id": "r2", "league_id": "26713", "game_ts": "2026-09-13T18:00:00"},
                    {"id": "r3", "league_id": "999", "game_ts": _tbc_iso}],
       "availability": [{"user_id": 1, "league_id": "26713",
                         "games": [_tbc_iso, "2026-09-13T18:00:00"]}]}
_times = subs.league_times([dict(OU, time="7:00 pm"), {"id": 999, "time": None, "draws": []}])
check("retime/only leagues we can put a clock on", _times, {"26713": subs.clock_time(19, 0)})
_done = subs.retime_tbc(_st, _times)
check("retime/board finally shows the time", subs.fmt_when(_st["requests"][0]["game_ts"]),
      "Sun Sep 6 · 7:00 PM")
check("retime/the date never moves",
      datetime.fromisoformat(_st["requests"][0]["game_ts"]).date(), datetime(2026, 9, 6).date())
check("retime/a real time is never overwritten", _st["requests"][1]["game_ts"],
      "2026-09-13T18:00:00")
check("retime/a league with no time is left alone", _st["requests"][2]["game_ts"], _tbc_iso)
check("retime/availability too", _st["availability"][0]["games"][0], "2026-09-06T19:00:00")
check("retime/and only its TBC game", _st["availability"][0]["games"][1],
      "2026-09-13T18:00:00")
# The changed requests come back whole: their alert pages still say "time TBC"
# and the caller has to redraw them.
check("retime/reports what to redraw", [r["id"] for r in _done["requests"]], ["r1"])
check("retime/reports availability as well", len(_done["availability"]), 1)
check("retime/a second pass is a no-op", subs.retime_tbc(_st, _times),
      {"requests": [], "availability": []})
check("retime/nothing to go on is a no-op",
      subs.retime_tbc({"requests": [{"id": "r", "league_id": "26713", "game_ts": _tbc_iso}],
                       "availability": []}, {}),
      {"requests": [], "availability": []})

# Seasons that are over, for expiry. A league we could not read is absent from the
# list entirely and so is never in this set — that is the whole point of it.
check("dead/ended flag counts",
      subs.dead_league_ids([{"id": 1, "ended": True, "draws": []}], NOW), {"1"})
check("dead/a played-out season counts",
      subs.dead_league_ids([{"id": 2, "draws": [{"date": "2026-08-10"}]}], NOW), {"2"})
check("dead/a running season does not",
      subs.dead_league_ids([{"id": 3, "draws": [{"date": "2026-09-30"}]}], NOW), set())
check("dead/no schedule yet is not a finished season",
      subs.dead_league_ids([{"id": 4, "draws": []}], NOW), set())

# Fall leagues (no draws yet) now sort into their night by start date instead of
# piling up at the end of the list.
FALL = [
    {"title": "Tuesday League – Fall 2026 – Begins September 1", "day": "Tuesday", "draws": []},
    {"title": "Sunday Night Over/Under – Fall 2026 League 1 – Begins September 6",
     "day": "Sunday", "draws": []},
    {"title": "Sunday Rise and Shine League – Fall 2026 League 1 – Begins September 6",
     "day": "Sunday", "draws": []},
    {"title": "Friday TGIF – Fall 2026 League 1 – Begins Sept 4", "day": "Friday", "draws": []},
]
check("ou/fall leagues sort by night",
      [subs.league_name(l["title"]) for l in sorted(FALL, key=subs.league_sort_key)],
      ["Sunday Night Over/Under", "Sunday Rise and Shine League",
       "Tuesday League", "Friday TGIF"])
# Day of week is derivable from the start date even with no `day` field at all.
check("ou/weekday from the title alone",
      subs.league_weekday_index({"title": "Mystery League – Begins September 6", "draws": []}),
      0)


# ── 2c. Surviving a site that injects junk into its own API ──────────────────
# 2026-08-16: the club's WordPress began serving SEO-spam anchor tags ahead of
# EVERY response, REST API included, so json.loads died at char 0 with
# "Expecting value: line 1 column 1" and the bot fell back to a stale cache.
SPAM = ('<a style="display:none;" href="https://live-drawhk.vip/">live draw hk</a>\n'
        '<a style="display:none;" href="https://live-drawsdy.vip/">live draw sdy</a>\n')
import json as _json
POSTS = [{"id": 1, "title": {"rendered": "Thursday League"}}]

check("salvage/recovers posts", lc._salvage_json(SPAM + _json.dumps(POSTS)),
      (POSTS, SPAM))
check("salvage/clean body reports no junk", lc._salvage_json(_json.dumps(POSTS)),
      (POSTS, ""))
check("salvage/junk after the json too",
      lc._salvage_json(_json.dumps(POSTS) + SPAM)[0], POSTS)
check("salvage/junk both ends",
      lc._salvage_json(SPAM + _json.dumps(POSTS) + SPAM)[0], POSTS)
check("salvage/object payload",
      lc._salvage_json(SPAM + '{"code":"rest_no_route"}')[0], {"code": "rest_no_route"})
# Bodies with no usable JSON are reported, never guessed at.
check("salvage/empty", lc._salvage_json(""), (None, None))
check("salvage/pure html", lc._salvage_json("<html><body>nope</body></html>"), (None, None))
check("salvage/truncated json", lc._salvage_json(SPAM + '[{"id": 1,'), (None, None))

# When a body really is unusable, the log line names the cause rather than
# leaving you with a bare JSONDecodeError.
check("describe/empty", lc._describe_body("", "application/json"),
      "an empty body (content-type application/json)")
check("describe/cloudflare",
      lc._describe_body("<html><title>Just a moment...</title>", "text/html"),
      "a Cloudflare challenge page")
_html = "<html>Error establishing a database connection</html>"
check("describe/other html quotes the start",
      lc._describe_body(_html, "text/html"),
      f"{len(_html)} bytes of text/html starting {_html!r}")

# ── Start time off the Details prose ─────────────────────────────────────────
# A league whose schedule isn't posted has no draw headings to read a time off.
# The prose states it; without it the sub board can only offer "time TBC".
check("prose/from X to Y",
      lc._prose_start_time("will run for 8 weeks from September 6 through "
                           "October 25 from 7pm to 9:15pm."), "7:00 pm")
# The opening time routinely omits its own am/pm and borrows the closing one.
check("prose/borrowed meridiem",
      lc._prose_start_time("will run for 7 weeks, from September 4 through "
                           "October 16, from 8:30-10:45pm."), "8:30 pm")
check("prose/en dash", lc._prose_start_time("from 8:30\u201310:45pm"), "8:30 pm")
check("prose/dotted meridiem",
      lc._prose_start_time("Sunday mornings, from 9:00 a.m. to 11:15 a.m."), "9:00 am")
# Borrowing only works when the opening time actually precedes the closing one
# on the clock; 11:30 before 1pm is the morning.
check("prose/borrow flips over noon", lc._prose_start_time("from 11:30-1pm"), "11:30 am")
check("prose/noon start", lc._prose_start_time("from 12-2pm"), "12:00 pm")
# A date range is not a time, and a deadline is not a start time — only a "from"
# sitting immediately before a clock time counts.
check("prose/date range alone",
      lc._prose_start_time("Runs from September 6 through October 25."), None)
check("prose/deadline ignored",
      lc._prose_start_time("Registration closes at 5pm on Friday."), None)
check("prose/nothing", lc._prose_start_time(""), None)

_NO_SCHEDULE = """<html><body>
<p>The Sunday Evening League will run for 8 weeks from September 6 through
October 25 from 7pm to 9:15pm.</p>
<h4>Schedule &amp; Scores</h4><p>Schedule Coming Soon!</p>
</body></html>"""
_parsed = lc.parse_league_html(_NO_SCHEDULE)
check("prose/page with no schedule still has a time", _parsed["time"], "7:00 pm")
check("prose/and still no draws", _parsed["draws"], [])

# A posted schedule outranks the prose: the draws are what people actually play,
# and the prose can be a season-opening blurb that was never corrected.
_SCHEDULED = """<html><body>
<p>This league runs from September 4 through October 16, from 8:30-10:45pm.</p>
<h6>September 4, 2026 7:45 pm T1 T2 Sheet A Sheet B is open</h6>
</body></html>"""
check("prose/draws win", lc.parse_league_html(_SCHEDULED)["time"], "7:45 pm")

# A league whose page we couldn't read must NOT get invented league nights —
# "no draws" only means "not scheduled yet" when the fetch actually worked.
check("fetch_failed/no projected nights",
      subs.game_options({"day": "Sunday", "time": "9:00 am", "draws": [],
                         "fetch_failed": True}, NOW), [])
check("fetch_failed/still not 'over' (we don't know)",
      subs.league_is_over({"draws": [], "fetch_failed": True}, NOW), False)

# ── 2e. The stdlib `time` module must stay unshadowed ────────────────────────
# Regression, 2026-08-18: adding `from datetime import ... time ...` for TIME_TBC
# silently replaced the stdlib `time` module that _is_repeat_click uses, so EVERY
# debounced button (take a spot, fill for someone, remove a sub, cancel a
# request) died with "type object 'datetime.time' has no attribute 'monotonic'".
# py_compile and every import-only test passed happily — only clicking caught it.
import time as _stdlib_time
check("clock/subs.time is the stdlib module", subs.time is _stdlib_time, True)
check("clock/TIME_TBC is a datetime.time", isinstance(subs.TIME_TBC, subs.clock_time), True)

# Exercise the debounce itself rather than trusting the identity check alone.
_cd = {}
check("clock/first click passes",
      subs.Subs._is_repeat_click(_cd, ("cancelreq", 1, "abc")), False)
check("clock/immediate repeat is swallowed",
      subs.Subs._is_repeat_click(_cd, ("cancelreq", 1, "abc")), True)
check("clock/a different target is not a repeat",
      subs.Subs._is_repeat_click(_cd, ("cancelreq", 1, "xyz")), False)
check("clock/a different user is not a repeat",
      subs.Subs._is_repeat_click(_cd, ("cancelreq", 2, "abc")), False)

# ── 3. Streak board: one line per streak length, all the names on it ─────────
# Superseded the per-person list (2026-08-20). That version repeated a medal down
# the board — three people tied on the record each got their own 🥇 row — and then
# showed the next pair as a bare "4.", which read as a numbering glitch instead of
# a place. Grouping says the same thing in one line per place.
def rows(*pairs):
    return [{"name": n, "streak": w} for n, w in pairs]


# The reported case: the tied group is one gold line, and the group under it is SILVER.
out = botmod._streak_rows(rows(("Ann", 2), ("Bo", 2), ("Cy", 2), ("Di", 1)), "streak")
check("board/3-way tie is one gold line", out.splitlines(),
      ["🥇  **2 wks** — Ann, Bo, Cy", "🥈  **1 wk** — Di"])
check("board/no bare rank after a tie", "`4.`" in out, False)
# Dense placing: distinct streak lengths, not people, so a 2-way tie is followed by 3rd.
out = botmod._streak_rows(rows(("Ann", 5), ("Bo", 3), ("Cy", 3), ("Di", 2)), "streak")
check("board/tie for 2nd", [l.split()[0] for l in out.splitlines()], ["🥇", "🥈", "🥉"])
# No ties: one name per line, same as it always looked.
out = botmod._streak_rows(rows(("A", 4), ("B", 3), ("C", 2), ("D", 1)), "streak")
check("board/no ties", [l.split()[0] for l in out.splitlines()], ["🥇", "🥈", "🥉", "`4.`"])
# Five groups maximum, with everyone below them accounted for in the tail.
out = botmod._streak_rows(rows(("A", 9), ("B", 8), ("C", 7), ("D", 6), ("E", 2), ("F", 2), ("G", 1)),
                          "streak")
check("board/five groups then a tail", len(out.splitlines()), 6)
check("board/E and F share the fifth line", out.splitlines()[4], "`5.`  **2 wks** — E, F")
check("board/tail counts the rest", out.splitlines()[-1], "…and 1 more with shorter streaks")
# A whole club tied on one week must not blow past the 1024-char embed field cap.
big = rows(*[(f"P{i}", 1) for i in range(30)])
out = botmod._streak_rows(big, "streak")
check("board/one line for one big tie", len(out.splitlines()), 1)
check("board/big tie summarised", "+20 more" in out, True)
check("board/big tie length", len(out) < 1024, True)
check("board/empty", botmod._streak_rows([], "streak"), "—")
check("board/all-time key", botmod._streak_rows([{"name": "A", "best": 1}], "best"),
      "🥇  **1 wk** — A")
# Board placing and the "Nth longest in the club" sign-up line must agree — both
# count distinct streak LENGTHS above you (dense ranking).
lb = rows(("Ann", 2), ("Bo", 2), ("Cy", 1))
places = {l.split("**")[1]: l.split()[0] for l in botmod._streak_rows(lb, "streak").splitlines()}
check("board/Cy's group is silver", places["1 wk"], "🥈")


print("\n".join(f"FAIL: {f}" for f in FAILS) or f"All checks passed.")
raise SystemExit(1 if FAILS else 0)
