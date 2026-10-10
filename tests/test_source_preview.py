"""Bounded memory pages must keep recent cited evidence ahead of old context."""
import unittest
from datetime import datetime,timedelta
from tests._support import Base
from memcal import archive,db,detail,events,trace


class TestBoundedSourcePreviewKeepsRecentEvidence(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-08-10')
        self.event=events.upsert(self.conn,{'title':'Workshop','date':'2026-08-20',
            'location':'East room','status':'confirmed'},written_by='dream').event

    def line(self,n,text=None,*,cited=True):
        row=archive.append(self.conn,channel='imessage',external_id=f'workshop-{n}',
            ts=(datetime(2026,8,10,10)+timedelta(minutes=n)).isoformat(),thread='workshop',
            text=text or f'Agenda detail {n}',person='Taylor',gated=True)
        if cited:trace.stamp(self.conn,kind='event',ref=self.event.key,verb='updated',archive_ids=[row])
        return row

    def test_a_long_citation_history_keeps_latest_evidence_within_the_bound(self):
        ids=[self.line(i) for i in range(60)]
        rows=trace.source_rows(self.conn,'event',self.event.key,context=0,limit=12)
        self.assertEqual([r['id'] for r in rows],ids[-12:])
        self.assertTrue(all(r['evidence'] for r in rows))

    def test_the_normal_event_open_keeps_recent_reason_and_slot_evidence(self):
        for i in range(20):self.line(i)
        self.line(30,'The speaker switched slots because their train is delayed.',cited=False)
        correction=self.line(31,'We are using the west room now.')
        events.upsert(self.conn,{'key':self.event.key,'title':'Workshop','date':'2026-08-20',
            'location':'West room'},written_by='dream',evidence_ts='2026-08-10T10:31:00')
        opened=detail.open_handle(self.conn,self.cfg,f'E{self.event.id}')
        self.assertIn('train is delayed',opened)
        self.assertIn(f'[{correction}]',opened)
        preview=detail._sources_text(self.conn,'event',self.event.key)
        self.assertIn('train is delayed',preview)
        self.assertLessEqual(sum('· Taylor:' in line for line in preview.splitlines()),12)

    def test_uncited_neighbours_do_not_displace_the_newest_citation(self):
        older=self.line(0,'Workshop starts at ten.')
        for i in range(1,10):self.line(i,cited=False)
        newer=self.line(10,'The speaker has changed.')
        rows=trace.source_rows(self.conn,'event',self.event.key,context=5,limit=2)
        self.assertIn(newer,[r['id'] for r in rows])
        self.assertTrue(next(r for r in rows if r['id']==newer)['evidence'])
        self.assertEqual(len(rows),2)


class TestSourceHeadingsFollowDisplayedTime(Base):
    def test_promoted_current_evidence_labels_older_context_as_earlier(self):
        rows=[{'ts':ts,'channel':'imessage','thread':'workshop'} for ts in (
            '2026-08-12T10:00:00','2026-08-11T10:00:00','2026-08-13T10:00:00')]
        marked=trace._mark_source_shifts(self.conn,rows)
        self.assertIn('1 day earlier',marked[1]['source_heading'])
        self.assertIn('2 days later',marked[2]['source_heading'])
        self.assertNotIn('-1',marked[1]['source_heading'])


if __name__ == '__main__':
    unittest.main()
