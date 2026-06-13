"""
League data discovery script.

Investigates:
  1. GF entry counts for a known league (a test league with a known team count
     per its standings page) — tells us whether entries map to teams or
     individuals.
  2. GravityView REST API — might expose structured team/standings data.
  3. dbd-participants posts for the same league — see if they help.

Usage:
  docker compose run --rm curlbot python discover_leagues.py
"""

import asyncio
import json
import os

from dotenv import load_dotenv
load_dotenv()

from gf_client import GFClient
import aiohttp

BASE_URL = os.environ.get("SITE_BASE_URL", "https://example.com")

# A test league post id (set to a real league id when running against a site).
# source_id in GF = WP post ID of the league registration page.
TEST_LEAGUE_SOURCE_ID = os.environ.get("TEST_LEAGUE_ID", "0")
TEST_LEAGUE_NAME      = "test league"

LEAGUE_FORM_IDS = {
    108: "League Reg - Ind Only",
    113: "League Reg - Ind & Team",
    140: "Unknown league form",
    148: "Unknown league form",
}


def pp(obj, limit=2000):
    s = json.dumps(obj, indent=2)
    print(s[:limit] + ("\n  …truncated" if len(s) > limit else ""))


async def fetch_all_entries(gf: GFClient, form_id: int, source_id: str) -> list[dict]:
    """Fetch all entries for a given source_id (pages through if needed)."""
    all_entries = []
    page = 1
    page_size = 100
    while True:
        data = await gf.get(
            f"/wp-json/gf/v2/forms/{form_id}/entries",
            params={
                "paging[page_size]": page_size,
                "paging[current_page]": page,
                "search": json.dumps({
                    "field_filters": [{"key": "source_id", "value": source_id}]
                }),
            },
        )
        entries = data.get("entries", [])
        all_entries.extend(entries)
        if len(all_entries) >= int(data.get("total_count", 0)):
            break
        page += 1
    return all_entries


async def probe_gravityview(session: aiohttp.ClientSession):
    """List GravityView views and try to find league-related ones."""
    print("\n=== GravityView REST API ===")
    try:
        async with session.get(f"{BASE_URL}/wp-json/gravityview/v1/views") as r:
            if r.status != 200:
                print(f"  /views → HTTP {r.status}")
                return
            data = await r.json()
            views = data if isinstance(data, list) else data.get("views", data)
            for v in (views[:20] if isinstance(views, list) else []):
                print(f"  View {v.get('ID','?'):>5}: {v.get('post_title','?')}")
    except Exception as e:
        print(f"  Error: {e}")


async def probe_dbd_participants(session: aiohttp.ClientSession, source_id: str):
    """Check dbd-participants posts and see if any link to our league."""
    print(f"\n=== dbd-participants (first 5, looking for league {source_id}) ===")
    try:
        # Try filtering by parent or meta
        async with session.get(
            f"{BASE_URL}/wp-json/wp/v2/dbd-participants",
            params={"per_page": 5, "search": source_id},
        ) as r:
            if r.status != 200:
                print(f"  HTTP {r.status}")
                return
            items = await r.json()
            if not items:
                print("  No items found with source_id in search")
                return
            for item in items[:3]:
                print(f"  ID {item['id']}: {item['title']['rendered'][:80]}")
                # Print a snippet of content to see field values
                raw = item.get("content", {}).get("rendered", "")
                print(f"    content snippet: {raw[:300]}")
    except Exception as e:
        print(f"  Error: {e}")


async def main():
    async with GFClient() as gf, aiohttp.ClientSession() as session:

        # ── 1. GF entries for the test league, all forms ─────────────────────
        print(f"\n=== GF Entries for {TEST_LEAGUE_NAME} ===")
        print(f"    source_id = {TEST_LEAGUE_SOURCE_ID}\n")
        all_league_entries = []
        for form_id, form_name in LEAGUE_FORM_IDS.items():
            try:
                entries = await fetch_all_entries(gf, form_id, TEST_LEAGUE_SOURCE_ID)
                print(f"  Form {form_id} ({form_name}): {len(entries)} entries")
                all_league_entries.extend(entries)

                # Print key fields from each entry
                for i, e in enumerate(entries):
                    reg_type   = e.get("11", "")    # Registration type
                    name       = e.get("3",  "?")   # Registrant name
                    field6     = e.get("6",  "")    # Possible team name/handle
                    pos_fields = {k: e[k] for k in ("56","57","58","59","97","98","99","100") if e.get(k)}
                    print(f"    [{i+1}] name={name!r}  reg_type={reg_type!r}  field6={field6!r}  positions={pos_fields}")
            except Exception as ex:
                print(f"  Form {form_id}: Error — {ex}")

        # ── The key question: distinct team identities ────────────────────────
        print(f"\n=== Team Identity Analysis (field 6 = team name/handle?) ===")
        f6_values = [e.get("6", "").strip() for e in all_league_entries]
        distinct_teams = sorted(set(v for v in f6_values if v))
        blank_count    = f6_values.count("")
        print(f"  Total entries across all forms : {len(all_league_entries)}")
        print(f"  Distinct non-blank field 6 vals: {len(distinct_teams)}")
        print(f"  Blank field 6                  : {blank_count}")
        print(f"  Distinct values: {distinct_teams}")
        print()
        print(f"  Expected teams from standings  : 6")
        print(f"  → If distinct field 6 ≈ 6, that's the team identifier.")
        print(f"  → If distinct field 6 ≈ 1 per entry, it's per-person.")
        print(f"  → If mostly blank, field 6 isn't the team field — check field 3.")

        # Also check forms 140 and 148
        print(f"\n=== Checking forms 140, 148 for source_id {TEST_LEAGUE_SOURCE_ID} ===")
        for fid in (140, 148):
            try:
                entries = await fetch_all_entries(gf, fid, TEST_LEAGUE_SOURCE_ID)
                print(f"  Form {fid}: {len(entries)} entries")
                for e in entries[:3]:
                    print(f"    {json.dumps({k: v for k, v in e.items() if v and k not in ('ip','user_agent','source_url','submission_speeds')}, indent=2)[:400]}")
            except Exception as ex:
                print(f"  Form {fid}: {ex}")

        # ── 3. GravityView API ────────────────────────────────────────────────
        await probe_gravityview(session)

        # ── 4. dbd-participants ───────────────────────────────────────────────
        await probe_dbd_participants(session, TEST_LEAGUE_SOURCE_ID)

        # ── 5. Quick check: what does the wp/v2/leagues endpoint expose? ──────
        print(f"\n=== wp/v2/leagues for post {TEST_LEAGUE_SOURCE_ID} (meta fields) ===")
        try:
            async with session.get(
                f"{BASE_URL}/wp-json/wp/v2/leagues/{TEST_LEAGUE_SOURCE_ID}",
                params={"_fields": "id,title,meta,acf,content,slug"},
            ) as r:
                data = await r.json()
                print(f"  title : {data.get('title',{}).get('rendered','?')}")
                print(f"  meta  : {data.get('meta', {})}")
                print(f"  acf   : {data.get('acf', [])}")
                print(f"  content rendered: {str(data.get('content',{}).get('rendered',''))[:200]!r}")
        except Exception as ex:
            print(f"  Error: {ex}")


asyncio.run(main())
