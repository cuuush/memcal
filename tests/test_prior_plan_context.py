"""The original clinic detail must reach extraction without repeating its name."""
import unittest
from _support import Base
from memcal import archive,db,events,threads
from memcal.dream import bundle


class TestPriorPlanDetailsAreRestored(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-10-20')
        events.upsert(self.conn,{'title':'Consultation','date':'2026-10-21'})
        threads.record(self.conn,'imessage','dm',is_group=False)

    def line(self,text,stamp,*,thread='dm',gated=False):
        aid=archive.append(self.conn,channel='imessage',external_id=str(
            self.conn.execute('SELECT count(*) FROM archive').fetchone()[0]),
            thread=thread,ts=stamp,text=text,person='Sam',gated=gated,gate_reason='temporal' if gated else None)
        return self.conn.execute('SELECT * FROM archive WHERE id=?',(aid,)).fetchone()

    def prior(self,*,thread='dm',day='2026-10-05'):
        self.line('The first workshop is with me and Alex.',day+'T17:50:00-04:00',thread=thread)
        anchor=self.line('That is why we want the consultation.',day+'T17:56:00-04:00',thread=thread)
        for n in range(3):self.line('Some other conversation.',day+f'T17:56:{10+n:02d}-04:00',thread=thread)
        clinic=self.line('It is actually at Elm Counseling Center.',day+'T17:57:47-04:00',thread=thread)
        return anchor,clinic

    def current(self,*,day='2026-10-21'):
        return self.line(f'Want to join me and Alex for the consultation on {day} at 4 pm?',
                         '2026-10-08T12:00:00-04:00',gated=True)

    def test_full_bundle_build_restores_clinic_and_real_citation_without_consuming_it(self):
        _anchor,clinic=self.prior()
        new=self.current()
        archive.spool_add(self.conn,new['id'],'person:Sam');self.conn.commit()
        b=bundle.build(self.conn)[0]
        self.assertIn(clinic['id'],b.context_ids)
        self.assertEqual(len(b.spool_ids),1)
        index=next(i for i,row in enumerate(b.items,1) if row['id']==clinic['id'])
        self.assertEqual(b.cite([f'L{index}']),[clinic['id']])
        self.assertIn('[context]',b.render())

    def test_same_topic_in_a_different_chat_is_not_retrieved(self):
        _anchor,clinic=self.prior(thread='somebody-else')
        b=bundle.Bundle(entity='person:Sam',items=[self.current()])
        bundle.add_plan_context(self.conn,b)
        self.assertNotIn(clinic['id'],b.context_ids)

    def test_matching_word_on_a_different_day_is_not_enough(self):
        _anchor,clinic=self.prior()
        b=bundle.Bundle(entity='person:Sam',items=[self.current(day='2026-11-09')])
        bundle.add_plan_context(self.conn,b)
        self.assertNotIn(clinic['id'],b.context_ids)

    def test_context_older_than_the_retrieval_window_is_not_restored(self):
        _anchor,clinic=self.prior(day='2026-08-01')
        b=bundle.Bundle(entity='person:Sam',items=[self.current()])
        bundle.add_plan_context(self.conn,b)
        self.assertNotIn(clinic['id'],b.context_ids)

    def test_muted_thread_is_not_reopened(self):
        _anchor,clinic=self.prior()
        self.conn.execute("UPDATE threads SET decision='mute' WHERE thread='dm'")
        b=bundle.Bundle(entity='person:Sam',items=[self.current()])
        bundle.add_plan_context(self.conn,b)
        self.assertNotIn(clinic['id'],b.context_ids)

    def test_dense_history_still_has_a_fixed_context_budget(self):
        for day in range(1,8):
            self.line('We talked about the consultation.',f'2026-10-{day:02d}T09:00:00-04:00')
            for n in range(15):
                self.line('A separate detail.',f'2026-10-{day:02d}T09:01:{n:02d}-04:00')
        b=bundle.Bundle(entity='person:Sam',items=[self.current()])
        bundle.add_plan_context(self.conn,b)
        self.assertLessEqual(len(b.context_ids),24)
        self.assertEqual(len(b.items),len(b.context_ids)+1)

    def test_upcoming_plan_is_not_crowded_out_by_many_past_topics(self):
        _anchor,clinic=self.prior()
        items=[self.current()]
        for n,title in enumerate(('Long Name One','Long Name Two','Long Name Three','Long Name Four','Long Name Five')):
            day=f'2026-10-{10+n:02d}'
            events.upsert(self.conn,{'title':title,'date':day})
            items.append(self.line(f'{title} on {day}', '2026-10-20T12:00:00-04:00',gated=True))
        b=bundle.Bundle(entity='person:Sam',items=items)
        bundle.add_plan_context(self.conn,b)
        self.assertIn(clinic['id'],b.context_ids)


if __name__=='__main__':
    unittest.main()
