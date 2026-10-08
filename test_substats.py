"""Unit tests for the sub records: who has subbed the most, and who is on a run.

  1. The board forgets a game the moment it is over, so the record of who subbed
     is written in the same pass that prunes the request, and only for a game
     that was actually played.
  2. Records are kept per league SLOT (the site's league category), not per
     league: a new season is a new league id and must carry the totals on.
  3. Two separate measures. GAMES is a plain count and owes nothing to streaks.
     A STREAK is consecutive weeks subbed, where a week the slot did not play is
     skipped and a week it played without you ends the run.
  4. The top sub of a slot wears a crown on the board, once they have enough
     games for it to mean something.

Run:  python3 test_substats.py    (no network; needs discord.py + bs4 + aiohttp + dotenv)

Same harness rules as test_standing.py: neuter discord.Client.run (bot.py calls
bot.run() at import time) and point SUBS_STORE_PATH at a scratch file BEFORE
importing subs, because the cog loads and saves its store on construction.
"""
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timedelta

import discord

discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")
SCRATCH = tempfile.mkdtemp()
os.environ["SUBS_STORE_PATH"] = os.path.join(SCRATCH, "subs_store.json")

import subs
import sub_store as store

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


def at(seq, i, name):
    """seq[i], or a recorded FAILURE and None. A regression must fail, not crash:
    one traceback swallows every failure recorded before it."""
    try:
        return seq[i]
    except (IndexError, KeyError, TypeError):
        check(name, f"missing [{i}]", "present")
        return None


def row_at(seq, i, name):
    """A history row by position, or a recorded failure and an empty one."""
    return at(seq, i, name) or {}


def value_at(fields, i, name):
    """An embed field's text by position, or a recorded failure and ""."""
    return getattr(at(fields, i, name), "value", "")


# ── Fixtures ────────────────────────────────────────────────────────────────
# A fixed Thursday lunchtime, so every week boundary below is the same on any day
# the suite runs.
NOW = datetime(2026, 10, 8, 12, 0)
THURSDAYS = ["2026-09-03", "2026-09-10", "2026-09-17", "2026-09-24", "2026-10-01"]
TUESDAYS = ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29", "2026-10-06"]

THU, TUE = "101", "202"
SLOTS = {THU: {"slot": "thurs", "name": "Thursday League"},
         TUE: {"slot": "tues", "name": "Tuesday League"}}


class U:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name


ANN, BO, CARA, DEV = U(1, "Ann Adams"), U(2, "Bo Brooks"), U(3, "Cara Cole"), U(4, "Dev Diaz")
QUINN = U(90, "Quinn Quill")          # the one who keeps needing a sub


def fresh(played=None):
    st = store.empty_state()
    store.note_leagues(st, SLOTS, played if played is not None
                       else {"thurs": THURSDAYS, "tues": TUESDAYS})
    return st


def game(st, lid, day, people=(), *, hour=20, pending=(), team="Ashby", spots=None, kind="sub"):
    """A request for one game with these people on it. Not yet played: that takes
    an expire() after the game's time."""
    r = store.new_request(st, requester_id=QUINN.id, requester_name=QUINN.display_name,
                          spots_needed=spots or max(1, len(people) + len(pending)),
                          game_ts=f"{day}T{hour:02d}:00:00", league_id=lid,
                          league="Thursday League 9/3 – 11/19", team=team, kind=kind,
                          now=NOW)
    for u in people:
        store.add_sub(r, u.id, u.display_name, NOW)
    for u in pending:
        r["pending"].append({"user_id": u.id, "name": u.display_name, "ts": "x"})
    return r


def play(st, now=NOW):
    return store.expire(st, now, 0)


def season(rows, *, played=None, now=NOW):
    """A store in which each (league id, day, [people]) game has been played."""
    st = fresh(played)
    for lid, day, people in rows:
        game(st, lid, day, people)
    play(st, now)
    return st


def streak(st, user, key, slot=None, now=NOW):
    row = next((r for r in store.streaks(st, now, slot) if r["user_id"] == user.id), None)
    return row[key] if row else None


def games(st, slot=None):
    return [(r["name"], r["games"]) for r in store.sub_totals(st, slot)]


# ── 1. Writing the record ───────────────────────────────────────────────────
st = store.empty_state()
check("store/new state has the three record keys",
      (st.get("history"), st.get("played"), st.get("league_slots")), ([], {}, {}))

# A store written before any of this existed loads with the keys in place.
_old = os.path.join(SCRATCH, "old.json")
with open(_old, "w") as f:
    json.dump({"boards": {}, "requests": [], "availability": [], "standing": []}, f)
_loaded = store.load(_old)
check("store/an older file gains the keys on load",
      (_loaded.get("history"), _loaded.get("played"), _loaded.get("league_slots")),
      ([], {}, {}))

st = fresh()
game(st, THU, "2026-10-01", [ANN, BO], pending=[CARA])
upcoming = game(st, THU, "2026-10-15", [DEV])
dropped = play(st)
check("record/the played request is pruned", len(dropped["requests"]), 1)
check("record/one row per sub who was on it",
      sorted(h["name"] for h in st["history"]), ["Ann Adams", "Bo Brooks"])
check("record/a pending invite never said yes, so it is not a game",
      [h for h in st["history"] if h["user_id"] == CARA.id], [])
check("record/a game still to come is not recorded",
      [h for h in st["history"] if h["user_id"] == DEV.id], [])
check("record/and its request is still on the board",
      [r["id"] for r in st["requests"]], [upcoming["id"]])
row = row_at(st["history"], 0, "record/a row exists")
check("record/a row says who, when and where",
      (row.get("user_id"), row.get("game_ts"), row.get("league_id"), row.get("team")),
      (ANN.id, "2026-10-01T20:00:00", THU, "Ashby"))
check("record/filed under the league's slot, with the name to head it",
      (row.get("slot"), row.get("slot_name")), ("thurs", "Thursday League"))

# Nothing but a played game leaves a row.
st = fresh()
cancelled = game(st, THU, "2026-10-15", [ANN])
store.close_request(st, cancelled["id"])
check("record/a cancelled request leaves nothing", st["history"], [])

st = fresh()
undated = game(st, THU, "2026-10-01", [ANN])
undated["game_ts"] = ""
undated["created_ts"] = "2026-08-01T10:00:00"
aged = play(st)
check("record/an undated request ages out", len(aged["requests"]), 1)
check("record/…without becoming a game anyone played", st["history"], [])

st = fresh()
undated = game(st, THU, "2026-10-01", [ANN])
undated["game_ts"] = ""
store.expire(st, NOW, 0, dead_leagues=[THU])
check("record/a season ending is not a game either",
      (st["requests"], st["history"]), ([], []))

check("record/asked directly, an undated request is still not a game",
      (store.record_played(st, undated), st["history"]), (0, []))

st = fresh()
game(st, THU, "2026-10-01", [ANN], kind="pickup")
play(st)
check("record/only subbing counts as subbing", st["history"], [])

# The same request can't be counted twice, whoever calls it.
st = fresh()
r = game(st, THU, "2026-10-01", [ANN])
check("record/first write", store.record_played(st, r), 1)
check("record/second write adds nothing", store.record_played(st, r), 0)
play(st)
check("record/and expire doesn't double it", len(st["history"]), 1)

# An auto-assigned super sub who never tapped confirm was still the name on the
# spot when the game was played.
st = fresh()
r = game(st, THU, "2026-10-01", spots=1)
store.assign_auto(r, BO.id, BO.display_name, "batch1", NOW)
play(st)
check("record/an unconfirmed super sub still played", [h["name"] for h in st["history"]],
      ["Bo Brooks"])

# ── 1b. What the league list tells us ───────────────────────────────────────
st = store.empty_state()
check("note/first sight of the leagues is a change",
      store.note_leagues(st, SLOTS, {"thurs": THURSDAYS}), True)
check("note/the same news twice is not", store.note_leagues(st, SLOTS, {"thurs": THURSDAYS}),
      False)
check("note/dates are kept once, in order",
      (store.note_leagues(st, SLOTS, {"thurs": ["2026-10-01", "2026-09-03", "2026-08-27"]}),
       st["played"].get("thurs", [])),
      (True, ["2026-08-27"] + THURSDAYS))
check("note/a date that isn't one is ignored",
      (store.note_leagues(st, SLOTS, {"thurs": ["soon", ""]}), len(st["played"].get("thurs", []))),
      (False, 6))
check("note/a league with no slot is not given one",
      store.note_leagues(st, {**SLOTS, "303": {"slot": "", "name": "Odd League"}}) or
      "303" in st["league_slots"], False)

# A game played while the league was unknown is filed blank, then put right the
# first time the league list has it.
st = store.empty_state()
game(st, THU, "2026-10-01", [ANN])
play(st)
check("note/unknown league, blank slot",
      row_at(st["history"], 0, "note/row").get("slot"), "")
check("note/blank rows stay out of every slot", store.slots_with_history(st), [])
check("note/…but still count across all leagues", games(st), [("Ann Adams", 1)])
store.note_leagues(st, SLOTS)
_fixed = row_at(st["history"], 0, "note/row after")
check("note/learning the league fills the row in",
      (_fixed.get("slot"), _fixed.get("slot_name")), ("thurs", "Thursday League"))

# The lookup is trimmed to what something still refers to; the record is not.
st = fresh()
game(st, THU, "2026-10-15", [ANN])
game(st, TUE, "2026-10-01", [BO])
play(st)                                   # Tuesday's game is now history
only_new = {"404": {"slot": "thurs", "name": "Thursday League"}}
store.note_leagues(st, only_new)
check("note/a league with a live request keeps its slot", THU in st["league_slots"], True)
check("note/one nothing refers to is forgotten", TUE in st["league_slots"], False)
check("note/its games are not", games(st, "tues"), [("Bo Brooks", 1)])
# An unreadable league list is an empty one, and proves nothing.
store.note_leagues(st, {})
check("note/an empty list forgets nothing", sorted(st["league_slots"]), [THU, "404"])
st2 = fresh()
store.add_standing(st2, user_id=BO.id, name="Bo Brooks", league_id=TUE,
                   league="Tuesday League", team="", now=NOW)
store.note_leagues(st2, only_new)
check("note/a super sub arrangement keeps its league's slot too",
      TUE in st2["league_slots"], True)

# ── 2. Games: a plain count per slot ────────────────────────────────────────
st = season([(THU, d, [ANN]) for d in THURSDAYS[:3]]
            + [(THU, "2026-10-01", [BO, CARA]), (TUE, "2026-09-29", [BO])])
check("games/most first, in one slot", games(st, "thurs"),
      [("Ann Adams", 3), ("Bo Brooks", 1), ("Cara Cole", 1)])
check("games/another slot is another count", games(st, "tues"), [("Bo Brooks", 1)])
check("games/all leagues together", games(st),
      [("Ann Adams", 3), ("Bo Brooks", 2), ("Cara Cole", 1)])

# The reason slots exist: a new season is a new league id, same slot.
NEW_THU = "505"
st = season([(THU, d, [ANN]) for d in THURSDAYS[:2]])
store.note_leagues(st, {NEW_THU: SLOTS[THU]}, {"thurs": ["2026-10-08"]})
game(st, NEW_THU, "2026-10-08", [ANN])
play(st, NOW + timedelta(days=1))
check("games/a new league in the same slot carries the count on",
      games(st, "thurs"), [("Ann Adams", 3)])
check("games/both league ids are in the record",
      sorted({h["league_id"] for h in st["history"]}), [THU, NEW_THU])

# Games owe nothing to streaks: three in a row and three scattered are both three.
in_a_row = season([(THU, d, [ANN]) for d in THURSDAYS[:3]])
scattered = season([(THU, d, [ANN]) for d in (THURSDAYS[0], THURSDAYS[2], THURSDAYS[4])])
check("games/the count ignores whether they were consecutive",
      (games(in_a_row, "thurs"), games(scattered, "thurs")),
      ([("Ann Adams", 3)], [("Ann Adams", 3)]))
check("games/while the streaks differ",
      (streak(in_a_row, ANN, "best", "thurs"), streak(scattered, ANN, "best", "thurs")),
      (3, 1))
check("games/and the crown follows games, not runs",
      (store.top_subs(in_a_row), store.top_subs(scattered)),
      ({"thurs": frozenset({ANN.id})}, {"thurs": frozenset({ANN.id})}))

# People rename themselves; the board uses what they are called now.
st = season([(THU, THURSDAYS[0], [ANN])])
game(st, THU, THURSDAYS[1], [U(ANN.id, "Ann Abbott")])
play(st)
check("games/the latest name wins", games(st, "thurs"), [("Ann Abbott", 2)])

# ── 2b. The top sub ─────────────────────────────────────────────────────────
st = season([(THU, d, [ANN]) for d in THURSDAYS[:2]])
check("top/two games is not yet a title", store.top_subs(st), {})
check("top/unless the bar is set there", store.top_subs(st, 2), {"thurs": frozenset({ANN.id})})
st = season([(THU, d, [ANN, BO]) for d in THURSDAYS[:3]] + [(THU, THURSDAYS[3], [CARA])])
check("top/a tie shares it", store.top_subs(st), {"thurs": frozenset({ANN.id, BO.id})})
st = season([(THU, d, [ANN]) for d in THURSDAYS[:3]] + [(TUE, d, [BO]) for d in TUESDAYS[:2]])
check("top/each slot has its own, or none", store.top_subs(st), {"thurs": frozenset({ANN.id})})

# ── 3. Streaks ──────────────────────────────────────────────────────────────
st = season([(THU, d, [ANN]) for d in THURSDAYS[:3]])
check("streak/three weeks running, then two missed",
      (streak(st, ANN, "best", "thurs"), streak(st, ANN, "current", "thurs")), (3, 0))
st = season([(THU, d, [ANN]) for d in THURSDAYS[2:]])
check("streak/a run that reaches the latest week is still going",
      (streak(st, ANN, "best", "thurs"), streak(st, ANN, "current", "thurs")), (3, 3))
st = season([(THU, d, [ANN]) for d in (THURSDAYS[0], THURSDAYS[1], THURSDAYS[3], THURSDAYS[4])])
check("streak/a played week without you ends it, and the next one starts over",
      (streak(st, ANN, "best", "thurs"), streak(st, ANN, "current", "thurs")), (2, 2))

# The slot was dark on 9/17 and 9/24: those weeks aren't in the calendar, so
# they neither add to the run nor break it.
st = season([(THU, d, [ANN]) for d in (THURSDAYS[0], THURSDAYS[1], THURSDAYS[4])],
            played={"thurs": [THURSDAYS[0], THURSDAYS[1], THURSDAYS[4]]})
check("streak/a week the league was off is skipped",
      (streak(st, ANN, "best", "thurs"), streak(st, ANN, "current", "thurs")), (3, 3))

# Same thing across a season boundary, which is a gap AND a new league id.
st = season([(THU, d, [ANN]) for d in THURSDAYS[:2]],
            played={"thurs": THURSDAYS[:2]})
store.note_leagues(st, {NEW_THU: SLOTS[THU]}, {"thurs": ["2026-10-08"]})
game(st, NEW_THU, "2026-10-08", [ANN])
play(st, NOW + timedelta(days=1))
check("streak/a run carries across seasons of the same slot",
      streak(st, ANN, "current", "thurs", NOW + timedelta(days=1)), 3)

# Twice in one week is one week.
st = season([(THU, THURSDAYS[0], [ANN]), (TUE, TUESDAYS[0], [ANN]),
             (THU, THURSDAYS[1], [ANN])])
check("streak/two games in a week are one week of streak", streak(st, ANN, "best"), 2)
check("streak/…and still two games each way",
      (games(st, "thurs"), games(st, "tues")), ([("Ann Adams", 2)], [("Ann Adams", 1)]))

# Overall is any league: Tuesday one week and Thursday the next is a run of two,
# though neither slot on its own has one.
st = season([(TUE, "2026-09-22", [ANN]), (THU, "2026-10-01", [ANN])],
            played={"tues": ["2026-09-22"], "thurs": ["2026-10-01"]})
check("streak/overall runs across leagues", streak(st, ANN, "best"), 2)
check("streak/each slot counts only itself",
      (streak(st, ANN, "best", "thurs"), streak(st, ANN, "best", "tues")), (1, 1))

# Tonight's draw: it is on the calendar as soon as it is over, but a request can
# sit until midnight (a start time still to be confirmed is parked at 23:59).
# Not having a row for tonight YET must not read as having missed it.
tonight = "2026-10-08"
st = season([(THU, d, [ANN]) for d in THURSDAYS[3:]], played={"thurs": THURSDAYS + [tonight]})
late = NOW.replace(hour=21)
check("streak/tonight's draw can't end a run until the day is over",
      streak(st, ANN, "current", "thurs", late), 2)
check("streak/the morning after, it has",
      streak(st, ANN, "current", "thurs", late + timedelta(days=1)), 0)
game(st, THU, tonight, [ANN])
play(st, late)
check("streak/and playing tonight extends it straight away",
      streak(st, ANN, "current", "thurs", late), 3)

# A week someone subbed in is a played week even if the calendar never heard.
st = season([(THU, THURSDAYS[0], [ANN, BO]), (THU, THURSDAYS[1], [ANN])], played={})
check("streak/history alone is enough to end someone else's run",
      (streak(st, ANN, "current", "thurs"), streak(st, BO, "current", "thurs")), (2, 0))

# The boards: one week is not a streak.
st = season([(THU, d, [ANN]) for d in THURSDAYS[2:]] + [(THU, THURSDAYS[4], [BO])]
            + [(THU, d, [CARA]) for d in THURSDAYS[:2]])
check("board/current, longest first, from two weeks up",
      [(r["name"], r["current"]) for r in store.streak_board(st, NOW, "current", "thurs")],
      [("Ann Adams", 3)])
check("board/records include runs that have ended",
      [(r["name"], r["best"]) for r in store.streak_board(st, NOW, "best", "thurs")],
      [("Ann Adams", 3), ("Cara Cole", 2)])
check("board/slots come out in week order",
      store.slots_with_history(season([(THU, THURSDAYS[0], [ANN]),
                                       (TUE, TUESDAYS[0], [BO])])), ["tues", "thurs"])

# ── 4. Reading the league list ──────────────────────────────────────────────
LEAGUES = [
    {"id": 101, "category": "thurs", "day": "Thursday", "time": "8:00 pm",
     "title": "Thursday League – Fall 2026 – Begins September 3",
     "draws": [{"date": d, "weekday": "Thursday", "time": "8:00 pm"}
               for d in THURSDAYS + ["2026-10-08", "2026-10-15"]]},
    {"id": 202, "category": "tues", "day": "Tuesday", "time": "8:00 pm",
     "title": "Tuesday League &#8211; Fall 2026 &#8211; Begins September 1",
     "draws": [{"date": d, "weekday": "Tuesday", "time": "8:00 pm"} for d in TUESDAYS]},
    {"id": 303, "category": None, "title": "Uncategorised Bonspiel", "draws": []},
]
check("leagues/slot is the category, name is the league without its season",
      subs.league_slot_map(LEAGUES), SLOTS)
check("leagues/only draws that are over are played",
      subs.played_dates(LEAGUES, NOW, 0), {"thurs": THURSDAYS, "tues": TUESDAYS})
check("leagues/tonight's goes on once it has started",
      subs.played_dates(LEAGUES, NOW.replace(hour=20, minute=1), 0)["thurs"][-1], "2026-10-08")
check("leagues/and waits out the same grace a request does",
      subs.played_dates(LEAGUES, NOW.replace(hour=20, minute=1), 3)["thurs"][-1], "2026-10-01")
check("leagues/a draw with no readable date is skipped, not fatal",
      subs.played_dates([{"category": "thurs", "draws": [{"date": "TBD", "time": "8 pm"},
                                                         {"time": "8:00 pm"}]}], NOW, 0), {})

# ── 5. The crown on the board ───────────────────────────────────────────────
subs.club_now = lambda: NOW
st = season([(THU, d, [ANN]) for d in THURSDAYS[:3]] + [(TUE, d, [BO]) for d in TUESDAYS[:3]])
thu_game = game(st, THU, "2026-10-08", [ANN, BO], spots=3)
tue_game = game(st, TUE, "2026-10-13", [ANN])
line = subs._req_status_line(thu_game, frozenset({ANN.id}))
check("crown/sits on the top sub's name", "Ann Adams 👑" in line, True)
check("crown/and on nobody else's", line.count("👑"), 1)
check("crown/no crown passed, no crown drawn", "👑" in subs._req_status_line(thu_game), False)
board = subs.build_embed(st)
thu_line = next((l for l in board.description.splitlines() if "3" in l and "Ashby" in l
                 and "Bo Brooks" in l), "")
tue_line = next((l for l in board.description.splitlines() if "Bo Brooks" not in l
                 and "Ann Adams" in l), "")
check("crown/Thursday's top sub is crowned on a Thursday game",
      ("Ann Adams 👑" in thu_line, "Bo Brooks 👑" in thu_line), (True, False))
check("crown/…and is just a sub on a Tuesday", "👑" in tue_line, False)
check("crown/the footer explains it while it shows", "👑" in (board.footer.text or ""), True)
auto = game(st, TUE, "2026-10-20", spots=1)
store.assign_auto(auto, BO.id, BO.display_name, "batch", NOW)
check("crown/reads properly beside the unconfirmed tag",
      "Bo Brooks 👑 (unconfirmed)" in subs._req_status_line(auto, frozenset({BO.id})), True)
plain = season([(THU, THURSDAYS[0], [ANN])])
game(plain, THU, "2026-10-08", [ANN])
check("crown/no title yet, no crown and no legend",
      ("👑" in subs.build_embed(plain).description, "👑" in subs.build_embed(plain).footer.text),
      (False, False))

# ── 6. The records screen ───────────────────────────────────────────────────
empty = subs.build_stats_embed(store.empty_state(), NOW)
check("stats/an empty record says so", "No sub records yet" in (empty.description or ""), True)
check("stats/…and shows no boards", len(empty.fields), 0)

st = season([(THU, d, [ANN]) for d in THURSDAYS[:3]] + [(THU, THURSDAYS[4], [ANN])]
            + [(THU, d, [BO]) for d in THURSDAYS[3:]] + [(THU, THURSDAYS[1], [CARA])]
            + [(TUE, d, [BO, DEV]) for d in TUESDAYS[3:]])
emb = subs.build_stats_embed(st, NOW)
fields = {f.name: f.value for f in emb.fields}
foot = emb.footer.text or ""
check("stats/a field per slot, in week order, then the all-league boards",
      [f.name for f in emb.fields],
      ["Tuesday League", "Thursday League", "All leagues · most games",
       "🔥  Current streaks", "📈  Longest streaks"])
thu = fields.get("Thursday League", "").splitlines()
check("stats/the slot leader wears the crown here too",
      at(thu, 0, "stats/thu line 1"), "👑  **4 games** · Ann Adams")
check("stats/then silver and bronze",
      thu[1:3], ["🥈  **2 games** · Bo Brooks", "🥉  **1 game** · Cara Cole"])
check("stats/the slot's live run and its record sit under the games",
      thu[3:], ["🔥  2 wks running · Bo Brooks", "📈  longest run 3 wks · Ann Adams"])
check("stats/a tie is one line and one place",
      at(fields.get("Tuesday League", "").splitlines(), 0, "stats/tue line 1"),
      "👑  **3 games** · Bo Brooks, Dev Diaz")
check("stats/all leagues counts every game once",
      fields.get("All leagues · most games", "").splitlines()[:2],
      ["🥇  **5 games** · Bo Brooks", "🥈  **4 games** · Ann Adams"])
check("stats/overall streaks run across leagues",
      fields.get("🔥  Current streaks", ""), "🥇  **3 wks** · Bo Brooks, Dev Diaz")
check("stats/the footer says what the crown and a streak mean",
      ("👑" in foot, "3 or more" in foot, "off" in foot),
      (True, True, True))
# A leader below the bar gets the ordinary medal, not the crown.
few = subs.build_stats_embed(season([(THU, THURSDAYS[0], [ANN])]), NOW)
check("stats/gold, not a crown, until the title is earned",
      value_at(few.fields, 0, "stats/few field"), "🥇  **1 game** · Ann Adams")
check("stats/nobody on a run is said plainly",
      {f.name: f.value for f in few.fields}.get("🔥  Current streaks"),
      "Nobody on a run right now")

# It posts to a channel, so: nobody is pinged, and the copy holds the house line.
everything = " ".join([emb.title, emb.description or "", foot]
                      + [f.name + " " + f.value for f in emb.fields])
check("stats/no mentions anywhere in it", "<@" in everything, False)
check("stats/dates, never nights", "night" in everything.lower(), False)

# Discord's limits, at a size no club will reach: 40 people level in six slots,
# each headed by a league name far longer than any real one.
CROWD_SLOTS = ("sunam", "sunpm", "tues", "thurs", "friday-tgif", "sat")
crowd = store.empty_state()
store.note_leagues(crowd, {str(900 + n): {"slot": slot, "name": f"{slot} League " + "x" * 80}
                           for n, slot in enumerate(CROWD_SLOTS)})
for n in range(len(CROWD_SLOTS)):
    for day in ("2026-09-07", "2026-09-14", "2026-09-21"):
        game(crowd, str(900 + n), day,
             [U(1000 + m, f"Member Number{m:02d}") for m in range(40)])
play(crowd)
big = subs.build_stats_embed(crowd, NOW)
check("stats/every field fits", max([len(f.value) for f in big.fields] or [9999]) <= 1024, True)
check("stats/every field name fits", max([len(f.name) for f in big.fields] or [9999]) <= 256, True)
check("stats/the whole embed fits", len(big) <= 6000, True)
check("stats/a long tie is summarised, not dropped",
      "+34 more" in value_at(big.fields, 0, "stats/big field"), True)


# ── 7. Wiring: the housekeeping passes are what keep the record ─────────────
COGS = []


def make_cog(leagues=LEAGUES):
    cog = subs.Subs(object())
    COGS.append(cog)
    cog.state = store.empty_state()
    cog.calls, cog.saves = [], []

    async def render_all_boards():
        cog.calls.append("boards")

    async def refresh_page(req):
        cog.calls.append("refresh")

    async def post_page(req, *, reason="new", channel=None):
        cog.calls.append("page")

    async def maintenance_leagues():
        return leagues

    cog.render_all_boards = render_all_boards
    cog.refresh_page = refresh_page
    cog.post_page = post_page
    cog.maintenance_leagues = maintenance_leagues
    cog._save = lambda: cog.saves.append(1)
    return cog


class Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))


class Interaction:
    def __init__(self):
        self.response = Response()
        self.guild_id = 7
        self.user = ANN


async def the_expiry_loop_keeps_the_record():
    cog = make_cog()
    game(cog.state, THU, "2026-10-01", [ANN])
    await subs.Subs.expiry_loop.coro(cog)
    row = row_at(cog.state["history"], 0, "wiring/expiry loop wrote a row")
    check("wiring/the loop records the game it prunes", row.get("name"), "Ann Adams")
    # The slot is only there if the league list was taken in BEFORE the prune.
    check("wiring/…under its slot, so leagues are noted first", row.get("slot"), "thurs")
    check("wiring/…with the played weeks on the calendar",
          cog.state["played"].get("thurs", [])[:5], THURSDAYS)
    check("wiring/and it is saved", len(cog.saves) >= 1, True)


async def a_quiet_pass_still_saves_what_it_learned():
    cog = make_cog()
    await subs.Subs.expiry_loop.coro(cog)
    check("wiring/league news alone is worth a save", len(cog.saves), 1)
    check("wiring/…but not a redraw of every board", cog.calls, [])
    await subs.Subs.expiry_loop.coro(cog)
    check("wiring/nothing new, nothing written", len(cog.saves), 1)


async def startup_keeps_the_record_too():
    cog = make_cog()
    game(cog.state, THU, "2026-10-01", [BO])
    await cog.startup()
    row = row_at(cog.state["history"], 0, "wiring/startup wrote a row")
    check("wiring/a game that ended while the bot was down is recorded on startup",
          (row.get("name"), row.get("slot")), ("Bo Brooks", "thurs"))


async def both_passes_call_the_note():
    """Wiring, not behaviour: stub the callee and prove each CALLER runs it."""
    for label, run in (("expiry loop", lambda c: subs.Subs.expiry_loop.coro(c)),
                       ("startup", lambda c: c.startup())):
        cog = make_cog()
        seen = []
        cog._note_leagues = lambda leagues, now: seen.append(len(leagues)) or False
        await run(cog)
        check(f"wiring/{label} feeds the league list to the records", seen, [len(LEAGUES)])


async def an_unreadable_league_list_loses_nothing():
    cog = make_cog(leagues=[])
    cog.state = season([(THU, THURSDAYS[0], [ANN])])
    game(cog.state, THU, "2026-10-01", [ANN])
    await subs.Subs.expiry_loop.coro(cog)
    check("wiring/the game is still recorded", len(cog.state["history"]), 2)
    check("wiring/under the slot we already knew",
          row_at(cog.state["history"], -1, "wiring/last row").get("slot"), "thurs")
    check("wiring/and the calendar is untouched", cog.state["played"].get("thurs", []), THURSDAYS)


async def stats_posts_the_records_and_pings_nobody():
    cog = make_cog()
    cog.state = season([(THU, d, [ANN]) for d in THURSDAYS[:3]])
    inter = Interaction()
    await subs.Subs.subs_cmd.callback(cog, inter, stats=True)
    sent = at(inter.response.sent, 0, "cmd/stats sent something")
    if sent is None:
        return
    content, kw = sent
    check("cmd/one message", len(inter.response.sent), 1)
    check("cmd/it is the records embed", getattr(kw.get("embed"), "title", None), subs.STATS_TITLE)
    check("cmd/posted to the channel, like the practice records", kw.get("ephemeral", False),
          False)
    # A buzz is a DM, or a message whose CONTENT mentions someone. This is neither.
    check("cmd/no content to carry a mention", content, None)
    check("cmd/and no view to tap by mistake", kw.get("view"), None)


async def bare_subs_is_unchanged():
    cog = make_cog()
    inter = Interaction()
    await subs.Subs.subs_cmd.callback(cog, inter)
    sent = at(inter.response.sent, 0, "cmd/bare sent something")
    if sent is None:
        return
    check("cmd/bare /subs is still the private board",
          (sent[1].get("ephemeral"), getattr(sent[1].get("embed"), "title", None)),
          (True, subs.BOARD_TITLE))


async def main():
    for fn in (the_expiry_loop_keeps_the_record,
               a_quiet_pass_still_saves_what_it_learned,
               startup_keeps_the_record_too,
               both_passes_call_the_note,
               an_unreadable_league_list_loses_nothing,
               stats_posts_the_records_and_pings_nobody,
               bare_subs_is_unchanged):
        await fn()
    for c in COGS:
        await c._flush_all_notices()


asyncio.run(main())

check("cmd/stats is an option on /subs",
      sorted(p.name for p in subs.Subs.subs_cmd.parameters), ["show", "stats"])
check("cmd/the description fits Discord's 100", len(subs.Subs.subs_cmd.description) <= 100, True)
# An import added to subs.py must not shadow the stdlib `time` the debounce uses.
check("guard/subs.time is still the module", subs.time is time, True)

if FAILS:
    print("\n".join(f"FAIL: {f}" for f in FAILS))
    raise SystemExit(1)
print("All sub record checks passed.")
