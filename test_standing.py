"""Unit tests for super subs — standing arrangements that auto-assign.

  1. A super sub is bound to a LEAGUE + TEAM, never a weekday. Whoever sets it up
     picks both from the same pickers everyone else uses, so nothing has to infer
     what "the Thursday league" means, and the binding is dated for free.
  2. When that team needs a sub, the super sub is PUT ON the spot as the request is
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
import time
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
# A full season's worth, for the notification-volume checks below.
NINE = [night(7 * i + 4) for i in range(9)]

TEAMED = {
    "id": 555, "title": "Thursday League – Fall 2026 League 1 – Begins September 3",
    "day": "Thursday", "time": "7:45 pm",
    "draws": [{"date": iso[:10], "weekday": "Thursday", "time": "7:45 pm"} for iso in THU],
    "team_names": ["Ashby", "Vance"],
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


ALEX, ROBIN, SAM, BRUCE = U(1, "Alex Reed"), U(2, "Robin Vale"), U(3, "Sam Ortiz"), U(4, "Bruce Iyer")


COGS = []


async def flush(cog):
    """Drain the notice outbox now instead of waiting out NOTIFY_WINDOW."""
    await cog._flush_all_notices()


def make_cog():
    """A Subs cog with everything that touches Discord stubbed, recording calls."""
    cog = subs.Subs(object())
    COGS.append(cog)
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
    """Register a super sub straight in the store (the UI path is tested separately)."""
    return store.add_standing(cog.state, user_id=member.id, name=member.display_name,
                              league_id=str(league["id"]), league=subs.league_label(league),
                              team=team, created_by=ALEX.id, now=subs.club_now())


async def post(cog, requester, dates, *, team="Ashby", spots=1, league=TEAMED, channel=None):
    return await cog.add_series(
        requester=requester, league_id=str(league["id"]),
        league=subs.league_label(league), team=team, game_isos=dates, spots=spots,
        channel=channel or Ch())


# ── 1. The store: an arrangement, not a sign-up ─────────────────────────────
st = store.empty_state()
check("store/new state has a standing list", st["standing"], [])
check("store/add", store.add_standing(st, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                                      league="Thursday League", team="Ashby"), "added")
# An empty team is the WHOLE LEAGUE, not a malformed arrangement — that's the shape
# the club actually asked for: sign up for the league, first team to need someone
# gets you.
st_lg = store.empty_state()
check("league/an empty team means the whole league",
      store.add_standing(st_lg, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                         league="Thursday League", team=""), "added")
check("league/it covers a team it never named",
      [g["name"] for g in store.standing_for(st_lg, "555", "Ashby")], ["Robin Vale"])
check("league/and any other team in that league",
      [g["name"] for g in store.standing_for(st_lg, "555", "Vance")], ["Robin Vale"])
check("league/but not another league",
      store.standing_for(st_lg, "777", "Ashby"), [])
check("league/a team's own super sub is offered first",
      (store.add_standing(st_lg, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                          league="Thursday League", team="Ashby"),
       [g["name"] for g in store.standing_for(st_lg, "555", "Ashby")]),
      ("added", ["Sam Ortiz", "Robin Vale"]))
check("league/on a team they don't cover, the league's is alone",
      [g["name"] for g in store.standing_for(st_lg, "555", "Vance")], ["Robin Vale"])
check("league/one arrangement each: no team on top of the league",
      store.add_standing(st_lg, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                         league="Thursday League", team="Vance"), "other_team")
check("league/nor the league on top of a team",
      store.add_standing(st_lg, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                         league="Thursday League", team=""), "other_team")
check("store/same person twice",
      store.add_standing(st, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                         league="Thursday League", team="Ashby"), "already")
store.add_standing(st, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                   league="Thursday League", team="Ashby")
check("store/priority is order of arrangement",
      [g["name"] for g in store.standing_for(st, "555", "Ashby")], ["Robin Vale", "Sam Ortiz"])
check("store/team match is case and space tolerant",
      [g["name"] for g in store.standing_for(st, "555", "  ashby ")], ["Robin Vale", "Sam Ortiz"])
check("store/another team has none", store.standing_for(st, "555", "Vance"), [])
check("store/a teamless request has none", store.standing_for(st, "555", ""), [])
check("store/wrong league has none", store.standing_for(st, "999", "Ashby"), [])
check("store/remove", store.remove_standing(st, ROBIN.id, "555", "Ashby"), True)
check("store/remove promotes nobody but leaves order",
      [g["name"] for g in store.standing_for(st, "555", "Ashby")], ["Sam Ortiz"])
check("store/remove twice", store.remove_standing(st, ROBIN.id, "555", "Ashby"), False)

# THE load-bearing difference from availability: an arrangement never goes stale.
st2 = store.empty_state()
old = subs.club_now() - timedelta(days=90)
store.add_standing(st2, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                   league="Thursday League", team="Ashby", now=old)
store.upsert_availability(st2, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                          league="Thursday League", games=[], now=old)
store.expire(st2, subs.club_now(), 3)
check("store/expire ages out a 90-day-old availability", st2["availability"], [])
check("store/expire NEVER ages out an arrangement", len(st2["standing"]), 1)



# ── 1b. One team each, and never two games at once ──────────────────────────
# Someone was made the super sub for ALL NINE teams of one league — nothing stopped
# it, and it sounds like "he covers the whole night". It isn't: the two teams that
# both need someone for the same draw then BOTH get him, on different sheets at the
# same time. Nine teams is up to four of those a week.
st4 = store.empty_state()
check("oneteam/first team is fine",
      store.add_standing(st4, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                         league="Thursday League", team="Ashby"), "added")
check("oneteam/a second team in the SAME league is refused",
      store.add_standing(st4, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                         league="Thursday League", team="Vance"), "other_team")
check("oneteam/and nothing was written",
      [g["team"] for g in st4["standing"]], ["Ashby"])
check("oneteam/the same team again is 'already', not a clash",
      store.add_standing(st4, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                         league="Thursday League", team="Ashby"), "already")
check("oneteam/another league is a different arrangement",
      store.add_standing(st4, user_id=ROBIN.id, name="Robin Vale", league_id="777",
                         league="Sunday League", team="Vance"), "added")
check("oneteam/somebody else can hold that team",
      store.add_standing(st4, user_id=SAM.id, name="Sam Ortiz", league_id="555",
                         league="Thursday League", team="Vance"), "added")
check("oneteam/conflict names the team they already hold",
      (store.standing_conflict(st4, ROBIN.id, "555", "Vance") or {}).get("team"), "Ashby")
check("oneteam/no conflict with the team they hold",
      store.standing_conflict(st4, ROBIN.id, "555", "Ashby"), None)

# busy_at: the backstop, for the clashes one-team-per-league can't see (two leagues
# sharing a slot, or a spot they took by hand).
st5 = store.empty_state()
r_a = store.new_request(st5, requester_id=99, requester_name="A", spots_needed=1,
                        game_ts=THU[0], league_id="555", team="Ashby")
r_b = store.new_request(st5, requester_id=98, requester_name="B", spots_needed=1,
                        game_ts=THU[0], league_id="777", team="Vance")
r_c = store.new_request(st5, requester_id=97, requester_name="C", spots_needed=1,
                        game_ts=THU[1], league_id="777", team="Vance")
store.add_sub(r_a, ROBIN.id, "Robin Vale")
check("busy/finds the game they're already on at that time",
      (store.busy_at(st5, ROBIN.id, THU[0]) or {})["id"], r_a["id"])
check("busy/ignores the request being asked about",
      store.busy_at(st5, ROBIN.id, THU[0], exclude_rid=r_a["id"]), None)
check("busy/a different time is not a clash", store.busy_at(st5, ROBIN.id, THU[1]), None)
check("busy/someone else's game is not their clash",
      store.busy_at(st5, SAM.id, THU[0]), None)
check("busy/no timestamp, no opinion", store.busy_at(st5, ROBIN.id, ""), None)


# ── 2. Assignment mechanics ─────────────────────────────────────────────────
st3 = store.empty_state()
req = store.new_request(st3, requester_id=BRUCE.id, requester_name="Bruce Iyer",
                        spots_needed=1, game_ts=THU[0], league_id="555",
                        league="Thursday League", team="Ashby")
check("assign/requester can't sub their own game",
      store.assign_auto(req, BRUCE.id, "Bruce Iyer", "aaa"), "requester")
check("assign/ok", store.assign_auto(req, ROBIN.id, "Robin Vale", "aaa"), "assigned")
check("assign/covers the spot", store.open_spots(req), 0)
check("assign/is an ordinary filled entry", [f["user_id"] for f in req["filled"]], [ROBIN.id])
check("assign/carries the batch id", req["filled"][0]["auto"], "aaa")
check("assign/starts unconfirmed", req["filled"][0]["confirmed"], False)
check("assign/again is a no-op", store.assign_auto(req, ROBIN.id, "Robin Vale", "aaa"), "already")
check("assign/no spots left", store.assign_auto(req, SAM.id, "Sam Ortiz", "aaa"), "full")
check("assign/unconfirmed list", [f["name"] for f in store.unconfirmed_auto(req)], ["Robin Vale"])
check("assign/confirm", store.confirm_auto(req, ROBIN.id), "confirmed")
check("assign/confirm twice", store.confirm_auto(req, ROBIN.id), "already")
check("assign/nothing left unconfirmed", store.unconfirmed_auto(req), [])
check("assign/drop", store.decline_auto(req, ROBIN.id), "removed")
check("assign/drop reopens the spot", store.open_spots(req), 1)
check("assign/a drop is remembered for THIS date", req["auto_declined"], [ROBIN.id])
check("assign/and nothing puts them back on it",
      store.assign_auto(req, ROBIN.id, "Robin Vale", "bbb"), "declined")
check("assign/but the next person still can",
      store.assign_auto(req, SAM.id, "Sam Ortiz", "bbb"), "assigned")
check("assign/drop someone who isn't on it", store.decline_auto(req, ALEX.id), "absent")

# A manual (self-serve) fill is NOT an auto assignment and is never chased.
manual = store.new_request(st3, requester_id=BRUCE.id, requester_name="Bruce Iyer",
                           spots_needed=1, game_ts=THU[1], league_id="555", team="Ashby")
store.add_sub(manual, SAM.id, "Sam Ortiz")
check("assign/a hand-raise is not an assignment", store.auto_entries(manual), [])
check("assign/and never chased", store.unconfirmed_auto(manual), [])


# ── 3. Posting: the super sub gets it, the room is never asked ──────────────────
async def the_team_with_a_goto_never_reaches_the_room():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    made, skipped, filled = await post(cog, BRUCE, [THU[0]])
    check("post/one request made", made, 1)
    check("post/and it was filled by the arrangement", filled, 1)
    req = cog.state["requests"][0]
    check("post/nobody is asked to cover it", store.open_spots(req), 0)
    check("post/no alert page at all",
          [c for c in cog.calls if c[0] == "page"], [])
    check("post/the board still gets rendered",
          ("board",) in cog.calls, True)
    check("post/nothing is said until the notice window closes", cog.dms, [])
    await flush(cog)
    check("post/the super sub is DM'd", [d[0] for d in cog.dms], [ROBIN.id])
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
    made, skipped, filled = await post(cog, BRUCE, [THU[0]], team="Vance")
    check("post/no arrangement means nothing is auto-filled", filled, 0)
    check("post/and the alert goes up as before",
          [c[1] for c in cog.calls if c[0] == "page"], ["new"])
    check("post/nobody is DM'd", cog.dms, [])


async def six_dates_are_one_dm_but_six_assignments():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    made, skipped, filled = await post(cog, BRUCE, THU)
    check("post/every date is its own request", made, 6)
    check("post/every one auto-filled", filled, 6)
    await flush(cog)
    check("post/one DM for the lot", len(cog.dms), 1)
    aids = {f["auto"] for r in cog.state["requests"] for f in r["filled"]}
    check("post/sharing one batch id", len(aids), 1)
    check("post/but six separate records",
          len({r["id"] for r in cog.state["requests"]}), 6)
    check("post/no alerts", [c for c in cog.calls if c[0] == "page"], [])


async def priority_fills_the_second_spot_not_a_spare():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    goto(cog, SAM, "Ashby")
    await post(cog, BRUCE, [THU[0]], spots=2)
    req = cog.state["requests"][0]
    check("priority/both spots go to the two super subs, in order",
          [f["name"] for f in req["filled"]], ["Robin Vale", "Sam Ortiz"])
    await flush(cog)
    check("priority/two people, two DMs", sorted(d[0] for d in cog.dms),
          sorted([ROBIN.id, SAM.id]))

    cog2 = make_cog()
    goto(cog2, ROBIN, "Ashby")
    goto(cog2, SAM, "Ashby")
    await post(cog2, BRUCE, [THU[0]], spots=1)
    check("priority/one spot goes to the first only",
          [f["name"] for f in cog2.state["requests"][0]["filled"]], ["Robin Vale"])


async def the_requesters_own_arrangement_doesnt_cover_them():
    cog = make_cog()
    goto(cog, BRUCE, "Ashby")          # Bruce is the super sub AND the one who's out
    made, skipped, filled = await post(cog, BRUCE, [THU[0]])
    check("post/you are never assigned to your own ask", filled, 0)
    check("post/so the room is asked",
          [c[1] for c in cog.calls if c[0] == "page"], ["new"])


# ── 4. Dropping and confirming ──────────────────────────────────────────────
async def a_league_wide_super_sub_goes_to_whoever_asks_first():
    """What the club actually wanted: sign up for the LEAGUE, and the first team that
    needs someone gets you. Two teams asking for the same draw is the interesting
    case — nobody plays two games at once, so the second falls through to the room."""
    cog = make_cog()
    store.add_standing(cog.state, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                       league=subs.league_label(TEAMED), team="")      # whole league
    await post(cog, BRUCE, [THU[0]], team="Ashby")
    first = at(cog.state["requests"], 0, "league/the first ask exists")
    if first is None:
        return
    check("league/the first team to ask gets them", store.open_spots(first), 0)
    check("league/on a team the arrangement never named",
          (store.auto_entry(first, ROBIN.id) or {}).get("name"), "Robin Vale")

    cog.calls.clear()
    await cog.add_series(requester=ALEX, league_id="555", league=subs.league_label(TEAMED),
                         team="Vance", game_isos=[THU[0]], spots=1, channel=Ch())
    second = at(cog.state["requests"], 1, "league/the second ask exists")
    if second is None:
        return
    check("league/the second team on the same draw does NOT", store.open_spots(second), 1)
    check("league/so that one goes to the room",
          [c[1] for c in cog.calls if c[0] == "page"], ["new"])

    # A different week is a different question.
    await cog.add_series(requester=ALEX, league_id="555", league=subs.league_label(TEAMED),
                         team="Vance", game_isos=[THU[1]], spots=1, channel=Ch())
    third = at(cog.state["requests"], 2, "league/the third ask exists")
    check("league/another draw is theirs again",
          store.open_spots(third) if third else None, 0)


async def a_team_super_sub_is_asked_before_the_league_one():
    cog = make_cog()
    store.add_standing(cog.state, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                       league=subs.league_label(TEAMED), team="")      # whole league
    goto(cog, SAM, "Ashby")                                            # that team's own
    await post(cog, BRUCE, [THU[0]], team="Ashby")
    got = at(cog.state["requests"], 0, "league/a request exists")
    check("league/the team's own super sub takes it",
          [f["name"] for f in (got or {}).get("filled", [])], ["Sam Ortiz"])
    # …and the league-wide one is still free for another team on that draw.
    await cog.add_series(requester=ALEX, league_id="555", league=subs.league_label(TEAMED),
                         team="Vance", game_isos=[THU[0]], spots=1, channel=Ch())
    other = at(cog.state["requests"], 1, "league/the other request exists")
    check("league/and the league's covers the other team",
          [f["name"] for f in (other or {}).get("filled", [])], ["Robin Vale"])


async def a_new_league_arrangement_sweeps_the_whole_league():
    """Found in a live store: a whole-league arrangement was made while three of that
    league's requests were open, and picked up none of them. The retroactive sweep was
    matching the arrangement's team against the request's team — and a whole-league
    arrangement has no team, so it only ever matched the team-LESS requests."""
    cog = make_cog()
    await post(cog, BRUCE, [THU[0]], team="Ashby")
    await post(cog, ALEX, [THU[1]], team="Vance")
    check("sweep/both are open to begin with",
          [store.open_spots(r) for r in cog.state["requests"]], [1, 1])
    result, isos = await cog.add_standing(
        actor=ALEX, member=ROBIN, league_id="555",
        league=subs.league_label(TEAMED), team="", channel=Ch())
    check("sweep/added", result, "added")
    check("sweep/it takes the league's open dates, whatever the team", len(isos), 2)
    check("sweep/nothing left asking",
          [store.open_spots(r) for r in cog.state["requests"]], [0, 0])

    # A one-team arrangement still only sweeps its own team.
    cog2 = make_cog()
    await post(cog2, BRUCE, [THU[0]], team="Ashby")
    await post(cog2, ALEX, [THU[1]], team="Vance")
    _r, isos2 = await cog2.add_standing(actor=ALEX, member=ROBIN, league_id="555",
                                        league=subs.league_label(TEAMED), team="Ashby",
                                        channel=Ch())
    check("sweep/a team arrangement takes only that team's", len(isos2), 1)
    check("sweep/leaving the other team's open",
          store.open_spots(cog2.state["requests"][1]), 1)


async def nobody_is_tagged_for_a_slot_they_are_already_playing():
    """Also found live: the alert for the second team needing someone on a draw tagged
    the league's super sub — who was already down for the first team, same time. The
    assignment guard knew; the alert didn't, and invited them to be in two places."""
    cog = make_cog()
    store.add_standing(cog.state, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                       league=subs.league_label(TEAMED), team="")
    store.upsert_availability(cog.state, user_id=SAM.id, name="Sam Ortiz",
                              league_id="555", league="Thursday League", games=[])
    await post(cog, BRUCE, [THU[0]], team="Ashby")           # Robin takes this one
    await cog.add_series(requester=ALEX, league_id="555", league=subs.league_label(TEAMED),
                         team="Vance", game_isos=[THU[0]], spots=1, channel=Ch())
    second = at(cog.state["requests"], 1, "tag/the second request exists")
    if second is None:
        return
    body = cog._page_body(second, reason="new")
    check("tag/the super sub is not tagged for a game they're already playing",
          f"<@{ROBIN.id}>" in body, False)
    check("tag/but someone free still is", f"<@{SAM.id}>" in body, True)

    # Take Sam elsewhere at that time too, and the alert stops pretending.
    # (Not onto request[0] — that one is full, so add_sub would be a no-op and the
    # check would pass for the wrong reason.)
    elsewhere = store.new_request(cog.state, requester_id=77, requester_name="Someone",
                                  spots_needed=1, game_ts=THU[0], league_id="999",
                                  team="Corwin")
    check("tag/Sam really is booked elsewhere",
          store.add_sub(elsewhere, SAM.id, "Sam Ortiz"), "added")
    body2 = cog._page_body(second, reason="new")
    check("tag/availability is filtered the same way",
          f"<@{SAM.id}>" in body2, False)
    check("tag/and it says nobody is listed",
          "No one's listed as available" in body2, True)


async def never_assigned_to_two_games_at_once():
    """The backstop for the clash one-team-per-league can't prevent: two leagues that
    play the same slot. An arrangement is a standing yes, not a promise to be in two
    places — so the clashing date goes to the room like any other."""
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")                                    # league 555
    store.add_standing(cog.state, user_id=ROBIN.id, name="Robin Vale",
                       league_id="777", league="Sunday League", team="Vance")
    await post(cog, BRUCE, [THU[0]])                             # 555 · Ashby
    check("clash/the first one is theirs",
          store.open_spots(cog.state["requests"][0]), 0)
    cog.calls.clear()
    await cog.add_series(requester=ALEX, league_id="777", league="Sunday League",
                         team="Vance", game_isos=[THU[0]], spots=1, channel=Ch())
    second = at(cog.state["requests"], 1, "clash/the second request exists")
    if second is None:
        return
    check("clash/the same slot is NOT auto-filled", store.open_spots(second), 1)
    check("clash/so the room is asked for it",
          [c[1] for c in cog.calls if c[0] == "page"], ["new"])
    check("clash/and they aren't on it", store.auto_entry(second, ROBIN.id), None)

    # A different slot in that same league is still theirs.
    await cog.add_series(requester=ALEX, league_id="777", league="Sunday League",
                         team="Vance", game_isos=[THU[1]], spots=1, channel=Ch())
    third = at(cog.state["requests"], 2, "clash/the third request exists")
    check("clash/a clear slot is still auto-filled",
          store.open_spots(third) if third else None, 0)


async def the_ui_says_no_before_the_button_is_pressed():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    v = subs.StandingAddView([TEAMED], cog.state)
    v.league_id, v.team, v.team_raw, v.member = "555", "Vance", "Vance", ROBIN
    v.build()
    check("oneteam/the flow knows about the clash",
          (v.conflict() or {}).get("team"), "Ashby")
    check("oneteam/so it isn't ready", v.ready(), False)
    check("oneteam/the button is dead",
          at(v.children, 3, "oneteam/submit button").disabled
          if at(v.children, 3, "oneteam/submit button") else None, True)
    check("oneteam/and it says which team they already have",
          "Ashby" in v.prompt() and "One team each" in v.prompt(), True)
    v.member = SAM
    v.build()
    check("oneteam/somebody else is fine", v.ready(), True)


async def dropping_one_date_leaves_the_rest():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, THU[:3])
    second = at(cog.state["requests"], 1, "drop/three dates were posted")
    if second is None:
        return
    rid = second["id"]
    cog.calls.clear()
    dropped = await cog.drop_auto(ROBIN, [rid])
    check("drop/just that date", dropped, [second["game_ts"]])
    check("drop/it reopens", store.open_spots(store.find_request(cog.state, rid)), 1)
    check("drop/and the room is asked for it",
          [c for c in cog.calls if c[0] == "page"], [("page", "bump", rid)])
    others = [r for r in cog.state["requests"] if r["id"] != rid]
    check("drop/the other dates are untouched",
          [store.open_spots(r) for r in others], [0, 0])
    check("drop/the arrangement itself stands",
          len(store.standing_for(cog.state, "555", "Ashby")), 1)
    check("drop/the requester is told", any(d[0] == BRUCE.id for d in cog.dms), True)
    await flush(cog)
    # And a re-post of that same date must not put them back on it.
    again = await cog._auto_assign([store.find_request(cog.state, rid)])
    check("drop/never re-assigned to a date they dropped", again, {})


async def confirming_clears_the_flag():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, THU[:2])
    req = at(cog.state["requests"], 0, "confirm/a request exists")
    entry = at((req or {}).get("filled", []), 0, "confirm/somebody was assigned")
    if entry is None:
        return
    line = subs._req_status_line(req)
    check("confirm/an unconfirmed assignment says so", "(unconfirmed)" in line, True)
    done = await cog.confirm_auto(ROBIN)
    check("confirm/every date they're down for", len(done), 2)
    check("confirm/the board stops flagging it",
          "(unconfirmed)" in subs._req_status_line(req), False)
    check("confirm/twice is a no-op", await cog.confirm_auto(ROBIN), [])
    check("confirm/it only ever confirms your own", await cog.confirm_auto(SAM), [])
    await flush(cog)


async def an_unconfirmed_assignment_is_chased_before_game_day():
    cog = make_cog()
    ch = Ch()
    goto(cog, ROBIN, "Ashby")

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
    check("chase/and it names the person", f"<@{ROBIN.id}>" in (said[0] if said else ""), True)
    check("chase/the spot is NOT reopened — they're still the sub",
          store.open_spots(first), 0)
    check("chase/and no DM saying the same thing again — the @-mention IS the ping",
          [d for d in cog.dms if d[0] == ROBIN.id and "\u23f0" in d[1]], [])
    ch.sent.clear()
    await cog._chase_unconfirmed(subs.club_now())
    check("chase/only once per request", ch.sent, [])

    # A confirmed one is never chased.
    cog2 = make_cog()
    ch2 = Ch()

    async def resolve2(cid):
        return ch2
    cog2._resolve_channel = resolve2
    goto(cog2, ROBIN, "Ashby")
    await post(cog2, BRUCE, [night(1)], channel=ch2)
    req2 = at(cog2.state["requests"], 0, "chase/the second request exists")
    seat = at((req2 or {}).get("filled", []), 0, "chase/somebody was assigned to it")
    if seat is None:
        return
    await cog2.confirm_auto(ROBIN)
    await cog2._chase_unconfirmed(subs.club_now())
    check("chase/a confirmed assignment is left alone", ch2.sent, [])



# ── 4b. How many times does the bot buzz one person? ────────────────────────
# A season's worth of dates going up used to produce NINE notifications for one
# member. Nine dates posted one at a time are nine assignments and nine alerts, but
# to the person on the other end they are ONE thing that happened, and a member who
# mutes the bot is a member the board can no longer reach. Every count below is a
# phone buzz: a DM is 1, a channel message carrying your @-mention is 1, an edit is 0.

async def nine_postings_are_one_message():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    for d in NINE:                     # a chair working through the season, one at a time
        await post(cog, BRUCE, [d])
    check("volume/nine postings, nine assignments",
          sum(len(store.auto_entries(r)) for r in cog.state["requests"]), 9)
    check("volume/and nothing sent while the window is open", cog.dms, [])
    await flush(cog)
    check("volume/ONE message, not nine", len(cog.dms), 1)
    body = cog.dms[0][1] if cog.dms else ""
    check("volume/that names every date", "9 dates" in body, True)
    check("volume/with buttons covering all of them",
          type(cog.dms[0][2]).__name__ if cog.dms else None, "AutoAssignView")
    # And nothing is announced twice.
    cog.dms.clear()
    cog._queue_sub_notice(ROBIN.id)
    await flush(cog)
    check("volume/already-told dates are never re-announced", cog.dms, [])


async def a_tenth_date_later_is_its_own_message():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, THU[:3])
    await flush(cog)
    check("volume/first three", len(cog.dms), 1)
    await post(cog, BRUCE, [THU[4]])      # a week later — genuinely new news
    await flush(cog)
    check("volume/a later posting is its own message", len(cog.dms), 2)
    body = cog.dms[1][1] if len(cog.dms) > 1 else ""
    check("volume/and only mentions the new date",
          (subs.fmt_when(THU[4]) in body, subs.fmt_when(THU[0]) in body), (True, False))


async def a_burst_of_alerts_pings_you_once():
    """No super sub — just someone listed as available. This is the prod case."""
    cog = make_cog()
    ch = Ch()

    async def resolve(cid):
        return ch
    cog._resolve_channel = resolve
    cog.post_page = subs.Subs.post_page.__get__(cog)     # the REAL alert path
    store.upsert_availability(cog.state, user_id=ROBIN.id, name="Robin Vale",
                              league_id="555", league="Thursday League", games=[])
    for d in NINE:
        await post(cog, BRUCE, [d], team="Vance", channel=ch)
    check("volume/nine asks means nine alerts", len(ch.sent), 9)
    pinged = sum(1 for body, _ in ch.sent if f"<@{ROBIN.id}>" in body)
    named = sum(1 for body, _ in ch.sent if "Robin" in body and f"<@{ROBIN.id}>" not in body)
    check("volume/but he is @-mentioned ONCE", pinged, 1)
    check("volume/and named without a ping on the rest", named, 8)
    check("volume/the alert still reads right",
          "tagged a moment ago" in (ch.sent[-1][0] if ch.sent else ""), True)


async def the_chase_says_it_once_per_room():
    cog = make_cog()
    ch = Ch()

    async def resolve(cid):
        return ch
    cog._resolve_channel = resolve
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, [night(1, 19), night(1, 17), night(1, 15)], channel=ch)
    await flush(cog)
    cog.dms.clear()
    ch.sent.clear()
    await cog._chase_unconfirmed(subs.club_now())
    check("volume/three unconfirmed dates, ONE message", len(ch.sent), 1)
    body = ch.sent[0][0] if ch.sent else ""
    check("volume/naming all three", body.count("·") >= 3, True)
    check("volume/mentioning him once", body.count(f"<@{ROBIN.id}>"), 1)
    check("volume/and no DM on top", cog.dms, [])


async def someone_with_dms_closed_is_not_chased_forever():
    cog = make_cog()

    async def dead_dm(uid, text, view=None):
        cog.dms.append((uid, text, view))
        return False                      # their DMs are closed
    cog._dm = dead_dm
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, THU[:2])
    await flush(cog)
    check("volume/one attempt", len(cog.dms), 1)
    cog._queue_sub_notice(ROBIN.id)
    await flush(cog)
    check("volume/not retried forever", len(cog.dms), 1)



# ── 4c. One message means one MESSAGE, not two stuck together ───────────────
# A single DM carrying two unrelated ⭐ announcements — an arrangement for one team,
# then a date for a different one — reads as two messages. Both halves were correct;
# the shape wasn't. The second half was there at all because its notice had been lost
# to a restart hours earlier and nothing ever retried it.

def a_notice_reads_as_one_thing():
    TL = "Tuesday League 9/1 – 12/22"
    one = subs.build_notice([], {(TL, "Ashby"): [THU[0]]})
    check("shape/the everyday case is one sentence and a prompt",
          one.count("⭐"), 1)
    check("shape/and names the team and the date",
          all(x in one for x in ("Ashby", subs.fmt_when(THU[0]))), True)

    made = subs.build_notice([(TL, "Corwin", "Alex")], {})
    check("shape/a bare arrangement says how it works",
          "put on it automatically" in made, True)
    check("shape/and doesn't offer a Confirm for dates that don't exist",
          "Confirm so the team knows" in made, False)

    # The reported case: an arrangement for one team, a date for another.
    mixed = subs.build_notice([(TL, "Corwin", "Alex")], {(TL, "Ashby"): [THU[0]]})
    check("shape/never two ⭐ announcements stacked", mixed.count("⭐"), 1)
    check("shape/the dates are joined to the arrangement, not appended",
          "**You're also down for:**" in mixed, True)
    check("shape/and the other team is named on its own line",
          "· **Ashby**" in mixed, True)

    # An arrangement that sweeps up its OWN team's dates shouldn't repeat itself.
    own = subs.build_notice([(TL, "Ashby", "Alex")], {(TL, "Ashby"): [THU[0], THU[1]]})
    check("shape/its own team is named once, not three times", own.count("Ashby"), 1)
    check("shape/and the dates still show", "**You're down for:**" in own, True)

    two = subs.build_notice([], {(TL, "Ashby"): [THU[0]], (TL, "Corwin"): [THU[1]]})
    check("shape/two teams get one list, not two announcements", two.count("⭐"), 1)
    check("shape/naming both", ("Ashby" in two, "Corwin" in two), (True, True))
    check("shape/nothing to say, nothing said", subs.build_notice([], {}), "")


async def startup_is_what_retries_them():
    """Wiring, not behaviour: the retry existing is worthless if (re)connect doesn't
    run it. The chase taught this lesson once already."""
    cog = make_cog()
    ran = []
    cog._requeue_unnotified = lambda: ran.append(1)
    await cog.startup()
    check("restart/startup is what re-queues them", len(ran), 1)


async def a_notice_lost_to_a_restart_is_retried():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, THU[:2])
    check("restart/queued but not yet sent", cog.dms, [])

    # The bot goes down mid-window: the task dies, the store keeps `notified: False`.
    cog._notices.clear()
    await flush(cog)
    check("restart/so nothing was ever sent", cog.dms, [])
    check("restart/and the store still says they're owed one",
          len(store.auto_requests(cog.state, ROBIN.id, only_unnotified=True)), 2)

    cog._requeue_unnotified()          # what startup() does on reconnect
    await flush(cog)
    check("restart/it goes out on the next start", len(cog.dms), 1)
    sent = at(cog.dms, 0, "restart/there is a message to inspect")
    check("restart/naming the dates",
          "own for" in (sent[1] if sent else ""), True)
    cog.dms.clear()
    cog._requeue_unnotified()
    await flush(cog)
    check("restart/and not again after that", cog.dms, [])



# ── 4d. Notices are not delayed ─────────────────────────────────────────────
# There WAS a 180s hold folding messages together. It meant a super sub who had just
# been set up heard nothing about their first assignment for minutes, because the
# arrangement DM had started the clock. It is off by default now; only the settle
# remains, which is what makes one action produce one message rather than several.

def notices_are_not_rate_limited_by_default():
    cog = make_cog()
    check("timing/off by default", subs.NOTIFY_WINDOW, 0)
    check("timing/nothing waits longer than the settle",
          cog._notice_delay(ROBIN.id), subs.NOTIFY_SETTLE)
    check("timing/but the settle is real — one action, one message",
          cog._notice_delay(ROBIN.id) > 0, True)
    cog._notice_sent_at[ROBIN.id] = time.monotonic()
    check("timing/and a second message is not held back either",
          cog._notice_delay(ROBIN.id), subs.NOTIFY_SETTLE)


def the_rate_limit_still_works_if_it_is_turned_back_on():
    """The mechanism is kept, off, for the day someone is genuinely flooded."""
    cog = make_cog()
    saved = subs.NOTIFY_WINDOW
    subs.NOTIFY_WINDOW = 120
    try:
        check("timing/first is still immediate",
              cog._notice_delay(ROBIN.id) <= subs.NOTIFY_SETTLE, True)
        cog._notice_sent_at[ROBIN.id] = time.monotonic()
        d = cog._notice_delay(ROBIN.id)
        check("timing/a second inside the window waits",
              subs.NOTIFY_WINDOW - 5 <= d <= subs.NOTIFY_WINDOW, True)
        check("timing/the gap is per person, not global",
              cog._notice_delay(SAM.id) <= subs.NOTIFY_SETTLE, True)
        cog._notice_sent_at[ROBIN.id] = time.monotonic() - subs.NOTIFY_WINDOW - 1
        check("timing/once it has passed, immediate again",
              cog._notice_delay(ROBIN.id) <= subs.NOTIFY_SETTLE, True)
    finally:
        subs.NOTIFY_WINDOW = saved


async def sending_starts_the_clock():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, [THU[0]])
    check("timing/nothing sent yet, so nothing to wait for",
          ROBIN.id in cog._notice_sent_at, False)
    await flush(cog)
    check("timing/sending records when", ROBIN.id in cog._notice_sent_at, True)


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
    await post(cog, BRUCE, THU[:2])                 # posted BEFORE the arrangement existed
    check("standing/those went to the room",
          len([c for c in cog.calls if c[0] == "page"]), 1)
    cog.calls.clear()
    result, isos = await cog.add_standing(
        actor=ALEX, member=ROBIN, league_id="555",
        league=subs.league_label(TEAMED), team="Ashby", channel=Ch())
    check("standing/added", result, "added")
    check("standing/and it swept up the open dates", len(isos), 2)
    check("standing/nothing is left asking",
          [store.open_spots(r) for r in cog.state["requests"]], [0, 0])
    await flush(cog)
    told = [d for d in cog.dms if d[0] == ROBIN.id]
    check("standing/they hear it from the bot, not from the board", len(told), 1)
    check("standing/and the arrangement and the dates are ONE message",
          all(x in told[0][1] for x in ("made you the **super sub**", "You're down for"))
          if told else None, True)


async def ending_an_arrangement_leaves_the_dates_alone():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    await post(cog, BRUCE, THU[:2])
    removed = await cog.remove_standing(ROBIN.id, "555", "Ashby", actor=ALEX)
    check("standing/removed", removed, True)
    check("standing/the dates he's on stay his",
          [store.open_spots(r) for r in cog.state["requests"]], [0, 0])
    check("standing/he's told", any("ended your super sub" in d[1] for d in cog.dms), True)
    await flush(cog)
    made, skipped, filled = await post(cog, BRUCE, [THU[3]])
    check("standing/but nothing new is assigned", filled, 0)


# ── 6. Teams posted after the fact ──────────────────────────────────────────
async def attaching_a_team_warns_about_the_double_book():
    cog = make_cog()
    # Bruce posted before the chair set teams; Alex later posts for the same team+draw.
    await post(cog, BRUCE, [THU[0]], team="", league=UNTEAMED)
    await post(cog, ALEX, [THU[0]], team="Ashby", league=UNTEAMED)
    rid = cog.state["requests"][0]["id"]
    done, clashes, assigned = await cog.set_team_for(BRUCE, [rid], "Ashby")
    check("reconcile/the team is set", store.find_request(cog.state, rid)["team"], "Ashby")
    check("reconcile/one date done", len(done), 1)
    check("reconcile/and the clash is called out", len(clashes), 1)

    # No clash when nobody else has that team on that draw.
    cog2 = make_cog()
    await post(cog2, BRUCE, [THU[0]], team="", league=UNTEAMED)
    rid2 = cog2.state["requests"][0]["id"]
    done2, clashes2, _ = await cog2.set_team_for(BRUCE, [rid2], "Ashby")
    check("reconcile/no clash, no warning", (len(done2), clashes2), (1, []))

    # Only your own requests.
    cog3 = make_cog()
    await post(cog3, BRUCE, [THU[0]], team="", league=UNTEAMED)
    rid3 = cog3.state["requests"][0]["id"]
    done3, _, _ = await cog3.set_team_for(ALEX, [rid3], "Ashby")
    check("reconcile/you can only name the team on your own ask", done3, [])


async def attaching_a_team_hands_it_to_the_goto():
    cog = make_cog()
    await post(cog, BRUCE, [THU[0]], team="", league=UNTEAMED)
    store.add_standing(cog.state, user_id=ROBIN.id, name="Robin Vale",
                       league_id=str(UNTEAMED["id"]), league="Sunday league", team="Ashby")
    rid = cog.state["requests"][0]["id"]
    done, clashes, assigned = await cog.set_team_for(BRUCE, [rid], "Ashby")
    check("reconcile/naming the team lets the super sub pick it up", assigned, 1)
    check("reconcile/covered", store.open_spots(store.find_request(cog.state, rid)), 0)


async def the_nudge_goes_out_once_per_person_per_league():
    cog = make_cog()
    await post(cog, BRUCE, THU[:3], team="", league=UNTEAMED)
    await post(cog, ALEX, [THU[0]], team="", league=UNTEAMED)

    async def leagues():
        # The chair has now posted teams for that league.
        return [dict(UNTEAMED, team_names=["Ashby", "Vance"])]
    cog.get_leagues = leagues
    cog.dms.clear()
    await subs.Subs.team_reconcile_loop.coro(cog)
    check("nudge/one DM per person, not per request", sorted(d[0] for d in cog.dms),
          sorted([BRUCE.id, ALEX.id]))
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


# ── 7. The alert tags the super sub first ───────────────────────────────────────
async def the_alert_puts_the_goto_line_first():
    cog = make_cog()
    goto(cog, ROBIN, "Ashby")
    goto(cog, SAM, "Ashby")
    await post(cog, BRUCE, [THU[0]], spots=1)        # Robin takes it, Sam does not
    await flush(cog)
    req = at(cog.state["requests"], 0, "alert/the request exists")
    if req is None:
        return
    await cog.drop_auto(ROBIN, [req["id"]])            # now it's open again
    store.upsert_availability(cog.state, user_id=ALEX.id, name="Alex Reed",
                              league_id="555", league="Thursday League", games=[])
    body = cog._page_body(req, reason="bump")
    lines = [l for l in body.split("\n") if l.startswith("<@")]
    check("alert/two tag lines", len(lines), 2)
    first_line, second_line = (at(lines, 0, "alert/a super sub line") or ""), (at(lines, 1, "alert/an availability line") or "")
    check("alert/the super sub is tagged on the first line",
          first_line.startswith(f"<@{SAM.id}>"), True)
    check("alert/and named as the super sub", "super sub for this team" in first_line, True)
    check("alert/general availability comes after", f"<@{ALEX.id}>" in second_line, True)
    check("alert/someone who dropped this date is not tagged for it",
          f"<@{ROBIN.id}>" in body, False)


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
    vals = [o.value for o in (tsel.options if tsel else [])]
    check("ui/no 'teams aren't set' option here — that's the requester's escape hatch",
          subs.NO_TEAM in vals, False)
    check("ui/the whole league is offered, and offered first",
          vals[:1], [subs.ANY_TEAM])
    check("ui/the need-a-sub flow is not offered a league-wide option",
          subs.ANY_TEAM in [o.value for o in subs.TeamSelect(["Ashby"], None).options], False)
    submit = at(v.children, 3, "ui/there is a submit button")
    check("ui/the button is dead until every part is picked",
          submit.disabled if submit else None, True)
    v.team, v.team_raw, v.member = "Ashby", "Ashby", ROBIN
    v.build()
    submit = at(v.children, 3, "ui/still a submit button")
    check("ui/and live once they are", submit.disabled if submit else None, False)
    check("ui/it says what it will do",
          ("Robin" in submit.label and "Ashby" in submit.label) if submit else None, True)
    check("ui/the prompt says the first one is the auto-assign",
          "auto-assigned" in v.prompt(), True)

    # Picking "any team" is a real answer, not an empty one: the resolved team is ""
    # and only the raw pick separates it from nothing chosen yet.
    w = subs.StandingAddView([TEAMED], store.empty_state())
    w.league_id, w.member = "555", ROBIN
    w.build()
    check("ui/nothing picked yet is not ready", w.ready(), False)
    w.team_raw, w.team = subs.ANY_TEAM, ""
    w.build()
    check("ui/the whole league IS ready", w.ready(), True)
    wbtn = at(w.children, 3, "ui/league-wide submit button")
    check("ui/its button is live", wbtn.disabled if wbtn else None, False)
    check("ui/and says it's the league, not a team",
          "league's super sub" in wbtn.label if wbtn else None, True)
    check("ui/the prompt explains first-come",
          "first team that needs a sub" in w.prompt(), True)

    st = store.empty_state()
    store.add_standing(st, user_id=ROBIN.id, name="Robin Vale", league_id="555",
                       league="Thursday League", team="Ashby")
    v2 = subs.StandingAddView([TEAMED], st)
    v2.league_id, v2.team, v2.team_raw, v2.member = "555", "Ashby", "Ashby", SAM
    v2.build()
    check("ui/a second person is told they're #2 and why", "#2" in v2.prompt(), True)
    check("ui/summary lists the arrangement", "Robin Vale" in subs.standing_summary(st), True)
    check("ui/empty summary says so", "No super subs" in subs.standing_summary(store.empty_state()), True)


# ── 9. Invariants that must survive this change ─────────────────────────────
def a_select_never_mutates_shared_state():
    """An ast walk over every *Select class's callback. The rule: a select may
    only set view state or open a confirm view — a mis-tap has no undo. This is the
    audit that used to be ad-hoc; three new selects arrived with super subs."""
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
    # 10 before super subs, plus StandingMemberSelect and StandingRemoveSelect. The
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
             "team": "Ashby", "requester_name": "Bruce Iyer"}]
    check("invariant/the description is the caller's too",
          subs.NightSelect(reqs, [], placeholder="x",
                           description_of=lambda r: "mine").options[0].description, "mine")


def dates_never_nights():
    """Copy says "dates", never "nights" — some leagues are daytime hours. The rule is about
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
               a_league_wide_super_sub_goes_to_whoever_asks_first,
               a_team_super_sub_is_asked_before_the_league_one,
               a_new_league_arrangement_sweeps_the_whole_league,
               nobody_is_tagged_for_a_slot_they_are_already_playing,
               never_assigned_to_two_games_at_once,
               the_ui_says_no_before_the_button_is_pressed,
               dropping_one_date_leaves_the_rest,
               confirming_clears_the_flag,
               an_unconfirmed_assignment_is_chased_before_game_day,
               the_reminder_loop_actually_runs_the_chase,
               nine_postings_are_one_message,
               a_tenth_date_later_is_its_own_message,
               a_burst_of_alerts_pings_you_once,
               the_chase_says_it_once_per_room,
               someone_with_dms_closed_is_not_chased_forever,
               a_notice_lost_to_a_restart_is_retried,
               sending_starts_the_clock,
               startup_is_what_retries_them,
               a_new_arrangement_covers_what_is_already_asking,
               ending_an_arrangement_leaves_the_dates_alone,
               attaching_a_team_warns_about_the_double_book,
               attaching_a_team_hands_it_to_the_goto,
               the_nudge_goes_out_once_per_person_per_league,
               the_alert_puts_the_goto_line_first):
        await fn()


async def _main():
    await main()
    for c in COGS:
        await c._flush_all_notices()


asyncio.run(_main())
a_notice_reads_as_one_thing()
notices_are_not_rate_limited_by_default()
the_rate_limit_still_works_if_it_is_turned_back_on()
the_manager_ui_holds_its_shape()
a_select_never_mutates_shared_state()
one_picker_four_meanings_keeps_its_callers_words()
dates_never_nights()

if FAILS:
    print("\n".join(f"FAIL: {f}" for f in FAILS))
    raise SystemExit(1)
print("All super sub checks passed.")
