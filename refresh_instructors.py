#!/usr/bin/env python3
"""
Print the instructor board, without touching Discord.

    python refresh_instructors.py

Reads the sheet and renders exactly what the bot would post. Needs only
SHEET_ID: no Discord token, no channel, nothing sent anywhere. Use it to check
the sheet parses and the staffing bands look right while developing.

To actually POST the board out of band, use `/instructors` in Discord: posting
goes through the bot's gateway connection, so it belongs in the running process
rather than in a one-shot script.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

import instructor_board as board          # noqa: E402
import instructor_sheet                   # noqa: E402


async def amain() -> int:
    try:
        csv_text = await instructor_sheet.fetch_csv()
    except RuntimeError as e:
        print(f"Could not read the sheet: {e}", file=sys.stderr)
        return 2
    events = instructor_sheet.parse_events(csv_text)
    print(board.render(events))
    print(f"\n---\n{board.summary_line(events)}", file=sys.stderr)
    return 0


def main() -> int:
    argparse.ArgumentParser(description="Print the instructor board").parse_args()
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
