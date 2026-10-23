"""Unit tests for /curlbot refresh and the league refetch it relies on.

Run:  python3 test_refresh.py

Covers: refetch_leagues (raises and leaves the cache alone on failure, unlike
get_cached_leagues), the nightly script's failure branch, the /sheets cache
refresh (a failed window keeps its old copy), the wiring of refresh_all_data
(every source refetched, boards redrawn, each failure reported), and the
command handler itself (permission check, the one-at-a-time guard, the reply).

Network and Discord are stubbed. This is NOT a substitute for a live smoke test.
"""
import asyncio
import json
import os
import tempfile
import time as stdlib_time

import discord

# bot.py calls bot.run() at import time; neuter it so we can import the module.
discord.Client.run = lambda self, *a, **k: None
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import bot as botmod
import league_client as lc
import pond_ice
import refresh_leagues as rl

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")


def check_true(name, got):
    if not got:
        FAILS.append(f"{name}\n   got:  {got!r}\n   want: truthy")


def run(coro):
    return asyncio.run(coro)


# ── module hygiene ────────────────────────────────────────────────────────────
check_true("hygiene/bot.time is the stdlib module", botmod.time is stdlib_time)

# ── refetch_leagues ──────────────────────────────────────────────────────────
GOOD = [{"id": 1, "title": "Monday League", "ended": False},
        {"id": 2, "title": "Old League", "ended": True}]


def fake_client(result=None, exc=None):
    class FakeLC:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def all_active_league_info(self):
            if exc:
                raise exc
            return result
    return FakeLC


real_client = lc.LeagueClient
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "league_cache.json")
try:
    lc.LeagueClient = fake_client(GOOD)
    got = run(lc.refetch_leagues(cache_path=path))
    check("refetch/returns the leagues", got, GOOD)
    with open(path) as f:
        check("refetch/writes the cache", json.load(f).get("leagues"), GOOD)

    # Every page failed: raise, and keep the good file.
    lc.LeagueClient = fake_client([{"id": 1, "fetch_failed": True}])
    try:
        run(lc.refetch_leagues(cache_path=path))
        FAILS.append("refetch/all pages failed\n   did not raise")
    except lc.LeagueFetchError:
        pass
    with open(path) as f:
        check("refetch/a failed refetch leaves the cache", json.load(f).get("leagues"), GOOD)

    # Network error: raises too.
    lc.LeagueClient = fake_client(exc=OSError("down"))
    try:
        run(lc.refetch_leagues(cache_path=path))
        FAILS.append("refetch/network error\n   did not raise")
    except OSError:
        pass

    # get_cached_leagues keeps its forgiving behaviour for member-facing reads.
    check("cached/force still falls back to the stale file",
          run(lc.get_cached_leagues(force=True, cache_path=path)), GOOD)
finally:
    lc.LeagueClient = real_client

# ── the nightly script's failure branch can now fire ──────────────────────────
real_rl = rl.refetch_leagues


async def _boom(**k):
    raise OSError("down")

try:
    rl.refetch_leagues = _boom
    found = run(rl.refresh_leagues())
    check("nightly/a failed refetch is a finding",
          [f.get("key") for f in found], ["league_refresh"])
finally:
    rl.refetch_leagues = real_rl

# ── refresh_sessions_cache ───────────────────────────────────────────────────
real_fetch = botmod.fetch_sessions
fetched = []


async def fetch_ok(weeks):
    fetched.append(weeks)
    return [{"weeks": weeks, "fresh": True}]


async def fetch_fail_on_2(weeks):
    fetched.append(weeks)
    if weeks == 2:
        raise botmod.SheetsError("down")
    return [{"weeks": weeks, "fresh": True}]

try:
    botmod._sessions_cache.clear()
    botmod.fetch_sessions = fetch_ok
    ok, failed = run(botmod.refresh_sessions_cache())
    check("sessions/empty cache still warms the 1-week window", (ok, failed), ([1], []))
    check("sessions/…and stores it", (botmod._sessions_cache.get(1) or (0, None))[1], [{"weeks": 1, "fresh": True}])

    # Existing windows are refetched whatever their age; a failure keeps the old copy.
    stale = [{"stale": True}]
    now = stdlib_time.monotonic()
    botmod._sessions_cache.clear()
    botmod._sessions_cache.update({2: (now, stale), 4: (now, stale)})
    fetched.clear()
    botmod.fetch_sessions = fetch_fail_on_2
    ok, failed = run(botmod.refresh_sessions_cache())
    check("sessions/every window is refetched, fresh or not", fetched, [1, 2, 4])
    check("sessions/reports what worked and what didn't", (ok, failed), ([1, 4], [2]))
    check("sessions/a failed window keeps its old copy",
          (botmod._sessions_cache.get(2) or (0, None))[1], stale)
    check("sessions/a good window is replaced", (botmod._sessions_cache.get(4) or (0, None))[1],
          [{"weeks": 4, "fresh": True}])
finally:
    botmod.fetch_sessions = real_fetch
    botmod._sessions_cache.clear()

# ── refresh_all_data wiring ──────────────────────────────────────────────────
calls = []


class FakeCog:
    async def startup(self):
        calls.append("subs.startup")


def install_stubs(*, leagues_fail=False, feeds=None, sessions=([1], []), redraw_fail=False):
    calls.clear()

    async def refetch():
        calls.append("leagues")
        if leagues_fail:
            raise OSError("down")
        return GOOD

    async def fetch_feeds(urls, ttl=0, force=False):
        calls.append(("feeds", force))
        return feeds if feeds is not None else [{"ok": True}, {"ok": True}]

    async def refresh_sessions():
        calls.append("sessions")
        return sessions

    async def resync(weeks):
        calls.append(("resync", weeks))
        if redraw_fail:
            raise RuntimeError("discord said no")

    async def render(ch=None):
        calls.append("practice.render")

    async def board_channel():
        return None

    botmod.refetch_leagues = refetch
    pond_ice.fetch_feeds = fetch_feeds
    botmod.refresh_sessions_cache = refresh_sessions
    botmod._resync_practice_sheets = resync
    botmod.render_practice_board = render
    botmod._board_channel = board_channel
    botmod.bot.get_cog = lambda name: FakeCog() if name == "Subs" else None
    botmod.POND_ICS_URLS = ["https://cal.example/a.ics", "https://cal.example/b.ics"]


saved = {n: getattr(botmod, n) for n in ("refetch_leagues", "refresh_sessions_cache",
                                         "_resync_practice_sheets", "render_practice_board",
                                         "_board_channel", "POND_ICS_URLS")}
saved_feeds = pond_ice.fetch_feeds
saved_get_cog = botmod.bot.get_cog
try:
    install_stubs()
    lines = run(botmod.refresh_all_data())
    check("wire/every source refetched, then the boards, in order", calls,
          ["leagues", ("feeds", True), "sessions", "subs.startup", ("resync", 1),
           "practice.render"])
    check("wire/all green", [ln.startswith("✅") for ln in lines], [True] * 4)
    check_true("wire/league count reported", any("2 refetched (1 active)" in ln for ln in lines))

    # Practice counts resync over the widest refreshed window.
    install_stubs(sessions=([1, 4], [2]))
    lines = run(botmod.refresh_all_data())
    check_true("wire/resync uses the widest window", ("resync", 4) in calls)
    check_true("wire/a failed /sheets window is reported",
               any(ln.startswith("⚠️") and "/sheets" in ln for ln in lines))

    # Nothing refreshed: don't resync practice counts from nothing.
    install_stubs(sessions=([], [1]))
    lines = run(botmod.refresh_all_data())
    check("wire/no resync when no window refreshed",
          [c for c in calls if isinstance(c, tuple) and c[0] == "resync"], [])
    check("wire/…and the boards still redraw cleanly",
          [ln for ln in lines if "Boards" in ln], ["✅  Boards redrawn"])

    # A league failure is reported and the rest still runs.
    install_stubs(leagues_fail=True)
    lines = run(botmod.refresh_all_data())
    check_true("wire/league failure reported", lines and lines[0].startswith("⚠️"))
    check_true("wire/…and the rest still ran", "sessions" in calls and "subs.startup" in calls)

    # One dead rink feed.
    install_stubs(feeds=[{"ok": True}, {"ok": False}])
    lines = run(botmod.refresh_all_data())
    check_true("wire/partial feed failure reported",
               any("1 of 2 loaded" in ln for ln in lines))

    # No feeds configured: no feed fetch, no feed line.
    install_stubs()
    botmod.POND_ICS_URLS = []
    lines = run(botmod.refresh_all_data())
    check("wire/no feeds configured, no feed fetch",
          [c for c in calls if isinstance(c, tuple) and c[0] == "feeds"], [])
    check("wire/…and no feed line", [ln for ln in lines if "Rink" in ln], [])

    # A redraw failure doesn't lose the report.
    install_stubs(redraw_fail=True)
    lines = run(botmod.refresh_all_data())
    check_true("wire/redraw failure reported, not raised",
               lines and lines[-1].startswith("⚠️") and "Boards" in lines[-1])
finally:
    for n, v in saved.items():
        setattr(botmod, n, v)
    pond_ice.fetch_feeds = saved_feeds
    botmod.bot.get_cog = saved_get_cog

# ── the command handler ──────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **k):
        self.log.append(("send", content, k.get("ephemeral")))

    async def defer(self, **k):
        self.log.append(("defer", k.get("ephemeral")))


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **k):
        self.log.append(("followup", content, k.get("ephemeral")))


class FakeInteraction:
    def __init__(self, manage_guild):
        self.log = []
        self.permissions = discord.Permissions(manage_guild=manage_guild)
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)


check("cmd/not a top-level command", botmod.bot.tree.get_command("refresh"), None)
group = botmod.bot.tree.get_command("curlbot")
check_true("cmd/the /curlbot group is registered", group is not None)
cmd = group.get_command("refresh") if group is not None else None
check_true("cmd/refresh is a /curlbot subcommand", cmd is not None)
if cmd is not None:
    check_true("cmd/hidden from non-managers by default",
               group.default_permissions is not None and group.default_permissions.manage_guild)
    check("cmd/server only", group.guild_only, True)

    saved_rad = botmod.refresh_all_data
    ran = []

    async def fake_refresh():
        ran.append(1)
        return ["✅  Leagues: 2 refetched (1 active)"]

    try:
        botmod.refresh_all_data = fake_refresh

        it = FakeInteraction(manage_guild=False)
        run(cmd.callback(it))
        check("cmd/non-manager is refused privately",
              [(e[0], e[-1]) for e in it.log], [("send", True)])
        check("cmd/…and nothing is refreshed", ran, [])

        it = FakeInteraction(manage_guild=True)
        run(cmd.callback(it))
        check("cmd/manager: deferred privately, then one private reply",
              [(e[0], e[-1]) for e in it.log], [("defer", True), ("followup", True)])
        check("cmd/…the refresh ran once", ran, [1])
        check_true("cmd/…the reply carries the status lines",
                   len(it.log) == 2 and "Leagues: 2 refetched" in (it.log[1][1] or ""))

        # A second press while one is running is turned away.
        async def overlapping():
            await botmod._refresh_lock.acquire()
            try:
                it2 = FakeInteraction(manage_guild=True)
                try:
                    # Without the guard the press queues on the lock forever.
                    await asyncio.wait_for(cmd.callback(it2), timeout=2)
                except asyncio.TimeoutError:
                    it2.log.append(("hung waiting for the running refresh", None))
                return it2.log
            finally:
                botmod._refresh_lock.release()
        ran.clear()
        log2 = run(overlapping())
        check("cmd/a concurrent press is refused", [(e[0], e[-1]) for e in log2], [("send", True)])
        check("cmd/…without a second refresh", ran, [])
    finally:
        botmod.refresh_all_data = saved_rad


if FAILS:
    print(f"❌  {len(FAILS)} failure(s):")
    for f in FAILS:
        print(" - " + f)
    raise SystemExit(1)
print("✅  all refresh checks passed")
