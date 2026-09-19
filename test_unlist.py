"""Unit tests for taking a practice back — the 🙅 menu on the /sheets report.

Run:  python3 test_unlist.py

Covers the edit window (this week plus all of last week, floored to Sunday), the
per-practice attendance records that make a single past practice addressable at
all, what a removal does to a streak, and the Discord components the flow renders.

The view layer is exercised on purpose, not just the pure store: every curlbot bug
that has reached production so far lived in the Discord layer. Constructing the
selects and reading their options is the closest this gets to clicking — it is NOT
a substitute for a live smoke test.
"""
import ast
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

import discord

# bot.py calls bot.run() at import time; neuter it so we can import the module.
discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import bot as botmod
import practice_store as ps

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


def check_true(name, got):
    if not got:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: truthy")


def at(seq, i, name):
    """Index without crashing the suite: a regression that empties a list should
    produce a FAIL line, not a traceback that swallows every failure before it."""
    try:
        return seq[i]
    except (IndexError, KeyError, TypeError):
        FAILS.append(f"{name}\n   nothing at index {i!r} of {seq!r}")
        return None


# Fri Sep 18 2026, 10am. Seven days back is Fri Sep 11; that week's Sunday is Sep 6.
NOW = datetime(2026, 9, 18, 10, 0)
FLOOR = datetime(2026, 9, 6, 0, 0)

ANN, BO = 101, 202


def key_of(when: datetime) -> str:
    return when.strftime("%Y%m%dT%H%M")


def seed(*, live=(), past=(), user=ANN, name="Ann Lee") -> dict:
    """A store with `live` sessions the user is signed up for and `past` ones already
    confirmed against their streak. Both are plain datetimes."""
    state = ps.empty_state()
    for when in live:
        k = key_of(when)
        ps.register_session(state, k, when_ts=when.isoformat(),
                            label=when.strftime("%a %b %-d · %-I:%M %p"))
        ps.toggle(state, k, user, name, when_ts=when.isoformat(), now=NOW)
    for when in past:
        k = key_of(when)
        ps._confirm_practice(state, user, name, k, when.isoformat(),
                             when.strftime("%a %b %-d · %-I:%M %p"))
    return state


# ── 1. The window: Sunday of the week containing seven days ago ───────────────
# Stated as "today minus seven days, then back to that week's Sunday", so the floor
# walks with the weekday and only jumps on Sundays.

for label, today, want in [
    ("Sunday",    datetime(2026, 9, 20, 9, 0),  datetime(2026, 9, 13)),
    ("Monday",    datetime(2026, 9, 14, 9, 0),  datetime(2026, 9, 6)),
    ("Tuesday",   datetime(2026, 9, 15, 9, 0),  datetime(2026, 9, 6)),
    ("Wednesday", datetime(2026, 9, 16, 9, 0),  datetime(2026, 9, 6)),
    ("Thursday",  datetime(2026, 9, 17, 9, 0),  datetime(2026, 9, 6)),
    ("Friday",    datetime(2026, 9, 18, 9, 0),  datetime(2026, 9, 6)),
    ("Saturday",  datetime(2026, 9, 19, 9, 0),  datetime(2026, 9, 6)),
]:
    check(f"window/{label} floors to its Sunday", ps.window_start(today), want)

check("window/a Sunday floor is midnight, not the current hour",
      ps.window_start(datetime(2026, 9, 18, 23, 59)), FLOOR)
check_true("window/at least seven full days are always editable",
           all((d - ps.window_start(d)).days >= 7
               for d in (datetime(2026, 9, 1) + timedelta(days=i) for i in range(30))))
check_true("window/never more than a fortnight",
           all((d - ps.window_start(d)).days <= 13
               for d in (datetime(2026, 9, 1) + timedelta(days=i) for i in range(30))))


# ── 2. expire() keeps the practice, not just the week ────────────────────────
# A week on its own can't be un-done: "I wasn't there Tuesday" needs to know which.

state = ps.empty_state()
played = datetime(2026, 9, 15, 19, 45)
k = key_of(played)
ps.register_session(state, k, when_ts=played.isoformat(), label="Tue Sep 15 · 7:45 PM")
ps.toggle(state, k, ANN, "Ann Lee", when_ts=played.isoformat(), now=NOW)
dropped = ps.expire(state, NOW)
rec = state["attendance"][str(ANN)]
check("expire/session is gone", dropped, [k])
check("expire/week is credited", rec["weeks"], ["2026-W38"])
check("expire/the practice itself is kept", [p["key"] for p in rec["practices"]], [k])
check("expire/with the label the menu will show",
      at(rec["practices"], 0, "expire/record") and rec["practices"][0]["label"],
      "Tue Sep 15 · 7:45 PM")
check("expire/a sign-up alone credits nothing",
      ps.empty_state()["attendance"], {})


# ── 3. What's on the menu ────────────────────────────────────────────────────

state = seed(live=[datetime(2026, 9, 19, 13, 30)],
             past=[datetime(2026, 9, 15, 19, 45), datetime(2026, 9, 8, 19, 45)])
rows = ps.editable_practices(state, ANN, NOW)
check("menu/both halves of the pool, earliest first",
      [r["key"] for r in rows],
      ["20260908T1945", "20260915T1945", "20260919T1330"])
check("menu/a played practice is flagged as counted",
      [r["counted"] for r in rows], [True, True, False])

# Sep 5 is a Saturday — one day the wrong side of the Sep 6 floor.
state = seed(past=[datetime(2026, 9, 5, 19, 45), datetime(2026, 9, 6, 13, 30)])
check("menu/nothing older than the window's Sunday",
      [r["key"] for r in ps.editable_practices(state, ANN, NOW)], ["20260906T1330"])

state = seed(live=[datetime(2026, 9, 19, 13, 30)], past=[datetime(2026, 9, 15, 19, 45)])
check("menu/someone else's practices aren't on it",
      ps.editable_practices(state, BO, NOW), [])

# A live slot the member isn't signed up for is not theirs to remove either.
state = seed(live=[datetime(2026, 9, 19, 13, 30)])
ps.toggle(state, "20260919T1330", ANN, "Ann Lee", now=NOW)   # toggles Ann back off
check("menu/empty once they've left everything",
      ps.editable_practices(state, ANN, NOW), [])


# ── 4. Removing a live sign-up ───────────────────────────────────────────────

tonight = datetime(2026, 9, 18, 19, 45)
state = seed(live=[tonight])
out = ps.remove_practice(state, ANN, key_of(tonight), NOW)
check("live/reports itself as still on the board", (out or {}).get("live"), True)
check("live/nothing came off a streak", (out or {}).get("week_lost"), False)
check("live/the sign-up is gone", ps.count(state, key_of(tonight)), 0)
check("live/and the session stays for everyone else",
      key_of(tonight) in state["sessions"], True)
check("live/a second press finds nothing",
      ps.remove_practice(state, ANN, key_of(tonight), NOW), None)
check("live/a slot that never happened doesn't invent a streak",
      ps.current_streak(state, ANN, NOW), 0)


# ── 5. Removing a practice that's already counted ────────────────────────────

state = seed(past=[datetime(2026, 9, 15, 19, 45)])
check("counted/streak before", ps.current_streak(state, ANN, NOW), 1)
out = ps.remove_practice(state, ANN, "20260915T1945", NOW)
check("counted/says the week came off", (out or {}).get("week_lost"), True)
check("counted/not flagged as live", (out or {}).get("live"), False)
check("counted/streak after", ps.current_streak(state, ANN, NOW), 0)
check("counted/the week is off the record", state["attendance"][str(ANN)]["weeks"], [])

# Two practices in one week are still one week of streak, so taking one back leaves it.
state = seed(past=[datetime(2026, 9, 15, 19, 45), datetime(2026, 9, 17, 13, 30)])
out = ps.remove_practice(state, ANN, "20260915T1945", NOW)
check("counted/one of two in a week keeps the week", (out or {}).get("week_lost"), False)
check("counted/…and the streak", ps.current_streak(state, ANN, NOW), 1)
check("counted/…and the other practice", 
      [p["key"] for p in state["attendance"][str(ANN)]["practices"]], ["20260917T1330"])

# A run of weeks, with the most recent one taken back.
state = seed(past=[datetime(2026, 9, 1, 19, 45),     # W36
                   datetime(2026, 9, 8, 19, 45),     # W37
                   datetime(2026, 9, 15, 19, 45)])   # W38
check("streak/three weeks running", ps.current_streak(state, ANN, NOW), 3)
ps.remove_practice(state, ANN, "20260915T1945", NOW)
check("streak/removing this week's shortens it, not breaks it",
      ps.current_streak(state, ANN, NOW), 2)
check("streak/the all-time record falls with it", ps.best_streak(state, ANN), 2)
check("streak/and the leaderboard agrees",
      [(e["name"], e["streak"]) for e in ps.streak_leaderboard(state, NOW)],
      [("Ann Lee", 2)])
# Sep 1 is outside the window — history past a fortnight is not rewritable.
check("streak/an old practice is out of reach",
      ps.remove_practice(state, ANN, "20260901T1945", NOW), None)
check("streak/…and survives the attempt", ps.current_streak(state, ANN, NOW), 2)

# Removing the MIDDLE of a run breaks it rather than shortening it.
state = seed(past=[datetime(2026, 9, 1, 19, 45),
                   datetime(2026, 9, 8, 19, 45),
                   datetime(2026, 9, 15, 19, 45)])
ps.remove_practice(state, ANN, "20260908T1945", NOW)
check("streak/a hole in the middle ends the run at the hole",
      ps.current_streak(state, ANN, NOW), 1)


# ── 6. Weeks credited before the dates behind them were kept ─────────────────
# State written by an earlier build has weeks and nothing behind them. Such a week
# has no date to take back, so it would otherwise be stuck on a streak forever —
# which is exactly what happened on the first day this shipped. It's offered as the
# week itself instead.

def legacy(weeks, user=ANN) -> dict:
    """A store as the previous build left it: weeks, no practices — round-tripped
    through load(), because that's where the placeholders get filled in."""
    import json, tempfile
    raw = {"sessions": {}, "board": None,
           "attendance": {str(user): {"name": "Ann Lee", "weeks": list(weeks)}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = f.name
    out = ps.load(path)
    os.unlink(path)
    return out


state = legacy(["2026-W38"])          # the week of Mon Sep 14
check("legacy/load stands a placeholder behind every credited week",
      [(p["key"], p["week"]) for p in state["attendance"][str(ANN)]["practices"]],
      [("", "2026-W38")])
rows = ps.editable_practices(state, ANN, NOW)
check("legacy/the week is offered as a week", [r["key"] for r in rows], ["week:2026-W38"])
check("legacy/…labelled so nobody reads it as a date",
      [r["label"] for r in rows], ["Week of Sep 14 — date not recorded"])
check("legacy/…and flagged as a whole week", [r["whole_week"] for r in rows], [True])
check("legacy/streak before", ps.current_streak(state, ANN, NOW), 1)
out = ps.remove_practice(state, ANN, "week:2026-W38", NOW)
check("legacy/removing it takes the week", (out or {}).get("week_lost"), True)
check("legacy/…and the streak with it", ps.current_streak(state, ANN, NOW), 0)
check("legacy/…leaving nothing behind",
      state["attendance"][str(ANN)]["practices"], [])
check("legacy/a second go finds nothing",
      ps.remove_practice(state, ANN, "week:2026-W38", NOW), None)

# Practising again that week earns it back — the removal corrects the past, it
# doesn't bar the week.
state = legacy(["2026-W38"])
ps.remove_practice(state, ANN, "week:2026-W38", NOW)
later = datetime(2026, 9, 19, 2, 0)
ps.register_session(state, "20260918T2030", when_ts="2026-09-18T20:30:00",
                    label="Fri Sep 18 · 8:30 PM")
ps.toggle(state, "20260918T2030", ANN, "Ann Lee", when_ts="2026-09-18T20:30:00", now=NOW)
ps.expire(state, later)
check("legacy/practising again that week earns it back",
      ps.current_streak(state, ANN, later), 1)

# A dated practice landing in a week that was already credited: both are offered,
# and each stands on its own — one is a date, the other is the week's placeholder.
state = legacy(["2026-W38"])
ps._confirm_practice(state, ANN, "Ann Lee", "20260917T1330",
                     datetime(2026, 9, 17, 13, 30).isoformat(), "Thu Sep 17 · 1:30 PM")
check("legacy/both the week and the new date are on the menu",
      [r["key"] for r in ps.editable_practices(state, ANN, NOW)],
      ["week:2026-W38", "20260917T1330"])
out = ps.remove_practice(state, ANN, "20260917T1330", NOW)
check("legacy/removing the dated one leaves the week's placeholder alone",
      (out or {}).get("week_lost"), False)
check("legacy/…so the streak survives", ps.current_streak(state, ANN, NOW), 1)
check("legacy/…and the week is still offered",
      [r["key"] for r in ps.editable_practices(state, ANN, NOW)], ["week:2026-W38"])

state = legacy(["2026-W38"])
ps._confirm_practice(state, ANN, "Ann Lee", "20260917T1330",
                     datetime(2026, 9, 17, 13, 30).isoformat(), "Thu Sep 17 · 1:30 PM")
out = ps.remove_practice(state, ANN, "week:2026-W38", NOW)
check("legacy/removing the week leaves a dated practice alone",
      (out or {}).get("week_lost"), False)
check("legacy/…so the streak survives that way round too",
      ps.current_streak(state, ANN, NOW), 1)
check("legacy/…and only the dated one is left",
      [r["key"] for r in ps.editable_practices(state, ANN, NOW)], ["20260917T1330"])

# A week older than the window is no more rewritable than an old date is. The week
# is judged on its MONDAY: with no date inside it, "this week and last" is the only
# honest reading.
check("legacy/a week before last is out of reach",
      ps.editable_practices(legacy(["2026-W36"]), ANN, NOW), [])
check("legacy/…and last week is in it",
      [r["key"] for r in ps.editable_practices(legacy(["2026-W37"]), ANN, NOW)],
      ["week:2026-W37"])
check("legacy/a week out of reach can't be removed by key either",
      ps.remove_practice(legacy(["2026-W36"]), ANN, "week:2026-W36", NOW), None)
check("legacy/nor can a week that was never credited",
      ps.remove_practice(legacy(["2026-W38"]), ANN, "week:2026-W37", NOW), None)

# A placeholder is keyless AND dateless, so the window check alone would hide it.
# Belt and braces: one that somehow carried a date is still never offered as a date,
# because there is no date — that's the whole point of it.
dated = ps.empty_state()
dated["attendance"][str(ANN)] = {"name": "Ann Lee", "weeks": ["2026-W38"], "practices": [
    {"key": "", "label": "", "when_ts": "2026-09-16T19:45:00", "week": "2026-W38"}]}
check("legacy/a placeholder is never offered as a date",
      [r["key"] for r in ps.editable_practices(dated, ANN, NOW)], ["week:2026-W38"])
check_true("legacy/…and nothing on the menu is left unlabelled",
           all(r["label"] for r in ps.editable_practices(dated, ANN, NOW)))

# A store already carrying practices is left alone — no duplicate placeholders.
state = seed(past=[datetime(2026, 9, 15, 19, 45)])
before = [dict(p) for p in state["attendance"][str(ANN)]["practices"]]
ps._backfill_markers(state["attendance"][str(ANN)])
check("legacy/backfill doesn't double up on a week that has its dates",
      state["attendance"][str(ANN)]["practices"], before)


# ── 7. The components the flow renders ───────────────────────────────────────

state = seed(live=[datetime(2026, 9, 19, 13, 30)], past=[datetime(2026, 9, 15, 19, 45)])
rows = ps.editable_practices(state, ANN, NOW)
flow = botmod.UnlistFlowView(1, rows)

sel = at(flow.children, 0, "view/select is first")
check("view/one option per editable practice",
      [o.value for o in sel.options] if sel else None,
      ["20260915T1945", "20260919T1330"])
check("view/options are labelled by date, not by key",
      [o.label for o in sel.options] if sel else None,
      ["Tue Sep 15 · 7:45 PM", "Sat Sep 19 · 1:30 PM"])
check("view/a counted practice says so",
      at(sel.options, 0, "view/first option") and sel.options[0].description,
      "counted toward your streak")
check("view/an upcoming one says what happens if they leave it alone",
      at(sel.options, 1, "view/second option") and sel.options[1].description,
      "signed up — counts once it's played")
check("view/placeholder is this flow's, not a shared component's",
      sel.placeholder if sel else None, "Which practice aren't you making?…")

check("view/no confirm button before a pick",
      [type(c).__name__ for c in flow.children],
      ["UnlistSlotSelect", "UnlistCancelButton"])
flow.key = "20260915T1945"
flow.build()
check("view/confirm appears once a date is picked",
      [type(c).__name__ for c in flow.children],
      ["UnlistSlotSelect", "UnlistConfirmButton", "UnlistCancelButton"])
picker = at(flow.children, 0, "view/picker survives a rebuild")
confirm = at(flow.children, 1, "view/confirm survives a rebuild")
check("view/the pick stays visible in the dropdown",
      [o.value for o in picker.options if o.default] if picker else None,
      ["20260915T1945"])
check("view/the confirm is a danger button, not a quiet one",
      confirm.style if confirm else None, discord.ButtonStyle.danger)
check_true("view/the prompt names the practice being removed",
           "Tue Sep 15 · 7:45 PM" in flow.prompt())
check_true("view/…and warns that a counted one costs a streak",
           "streak" in flow.prompt())
flow.key = "20260919T1330"
check_true("view/an upcoming one warns the board will see it",
           "board" in flow.prompt())

check_true("view/the menu times out rather than sitting there dead",
           flow.timeout is not None)
check("view/one-shot guard starts unset", flow.submitted, False)

# The report's own button: persistent, and it round-trips its custom_id.
btn = botmod.UnlistPracticeButton(2)
check("button/custom_id carries the window", btn.item.custom_id, "sheet:unlist:2")
check("button/says what it's for", btn.item.label, "Not coming")
match = btn.template.match("sheet:unlist:3")
check_true("button/its template matches its own custom_id", match is not None)
restored = asyncio.run(botmod.UnlistPracticeButton.from_custom_id(None, None, match))
check("button/survives a restart with the same window", restored.weeks, 3)
check("button/…and rebuilds the same custom_id", restored.item.custom_id, "sheet:unlist:3")


# The week entry has to read as a week on screen, not as a date the member will
# hunt for in their calendar.
wk = botmod.UnlistFlowView(1, ps.editable_practices(legacy(["2026-W38"]), ANN, NOW))
wk_sel = at(wk.children, 0, "view/week picker")
check("view/a week reads as a week in the dropdown",
      [o.label for o in wk_sel.options] if wk_sel else None,
      ["Week of Sep 14 — date not recorded"])
wk.key = "week:2026-W38"
wk.build()
check_true("view/the prompt says no date was recorded",
           "date was recorded" in wk.prompt())
check_true("view/…and that practising again earns it back",
           "earns it back" in wk.prompt())
check_true("view/…without calling it a sign-up anyone can see go",
           "board will see" not in wk.prompt())


# ── 7b. Pressing the button, not just building it ────────────────────────────
# Constructing a view says nothing about whether the callback runs the removal or
# tells the right people. Drive the confirm button with a stub interaction.

class FakeResponse:
    def __init__(self):
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self, *a, **k):
        self.deferred = True


class FakeInteraction:
    def __init__(self, user_id=ANN):
        self.response = FakeResponse()
        self.user = type("U", (), {"id": user_id, "display_name": "Ann Lee"})()
        self.channel = object()
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


def press(flow, interaction):
    """Press the confirm button. Records a failure and returns rather than raising if
    it isn't there — a regression that removes the button must produce FAIL lines,
    not one traceback that swallows every failure recorded before it."""
    confirm = next((c for c in flow.children
                    if type(c).__name__ == "UnlistConfirmButton"), None)
    if confirm is None:
        FAILS.append(f"press/no confirm button to press on {flow.key!r}")
        return
    asyncio.run(confirm.callback(interaction))


def with_store(state, fn):
    """Run fn with the bot pointed at `state` and everything that talks to Discord
    stubbed out, recording what it tried to do."""
    calls = {"bumped": 0, "repainted": 0, "rebuilt": 0}

    async def fake_payload(weeks, user):
        calls["rebuilt"] += 1
        return discord.Embed(title="report"), discord.ui.View()

    async def fake_bump(channel):
        calls["bumped"] += 1

    async def fake_render(channel=None):
        calls["repainted"] += 1

    async def fake_board_channel():
        return None

    saved = {k: getattr(botmod, k) for k in
             ("build_sheets_payload", "bump_practice_board", "render_practice_board",
              "_board_channel", "_practice_state", "PRACTICE_STORE_PATH")}
    with tempfile.TemporaryDirectory() as d:
        botmod.build_sheets_payload = fake_payload
        botmod.bump_practice_board = fake_bump
        botmod.render_practice_board = fake_render
        botmod._board_channel = fake_board_channel
        botmod._practice_state = state
        botmod.PRACTICE_STORE_PATH = os.path.join(d, "practice_signups.json")
        try:
            fn(calls)
        finally:
            for k, v in saved.items():
                setattr(botmod, k, v)
    return calls


# Dropping out of tonight's practice: the club is watching the board, so republish it.
state = seed(live=[datetime(2026, 9, 18, 19, 45)])


def drop_live(calls):
    flow = botmod.UnlistFlowView(1, ps.editable_practices(state, ANN, NOW))
    flow.key = "20260918T1945"
    flow.build()
    it = FakeInteraction()
    press(flow, it)
    check("press/the sign-up is actually gone", ps.count(state, "20260918T1945"), 0)
    check("press/the board is republished where everyone's looking", calls["bumped"], 1)
    check("press/…and not also repainted in place", calls["repainted"], 0)
    check("press/the report is rebuilt under the answer", calls["rebuilt"], 1)
    edit = at(it.edits, -1, "press/an edit was sent")
    check_true("press/the answer names the date",
               edit is not None and "Sep 18" in (edit.get("content") or ""))
    check_true("press/…and doesn't mention a streak it didn't touch",
               edit is not None and "streak" not in (edit.get("content") or ""))
    # One-shot: a second press must not run a second removal or a second bump.
    press(flow, it)
    check("press/a second press is a no-op", (calls["bumped"], calls["rebuilt"]), (1, 1))


with_store(state, drop_live)
check("press/the store was written", ps.count(state, "20260918T1945"), 0)

# Correcting a practice that already happened: nothing on the board changes, but the
# streak table printed on it does — so repaint where it lives, don't repost it.
state = seed(past=[datetime(2026, 9, 15, 19, 45)])


def drop_past(calls):
    flow = botmod.UnlistFlowView(1, ps.editable_practices(state, ANN, NOW))
    flow.key = "20260915T1945"
    flow.build()
    it = FakeInteraction()
    press(flow, it)
    check("press/a past correction doesn't repost the board", calls["bumped"], 0)
    check("press/…it repaints it in place", calls["repainted"], 1)
    check("press/the streak is recomputed", ps.current_streak(state, ANN, NOW), 0)
    edit = at(it.edits, -1, "press/an edit was sent")
    check_true("press/and they're told the week came off",
               edit is not None and "streak" in (edit.get("content") or ""))


with_store(state, drop_past)

# Correcting a week with no date on record: the streak drops, and nothing about the
# board's sign-up list changed, so it's repainted rather than reposted.
state = legacy(["2026-W38"])


def drop_week(calls):
    flow = botmod.UnlistFlowView(1, ps.editable_practices(state, ANN, NOW))
    flow.key = "week:2026-W38"
    flow.build()
    it = FakeInteraction()
    press(flow, it)
    check("press/the week comes off the streak", ps.current_streak(state, ANN, NOW), 0)
    check("press/a week correction doesn't repost the board", calls["bumped"], 0)
    check("press/…it repaints it", calls["repainted"], 1)
    edit = at(it.edits, -1, "press/an edit was sent")
    content = (edit or {}).get("content") or ""
    check_true("press/the answer names the week", "Week of Sep 14" in content)
    check_true("press/…and says it came off the streak", "streak" in content)
    check_true("press/…without claiming they were taken off a sign-up",
               "Took you off" not in content)


with_store(state, drop_week)


# Someone else's key, pressed against this member's menu, removes nothing.
state = seed(past=[datetime(2026, 9, 15, 19, 45)])


def drop_stale(calls):
    flow = botmod.UnlistFlowView(1, ps.editable_practices(state, ANN, NOW))
    flow.key = "20260915T1945"
    flow.build()
    ps.remove_practice(state, ANN, "20260915T1945", NOW)   # gone between menu and press
    it = FakeInteraction()
    press(flow, it)
    check("press/a stale pick says so instead of erroring", calls["bumped"], 0)
    edit = at(it.edits, -1, "press/an edit was sent")
    check_true("press/…in words, not a traceback",
               edit is not None and "already" in (edit.get("content") or ""))


with_store(state, drop_stale)


# ── 8. A select never mutates; only a button does ────────────────────────────
# Mis-tapping a dropdown has no undo, so every store write in this file has to sit
# behind a labelled button. Same audit as the one over subs.py, run over bot.py.

MUTATORS = {"toggle", "remove_practice", "expire", "register_session", "save",
            "add", "release", "_confirm_practice"}
tree = ast.parse(open("bot.py", encoding="utf-8").read())
offenders, seen = [], []
for node in tree.body:
    if not isinstance(node, ast.ClassDef) or not node.name.endswith(("Select", "Pick")):
        continue
    cb = next((n for n in node.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "callback"), None)
    if cb is None:
        continue
    seen.append(node.name)
    for sub in ast.walk(cb):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in MUTATORS):
            offenders.append(f"{node.name}.{sub.func.attr}")
check("invariant/every select in bot.py was audited", sorted(seen),
      ["BlockCountSelect", "BlockSlotSelect", "ReleaseBlockSelect", "UnlistSlotSelect"])
check("invariant/no select mutates shared state", offenders, [])
# The rule is worth nothing if the mutators it looks for stop matching the code, so
# check the audit still has something to find: the release it used to miss now sits
# in a button, and that button must be the thing calling it.
buttons = {}
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name.endswith("Button"):
        buttons[node.name] = {n.func.attr for n in ast.walk(node)
                              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
check_true("invariant/releasing moved to a button, it didn't just vanish",
           "release" in buttons.get("ReleaseConfirmButton", set()))
check_true("invariant/…and the removal likewise",
           "remove_practice" in buttons.get("UnlistFlowView", set())
           or "remove" in buttons.get("UnlistConfirmButton", set()))

# The removal is wired to the button, not left as dead code.
src = open("bot.py", encoding="utf-8").read()
check_true("wiring/the confirm button runs the removal",
           "await flow.remove(interaction)" in src)
check_true("wiring/the report offers the button", "UnlistPracticeButton(weeks)" in src)
check("wiring/…on the empty report too", src.count("UnlistPracticeButton(weeks)"), 2)
check_true("wiring/and it's registered so it survives a restart",
           "UnlistPracticeButton,\n        BlockSheetsButton," in src)
# A live slot is republished where everyone's watching; a past one only repaints the
# streak table on the board it already lives on.
check_true("wiring/a live removal bumps the board",
           'if removed["live"]:\n            await bump_practice_board' in src)
check_true("wiring/a past one refreshes in place",
           "await render_practice_board(await _board_channel())" in src)


# ── 9. Copy ──────────────────────────────────────────────────────────────────
# Some leagues curl in daylight, so nothing user-facing says "night".

user_text = [flow.prompt(), botmod.UnlistPracticeButton(1).item.label,
             botmod.UnlistSlotSelect(rows, None).placeholder]
user_text += [o.description for o in botmod.UnlistSlotSelect(rows, None).options]
check("copy/nothing says 'night'",
      [t for t in user_text if "night" in (t or "").lower()], [])


# ── Report ───────────────────────────────────────────────────────────────────

if FAILS:
    print(f"\n❌  {len(FAILS)} failure(s):\n")
    for f in FAILS:
        print(" - " + f)
    raise SystemExit(1)
print("✅  all unlist checks passed")
