"""
Overnight data refresh: warm the league cache, then check the rink's ice feeds.

Run out-of-band (a nightly cron / scheduled task) so the live bot never has to
block on a site fetch:

  docker compose run --rm curlbot python refresh_leagues.py

Two jobs, in one place because both are "go to the network and find out what's
really out there", and this is the only thing that does that on a schedule:

  1. Force-refresh league_cache.json. The bot also refreshes lazily when the
     cache goes stale (LEAGUE_CACHE_TTL, default 6h), so this only moves the
     network cost off the request path.
  2. Check the facility's reserved-ice feeds and post to Discord if the picture
     looks wrong — a feed that won't load, or a weekday that had ice recently
     and has none coming. That second case is a real /sheets outage that used to
     be invisible: see pond_check for what it detects and why. Set
     ALERT_CHANNEL_ID to the channel it posts to; unset, findings only print.

Nothing is sent when everything looks fine, and a problem that persists repeats
only every ALERT_REPEAT_DAYS — silence has to mean healthy or the alerts get
muted. Use --no-alert for a look without messaging anyone.
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import alerts                                          # noqa: E402
import pond_check                                      # noqa: E402
from league_client import get_cached_leagues, CACHE_PATH  # noqa: E402

POND_ICS_URLS = [u.strip() for u in os.environ.get("POND_ICS_URLS", "").split(",") if u.strip()]
POND_MATCH = os.environ.get("POND_MATCH", "curl")


async def refresh_leagues() -> list[dict]:
    """Refresh the league cache and print what's in it. Findings on failure."""
    try:
        leagues = await get_cached_leagues(force=True)
    except Exception:  # noqa: BLE001 — the message must not carry the request URL
        print(f"Could not refresh {CACHE_PATH}", file=sys.stderr)
        return [{"key": "league_refresh",
                 "text": "Couldn't refresh the league cache tonight. /sheets is "
                         "serving whatever it last cached, so league draws may be "
                         "stale or missing."}]

    active = [lg for lg in leagues if not lg.get("ended")]
    print(f"Refreshed {CACHE_PATH}: {len(leagues)} leagues ({len(active)} active)")
    for lg in active:
        nd = lg.get("next_draw")
        nxt = f"next {nd['weekday']} {nd['date']} {nd['time']}" if nd else "no upcoming draws"
        print(f"  • {lg.get('day')}: {lg.get('teams')} teams — {nxt}")
    if not active:
        return [{"key": "no_active_leagues",
                 "text": "The league pages list no active leagues. Between seasons "
                         "that's expected; mid-season it means /sheets has lost the "
                         "league draws."}]
    return []


async def check_pond(weeks_back: int, weeks_ahead: int) -> list[dict]:
    """Check the reserved-ice feeds and print the picture. Findings on trouble."""
    if not POND_ICS_URLS:
        print("Reserved ice: no POND_ICS_URLS configured — skipping.")
        return []
    feeds, past, ahead = await pond_check.collect(
        POND_ICS_URLS, weeks_back=weeks_back, weeks_ahead=weeks_ahead, match=POND_MATCH)
    print(pond_check.report(feeds, past, ahead, weeks_back, weeks_ahead))
    return pond_check.findings(feeds, past, ahead, weeks_back, weeks_ahead)


async def amain(args) -> int:
    found = await refresh_leagues()
    found += await check_pond(args.weeks_back, args.weeks_ahead)

    if not found:
        print("\nNothing to report.")
    else:
        print(f"\n{len(found)} finding(s):")
        for f in found:
            print(f"  ⚠ {f['text']}")

    if args.no_alert:
        return 0

    state = alerts.load()
    to_send, resolved, new_state = alerts.due(state, found, pond_check.now_local())
    sent = False
    if to_send:
        sent = await alerts.send(pond_check.alert_text(to_send))
    elif resolved:
        sent = await alerts.send(alerts.resolved_text(resolved))
    if to_send and not sent:
        # The state only advances when the message actually landed, so a Discord
        # hiccup delays the alert rather than swallowing it.
        print("Alert not delivered; leaving it to fire again next run.", file=sys.stderr)
        return 0
    alerts.save(new_state)
    if sent:
        print("Alert sent.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Refresh cached data and check the ice feeds.")
    p.add_argument("--weeks-ahead", type=int, default=pond_check.WEEKS_AHEAD,
                   dest="weeks_ahead", help="how far ahead the ice check looks")
    p.add_argument("--weeks-back", type=int, default=pond_check.WEEKS_BACK,
                   dest="weeks_back", help="how far back it looks for the usual pattern")
    p.add_argument("--no-alert", action="store_true", dest="no_alert",
                   help="print findings without messaging anyone (and without "
                        "recording that they were reported)")
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
