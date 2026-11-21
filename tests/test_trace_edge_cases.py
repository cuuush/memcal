"""Source-supported partial changes and independently scheduled occasions."""
import unittest
from tests._support import Base
from tests.test_merge import FakeClient
from memcal import archive, db, events, live
from memcal.dream import apply as apply_stage, merge
from memcal.dream.bundle import Bundle

class SourceFixture(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-09-10T21:00:00')

    def bundle(self, text, external='correction', ts='2026-09-10T12:00:00'):
        archive.append(self.conn, channel='imessage', external_id=external,
            ts=ts, thread='guest', handle='aster@example.test', person='Aster', text=text)
        item=self.conn.execute('SELECT * FROM archive WHERE external_id=?',(external,)).fetchone()
        return Bundle(entity='person:Aster',items=[item]), item['id']


class TestPartialKeyedCorrection(SourceFixture):
    def test_keyed_end_correction_preserves_unmentioned_fields_and_replays(self):
        event=events.upsert(self.conn,dict(title='Guest stay',date='2026-09-09',until='2026-09-14',
            time='16:00',subject='Aster',participants=['Aster'],kind='commitment',status='confirmed'),
            written_by='live', evidence_ts='2026-09-08T10:00:00').event
        bundle,aid=self.bundle('My visit ends September 12 at 11:00 instead.')
        row=dict(key=event.key,until='2026-09-12',note='Leaves at 11:00',
            end_boundary={'role':'event_end','cite_ids':[aid]},
            field_cite_ids={'until':[aid],'note':[aid]},cite_ids=[aid])
        for _ in range(2):apply_stage.apply_diffs(self.conn,self.cfg,[(bundle,{'events':[row]},'stub')],written_by='dream:nightly')
        saved=events.get(self.conn,event.key)
        self.assertEqual((saved.until,saved.note),('2026-09-12','Leaves at 11:00'))
        self.assertEqual((saved.date,saved.time,saved.subject,saved.participants,saved.status),
                         ('2026-09-09','16:00','Aster',['Aster'],'confirmed'))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM event_history WHERE event_id=? AND field='until' AND new_value='2026-09-12'",(event.id,)).fetchone()[0],1)

    def test_note_only_update_does_not_reaffirm_stored_date(self):
        event=events.upsert(self.conn,dict(title='Workshop',date='2026-09-12',status='confirmed'),
            written_by='live',evidence_ts='2026-09-08T10:00:00').event
        bundle,aid=self.bundle('Bring an apron.')
        before=self.conn.execute("SELECT count(*) FROM event_history WHERE event_id=? AND field='date'",(event.id,)).fetchone()[0]
        apply_stage.apply_diffs(self.conn,self.cfg,[(bundle,{'events':[dict(key=event.key,note='Bring an apron',cite_ids=[aid])]},'stub')],written_by='dream:nightly')
        after=self.conn.execute("SELECT count(*) FROM event_history WHERE event_id=? AND field='date'",(event.id,)).fetchone()[0]
        self.assertEqual(before,after)
        self.assertEqual(events.get(self.conn,event.key).note,"Bring an apron")
        events.upsert(self.conn,dict(key=event.key,date='2026-09-13'),written_by='dream:nightly',evidence_ts={'date':'2026-09-09T10:00:00'})
        self.assertEqual(events.get(self.conn,event.key).date,'2026-09-13')

    def test_partial_location_correction_preserves_derived_join_link_evidence(self):
        event=events.upsert(self.conn,dict(title='Workshop',date='2026-09-12',
            join_url='https://meet.google.com/aaa-bbbb-ccc',status='confirmed'),
            written_by='live',evidence_ts='2026-09-08T10:00:00').event
        bundle,aid=self.bundle('Use https://meet.google.com/ddd-eeee-fff now.')
        row=dict(key=event.key,location='https://meet.google.com/ddd-eeee-fff',cite_ids=[aid])
        apply_stage.apply_diffs(self.conn,self.cfg,[(bundle,{'events':[row]},'stub')],written_by='dream:nightly')
        self.assertEqual(events.get(self.conn,event.key).join_url,'https://meet.google.com/ddd-eeee-fff')

    def test_unknown_partial_key_cannot_create_an_event(self):
        bundle,aid=self.bundle('Departure is now September 12.')
        apply_stage.apply_diffs(self.conn,self.cfg,[(bundle,{'events':[dict(
            key='missing',until='2026-09-12',cite_ids=[aid])]},'stub')],written_by='dream:nightly')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0],0)

    def test_older_partial_correction_does_not_undo_newer_end(self):
        event=events.upsert(self.conn,dict(title='Stay',date='2026-09-09',until='2026-09-12',
            subject='Aster',status='confirmed'),written_by='live',
            evidence_ts='2026-09-10T13:00:00').event
        bundle,aid=self.bundle('Earlier plan: leave September 14.')
        row=dict(key=event.key,until='2026-09-14',end_boundary={'role':'event_end','cite_ids':[aid]},field_cite_ids={'until':[aid]},cite_ids=[aid])
        apply_stage.apply_diffs(self.conn,self.cfg,[(bundle,{'events':[row]},'stub')],written_by='dream:nightly')
        self.assertEqual(events.get(self.conn,event.key).until,'2026-09-12')

    def test_source_occurrence_exception_can_replace_an_unconfirmed_projection(self):
        bundle,aid=self.bundle('Our weekly workshop is Wednesdays, but September 17 Thursday this week only.',ts='2026-09-10T12:00:00')
        row=dict(title='Workshop',date='2026-09-17',time='20:00',kind='commitment',status='confirmed',series='workshop',cite_ids=[aid],field_cite_ids={k:[aid] for k in ['date','time','status','title','kind']})
        diff={'series':[dict(slug='workshop',title='Workshop',cadence='weekly',weekday=2,time='20:00',effective_on='2026-09-14',cite_ids=[aid])],'events':[row]}
        apply_stage.apply_diffs(self.conn,self.cfg,[(bundle,diff,'stub')],written_by='dream:nightly')
        rows=events.between(self.conn,'2026-09-16','2026-09-17')
        self.assertEqual([(e.date,e.status) for e in rows],[('2026-09-17','confirmed')])

class TestMergeKeepsFreshSourceVisibility(SourceFixture):
    def test_context_copy_cannot_hide_fresh_occurrence_exception(self):
        bundle,aid=self.bundle('Our workshop moves this week to September 17 at 20:00.')
        context=Bundle(entity='person:Relay',items=bundle.items,context_ids={aid})
        row=dict(title='Workshop',date='2026-09-17',time='20:00',kind='commitment',status='confirmed',series='workshop',cite_ids=[aid],field_cite_ids={k:[aid] for k in ['date','time','status','title','kind']})
        rule=dict(slug='workshop',title='Workshop',cadence='weekly',weekday=2,time='20:00',effective_on='2026-09-14',cite_ids=[aid])
        proposals=[(context,{'series':[rule],'events':[dict(row)]},'context'),(bundle,{'events':[dict(row)]},'fresh')]
        proposals,_=merge.merge_all(FakeClient(),self.cfg,proposals,conn=self.conn)
        apply_stage.apply_diffs(self.conn,self.cfg,proposals,written_by='dream:nightly')
        from memcal import series
        series.roll_forward(self.conn)
        rows=events.between(self.conn,'2026-09-16','2026-09-17')
        self.assertEqual([(e.date,e.status) for e in rows],[('2026-09-17','confirmed')])

    def test_only_context_mentions_still_cannot_create_an_event(self):
        bundle,aid=self.bundle('An old workshop plan.')
        bundle.context_ids={aid}
        other=Bundle(entity='person:Relay',items=bundle.items,context_ids={aid})
        row=dict(title='Workshop',date='2026-09-12',status='confirmed',cite_ids=[aid])
        proposals=[(b,{'events':[dict(row)]},'stub') for b in [bundle,other]]
        proposals,_=merge.merge_all(FakeClient(),self.cfg,proposals,conn=self.conn)
        apply_stage.apply_diffs(self.conn,self.cfg,proposals,written_by='dream:nightly')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0],0)

class TestDistinctPrivateOccasions(SourceFixture):
    def test_refused_merge_keeps_two_plans_without_rosters_and_replays(self):
        a,aid=self.bundle('Workshop on September 12 at 11:00 in East Studio.',external='a')
        b,bid=self.bundle('Separate workshop on September 12 at 18:00 in West Studio.',external='b')
        def proposals():
            return [(bundle,{'events':[dict(title='Workshop',date='2026-09-12',time=clock,
                location=place,kind='commitment',status='confirmed',participants=[],
                attendance=[],cite_ids=[cite],field_cite_ids={k:[cite] for k in ['date','time','location','status']})]},'stub')
                for bundle,cite,clock,place in [(a,aid,'11:00','East Studio'),(b,bid,'18:00','West Studio')]]
        for _ in range(2):
            proposed,_=merge.merge_all(FakeClient(answer={'same_event':False}),self.cfg,proposals(),conn=self.conn)
            apply_stage.apply_diffs(self.conn,self.cfg,proposed,written_by='dream:nightly')
        rows=events.between(self.conn,'2026-09-12','2026-09-12')
        self.assertEqual({(e.time,e.location) for e in rows},{('11:00','East Studio'),('18:00','West Studio')})
        self.assertEqual(len(rows),2)

if __name__=='__main__':unittest.main()
