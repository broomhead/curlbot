"""Unit tests for the two 2026-08-26 sub-board features:

  1. Long-term subs — "Ben has Tuesdays for the next 8 weeks". A run is N ordinary
     requests, one per league night, sharing a series_id: one alert, one tap to
     cover it all, every night still independently claimable and droppable.
  2. Editing a live request's spot count — "we found one, now we need three" —
     which used to be a dead end (the duplicate guard refused a second post, so the
     only move was to cancel and lose the sub you had).

Run:  python3 test_series.py     (no network; needs discord.py + bs4 + aiohttp + dotenv)

Like test_fixes.py, this neuters discord.Client.run because bot.py calls bot.run()
at import time with no __main__ guard. It also points SUBS_STORE_PATH at a scratch
file BEFORE importing subs — the cog loads (and saves) its store on construction,
and a test must never touch the real one.
"""
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


# ── Fixtures ────────────────────────────────────────────────────────────────
NOW = subs.club_now()


def night(n_days: int, hour: int = 19, minute: int = 45) -> str:
    """A league night `n_days` from now, at a fixed time. Everything on the board is
    today-forward and locks near tip-off, so fixtures must be genuinely future."""
    return (NOW.replace(hour=hour, minute=minute, second=0, microsecond=0)
            + timedelta(days=n_days)).isoformat()


TUE = [night(7 * i + 3) for i in range(6)]          # six weekly nights
GAMES = [{"iso": iso, "label": subs.fmt_when(iso)} for iso in TUE]

LEAGUE = {
    "id": 999, "title": "Tuesday Night League – Fall 2026 League 1 – Begins September 1",
    "day": "Tuesday", "time": "7:45 pm",
    "draws": [{"date": iso[:10], "weekday": "Tuesday", "time": "7:45 pm"} for iso in TUE],
    "team_names": ["Smith", "Alvarez"],
}


class U:
    """Stand-in for a discord.Member: the bot only ever reads .id / .display_name."""
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name


class Ch:
    def __init__(self, cid=1):
        self.id = cid
        self.guild = None


ANN, BEN, CY, DEE = U(1, "Ann Lee"), U(2, "Ben Ross"), U(3, "Cy Park"), U(4, "Dee Kim")


class FakeResponse:
    """Just enough of discord's InteractionResponse to run a callback and see what it
    did — the point of these tests is WHICH call happens, not how Discord renders it."""
    def __init__(self):
        self.calls = []

    async def edit_message(self, **kw):
        self.calls.append(("edit_message", kw))

    async def defer(self, **kw):
        self.calls.append(("defer", kw))

    async def send_message(self, *a, **kw):
        self.calls.append(("send_message", kw))


class FakeInteraction:
    def __init__(self, user, cog=None, channel=None):
        self.user = user
        self.channel = channel or Ch()
        self.response = FakeResponse()
        self.edits = []
        self.client = type("C", (), {"get_cog": staticmethod(lambda _n: cog)})()

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


def make_cog():
    """A Subs cog with every Discord-touching method stubbed out, recording calls."""
    cog = subs.Subs(object())
    cog.state = store.empty_state()
    cog.calls = []

    async def _rec(tag):
        pass

    async def render_board(gid, fallback_channel=None):
        cog.calls.append(("board", None))

    async def bump_board(gid, fallback_channel=None):
        cog.calls.append(("board", None))

    async def post_page(req, *, reason="new", channel=None):
        cog.calls.append(("page", reason, req["id"]))

    async def refresh_page(req):
        cog.calls.append(("refresh", req["id"]))

    async def dm(uid, text):
        cog.calls.append(("dm", uid, text))

    cog.render_board = render_board
    cog.bump_board = bump_board
    cog.post_page = post_page
    cog.refresh_page = refresh_page
    cog._dm_requester = dm
    cog._save = lambda: None
    return cog


# ── 1. fmt_run: several dates in one line ───────────────────────────────────
# "dates", never "nights" — Brian: "some leagues are daytime hours".
check("fmt_run/one date", subs.fmt_run([TUE[0]]), subs.fmt_when(TUE[0]))
check("fmt_run/has a span, a count and one time",
      [("→" in subs.fmt_run(TUE)), ("· 6 dates ·" in subs.fmt_run(TUE)),
       subs.fmt_run(TUE).count("7:45")], [True, True, 1])
check("fmt_run/never says nights", "night" in subs.fmt_run(TUE).lower(), False)
# Mixed start times can't claim a single one (a posting over a rescheduled draw).
check("fmt_run/mixed times drop the time",
      subs.fmt_run([TUE[0], TUE[1].replace("19:45", "18:00")]).endswith("· 2 dates"), True)
check("fmt_run/empty", subs.fmt_run([]), "no dates")


# ── 2. Posting is ONE screen: tick the dates you need ───────────────────────
# The "Repeat / assign…" second page (4/6/8 weeks, rest-of-season, name-the-sub-up-front)
# was built and then removed the same day. Multi-select is the whole feature; naming who
# covers the dates is Fill-for's job, offered on the alert. Don't re-add a second page.
flow = subs.NeedSubFlowView([LEAGUE], {"requests": []}, ANN.id)
flow.league_id = "999"
check("flow/nothing picked is not ready", flow.ready(), False)
flow.game_isos = [TUE[0], TUE[2]]
check("flow/the ticked dates are the posting", flow.dates(), [TUE[0], TUE[2]])
check("flow/nothing is inferred beyond them", len(flow.dates()), 2)
check("flow/ticked is ready", flow.ready(), True)

items = [type(i).__name__ for i in flow.build().children]
check("flow/one screen, five controls", items,
      ["LeagueSelect", "TeamSelect", "GameSelect", "SpotsSelect", "PostNeedButton"])
check("flow/no second page survives", [n for n in items if n in
      ("MoreOptionsButton", "BackButton", "RunSelect", "RunMemberSelect")], [])
check("flow/no run helpers survive",
      [n for n in ("run_from", "RUN_REST", "RUN_WEEK_CHOICES", "RunSelect",
                   "RunMemberSelect", "MoreOptionsButton", "BackButton")
       if hasattr(subs, n)], [])
check("flow/post button counts the DATES",
      [i.label for i in flow.build().children if isinstance(i, subs.PostNeedButton)],
      ["Post 2 dates"])
flow.game_isos = [TUE[0]]
check("flow/one date is a plain request",
      [i.label for i in flow.build().children if isinstance(i, subs.PostNeedButton)],
      ["Post request"])
check("flow/our own copy says dates, not nights",
      "night" in flow.prompt().split("League:")[0].lower()
      or "night" in flow.prompt().rsplit("**", 1)[-1].lower(), False)


# ── 3. add_series: one request per night, one alert, optional named sub ─────
async def series_posts_one_request_per_night():
    cog = make_cog()
    made, skipped, filled = await cog.add_series(
        requester=ANN, league_id="999", league="Tuesday Night League",
        team="Smith", game_isos=TUE[:4], spots=1, channel=Ch())
    check("series/created one per night", (made, skipped, filled), (4, 0, 0))
    reqs = cog.state["requests"]
    check("series/all share one id", len({r["series_id"] for r in reqs}), 1)
    check("series/id is not empty", bool(reqs[0]["series_id"]), True)
    check("series/one date each", sorted(r["game_ts"] for r in reqs), TUE[:4])
    # ONE alert for the whole run — eight pings for eight Tuesdays is how a useful
    # board becomes a muted one.
    check("series/exactly one alert", [c for c in cog.calls if c[0] == "page"],
          [("page", "new", reqs[0]["id"])])


async def single_night_is_not_a_series():
    cog = make_cog()
    made, _, _ = await cog.add_series(
        requester=ANN, league_id="999", league="L", team="Smith",
        game_isos=[TUE[0]], spots=2, channel=Ch())
    check("series/single night made", made, 1)
    check("series/single night has no series id", cog.state["requests"][0]["series_id"], "")
    check("series/single night keeps spots", cog.state["requests"][0]["spots_needed"], 2)


async def a_run_lays_over_a_night_already_posted():
    """Week 1 was posted by hand last week; a later multi-date post covering the same
    stretch must not double-book it."""
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=[TUE[0]], spots=1, channel=Ch())
    cog.calls.clear()
    made, skipped, filled = await cog.add_series(
        requester=ANN, league_id="999", league="L", team="Smith",
        game_isos=TUE[:4], spots=1, channel=Ch())
    check("overlay/skipped the existing date", (made, skipped), (3, 1))
    check("overlay/no duplicate date", len(cog.state["requests"]), 4)
    check("overlay/existing date untouched by the new series id",
          cog.state["requests"][0]["series_id"], "")


async def a_run_over_an_entirely_posted_stretch_reports_nothing_made():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:2], spots=1, channel=Ch())
    made, skipped, filled = await cog.add_series(
        requester=ANN, league_id="999", league="L", team="Smith",
        game_isos=TUE[:2], spots=1, channel=Ch())
    check("overlay/all skipped", (made, skipped, filled), (0, 2, 0))


# ── 4. claim_series: one tap covers the run ─────────────────────────────────
async def one_tap_covers_the_run():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:4], spots=1, channel=Ch())
    sid = cog.state["requests"][0]["series_id"]
    took, skipped = await cog.claim_series(BEN, sid, Ch())
    check("claim/took every night", (len(took), skipped), (4, 0))
    check("claim/nothing left open",
          [store.open_spots(r) for r in cog.state["requests"]], [0] * 4)
    # Second tap by the same person is a no-op, not a double-fill.
    took2, skipped2 = await cog.claim_series(BEN, sid, Ch())
    check("claim/second tap takes nothing", (took2, skipped2), ([], 4))
    check("claim/requester is told once",
          len([c for c in cog.calls if c[0] == "dm"]), 1)


async def claiming_a_run_skips_what_it_should():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:4], spots=1, channel=Ch())
    reqs = store.requests_sorted(cog.state)
    sid = reqs[0]["series_id"]
    # Cy already has week 2.
    store.add_sub(reqs[1], CY.id, CY.display_name, now=NOW)
    took, skipped = await cog.claim_series(BEN, sid, Ch())
    check("claim/partial cover is fine", (len(took), skipped), (3, 1))
    check("claim/didn't bump Cy",
          [f["name"] for f in reqs[1]["filled"]], ["Cy Park"])
    # The requester can't sub their own run.
    cog2 = make_cog()
    await cog2.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                          game_isos=TUE[:3], spots=1, channel=Ch())
    took, skipped = await cog2.claim_series(ANN, cog2.state["requests"][0]["series_id"], Ch())
    check("claim/not your own run", (took, skipped), ([], 3))


async def a_run_alert_speaks_for_the_whole_run():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:4], spots=2, channel=Ch())
    anchor = store.requests_sorted(cog.state)[0]
    body = cog._page_body(anchor, reason="new")
    check("alert/aggregates the whole posting", "8 spots open in total" in body, True)
    check("alert/says the date count once, not twice", body.count("4 dates"), 1)
    check("alert/names the span", subs.fmt_run(TUE[:4]) in body, True)
    view = cog._page_view(anchor)
    # Take them all · take some · and record who's covering them — the last one is how
    # a sub gets named now that posting doesn't ask.
    check("alert/take all, take some, fill for someone",
          [type(c).__name__ for c in view.children],
          ["SeriesClaimButton", "RunPickButton", "FillForButton"])
    check("alert/buttons are labelled in DATES",
          [c.item.label for c in view.children],
          ["Take 4 dates", "I'll take some…", "Fill for someone"])
    # The Fill-for button carries this posting's id, so it opens pre-aimed at it.
    check("alert/fill-for is pre-aimed at this posting",
          [c.item.custom_id for c in view.children if isinstance(c, subs.FillForButton)],
          [f"sub:fillfor:run:{anchor['series_id']}"])
    # A reminder is about ONE night — the one starting in a few hours — never the run.
    rbody = cog._page_body(anchor, reason="reminder")
    check("alert/reminder is single-night", "across the run" in rbody, False)
    check("alert/reminder names its own night", subs.fmt_when(anchor["game_ts"]) in rbody, True)
    rview = cog._page_view(anchor, "reminder")
    check("alert/reminder is a plain claim plus fill-for",
          [type(c).__name__ for c in rview.children], ["PageClaimButton", "FillForButton"])
    check("alert/single-date fill-for is pre-aimed too",
          [c.item.custom_id for c in rview.children if isinstance(c, subs.FillForButton)],
          [f"sub:fillfor:one:{anchor['id']}"])


async def a_runs_alert_survives_its_first_night_filling():
    """Covering week 1 must not retire the ask for the other three."""
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:4], spots=1, channel=Ch())
    reqs = store.requests_sorted(cog.state)
    store.add_sub(reqs[0], CY.id, CY.display_name, now=NOW)
    check("alert/still open on the anchor's own count", store.open_spots(reqs[0]), 0)
    check("alert/run still has open nights", len(cog._series_open(reqs[0])), 3)
    check("alert/keeps the multi-date buttons while any date is open",
          [type(c).__name__ for c in cog._page_view(reqs[0]).children],
          ["SeriesClaimButton", "RunPickButton", "FillForButton"])
    # The picker offers only what's still takeable — the filled week 1 is gone from it.
    picker = subs.NightPickView(cog.state, reqs[0]["series_id"])
    check("alert/picker drops the night that filled",
          [r["game_ts"] for r in picker.nights()], [r["game_ts"] for r in reqs[1:]])


async def a_played_night_hands_the_alert_on():
    """The run's page lives on its soonest open night. When that night is played and
    pruned, the alert must move to the next one or the rest of the run goes quiet."""
    cog = make_cog()
    yesterday = (NOW - timedelta(days=1)).replace(hour=19, minute=45,
                                                  second=0, microsecond=0).isoformat()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=[yesterday] + TUE[:2], spots=1, channel=Ch())
    old = next(r for r in cog.state["requests"] if r["game_ts"] == yesterday)
    old["alert"] = {"channel_id": 1, "message_id": 5}
    cog._resolve_channel = lambda cid: asyncio.sleep(0, result=None)
    cog.render_all_boards = lambda: asyncio.sleep(0)
    cog.calls.clear()
    await subs.Subs.expiry_loop.coro(cog)
    check("expiry/played night is gone",
          [r["game_ts"] for r in cog.state["requests"]], TUE[:2])
    check("expiry/alert handed to the next night",
          [c for c in cog.calls if c[0] == "page"],
          [("page", "bump", store.requests_sorted(cog.state)[0]["id"])])


# ── 5. Editing the spot count on a live request ─────────────────────────────
def a_request(spots=1, filled=(), ts=None):
    st = store.empty_state()
    r = store.new_request(st, requester_id=ANN.id, requester_name=ANN.display_name,
                          game_ts=ts or TUE[0], spots_needed=spots, league_id="999",
                          league="Tuesday Night League", team="Smith", now=NOW)
    for u in filled:
        store.add_sub(r, u.id, u.display_name, now=NOW)
    return st, r


st, r = a_request(spots=1, filled=[BEN])
check("spots/covered counts pending too", store.covered(r), 1)
check("spots/raise", (store.set_spots(r, 3), r["spots_needed"], store.open_spots(r)), ("ok", 3, 2))
check("spots/same number is a no-op", store.set_spots(r, 3), "unchanged")
check("spots/lower to exactly the people on it", store.set_spots(r, 1), "ok")
check("spots/can't go below them", (store.set_spots(r, 0), r["spots_needed"]), ("too_low", 1))
_, r2 = a_request(spots=2, filled=[BEN, CY])
check("spots/never bumps a listed sub", (store.set_spots(r2, 1), r2["spots_needed"]),
      ("too_low", 2))


async def anyone_may_raise_only_the_owner_may_lower():
    cog = make_cog()
    cog.state, req = a_request(spots=1, filled=[BEN])
    # Brian's case: one request, one sub on it, now two more are needed.
    result, out = await cog.set_request_spots(CY, req["id"], 3, channel=Ch())
    check("edit/a teammate may raise it", (result, out["spots_needed"], store.open_spots(out)),
          ("ok", 3, 2))
    check("edit/Ben keeps his spot", [f["name"] for f in out["filled"]], ["Ben Ross"])
    # New spots deserve a fresh shout — nobody re-reads an alert that said "covered".
    check("edit/re-pings the room", [c for c in cog.calls if c[0] == "page"],
          [("page", "bump", req["id"])])
    check("edit/owner is told", [c[0] for c in cog.calls].count("dm"), 1)

    cog.calls.clear()
    result, _ = await cog.set_request_spots(CY, req["id"], 2, channel=Ch())
    check("edit/a teammate may not lower it", result, "too_low")
    check("edit/nothing changed", cog.state["requests"][0]["spots_needed"], 3)
    result, _ = await cog.set_request_spots(ANN, req["id"], 2, channel=Ch())
    check("edit/the owner may lower it", (result, cog.state["requests"][0]["spots_needed"]),
          ("ok", 2))
    check("edit/no re-ping when spots shrink", [c for c in cog.calls if c[0] == "page"], [])


async def editing_re_arms_the_pre_game_reminder():
    """A request that already fired its T-24h reminder while it was covered would
    never chase the spots that just opened."""
    cog = make_cog()
    cog.state, req = a_request(spots=1, filled=[BEN])
    req["reminded"] = True
    await cog.set_request_spots(ANN, req["id"], 3, channel=Ch())
    check("edit/reminder re-armed", req["reminded"], False)
    cog.calls.clear()
    await cog.set_request_spots(ANN, req["id"], 3, channel=Ch())
    check("edit/no-op does nothing", [c for c in cog.calls], [])


async def a_locked_game_cannot_be_edited():
    cog = make_cog()
    soon = (NOW + timedelta(minutes=5)).isoformat()
    cog.state, req = a_request(spots=1, ts=soon)
    check("edit/locked", (await cog.set_request_spots(ANN, req["id"], 3, channel=Ch()))[0], "locked")
    check("edit/gone", (await cog.set_request_spots(ANN, "deadbeef", 3, channel=Ch()))[0], "closed")


# The flow turns into an editor when the night already has an open request — the
# duplicate that used to be a dead end.
st, req = a_request(spots=1, filled=[BEN])
edit_flow = subs.NeedSubFlowView([LEAGUE], st, ANN.id)
edit_flow.league_id = "999"
edit_flow.team = "Smith"
edit_flow.game_isos = [TUE[0]]
check("editflow/spots this the existing request", edit_flow.existing() is not None, True)
check("editflow/defaults to what it asks now", edit_flow.spots, 1)
items = {type(i).__name__: i for i in edit_flow.build().children}
check("editflow/spots select says TOTAL", items["SpotsSelect"].placeholder, "Total spots needed…")
check("editflow/button is an update", items["PostNeedButton"].label, "Update spots")
check("editflow/an edit is still one screen", "MoreOptionsButton" in items, False)
check("editflow/prompt explains itself",
      "keeps their place" in edit_flow.prompt() and "1/1" in edit_flow.prompt(), True)
# Two nights is a run, not an edit — a run lays over what's there rather than editing it.
edit_flow.game_isos = [TUE[0], TUE[1]]
check("editflow/multi-night is never an edit", edit_flow.existing(), None)
check("editflow/multi-date posts as normal",
      [i.label for i in edit_flow.build().children if isinstance(i, subs.PostNeedButton)],
      ["Post 2 dates"])
# A DIFFERENT team on the same night is a different request, not an edit.
edit_flow.game_isos = [TUE[0]]
edit_flow.team = "Alvarez"
check("editflow/other team is its own request", edit_flow.existing(), None)


# ── 6b. The night picker's wording follows the FLOW, not the multi flag ──────
# Brian: "'Games you can cover' shows up when I open a sub request." Going
# multi-select in the need-a-sub flow handed the requester the availability flow's
# placeholder — opposite person, opposite meaning.
need = subs.NeedSubFlowView([LEAGUE], {"requests": []}, ANN.id)
need.league_id = "999"
need_ph = next(i for i in need.build().children if isinstance(i, subs.GameSelect)).placeholder
avail = subs.AvailFlowView([LEAGUE], BEN.id, {"requests": []})
avail.league_id = "999"
avail_ph = next(i for i in avail.build().children if isinstance(i, subs.GameSelect)).placeholder
check("wording/requester is not asked what they can cover", "cover" in need_ph.lower(), False)
check("wording/requester is asked when they need one", need_ph,
      "Which date — tick as many as you need…")
check("wording/and never in 'nights'", "night" in need_ph.lower(), False)
check("wording/the sub still is asked what they can cover", avail_ph, "Games you can cover…")
check("wording/the two flows never share a placeholder", need_ph == avail_ph, False)


# ── 6. Old stores keep working ──────────────────────────────────────────────
import json
legacy = os.path.join(tempfile.mkdtemp(), "legacy.json")
with open(legacy, "w", encoding="utf-8") as fh:
    json.dump({"requests": [{"id": "abc", "kind": "sub", "requester_id": 1,
                             "requester_name": "Ann", "game_ts": TUE[0], "spots_needed": 1}],
               "availability": []}, fh)
loaded = store.load(legacy)
check("legacy/gets a series_id", loaded["requests"][0]["series_id"], "")
check("legacy/is not a series", store.series_requests(loaded, ""), [])


# ── 7. The board lists NIGHTS, not runs ─────────────────────────────────────
# Brian: "what if Bruce is covering 5 weeks, but one week he can't suddenly? His run is
# broken. Don't look at them as runs. They're just subbing opportunities and the input
# form let us mass-fill them. Each one can change after that."

async def the_board_lists_nights_not_runs():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="Tuesday Night League",
                         team="Smith", game_isos=TUE[:6], spots=1, channel=Ch())
    kinds = [type(c).__name__ for c in subs.build_view(cog.state).children]
    check("board/no run button — one per night", "RunPickButton" in kinds, False)
    # TUE[0] and TUE[1] are within 14 days; TUE[2:] are not.
    check("board/a button per night inside the horizon",
          [c.item.label for c in subs.build_view(cog.state).children
           if isinstance(c, subs.PageClaimButton)],
          [f"{subs.fmt_when_short(t)} Smith" for t in TUE[:2]])
    check("board/text matches the buttons line for line",
          subs.build_embed(cog.state).description.count("Team Smith"), 2)


async def a_dropped_night_rejoins_as_its_own_opportunity():
    """The whole point of storing nights separately: one person pulling out of one week
    changes that week and nothing else."""
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:5], spots=1, channel=Ch())
    # Posting no longer names anyone — Fill-for does, which is what the alert's own
    # "Fill for someone" button is for.
    await cog.fill_nights_for(ANN, [r["id"] for r in store.requests_sorted(cog.state)],
                              BEN, Ch())
    check("drop/Ben starts on all five",
          [len(r["filled"]) for r in store.requests_sorted(cog.state)], [1] * 5)
    # Covered nights get no claim button — but they're still worth being able to LOOK
    # at, which is exactly what Brian's 16 covered Tuesdays are for.
    kinds = [type(c).__name__ for c in subs.build_view(cog.state).children]
    check("drop/nothing open, so no game buttons", "PageClaimButton" in kinds, False)
    check("drop/covered nights past the horizon are still reachable",
          "ShowAllButton" in kinds, True)
    # The reopened night is inside the horizon, so it's shown outright — the tail's
    # "still need a sub" count is only ever about the games it is NOT showing.
    check("drop/the reopened date is shown, not deferred",
          "still need a sub" in subs.build_embed(cog.state).description, False)

    # Ben can't make week 2 after all.
    week2 = store.requests_sorted(cog.state)[1]
    result, req = await cog.remove_sub_by_anyone(ANN, week2["id"], BEN.id, Ch())
    check("drop/removed", result, "removed")
    check("drop/only that night reopened",
          [store.open_spots(r) for r in store.requests_sorted(cog.state)], [0, 1, 0, 0, 0])
    check("drop/Ben still on the other four",
          [f["name"] for r in store.requests_sorted(cog.state) for f in r["filled"]],
          ["Ben Ross"] * 4)
    check("drop/it comes back as one ordinary opportunity",
          [c.item.label for c in subs.build_view(cog.state).children
           if isinstance(c, subs.PageClaimButton)],
          [f"{subs.fmt_when_short(TUE[1])} Smith"])
    # And the bulk pickers see a single game, not a broken run.
    check("drop/bulk picker sees a single, not a run",
          [(u["kind"], len(u["reqs"])) for u in subs.open_units(cog.state)], [("one", 1)])


async def the_board_stops_at_the_horizon_and_says_so():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:6], spots=1, channel=Ch())
    e = subs.build_embed(cog.state)
    check("horizon/only the near term is listed",
          [subs.fmt_when(t) in e.description for t in TUE],
          [True, True, False, False, False, False])
    check("horizon/the rest is one honest line",
          f"…and **4** more games through {subs.fmt_when_short(TUE[5])}" in e.description, True)
    check("horizon/no extra commentary in the tail",
          "still need a sub" in e.description, False)
    check("horizon/and it points at the way to see them",
          e.description.rstrip().endswith("tap **Show all**."), True)
    check("horizon/footer says how far it goes",
          "showing the next 14 days" in e.footer.text, True)
    check("horizon/board offers Show all",
          "ShowAllButton" in [type(c).__name__ for c in subs.build_view(cog.state).children], True)

    # Show all = the same board with the horizon lifted. Nothing is hidden, only moved.
    full = subs.build_embed(cog.state, horizon_days=None)
    check("showall/every night is there",
          all(subs.fmt_when(t) in full.description for t in TUE[:6]), True)
    check("showall/no tail line", "more game" in full.description, False)
    check("showall/footer says so", "showing everything" in full.footer.text, True)
    fullview = [type(c).__name__ for c in subs.build_view(cog.state, horizon_days=None).children]
    check("showall/a button for every night", fullview.count("PageClaimButton"), 6)
    check("showall/no dead Show all button on the full copy",
          "ShowAllButton" in fullview, False)


async def no_show_all_button_when_nothing_is_hidden():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:2], spots=1, channel=Ch())
    check("horizon/nothing beyond it, so no button",
          "ShowAllButton" in [type(c).__name__ for c in subs.build_view(cog.state).children],
          False)
    check("horizon/and no tail line", "more game" in subs.build_embed(cog.state).description, False)
    # Availability for a far-off night counts as something to reveal, even with no request.
    await cog.add_availability(user=CY, league_id="999", league="L", games=[TUE[5]], channel=Ch())
    check("horizon/a far-off availability also earns the button",
          "ShowAllButton" in [type(c).__name__ for c in subs.build_view(cog.state).children],
          True)

async def the_night_picker_ticks_everything_and_takes_a_subset():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:5], spots=1, channel=Ch())
    sid = cog.state["requests"][0]["series_id"]
    view = subs.NightPickView(cog.state, sid)
    check("picker/all nights ticked by default", len(view.rids), 5)
    sel = next(c for c in view.children if isinstance(c, subs.NightSelect))
    check("picker/every option is pre-selected", [o.default for o in sel.options], [True] * 5)
    check("picker/is multi-select", (sel.min_values, sel.max_values), (1, 5))
    btn = next(c for c in view.children if isinstance(c, subs.TakeNightsButton))
    check("picker/button counts the ticked dates", btn.label, "Take 5 dates")
    check("picker/prompt names the span", subs.fmt_run(TUE[:5]) in view.prompt(), True)

    # Untick down to two — "I can do the first two but not the rest".
    two = [r["id"] for r in store.requests_sorted(cog.state)[:2]]
    took, skipped = await cog.claim_nights(BEN, two, Ch())
    check("picker/took exactly the two picked", took, TUE[:2])
    check("picker/left the rest alone",
          (skipped, [store.open_spots(r) for r in store.requests_sorted(cog.state)]),
          (0, [0, 0, 1, 1, 1]))
    check("picker/one DM for the lot, not one per night",
          len([c for c in cog.calls if c[0] == "dm"]), 1)
    check("picker/what's left is still one run",
          [(u["kind"], len(u["reqs"])) for u in subs.open_units(cog.state)], [("run", 3)])


async def fill_for_someone_has_the_same_reach():
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:4], spots=1, channel=Ch())
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Alvarez",
                         game_isos=[TUE[0]], spots=1, channel=Ch())
    v = subs.FillForView(cog.state)
    check("fillfor/starts on the game picker",
          [type(c).__name__ for c in v.children], ["FillForPick"])
    pick = v.children[0]
    check("fillfor/a multi-date posting is ONE option", [o.label for o in pick.options],
          ["Smith · 4 dates", f"{subs.fmt_when_short(TUE[0])} Alvarez"])
    check("fillfor/run option shows the span and total open",
          pick.options[0].description, f"{subs.fmt_run(TUE[:4])} · 4 open")

    # Choosing the run defaults to ALL its nights and adds the night select.
    v.key = pick.options[0].value
    v.on_unit_change()
    check("fillfor/whole run ticked by default", len(v.rids), 4)
    check("fillfor/multi-date gets a date select",
          [type(c).__name__ for c in v.build().children],
          ["FillForPick", "NightSelect", "FillForMemberSelect", "FillForSubmitButton"])
    # A single game skips straight to the member — no night select to wade through.
    v.key = pick.options[1].value
    v.on_unit_change()
    check("fillfor/single game has no date select",
          [type(c).__name__ for c in v.build().children],
          ["FillForPick", "FillForMemberSelect", "FillForSubmitButton"])

    # Put Ben on three of the four.
    three = [r["id"] for r in store.requests_sorted(cog.state) if r["team"] == "Smith"][:3]
    filled, skipped, why = await cog.fill_nights_for(ANN, three, BEN, Ch())
    check("fillfor/filled the three picked", (filled, skipped), (TUE[:3], 0))
    check("fillfor/Ben is on each",
          [f["name"] for r in store.requests_sorted(cog.state) if r["id"] in three
           for f in r["filled"]], ["Ben Ross"] * 3)
    check("fillfor/no DM to yourself", len([c for c in cog.calls if c[0] == "dm"]), 0)


async def fill_for_opens_pre_aimed_from_an_alert():
    """The alert is already about one specific ask — making someone re-find it in a
    dropdown is a step for nothing. Also the ONLY way to name a sub up front now that
    posting doesn't ask."""
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:4], spots=1, channel=Ch())
    await cog.add_series(requester=CY, league_id="999", league="L", team="Alvarez",
                         game_isos=[TUE[0]], spots=1, channel=Ch())
    sid = store.requests_sorted(cog.state)[0]["series_id"]

    v = subs.FillForView(cog.state, key=f"run:{sid}")
    check("prefill/lands on the right posting", v.unit()["sid"], sid)
    check("prefill/all its dates ticked", len(v.rids), 4)
    check("prefill/straight to the member picker",
          [type(c).__name__ for c in v.build().children],
          ["FillForPick", "NightSelect", "FillForMemberSelect", "FillForSubmitButton"])
    check("prefill/and it's the one selected in the dropdown",
          [o.default for o in v.build().children[0].options], [True, False])

    single = [r for r in store.requests_sorted(cog.state) if r["team"] == "Alvarez"][0]
    v1 = subs.FillForView(cog.state, key=f"one:{single['id']}")
    check("prefill/single date needs no date picker",
          [type(c).__name__ for c in v1.build().children],
          ["FillForPick", "FillForMemberSelect", "FillForSubmitButton"])
    check("prefill/aimed at that one request", v1.rids, [single["id"]])

    # A stale alert (its spot filled while the message sat there) must not explode —
    # it just falls back to the ordinary picker.
    v2 = subs.FillForView(cog.state, key="one:deadbeef")
    check("prefill/stale key falls back to the picker", (v2.unit(), v2.rids), (None, []))
    check("prefill/still usable",
          [type(c).__name__ for c in v2.build().children], ["FillForPick"])

    # The board's bare button and the alert's keyed one are the same class.
    import re
    tpl = subs.FillForButton.__discord_ui_template__ if hasattr(
        subs.FillForButton, "__discord_ui_template__") else None
    for cid, want in ((subs.CID_FILLFOR, ""), (f"sub:fillfor:run:{sid}", f"run:{sid}")):
        m = re.fullmatch(r"sub:fillfor(?::(?P<kind>run|one):(?P<ident>[0-9a-f]+))?", cid)
        check(f"prefill/custom_id parses: {cid[:20]}", m is not None, True)
        got = f"{m['kind']}:{m['ident']}" if m and m["kind"] else ""
        check(f"prefill/round-trips to {want or '(bare)'}", got, want)


async def picking_a_name_does_not_fill_anything():
    """Brian: "One mistake, and no undo." The member select used to BE the action, so a
    mis-tap put the wrong person on someone else's game. Nothing is recorded until the
    green button, like every other form in this cog."""
    cog = make_cog()
    await cog.add_series(requester=ANN, league_id="999", league="L", team="Smith",
                         game_isos=TUE[:3], spots=1, channel=Ch())
    v = subs.FillForView(cog.state)
    v.key = v.children[0].options[0].value
    v.on_unit_change()

    def submit(view):   # None-safe: a missing button should FAIL, not crash the suite
        return next((c for c in view.build().children
                     if isinstance(c, subs.FillForSubmitButton)), None)

    btn = submit(v)
    check("submit/the form has a submit button", btn is not None, True)
    if btn is None:
        return
    check("submit/disabled until someone is picked", btn.disabled, True)
    check("submit/neutral label while empty", btn.label, "Mark them in")

    # Selecting a name only records the choice — the store must be untouched.
    v.member = BEN
    check("submit/nothing filled by the pick alone",
          [len(r["filled"]) for r in store.requests_sorted(cog.state)], [0, 0, 0])
    btn = submit(v)
    check("submit/enabled once a name is set", btn.disabled, False)
    check("submit/label names the person and the count", btn.label, "Mark Ben in for 3 dates")
    check("submit/prompt says nothing is recorded yet",
          "Press the green button" in v.prompt(), True)
    # The select shows who's picked, so a rebuilt view can't look empty and invite a
    # second, accidental pick.
    sel = next((c for c in v.build().children
                if isinstance(c, subs.FillForMemberSelect)), None)
    check("submit/the pick is visible after a rebuild",
          [d.id for d in sel.default_values] if sel else None, [BEN.id])

    # One date → singular label, no count, and the prompt tracks the real selection.
    v.rids = v.rids[:1]
    check("submit/singular label", submit(v).label, "Mark Ben in")
    check("submit/prompt counts what's actually ticked",
          "**1 of 3** dates ticked" in v.prompt(), True)
    check("submit/and stops claiming they all are", "All 3 dates are ticked" in v.prompt(), False)

    # Only the button commits it.
    v.rids = [r["id"] for r in store.requests_sorted(cog.state)]
    filled, skipped, why = await cog.fill_nights_for(ANN, v.rids, BEN, Ch())
    check("submit/the button's call is what fills", (len(filled), skipped), (3, 0))
    check("submit/now they're on", [len(r["filled"]) for r in store.requests_sorted(cog.state)],
          [1, 1, 1])


async def clearing_your_availability_asks_first():
    """The last select in ➖ Remove that still acted on touch. Removing a sub and
    cancelling a request both confirm; a stray tap here silently un-volunteers you from
    a game people may already be counting on you for."""
    cog = make_cog()
    await cog.add_availability(user=BEN, league_id="999", league="Tuesday League 9/1 – 10/6",
                               games=[TUE[0], TUE[1]], channel=Ch())
    await cog.add_availability(user=BEN, league_id="888", league="Thursday League",
                               games=[], channel=Ch())
    check("rmavail/two listings", len(cog.state["availability"]), 2)

    home = subs.RemoveHomeView(cog.state, BEN.id)
    sel = next((c for c in home.children if isinstance(c, subs.RemoveAvailSelect)), None)
    check("rmavail/the picker is there", sel is not None, True)

    # Picking must OPEN a confirm, not delete.
    sel._values = ["999"]
    it = FakeInteraction(BEN, cog)
    await sel.callback(it)
    check("rmavail/picking deletes nothing", len(cog.state["availability"]), 2)
    # .get() throughout: a regression here should FAIL cleanly, not crash the suite
    # before the other checks get to print.
    kind, kw = it.response.calls[0] if it.response.calls else ("(nothing)", {})
    check("rmavail/it opens a confirm instead", kind, "edit_message")
    content = kw.get("content", "")
    check("rmavail/named in the question",
          "Tuesday League 9/1 – 10/6" in content and "Clear your availability" in content, True)
    confirm = kw.get("view")
    check("rmavail/confirm has clear + keep",
          [c.label for c in confirm.children] if confirm else None, ["Clear it", "Keep"])
    if confirm is None:
        return

    # "Keep" leaves it alone.
    it2 = FakeInteraction(BEN, cog)
    await confirm.children[1].callback(it2)
    check("rmavail/Keep keeps it", len(cog.state["availability"]), 2)
    check("rmavail/and says so",
          "still listed" in it2.response.calls[0][1].get("content", ""), True)

    # "Clear it" is what actually removes — and only that one listing.
    it3 = FakeInteraction(BEN, cog)
    await confirm.children[0].callback(it3)
    check("rmavail/Clear it removes exactly one", len(cog.state["availability"]), 1)
    check("rmavail/the other league survives",
          cog.state["availability"][0]["league_id"], "888")
    check("rmavail/confirmation names it",
          "Tuesday League 9/1 – 10/6" in (it3.edits[-1].get("content", "") if it3.edits else ""),
          True)

    # A listing that vanished between opening the menu and picking it doesn't explode.
    sel._values = ["999"]
    it4 = FakeInteraction(BEN, cog)
    await sel.callback(it4)
    check("rmavail/already gone is handled",
          it4.response.calls[0][1].get("content") if it4.response.calls else None,
          "That listing is already gone.")

    check("rmavail/label reads league + games",
          subs._avail_label({"league": "Tuesday League", "games": [TUE[0]]}),
          f"Tuesday League · {subs.fmt_when_short(TUE[0])}")
    check("rmavail/whole-league listings say any game",
          subs._avail_label({"league": "Thursday League", "games": []}),
          "Thursday League · any game")


async def fill_for_keeps_the_precise_single_night_reasons():
    """The generalised call must not blur "they're already on it" into a bare count."""
    cog = make_cog()
    cog.state, req = a_request(spots=1)
    filled, skipped, why = await cog.fill_nights_for(CY, [req["id"]], ANN, Ch())
    check("fillfor/can't sub your own request", (filled, why), ([], "requester"))
    await cog.fill_nights_for(CY, [req["id"]], BEN, Ch())
    filled, skipped, why = await cog.fill_nights_for(CY, [req["id"]], BEN, Ch())
    check("fillfor/already on it", (filled, why), ([], "already"))
    filled, skipped, why = await cog.fill_nights_for(CY, [req["id"]], DEE, Ch())
    check("fillfor/no spots left", (filled, why), ([], "full"))
    filled, skipped, why = await cog.fill_nights_for(CY, ["deadbeef"], DEE, Ch())
    check("fillfor/gone", (filled, why), ([], "closed"))


for fn in (series_posts_one_request_per_night, single_night_is_not_a_series,
           a_run_lays_over_a_night_already_posted,
           a_run_over_an_entirely_posted_stretch_reports_nothing_made,
           one_tap_covers_the_run,
           claiming_a_run_skips_what_it_should, a_run_alert_speaks_for_the_whole_run,
           a_runs_alert_survives_its_first_night_filling, a_played_night_hands_the_alert_on,
           anyone_may_raise_only_the_owner_may_lower, editing_re_arms_the_pre_game_reminder,
           a_locked_game_cannot_be_edited,
           the_board_lists_nights_not_runs,
           a_dropped_night_rejoins_as_its_own_opportunity,
           the_board_stops_at_the_horizon_and_says_so,
           no_show_all_button_when_nothing_is_hidden,
           the_night_picker_ticks_everything_and_takes_a_subset,
           fill_for_someone_has_the_same_reach,
           fill_for_opens_pre_aimed_from_an_alert,
           picking_a_name_does_not_fill_anything,
           clearing_your_availability_asks_first,
           fill_for_keeps_the_precise_single_night_reasons):
    asyncio.run(fn())

print("\n".join(f"FAIL: {f}" for f in FAILS) or "All series + spot-edit checks passed.")
raise SystemExit(1 if FAILS else 0)
