"""Partiful policy layered on the generic iCalendar transport."""

from __future__ import annotations

import re
import sqlite3

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
