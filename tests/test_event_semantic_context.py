"""Regressions at the extraction -> merge -> stored event boundary."""

import unittest

from _support import Base
from memcal import archive, brief, db, detail, events, identity, llm, threads
from memcal.dream import apply, bundle, merge, propose


class EventContextBase(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-10-20')
        identity.set_me(self.conn, 'Christopher Owner')
        identity.link(self.conn, '+444', 'Nick')

    def line(self, text, *, person='Nick', thread='dm', stamp='2026-10-20T14:00:00-04:00', **kwargs):
        aid = archive.append(self.conn, channel='imessage', external_id=str(
            self.conn.execute('SELECT count(*) FROM archive').fetchone()[0]),
            ts=stamp, text=text, person=person, thread=thread, **kwargs)
        return self.conn.execute('SELECT * FROM archive WHERE id=?', (aid,)).fetchone()

    def save(self, rows, event):
        b = bundle.Bundle(entity='person:Nick', items=rows)
        event = {'title': 'Therapy appointment', 'date': '2026-10-21',
                 'kind': 'commitment', 'status': 'tentative', 'participants': [],
                 'cites': ['L1'], **event}
        diff = {'events': [event]}
        propose._resolve_cites(b, diff)
        apply.apply_diffs(self.conn, self.cfg, [(b, diff, '')], written_by='dream:test')
        return events.between(self.conn, '2026-10-21', '2026-10-21')[0]


class TestStructuredAttendance(EventContextBase):
    def test_inviting_nick_preserves_mom_and_conditional_reply_is_not_attendance(self):
        invitation = self.line('Do you want to come to therapy with me and Mom?', person='me', from_me=True)
        reply = self.line('I might come if my exam schedule allows it.')
        ev = self.save([invitation, reply], {'participants': ['Nick'], 'attendance': [
            {'name': 'Mom', 'role': 'attending', 'cites': ['L1']},
            {'name': 'Nick', 'role': 'conditional', 'cites': ['L2']}]})
        self.assertEqual(ev.participants, ['Mom'])
        self.assertIn('Nick: attendance conditional', ev.note)
        opened = detail.open_handle(self.conn, self.cfg, f'E{ev.id}')
        self.assertIn(reply['text'], opened)

    def test_an_invitation_patch_keeps_other_known_guests(self):
        ev, _ = events.upsert(self.conn, {'title': 'Therapy appointment', 'date': '2026-10-21',
            'participants': ['Mom', 'Nick']}, evidence_ts='2026-10-19T12:00:00-04:00')
        msg = self.line('Nick has only been invited, not accepted.')
        saved = self.save([msg], {'key': ev.key, 'participants': ['Nick'], 'attendance': [
            {'name': 'Nick', 'role': 'invited', 'cites': ['L1']}]})
        self.assertEqual(saved.participants, ['Mom'])
        self.assertIn('attendance unconfirmed', saved.note)

    def test_role_patch_also_finds_exact_existing_event_when_model_omits_key(self):
        ev, _ = events.upsert(self.conn, {'title': 'Therapy appointment', 'date': '2026-10-21',
            'participants': ['Mom', 'Nick']}, evidence_ts='2026-10-19T12:00:00-04:00')
        saved = self.save([self.line('I cannot attend.')], {'attendance': [
            {'name': 'Nick', 'role': 'declined', 'cites': ['L1']}]})
        self.assertEqual(saved.key, ev.key)
        self.assertEqual(saved.participants, ['Mom'])
        self.assertIn('Nick: not attending', saved.note)

    def test_an_old_conditional_reply_cannot_remove_a_later_confirmed_guest(self):
        ev, _ = events.upsert(self.conn, {'title': 'Therapy appointment', 'date': '2026-10-21',
            'participants': ['Mom', 'Nick']}, evidence_ts='2026-10-20T16:00:00-04:00')
        msg = self.line('I might come.')
        saved = self.save([msg], {'key': ev.key, 'attendance': [
            {'name': 'Nick', 'role': 'conditional', 'cites': ['L1']}]})
        self.assertEqual(saved.participants, ['Mom', 'Nick'])
        self.assertNotIn('attendance conditional', saved.note or '')

    def test_a_later_acceptance_replaces_the_generated_conditional_note(self):
        ev = self.save([self.line('I might come.')], {'attendance': [
            {'name': 'Nick', 'role': 'conditional', 'cites': ['L1']}]})
        saved = self.save([self.line('I am coming.', stamp='2026-10-20T15:00:00-04:00')], {
            'key': ev.key, 'attendance': [{'name': 'Nick', 'role': 'attending', 'cites': ['L1']}]})
        self.assertEqual(saved.participants, ['Nick'])
        self.assertNotIn('attendance conditional', saved.note)
        self.assertIn('participation confirmed', saved.note)

    def test_performers_are_saved_as_hosts_and_not_companions(self):
        msg = self.line('The advertised lineup features DJ Example.')
        ev = self.save([msg], {'title': 'Club night', 'participants': ['DJ Example'],
            'kind': 'opportunity', 'attendance': [
                {'name': 'DJ Example', 'role': 'host', 'cites': ['L1']}]})
        self.assertEqual(ev.participants, [])
        self.assertEqual(ev.hosts, ['DJ Example'])
        self.assertIn('DJ Example', detail.open_handle(self.conn, self.cfg, f'E{ev.id}'))

    def test_uncited_roles_cannot_manufacture_companions(self):
        ev = self.save([self.line('See you tomorrow.')], {'participants': ['Imaginary person'],
            'attendance': [{'name': 'Imaginary person', 'role': 'attending', 'cites': ['L99']}]})
        self.assertEqual(ev.participants, [])

    def test_later_acceptance_wins_over_earlier_conditional_role(self):
        ev = self.save([self.line('I might come.'), self.line('Yes, I am coming.',
            stamp='2026-10-20T15:00:00-04:00')], {'attendance': [
            {'name': 'Nick', 'role': 'conditional', 'cites': ['L1']},
            {'name': 'Nick', 'role': 'attending', 'cites': ['L2']}]})
        self.assertEqual(ev.participants, ['Nick'])


class TestReadableEventWording(EventContextBase):
    def test_visit_to_user_uses_visitor_subject_and_preserves_tentative_status(self):
        ev = self.save([self.line('I can come over if Thursday works for you.')], {
            'title': 'Visit Christopher', 'subject': 'me', 'visit_target': 'me',
            'field_cites': {'title': ['L1'], 'subject': ['L1']}})
        self.assertEqual(ev.title, 'Thinking of coming over')
        self.assertEqual(ev.subject, 'Nick')
        self.assertEqual(ev.status, 'tentative')
        self.assertIn('Nick', ev.one_line())

    def test_visit_to_namesake_is_not_rewritten(self):
        ev = self.save([self.line('Visit Christopher Other tomorrow.')], {
            'title': 'Visit Christopher Other', 'visit_target': 'Christopher Other'})
        self.assertEqual(ev.title, 'Visit Christopher Other')

    def test_verified_full_wording_expands_stored_title_and_location(self):
        ev = self.save([self.line('Dog Run 142 is holding its Halloween event.')], {
            'title': 'DR142 Halloween', 'location': 'DR142', 'expansions': [
                {'short': 'DR142', 'expanded': 'Dog Run 142', 'cites': ['L1']}]})
        self.assertEqual(ev.title, 'Dog Run 142 Halloween')
        self.assertEqual(ev.location, 'Dog Run 142')
        self.assertIn('Dog Run 142 Halloween', brief.render(self.conn, self.cfg))

    def test_an_invented_expansion_stays_explicitly_unresolved(self):
        ev = self.save([self.line('DR142 Halloween tomorrow.')], {
            'title': 'DR142 Halloween', 'expansions': [
                {'short': 'DR142', 'expanded': 'Dog Run 142', 'cites': ['L1']}]})
        self.assertEqual(ev.title, 'DR142 Halloween')
        self.assertIn('full meaning is not established', ev.note)

    def test_schema_can_represent_roles_expansions_and_visit_recipient(self):
        for field in ('attendance', 'expansions', 'visit_target'):
            self.assertIn(field, propose.EVENT_DIFF_V2['required'])
            self.assertIn(field, propose.EVENT_DIFF_V2['properties'])


class TestRelatedGroupContext(EventContextBase):
    def make_group(self, thread='players', *, muted=False, text='Our dungeons and dragons campaign'):
        threads.record(self.conn, 'imessage', thread, label='Game group', is_group=True,
                       participants=['+111', '+222', '+333'])
        identity.link(self.conn, '+111', 'Quentin')
        identity.link(self.conn, '+222', 'Katie')
        if muted:
            self.conn.execute("UPDATE threads SET decision='mute' WHERE thread=?", (thread,))
        topic = self.line(text, thread=thread, person='Quentin', stamp='2026-10-19T14:00:00-04:00')
        reply = self.line('November 1 works for me.', thread=thread, person='Katie')
        return topic, reply

    def test_dm_restores_retained_group_topic_replies_roster_and_citable_ids(self):
        topic, reply = self.make_group()
        dm = self.line('Want to join our dungeons and dragons campaign?', person='me',
                       from_me=True, gated=True, gate_reason='invitation')
        archive.spool_add(self.conn, dm['id'], 'person:Nick')
        self.conn.commit()
        b = bundle.build(self.conn)[0]
        self.assertIn(topic['id'], b.context_ids)
        self.assertIn(reply['id'], b.context_ids)
        self.assertEqual(len(b.spool_ids), 1)
        block = propose.build_bundle_block(self.cfg, b, self.conn)
        self.assertIn('Katie', block)
        self.assertIn('Quentin', block)
        self.assertIn('1 unresolved member', block)
        self.assertIn('membership is not attendance', block)
        self.assertIn('Game group', block)
        index = next(i for i, r in enumerate(b.items, 1) if r['id'] == reply['id'])
        self.assertEqual(b.cite([f'L{index}']), [reply['id']])

    def test_muted_group_is_not_restored(self):
        topic, _ = self.make_group(muted=True)
        dm = self.line('Our dungeons and dragons campaign tomorrow', gated=True, gate_reason='temporal')
        b = bundle.Bundle(entity='person:Nick', items=[dm])
        bundle.add_related_group_context(self.conn, b)
        self.assertNotIn(topic['id'], b.context_ids)

    def test_shared_person_or_vague_date_alone_does_not_nominate_a_group(self):
        topic, _ = self.make_group(text='A different outing with Nick')
        dm = self.line('Dinner tomorrow with Nick', gated=True, gate_reason='temporal')
        b = bundle.Bundle(entity='person:Nick', items=[dm])
        bundle.add_related_group_context(self.conn, b)
        self.assertNotIn(topic['id'], b.context_ids)


class TestRoleEvidenceSurvivesMerge(EventContextBase):
    def proposals(self, *, conflict=False):
        a = self.line('I might come to therapy.', person='Nick')
        b = self.line('Mom is coming; DJ Example hosts the clinic fundraiser.', person='Mom', thread='mom')
        out = []
        for source, lines, roles, time in [
            ('Nick', [a], [{'name': 'Nick', 'role': 'conditional', 'cites': ['L1']}], '16:00'),
            ('Mom', [b], [{'name': 'Mom', 'role': 'attending', 'cites': ['L1']},
                        {'name': 'DJ Example', 'role': 'host', 'cites': ['L1']}],
             '17:00' if conflict else '16:00')]:
            bdl = bundle.Bundle(entity='person:' + source, items=lines)
            diff = {'events': [{'title': 'Therapy appointment', 'date': '2026-10-21',
                'time': time, 'status': 'tentative', 'participants': ['Nick', 'DJ Example'],
                'attendance': roles, 'cites': ['L1']}]}
            propose._resolve_cites(bdl, diff)
            out.append((bdl, diff, ''))
        self.conn.commit()
        return out

    def verify(self, proposals):
        apply.apply_diffs(self.conn, self.cfg, proposals, written_by='dream:test')
        ev = events.between(self.conn, '2026-10-21', '2026-10-21')[0]
        self.assertEqual(ev.participants, ['Mom'])
        self.assertEqual(ev.hosts, ['DJ Example'])
        self.assertIn('attendance conditional', ev.note)

    def test_local_merge_keeps_roles_and_other_bundles_citations(self):
        class NoModel:
            def complete(self, **kwargs):
                raise AssertionError('agreeing event fragments should merge locally')
        proposals, log = merge.merge_all(NoModel(), self.cfg, self.proposals(), conn=self.conn)
        self.assertTrue(log)
        self.verify(proposals)

    def test_model_merge_cannot_turn_conditional_guest_or_performer_into_companion(self):
        class Model:
            def complete(self, **kwargs):
                return llm.Reply(text='{}', data={'same_event': True, 'date': '2026-10-21',
                    'title': 'Therapy appointment', 'time': '17:00',
                    'participants': ['Nick', 'DJ Example'], 'status': 'tentative'}, model='test')
        proposals, log = merge.merge_all(Model(), self.cfg, self.proposals(conflict=True), conn=self.conn)
        self.assertTrue(any(s.startswith('merged') for s in log))
        self.verify(proposals)

if __name__ == '__main__':
    unittest.main()
