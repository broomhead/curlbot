"""
League data client for the club's WordPress site (DataBowl plugin).

The admin "Participants" / DataBowl Schedule & Scores screens read WordPress
post meta on the `leagues` post type. That meta is NOT exposed via REST
(meta keys aren't registered with show_in_rest, and the GF consumer key/secret
do not elevate wp/v2 access). HOWEVER, DataBowl renders the same meta into the
*public* league page HTML — no login required.

So this client:
  1. Lists active leagues via wp/v2/leagues (gives id, title, link, day slug).
  2. GETs each league's public page and parses two sections:
       • "Standings"        -> an HTML <table>; data-row count = number of teams.
       • "Schedule & Scores"-> one <h6> per draw, e.g.
             "June 16, 2026 7:45 pm  Griffith - Poklitar -  Sheet A  ...  Sheet D is open."
         Date + time come straight from the header; a draw is upcoming when its
         date >= today (club timezone). Played draws are in the past.
         Sheets used per draw = count("Sheet X") - count("Sheet X is open").

Returns, per league: teams, day, time, ended, and a list of draws (with an
`upcoming` flag and `sheets_used`).

Read-only, unauthenticated. Uses a browser User-Agent so Cloudflare lets it
through (same workaround as gf_client).
"""

from __future__ import annotations

import re
import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = os.environ.get("SITE_BASE_URL", "https://example.com")
LEAGUES_ENDPOINT = f"{BASE_URL}/wp-json/wp/v2/leagues"
TIMEOUT = aiohttp.ClientTimeout(total=20)

# Club timezone (America/Chicago). CDT = UTC-5 in summer; good enough for a
# date-only "is this draw in the future" comparison. Matches bot.py.
TIMEZONE_OFFSET = -5

# wp `league_category` slug -> day of week (fallback when no draws are listed).
DAY_BY_CATEGORY = {
    "sunam": "Sunday",
    "sunpm": "Sunday",
    "sunday-development": "Sunday",
    "tues": "Tuesday",
    "thurs": "Thursday",
    "friday-tgif": "Friday",
}

# Markers that identify what a non-JSON body actually was, so a failure names
# itself in the log instead of arriving as "Expecting value: line 1 column 1".
_CF_MARKERS = ("just a moment", "cf-browser-verification", "attention required",
               "checking your browser", "cloudflare")


def _describe_body(body: str, content_type: str) -> str:
    """A short, log-safe description of a response body we couldn't use."""
    if not body.strip():
        return f"an empty body (content-type {content_type or 'none'})"
    low = body[:2000].lower()
    if any(m in low for m in _CF_MARKERS):
        return "a Cloudflare challenge page"
    head = " ".join(body[:160].split())
    return f"{len(body)} bytes of {content_type or 'unknown type'} starting {head!r}"


def _salvage_json(body: str):
    """Parse JSON from a body that may have junk glued to the front or back.

    The club's WordPress has been seen serving SEO-spam anchor tags ahead of
    EVERY response, REST API included — the JSON is intact, it just isn't at
    byte 0 any more. raw_decode() reads one value from an offset and ignores
    whatever trails it, so we recover the payload instead of losing the league
    data to someone else's injected markup. Returns (value, junk_prefix) or
    (None, None) when there's no JSON in there at all.
    """
    try:
        return json.loads(body), ""
    except json.JSONDecodeError:
        pass
    starts = [i for i in (body.find("["), body.find("{")) if i >= 0]
    if not starts:
        return None, None
    start = min(starts)
    try:
        value, _end = json.JSONDecoder().raw_decode(body, start)
    except (json.JSONDecodeError, ValueError):
        return None, None
    return value, body[:start]


class LeagueFetchError(RuntimeError):
    """A league endpoint returned something we can't use (with the why)."""


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# "June 16, 2026 7:45 pm" at the start of a draw heading.
_DRAW_RE = re.compile(
    r"^\s*([A-Z][a-z]+ \d{1,2},\s*\d{4})\s+(\d{1,2}:\d{2}\s*[ap]\.?m\.?)",
    re.IGNORECASE,
)
_SHEET_RE = re.compile(r"Sheet [A-Z]\b", re.IGNORECASE)
_SHEET_OPEN_RE = re.compile(r"Sheet [A-Z]\s+is open", re.IGNORECASE)


def _now_club() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TIMEZONE_OFFSET)


def _category_slug(league_post: dict) -> str | None:
    """Extract the league_category slug from a wp/v2/leagues post's class_list."""
    for cls in league_post.get("class_list", []):
        if cls.startswith("league_category-"):
            return cls[len("league_category-"):]
    return None


def _parse_draw_heading(text: str, today) -> dict | None:
    """Parse one Schedule & Scores <h6> into a draw dict, or None if not a draw."""
    text = " ".join(text.split())  # collapse whitespace/newlines
    m = _DRAW_RE.match(text)
    if not m:
        return None
    date_str, time_str = m.group(1), m.group(2)
    try:
        draw_date = datetime.strptime(
            re.sub(r"\s+", " ", date_str), "%B %d, %Y"
        ).date()
    except ValueError:
        return None

    time_norm = time_str.lower().replace(".", "").replace(" ", "")  # "7:45pm"
    time_pretty = re.sub(r"([ap]m)$", r" \1", time_norm)             # "7:45 pm"

    sheets_total = len(_SHEET_RE.findall(text))
    sheets_open = len(_SHEET_OPEN_RE.findall(text))
    sheets_used = max(0, sheets_total - sheets_open)

    return {
        "date": draw_date.isoformat(),
        "weekday": draw_date.strftime("%A"),
        "time": time_pretty,
        "upcoming": draw_date >= today,
        "sheets_used": sheets_used,
    }


def parse_league_html(html: str) -> dict[str, Any]:
    """Parse a league page's HTML into structured league info."""
    soup = BeautifulSoup(html, "html.parser")
    text_all = soup.get_text(" ", strip=True)
    ended = "this league has ended" in text_all.lower()

    # ── Teams: the Standings table ───────────────────────────────────────────
    teams = None
    team_names: list[str] = []
    for table in soup.find_all("table"):
        head = table.get_text(" ", strip=True).lower()
        if "team name" in head or "win %" in head:
            rows = table.find_all("tr")
            data_rows = [
                r for r in rows
                if r.find_all("td") and not r.find_all("th")
            ]
            teams = len(data_rows)
            for r in data_rows:
                cells = r.find_all("td")
                if len(cells) >= 2:
                    team_names.append(cells[1].get_text(" ", strip=True))
            break

    # ── Draws: the Schedule & Scores <h6> headers ────────────────────────────
    today = _now_club().date()
    draws: list[dict] = []
    for h in soup.find_all(["h6", "h5"]):
        draw = _parse_draw_heading(h.get_text(" ", strip=True), today)
        if draw:
            draws.append(draw)
    draws.sort(key=lambda d: d["date"])

    upcoming = [d for d in draws if d["upcoming"]]

    # Day + time: prefer real draw data, fall back to None (caller can use slug).
    # NB: named draw_time, not `time` — this module imports the stdlib `time`
    # module for the cache clock, and a local of that name is a trap waiting for
    # whoever next adds a time.time() call in here.
    day = draws[0]["weekday"] if draws else None
    draw_time = None
    if draws:
        # most common time across draws
        times = [d["time"] for d in draws if d["time"]]
        if times:
            draw_time = max(set(times), key=times.count)

    return {
        "teams": teams,
        "team_names": team_names,
        "ended": ended,
        "day": day,
        "time": draw_time,
        "draws": draws,
        "upcoming_draws": upcoming,
        "next_draw": upcoming[0] if upcoming else None,
    }


class LeagueClient:
    """Read-only client for the club's league pages."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(headers=_BROWSER_HEADERS, timeout=TIMEOUT)
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    async def active_leagues(self, per_page: int = 20) -> list[dict]:
        """
        Return current league posts as lightweight dicts:
        {id, title, slug, link, category, day}. Ordered newest-first by WP.
        """
        params = {
            "_fields": "id,title,slug,link,class_list",
            "per_page": per_page,
            "status": "publish",
        }
        async with self._session.get(LEAGUES_ENDPOINT, params=params) as r:
            r.raise_for_status()
            body = await r.text()
            ctype = r.headers.get("Content-Type", "")

        posts, junk = _salvage_json(body)
        if posts is None:
            # Log the endpoint path only — never the full URL with params.
            raise LeagueFetchError(
                f"wp/v2/leagues returned {_describe_body(body, ctype)}")
        if junk:
            # Recovered, but say so loudly and every time: content prepended to
            # the site's API responses means the SITE is injecting it, which is
            # a problem well beyond this bot.
            log.warning(
                "wp/v2/leagues had %d bytes of junk before the JSON — the site is "
                "injecting content into its API responses. Recovered the payload; "
                "prefix began %r", len(junk), " ".join(junk[:120].split()))
        if not isinstance(posts, list):
            raise LeagueFetchError(
                f"wp/v2/leagues returned {type(posts).__name__}, expected a list")

        out = []
        for p in posts:
            slug = _category_slug(p)
            out.append({
                "id": p["id"],
                "title": p.get("title", {}).get("rendered", ""),
                "slug": p.get("slug", ""),
                "link": p.get("link", ""),
                "category": slug,
                "day": DAY_BY_CATEGORY.get(slug),
            })
        return out

    async def league_info(self, link: str) -> dict[str, Any]:
        """Fetch a league page and parse teams / day / time / draws."""
        async with self._session.get(link) as r:
            r.raise_for_status()
            html = await r.text()
        return parse_league_html(html)

    async def all_active_league_info(self) -> list[dict]:
        """Convenience: active leagues merged with parsed page info."""
        leagues = await self.active_leagues()
        results = []
        for lg in leagues:
            try:
                info = await self.league_info(lg["link"])
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to parse league %s: %s", lg["link"], e)
                # Flag it rather than returning a bare entry: a league with no
                # `draws` is otherwise indistinguishable from one whose schedule
                # simply isn't posted yet, and the sub board invents league
                # nights for those (see subs.game_options).
                info = {"fetch_failed": True}
            merged = {**lg, **info}
            # Fall back to category-derived day if the page had no draws.
            if not merged.get("day"):
                merged["day"] = lg["day"]
            results.append(merged)
        return results


# ── JSON file cache ──────────────────────────────────────────────────────────
# The bot reads from this file instead of hitting the site on every request.
# It self-refreshes lazily: when the file is older than CACHE_TTL (or missing),
# the next call refetches and rewrites it. Override via env vars.
#
# In dev the repo is mounted into the container, so the cache survives restarts.
# In prod (no volume) it's ephemeral — fine, it just refetches once on boot.
# Force a refresh out-of-band with refresh_leagues.py (cron / scheduled task).

CACHE_PATH = os.environ.get("LEAGUE_CACHE_PATH", "league_cache.json")
CACHE_TTL = int(os.environ.get("LEAGUE_CACHE_TTL", "21600"))  # seconds (6h)


def _read_cache(path: str, ttl: int) -> list[dict] | None:
    """Return cached leagues if the file exists and is younger than ttl, else None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        age = time.time() - float(blob.get("fetched_at", 0))
        if age <= ttl:
            log.debug("League cache hit (age %.0fs)", age)
            return blob.get("leagues")
        log.debug("League cache stale (age %.0fs > ttl %ds)", age, ttl)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError):
        pass
    return None


def _write_cache(path: str, leagues: list[dict]) -> None:
    """Atomically write the cache file."""
    payload = {
        "fetched_at": time.time(),
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "leagues": leagues,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


async def get_cached_leagues(
    *, force: bool = False, cache_path: str = CACHE_PATH, ttl: int = CACHE_TTL
) -> list[dict]:
    """
    Return active-league info, served from the JSON cache when fresh.

    Refetches from the site (and rewrites the cache) when forced, when the
    cache is missing, or when it's older than `ttl`. If a refetch fails but a
    stale cache exists, the stale data is returned rather than raising.
    """
    if not force:
        cached = _read_cache(cache_path, ttl)
        if cached is not None:
            return cached

    try:
        async with LeagueClient() as lc:
            leagues = await lc.all_active_league_info()
        if leagues and all(lg.get("fetch_failed") for lg in leagues):
            # Every page failed: the list call worked but the site is unwell.
            # Caching this would overwrite good data with a set of leagues that
            # have no teams and no draws.
            raise LeagueFetchError(
                f"all {len(leagues)} league pages failed to load; keeping the old cache")
        _write_cache(cache_path, leagues)
        return leagues
    except Exception as e:  # noqa: BLE001 — fall back to stale cache on network errors
        log.warning("League refetch failed (%s); trying stale cache", e)
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f).get("leagues", [])
        except (FileNotFoundError, json.JSONDecodeError):
            raise e


def draw_to_datetime(draw: dict) -> datetime | None:
    """Combine a draw's date ('YYYY-MM-DD') and time ('7:45 pm') into a datetime."""
    date_str = draw.get("date")
    time_str = (draw.get("time") or "").strip().upper()  # "7:45 PM"
    if not date_str:
        return None
    if not time_str:
        return datetime.strptime(date_str, "%Y-%m-%d")
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p")
    except ValueError:
        return datetime.strptime(date_str, "%Y-%m-%d")


def leagues_on_weekday(leagues: list[dict], weekday: str) -> list[dict]:
    """Filter cached leagues to those that play on the given weekday (e.g. 'Tuesday')."""
    return [
        lg for lg in leagues
        if not lg.get("ended") and (lg.get("day") == weekday)
    ]
