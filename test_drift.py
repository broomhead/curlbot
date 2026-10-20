"""Unit tests for drift: a posted request the league page no longer agrees with.

  1. request_drift reads what changed — a team gone from the roster, a date gone
     from the schedule, the team now on bye, a moved start time — and reads NOTHING
     into a page it couldn't fully read.
  2. The housekeeping pass flags each mismatch once, tells the people involved (one
     DM each, however much changed), applies a moved time, and clears silently when
     the mismatch goes away. Nothing is ever deleted by it.
  3. Re-picking the team clears the flag; cancelling tells everyone on the request;
     a server manager can cancel a request its owner can't.

Run:  python3 test_drift.py     (no network; needs discord.py + bs4 + aiohttp + dotenv)

Same harness rules as test_standing.py: neuter discord.Client.run and point
SUBS_STORE_PATH at a scratch file BEFORE importing subs.
"""
import asyncio
import json
import os
import tempfile
from datetime import timedelta

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
    """seq[i], or a recorded failure and None — a regression must fail, not crash."""
    try:
        return seq[i]
    except (IndexError, KeyError, TypeError):
        check(name, f"missing [{i}]", "present")
        return None


# ── Fixtures ────────────────────────────────────────────────────────────────
NOW = subs.club_now()


def night(n_days, hour=19, minute=45):
    return (NOW.replace(hour=hour, minute=minute, second=0, microsecond=0)
            + timedelta(days=n_days)).isoformat()


THU = [night(7 * i + 4) for i in range(6)]
TEAMS = ["Ashby", "Vance", "Corwin"]


def league(team_names=TEAMS, byes=None, dates=THU, time="7:45 pm", **kw):
    """A league as league_client returns it. `byes` maps a draw index to its byes."""
    byes = byes or {}
    lg = {
        "id": 555, "title": "Thursday League – Fall 2026 League 1 – Begins September 3",
        "day": "Thursday", "time": time,
        "draws": [{"date": iso[:10], "weekday": "Thursday", "time": time,
                   "byes": list(byes.get(i, []))} for i, iso in enumerate(dates)],
        "team_names": list(team_names), "teams": len(team_names),
    }
    lg.update(kw)
    return lg


def req(team="Ashby", when=None, **kw):
    r = {"id": "r1", "team": team, "game_ts": when or THU[1], "league_id": "555",
         "filled": [], "pending": [], "drift": ""}
    r.update(kw)
    return r


def kind(d):
    return d and d.get("kind")


# ── 1. What counts as drift ─────────────────────────────────────────────────
LG = league()
check("drift/all matches", subs.request_drift(req(), LG, NOW), None)
check("drift/team renamed away", kind(subs.request_drift(req(), league(["Ashbury", "Vance", "Corwin"]), NOW)),
      subs.DRIFT_TEAM_GONE)
check("drift/case and spacing are not a rename",
      subs.request_drift(req(team="  ashby "), LG, NOW), None)
check("drift/no roster posted is not a withdrawal",
      subs.request_drift(req(), league([]), NOW), None)
check("drift/a half-parsed standings table judges nothing",
      subs.request_drift(req(), league(["Vance", "Corwin"], teams=3), NOW), None)
check("drift/an unreadable page judges nothing",
      subs.request_drift(req(), league(["Vance"], fetch_failed=True), NOW), None)
check("drift/a league not in the list judges nothing", subs.request_drift(req(), None, NOW), None)
check("drift/teamless request has no team to lose", subs.request_drift(req(team=""), LG, NOW), None)

check("drift/team now on bye that week",
      kind(subs.request_drift(req(), league(byes={1: ["Ashby"]}), NOW)), subs.DRIFT_BYE)
check("drift/someone else's bye is not ours",
      subs.request_drift(req(), league(byes={1: ["Vance"]}), NOW), None)
# The date was dropped from the schedule (a holiday week taken out).
dropped = [THU[0], THU[2], THU[3], THU[4], THU[5]]
check("drift/date taken off the schedule",
      kind(subs.request_drift(req(), league(dates=dropped), NOW)), subs.DRIFT_NO_DRAW)
check("drift/…even with no team named",
      kind(subs.request_drift(req(team=""), league(dates=dropped), NOW)), subs.DRIFT_NO_DRAW)
check("drift/past the last posted draw is a schedule posted part way, not a change",
      subs.request_drift(req(when=THU[5]), league(dates=THU[:3]), NOW), None)
check("drift/no schedule at all judges no dates",
      subs.request_drift(req(), league(dates=[]), NOW), None)
check("drift/a dated sig names the date", (subs.request_drift(req(), league(dates=dropped), NOW) or {}).get("sig"),
      f"no_draw:{THU[1][:10]}")

moved = subs.request_drift(req(), league(time="9:00 pm"), NOW)
check("drift/moved start time is applied, not asked about", kind(moved), subs.DRIFT_TIME)
check("drift/…to the draw's time", (moved or {}).get("iso"), THU[1][:10] + "T21:00:00")
tbc = THU[1][:10] + "T23:59:00"
check("drift/time TBC is retime_tbc's, not ours",
      subs.request_drift(req(when=tbc), league(time="9:00 pm"), NOW), None)
two = league()
two["draws"].append({"date": THU[1][:10], "weekday": "Thursday", "time": "9:30 pm", "byes": []})
check("drift/two draw times that date: which is ours isn't on the page",
      subs.request_drift(req(when=THU[1][:10] + "T18:00:00"), two, NOW), None)
check("drift/played games are expiry's", subs.request_drift(
    req(when=night(-7)), league(["Vance"], dates=[night(-7)] + THU), NOW), None)
soon = (NOW + timedelta(minutes=10)).replace(second=0, microsecond=0).isoformat()
check("drift/a locked roster is left alone",
      subs.request_drift(req(when=soon), league(["Vance"]), NOW), None)

g = {"team": "Ashby", "league_id": "555"}
check("standing/renamed team", kind(subs.standing_drift(g, league(["Ashbury"]), NOW)), subs.DRIFT_TEAM_GONE)
check("standing/still there", subs.standing_drift(g, LG, NOW), None)
check("standing/league-wide arrangement has no team to lose",
      subs.standing_drift({"team": "", "league_id": "555"}, league(["Ashbury"]), NOW), None)
check("standing/a finished season is not drift",
      subs.standing_drift(g, league(["Ashbury"], ended=True), NOW), None)

check("needs_team/teamless", subs.needs_team({"team": ""}), True)
check("needs_team/named, fine", subs.needs_team({"team": "Ashby", "drift": ""}), False)
check("needs_team/renamed away", subs.needs_team({"team": "Ashby", "drift": "team_gone:ashby"}), True)
check("needs_team/on bye: maybe the wrong team was picked",
      subs.needs_team({"team": "Ashby", "drift": "bye:x:ashby"}), True)
check("needs_team/dropped date: no team fixes that",
      subs.needs_team({"team": "Ashby", "drift": "no_draw:x"}), False)

st = {"requests": [req("Ashby"), req("Vance")], "standing": []}
check("wholesale/every stored team gone at once is the page, not renames",
      subs.wholesale_team_loss(st, league(["ASHBY FC", "VANCE FC", "CORWIN FC"])), True)
check("wholesale/one rename among others is a rename",
      subs.wholesale_team_loss(st, league(["Ashbury", "Vance", "Corwin"])), False)
check("wholesale/a single stored team going is a rename",
      subs.wholesale_team_loss({"requests": [req("Ashby")], "standing": []}, league(["Ashbury"])), False)

# The store tolerates files written before the field existed.
path = os.path.join(tempfile.mkdtemp(), "old.json")
with open(path, "w") as f:
    json.dump({"requests": [{"id": "x"}], "standing": [{"user_id": 1}]}, f)
old = store.load(path)
check("store/old request gets drift", old["requests"][0].get("drift"), "")
check("store/old arrangement gets drift", old["standing"][0].get("drift"), "")
check("store/new request starts clean", store.new_request(store.empty_state(), requester_id=1,
      requester_name="A", spots_needed=1).get("drift"), "")

# The board flags it — shown, not hidden.
line = subs._req_status_line({"team": "Ashby", "spots_needed": 1, "filled": [], "pending": [],
                              "drift": "team_gone:ashby"})
check("board/flag on the spot", "⚠️ team isn't on the league page" in line, True)
check("board/no flag when clean", "⚠️" in subs._req_status_line(
    {"team": "Ashby", "spots_needed": 1, "filled": [], "pending": [], "drift": ""}), False)


# ── Cog harness ─────────────────────────────────────────────────────────────
class U:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name


class Ch:
    def __init__(self, cid=1):
        self.id = cid
        self.guild = None

    async def send(self, content=None, **kw):
        return type("M", (), {"id": 42})()


ANN, BO, ROBIN, ALEX = U(1, "Ann Lee"), U(2, "Bo Brooks"), U(3, "Robin Vale"), U(4, "Alex Reed")
COGS = []


def make_cog():
    cog = subs.Subs(object())
    COGS.append(cog)
    cog.state = store.empty_state()
    cog.calls, cog.dms = [], []

    async def noop(*a, **k):
        cog.calls.append(("board",))

    async def refresh_page(r):
        cog.calls.append(("refresh", r["id"]))

    async def post_page(r, *, reason="new", channel=None):
        cog.calls.append(("page", reason, r["id"]))

    async def dm(uid, text, view=None):
        cog.dms.append((uid, text, view))
        return True

    async def dm_requester(uid, text):
        cog.dms.append((uid, text, None))

    cog.render_board = cog.bump_board = cog.render_all_boards = noop
    cog.refresh_page, cog.post_page = refresh_page, post_page
    cog._dm, cog._dm_requester = dm, dm_requester
    cog._save = lambda: None
    return cog


def per_person(cog):
    out = {}
    for uid, _, _ in cog.dms:
        out[uid] = out.get(uid, 0) + 1
    return out


async def post(cog, who, dates, team="Ashby", lg=LG):
    await cog.add_series(requester=who, league_id="555", league=subs.league_label(lg),
                         team=team, game_isos=dates, spots=1, channel=Ch())
    return [r for r in cog.state["requests"] if r["requester_id"] == who.id]


async def flush(cog):
    await cog._flush_all_notices()
    cog.dms.clear()
    cog.calls.clear()


# ── 2. The pass ─────────────────────────────────────────────────────────────
async def a_renamed_team_asks_the_requester_and_warns_the_sub():
    cog = make_cog()
    rs = await post(cog, ANN, [THU[1]])
    r = at(rs, 0, "rename/posted")
    if r is None:
        return
    await cog.fill_request_spot(BO, r["id"], channel=Ch())
    await flush(cog)
    renamed = league(["Ashbury", "Vance", "Corwin"])
    done = await cog.reconcile_drift([renamed], NOW)
    check("rename/flagged", [x["id"] for x in done["flagged"]], [r["id"]])
    check("rename/nothing deleted", len(cog.state["requests"]), 1)
    check("rename/sub still on it", [m["user_id"] for m in r["filled"]], [BO.id])
    check("rename/one DM each", per_person(cog), {ANN.id: 1, BO.id: 1})
    ann = next((d for d in cog.dms if d[0] == ANN.id), None)
    bo = next((d for d in cog.dms if d[0] == BO.id), None)
    check("rename/requester DM says why", bool(ann and "**Ashby** isn't on the league page" in ann[1]), True)
    check("rename/requester gets fix + cancel",
          [type(c).__name__ for c in (ann[2].children if ann and ann[2] else [])],
          ["SetTeamButton", "DriftCancelButton"])
    check("rename/sub gets a heads-up, no buttons", bool(bo and "Heads-up" in bo[1] and bo[2] is None), True)
    check("rename/board redrawn", ("board",) in cog.calls, True)

    cog.dms.clear()
    await cog.reconcile_drift([renamed], NOW)
    check("rename/raised once, not every pass", cog.dms, [])

    # The requester re-picks: the flag goes, the team is the new name, nobody's told.
    view = subs.SetTeamView(cog.state, renamed, ANN.id)
    check("rename/the fix view offers this request", [x["id"] for x in view.pending()], [r["id"]])
    check("rename/the fix view says why", "isn't on the league page" in view.prompt(), True)
    check("rename/…with the new roster to pick from",
          any(isinstance(c, subs.TeamSelect) for c in view.children), True)
    done_, clashes, _ = await cog.set_team_for(ANN, [r["id"]], "Ashbury", channel=Ch())
    check("rename/re-picked", (r["team"], r["drift"]), ("Ashbury", ""))
    cog.dms.clear()
    await cog.reconcile_drift([renamed], NOW)
    check("rename/fixed stays quiet", cog.dms, [])


async def the_new_name_on_bye_is_a_new_thing_to_say():
    cog = make_cog()
    rs = await post(cog, ANN, [THU[1]])
    r = at(rs, 0, "bye/posted")
    if r is None:
        return
    await flush(cog)
    await cog.reconcile_drift([league(byes={1: ["Ashby"]})], NOW)
    check("bye/flagged", subs.drift_kind(r["drift"]), subs.DRIFT_BYE)
    check("bye/requester told", per_person(cog), {ANN.id: 1})
    ann = at(cog.dms, 0, "bye/dm")
    check("bye/says it", bool(ann and "on bye that week" in ann[1]), True)
    # Re-picking the SAME team is held back by the bye; the fix view knows it.
    view = subs.SetTeamView(cog.state, league(byes={1: ["Ashby"]}), ANN.id)
    view.team = "Ashby"
    check("bye/same team can't go back on", view.chosen(), [])
    # The schedule changes again: a different drift is a new message.
    cog.dms.clear()
    await cog.reconcile_drift([league(dates=[THU[0], THU[2], THU[3]])], NOW)
    check("bye/a different change is said again", per_person(cog), {ANN.id: 1})
    # And back to normal: cleared, silently.
    cog.dms.clear()
    done = await cog.reconcile_drift([LG], NOW)
    check("bye/clears", (r["drift"], [x["id"] for x in done["cleared"]]), ("", [r["id"]]))
    check("bye/clearing says nothing", cog.dms, [])


async def a_moved_start_time_moves_the_request_and_tells_everyone_on_it():
    cog = make_cog()
    rs = await post(cog, ANN, [THU[1]])
    r = at(rs, 0, "time/posted")
    if r is None:
        return
    await cog.fill_request_spot(BO, r["id"], channel=Ch())
    await flush(cog)
    await cog.reconcile_drift([league(time="9:00 pm")], NOW)
    check("time/moved", r["game_ts"], THU[1][:10] + "T21:00:00")
    check("time/not a stored drift", r["drift"], "")
    check("time/both told once", per_person(cog), {ANN.id: 1, BO.id: 1})
    check("time/says the old time", all("was" in d[1] for d in cog.dms), True)
    check("time/alert page redrawn", ("refresh", r["id"]) in cog.calls, True)
    cog.dms.clear()
    await cog.reconcile_drift([league(time="9:00 pm")], NOW)
    check("time/once", cog.dms, [])


async def a_super_sub_on_a_renamed_team_hears_about_it():
    cog = make_cog()
    store.add_standing(cog.state, user_id=ROBIN.id, name=ROBIN.display_name, league_id="555",
                       league="Thursday League", team="Ashby", created_by=ALEX.id, now=NOW)
    await cog.reconcile_drift([league(["Ashbury", "Vance", "Corwin"])], NOW)
    check("standing/super sub + whoever set it up", per_person(cog), {ROBIN.id: 1, ALEX.id: 1})
    check("standing/points at /supersub", all("/supersub" in d[1] for d in cog.dms), True)
    check("standing/arrangement kept", len(cog.state["standing"]), 1)
    cog.dms.clear()
    await cog.reconcile_drift([league(["Ashbury", "Vance", "Corwin"])], NOW)
    check("standing/once", cog.dms, [])


async def no_news_is_not_bad_news():
    for label, leagues in (("empty list", []),
                           ("league missing from the list", [dict(LG, id=999)]),
                           ("page unreadable", [league(["Ashbury"], fetch_failed=True)]),
                           ("every name changed at once",
                            [league(["ASHBY FC", "VANCE FC", "CORWIN FC"])])):
        cog = make_cog()
        await post(cog, ANN, [THU[1]], team="Ashby")
        await post(cog, BO, [THU[2]], team="Vance")
        await flush(cog)
        done = await cog.reconcile_drift(leagues, NOW)
        check(f"silence/{label}: nothing flagged", done["flagged"], [])
        check(f"silence/{label}: nobody told", cog.dms, [])


async def five_dates_and_an_arrangement_are_one_message():
    cog = make_cog()
    await post(cog, ANN, THU[1:6])
    store.add_standing(cog.state, user_id=ANN.id, name=ANN.display_name, league_id="555",
                       league="Thursday League", team="Ashby", created_by=ANN.id, now=NOW)
    await flush(cog)
    await cog.reconcile_drift([league(["Ashbury", "Vance", "Corwin"])], NOW)
    check("volume/one DM for everything", per_person(cog), {ANN.id: 1})
    d = at(cog.dms, 0, "volume/dm")
    check("volume/lists all five", d and d[1].count("**Ashby** isn't on the league page"), 5)
    check("volume/and the arrangement, in the same message", bool(d and "Super sub" in d[1]), True)
    check("volume/one fix button for the league",
          [type(c).__name__ for c in (d[2].children if d and d[2] else [])],
          ["SetTeamButton", "DriftCancelButton"])


# ── 3. Cancel, and who may ──────────────────────────────────────────────────
async def cancelling_tells_the_subs_and_a_manager_can_do_it():
    cog = make_cog()
    rs = await post(cog, ANN, [THU[1]])
    r = at(rs, 0, "cancel/posted")
    if r is None:
        return
    await cog.fill_request_spot(BO, r["id"], channel=Ch())
    store.add_sub(r, store.guest_id("Pat Guest"), "Pat Guest")
    await flush(cog)
    await cog.close_request(r["id"], Ch(), actor=ANN)
    check("cancel/own: the sub is told, the requester isn't, a guest can't be",
          per_person(cog), {BO.id: 1})
    check("cancel/gone", cog.state["requests"], [])

    rs = await post(cog, ANN, [THU[2]])
    r2 = at(rs, 0, "cancel/second")
    if r2 is None:
        return
    await cog.fill_request_spot(BO, r2["id"], channel=Ch())
    await flush(cog)
    await cog.close_request(r2["id"], Ch(), actor=ROBIN)
    check("cancel/by someone else: requester and sub each told once",
          per_person(cog), {ANN.id: 1, BO.id: 1})
    ann = next((d for d in cog.dms if d[0] == ANN.id), None)
    check("cancel/requester told who did it", bool(ann and "Robin Vale cancelled" in ann[1]), True)


def managers_see_every_request():
    class P:
        def __init__(self, mg):
            self.manage_guild = mg

    class I:
        def __init__(self, mg):
            self.permissions = P(mg)

    check("admin/manage server can", subs.can_cancel_any(I(True)), True)
    check("admin/others can't", subs.can_cancel_any(I(False)), False)
    check("admin/no permissions object (DM)", subs.can_cancel_any(object()), False)

    state = store.empty_state()
    mine = store.new_request(state, requester_id=ANN.id, requester_name="Ann Lee", spots_needed=1,
                             game_ts=THU[2], league_id="555", team="Vance")
    theirs = store.new_request(state, requester_id=BO.id, requester_name="Bo Brooks", spots_needed=1,
                               game_ts=THU[1], league_id="555", team="Ashby")
    theirs["drift"] = "team_gone:ashby"

    def cancel_values(view):
        sel = next((c for c in view.children if isinstance(c, subs.CancelRequestSelect)), None)
        return [o.value for o in sel.options] if sel else []

    check("admin/a member sees only their own",
          cancel_values(subs.RemoveHomeView(state, ANN.id)), [mine["id"]])
    check("admin/a manager sees all, flagged first",
          cancel_values(subs.RemoveHomeView(state, ANN.id, admin=True)), [theirs["id"], mine["id"]])
    sel = next((c for c in subs.RemoveHomeView(state, ANN.id, admin=True).children
                if isinstance(c, subs.CancelRequestSelect)), None)
    first = sel.options[0] if sel and sel.options else None
    check("admin/whose it is is on the option", bool(first and "Bo's" in first.label), True)
    check("admin/why it's flagged is on the option",
          bool(first and "team isn't on the league page" in (first.description or "")), True)


# ── Wiring: a hook nobody calls passes every test that calls it directly ────
async def the_housekeeping_passes_run_it():
    for name in ("expiry_loop", "startup"):
        cog = make_cog()
        seen = []

        async def leagues():
            return [LG]

        async def rec(lgs, now):
            seen.append(lgs)
            return {}
        cog.maintenance_leagues = leagues
        cog.reconcile_drift = rec
        cog._requeue_unnotified = lambda: None
        if name == "expiry_loop":
            await subs.Subs.expiry_loop.coro(cog)
        else:
            await cog.startup()
        check(f"wiring/{name} runs reconcile_drift with the same league list", seen, [[LG]])


def the_new_button_survives_a_restart():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")) as f:
        src = f.read()
    check("wiring/DriftCancelButton registered as a dynamic item",
          "subs.DriftCancelButton" in src, True)


async def main():
    for fn in (a_renamed_team_asks_the_requester_and_warns_the_sub,
               the_new_name_on_bye_is_a_new_thing_to_say,
               a_moved_start_time_moves_the_request_and_tells_everyone_on_it,
               a_super_sub_on_a_renamed_team_hears_about_it,
               no_news_is_not_bad_news,
               five_dates_and_an_arrangement_are_one_message,
               cancelling_tells_the_subs_and_a_manager_can_do_it,
               the_housekeeping_passes_run_it):
        await fn()
    for c in COGS:
        await c._flush_all_notices()


asyncio.run(main())
managers_see_every_request()
the_new_button_survives_a_restart()

if FAILS:
    print("\n".join(f"FAIL: {f}" for f in FAILS))
    raise SystemExit(1)
print("All drift checks passed.")
