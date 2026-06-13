# Curling Club Practice-Ice Bot

A Discord bot that reports how many of a curling club's 4 sheets are free during
upcoming practice ice. It reads a WordPress site's public event calendar (The
Events Calendar), Gravity Forms registrations, and the league pages, then lists
every upcoming session that leaves a sheet open.

It is configured for a single site via environment variables — the target
domain and club name are not hardcoded.

## What it shows

`/sheets [upcoming]` lists practice-ice opportunities in time order, each as
**time · type · sheets free · date**. Practice ice comes from four source types:

- **Practice blocks** — designated open-ice sessions on the calendar.
- **Learn-to-Curls** — sheet usage from registration headcount (`ceil(people / 8)`).
- **Private events** — sheet usage derived from the booking fee.
- **Leagues** — team count and draw schedule parsed from the public league pages.

Free sheets during any session = 4 − sheets used by every overlapping session,
so concurrent bookings stack correctly. Sessions with no available data are
flagged rather than guessed.

`upcoming` (1–5, default 1) is how many designated practice blocks to look
ahead; that span defines the window in which LTCs, private events, and league
draws are also surfaced.

## Setup

1. Create a Discord application and bot (Developer Portal → New Application →
   Bot → copy the token). Under OAuth2 → URL Generator, select `bot` +
   `applications.commands` and the `Send Messages` permission, then invite it.
2. Copy `.env.example` to `.env` and fill in the values (Discord token, site
   URL, club name, Gravity Forms REST key/secret).
3. Run it.

### Docker (recommended)

```bash
docker compose up --build -d
```

### Plain Python

```bash
pip install -r requirements.txt
python bot.py
```

The bot syncs slash commands on startup; allow up to a minute for `/sheets` to
appear in Discord.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `SITE_BASE_URL` | Target WordPress site, no trailing slash |
| `CLUB_NAME` | Name shown in the Discord embed |
| `GF_CONSUMER_KEY` / `GF_CONSUMER_SECRET` | Gravity Forms REST API v2 credentials |
| `LEAGUE_CACHE_TTL` | League-page cache lifetime in seconds (default 21600 = 6h) |

A few site-specific constants live at the top of `bot.py` — `TOTAL_SHEETS`,
`PEOPLE_PER_SHEET`, `PRICE_PER_PERSON`, `TIMEZONE_OFFSET`, the form IDs, and the
practice category slug. Adjust to match your site.

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
