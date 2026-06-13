"""
Validate league_client.py against the live league pages.

Run:
  docker compose run --rm curlbot python discover_league_pages.py

Expected (as of 2026-06-12):
  • Tuesday  Summer 2026 League 1 — 6 teams, Tuesday 7:45 pm, upcoming draws Jun 16/23/30
  • Thursday Summer 2026 League 1 — 8 teams, Thursday 7:45 pm, upcoming draws Jun 18/25
"""

import asyncio
import json

from league_client import LeagueClient


async def main():
    async with LeagueClient() as lc:
        leagues = await lc.all_active_league_info()
        for lg in leagues:
            ended = " [ENDED]" if lg.get("ended") else ""
            print(f"\n=== {lg['title']}{ended} ===")
            print(f"  id={lg['id']}  category={lg.get('category')}")
            print(f"  teams : {lg.get('teams')}  -> sheets in play = {_sheets(lg)}")
            print(f"  day   : {lg.get('day')}    time: {lg.get('time')}")
            if lg.get("team_names"):
                print(f"  roster: {', '.join(lg['team_names'])}")
            up = lg.get("upcoming_draws") or []
            print(f"  upcoming draws ({len(up)}):")
            for d in up:
                print(f"    - {d['date']} ({d['weekday']}) {d['time']}  "
                      f"sheets_used={d['sheets_used']}")
            if lg.get("next_draw"):
                nd = lg["next_draw"]
                print(f"  NEXT  : {nd['weekday']} {nd['date']} {nd['time']} "
                      f"({nd['sheets_used']} sheets)")


def _sheets(lg):
    t = lg.get("teams")
    return (t + 1) // 2 if isinstance(t, int) else "?"


asyncio.run(main())
