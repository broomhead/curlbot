"""
Facility "reserved ice" source for /sheets.

The club's home rink reserves curling ice the club may use for LTCs/events, but
which isn't on the club's own calendar until something is actually booked on it.
Those blocks live on the facility's public Google Calendar. We read that
calendar's iCal feed, keep events whose title marks them as curling, and hand
them to bot.py to surface as open practice ice whenever nothing the club has
already booked overlaps them.

Design mirrors practice_ice.py: the parsing/expansion is pure (no Discord /
network) so it's unit-testable; only `fetch_reserved_curling` does I/O.

Feed times are wall-clock local (TZID America/Chicago, same as the club). We
return naive local datetimes to match the rest of the bot.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Chicago")
_UTC = ZoneInfo("UTC")
# Python's date.weekday(): Mon=0 … Sun=6 — index into iCal two-letter codes.
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

DEFAULT_DURATION = timedelta(hours=2)  # used only if an event omits DTEND
_MAX_ITER = 4000                        # recurrence expansion safety bound


# ── pure parsing ────────────────────────────────────────────────────────────

def _unfold(text: str) -> str:
    """Undo RFC 5545 line folding (continuation lines start with space/tab)."""
    return text.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "")


def _parse_line(line: str):
    """Split 'NAME;PARAM=v:VALUE' into (NAME, {params}, value); None if not a prop."""
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    parts = left.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return parts[0].upper(), params, value.strip()


def _to_local_naive(value: str, params: dict):
    """Parse an iCal date-time to a naive America/Chicago wall-clock datetime.

    Returns None for date-only (all-day) values — reserved ice is always timed.
    """
    v = value.strip()
    if "T" not in v:
        return None  # VALUE=DATE all-day — not a timed block
    is_utc = v.endswith("Z")
    core = v[:-1] if is_utc else v
    dt = None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            dt = datetime.strptime(core, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if is_utc:
        return dt.replace(tzinfo=_UTC).astimezone(LOCAL_TZ).replace(tzinfo=None)
    tzid = params.get("TZID")
    if tzid and tzid != "America/Chicago":
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid)).astimezone(LOCAL_TZ).replace(tzinfo=None)
        except Exception:  # noqa: BLE001 — unknown TZID, fall back to wall time
            pass
    return dt  # already local wall time


def parse_events(ics_text: str) -> list[dict]:
    """Parse VEVENTs into dicts: summary, dtstart, dtend, rrule, exdates(set)."""
    events: list[dict] = []
    cur = None
    for line in _unfold(ics_text).split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {"summary": "", "dtstart": None, "dtend": None, "rrule": "", "exdates": set()}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None:
            parsed = _parse_line(line)
            if not parsed:
                continue
            name, params, value = parsed
            if name == "SUMMARY":
                cur["summary"] = value
            elif name == "DTSTART":
                cur["dtstart"] = _to_local_naive(value, params)
            elif name == "DTEND":
                cur["dtend"] = _to_local_naive(value, params)
            elif name == "RRULE":
                cur["rrule"] = value
            elif name == "EXDATE":
                for v in value.split(","):
                    d = _to_local_naive(v, params)
                    if d is not None:
                        cur["exdates"].add(d)
    return events


def _rrule_dict(rrule: str) -> dict:
    out = {}
    for kv in rrule.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.upper()] = v
    return out


def expand(event: dict, window_start: datetime, window_end: datetime) -> list[tuple]:
    """Yield (start, end) occurrences of `event` overlapping [window_start, window_end].

    Handles single events plus WEEKLY/DAILY RRULEs (INTERVAL, BYDAY, UNTIL, COUNT)
    and EXDATE exclusions. MONTHLY/YEARLY/unknown rules degrade to the single base
    occurrence — fine for ice bookings, which are one-off or weekly.
    """
    ds = event.get("dtstart")
    if ds is None:
        return []
    de = event.get("dtend") or (ds + DEFAULT_DURATION)
    dur = de - ds if de > ds else DEFAULT_DURATION
    ex = event.get("exdates") or set()
    occ: list[tuple] = []

    def emit(start_dt: datetime):
        if start_dt in ex:
            return
        end_dt = start_dt + dur
        if end_dt <= window_start or start_dt >= window_end:
            return
        occ.append((start_dt, end_dt))

    rr = event.get("rrule")
    if not rr:
        emit(ds)
        return occ

    p = _rrule_dict(rr)
    freq = p.get("FREQ")
    interval = max(1, int(p.get("INTERVAL", "1") or 1))
    count = int(p["COUNT"]) if p.get("COUNT") else None
    until = _to_local_naive(p["UNTIL"], {}) if p.get("UNTIL") else None
    emitted = 0

    if freq == "WEEKLY":
        days = [b[-2:] for b in p["BYDAY"].split(",")] if p.get("BYDAY") else [_WEEKDAYS[ds.weekday()]]
        week0 = ds.date() - timedelta(days=ds.weekday())  # Monday of the start week
        d = ds.date()
        guard = _MAX_ITER
        while guard > 0:
            guard -= 1
            start_dt = datetime.combine(d, ds.time())
            if until and start_dt > until:
                break
            if start_dt > window_end and (count is None):
                break
            if start_dt >= ds and _WEEKDAYS[d.weekday()] in days:
                if ((d - week0).days // 7) % interval == 0:
                    emitted += 1
                    if count is not None and emitted > count:
                        break
                    emit(start_dt)
            d += timedelta(days=1)
    elif freq == "DAILY":
        d = ds
        guard = _MAX_ITER
        while guard > 0:
            guard -= 1
            if until and d > until:
                break
            if d > window_end and count is None:
                break
            emitted += 1
            if count is not None and emitted > count:
                break
            emit(d)
            d += timedelta(days=interval)
    else:
        emit(ds)
    return occ


def reserved_curling_sessions(ics_texts, window_start, window_end, match: str = "curl") -> list[dict]:
    """All curling-marked occurrences across the feeds, de-duped and time-sorted.

    Each item: {"start": dt, "end": dt, "title": str}. `match` is matched
    case-insensitively against each event's SUMMARY.
    """
    needle = (match or "curl").lower()
    rows: list[dict] = []
    for txt in ics_texts:
        for ev in parse_events(txt):
            if needle not in ev["summary"].lower():
                continue
            for start, end in expand(ev, window_start, window_end):
                rows.append({"start": start, "end": end, "title": ev["summary"]})
    seen, uniq = set(), []
    for o in sorted(rows, key=lambda x: x["start"]):
        key = (o["start"], o["end"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(o)
    return uniq


# ── Feed labels + suspicious titles (pure) ──────────────────────────────────

# Titles the rink uses for ice it has pencilled in but not committed. They match
# `curl` like any other block, so they'd be advertised as free practice ice —
# worth flagging rather than silently trusting.
TENTATIVE_MARKERS = ("tentative", "hold", "pending", "temp")


def is_tentative(title: str) -> bool:
    """True if a block's title marks it as provisional rather than confirmed."""
    t = (title or "").lower()
    return any(m in t for m in TENTATIVE_MARKERS)


def feed_label(url: str) -> str:
    """Short name for a feed, for reports: the calendar id without its domain.

    Falls back to the trimmed URL for anything that isn't the usual iCal shape.
    """
    from urllib.parse import unquote

    marker = "/ical/"
    if marker in url:
        rest = url.split(marker, 1)[1]
        cal_id = unquote(rest.split("/", 1)[0])
        return cal_id.split("@", 1)[0] or cal_id
    return url[:40]


def weekdays_covered(occurrences) -> set:
    """The weekday numbers (Mon=0 … Sun=6) that these occurrences land on."""
    return {o["start"].weekday() for o in occurrences}


# ── I/O (cached fetch) ──────────────────────────────────────────────────────

# url -> last text we successfully read, plus when the set was last refreshed.
_cache = {"ts": 0.0, "texts": {}}


async def fetch_feeds(urls, ttl: int = 21600, force: bool = False) -> list[dict]:
    """Read each feed, one entry per URL: {"url", "label", "text", "ok"}.

    `ok` is False when this run couldn't read that feed; `text` then falls back to
    the last good copy of THAT feed (or None if there's never been one), so one
    dead feed can't blank out the others. `force` bypasses the cache — for the
    out-of-band refresh, which exists precisely to go to the network.
    """
    if not urls:
        return []
    import aiohttp  # local import keeps the parser import-light and pure

    now = time.monotonic()
    cached = _cache["texts"]
    if not force and cached and now - _cache["ts"] < ttl:
        # Inside the TTL, `ok` means "we have a copy of this feed" — the health
        # check passes force=True precisely so its `ok` reflects a live attempt.
        return [{"url": u, "label": feed_label(u), "text": cached.get(u), "ok": u in cached}
                for u in urls]

    results: list[dict] = []
    try:
        async with aiohttp.ClientSession() as s:
            for url in urls:
                text, ok = None, False
                try:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            text, ok = await r.text(), True
                        else:
                            log.warning("Pond ICS fetch -> HTTP %s (%s)", r.status, feed_label(url))
                except Exception as ex:  # noqa: BLE001 — per-feed failure
                    log.warning("Pond ICS fetch failed (%s): %s", feed_label(url), ex)
                results.append({"url": url, "label": feed_label(url), "text": text, "ok": ok})
    except Exception as ex:  # noqa: BLE001 — session-level failure
        log.warning("Pond ICS session failed: %s", ex)
        results = [{"url": u, "label": feed_label(u), "text": None, "ok": False} for u in urls]

    for f in results:
        if f["ok"]:
            cached[f["url"]] = f["text"]
        else:
            f["text"] = cached.get(f["url"])  # serve this feed's last good copy
    if any(f["ok"] for f in results):
        _cache["ts"] = now
    return results


async def fetch_reserved_curling(urls, window_start, window_end,
                                 ttl: int = 21600, match: str = "curl") -> list[dict]:
    """Fetch the configured iCal feeds (cached `ttl` seconds) and return the
    curling occurrences in the window. Network failures degrade to stale cache,
    then to an empty list, so /sheets never breaks on a feed hiccup."""
    feeds = await fetch_feeds(urls, ttl)
    texts = [f["text"] for f in feeds if f["text"]]
    return reserved_curling_sessions(texts, window_start, window_end, match)
