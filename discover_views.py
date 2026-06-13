"""
Run this to map out Gravity Forms data available via the GF REST API.
Helps identify:
  - Which form holds private event bookings (look for price/sheets field)
  - Which form holds Learn to Curl registrations (individual + group counts)

Usage:
  docker compose run --rm curlbot python discover_views.py
"""

import asyncio
import json
import os

from dotenv import load_dotenv
load_dotenv()

from gf_client import GFClient


def pp(obj, limit=1500):
    s = json.dumps(obj, indent=2)
    print(s[:limit] + ("\n  …truncated" if len(s) > limit else ""))


async def main():
    async with GFClient() as gf:

        # ── List all forms ────────────────────────────────────────────────────
        print("\n=== Gravity Forms ===")
        forms = await gf.forms()
        if not forms:
            print("  No forms returned — check API key permissions")
            return

        for f in forms:
            fields = [fi.get("label", "?") for fi in f.get("fields", [])]
            print(f"\n  Form {f['id']:>3}: {f['title']}")
            print(f"            Fields: {fields}")

        # ── Sample entries from each form ─────────────────────────────────────
        print("\n=== Sample entries (last 2 per form) ===")
        for f in forms:
            fid = f["id"]
            print(f"\n  ── Form {fid}: {f['title']} ──")
            try:
                entries = await gf.entries(fid, params={
                    "paging[page_size]": 2,
                    "sorting[key]":       "date_created",
                    "sorting[direction]": "DESC",
                })
                if not entries:
                    print("    (no entries)")
                for e in entries[:2]:
                    pp(e)
            except Exception as ex:
                print(f"    Error: {ex}")


asyncio.run(main())
