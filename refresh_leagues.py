"""
Force-refresh the league cache (league_cache.json).

Use this to update the cache out-of-band so the live bot never has to block on
a site fetch. Run on a schedule, e.g. a daily cron / scheduled task:

  docker compose run --rm curlbot python refresh_leagues.py

The bot also refreshes lazily on its own when the cache goes stale
(LEAGUE_CACHE_TTL, default 6h), so this script is optional — it just keeps the
file warm and moves the network cost off the request path.
"""

import asyncio

from league_client import get_cached_leagues, CACHE_PATH


async def main():
    leagues = await get_cached_leagues(force=True)
    active = [lg for lg in leagues if not lg.get("ended")]
    print(f"Refreshed {CACHE_PATH}: {len(leagues)} leagues ({len(active)} active)")
    for lg in active:
        nd = lg.get("next_draw")
        nxt = f"next {nd['weekday']} {nd['date']} {nd['time']}" if nd else "no upcoming draws"
        print(f"  • {lg.get('day')}: {lg.get('teams')} teams — {nxt}")


asyncio.run(main())
