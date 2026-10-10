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
        self.line(30,'The speaker switched slots because their train is delayed.')
        correction=self.line(31,'We are using the west room now.')
        events.upsert(self.conn,{'key':self.event.key,'title':'Workshop','date':'2026-08-20',
            'location':'West room'},written_by='dream',evidence_ts='2026-08-10T10:31:00')
        opened=detail.open_handle(self.conn,self.cfg,f'E{self.event.id}')
        self.assertIn('train is delayed',opened)
        self.assertIn(f'[{correction}]',opened)
        self.assertLessEqual(sum('· Taylor:' in line for line in opened.splitlines()),12)

    def test_uncited_neighbours_do_not_displace_cited_lines(self):
        older=self.line(0,'Workshop starts at ten.')
        for i in range(1,10):self.line(i,cited=False)
        newer=self.line(10,'The speaker has changed.')
        rows=trace.source_rows(self.conn,'event',self.event.key,context=5,limit=2)
        self.assertEqual([r['id'] for r in rows],[older,newer])
        self.assertTrue(all(r['evidence'] for r in rows))


if __name__ == '__main__':
    unittest.main()
