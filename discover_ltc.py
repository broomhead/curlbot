"""
Diagnose the LTC headcount for a single event — find the right join key.

Admin shows 30 people for June 13, but filtering form-110 entries by
source_id==25704 only sums 23 (and misses an entry with field 2 = 6). So
entries land under multiple source_ids; source_id is the wrong key.

Every June 13 entry carries field 34 (Event Name) = "Saturday Learn to Curl
- June 13". This script tests matching by Event Name instead, pages through
ALL matches, and dumps candidate join keys (source_id, field 31 date, field
35 date) so we can pick the most robust one for the bot.

Run:
  docker compose run --rm curlbot python discover_ltc.py "June 13"
  docker compose run --rm curlbot python discover_ltc.py "June 13" 25704
"""

import asyncio
import json
import sys
import math
from collections import Counter

from dotenv import load_dotenv
load_dotenv()

from gf_client import GFClient

LTC_FORM_ID = 110
PEOPLE_PER_SHEET = 8


def people(entry: dict) -> int:
    try:
        return int(float(str(entry.get("2", "")).strip()))
    except (ValueError, TypeError):
        return 0


async def fetch_filtered(gf: GFClient, field_filters: list[dict]) -> list[dict]:
    """All entries matching the given field_filters, paging through."""
    out, page, total = [], 1, None
    while True:
        data = await gf.get(
            f"/wp-json/gf/v2/forms/{LTC_FORM_ID}/entries",
            params={
                "paging[page_size]": 100,
                "paging[current_page]": page,
                "search": json.dumps({"field_filters": field_filters}),
            },
        )
        batch = data.get("entries", [])
        out.extend(batch)
        total = int(data.get("total_count", len(out)))
        if len(out) >= total or not batch:
            break
        page += 1
    return out


def report(label: str, entries: list[dict]):
    print(f"\n=== {label}  ({len(entries)} entries) ===")
    total_people = 0
    for e in entries:
        p = people(e)
        total_people += p
        print(f"  entry {e.get('id'):>6}  f2={p:>2}  source_id={e.get('source_id')!r:>8}  "
              f"f31={e.get('31')!r}  f34={e.get('34')!r}  paid={e.get('payment_status')!r}")
    sheets = math.ceil(total_people / PEOPLE_PER_SHEET) if total_people else 0
    print(f"  --> Σ people = {total_people}  =>  {sheets} sheets")
    print(f"  source_ids seen: {dict(Counter(str(e.get('source_id')) for e in entries))}")
    print(f"  field 31 seen  : {dict(Counter(str(e.get('31')) for e in entries))}")
    return total_people


async def main():
    name_needle = sys.argv[1] if len(sys.argv) > 1 else "June 13"
    date_iso    = sys.argv[2] if len(sys.argv) > 2 else "2026-06-13"

    async with GFClient() as gf:
        # The bot's NEW method: match by event date (field 31, ISO).
        by_date = await fetch_filtered(
            gf, [{"key": "31", "operator": "is", "value": date_iso}]
        )
        n_date = report(f"Matched by event DATE (field 31 is {date_iso!r}) — bot's new method", by_date)

        # Cross-check: match by Event Name (field 34) contains.
        by_name = await fetch_filtered(
            gf, [{"key": "34", "operator": "contains", "value": name_needle}]
        )
        n_name = report(f"Matched by Event Name (field 34) contains {name_needle!r}", by_name)

        print(f"\nfield 31 (date) -> {n_date} people; field 34 (name) -> {n_name} people. "
              f"Both should equal the admin count (34).")


asyncio.run(main())
