"""Partiful policy layered on the generic iCalendar transport."""

from __future__ import annotations

import concurrent.futures
import html
import re
import sqlite3
import urllib.request

from ... import archive, db, events, trace

#: Partiful's in-band placeholder for a field it will not disclose yet. Matched on the
#: shape of the sentence rather than on one exact string, because the wording is theirs
#: to change and a near-miss silently restores the original bug.
#:
#: This is deliberately **not** a general "is this a real address" test. Judging venues
#: by their prose is how a hand-grown list of thirteen regexes starts; this asks the far
#: narrower question the feed actually answers — *is this the platform declining to
#: tell us* — and anything it does not recognise is treated as a real location, which
#: is the safe direction. A withheld location wrongly kept is a wrong venue on a row the user
#: can correct; a real location wrongly dropped is a fact destroyed at ingest.
WITHHELD = re.compile(
    r"""(?:location|address|venue|details?)\b[^.]{0,40}\b
        (?:once|after|upon|when)\b[^.]{0,20}\bRSVP
      | \bRSVP\b[^.]{0,30}\b(?:to\s+see|for\s+the)\b[^.]{0,20}
        (?:location|address|venue)
      | ^\s*(?:TBD|TBA|to\s+be\s+(?:determined|announced))\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Public event pages disclose owner ids, and the corresponding public profile pages
# disclose the names the site renders under "Hosted by". Keep this deliberately small
# and bounded: calendar collection must still finish when Partiful is slow or changes
# its markup.
HOST_LOOKUP_TIMEOUT = 5
HOST_LOOKUP_WORKERS = 4
HOST_LOOKUP_LIMIT = 20
_EVENT_ID = re.compile(r"https?://(?:www\.)?partiful\.com/e/([A-Za-z0-9_-]+)", re.I)
_OWNERS = re.compile(r'"owners":\[(.*?)\]', re.S)
_OWNER_ID = re.compile(r'"id":"([A-Za-z0-9_-]+)"')
_PROFILE_TITLE = re.compile(r"<title[^>]*>(.*?)\s*\|\s*Partiful</title>", re.I | re.S)


def _fetch_text(url: str, timeout: int = HOST_LOOKUP_TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "memcal/partiful-hosts"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _event_url(item: dict) -> str | None:
    text = " ".join(str(item.get(key) or "") for key in ("url", "description"))
    found = _EVENT_ID.search(text)
    return f"https://partiful.com/e/{found.group(1)}" if found else None


def owner_ids(page: str) -> list[str]:
    """Return public owner ids in the order Partiful renders them."""
    block = _OWNERS.search(page or "")
    if not block:
        return []
    return list(dict.fromkeys(_OWNER_ID.findall(block.group(1))))


def profile_name(page: str) -> str | None:
    """Read the display name from a public Partiful profile page."""
    found = _PROFILE_TITLE.search(page or "")
    if not found:
        return None
    name = " ".join(html.unescape(found.group(1)).split()).strip()
    return name if name and name.casefold() != "partiful" else None


def _cached_hosts(conn: sqlite3.Connection | None, item: dict) -> list[str]:
    if conn is None or not item.get("uid"):
        return []
    row = conn.execute(
        """SELECT e.hosts FROM calendar_items c
             JOIN events e ON e.key = c.event_key
            WHERE c.event_uid = ? AND c.provider = 'partiful'
              AND e.hosts != '[]'
            ORDER BY c.last_seen_at DESC LIMIT 1""",
        (str(item["uid"]),),
    ).fetchone()
    return db.jload(row["hosts"], []) if row else []


def enrich_hosts(items: list[dict], *, conn: sqlite3.Connection | None = None,
                 fetch=_fetch_text, report=None) -> int:
    """Add public host names to upcoming Partiful calendar items.

    Failures are non-fatal and the number of event pages is capped. The calendar feed
    remains the source of dates and RSVP state; this only fills the separate host field.
    """
    targets: list[tuple[dict, str]] = []
    for item in items:
        if not POLICY.claims(item):
            continue
        known = list(item.get("hosts") or _cached_hosts(conn, item))
        if known:
            item["hosts"] = known
            continue
        try:
            if db.parse_ts(str(item.get("start") or "")).date() < db.today():
                continue
        except ValueError:
            continue
        url = _event_url(item)
        if url:
            targets.append((item, url))
        if len(targets) >= HOST_LOOKUP_LIMIT:
            break
    if not targets:
        return 0

    event_pages: dict[str, str] = {}
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=HOST_LOOKUP_WORKERS) as pool:
        futures = {pool.submit(fetch, url): url for _item, url in targets}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                event_pages[url] = future.result()
            except Exception:
                failed += 1

    ids = list(dict.fromkeys(
        owner for page in event_pages.values() for owner in owner_ids(page)
    ))
    profiles: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=HOST_LOOKUP_WORKERS) as pool:
        futures = {
            pool.submit(fetch, f"https://partiful.com/u/{owner}"): owner for owner in ids
        }
        for future in concurrent.futures.as_completed(futures):
            owner = futures[future]
            try:
                name = profile_name(future.result())
                if name:
                    profiles[owner] = name
            except Exception:
                failed += 1

    enriched = 0
    for item, url in targets:
        names = [profiles[owner] for owner in owner_ids(event_pages.get(url, ""))
                 if owner in profiles]
        if names:
            item["hosts"] = list(dict.fromkeys(names))
            enriched += 1
    if report is not None:
        if enriched:
            report.notes.append(f"Partiful hosts found for {enriched} event(s)")
        if failed:
            report.notes.append(f"Partiful host lookup failed for {failed} public page(s)")
    return enriched


def disclosed(location: str | None) -> str | None:
    """The location if the feed is actually telling us one, else None.

    Returns None for the placeholder so it never reaches `events.location`. A status
    message living in the venue field is the second half of this bug: the brief rendered
    "Location available once RSVP'd" where the address goes, one clause away from a note
    claiming the user had replied.
    """
    text = " ".join(str(location or "").split())
    if not text or WITHHELD.search(text):
        return None
    return text


class Partiful:
    name = "partiful"

    def claims(self, item: dict, cfg=None) -> bool:
        """Recognize Partiful by calendar name or event-owned URLs/descriptions."""
        names = (getattr(cfg, "secret", lambda *_: None)("MEMCAL_PARTIFUL_CALENDAR") or "")
        configured = {name.strip().casefold() for name in names.split(",") if name.strip()}
        calendar_name = str(item.get("calendar_name") or "").strip().casefold()
        if calendar_name in configured:
            return True
        text = " ".join(str(item.get(key) or "") for key in (
            "calendar_name", "url", "description",
        )).casefold()
        return "partiful" in text or "partiful.com" in text

    def fields(self, item: dict, common: dict) -> dict:
        """Apply the RSVP inference while retaining normalized calendar fields."""
        fields = dict(common)
        # The reply link, which `claims()` has always read for detection and the
        # connector then threw away. It is the difference between "there is a party"
        # and "here is where you reply to it" — the thing you can forward to somebody.
        if item.get("url"):
            fields["rsvp_url"] = str(item["url"])
        hosts = [str(name).strip() for name in (item.get("hosts") or [])
                 if str(name).strip()]
        if hosts:
            fields["hosts"] = list(dict.fromkeys(hosts))
        # Normalized rather than raw: `_normalized` has already lifted a join link out
        # of this field, and the placeholder has to go the same way.
        fields["location"] = disclosed(fields.get("location"))
        if fields["location"]:
            fields.update(kind="commitment", status="confirmed")
        else:
            # `rsvp_url` makes `plain_state()` render this as "not replied" rather than
            # "could go" — an invitation has a button, and saying both with the same
            # three words throws that away.
            fields.update(kind="opportunity", status="mentioned")
        # `note` is *not* touched. It carries what the invitation said about itself,
        # which `ical._normalized` lifts from the calendar description, and this used to
        # overwrite it with the string "Partiful RSVP yes" — so 17 of 18 live rows held
        # that phrase where "Doors 6:30, ask for Nadia at the desk" should have been,
        # destroyed at ingest and absent from the archive too. RSVP state is a fact
        # about the row and it already has typed homes above.
        return fields

    def describe(self, item: dict, fields: dict) -> list[str]:
        """What the *feed* said, for the line a model reads.

        Only ever a statement about disclosure. The previous version wrote "Partiful
        RSVP yes" whenever the location field was non-empty, so an unanswered invitation
        carried a false RSVP into the archive as well as onto the row — and the archive
        is the thing that is never rewritten.
        """
        if fields.get("location"):
            return ["Partiful disclosed the location, which it does once you RSVP yes"]
        return ["Partiful has not disclosed the location, which means no RSVP yet"]

    def reconcile_missing(
        self,
        conn: sqlite3.Connection,
        *,
        seen: set[str],
        seen_uids: set[str] | None = None,
        scan_start: str,
        scan_end: str,
        report,
    ) -> None:
        """Reconcile individual Partiful events missing from a complete snapshot."""
        today = db.today().isoformat()
        rows = conn.execute(
            """SELECT * FROM calendar_items
                WHERE provider = 'partiful' AND active = 1
                  AND starts_at >= ? AND starts_at < ? AND substr(last_seen_at, 1, 10) < ?
                ORDER BY starts_at""",
            (db.utc_stamp(scan_start), db.utc_stamp(scan_end), today),
        ).fetchall()
        uids = seen_uids or set()
        missing = [row for row in rows
                   if row["identity"] not in seen and str(row["event_uid"]) not in uids]
        if not missing:
            return

        # No Partiful event at all survived this snapshot. That is subscription-level
        # evidence, not one RSVP decision repeated N times.
        if not seen:
            conn.execute(
                """UPDATE calendar_items SET active = 0, updated_at = ?
                    WHERE provider = 'partiful' AND active = 1""",
                (db.now(),),
            )
            report.notes.append(
                f"all {len(missing)} tracked Partiful event(s) disappeared; "
                "treated as a Partiful calendar unsubscribe, not declines"
            )
            return

        for row in missing:
            event = events.get(conn, row["event_key"])
            if event is None:
                continue
            text = f"{event.title} — disappeared from Partiful subscription — declined"
            external_id = f"{row['identity']}:declined:{today}"
            archive_id = archive.append(
                conn,
                stream="ical",
                external_id=external_id,
                ts=db.now(),
                thread=row["calendar_uid"],
                text=text,
                meta={
                    "calendar": row["calendar_name"],
                    "calendar_origin": "subscribed",
                    "provider": "partiful",
                    "state": "disappeared",
                },
                gated=False,
                gate_reason="calendar-structured",
            )
            updated, verb = events.upsert(
                conn,
                {
                    "key": event.key,
                    "title": event.title,
                    "date": event.date,
                    "status": "declined",
                    "source": f"ical:subscribed:{row['calendar_name']}",
                },
                written_by="ical",
                match=False,
            )
            conn.execute(
                "UPDATE calendar_items SET active = 0, updated_at = ? WHERE identity = ?",
                (db.now(), row["identity"]),
            )
            if archive_id:
                report.archived += 1
                trace.stamp(
                    conn,
                    kind="event",
                    ref=updated.key,
                    verb=verb,
                    entity=f"calendar:{row['calendar_name']}",
                    stage="ical",
                    archive_ids=[archive_id],
                )
            report.notes.append(f"Partiful declined: {event.title}")


POLICY = Partiful()
