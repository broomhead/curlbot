"""Unit tests for go-to (standing) subs — the 2026-08-27 work.

  1. A go-to sub is bound to a LEAGUE + TEAM, never a weekday. Whoever sets it up
     picks both from the same pickers everyone else uses, so nothing has to infer
     what "the Thursday league" means, and the binding is dated for free.
  2. When that team needs a sub, the go-to is PUT ON the spot as the request is
     posted — the room is never asked — and DM'd to confirm or drop.
  3. Because a name lands on a game its owner hasn't seen, two things must hold:
     an unconfirmed assignment is visible as unconfirmed, and it gets chased before
     game day.
  4. The teamless escape hatch is now only for leagues whose chair hasn't posted
     teams; the ones already out there get reconciled by DM.

Run:  python3 test_standing.py    (no network; needs discord.py + bs4 + aiohttp + dotenv)

Same harness rules as test_series.py: neuter discord.Client.run (bot.py calls
bot.run() at import time) and point SUBS_STORE_PATH at a scratch file BEFORE
importing subs, because the cog loads and saves its store on construction.
"""
import ast
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

import discord

discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["SUBS_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "subs_store.json")

import subs
import sub_store as store

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


def at(seq, i, name):
    """seq[i], or a recorded FAILURE and None — never an IndexError.

    check() collects failures and prints them at the end, so one traceback mid-suite
    swallows every failure recorded before it. A regression must fail loudly and let
    the rest of the suite run (see feedback-curlbot-testing)."""
    try:
        return seq[i]
    except (IndexError, KeyError, TypeError):
        check(name, f"missing [{i}]", "present")
        return None


# ── Fixtures ────────────────────────────────────────────────────────────────
NOW = subs.club_now()


def night(n_days: int, hour: int = 19, minute: int = 45) -> str:
    return (NOW.replace(hour=hour, minute=minute, second=0, microsecond=0)
            + timedelta(days=n_days)).isoformat()


THU = [night(7 * i + 4) for i in range(6)]

TEAMED = {
    "id": 555, "title": "Thursday League – Fall 2026 League 1 – Begins September 3",
    "day": "Thursday", "time": "7:45 pm",
    "draws": [{"date": iso[:10], "weekday": "Thursday", "time": "7:45 pm"} for iso in THU],
    "team_names": ["Delaney", "Okafor"],
}
UNTEAMED = {
    "id": 556, "title": "Sunday Rise and Shine League – Fall 2026 League 1 – Begins September 6",
    "day": "Sunday", "time": "9:00 am",
    "draws": [{"date": iso[:10], "weekday": "Sunday", "time": "9:00 am"} for iso in THU],
    "team_names": [],
}


class U:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name


class Ch:
    def __init__(self, cid=1):
        self.id = cid
        self.guild = None
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return type("M", (), {"id": 42})()


LISA, WILL, SAM, BRUCE = U(1, "Lisa Chen"), U(2, "Will Grant"), U(3, "Sam Ortiz"), U(4, "Bruce Iyer")


def make_cog():
    """A Subs cog with everything that touches Discord stubbed, recording calls."""
    cog = subs.Subs(object())
    cog.state = store.empty_state()
    cog.calls = []
    cog.dms = []

    async def render_board(gid, fallback_channel=None):
        cog.calls.append(("board",))

    async def bump_board(gid, fallback_channel=None):
        cog.calls.append(("board",))

    async def render_all_boards():
        cog.calls.append(("board",))

    async def post_page(req, *, reason="new", channel=None):
        cog.calls.append(("page", reason, req["id"]))

    async def refresh_page(req):
        cog.calls.append(("refresh", req["id"]))

    async def dm(uid, text, view=None):
        cog.dms.append((uid, text, view))
        return True

    async def dm_requester(uid, text):
        cog.dms.append((uid, text, None))

    cog.render_board = render_board
    cog.bump_board = bump_board
    cog.render_all_boards = render_all_boards
    cog.post_page = post_page
    cog.refresh_page = refresh_page
    cog._dm = dm
    cog._dm_requester = dm_requester
    cog._save = lambda: None
    return cog


def goto(cog, member, team, league=TEAMED):
    """Register a go-to sub straight in the store (the UI path is tested separately)."""
    return store.add_standing(cog.state, user_id=member.id, name=member.display_name,
                              league_id=str(league["id"]), league=subs.league_label(league),
                              team=team, created_by=LISA.id, now=subs.club_now())


async def post(cog, requester, dates, *, team="Delaney", spots=1, league=TEAMED, channel=None):
    return await cog.add_series(
        requester=requester, league_id=str(league["id"]),
        league=subs.league_label(league), team=team, game_isos=dates, spots=spots,
        channel=channel or Ch())


# ── 1. The store: an arrangement, not a sign-up ─────────────────────────────
st = store.empty_state()
check("store/new state has a standing list", st["standing"], [])
check("store/add", store.add_standing(st, user_id=WILL.id, name="Will Grant", league_id="555",
                                      league="Thursday League", team="Delaney"), "added")
check("store/no team is not an arrangement",
      store.add_standing(st, user_id=WILL.id, name="Will Grant", league_id="555",
                         league="Thursday League", team=""), "no_team")
check("store/same person twice",
      store.add_standing(st, user_id=WILL.id, name="Will Grant", league_id="555",
                         league="Thursday League", team="Delaney"), "already")
store.add_standing(st, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                   league="Thursday League", team="Delaney")
check("store/priority is order of arrangement",
      [g["name"] for g in store.standing_for(st, "555", "Delaney")], ["Will Grant", "Sam Ortiz"])
check("store/team match is case and space tolerant",
      [g["name"] for g in store.standing_for(st, "555", "  delaney ")], ["Will Grant", "Sam Ortiz"])
check("store/another team has none", store.standing_for(st, "555", "Okafor"), [])
check("store/a teamless request has none", store.standing_for(st, "555", ""), [])
check("store/wrong league has none", store.standing_for(st, "999", "Delaney"), [])
check("store/remove", store.remove_standing(st, WILL.id, "555", "Delaney"), True)
check("store/remove promotes nobody but leaves order",
      [g["name"] for g in store.standing_for(st, "555", "Delaney")], ["Sam Ortiz"])
check("store/remove twice", store.remove_standing(st, WILL.id, "555", "Delaney"), False)

# THE load-bearing difference from availability: an arrangement never goes stale.
st2 = store.empty_state()
old = subs.club_now() - timedelta(days=90)
store.add_standing(st2, user_id=WILL.id, name="Will Grant", league_id="555",
                   league="Thursday League", team="Delaney", now=old)
store.upsert_availability(st2, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                          league="Thursday League", games=[], now=old)
store.expire(st2, subs.club_now(), 3)
check("store/expire ages out a 90-day-old availability", st2["availability"], [])
check("store/expire NEVER ages out an arrangement", len(st2["standing"]), 1)


# ── 2. Assignment mechanics ─────────────────────────────────────────────────
st3 = store.empty_state()
req = store.new_request(st3, requester_id=BRUCE.id, requester_name="Bruce Iyer",
                        spots_needed=1, game_ts=THU[0], league_id="555",
                        league="Thursday League", team="Delaney")
check("assign/requester can't sub their own game",
      store.assign_auto(req, BRUCE.id, "Bruce Iyer", "aaa"), "requester")
check("assign/ok", store.assign_auto(req, WILL.id, "Will Grant", "aaa"), "assigned")
check("assign/covers the spot", store.open_spots(req), 0)
check("assign/is an ordinary filled entry", [f["user_id"] for f in req["filled"]], [WILL.id])
check("assign/carries the batch id", req["filled"][0]["auto"], "aaa")
check("assign/starts unconfirmed", req["filled"][0]["confirmed"], False)
check("assign/again is a no-op", store.assign_auto(req, WILL.id, "Will Grant", "aaa"), "already")
check("assign/no spots left", store.assign_auto(req, SAM.id, "Sam Ortiz", "aaa"), "full")
check("assign/unconfirmed list", [f["name"] for f in store.unconfirmed_auto(req)], ["Will Grant"])
check("assign/confirm", store.confirm_auto(req, WILL.id), "confirmed")
check("assign/confirm twice", store.confirm_auto(req, WILL.id), "already")
check("assign/nothing left unconfirmed", store.unconfirmed_auto(req), [])
check("assign/drop", store.decline_auto(req, WILL.id), "removed")
check("assign/drop reopens the spot", store.open_spots(req), 1)
check("assign/a drop is remembered for THIS date", req["auto_declined"], [WILL.id])
check("assign/and nothing puts them back on it",
      store.assign_auto(req, WILL.id, "Will Grant", "bbb"), "declined")
check("assign/but the next person still can",
      store.assign_auto(req, SAM.id, "Sam Ortiz", "bbb"), "assigned")
check("assign/drop someone who isn't on it", store.decline_auto(req, LISA.id), "absent")

# A manual (self-serve) fill is NOT an auto assignment and is never chased.
manual = store.new_request(st3, requester_id=BRUCE.id, requester_name="Bruce Iyer",
                           spots_needed=1, game_ts=THU[1], league_id="555", team="Delaney")
store.add_sub(manual, SAM.id, "Sam Ortiz")
check("assign/a hand-raise is not an assignment", store.auto_entries(manual), [])
check("assign/and never chased", store.unconfirmed_auto(manual), [])


# ── 3. Posting: the go-to gets it, the room is never asked ──────────────────
async def the_team_with_a_goto_never_reaches_the_room():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    made, skipped, filled = await post(cog, BRUCE, [THU[0]])
    check("post/one request made", made, 1)
    check("post/and it was filled by the arrangement", filled, 1)
    req = cog.state["requests"][0]
    check("post/nobody is asked to cover it", store.open_spots(req), 0)
    check("post/no alert page at all",
          [c for c in cog.calls if c[0] == "page"], [])
    check("post/the board still gets rendered",
          ("board",) in cog.calls, True)
    check("post/the go-to is DM'd", [d[0] for d in cog.dms], [WILL.id])
    dm = next(iter(cog.dms), None)
    check("post/there is a DM to inspect", dm is not None, True)
    if dm is None:
        return
    check("post/with buttons to answer in one tap", type(dm[2]).__name__, "AutoAssignView")
    check("post/and the buttons are confirm + drop",
          [type(c).__name__ for c in dm[2].children],
          ["ConfirmAutoButton", "DropAutoButton"])


async def a_team_without_one_still_asks_the_room():
    cog = make_cog()
    made, skipped, filled = await post(cog, BRUCE, [THU[0]], team="Okafor")
    check("post/no arrangement means nothing is auto-filled", filled, 0)
    check("post/and the alert goes up as before",
          [c[1] for c in cog.calls if c[0] == "page"], ["new"])
    check("post/nobody is DM'd", cog.dms, [])


async def six_dates_are_one_dm_but_six_assignments():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    made, skipped, filled = await post(cog, BRUCE, THU)
    check("post/every date is its own request", made, 6)
    check("post/every one auto-filled", filled, 6)
    check("post/one DM for the lot", len(cog.dms), 1)
    aids = {f["auto"] for r in cog.state["requests"] for f in r["filled"]}
    check("post/sharing one batch id", len(aids), 1)
    check("post/but six separate records",
          len({r["id"] for r in cog.state["requests"]}), 6)
    check("post/no alerts", [c for c in cog.calls if c[0] == "page"], [])


async def priority_fills_the_second_spot_not_a_spare():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    goto(cog, SAM, "Delaney")
    await post(cog, BRUCE, [THU[0]], spots=2)
    req = cog.state["requests"][0]
    check("priority/both spots go to the two go-tos, in order",
          [f["name"] for f in req["filled"]], ["Will Grant", "Sam Ortiz"])
    check("priority/two people, two DMs", sorted(d[0] for d in cog.dms), [WILL.id, SAM.id])

    cog2 = make_cog()
    goto(cog2, WILL, "Delaney")
    goto(cog2, SAM, "Delaney")
    await post(cog2, BRUCE, [THU[0]], spots=1)
    check("priority/one spot goes to the first only",
          [f["name"] for f in cog2.state["requests"][0]["filled"]], ["Will Grant"])


async def the_requesters_own_arrangement_doesnt_cover_them():
    cog = make_cog()
    goto(cog, BRUCE, "Delaney")          # Bruce is the go-to AND the one who's out
    made, skipped, filled = await post(cog, BRUCE, [THU[0]])
    check("post/you are never assigned to your own ask", filled, 0)
    check("post/so the room is asked",
          [c[1] for c in cog.calls if c[0] == "page"], ["new"])


# ── 4. Dropping and confirming ──────────────────────────────────────────────
async def dropping_one_date_leaves_the_rest():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    await post(cog, BRUCE, THU[:3])
    second = at(cog.state["requests"], 1, "drop/three dates were posted")
    if second is None:
        return
    rid = second["id"]
    cog.calls.clear()
    dropped = await cog.drop_auto(WILL, [rid])
    check("drop/just that date", dropped, [second["game_ts"]])
    check("drop/it reopens", store.open_spots(store.find_request(cog.state, rid)), 1)
    check("drop/and the room is asked for it",
          [c for c in cog.calls if c[0] == "page"], [("page", "bump", rid)])
    others = [r for r in cog.state["requests"] if r["id"] != rid]
    check("drop/the other dates are untouched",
          [store.open_spots(r) for r in others], [0, 0])
    check("drop/the arrangement itself stands",
          len(store.standing_for(cog.state, "555", "Delaney")), 1)
    check("drop/the requester is told", any(d[0] == BRUCE.id for d in cog.dms), True)
    # And a re-post of that same date must not put them back on it.
    again = await cog._auto_assign([store.find_request(cog.state, rid)])
    check("drop/never re-assigned to a date they dropped", again, {})


async def confirming_clears_the_flag():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    await post(cog, BRUCE, THU[:2])
    req = at(cog.state["requests"], 0, "confirm/a request exists")
    entry = at((req or {}).get("filled", []), 0, "confirm/somebody was assigned")
    if entry is None:
        return
    aid = entry["auto"]
    line = subs._req_status_line(req)
    check("confirm/an unconfirmed assignment says so", "(unconfirmed)" in line, True)
    done = await cog.confirm_auto(WILL, aid)
    check("confirm/every date in the batch", len(done), 2)
    check("confirm/the board stops flagging it",
          "(unconfirmed)" in subs._req_status_line(req), False)
    check("confirm/twice is a no-op", await cog.confirm_auto(WILL, aid), [])
    check("confirm/somebody else's batch is not theirs to confirm",
          await cog.confirm_auto(SAM, aid), [])


async def an_unconfirmed_assignment_is_chased_before_game_day():
    cog = make_cog()
    ch = Ch()
    goto(cog, WILL, "Delaney")

    async def resolve(cid):
        return ch
    cog._resolve_channel = resolve
    await post(cog, BRUCE, [night(1)], channel=ch)
    first = at(cog.state["requests"], 0, "chase/the request exists")
    if first is None:
        return
    check("chase/covered on paper", store.open_spots(first), 0)
    await cog._chase_unconfirmed(subs.club_now())
    check("chase/said out loud in the channel", len(ch.sent), 1)
    said = at(ch.sent, 0, "chase/there is a message to inspect")
    check("chase/and it names the person", f"<@{WILL.id}>" in (said[0] if said else ""), True)
    check("chase/the spot is NOT reopened — they're still the sub",
          store.open_spots(first), 0)
    check("chase/they get a nudge too", any(d[0] == WILL.id for d in cog.dms[1:]), True)
    ch.sent.clear()
    await cog._chase_unconfirmed(subs.club_now())
    check("chase/only once per request", ch.sent, [])

    # A confirmed one is never chased.
    cog2 = make_cog()
    ch2 = Ch()

    async def resolve2(cid):
        return ch2
    cog2._resolve_channel = resolve2
    goto(cog2, WILL, "Delaney")
    await post(cog2, BRUCE, [night(1)], channel=ch2)
    req2 = at(cog2.state["requests"], 0, "chase/the second request exists")
    seat = at((req2 or {}).get("filled", []), 0, "chase/somebody was assigned to it")
    if seat is None:
        return
    await cog2.confirm_auto(WILL, seat["auto"])
    await cog2._chase_unconfirmed(subs.club_now())
    check("chase/a confirmed assignment is left alone", ch2.sent, [])


# ── 5. Making and ending an arrangement ─────────────────────────────────────
async def the_reminder_loop_actually_runs_the_chase():
    cog = make_cog()
    ran = []

    async def chase(now):
        ran.append(now)
    cog._chase_unconfirmed = chase
    await subs.Subs.reminder_loop.coro(cog)
    check("chase/the reminder loop is what runs it", len(ran), 1)


async def a_new_arrangement_covers_what_is_already_asking():
    cog = make_cog()
    await post(cog, BRUCE, THU[:2])                 # posted BEFORE Will was set up
    check("standing/those went to the room",
          len([c for c in cog.calls if c[0] == "page"]), 1)
    cog.calls.clear()
    result, isos = await cog.add_standing(
        actor=LISA, member=WILL, league_id="555",
        league=subs.league_label(TEAMED), team="Delaney", channel=Ch())
    check("standing/added", result, "added")
    check("standing/and it swept up the open dates", len(isos), 2)
    check("standing/nothing is left asking",
          [store.open_spots(r) for r in cog.state["requests"]], [0, 0])
    check("standing/Will hears it from the bot, not from the board",
          any("go-to sub" in d[1] for d in cog.dms if d[0] == WILL.id), True)


async def ending_an_arrangement_leaves_the_dates_alone():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    await post(cog, BRUCE, THU[:2])
    removed = await cog.remove_standing(WILL.id, "555", "Delaney", actor=LISA)
    check("standing/removed", removed, True)
    check("standing/the dates he's on stay his",
          [store.open_spots(r) for r in cog.state["requests"]], [0, 0])
    check("standing/he's told", any("ended your go-to" in d[1] for d in cog.dms), True)
    made, skipped, filled = await post(cog, BRUCE, [THU[3]])
    check("standing/but nothing new is assigned", filled, 0)


# ── 6. Teams posted after the fact ──────────────────────────────────────────
async def attaching_a_team_warns_about_the_double_book():
    cog = make_cog()
    # Bruce posted before the chair set teams; Lisa later posts for the same team+draw.
    await post(cog, BRUCE, [THU[0]], team="", league=UNTEAMED)
    await post(cog, LISA, [THU[0]], team="Delaney", league=UNTEAMED)
    rid = cog.state["requests"][0]["id"]
    done, clashes, assigned = await cog.set_team_for(BRUCE, [rid], "Delaney")
    check("reconcile/the team is set", store.find_request(cog.state, rid)["team"], "Delaney")
    check("reconcile/one date done", len(done), 1)
    check("reconcile/and the clash is called out", len(clashes), 1)

    # No clash when nobody else has that team on that draw.
    cog2 = make_cog()
    await post(cog2, BRUCE, [THU[0]], team="", league=UNTEAMED)
    rid2 = cog2.state["requests"][0]["id"]
    done2, clashes2, _ = await cog2.set_team_for(BRUCE, [rid2], "Delaney")
    check("reconcile/no clash, no warning", (len(done2), clashes2), (1, []))

    # Only your own requests.
    cog3 = make_cog()
    await post(cog3, BRUCE, [THU[0]], team="", league=UNTEAMED)
    rid3 = cog3.state["requests"][0]["id"]
    done3, _, _ = await cog3.set_team_for(LISA, [rid3], "Delaney")
    check("reconcile/you can only name the team on your own ask", done3, [])


async def attaching_a_team_hands_it_to_the_goto():
    cog = make_cog()
    await post(cog, BRUCE, [THU[0]], team="", league=UNTEAMED)
    store.add_standing(cog.state, user_id=WILL.id, name="Will Grant",
                       league_id=str(UNTEAMED["id"]), league="Sunday league", team="Delaney")
    rid = cog.state["requests"][0]["id"]
    done, clashes, assigned = await cog.set_team_for(BRUCE, [rid], "Delaney")
    check("reconcile/naming the team lets the go-to pick it up", assigned, 1)
    check("reconcile/covered", store.open_spots(store.find_request(cog.state, rid)), 0)


async def the_nudge_goes_out_once_per_person_per_league():
    cog = make_cog()
    await post(cog, BRUCE, THU[:3], team="", league=UNTEAMED)
    await post(cog, LISA, [THU[0]], team="", league=UNTEAMED)

    async def leagues():
        # The chair has now posted teams for that league.
        return [dict(UNTEAMED, team_names=["Delaney", "Okafor"])]
    cog.get_leagues = leagues
    cog.dms.clear()
    await subs.Subs.team_reconcile_loop.coro(cog)
    check("nudge/one DM per person, not per request", sorted(d[0] for d in cog.dms),
          sorted([BRUCE.id, LISA.id]))
    first = next(iter(cog.dms), None)
    check("nudge/there is a DM to inspect", first is not None, True)
    check("nudge/it carries the button",
          [type(c).__name__ for c in (first[2].children if first else [])], ["SetTeamButton"])
    cog.dms.clear()
    await subs.Subs.team_reconcile_loop.coro(cog)
    check("nudge/never twice", cog.dms, [])

    # A league that still has no teams is left alone.
    cog2 = make_cog()
    await post(cog2, BRUCE, [THU[0]], team="", league=UNTEAMED)

    async def leagues2():
        return [UNTEAMED]
    cog2.get_leagues = leagues2
    cog2.dms.clear()
    await subs.Subs.team_reconcile_loop.coro(cog2)
    check("nudge/nothing to attach yet, nothing said", cog2.dms, [])


# ── 7. The alert tags the go-to first ───────────────────────────────────────
async def the_alert_puts_the_goto_line_first():
    cog = make_cog()
    goto(cog, WILL, "Delaney")
    goto(cog, SAM, "Delaney")
    await post(cog, BRUCE, [THU[0]], spots=1)        # Will takes it, Sam does not
    req = cog.state["requests"][0]
    await cog.drop_auto(WILL, [req["id"]])            # now it's open again
    store.upsert_availability(cog.state, user_id=LISA.id, name="Lisa Chen",
                              league_id="555", league="Thursday League", games=[])
    body = cog._page_body(req, reason="bump")
    lines = [l for l in body.split("\n") if l.startswith("<@")]
    check("alert/two tag lines", len(lines), 2)
    first_line, second_line = (at(lines, 0, "alert/a go-to line") or ""), (at(lines, 1, "alert/an availability line") or "")
    check("alert/the go-to is tagged on the first line",
          first_line.startswith(f"<@{SAM.id}>"), True)
    check("alert/and named as the go-to", "go-to sub for this team" in first_line, True)
    check("alert/general availability comes after", f"<@{LISA.id}>" in second_line, True)
    check("alert/someone who dropped this date is not tagged for it",
          f"<@{WILL.id}>" in body, False)


# ── 8. The manager UI ───────────────────────────────────────────────────────
def the_manager_ui_holds_its_shape():
    check("ui/only leagues with teams can carry an arrangement",
          [l["id"] for l in subs.leagues_with_teams([TEAMED, UNTEAMED])], [555])
    v = subs.StandingAddView([TEAMED], store.empty_state())
    check("ui/starts on the league", [type(c).__name__ for c in v.children], ["LeagueSelect"])
    check("ui/not ready", v.ready(), False)
    v.league_id = "555"
    v.build()
    check("ui/then team and person",
          [type(c).__name__ for c in v.children],
          ["LeagueSelect", "TeamSelect", "StandingMemberSelect", "StandingSubmitButton"])
    tsel = at(v.children, 1, "ui/there is a team select")
    check("ui/no teamless option here — an arrangement is per team",
          subs.NO_TEAM in [o.value for o in (tsel.options if tsel else [])], False)
    submit = at(v.children, 3, "ui/there is a submit button")
    check("ui/the button is dead until every part is picked",
          submit.disabled if submit else None, True)
    v.team, v.member = "Delaney", WILL
    v.build()
    submit = at(v.children, 3, "ui/still a submit button")
    check("ui/and live once they are", submit.disabled if submit else None, False)
    check("ui/it says what it will do",
          ("Will" in submit.label and "Delaney" in submit.label) if submit else None, True)
    check("ui/the prompt says the first one is the auto-assign",
          "auto-assigned" in v.prompt(), True)

    st = store.empty_state()
    store.add_standing(st, user_id=WILL.id, name="Will Grant", league_id="555",
                       league="Thursday League", team="Delaney")
    v2 = subs.StandingAddView([TEAMED], st)
    v2.league_id, v2.team, v2.member = "555", "Delaney", SAM
    v2.build()
    check("ui/a second person is told they're #2 and why", "#2" in v2.prompt(), True)
    check("ui/summary lists the arrangement", "Will Grant" in subs.standing_summary(st), True)
    check("ui/empty summary says so", "No go-to subs" in subs.standing_summary(store.empty_state()), True)


# ── 9. Invariants that must survive this change ─────────────────────────────
def a_select_never_mutates_shared_state():
    """An ast walk over every *Select class's callback. Brian's rule: a select may
    only set view state or open a confirm view — a mis-tap has no undo. This is the
    audit that used to be ad-hoc; three new selects arrived with go-to subs."""
    MUTATORS = {
        "add_sub", "remove_sub", "toggle_spot", "new_request", "close_request",
        "set_spots", "upsert_availability", "remove_availability", "expire",
        "assign_auto", "confirm_auto", "decline_auto", "add_standing",
        "remove_standing", "add_series", "claim_nights", "fill_nights_for",
        "claim_series", "set_request_spots", "fill_request_spot", "drop_auto",
        "set_team_for", "confirm_auto", "remove_sub_by_anyone", "claim_from_page",
        "_save", "save",
    }
    tree = ast.parse(open("subs.py", encoding="utf-8").read())
    offenders, seen = [], []
    for node in tree.body:
        # FillForPick is a Select too — the rule is about the component, not the name.
        if not isinstance(node, ast.ClassDef) or not node.name.endswith(("Select", "Pick")):
            continue
        cb = next((n for n in node.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "callback"), None)
        if cb is None:
            continue
        seen.append(node.name)
        for sub_node in ast.walk(cb):
            if (isinstance(sub_node, ast.Call)
                    and isinstance(sub_node.func, ast.Attribute)
                    and sub_node.func.attr in MUTATORS):
                offenders.append(f"{node.name}.{sub_node.func.attr}")
    # 10 before go-to subs, plus StandingMemberSelect and StandingRemoveSelect. The
    # count is asserted so a new select can't be added without landing in this audit.
    check("invariant/every select was audited", sorted(seen), sorted([
        "FillForPick", "FillForMemberSelect", "RemoveSubSelect", "CancelRequestSelect",
        "LeagueSelect", "TeamSelect", "GameSelect", "SpotsSelect", "RemoveAvailSelect",
        "NightSelect", "StandingMemberSelect", "StandingRemoveSelect"]))
    check("invariant/no select mutates shared state", offenders, [])


def one_picker_four_meanings_keeps_its_callers_words():
    """NightSelect now serves four different people — taking dates, dropping ones you
    were assigned, marking someone else in, and naming a team. Its placeholder belongs
    to the CALLER, the lesson from GameSelect showing requesters "Games you can cover…".

    Checked at the call sites, because that's where the bug was: a component with a
    sensible default reads fine in isolation and still says the wrong thing on screen."""
    tree = ast.parse(open("subs.py", encoding="utf-8").read())
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "NightSelect"]
    words = []
    missing = 0
    for c in sites:
        kw = next((k for k in c.keywords if k.arg == "placeholder"), None)
        if kw is None or not isinstance(kw.value, ast.Constant):
            missing += 1
        else:
            words.append(kw.value.value)
    check("invariant/every NightSelect call site names its own placeholder", missing, 0)
    check("invariant/there are four of them", len(sites), 4)
    check("invariant/and no two flows share a wording", len(set(words)), len(words))

    reqs = [{"id": "a", "game_ts": THU[0], "spots_needed": 1, "filled": [], "pending": [],
             "team": "Delaney", "requester_name": "Bruce Iyer"}]
    check("invariant/the description is the caller's too",
          subs.NightSelect(reqs, [], placeholder="x",
                           description_of=lambda r: "mine").options[0].description, "mine")


def dates_never_nights():
    """Brian: "don't use 'nights' as some leagues are daytime hours." The rule is about
    what a member READS, so this looks at string literals only — docstrings and the old
    internal identifiers (NightSelect, claim_nights) are deliberately left alone."""
    NEW = {"AutoAssignView", "ConfirmAutoButton", "DropAutoButton", "AutoDropView",
           "AutoDropSubmit", "SetTeamButton", "SetTeamView", "SetTeamSubmit",
           "StandingHomeView", "StandingAddButton", "StandingRemoveButton",
           "StandingAddView", "StandingMemberSelect", "StandingSubmitButton",
           "StandingRemoveSelect", "ConfirmRemoveStandingView"}
    tree = ast.parse(open("subs.py", encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            first = (node.body or [None])[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    bad = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in NEW:
            continue
        for n in ast.walk(node):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings and "night" in n.value.lower()):
                bad.append(f"{node.name}: {n.value[:50]}")
    check("invariant/no user-facing 'nights' in the new copy", bad, [])
    check("invariant/the new copy was actually scanned",
          len([n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in NEW]),
          len(NEW))


async def main():
    for fn in (the_team_with_a_goto_never_reaches_the_room,
               a_team_without_one_still_asks_the_room,
               six_dates_are_one_dm_but_six_assignments,
               priority_fills_the_second_spot_not_a_spare,
               the_requesters_own_arrangement_doesnt_cover_them,
               dropping_one_date_leaves_the_rest,
               confirming_clears_the_flag,
               an_unconfirmed_assignment_is_chased_before_game_day,
               the_reminder_loop_actually_runs_the_chase,
               a_new_arrangement_covers_what_is_already_asking,
               ending_an_arrangement_leaves_the_dates_alone,
               attaching_a_team_warns_about_the_double_book,
               attaching_a_team_hands_it_to_the_goto,
               the_nudge_goes_out_once_per_person_per_league,
               the_alert_puts_the_goto_line_first):
        await fn()


asyncio.run(main())
the_manager_ui_holds_its_shape()
a_select_never_mutates_shared_state()
one_picker_four_meanings_keeps_its_callers_words()
dates_never_nights()

if FAILS:
    print("\n".join(f"FAIL: {f}" for f in FAILS))
    raise SystemExit(1)
print("All go-to sub checks passed.")
