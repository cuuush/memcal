"""Restored context must not become another plan's source or conferencing link."""

import unittest

from tests._support import Base
from memcal import archive, db, events, llm, threads, todos
from memcal.dream import apply, bundle, merge, propose


class TestRestoredContextCannotCreateUnrelatedPlans(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-04T12:00:00-05:00')

    def line(self, text, *, channel='imessage', thread='players', person='Aster'):
        aid = archive.append(
            self.conn, channel=channel, external_id=str(
                self.conn.execute('SELECT count(*) FROM archive').fetchone()[0]),
            ts=db.now(), thread=thread, person=person, text=text, gated=True)
        return self.conn.execute('SELECT * FROM archive WHERE id=?', (aid,)).fetchone()

    def test_newsletter_cannot_retrieve_group_chat_context(self):
        threads.record(self.conn, 'imessage', 'players', is_group=True)
        game = self.line('Update your profile; play tomorrow.')
        email = self.line('Update your profile for new jobs tomorrow.', channel='email',
                          thread='jobs', person='Job Digest')
        source = bundle.Bundle(entity='person:Job Digest', items=[email])
        bundle.add_related_group_context(self.conn, source)
        self.assertEqual([r['id'] for r in source.items], [email['id']])
        self.assertNotIn(game['id'], source.context_ids)

    def test_dm_cannot_retrieve_multi_sender_email_as_a_group_chat(self):
        threads.record(self.conn, 'email', 'meeting', is_group=True)
        email = self.line('Workshop planning tomorrow.', channel='email', thread='meeting')
        dm = self.line('Workshop planning tomorrow.', thread='dm')
        source = bundle.Bundle(entity='person:Aster', items=[dm])
        bundle.add_related_group_context(self.conn, source)
        self.assertNotIn(email['id'], source.context_ids)

    def test_context_only_plan_waits_for_its_own_source_and_replay_keeps_one_identity(self):
        email = self.line('Job listings', channel='email', thread='jobs', person='Job Digest')
        game = self.line('Play games tomorrow?')
        polluted = bundle.Bundle(entity='person:Job Digest', items=[email, game],
                                 context_ids={game['id']})
        diff = {'events': [{'title': 'Play games', 'date': '2026-11-05', 'cites': ['L2']}],
                'questions': [{'action': 'ask', 'text': 'When are you playing games?',
                               'cites': ['L2']}]}
        propose._resolve_cites(polluted, diff)
        counts, _ = apply.apply_diffs(self.conn, self.cfg, [(polluted, diff)],
                                      written_by='dream:test')
        self.assertEqual(counts['event:rejected-context-only'], 1)
        self.assertEqual(counts['question:rejected-context-only'], 1)
        self.assertEqual(events.window(self.conn, 0, 10), [])
        self.assertEqual(todos.open_questions(self.conn), [])

        source = bundle.Bundle(entity='thread:imessage:players', items=[game])
        diff = {'events': [{'title': 'Play games', 'date': '2026-11-05', 'cites': ['L1']}]}
        propose._resolve_cites(source, diff)
        for _ in range(2):
            apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        saved = events.window(self.conn, 0, 10)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].source, source.entity)
        self.assertEqual({r[0] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event'")}, {game['id']})

    def test_cited_delivery_cannot_inherit_uncited_meeting_url(self):
        meeting = self.line('Join https://zoom.us/j/123456789', thread='meeting')
        delivery = self.line('Parcel delivery November 6.', channel='email', thread='parcel')
        source = bundle.Bundle(entity='person:Parcel Digest', items=[meeting, delivery],
                               context_ids={meeting['id']})
        diff = {'events': [{'title': 'Parcel delivery', 'date': '2026-11-06',
                            'cites': ['L2']}]}
        propose._resolve_cites(source, diff)
        apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        saved = events.window(self.conn, 0, 10)[0]
        self.assertIsNone(saved.join_url)
        self.assertIsNone(apply._join_link(source, []))
        self.assertEqual(apply._join_link(source, [meeting['id']]),
                         'https://zoom.us/j/123456789')

    def test_merged_citations_can_still_supply_the_actual_meeting_link(self):
        meeting = self.line('Join https://zoom.us/j/123456789', thread='meeting')
        other = self.line('Parcel delivery November 6.', thread='parcel')
        source = bundle.Bundle(entity='person:Aster', items=[other])
        self.assertEqual(apply._join_link(source, [meeting['id']], conn=self.conn),
                         'https://zoom.us/j/123456789')


class TestExcursionDepartureIsNotTheStayEnd(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-04T12:00:00-05:00')
        self.trip = events.upsert(self.conn, {
            'title': 'Lakeside vacation', 'date': '2026-11-05', 'until': '2026-11-08',
            'status': 'confirmed'}, written_by='cli').event

    def proposal(self, *, role='intermediate_departure', until='2026-11-06', cites=None):
        aid = archive.append(self.conn, channel='imessage', external_id=role,
            ts='2026-11-04T20:00:00-05:00', person='Aster', thread='family',
            text=('We check out Friday; that is the end of our vacation.' if role == 'event_end'
                  else 'We leave early Friday for our day excursion during the vacation.'))
        source = bundle.Bundle(entity='person:Aster', items=[self.conn.execute(
            'SELECT * FROM archive WHERE id=?', (aid,)).fetchone()])
        row = {'key': self.trip.key, 'title': self.trip.title, 'date': self.trip.date,
               'until': until, 'end_boundary': {'role': role,
                    'cites': ['L1'] if cites is None else cites},
               'cites': ['L1'], 'field_cites': {'until': ['L1']}}
        diff = {'events': [row]}
        propose._resolve_cites(source, diff)
        return source, diff

    def test_departure_cannot_shorten_existing_weekend_before_merge_or_on_replay(self):
        source, diff = self.proposal()
        self.assertNotIn('until', diff['events'][0])
        for _ in range(2):
            apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        self.assertEqual(events.get(self.conn, self.trip.key).until, '2026-11-08')

    def test_unknown_end_or_uncited_end_cannot_shorten_the_stay(self):
        for role, cites in [('unknown', ['L1']), ('event_end', ['L99'])]:
            source, diff = self.proposal(role=role, cites=cites)
            self.assertNotIn('until', diff['events'][0])
            apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        self.assertEqual(events.get(self.conn, self.trip.key).until, '2026-11-08')

    def test_a_cited_explicit_end_remains_actionable(self):
        source, diff = self.proposal(role='event_end')
        self.assertEqual(diff['events'][0]['until'], '2026-11-06')
        self.assertEqual(diff['events'][0]['field_cite_ids']['until'], [source.items[0]['id']])
        apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        self.assertEqual(events.get(self.conn, self.trip.key).until, '2026-11-06')
        self.assertIn('end_boundary', propose.EVENT_DIFF_V2['required'])

    def test_overlapping_personal_calendar_stay_is_shown_even_without_shared_title(self):
        trip = events.upsert(self.conn, {'title': 'Copper Ridge', 'date': self.trip.date,
            'until': self.trip.until, 'source': 'ical:created:Home', 'status': 'confirmed'},
            match=False).event
        source, _ = self.proposal()
        block = propose.build_open_rows(self.conn, source)
        self.assertIn(trip.key, block)
        self.assertIn('until 2026-11-08', block)
        self.assertIn('does not change its end', block)

    def test_merge_cannot_turn_an_internal_departure_back_into_an_end_date(self):
        source, diff = self.proposal()
        first = diff['events'][0]
        first['time'] = '18:00'
        other = bundle.Bundle(entity='person:Bram', items=source.items)
        second = {**first, 'time': '19:00'}
        aid = source.items[0]['id']

        class Model:
            usage = llm.Usage()
            calls = 0

            def complete(self, **kwargs):
                self.calls += 1
                return llm.Reply(text='{}', model='test', data={
                    'same_event': True, 'title': first['title'], 'date': first['date'],
                    'until': '2026-11-06', 'time': '18:00', 'participants': [],
                    'end_boundary': {'role': 'intermediate_departure', 'source_ids': [aid]},
                    'citations': [{'field': 'until', 'source_ids': [aid]}]})

        model = Model()
        proposals, _ = merge.merge_all(model, self.cfg, [
            (source, diff, ''), (other, {'events': [second]}, '')])
        self.assertEqual(model.calls, 1)
        merged = next(row for _, change, _ in proposals for row in change['events'])
        self.assertNotIn('until', merged)
        apply.apply_diffs(self.conn, self.cfg, proposals, written_by='dream:test')
        self.assertEqual(events.get(self.conn, self.trip.key).until, '2026-11-08')


if __name__ == '__main__':
    unittest.main()
