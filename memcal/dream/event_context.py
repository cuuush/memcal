"""Apply evidence-backed event roles and wording before storage arbitration.

The reader interprets language; code decides which roles become companions, keeps
invitations separate, and refuses abbreviation expansions without retained proof.
"""

from __future__ import annotations

import re
import sqlite3

from .. import db, events, identity
from .bundle import Bundle


def _words(text: str) -> str:
    return ' '.join(re.findall(r'\w+', str(text).casefold()))


def normalize(conn: sqlite3.Connection, bundle: Bundle, row: dict) -> dict:
    if not isinstance(row, dict):
        return row
    row = dict(row)
    lines = {int(r['id']): r for r in bundle.items}
    # Merge may put another bundle's citations on the keeper's row. Load only the
    # validated source ids carried by Merge, never a whole source conversation.
    other = [int(i) for i in (row.get('_evidence_times') or {}) if int(i) not in lines]
    if other:
        marks = ','.join('?' for _ in other)
        lines.update({int(r['id']): r for r in conn.execute(
            f'SELECT * FROM archive WHERE id IN ({marks})', other)})
    fields = dict(row.get('field_cite_ids') or {})
    cited = list(row.get('cite_ids') or [])

    def support(field, ids):
        fields[field] = sorted(set(fields.get(field, [])) | set(ids))
        cited.extend(i for i in ids if i not in cited)

    def add_note(text, ids):
        note = str(row.get('note') or '').strip()
        if text.casefold() not in note.casefold():
            row['note'] = (note + ' ' + text).strip()
        support('note', ids)

    roles = row.get('attendance')
    if isinstance(roles, list):
        existing = events.get(conn, str(row.get('key') or ''))
        if existing is None and row.get('date') and row.get('title'):
            try:
                matched = events.find_match(conn, title=str(row['title']), on=str(row['date']),
                    subject=row.get('subject') or 'me', time=row.get('time'),
                    location=row.get('location'), participants=row.get('participants') or [])
            except ValueError:
                matched = None  # Apply rejects malformed dates through its usual path.
            # Resolve only an exact same-day identity here. Other matching and date
            # moves remain with the normal storage matcher.
            if (matched and matched.date == row['date']
                    and matched.title.casefold() == str(row['title']).casefold()):
                existing = matched
                row['key'] = existing.key
        people = set(existing.participants if existing else [])
        hosts = set(existing.hosts if existing else [])
        versions, day_fields = events._field_versions(conn, existing.id) if existing else ({}, set())
        participant_floor = versions.get('participants')
        if existing and not participant_floor and '__born__' not in versions:
            participant_floor = conn.execute('SELECT evidence_ts FROM events WHERE id=?',
                                             (existing.id,)).fetchone()['evidence_ts']
        assessments = {}
        for entry in roles:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name') or '').strip()
            role = entry.get('role')
            ids = [i for i in entry.get('cite_ids', []) if i in lines]
            if not name or not ids or role not in ('attending', 'invited', 'conditional', 'host', 'declined'):
                continue
            if identity.is_me(conn, name):
                continue
            # The latest stated assessment wins. On a tie, uncertainty wins over an
            # assertion of attendance; duplicate contradictory output cannot confirm it.
            rank = (max(db.parse_ts(str(lines[i]['ts']), strict=True) for i in ids),
                    ('attending', 'host', 'invited', 'conditional', 'declined').index(role))
            previous = assessments.get(name.casefold())
            if previous is None or rank > previous[0]:
                assessments[name.casefold()] = (rank, name, role, ids)
        all_ids = []
        for rank, name, role, ids in assessments.values():
            floor = participant_floor
            if (floor and 'participants' not in day_fields
                    and rank[0] < db.parse_ts(floor, strict=True)
                    and any(p.casefold() == name.casefold() for p in people)):
                continue
            old_descriptions = [f'{name}: attendance conditional.',
                                f'{name}: invited; attendance unconfirmed.',
                                f'{name}: not attending.']
            note = str(row.get('note') or (existing.note if existing else '') or '')
            had_description = any(s.casefold() in note.casefold() for s in old_descriptions)
            if had_description:
                for description in old_descriptions:
                    note = re.sub(re.escape(description), '', note, flags=re.I).strip()
                row['note'] = note
            people = {p for p in people if p.casefold() != name.casefold()}
            if role == 'attending':
                people.add(name)
                if had_description:
                    add_note(f'{name}: participation confirmed.', ids)
            elif role == 'host':
                hosts.add(name)
                support('hosts', ids)
                if had_description:
                    add_note(f'{name}: host/performer.', ids)
            else:
                description = {'conditional': 'attendance conditional',
                               'invited': 'invited; attendance unconfirmed',
                               'declined': 'not attending'}[role]
                add_note(f'{name}: {description}.', ids)
            all_ids.extend(ids)
        # Empty or uncited assessments cannot erase known companions. A role patch
        # changes only the people it names, so inviting somebody cannot replace Mom
        # or an established group's guests with that invitation's recipient.
        row['participants'] = sorted(people)
        if assessments:
            # The flat legacy roster cannot lend a fresher line's authority to an
            # older role assessment. Roles own this field's evidence completely.
            fields['participants'] = sorted(set(all_ids))
            cited.extend(i for i in all_ids if i not in cited)
        if hosts:
            row['hosts'] = sorted(hosts)

    for expansion in row.get('expansions') or []:
        if not isinstance(expansion, dict):
            continue
        short = str(expansion.get('short') or '').strip()
        full = str(expansion.get('expanded') or '').strip()
        ids = [i for i in expansion.get('cite_ids', []) if i in lines]
        if not _words(short) or not _words(full) or not ids or _words(short) == _words(full):
            continue
        pattern = re.compile(r'(?<!\w)' + re.escape(short) + r'(?!\w)', re.I)
        used = [field for field in ('title', 'location', 'note')
                if pattern.search(str(row.get(field) or ''))]
        if not used:
            continue
        if not any(f' {_words(full)} ' in f" {_words(lines[i]['text'])} " for i in ids):
            add_note(f'{short}: full meaning is not established in cited evidence.', ids)
            continue
        for field in used:
            row[field] = pattern.sub(lambda _match: full, row[field])
            support(field, ids)

    if row.get('visit_target') == 'me' and re.search(
            r'\bvisit(?:ing)?\b|\bcom(?:e|ing) over\b|\bstop(?:ping)? by\b',
            str(row.get('title') or ''), re.I):
        # The target is an explicit semantic field, not a guess that any Christopher
        # in a title is the user. Namesakes and visits to other people stay intact.
        row['title'] = ('Thinking of coming over' if (row.get('status') or 'mentioned') in
                        ('tentative', 'mentioned') else 'Coming over')
        if not row.get('subject') or identity.is_me(conn, row['subject']):
            visitor = row.get('_visit_subject') or (
                bundle.entity.removeprefix('person:') if bundle.entity.startswith('person:') else '')
            if visitor and not identity.is_me(conn, visitor):
                row['subject'] = visitor
                support('subject', fields.get('title', []))

    if 'field_cite_ids' in row or fields:
        row['field_cite_ids'] = fields
    row['cite_ids'] = cited
    return row
