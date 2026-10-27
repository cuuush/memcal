"""Cross-conversation planning evidence survives inference, correction and replay."""
import unittest
from unittest import mock

from tests._support import Base
from memcal import actions, archive, brief, db, events, live, llm, trace
from memcal.dream import apply, bundle, merge, propose
from memcal.dream import run as dream_run


class TestOlderPlanningEvidenceEnrichesAnUntouchedGuess(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-10-20')
        self.counter = 0
        self.invite = self.line('Want to join our game on November 1?', 'dm',
                                '2026-10-20T15:00:00-04:00')
        self.group = self.line('November 1 works; we are playing at my place.', 'players',
                               '2026-10-19T12:00:00-04:00')
        self.ev = events.upsert(self.conn, {
            'title': 'Winter campaign', 'date': '2026-11-01', 'kind': 'opportunity',
            'status': 'mentioned', 'source': 'person:Visitor', 'participants': []},
            written_by='dream:ondemand', evidence_ts={
                'date': self.invite['ts'], 'status': self.invite['ts']}).event
        trace.stamp(self.conn, kind='event', ref=self.ev.key, verb='inserted',
                    entity='person:Visitor', archive_ids=[self.invite['id']])
        self.conn.commit()

    def line(self, text, thread, ts):
        self.counter += 1
        aid = archive.append(self.conn, channel='imessage', external_id=str(self.counter),
            text=text, person='Alex', thread=thread, ts=ts, gated=True, gate_reason='plan')
        return self.conn.execute('SELECT * FROM archive WHERE id=?', (aid,)).fetchone()

    def proposals(self, *, cited=True):
        entity = 'thread:imessage:players'
        archive.spool_add(self.conn, self.group['id'], entity)
        self.conn.commit()
        b = next(b for b in bundle.build(self.conn) if b.entity == entity)
        tag = f"L{next(i for i,r in enumerate(b.items,1) if r['id']==self.group['id'])}"
        row = {'key': self.ev.key, 'title': self.ev.title, 'date': self.ev.date,
               'kind': 'commitment', 'status': 'confirmed', 'participants': [],
               'cites': [tag], 'field_cites': {}}
        if cited:
            row['field_cites'] = {field: [tag] for field in ('kind','status')}
        diff = {'events': [row]}
        propose._resolve_cites(b, diff)
        return [(b, diff, '')]

    def judge(self, *, same=True, cited=True, fail=False, fields=('kind','status'), expect_saved=True):
        outer = self
        class Model:
            usage = llm.Usage()
            def complete(self, **kwargs):
                if fail:
                    raise RuntimeError('offline')
                if expect_saved:
                    outer.assertIn('Want to join our game', kwargs['suffix'])
                outer.assertIn(outer.group['text'], kwargs['suffix'])
                return llm.Reply(text='{}', model='test', data={
                    'same_event': same, 'title': outer.ev.title, 'date': outer.ev.date,
                    'kind': 'commitment', 'status': 'confirmed', 'participants': [],
                    'citations': [{'field': field, 'source_ids': [outer.group['id']]}
                                  for field in fields] if cited else [],
                })
        return Model()

    def apply(self, proposals):
        counts, log = apply.apply_diffs(self.conn, self.cfg, proposals,
                                        written_by='dream:nightly')
        return events.get(self.conn, self.ev.key), log

    def test_model_compares_both_sources_and_applies_older_plan_once(self):
        proposals, log = merge.merge_all(self.judge(), self.cfg, self.proposals(), conn=self.conn)
        self.assertTrue(any(s.startswith('merged') for s in log))
        ev, _ = self.apply(proposals)
        self.assertEqual((ev.kind,ev.status), ('commitment','confirmed'))
        histories = len(events.history(self.conn, ev.id))
        again, _ = self.apply(proposals)
        self.assertEqual(again.id, ev.id)
        self.assertEqual(len(events.history(self.conn, ev.id)), histories)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        saved = self.conn.execute("SELECT evidence_ts FROM event_history WHERE event_id=?"
                                 " AND field='status' ORDER BY id DESC", (ev.id,)).fetchone()
        self.assertEqual(saved[0], self.group['ts'])
        self.assertIn('Winter campaign', brief.render(self.conn,self.cfg))

    def test_keyless_planning_proposal_gets_the_same_source_comparison(self):
        proposals = self.proposals()
        proposals[0][1]['events'][0].pop('key')
        proposals,_=merge.merge_all(self.judge(),self.cfg,proposals,conn=self.conn)
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('commitment','confirmed'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_replaying_the_later_invitation_cannot_erase_the_reconciled_commitment(self):
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        self.apply(proposals)
        b = bundle.Bundle(entity='person:Visitor',items=[self.invite])
        diff = {'events':[{'key':self.ev.key,'title':self.ev.title,'date':self.ev.date,
                          'kind':'opportunity','status':'mentioned','cites':['L1'],
                          'field_cites':{'kind':['L1'],'status':['L1']}}]}
        propose._resolve_cites(b,diff)
        self.apply([(b,diff,'')])
        ev=events.get(self.conn,self.ev.key)
        self.assertEqual((ev.kind,ev.status),('commitment','confirmed'))

    def test_raw_older_proposal_cannot_bypass_comparison_and_leaves_cited_review(self):
        proposals = self.proposals()
        ev, log = self.apply(proposals)
        self.assertEqual(ev.status,'mentioned')
        self.assertTrue(any('older or uncited evidence' in line for line in log))
        question = self.conn.execute('SELECT * FROM questions WHERE about_event=?', (ev.id,)).fetchone()
        self.assertIsNotNone(question)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evidence WHERE kind='question'"
            " AND ref=? AND archive_id=?", (question['key'], self.group['id'])).fetchone()[0],1)
        self.apply(proposals)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM questions').fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM provenance WHERE verb='held'").fetchone()[0],1)

    def test_failed_or_unresolved_comparison_preserves_evidence_without_promoting(self):
        for model in (self.judge(fail=True), self.judge(same='unresolved')):
            proposals, _ = merge.merge_all(model,self.cfg,self.proposals(),conn=self.conn)
            ev, log = self.apply(proposals)
            self.assertEqual(ev.status,'mentioned')
            self.assertTrue(any('held' in line for line in log))

    def test_no_citation_cannot_gain_authority_from_a_comparison(self):
        proposals,_=merge.merge_all(self.judge(cited=False),self.cfg,
                                    self.proposals(cited=False),conn=self.conn)
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('opportunity','mentioned'))

    def test_only_one_supported_field_cannot_half_promote_a_guess(self):
        proposals,_=merge.merge_all(self.judge(fields=('status',)),self.cfg,
                                    self.proposals(),conn=self.conn)
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('opportunity','mentioned'))

    def test_comparison_cannot_promote_a_different_date_under_the_old_key(self):
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        proposals[0][1]['events'][0]['date']='2026-11-02'
        ev,_=self.apply(proposals)
        self.assertEqual((ev.date,ev.kind,ev.status),('2026-11-01','opportunity','mentioned'))

    def test_missing_stored_source_evidence_cannot_grant_reconsideration(self):
        self.conn.execute('DELETE FROM evidence WHERE ref=?',(self.ev.key,))
        proposals,_=merge.merge_all(self.judge(expect_saved=False),self.cfg,self.proposals(),conn=self.conn)
        self.assertNotIn('_reconsider',proposals[0][1]['events'][0])
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('opportunity','mentioned'))

    def test_typed_correction_during_comparison_cannot_be_undone(self):
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        live.update_event(self.conn,self.cfg,f'E{self.ev.id}',status='declined',
                          origin=actions.Origin.of('user'))
        ev,_=self.apply(proposals)
        self.assertEqual(ev.status,'declined')
        self.assertEqual(ev.id,self.ev.id)

    def test_later_dream_correction_even_same_values_prevents_reconsideration(self):
        # A reaffirmed status is a real later assessment, not the founding guess.
        events.upsert(self.conn,{'key':self.ev.key,'date':self.ev.date,'status':'mentioned'},
                      written_by='dream:nightly',evidence_ts={
                          'status':'2026-10-20T16:00:00-04:00'})
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('opportunity','mentioned'))

    def test_refused_same_event_decision_never_grants_reconsideration(self):
        proposals,_=merge.merge_all(self.judge(same=False),self.cfg,self.proposals(),conn=self.conn)
        self.assertNotIn('_reconsider',proposals[0][1]['events'][0])
        ev,_=self.apply(proposals)
        self.assertEqual(ev.status,'mentioned')
        separate = events.between(self.conn,self.ev.date,self.ev.date)
        self.assertEqual(len(separate),1)
        self.apply(proposals)
        self.assertEqual(len(events.between(self.conn,self.ev.date,self.ev.date)),1)

    def test_even_newer_evidence_cannot_bypass_a_refused_association(self):
        self.group = self.line('A separate session on November 1.', 'players',
                               '2026-10-20T17:00:00-04:00')
        proposals,_=merge.merge_all(self.judge(same=False),self.cfg,self.proposals(),conn=self.conn)
        ev,log=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('opportunity','mentioned'))
        self.assertTrue(any('source association needs review' in line for line in log))

    def test_dream_change_in_flight_invalidates_the_comparison_snapshot(self):
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        events.upsert(self.conn,{'key':self.ev.key,'date':self.ev.date,'note':'Later evidence'},
                      written_by='dream:nightly',evidence_ts={'note':'2026-10-20T17:00:00-04:00'})
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('opportunity','mentioned'))
        self.assertEqual(ev.note,'Later evidence')

    def test_entire_dream_marks_source_read_only_after_reconciliation_and_replays_safely(self):
        proposals = self.proposals()
        self.cfg.dream_naming = False
        with mock.patch.object(llm, 'client_for', return_value=self.judge()), \
             mock.patch.object(propose, 'propose_all', return_value=(proposals,[],[])):
            result = dream_run.dream(self.conn,self.cfg,skip_sweep=True)
        self.assertFalse(result.errors)
        self.assertEqual(events.get(self.conn,self.ev.key).status,'confirmed')
        queued = self.conn.execute('SELECT * FROM spool WHERE archive_id=?',
                                  (self.group['id'],)).fetchone()
        self.assertEqual(queued['run_id'],result.run_id)
        self.assertIsNotNone(queued['processed_at'])
        history = len(events.history(self.conn,self.ev.id))
        with mock.patch.object(llm, 'client_for', side_effect=AssertionError('nothing left to read')):
            dream_run.dream(self.conn,self.cfg,skip_sweep=True)
        self.assertEqual(len(events.history(self.conn,self.ev.id)),history)
        self.assertIn('Winter campaign',self.cfg.brief_path.read_text())

    def test_a_successful_comparison_retires_its_previous_review_question(self):
        self.apply(self.proposals())
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        ev,_=self.apply(proposals)
        self.assertEqual(ev.status,'confirmed')
        self.assertEqual(self.conn.execute('SELECT status FROM questions WHERE about_event=?',
                                          (ev.id,)).fetchone()[0],'dropped')

    def test_newer_cited_evidence_still_updates_a_previously_corrected_plan(self):
        events.upsert(self.conn,{'key':self.ev.key,'date':self.ev.date,'status':'mentioned'},
                      written_by='dream:nightly',evidence_ts={'status':'2026-10-20T16:00:00-04:00'})
        self.group=self.line('We confirmed November 1.', 'players','2026-10-20T17:00:00-04:00')
        proposals,_=merge.merge_all(self.judge(),self.cfg,self.proposals(),conn=self.conn)
        ev,_=self.apply(proposals)
        self.assertEqual((ev.kind,ev.status),('commitment','confirmed'))


if __name__ == '__main__':
    unittest.main()
