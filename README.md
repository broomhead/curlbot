# Curling Club Discord Bot

A Discord bot that helps a curling club coordinate ice time. It has four
features:

- **Practice ice (`/sheets`)** — reports how many sheets are free during upcoming
  practice ice, read live from the club's WordPress site (The Events Calendar,
  Gravity Forms registrations, and the public league pages).
- **Practice sign-ups** — an open pool to say "I want to practice this slot," with
  a shared, auto-updating board so members can rally when someone's going.
- **Subs board (`/subs`)** — "I need a sub" / "I can sub" coordination tied to real
  league teams and game dates (the team is optional — you can post before teams
  are set), with spot-filling, DM confirmations, and notifications.
- **Instructor board (`/instructors`)** — posts which upcoming Learn-to-Curls and
  events are short of instructors, read from a Google Sheet. Optional:
  unconfigured, it never loads and nothing else changes.

Everything is configured for a single site via environment variables — the
target domain, club name, and sheet count are not hardcoded. The number of sheets
is set with `NUM_SHEETS` (default 4), since facilities differ.

## Practice ice — what it shows

`/sheets [upcoming]` lists practice-ice opportunities in time order, each as
**time · type · sheets free · date**. The reply is **private** (ephemeral — only
you see it), so checking it doesn't post in the channel. Practice ice comes from
four source types:

- **Practice blocks** — designated open-ice sessions on the calendar.
- **Learn-to-Curls** — sheet usage from registration headcount (`ceil(people / 8)`).
- **Private events** — sheet usage derived from the booking fee.
- **Leagues** — team count and draw schedule parsed from the public league pages.

...and one thing that only ever *removes* ice: **blocks** (below).

Free sheets during any session = `NUM_SHEETS` − sheets used by every overlapping
session, so concurrent bookings stack correctly. Sessions with no available data
are flagged rather than guessed. `NUM_SHEETS` (default 4) is configurable since
facilities differ.

`upcoming` (1–5, default 1) is how many designated practice blocks to look
ahead; that span defines the window in which LTCs, private events, and league
draws are also surfaced.

**Practice sign-up pool.** Each opportunity has a sign-up button. Tap it to say
"I want to practice this slot"; tap again to drop off. There's no cap — it's an
open pool, so the report just shows free sheets and how many people have signed
up, and members sort out sheets themselves. Sign-ups are shared (everyone sees
the counts) even though each `/sheets` view is private; they age out a few hours
after the slot's start time.

Because people often show up when someone else is going, a shared **practice
board** is kept current in the channel: it's posted/pinned the first time someone
signs up (in whatever channel they used `/sheets`) and edited silently on every
join or leave. When someone *newly joins* a slot, the bot also posts a short
ping so others get notified and can join in.

### Blocking sheets (ice booked off the calendar)

Sometimes ice gets reserved outside every system the bot can see — someone
schedules a Learn-to-Curl by hand, or a group books sheets at the rink directly.
Money changes hands, but nothing lands on the club calendar or in Gravity Forms,
so `/sheets` keeps advertising ice that's already taken.

The **🚫 Block sheets** button on every `/sheets` report is the manual patch.
Pick the slot the ice is coming out of (or **Other date/time…** for ice that
isn't listed at all), choose how many sheets, and say what it's for. From then
on every report subtracts those sheets and shows a line naming who blocked them:

```
🟡  Sun Aug 23 · 1:30 PM · Practice · 1 sheet free
    Sunday Practice
    🚫 2 sheets blocked by Darin — Learn-to-Curl (1:30–3:00 PM)
```

Notes on how it behaves:

- **Anyone in the server can block or release.** There's no permission gate — at
  a club, whoever's at the rink is usually the one who knows. Every block and
  release is announced in the channel with the member's name, and the block
  carries that name on the report, so the accountability is social rather than
  technical.
- **Blocks apply immediately.** They're stored locally and folded into the
  arithmetic on each render, so they don't wait out the six-hour `/sheets` cache.
- **They expire on their own** once the ice is past (`BLOCK_GRACE_HOURS`), and can
  be released early from the same 🚫 menu — pick the block from "…or release a
  block".
- **A block that takes a slot to zero pings anyone signed up to practice then**,
  since their plans just changed.
- A block never gets a row of its own; it only reduces the free count on the rows
  it overlaps. Its window shows on the note when it covers only part of a slot.

State lives in `BLOCK_STORE_PATH` (default `sheet_blocks.json`).

## Subs board

The subs board coordinates "I need a sub" / "I can sub" through buttons. Two
commands:

- **`/subs`** opens your **private** copy of the board (ephemeral — only you see
  it, nothing is posted to the channel). Use it to check what's open and act.
- **`/subsboard`** posts the **shared** board to the channel and pins it
  (restricted to members who can manage messages). Run it once per channel.

Both show the same board; every action (taking a spot, posting a request,
inviting a sub) updates the shared pinned board, and acting from your private
board refreshes it in place. The buttons:

- **➕ Need a sub** walks you through league → your team → which game → how many
  spots. Leagues, teams, and games are pulled live from the club's league pages
  (the same source as `/sheets`), so you pick from real data instead of typing
  it. **The team is optional** — chairs often don't set teams until a day or two
  before the first draw, so a request can go up as "*<name>* needs a sub" and
  gain a team later. **The date never is:** when you need a sub is the point of
  the request.
  Leagues are shown by name and run dates ("Thursday League 8/6 – 8/27") rather
  than the admin's "Summer 2026 League 3", and are listed by night of the week
  (Sunday → Saturday) then by start date, so the leagues on one night sit
  together (finished seasons drop off the list). A league that hasn't started
  yet shows "from 9/6", read out of its title's "Begins …" tail.
  When a league's schedule isn't posted, the game picker projects its upcoming
  **league nights** — weekly, on its own night, from that same start date — so
  there's always a real date to attach a request to. A posted schedule always
  wins; projected nights are marked "not on the schedule yet". If the club
  hasn't announced the start time either, the option reads "Sun Sep 6 · time
  TBC" rather than guessing one: the date is what matters, and a neighbouring
  league's time would be wrong (Sunday morning is 9am, Sunday night isn't).

  **Several dates at once.** Tick more than one date in the picker and each one is
  posted as its own request; the button reads **Post 4 dates**. A date that already
  has an open request is left alone rather than double-booked.
  **This is a bulk input convenience, not a standing arrangement.** Underneath it's
  just N ordinary requests sharing a tag: each date keeps its own status, roster,
  lock and expiry, so the moment someone can't make week 3, that date simply reopens
  as an ordinary sub opportunity and the others carry on untouched. There is
  deliberately no "cancel them all" — dates are dropped one at a time, on purpose.
  So the board lists **dates**, one one-tap button each. Bulk lives where the bulk
  decision is actually made: the posting form, the alert for a fresh post, and
  **Fill for someone**.

  **Need more subs than you asked for?** Run **Need a sub** again for the same
  league, team and night. Instead of refusing it as a duplicate, the flow switches
  to editing that request: pick the new **total** and press **Update spots**. The
  sub you already found keeps their place, the board and the alert update, and the
  room is re-pinged about the spots that just opened. Anyone can raise the count
  (the team lost the players, not the person who happened to post); only whoever
  posted it can lower it, and never below the people already on it.
- One **🙋 button per open night** appears below the verbs, matching the board text
  line for line. One tap takes the spot; the requester is notified of every change.
- **📋 Show all** appears whenever there are games the board isn't listing. The board
  shows only the next **`SUBS_BOARD_DAYS`** (default 14) days — a Discord embed can't
  scroll, and a 16-night league season would bury the games people can still act on.
  Everything past the horizon condenses to one line that says how many games there
  are, how far out they run, and **how many still need a sub**, so a far-off unfilled
  game can't go unnoticed. Show all sends the whole board privately, buttons and all —
  nothing is ever hidden, only moved out of the channel's way.
- The **🆘 Sub needed** alert that goes up when a request is posted (and again ~24h
  before an unfilled game) carries the buttons for that ask. One date: **I'll take
  it**. Several: **Take 4 dates** or **I'll take some…**, which opens a picker with
  every open date ticked. Either way it also carries **➕ Fill for someone**,
  pre-aimed at that posting — since posting no longer asks who's covering the dates,
  this is where "Ben's got Tuesdays" gets recorded, one tap from the ask.
- **🙋 I can sub** lets you pick a league and the upcoming games you can cover
  (or none, for "any game"), listing you on the board's available-subs section.
- **➕ Fill for someone** records that a member is covering a spot (they told you
  offline). Pick the game, then — for a multi-date posting — untick any dates they
  can't make, then the person, then press the green **Mark <name> in** button.
  Choosing a name does *nothing* on its own: every form in this cog ends on an
  explicit button, because a mis-tap here would put the wrong person on someone
  else's game with no undo short of ➖ Remove.
- **➖ Remove** is the one place things come off the board: take a sub off a game,
  cancel a request you opened, or clear your own availability. All three ask before
  they act — nothing in this cog is destroyed by touching a dropdown.
- **🛠 Manage** (requester only) lets you add/remove a member directly, **invite
  an available sub** (they get a DM to confirm), or close a request early.

When inviting an available sub, they receive a DM with **Confirm / Can't**
buttons; the spot is held as **pending** (shown on the board) until they accept,
then it flips to filled. Either way the requester is notified.

Game pickers show **all upcoming games** for the chosen league. A request
**auto-expires** a few hours after its game starts (`SUBS_GRACE_HOURS`, default
3); a night whose start time is still TBC is treated as ending with that day, so
it survives the whole day it's needed. Availability tied to specific games
expires once those games pass — all checked every 15 minutes and on startup.
(Requests can no longer be posted without a date. Any made while that was briefly
allowed still show under a **Date TBD** heading and age out after
`SUBS_UNDATED_DAYS`.)

Notifications try a DM first and fall back to an @-mention in the board channel
if the member has DMs closed.

State is kept in a small JSON file (`SUBS_STORE_PATH`, default `subs_store.json`)
and the buttons survive bot restarts. The board is generic by design (each
request has a `kind` field), so the same machinery can later back pickup games,
team-building, etc.

> Requires `discord.py >= 2.4` (persistent per-request buttons). For pinning,
> invite the bot with the **Manage Messages** permission; without it the board
> still works, just unpinned.

## Setup

1. Create a Discord application and bot (Developer Portal → New Application →
   Bot → copy the token). Under OAuth2 → URL Generator, select `bot` +
   `applications.commands` and the `Send Messages`, `Embed Links`, and
   `Manage Messages` permissions (the last is needed to pin the boards), then
   invite it.
2. Copy `.env.example` to `.env` and fill in the values (Discord token, site
   URL, club name, sheet count, Gravity Forms REST key/secret).
3. Run it. The bot syncs slash commands on startup (`/sheets`, `/subs`,
   `/subsboard`); set `DEV_GUILD_ID` to your server for instant command updates
   while testing (otherwise a global sync can take up to ~1h to appear).

### Docker (recommended)

```bash
docker compose up --build -d
```

### Plain Python

```bash
pip install -r requirements.txt
python bot.py
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `DEV_GUILD_ID` | Optional. Server ID for instant slash-command sync (dev). Unset = global sync (~1h). |
| `SITE_BASE_URL` | Target WordPress site, no trailing slash |
| `CLUB_NAME` | Name shown in the Discord embed |
| `NUM_SHEETS` | Number of sheets at the facility (default 4) |
| `PRACTICE_STORE_PATH` | Practice sign-up pool state file (default `practice_signups.json`) |
| `BLOCK_STORE_PATH` | Ad-hoc sheet-block state file (default `sheet_blocks.json`) |
| `BLOCK_GRACE_HOURS` | Hours after a block ends before it's swept (default 1) |
| `GF_CONSUMER_KEY` / `GF_CONSUMER_SECRET` | Gravity Forms REST API v2 credentials |
| `LEAGUE_CACHE_TTL` | League-page cache lifetime in seconds (default 21600 = 6h) |
| `TIMEZONE_OFFSET` | Club UTC offset for subs date/time parsing (default -5) |
| `SUBS_STORE_PATH` | Subs board state file (default `subs_store.json`) |
| `SUBS_GRACE_HOURS` | Hours after game start before a request expires (default 3) |
| `SUBS_UNDATED_DAYS` | Days a legacy request with no game date lasts before ageing out (default 14) |
| `PEOPLE_PER_SHEET` | Max people on one sheet of ice (default 8) |
| `INSTRUCTOR_CHANNEL_ID` | Channel the instructor board posts to. Unset = feature off |
| `SHEET_ID` | Google Sheet id for the instructor sheet |
| `CHECK_TIMES` | Club-local instructor-board checks (default `09:00,16:00`) |
| `URGENT_DAYS` | How close an event has to be to count as urgent (default `14`) |
| `INSTRUCTORS_PER_SHEET` / `MIN_INSTRUCTORS_PER_SHEET` | Staffing target and floor (2 / 1) |

Sheet count is set via `NUM_SHEETS`. A few other site-specific constants live at
the top of `bot.py` — `PEOPLE_PER_SHEET`, `PRICE_PER_PERSON`, `TIMEZONE_OFFSET`,
the form IDs, and the practice category slug. Adjust to match your site.

## How data is sourced

- **Calendar & event details:** The Events Calendar REST API (`tribe/events/v1`).
- **LTC / private registrations:** Gravity Forms REST API v2. Credentials are
  passed as query parameters so they survive proxies that strip `Authorization`
  headers. LTC entries are matched by event date, summed across submissions.
- **Leagues:** league pages are fetched and parsed (standings → team count;
  schedule → upcoming draw day/time/sheets), then cached to `league_cache.json`.
  `refresh_leagues.py` force-refreshes the cache and is suitable for a cron job.

## Caching

League data is cached locally with a TTL (default 6h); calendar and Gravity
Forms data are fetched live per command via a single ranged request with
concurrent lookups.

## Security & privacy

- **Secrets live only in `.env`** (the Discord token and Gravity Forms key/secret),
  which is gitignored. Nothing sensitive is committed; `.env.example` holds only
  placeholders. No club identity is baked into source — everything is env-driven.
- **Runtime data stays local and out of git:** `subs_store.json`,
  `practice_signups.json`, and `league_cache.json` (plus their `.tmp` siblings)
  are gitignored. These hold Discord display names and numeric IDs only.
- **Errors are never echoed raw to Discord.** Gravity Forms credentials ride in
  the request query string (to survive proxies that strip auth headers), so a
  failed request's default error would embed the URL — and the credentials.
  `gf_client` raises a sanitized error (status + path only) and commands log full
  detail server-side while showing a generic message. When adding new code, never
  interpolate a raw exception or request URL into a user-facing message.
- **Member-facing commands are private:** `/sheets` and `/subs` reply ephemerally.
  The shared practice board and the subs board intentionally show display names so
  members can coordinate. DM confirm/decline actions are validated against the
  invited user's ID, so only the invitee can respond.

## Developer scripts

One-off exploration/diagnostic scripts (run via
`docker compose run --rm curlbot python <script>.py`):

- `discover_views.py` — list Gravity Forms and sample entries.
- `discover_leagues.py` — probe league data sources.
- `discover_league_pages.py` — validate league-page parsing.
- `discover_ltc.py` — diagnose an LTC's headcount and join key.

## Hosting

Any always-on machine works — a small VPS, a Raspberry Pi, or a free-tier cloud
service. Set the environment variables and run the bot.

## Instructor board

Optional. A coordinator keeps a Google Sheet of upcoming LTCs, private events and CPATH events with the
instructors signed up for each, then chases people by email when an event is
short. This posts the same ask into a Discord channel instead.

- **The sheet is the only source of truth.** No database, no state file, nothing
  to back up. Every check re-reads it.
- **The board in the channel is the state.** Each check renders the board and
  compares it against the last one the bot posted. Identical means nothing
  changed, so it stays quiet. Different means the sheet moved, so the old board
  is deleted and a fresh one posted at the bottom where people will see it.
- **No Google credentials.** The sheet is shared for link access, so the CSV
  export endpoint works unauthenticated. If sharing is ever revoked, Google
  returns a sign-in page with HTTP 200; the bot says so rather than parsing HTML
  as data.

### What it posts

Two groups, split by how close the event is, each a table in date order inside a
code block so the columns line up (Discord has no markdown tables):

**2 events in the next 14 days need instructors.**

🔴  **Needs instructors now (next 14 days)**
```
Date      Event    Time           Have/Need
--------  -------  -------------  ---------
Tue 8/25  Private  12:30-2:45 pm  6/8
   Ann Adams, Bo Brooks, Cara Cole,
   Dev Diaz, Eve Ellis, Finn Ford
Sat 8/29  Private  1:30-3:45 pm   1/6
   Ann Adams
```

🟡  **Coming up later**
```
Date       Event  Time       Have/Need
---------  -----  ---------  ---------
Sat 9/19   LTC    2-4:15 pm  1/8
   Bo Brooks
Sat 10/17  LTC    2-4:15 pm  0/6
   nobody yet
```

**Urgency is proximity, not severity.** An LTC eleven days out with half its
instructors is this week's problem; the same gap in October is not, and a board
that shouts about both teaches people to ignore it. The line is `URGENT_DAYS`
(default 14). The traffic lights are the same red/amber/green the subs and
practice boards use, and they sit on the headings rather than inside the code
block, where an emoji is not one monospace cell wide and would shove that row's
columns out of line. The embed's bar follows the same rule: red only when
something inside the window is short, amber when the only gaps are further out,
green when everything is covered. The headline counts the urgent asks only, so a
quiet fortnight reads as "Nothing urgent" even with October wide open.

A long name list is wrapped by the bot at whole names, every line indented, since
Discord would otherwise put the continuation flush left where it reads as
another row. A row plus its names runs 120 to 200 characters depending on how
full the roster is, so a busy stretch can reach Discord's 4096 character
description limit; events are dropped off the far end until it fits, with a note
saying how many. The near events are the ones anyone can still act on.

### How many instructors an event needs

From **sheets of ice**, using the same `ice.sheets_for_people()` that `/sheets`
uses, so the two can never disagree:

```
sheets = ceil(attendees / PEOPLE_PER_SHEET), capped at NUM_SHEETS
target = INSTRUCTORS_PER_SHEET per sheet      (default 2)
floor  = MIN_INSTRUCTORS_PER_SHEET per sheet  (default 1)
```

Two per sheet is the goal; a club can stretch below it (three across two sheets,
three across three), which is why there's a floor as well as a target.

An event with no attendee count gets no target at all: its row shows just how
many are signed up, rather than a shortfall invented from nothing. A name
written as `Jane Doe (if needed)` is tentative, listed with that qualifier and not counted
in the total, since counting a maybe would hide a real gap. Adding an
**`Instructors Needed`** column to the sheet overrides the computed target per
row; without one, everything works as-is.

### Setting it up

1. Turn on Developer Mode in Discord (Settings, Advanced), right click the
   channel you want the board in, Copy Channel ID, and put it in
   `INSTRUCTOR_CHANNEL_ID`.
2. Put the sheet's id in `SHEET_ID` and make sure the sheet is shared as
   "anyone with the link can view".

That's it. No extra tokens and no extra permissions: the bot posts and deletes
its own messages, which needs nothing beyond what it already has.

### Running it

Checks happen inside the bot process at `CHECK_TIMES` (default 09:00 and 16:00
club local). `/instructors` posts a fresh board immediately, replying privately
with what it did.

To see the board without touching Discord at all (handy while developing, and
it needs only `SHEET_ID`):

```bash
python refresh_instructors.py
```

> The board's text must stay a pure function of the sheet. A timestamp in it
> would make every check look like a change and post to the channel twice a day
> forever. There's a test for that, and for the no-em-dash house style.
