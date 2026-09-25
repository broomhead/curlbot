"""Unit tests for sheet blocks + the today-forward board floor (2026-08-20).

Run:  python3 test_blocks.py

Covers: the block store (add/release/expire/as_sessions), the manual-entry
parsers, blocks inside the free-sheet arithmetic, the Discord components the
block flow actually renders, and the availability fix that stopped last
Sunday's games showing on Thursday's board.

Deliberately exercises the VIEW layer, not just the pure functions: every
curlbot bug that has reached production so far lived in the Discord layer that
pure-function tests never touch. Constructing the selects and reading their
options is the closest we get to clicking without a gateway connection — it is
NOT a substitute for a live smoke test.
"""
import asyncio
from datetime import date, datetime, timedelta

import os

import discord

# bot.py calls bot.run() at import time; neuter it so we can import the module.
discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import block_store as bs
import bot as botmod
import practice_ice as pi
import practice_store as ps
import sub_store as store
import subs

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


def check_true(name, got):
    if not got:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: truthy")


def raises(label, fn, /, *a, **k):
    """Positional-only params: several of the functions under test take a `name`
    keyword of their own, which a normal signature would collide with."""
    try:
        fn(*a, **k)
    except ValueError:
        return
    except Exception as ex:  # noqa: BLE001
        FAILS.append(f"{label}\n   raised {type(ex).__name__}, wanted ValueError")
        return
    FAILS.append(f"{label}\n   did not raise")


NOW = datetime(2026, 8, 20, 10, 0)          # Thu Aug 20, 10am
SUN = datetime(2026, 8, 23, 13, 30)         # the Sunday Dana booked


# ── 1. Block store: add / release / expire ───────────────────────────────────

st = bs.empty_state()
b = bs.add(st, start=SUN, end=SUN + timedelta(hours=2, minutes=30), sheets=2,
           user_id=42, name="Dana", reason="LTC (off the books)", now=NOW)
check("add/id", b["id"], "20260823T1330-1")
check("add/sheets", b["sheets"], 2)
check("add/who", (b["user_id"], b["name"]), (42, "Dana"))

# Two blocks on the same slot must not collide on id.
b2 = bs.add(st, start=SUN, end=SUN + timedelta(hours=1), sheets=1,
            user_id=7, name="Kim", now=NOW)
check("add/second id", b2["id"], "20260823T1330-2")
check("add/both stored", len(st["blocks"]), 2)

raises("add/zero-length", bs.add, st, start=SUN, end=SUN, sheets=1, user_id=1, name="x")
raises("add/backwards", bs.add, st, start=SUN, end=SUN - timedelta(hours=1),
       sheets=1, user_id=1, name="x")
raises("add/too many sheets", bs.add, st, start=SUN, end=SUN + timedelta(hours=1),
       sheets=99, user_id=1, name="x")
raises("add/zero sheets", bs.add, st, start=SUN, end=SUN + timedelta(hours=1),
       sheets=0, user_id=1, name="x")
# add() does NOT cap duration: a block placed against a slot picked off the report
# inherits that session's real window, and an all-day practice block on the club
# calendar is legitimately 14+ hours. The cap is a typo guard on TYPED input only,
# so it lives in parse_duration (exercised below).
allday = bs.add(st, start=datetime(2026, 8, 23, 0, 0), end=datetime(2026, 8, 23, 23, 59),
                sheets=1, user_id=1, name="Calendar", now=NOW)
check("add/accepts an all-day calendar window", allday["sheets"], 1)
bs.release(st, allday["id"])

check("release/returns it", bs.release(st, b2["id"])["id"], b2["id"])
check("release/gone", bs.get(st, b2["id"]), None)
check("release/twice is safe", bs.release(st, b2["id"]), None)

# Expiry: a finished block sweeps, an upcoming one stays.
st2 = bs.empty_state()
past = bs.add(st2, start=NOW - timedelta(days=1), end=NOW - timedelta(days=1) + timedelta(hours=2),
              sheets=1, user_id=1, name="Old", now=NOW)
future = bs.add(st2, start=SUN, end=SUN + timedelta(hours=2), sheets=1,
                user_id=1, name="Soon", now=NOW)
dropped = bs.expire(st2, NOW, 1)
check("expire/dropped the past one", dropped, [past["id"]])
check("expire/kept the future one", list(st2["blocks"]), [future["id"]])

# A block still inside its grace window survives (ice that ran long).
st3 = bs.empty_state()
bs.add(st3, start=NOW - timedelta(hours=2), end=NOW - timedelta(minutes=30),
       sheets=1, user_id=1, name="Running late", now=NOW)
check("expire/grace keeps a just-finished block", len(bs.expire(st3, NOW, 1)), 0)

# A block whose dates are corrupt is kept, never silently dropped.
st4 = {"blocks": {"bad": {"id": "bad", "start": "nope", "end": "nope", "sheets": 1}}}
check("expire/keeps unparseable", bs.expire(st4, NOW, 1), [])
check("as_sessions/skips unparseable", bs.as_sessions(st4), [])


# ── 2. Manual-entry parsing ──────────────────────────────────────────────────

TODAY = date(2026, 8, 20)   # a Thursday
check("date/today", bs.parse_date("today", today=TODAY), TODAY)
check("date/tomorrow", bs.parse_date("Tomorrow", today=TODAY), date(2026, 8, 21))
check("date/weekday next", bs.parse_date("sunday", today=TODAY), date(2026, 8, 23))
check("date/same weekday means next week",
      bs.parse_date("thursday", today=TODAY), date(2026, 8, 27))
check("date/mon name", bs.parse_date("Aug 23", today=TODAY), date(2026, 8, 23))
check("date/mon name long", bs.parse_date("August 23", today=TODAY), date(2026, 8, 23))
check("date/day first", bs.parse_date("23 Aug", today=TODAY), date(2026, 8, 23))
check("date/slashes", bs.parse_date("8/23", today=TODAY), date(2026, 8, 23))
check("date/slashes 2-digit year", bs.parse_date("8/23/27", today=TODAY), date(2027, 8, 23))
check("date/iso", bs.parse_date("2026-08-23", today=TODAY), date(2026, 8, 23))
# December → January rolls the year rather than booking the past.
check("date/rolls the year", bs.parse_date("Jan 4", today=date(2026, 12, 20)), date(2027, 1, 4))
raises("date/garbage", bs.parse_date, "next week sometime", today=TODAY)
raises("date/not real", bs.parse_date, "Feb 30", today=TODAY)
raises("date/empty", bs.parse_date, "", today=TODAY)

check("clock/pm", bs.parse_clock("1:30 PM"), (13, 30))
check("clock/pm tight", bs.parse_clock("1:30pm"), (13, 30))
check("clock/hour only", bs.parse_clock("7 pm"), (19, 0))
check("clock/24h", bs.parse_clock("13:30"), (13, 30))
check("clock/midnight-ish 24h", bs.parse_clock("09:15"), (9, 15))
check("clock/am", bs.parse_clock("9:00 AM"), (9, 0))
check("clock/12am is midnight", bs.parse_clock("12:00 AM"), (0, 0))
check("clock/12pm is noon", bs.parse_clock("12:00 PM"), (12, 0))
# Bare "6" at a curling club means the evening draw, not 6am.
check("clock/bare evening", bs.parse_clock("6"), (18, 0))
check("clock/bare morning stays", bs.parse_clock("9"), (9, 0))
raises("clock/garbage", bs.parse_clock, "half past six")
raises("clock/bad minutes", bs.parse_clock, "1:75 PM")

check("dur/bare hours", bs.parse_duration("2"), timedelta(hours=2))
check("dur/decimal", bs.parse_duration("2.5"), timedelta(hours=2, minutes=30))
check("dur/h suffix", bs.parse_duration("2h"), timedelta(hours=2))
check("dur/minutes", bs.parse_duration("90m"), timedelta(minutes=90))
check("dur/colon", bs.parse_duration("2:15"), timedelta(hours=2, minutes=15))
check("dur/hours and minutes", bs.parse_duration("1h 30m"), timedelta(hours=1, minutes=30))
raises("dur/zero", bs.parse_duration, "0")
raises("dur/garbage", bs.parse_duration, "a while")
raises("dur/too long", bs.parse_duration, "24h")

check("sheets/plain", bs.parse_sheets("2", 4), 2)
check("sheets/wordy", bs.parse_sheets("2 sheets", 4), 2)
raises("sheets/over", bs.parse_sheets, "9", 4)
raises("sheets/zero", bs.parse_sheets, "0", 4)
raises("sheets/none", bs.parse_sheets, "two", 4)

start, end, sheets = bs.parse_manual("Aug 23", "1:30 PM", "2.5", "2", now=NOW, total=4)
check("manual/start", start, SUN)
check("manual/end", end, datetime(2026, 8, 23, 16, 0))
check("manual/sheets", sheets, 2)
raises("manual/past", bs.parse_manual, "2026-08-01", "1:30 PM", "2", "2", now=NOW, total=4)
# A recent bare date is a typo, NOT next year's — it must be refused, not silently
# booked 11 months out where nobody would ever see it again.
check("date/recent past does not roll a year",
      bs.parse_date("Aug 1", today=TODAY), date(2026, 8, 1))
raises("manual/recent past refused", bs.parse_manual, "Aug 1", "1:30 PM", "2", "2",
       now=NOW, total=4)
# ...while a genuinely long-past bare date still means the coming one.
check("date/long past still rolls", bs.parse_date("Jan 4", today=TODAY), date(2027, 1, 4))
raises("manual/absurdly far out", bs.parse_manual, "2027-08-23", "1:30 PM", "2", "2",
       now=NOW, total=4)


# ── 3. Blocks in the free-sheet arithmetic ───────────────────────────────────

def practice(h, m, eh, em=0, **kw):
    return {"start": datetime(2026, 8, 23, h, m), "end": datetime(2026, 8, 23, eh, em),
            "type": "Practice", "title": "Sunday Practice", "sheets_used": 0, **kw}


blk = bs.empty_state()
bs.add(blk, start=SUN, end=datetime(2026, 8, 23, 15, 0), sheets=2,
       user_id=42, name="Dana", reason="LTC", now=NOW)

sessions = [practice(13, 30, 16)] + bs.as_sessions(blk)
opps = pi.practice_opportunities(sessions, 4)
check("math/one row only (block gets none)", len(opps), 1)
check("math/free reduced", opps[0]["free"], 2)
check("math/row knows its block", len(opps[0]["blocks"]), 1)

icon, line = pi.format_opportunity(opps[0], 4)
check("format/icon", icon, "🟢")
check_true("format/names the blocker", "blocked by Dana" in line)
check_true("format/gives the reason", "LTC" in line)
check_true("format/spells out the partial window", "1:30–3:00 PM" in line)

# Blocking everything closes the slot rather than hiding it.
blk_all = bs.empty_state()
bs.add(blk_all, start=SUN, end=datetime(2026, 8, 23, 16, 0), sheets=4,
       user_id=42, name="Dana", now=NOW)
opps_full = pi.practice_opportunities([practice(13, 30, 16)] + bs.as_sessions(blk_all), 4)
check("math/full block leaves zero", opps_full[0]["free"], 0)
check("math/slot still listed", opps_full[0]["type"], "Practice")
check("format/full block icon", pi.format_opportunity(opps_full[0], 4)[0], "🔴")

# Blocks stack with a real booking rather than replacing it.
ltc = {"start": SUN, "end": datetime(2026, 8, 23, 16, 0), "type": "LTC",
       "title": "Learn to Curl", "sheets_used": 1}
stacked = pi.practice_opportunities([practice(13, 30, 16), ltc] + bs.as_sessions(blk), 4)
check("math/stacks with a real booking", {o["type"]: o["free"] for o in stacked},
      {"Practice": 1, "LTC": 1})

# A block that misses the session entirely changes nothing.
elsewhere = bs.empty_state()
bs.add(elsewhere, start=datetime(2026, 8, 23, 18, 0), end=datetime(2026, 8, 23, 20, 0),
       sheets=4, user_id=1, name="Someone", now=NOW)
missed = pi.practice_opportunities([practice(13, 30, 16)] + bs.as_sessions(elsewhere), 4)
check("math/non-overlapping block is inert", missed[0]["free"], 4)
check("math/and adds no note", missed[0]["blocks"], [])

# A block never becomes a row of its own, even with nothing else on the ice.
alone = pi.practice_opportunities(bs.as_sessions(blk), 4)
check("math/block alone renders nothing", alone, [])

# Windowing keeps out-of-range blocks out of the math.
check("as_sessions/window excludes before",
      bs.as_sessions(blk, datetime(2026, 8, 24), datetime(2026, 8, 25)), [])
check("as_sessions/window includes overlap",
      len(bs.as_sessions(blk, datetime(2026, 8, 23), datetime(2026, 8, 24))), 1)


# A league draw blocked down to zero must STAY on the report. It's the only place
# the block is ever shown, so hiding the row hid the block with it and the ice
# just silently vanished.
draw = {"start": datetime(2026, 8, 20, 19, 45), "end": datetime(2026, 8, 20, 22, 0),
        "type": "League", "title": "Thursday League", "sheets_used": 3}
lg = bs.empty_state()
bs.add(lg, start=draw["start"], end=draw["end"], sheets=1, user_id=1,
       name="Dana", reason="LTC", now=NOW)
blocked_league = pi.practice_opportunities([draw] + bs.as_sessions(lg), 4)
check("math/blocked league keeps its row", [o["type"] for o in blocked_league], ["League"])
check("math/blocked league at zero", blocked_league[0]["free"], 0)
check_true("math/and still explains itself",
           "blocked by Dana" in pi.format_opportunity(blocked_league[0], 4)[1])
# ...but an unblocked full league is still hidden, as before.
check("math/full league with no block stays hidden",
      pi.practice_opportunities([{**draw, "sheets_used": 4}], 4), [])

# annotate() sees what the display rules hide — the bot reasons over this.
check("math/annotate keeps the hidden row",
      [o["type"] for o in pi.annotate([{**draw, "sheets_used": 4}], 4)], ["League"])
check("math/annotate never emits a block row",
      pi.annotate(bs.as_sessions(lg), 4), [])

# Block notes are capped so a busy slot can't blow the 4096-char embed limit.
many_blocks = bs.empty_state()
for i in range(6):
    bs.add(many_blocks, start=SUN, end=datetime(2026, 8, 23, 16, 0), sheets=1,
           user_id=i, name=f"Member{i}", reason="x" * 100, now=NOW)
noisy = pi.practice_opportunities([practice(13, 30, 16)] + bs.as_sessions(many_blocks), 4)
notes = pi.block_notes(noisy[0])
check("format/notes capped", len(notes), pi.MAX_BLOCK_NOTES + 1)
check_true("format/cap is summarised", "3 more block" in notes[-1])
check_true("format/row stays well inside the embed limit",
           len(pi.format_opportunity(noisy[0], 4)[1]) < 700)


# A league that was ALREADY booked solid must stay hidden — "a block overlaps this
# row" is not "a block took ice off this row", and blaming the blocker for ice
# they never took is worse than saying nothing.
full_draw = {**draw, "sheets_used": 4}
evening = {"start": datetime(2026, 8, 20, 18, 0), "end": datetime(2026, 8, 20, 19, 30),
           "type": "Practice", "title": "Practice", "sheets_used": 0}
spanning = bs.empty_state()
bs.add(spanning, start=datetime(2026, 8, 20, 18, 0), end=datetime(2026, 8, 20, 20, 0),
       sheets=1, user_id=1, name="Dana", reason="LTC", now=NOW)
mixed = pi.practice_opportunities([evening, full_draw] + bs.as_sessions(spanning), 4)
check("math/already-full league stays hidden", [o["type"] for o in mixed], ["Practice"])
check("math/the row it did touch is right", mixed[0]["free"], 3)
check("math/untouched row carries no block note",
      pi.annotate([evening, full_draw] + bs.as_sessions(spanning), 4)[1]["blocks"], [])
check("math/free_if_unblocked recorded", mixed[0]["free_if_unblocked"], 4)

# Multi-day / midnight-spanning windows name both ends.
check("format/window spanning midnight names both days",
      bs.fmt_window(datetime(2026, 8, 23, 22, 0), datetime(2026, 8, 24, 1, 0)),
      "Sun Aug 23 10:00 PM – Mon Aug 24 1:00 AM")


# ── 4. The components Discord actually renders ───────────────────────────────

live = bs.empty_state()
held = bs.add(live, start=SUN, end=datetime(2026, 8, 23, 16, 0), sheets=2,
              user_id=42, name="Dana", reason="LTC", now=NOW)
rows = pi.practice_opportunities([practice(13, 30, 16), practice(19, 45, 22)]
                                 + bs.as_sessions(live), 4)

flow = botmod.BlockFlowView(1, rows, [held])
slot_select = flow.children[0]
check("view/opens on the slot picker", type(slot_select).__name__, "BlockSlotSelect")
check("view/one option per slot plus manual entry",
      [o.value for o in slot_select.options],
      ["20260823T1330", "20260823T1945", botmod.BLOCK_OTHER])
check("view/release menu present when something is blocked",
      any(type(c).__name__ == "ReleaseBlockSelect" for c in flow.children), True)
check("view/no count picker until a slot is chosen",
      any(type(c).__name__ == "BlockCountSelect" for c in flow.children), False)

flow.slot_key = "20260823T1330"
flow.build()
kinds = [type(c).__name__ for c in flow.children]
check("view/full flow after picking a slot", kinds,
      ["BlockSlotSelect", "BlockCountSelect", "BlockConfirmButton", "ReleaseBlockSelect"])
count_select = flow.children[1]
check("view/one count option per sheet", [o.value for o in count_select.options],
      ["1", "2", "3", "4"])
check_true("view/count options show what's left", "leaves 1 free" in
           [o.description for o in count_select.options][0])
check_true("view/prompt names the slot", "Sun Aug 23" in flow.prompt())
check_true("view/prompt shows the blocker", "Dana" in flow.prompt())

# Every select stays inside Discord's limits, and rows never double up.
for child in flow.children:
    if isinstance(child, discord.ui.Select):
        for o in child.options:
            check_true(f"limits/{type(child).__name__} label <=100", len(o.label) <= 100)
            check_true(f"limits/{type(child).__name__} desc <=100",
                       len(o.description or "") <= 100)
        check_true(f"limits/{type(child).__name__} <=25 options", len(child.options) <= 25)
check("limits/one component per row",
      sorted(c.row for c in flow.children), [0, 1, 2, 3])

# A report with no free ice still offers the menu (manual entry + release).
empty_flow = botmod.BlockFlowView(1, [], [])
check("view/manual entry survives an empty report",
      [o.value for o in empty_flow.children[0].options], [botmod.BLOCK_OTHER])
check("view/no release menu when nothing is blocked", len(empty_flow.children), 1)

# More slots than a select can hold: 24 + the manual-entry option = 25 exactly.
many = [practice(8 + i // 2, 30 * (i % 2), 9 + i // 2, 30 * (i % 2)) for i in range(30)]
big = botmod.BlockFlowView(1, pi.practice_opportunities(many, 4), [])
check("view/caps at Discord's 25 options", len(big.children[0].options), 25)
check("view/manual entry survives the cap",
      big.children[0].options[-1].value, botmod.BLOCK_OTHER)

# The persistent button's custom_id must round-trip through its own template.
btn = botmod.BlockSheetsButton(3)
check("button/custom_id", btn.item.custom_id, "sheet:block:3")
check_true("button/template matches its own id",
           botmod.BlockSheetsButton.__discord_ui_compiled_template__.match("sheet:block:3") is not None)
# ...and must NOT swallow a sign-up button's id (both start "sheet:").
check("button/doesn't match a join id",
      botmod.BlockSheetsButton.__discord_ui_compiled_template__.match("sheet:join:20260823T1330:1"), None)
check("join/doesn't match a block id",
      botmod.JoinPracticeButton.__discord_ui_compiled_template__.match("sheet:block:3"), None)

# Modals: Discord allows five inputs, no more.
manual = botmod.ManualBlockModal(1)
check("modal/manual field count", len(manual.children), 5)
check_true("modal/manual fits Discord's cap", len(manual.children) <= 5)
check("modal/reason-only field count", len(botmod.BlockReasonModal(1, SUN, SUN, 1).children), 1)
check("modal/only the reason is optional",
      [c.required for c in manual.children], [True, True, True, True, False])


# The picker must survive a cancelled modal: stopping the view deregisters it and
# every later click answers "This interaction failed".
check("view/not stopped while the modal is open", flow.is_finished(), False)
check_true("view/times out into disabled controls", hasattr(flow, "on_timeout"))

# Hand-editing sheet_blocks.json is the documented escape hatch, so an entry
# written without an "id" must still load and still be releasable.
import json
import tempfile

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"blocks": {"20260823T1330-1": {
        "start": "2026-08-23T13:30:00", "end": "2026-08-23T16:00:00",
        "sheets": 1, "name": "Hand"}}}, fh)
    handmade_path = fh.name
handmade = bs.load(handmade_path)
os.unlink(handmade_path)
check("store/id backfilled on load",
      handmade["blocks"]["20260823T1330-1"]["id"], "20260823T1330-1")
check("store/hand-written block is releasable",
      botmod.ReleaseBlockSelect(bs.active(handmade)).options[0].value, "20260823T1330-1")
# A select with zero options is itself a 50035, so the view must omit the release
# menu entirely rather than render an empty one.
idless = botmod.BlockFlowView(1, [], [])
check("view/no empty release menu",
      [type(c).__name__ for c in idless.children], ["BlockSlotSelect"])


# ── Releasing takes two taps ─────────────────────────────────────────────────
# The release dropdown used to let a block go the instant it was touched, putting
# sheets back on everyone's report and announcing it in the channel — from a
# mis-tap, with no undo. Picking now only picks; a labelled button does the work.

held_block = {"id": "20260823T1330-2", "start": "2026-08-23T13:30:00",
              "end": "2026-08-23T16:00:00", "sheets": 2, "name": "Robin Vale",
              "reason": "LTC", "user_id": 7}
rel = botmod.BlockFlowView(1, rows, [held_block])
check("release/no confirm button before a block is picked",
      [type(c).__name__ for c in rel.children], ["BlockSlotSelect", "ReleaseBlockSelect"])
rel.release_id = "20260823T1330-2"
rel.build()
check("release/picking one adds the confirm",
      [type(c).__name__ for c in rel.children],
      ["BlockSlotSelect", "ReleaseBlockSelect", "ReleaseConfirmButton"])
check("release/the confirm is a danger button",
      rel.children[2].style, discord.ButtonStyle.danger)
check("release/the pick stays visible in the dropdown",
      [o.value for o in rel.children[1].options if o.default], ["20260823T1330-2"])
check_true("release/the prompt names the block being released",
           "Robin Vale" in rel.prompt() or "LTC" in rel.prompt())
check_true("release/…and warns the channel gets told", "channel" in rel.prompt())
# A stale pick (someone else released it first) must not leave a live confirm —
# with other blocks still listed, so the confirm is dropped on the PICK being gone
# rather than on the menu being empty.
other = dict(held_block, id="20260823T1330-9", name="Ann Lee")
rel.blocks = [other]
rel.build()
check("release/a vanished pick takes its confirm with it, menu and all",
      [type(c).__name__ for c in rel.children], ["BlockSlotSelect", "ReleaseBlockSelect"])
check_true("release/…and the prompt stops naming it", "Releasing:" not in rel.prompt())
rel.blocks = []
rel.build()
check("release/an empty menu renders neither control",
      [type(c).__name__ for c in rel.children], ["BlockSlotSelect"])


# The select's own callback records the pick and nothing else — no store write, no
# channel note. Driven directly rather than inspected, because the whole bug was in
# the callback.
class _RelResponse:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class _RelInteraction:
    def __init__(self):
        self.response = _RelResponse()
        self.user = type("U", (), {"id": 7, "display_name": "Robin Vale"})()
        self.channel = None


def _touch_release_select():
    botmod._block_state = bs.empty_state()
    botmod.BLOCK_STORE_PATH = "/tmp/test_blocks_release.json"
    botmod.now_club = lambda: NOW
    placed = bs.add(botmod._block_state, start=SUN, end=datetime(2026, 8, 23, 16, 0),
                    sheets=2, user_id=7, name="Robin Vale", reason="LTC", now=NOW)
    flow = botmod.BlockFlowView(1, rows, bs.active(botmod._block_state))
    sel = next(c for c in flow.children if type(c).__name__ == "ReleaseBlockSelect")
    sel._values = [placed["id"]]          # what Discord hands a select on touch
    asyncio.run(sel.callback(_RelInteraction()))
    return flow, placed


def _press_release_twice():
    """An impatient double-tap must release once and answer once, and must not
    repaint the menu with "that block was already gone" — a baffling reply to a
    click the member only meant to make once. The second press is aimed at the
    button the FIRST press is still in the middle of replacing, which is exactly
    what a tap landing before the edit does."""
    botmod._block_state = bs.empty_state()
    botmod.BLOCK_STORE_PATH = "/tmp/test_blocks_release2.json"
    botmod.now_club = lambda: NOW
    announced = []

    async def fake_publish(interaction, weeks, note):
        announced.append(note)

    placed = bs.add(botmod._block_state, start=SUN, end=datetime(2026, 8, 23, 16, 0),
                    sheets=2, user_id=7, name="Robin Vale", reason="LTC", now=NOW)
    flow = botmod.BlockFlowView(1, rows, bs.active(botmod._block_state))
    flow.release_id = placed["id"]
    flow.build()
    btn = next(c for c in flow.children if type(c).__name__ == "ReleaseConfirmButton")
    it = _RelInteraction()
    saved = botmod._publish_block_change
    botmod._publish_block_change = fake_publish
    try:
        asyncio.run(btn.callback(it))
        btn._view = flow            # the rebuild detached it; the member's finger didn't
        asyncio.run(btn.callback(it))   # the double-tap
    finally:
        botmod._publish_block_change = saved
    return flow, it, announced


twice, twice_it, announced = _press_release_twice()
check("release/a double-tap releases once", len(bs.active(botmod._block_state)), 0)
check("release/…announces once", len(announced), 1)
check("release/…and acks both, so neither tap fails", len(twice_it.response.edits), 2)
check_true("release/…without telling them it was already gone",
           "already gone" not in twice.status)



# …and the same double-tap with another member's release in flight, which is the
# only way two presses both get past the release_id clear. There's no debounce on
# this path — releasing by id is idempotent, so the second press gets nothing back
# and falls out without re-announcing. This is the test that says so.
async def _contended_double_release():
    botmod._block_state = bs.empty_state()
    botmod.BLOCK_STORE_PATH = "/tmp/test_blocks_release3.json"
    botmod.now_club = lambda: NOW
    notes = []

    async def fake_publish(interaction, weeks, note):
        notes.append(note)

    placed = bs.add(botmod._block_state, start=SUN, end=datetime(2026, 8, 23, 16, 0),
                    sheets=2, user_id=7, name="Robin Vale", reason="LTC", now=NOW)
    flow = botmod.BlockFlowView(1, rows, bs.active(botmod._block_state))
    flow.release_id = placed["id"]
    flow.build()
    btn = next(c for c in flow.children if type(c).__name__ == "ReleaseConfirmButton")
    a, b = _RelInteraction(), _RelInteraction()
    saved = botmod._publish_block_change
    botmod._publish_block_change = fake_publish
    try:
        async with botmod._block_lock:      # somebody else is mid-release
            t1 = asyncio.create_task(btn.callback(a))
            t2 = asyncio.create_task(btn.callback(b))
            await asyncio.sleep(0)          # both reach the lock and queue on it
            await asyncio.sleep(0)
        await asyncio.gather(t1, t2)
    finally:
        botmod._publish_block_change = saved
    return flow, notes, a, b


raced, race_notes, a, b = asyncio.run(_contended_double_release())
check("release/a contended double-tap releases once", len(bs.active(botmod._block_state)), 0)
check("release/…and announces once", len(race_notes), 1)
check("release/…while both taps are acked", len(a.response.edits) + len(b.response.edits), 2)


touched, placed = _touch_release_select()
check("release/touching the dropdown releases nothing",
      bs.get(botmod._block_state, placed["id"]) is not None, True)
check("release/…it only records the pick", touched.release_id, placed["id"])
check("release/…and the sheets are still held",
      len(bs.active(botmod._block_state)), 1)


# A timed-out picker must stay dead: rebuilding it would put fresh ENABLED
# controls on a message Discord no longer routes — the exact dead-but-live-looking
# menu on_timeout exists to prevent. (A modal has no timeout of its own, so a slow
# submit really can outlive the view.)
async def _retire_cases():
    # retire() refreshes the release list from the LIVE store, so seed it — that
    # refresh is half the point (a block that just landed has to be releasable).
    botmod._block_state = bs.empty_state()
    botmod.BLOCK_STORE_PATH = "/tmp/test_blocks_store.json"
    botmod.now_club = lambda: NOW
    bs.add(botmod._block_state, start=SUN, end=datetime(2026, 8, 23, 16, 0),
           sheets=2, user_id=42, name="Dana", reason="LTC", now=NOW)
    # NB: View.stop() only takes effect inside a running loop (the stop future is
    # loop-bound), so both views are built AND stopped in here — doing it at module
    # scope silently tests nothing.
    stale = botmod.BlockFlowView(1, rows, [held])
    stale.slot_key = "20260823T1330"
    stale.build()
    stale.stop()
    await stale.retire("blocked")

    live = botmod.BlockFlowView(1, rows, [held])
    live.slot_key = "20260823T1330"
    live.build()
    await live.retire("🚫 Blocked: something")
    return stale, live


stale, live = asyncio.run(_retire_cases())
check("view/retire leaves a timed-out picker alone", stale.slot_key, "20260823T1330")
check("view/timed-out picker keeps its old controls",
      [type(c).__name__ for c in stale.children],
      ["BlockSlotSelect", "BlockCountSelect", "BlockConfirmButton", "ReleaseBlockSelect"])
check("view/retire drops the confirm button once a block lands",
      [type(c).__name__ for c in live.children], ["BlockSlotSelect", "ReleaseBlockSelect"])
check("view/retire makes the new block immediately releasable",
      [o.value for o in live.children[1].options], ["20260823T1330-1"])
check_true("view/retire says what happened", "🚫 Blocked: something" in live.prompt())

# A cross-midnight window must name the end's day in BOTH renderings, or it reads
# as a twenty-hour span running backwards.
check("format/span across midnight", pi._span(datetime(2026, 8, 23, 22, 0),
                                              datetime(2026, 8, 24, 2, 0)),
      "10:00 PM–Mon 2:00 AM")


# ── 5. The board is today-and-forward, always ────────────────────────────────
# Matt offered to sub on Aug 16 AND Aug 23; on the 20th the board still showed
# Aug 16, because availability only expired when its LAST game had passed.

def avail(uid, name, games, created="2026-08-10T09:00:00"):
    return {"user_id": uid, "name": name, "league_id": "7", "league": "Sunday League",
            "games": list(games), "note": "", "created_ts": created}


s = store.empty_state()
s["availability"].append(avail(1, "Matt", ["2026-08-16T13:00:00", "2026-08-23T13:00:00"]))
s["availability"].append(avail(2, "Only Past", ["2026-08-16T13:00:00"]))
s["availability"].append(avail(3, "Any Time", []))
dropped = store.expire(s, NOW, 3)
check("subs/played game pruned from a live entry",
      s["availability"][0]["games"], ["2026-08-23T13:00:00"])
check("subs/entry with only past games dropped",
      [a["name"] for a in s["availability"]], ["Matt", "Any Time"])
check("subs/prune is reported so callers save",
      [g["name"] for g in dropped["games"]], ["Matt", "Only Past"])

# An unparseable game is kept rather than silently binned.
s5 = store.empty_state()
s5["availability"].append(avail(4, "Corrupt", ["not-a-date"]))
store.expire(s5, NOW, 3)
check("subs/keeps unparseable availability", len(s5["availability"]), 1)

# The midnight floor: a late game must not survive into the next day on grace.
check("subs/day_floor", store.day_floor(datetime(2026, 8, 20, 23, 59)), datetime(2026, 8, 20))
check("subs/cutoff floors at midnight",
      store.board_cutoff(datetime(2026, 8, 20, 1, 0), 3), datetime(2026, 8, 20))
check("subs/cutoff still uses grace inside the day",
      store.board_cutoff(datetime(2026, 8, 20, 20, 0), 3), datetime(2026, 8, 20, 17, 0))


def req(rid, game_ts, created="2026-08-18T09:00:00"):
    return {"id": rid, "game_ts": game_ts, "created_ts": created, "spots_needed": 1,
            "filled": [], "pending": [], "league_id": "7", "league": "Sunday League",
            "team": "", "requester_id": 9, "requester_name": "Ann", "kind": "sub"}


s6 = store.empty_state()
s6["requests"] = [req("late", "2026-08-19T23:00:00"), req("today", "2026-08-20T09:00:00")]
store.expire(s6, datetime(2026, 8, 20, 1, 0), 3)
check("subs/yesterday's late game gone at 1am", [r["id"] for r in s6["requests"]], ["today"])

s7 = store.empty_state()
s7["requests"] = [req("earlier-today", "2026-08-20T09:00:00")]
store.expire(s7, datetime(2026, 8, 20, 10, 0), 3)
check("subs/a game earlier today is still today",
      [r["id"] for r in s7["requests"]], ["earlier-today"])

# An entry with no date of its own outlives the season it was posted for: an
# "available any time" offer made in a league's last week was still on the board
# a fortnight after that league finished ("Tuesday League 8/4 - 8/25" showing on
# the 31st). Seasons that are over end those entries; dated ones still go by date.
s9 = store.empty_state()
s9["availability"].append(avail(1, "Dead League", [], created="2026-08-19T09:00:00"))
s9["availability"].append(avail(2, "Live League", [], created="2026-08-19T09:00:00"))
s9["availability"][1]["league_id"] = "8"
s9["requests"] = [req("tbd", "", created="2026-08-19T09:00:00"),
                  req("dated", "2026-08-25T19:00:00")]
dropped = store.expire(s9, NOW, 3, dead_leagues={"7"})
check("dead/any-time offer goes when the season does",
      [a["name"] for a in s9["availability"]], ["Live League"])
check("dead/it is reported so the caller saves",
      [a["name"] for a in dropped["availability"]], ["Dead League"])
check("dead/date-TBD request goes too", [r["id"] for r in s9["requests"]], ["dated"])

# A dated game in a finished league is left alone here — it expires on its own
# date like every other game. Nothing else may quietly reach back past today.
s10 = store.empty_state()
s10["requests"] = [req("future", "2026-08-25T19:00:00")]
store.expire(s10, NOW, 3, dead_leagues={"7"})
check("dead/a dated game still expires on its date",
      [r["id"] for r in s10["requests"]], ["future"])

# A league we couldn't read is NOT a finished league. Expiry is handed only the
# ids it positively read as over, so an empty set on a site outage is a no-op,
# not a board wipe.
s11 = store.empty_state()
s11["availability"].append(avail(1, "Unknown League", [], created="2026-08-19T09:00:00"))
store.expire(s11, NOW, 3, dead_leagues=set())
check("dead/empty set prunes nothing", len(s11["availability"]), 1)
store.expire(s11, NOW, 3)
check("dead/omitting the argument prunes nothing", len(s11["availability"]), 1)

# ...and the board itself refuses to render a past date even if expiry hasn't run.
s8 = store.empty_state()
s8["availability"].append(avail(1, "Matt", ["2026-08-16T13:00:00", "2026-08-23T13:00:00"]))
real_now = subs.club_now
subs.club_now = lambda: NOW
try:
    desc = subs.build_embed(s8).description or ""
finally:
    subs.club_now = real_now
check_true("subs/board hides an unexpired past date", "Aug 16" not in desc)
check_true("subs/board still shows the upcoming one", "Aug 23" in desc)


# ── 5b. The streak board groups by streak length ─────────────────────────────
# One line per distinct streak, every tied name on it. Listing a person per line
# repeated 🥇 three times for a three-way tie and then showed a bare "4." for the
# next pair, which read as a numbering glitch rather than as a place.

LB = [{"name": n, "streak": w, "user_id": i} for i, (n, w) in enumerate(
    [("Ann", 3), ("Ben", 3), ("Cara", 3), ("Dave", 2), ("Eve", 2), ("Fay", 1)])]

check("streak/groups by length",
      [(w, names) for w, names in botmod.streak_groups(LB, "streak")],
      [(3, ["Ann", "Ben", "Cara"]), (2, ["Dave", "Eve"]), (1, ["Fay"])])

board = botmod._streak_rows(LB, "streak").split("\n")
check("streak/one line per group", len(board), 3)
check("streak/gold names the whole tied group", board[0], "🥇  **3 wks** — Ann, Ben, Cara")
check("streak/next group is SILVER, not a bare 4.", board[1], "🥈  **2 wks** — Dave, Eve")
check("streak/third group is bronze", board[2], "🥉  **1 wk** — Fay")
check_true("streak/no bare number in a 3-group board", not any("`4.`" in ln for ln in board))
check("streak/empty board", botmod._streak_rows([], "streak"), "—")
check("streak/singular week", botmod._streak_rows(
    [{"name": "Solo", "streak": 1, "user_id": 1}], "streak"), "🥇  **1 wk** — Solo")

# Only five groups, and the tail accounts for everyone below them.
deep = [{"name": f"P{i}", "streak": 9 - i, "user_id": i} for i in range(9)]
deep_board = botmod._streak_rows(deep, "streak").split("\n")
check("streak/caps at five groups plus a tail", len(deep_board), 6)
check("streak/tail counts every dropped person", deep_board[-1],
      "…and 4 more with shorter streaks")
check("streak/fourth and fifth groups are numbered", [deep_board[3][:4], deep_board[4][:4]],
      ["`4.`", "`5.`"])

# One enormous tie must not push the groups below it off the board, or blow the
# 1024-char embed-field limit.
huge = ([{"name": f"Curler{i:02d}", "streak": 2, "user_id": i} for i in range(40)]
        + [{"name": "Last", "streak": 1, "user_id": 99}])
huge_board = botmod._streak_rows(huge, "streak")
check_true("streak/huge tie is summarised", "+30 more" in huge_board)
check_true("streak/group below a huge tie survives", "Last" in huge_board)
check_true("streak/fits an embed field", len(huge_board) <= 1024)

# The sign-up ping must agree with the board: Dave is in the SECOND group, so the
# bot must tell him 2nd — not 4th, which is what counting people gave.
rank_state = ps.empty_state()
for uid, (name, weeks) in enumerate([("Ann", 3), ("Ben", 3), ("Cara", 3), ("Dave", 2)]):
    rank_state["attendance"][str(uid)] = {
        "name": name,
        "weeks": [f"2026-W{34 - k:02d}" for k in range(weeks)][::-1]}
rank, total, tied = ps.streak_rank(rank_state, 3, datetime(2026, 8, 20, 10, 0))
check("streak/rank agrees with the board's grouping", rank, 2)
check("streak/total still counts people", total, 4)
check("streak/not flagged as tied when alone in a group", tied, False)
check("streak/a member of the top tie ranks 1st",
      ps.streak_rank(rank_state, 0, datetime(2026, 8, 20, 10, 0))[0], 1)
check("streak/...and is flagged tied",
      ps.streak_rank(rank_state, 0, datetime(2026, 8, 20, 10, 0))[2], True)


# The sign-up ping ranks among streaks still GOING. A long run that has broken is on
# the all-time board, not the current one, and must not push a live streak down.
def _weeks(last: int, n: int) -> list[str]:
    return [f"2026-W{last - k:02d}" for k in range(n)][::-1]

ping_now = datetime(2026, 9, 25, 12, 0)          # Friday of W39
ping = ps.empty_state()
ping["attendance"] = {
    "1": {"name": "Robin Vale", "weeks": _weeks(37, 7)},   # 7 weeks, missed W38: broken
    "2": {"name": "Alex Reed", "weeks": _weeks(38, 2)},    # W37+W38: 2 and live
    "3": {"name": "Bo Brooks", "weeks": _weeks(38, 1)},
}
check("ping/broken run isn't active", ps.current_streak(ping, 1, ping_now), 0)
check("ping/live 2 ranks first among current", ps.streak_rank(ping, 2, ping_now)[0], 1)
note = botmod.streak_signup_note(ping, 2, ping_now)
check_true("ping/says longest active streak",
           "the longest active streak in the club!" in note)
check_true("ping/a 1-week streak gets no note",
           botmod.streak_signup_note(ping, 3, ping_now) == "")
check_true("ping/no note for a broken streak",
           botmod.streak_signup_note(ping, 1, ping_now) == "")

# All-time top three gets its own sentence. Records: 7 (broken), 5, 4, 3.
rec = ps.empty_state()
rec["attendance"] = {
    "1": {"name": "Robin Vale", "weeks": _weeks(30, 7)},
    "2": {"name": "Ann Lee", "weeks": _weeks(30, 5)},
    "3": {"name": "Cy Cole", "weeks": _weeks(38, 4)},      # live 4: 3rd best ever
    "4": {"name": "Dee Dunn", "weeks": _weeks(38, 3)},     # live 3: 4th ever
}
check("ever/live 4 is 3rd all time", ps.all_time_rank(rec, 3, ping_now), (3, False))
n4 = botmod.streak_signup_note(rec, 3, ping_now)
check_true("ever/top-3 streak is called out", "3rd longest streak in club history" in n4)
check_true("ever/...alongside its current place", "the longest active streak" in n4)
check_true("ever/4th all time isn't called out",
           "history" not in botmod.streak_signup_note(rec, 4, ping_now))
check("ever/no active streak ranks 0", ps.all_time_rank(rec, 1, ping_now), (0, False))
# A live streak past every record is a new club record; level with one ties it.
rec["attendance"]["5"] = {"name": "Eve Ebb", "weeks": _weeks(38, 8)}
check_true("ever/new record", "new all-time club record" in
           botmod.streak_signup_note(rec, 5, ping_now))
rec["attendance"]["5"]["weeks"] = _weeks(38, 7)
check_true("ever/ties the record", "ties the all-time club record" in
           botmod.streak_signup_note(rec, 5, ping_now))
# A past run of your own that beat this one still counts above it.
rec["attendance"]["6"] = {"name": "Fay Fox", "weeks": _weeks(20, 6) + _weeks(38, 2)}
check("ever/own older record sits above", ps.all_time_rank(rec, 6, ping_now)[0], 6)

# ── 6. Import hygiene (the class of bug that has actually shipped) ───────────
# `from datetime import time` in subs.py once shadowed the stdlib time module and
# broke every debounced button in production. Re-check it for every module here.
import time as stdlib_time

for mod in (botmod, subs, bs, pi, store):
    if hasattr(mod, "time"):
        check(f"hygiene/{mod.__name__}.time is the stdlib module",
              getattr(mod, "time"), stdlib_time)
check("hygiene/block_store has no stray 'time'", hasattr(bs, "time"), False)
check_true("hygiene/debounce still callable", botmod._is_repeat_click({}, ("u", "k")) is False)


# ── Report ───────────────────────────────────────────────────────────────────

if FAILS:
    print(f"\n❌  {len(FAILS)} failure(s):\n")
    for f in FAILS:
        print(" - " + f)
    raise SystemExit(1)
print("✅  all block + board-floor checks passed")
