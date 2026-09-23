"""Unit tests for byes: a team with no game on a draw can't need a sub that week.

  1. league_client reads "<Team> - On Bye" off each draw heading into `byes`.
  2. The need-a-sub flow never offers a team its own bye dates, and a bye date
     ticked BEFORE the team was picked can't be posted.
  3. Every date in the picker names who's on bye.
  4. Attaching a team to requests posted before teams existed holds back that
     team's bye dates.

Run:  python3 test_byes.py     (no network; needs discord.py + bs4 + aiohttp + dotenv)

Neuters discord.Client.run and points SUBS_STORE_PATH at a scratch file before
importing subs, like the other test files.
"""
import asyncio
import os
import tempfile
from datetime import timedelta

import discord

discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["SUBS_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "subs_store.json")

import subs
import league_client as lc

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


# ── 1. Parsing ──────────────────────────────────────────────────────────────
# The shape the league page really renders: every game is a .teams block plus a
# .match_sheet, and the bye is a div of its own inside the same <h6>.
def game(home, away, sheet):
    return (f'<div class="teams noMyGame"><div class="home_team">'
            f'<div class="home team name">{home}</div><div class="home team score">-</div></div>'
            f'<div class="away_team"><div class="away team name">{away}</div>'
            f'<div class="away team score">-</div></div></div>'
            f'<div class="match_sheet noMyGame"> Sheet {sheet} </div>')


def draw(when, games, byes=()):
    body = "".join(game(*g) for g in games)
    body += "".join(f'<div class="bye" style="width: 100%; clear:both;">\n\t {b} - On Bye </div>'
                    for b in byes)
    d, t = when
    return f'<h6><div class="match_date"> {d} </div><div class="match_time"> {t} </div>{body}</h6>'


PAGE = "<html><body>" + "".join([
    draw(("October 6, 2026", "8:00 pm"),
         [("Ashby", "Vance", "A"), ("Corwin", "Dunmore", "B")], byes=["Ellery"]),
    draw(("October 13, 2026", "8:00 pm"),
         [("Ellery", "Ashby", "A"), ("Vance", "Corwin", "B")], byes=["Dunmore"]),
    # A week with two teams sitting out.
    draw(("October 20, 2026", "8:00 pm"),
         [("Dunmore", "Ellery", "A")], byes=["Ashby", "Vance & Co"]),
    # A full week: nobody on bye.
    draw(("October 27, 2026", "8:00 pm"),
         [("Ashby", "Corwin", "A"), ("Vance", "Dunmore", "B")]),
]) + "</body></html>"

parsed = lc.parse_league_html(PAGE)
dr = parsed["draws"]
check("parse/four draws", len(dr), 4)
check("parse/the bye team, suffix stripped", [d.get("byes") for d in dr],
      [["Ellery"], ["Dunmore"], ["Ashby", "Vance & Co"], []])
# The bye line mustn't count as a game on the ice.
check("parse/sheets unaffected by the bye", [d["sheets_used"] for d in dr], [2, 2, 1, 2])
check("parse/time still read", {d["time"] for d in dr}, {"8:00 pm"})
check("parse/dash variants", lc._BYE_TAIL_RE.sub("", "Ashby – On Bye"), "Ashby")
check("parse/no dash", lc._BYE_TAIL_RE.sub("", "Ashby On bye"), "Ashby")
check("parse/team name containing 'bye' kept",
      lc._BYE_TAIL_RE.sub("", "Bye Bye Birdies - On Bye"), "Bye Bye Birdies")


# ── 1b. Standings: the team name moved from <td> to <th scope="row"> ────────
# The rosters all went empty overnight without a line of our code changing. The
# site now marks each team's own cell as a row header, and the old "a data row
# has no <th>" test threw every row away. Both shapes have to parse.
HEAD = ('<tr><th scope="col">Place</th><th scope="col" class="standing-teams">Team Name</th>'
        '<th scope="col">W</th><th scope="col">Win %</th></tr>')


def standings(rows):
    return f"<html><body><table>{HEAD}{rows}</table></body></html>"


NEW_MARKUP = standings("".join(
    f'<tr class="ThisIsNotMyTeam"><td>{i}</td>'
    f'<th scope="row" class="standing-teams">{n}</th><td>2</td><td>75%</td></tr>'
    for i, n in enumerate(["Ashby", "Vance", "Corwin"], 1)))
OLD_MARKUP = standings("".join(
    f"<tr><td>{i}</td><td>{n}</td><td>2</td><td>75%</td></tr>"
    for i, n in enumerate(["Ashby", "Vance", "Corwin"], 1)))

for label, page in (("row-header", NEW_MARKUP), ("all-td", OLD_MARKUP)):
    got = lc.parse_league_html(page)
    check(f"standings/{label}: names", got["team_names"], ["Ashby", "Vance", "Corwin"])
    check(f"standings/{label}: count", got["teams"], 3)
# The header row is never a team.
check("standings/header row excluded", "Team Name" in lc.parse_league_html(NEW_MARKUP)["team_names"], False)
check("standings/no table at all", lc.parse_league_html("<html><body></body></html>")["teams"], None)


# ── Fixtures for the flows: a live league, dates genuinely in the future ─────
NOW = subs.club_now()


def night(n_days):
    return (NOW.replace(hour=20, minute=0, second=0, microsecond=0)
            + timedelta(days=n_days)).isoformat()


WK = [night(7 * i + 2) for i in range(4)]
BYES = [["Ellery"], ["Dunmore"], ["Ashby", "Vance"], []]
LEAGUE = {
    "id": 777, "title": "Tuesday League – Fall 2026 – Begins September 1",
    "day": "Tuesday", "time": "8:00 pm",
    "draws": [{"date": iso[:10], "weekday": "Tuesday", "time": "8:00 pm", "byes": b}
              for iso, b in zip(WK, BYES)],
    "team_names": ["Ashby", "Vance", "Corwin", "Dunmore", "Ellery"],
}

opts = subs.game_options(LEAGUE, NOW)
check("games/carry byes", [o["byes"] for o in opts], BYES)
check("on_bye/match ignores case + spacing", subs.on_bye(opts[0], "  ellery "), True)
check("on_bye/other team", subs.on_bye(opts[0], "Ashby"), False)
check("on_bye/no team", subs.on_bye(opts[0], ""), False)
check("bye_dates/by date", sorted(d.isoformat() for d in subs.bye_dates(LEAGUE, "Ashby")),
      [WK[2][:10]])
# An unscheduled league has no byes to know about — projections are never blocked.
proj = subs.game_options({**LEAGUE, "draws": []}, NOW)
check("games/projected dates have no byes", any(o.get("byes") for o in proj), False)


# ── 3. The picker names who's on bye ────────────────────────────────────────
desc = [o.description for o in subs.GameSelect(opts, [], multi=True).options]
check("select/names the bye", desc,
      ["On bye: Ellery", "On bye: Dunmore", "On bye: Ashby, Vance", None])

# The caller's OWN bye is marked as theirs, with the one visual cue Discord gives
# a single option. It must never simply be absent — a missing date reads as a bug.
mine = subs.GameSelect(opts, [], multi=True, bye_team="Dunmore").options
check("select/own bye still listed", [o.value for o in mine], WK)
check("select/own bye is described as theirs",
      [o.description for o in mine][1], "Dunmore on bye — no game, no sub needed")
check("select/own bye is flagged",
      [str(o.emoji) if o.emoji else None for o in mine], [None, subs.BYE_EMOJI, None, None])
check("select/other dates unflagged", [o.emoji for o in mine if o.value != WK[1]], [None] * 3)


# ── 2. Need-a-sub: a team's bye dates are never on offer ────────────────────
def picker(view):
    sel = next((i for i in view.build().children if isinstance(i, subs.GameSelect)), None)
    check("flow/has a date picker", sel is not None, True)
    return [o.value for o in sel.options] if sel else []


flow = subs.NeedSubFlowView([LEAGUE], {"requests": []}, 1)
flow.league_id = "777"
check("flow/no team yet: every date", picker(flow), WK)
flow.team = "Dunmore"
check("flow/the bye date is still shown", picker(flow), WK)
check("flow/it just can't be posted", flow.bye_isos(), [WK[1]])
# Nothing to explain until they actually tick it.
check("flow/quiet until ticked", "on bye" in flow.prompt(), False)
# Through the FLOW, not by constructing the select by hand: the marking is wiring
# (the flow has to hand its team to the picker), and wiring is invisible to a test
# that builds the component itself.
gs = next(i for i in flow.build().children if isinstance(i, subs.GameSelect))
check("flow/the picker is told whose byes to mark", gs.options[1].description,
      "Dunmore on bye — no game, no sub needed")
check("flow/and marks it", str(gs.options[1].emoji), subs.BYE_EMOJI)
check("flow/only that one", [o.emoji for o in gs.options if o.value != WK[1]], [None] * 3)
flow.game_isos = [WK[1]]
check("flow/prompt says why", "**Dunmore** is on bye" in flow.prompt(), True)
check("flow/tells them what to do", "untick" in flow.prompt(), True)
check("flow/and says dates, never nights", "night" in flow.prompt().lower(), False)
flow.team = "Corwin"
check("flow/no byes, no note", "on bye" in flow.prompt(), False)

# Dates ticked first, team picked after: the bye date drops out and can't post.
flow = subs.NeedSubFlowView([LEAGUE], {"requests": []}, 1)
flow.league_id = "777"
flow.game_isos = [WK[1], WK[3]]
flow.team = "Dunmore"
check("flow/a ticked bye is not posted", flow.dates(), [WK[3]])
check("flow/and is named in the prompt", "**Dunmore** is on bye" in flow.prompt(), True)
check("flow/still ready on the real date", flow.ready(), True)
flow.game_isos = [WK[1]]
check("flow/only a bye ticked is not ready", flow.ready(), False)
btn = next((i for i in flow.build().children if isinstance(i, subs.PostNeedButton)), None)
check("flow/post button disabled", btn is not None and btn.disabled, True)


# The handler: whatever was ticked, only non-bye dates reach the store.
class U:
    def __init__(self, uid, name):
        self.id, self.display_name = uid, name


class Resp:
    async def defer(self, **kw):
        pass

    async def edit_message(self, **kw):
        pass


class It:
    def __init__(self, user, cog):
        self.user, self.channel, self.response = user, None, Resp()
        self.edits = []
        self.client = type("C", (), {"get_cog": staticmethod(lambda _n: cog)})()

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


class FakeCog:
    _is_repeat_click = staticmethod(subs.Subs._is_repeat_click)

    def __init__(self):
        self.state = {"requests": [], "availability": []}
        self._click_cooldown = {}
        self.posted, self.set = None, None

    async def add_series(self, **kw):
        self.posted = kw["game_isos"]
        return (len(kw["game_isos"]), 0, 0)

    async def set_team_for(self, user, rids, team, channel=None):
        self.set = (list(rids), team)
        return ([], [], 0)


async def post_through_button():
    cog = FakeCog()
    f = subs.NeedSubFlowView([LEAGUE], cog.state, 1)
    f.league_id, f.team, f.game_isos = "777", "Ellery", [WK[0], WK[1], WK[2]]
    b = next(i for i in f.build().children if isinstance(i, subs.PostNeedButton))
    await b.callback(It(U(1, "Ann Lee"), cog))
    check("button/posts only the non-bye dates", cog.posted, [WK[1], WK[2]])


# ── 4. Set-team: the team's bye dates are held back ─────────────────────────
def req(rid, iso):
    return {"id": rid, "kind": "sub", "requester_id": 1, "requester_name": "Ann Lee",
            "game_ts": iso, "spots_needed": 1, "filled": [], "pending": [],
            "league_id": "777", "league": "Tuesday League", "team": "", "series_id": ""}


async def set_team():
    cog = FakeCog()
    cog.state["requests"] = [req("r0", WK[0]), req("r1", WK[1]), req("r2", WK[2])]
    v = subs.SetTeamView(cog.state, LEAGUE, 1)
    check("setteam/all ticked to start", sorted(v.rids), ["r0", "r1", "r2"])
    v.team = "Ashby"                       # on bye WK[2]
    check("setteam/bye date held back", v.chosen(), ["r0", "r1"])
    nights = next((i for i in v.build().children if isinstance(i, subs.NightSelect)), None)
    check("setteam/every date still listed",
          sorted(o.value for o in nights.options) if nights else None, ["r0", "r1", "r2"])
    check("setteam/the bye one says so",
          next(o.description for o in nights.options if o.value == "r2"),
          "Ashby on bye — no game, no sub needed")
    check("setteam/prompt says why", "**Ashby** is on bye" in v.prompt(), True)
    sub = next(i for i in v.build().children if isinstance(i, subs.SetTeamSubmit))
    check("setteam/button counts real dates", sub.label, "Set Ashby on 2 dates")
    await sub.callback(It(U(1, "Ann Lee"), cog))
    check("setteam/handler sends only real dates", cog.set, (["r0", "r1"], "Ashby"))

    # Every pending date is the team's bye: no date list, button off, no crash.
    cog2 = FakeCog()
    cog2.state["requests"] = [req("r2", WK[2])]
    v2 = subs.SetTeamView(cog2.state, LEAGUE, 1)
    v2.team = "Vance"
    kids = v2.build().children
    check("setteam/all-bye: the date is still listed",
          [o.value for i in kids if isinstance(i, subs.NightSelect) for o in i.options], ["r2"])
    check("setteam/all-bye: button disabled",
          next(i for i in kids if isinstance(i, subs.SetTeamSubmit)).disabled, True)
    check("setteam/all-bye: explains", "can't be Vance's spot" in v2.prompt(), True)
    check("setteam/all-bye: says what to do next", "pick another team" in v2.prompt(), True)


async def main():
    await post_through_button()
    await set_team()

asyncio.run(main())

# `time` must stay the stdlib module (see test_fixes §2e) — and the midnight
# fallback in league_games must not call it.
import time as _t
check("stdlib time unshadowed", subs.time is _t, True)
try:
    hours = [o["dt"].hour for o in subs.league_games({"draws": [{"date": WK[0][:10]}]}, NOW)]
except Exception as e:  # noqa: BLE001 — a regression must FAIL, not crash
    hours = repr(e)
check("games/no time anywhere doesn't crash", hours, [0])

if FAILS:
    print(f"{len(FAILS)} FAIL(s):")
    for f in FAILS:
        print(" -", f)
    raise SystemExit(1)
print("All bye checks passed.")
