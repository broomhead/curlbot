"""
Sheets of ice: the one place the club's facility numbers live.

Both halves of the bot need to turn a headcount into a number of sheets. The
/sheets report does it to work out how much ice an LTC will occupy; the
instructor board does it to work out how many instructors that LTC needs. They
must never disagree, so the arithmetic lives here and both import it.

Pure module: no discord, no network, no env beyond the facility size.
"""

from __future__ import annotations

import math
import os

# Sheet count varies by facility - configure via NUM_SHEETS (default 4).
TOTAL_SHEETS = int(os.environ.get("NUM_SHEETS", "4"))
# Max people on one sheet.
PEOPLE_PER_SHEET = int(os.environ.get("PEOPLE_PER_SHEET", "8"))


def sheets_for_people(people: int | float, *, cap: bool = True) -> int:
    """Sheets needed for `people` participants: one per PEOPLE_PER_SHEET, at
    least one. Capped at the facility's sheet count unless cap=False (a private
    event's price can imply more ice than the club actually has, and the caller
    there wants the raw number)."""
    n = max(1, math.ceil(people / max(1, PEOPLE_PER_SHEET)))
    return min(TOTAL_SHEETS, n) if cap else n
