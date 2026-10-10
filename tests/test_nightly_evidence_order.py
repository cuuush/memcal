"""Peer nightly writes obey the same evidence ordering as typed corrections."""
from datetime import date
import unittest

from tests._support import Base
from memcal import archive,db,events
from memcal.dream import apply,bundle


class TestNightlyTimeOnlyUpdateDoesNotRestoreOldVenue(Base):
    def setUp(self):
        super().setUp()
        db.set_today(date(2026,8,6))

    def _proposal(self, external, stamp, text, fields, field_cites):
        aid=archive.append(self.conn,channel='imessage',external_id=external,
            ts=stamp,text=text,thread=external,person='Jordan',from_me=False,
            gated=True,gate_reason='temporal')
        self.conn.commit()
        item=self.conn.execute('SELECT * FROM archive WHERE id=?',(aid,)).fetchone()
        row={**fields,'cite_ids':[aid],
             'field_cite_ids':{name:[aid] for name in field_cites}}
        return bundle.Bundle(entity='thread:imessage:'+external,items=[item]),{'events':[row]}

    def test_source_order_replay_and_typed_read_keep_latest_per_field(self):
        ev,_=events.upsert(self.conn,{'title':'Dinner','date':'2026-08-07',
            'time':'19:00','location':'North Cafe','participants':['Jordan']},
            written_by='dream:nightly',evidence_ts='2026-08-05T18:00:00-04:00')
        change=self._proposal('group','2026-08-06T20:00:00-04:00','West Cafe instead.',
            {'key':ev.key,'title':'Dinner','date':ev.date,'location':'West Cafe'},['location'])
        time_only=self._proposal('dm','2026-08-06T18:00:00-04:00','Can we do 5 instead?',
            {'key':ev.key,'title':'Dinner','date':ev.date,'time':'17:00','location':'North Cafe'},['time'])
        for _ in range(2):
            apply.apply_diffs(self.conn,self.cfg,[change,time_only],written_by='dream:nightly')
        stored=events.get(self.conn,ev.key)
        self.assertEqual((stored.time,stored.location),('17:00','West Cafe'))
        history=self.conn.execute("SELECT * FROM event_history WHERE event_id=? AND field='location' AND written_by NOT LIKE '%:born'",
                                  (ev.id,)).fetchall()
        self.assertEqual(len(history),1)
        updated,_=events.upsert(self.conn,{'key':ev.key,'date':ev.date,'location':'South Cafe'},
            written_by='live',evidence_ts={'location':'2026-08-07T08:00:00-04:00'})
        self.assertEqual(updated.location,'South Cafe')
        apply.apply_diffs(self.conn,self.cfg,[change,time_only],written_by='dream:nightly')
        self.assertEqual(events.get(self.conn,ev.key).location,'South Cafe')

    def test_newer_peer_evidence_can_correct_old_value(self):
        ev,_=events.upsert(self.conn,{'title':'Dinner','date':'2026-08-07','location':'North Cafe'},
            written_by='dream:nightly',evidence_ts='2026-08-05T18:00:00-04:00')
        for stamp,venue in [('2026-08-06T20:00:00-04:00','West Cafe'),
                            ('2026-08-06T21:00:00-04:00','South Cafe')]:
            events.upsert(self.conn,{'key':ev.key,'date':ev.date,'location':venue},
                          written_by='dream:nightly',evidence_ts={'location':stamp})
        self.assertEqual(events.get(self.conn,ev.key).location,'South Cafe')


if __name__=='__main__':
    unittest.main()
