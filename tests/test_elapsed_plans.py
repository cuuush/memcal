"""A clock cannot witness attendance; repair only the old automatic inference."""
from memcal import db, events
from tests._support import Base


class TestElapsedPlans(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-08-20')

    def row(self, status='confirmed', **extra):
        return events.upsert(self.conn, {'title':'Appointment', 'date':self.d(-2),
            'status':status, **extra}, written_by='live').event

    def old_clock(self, row, old='confirmed'):
        self.conn.execute("UPDATE events SET status='happened' WHERE id=?", (row.id,))
        self.conn.execute("INSERT INTO event_history(event_id,field,old_value,new_value,changed_at,written_by) VALUES(?,'status',?,'happened',?,'code:past')", (row.id,old,db.now()))
        self.conn.commit()

    def test_elapsed_plans_do_not_prove_attendance(self):
        for status in ('confirmed','tentative'):
            with self.subTest(status=status):
                row=self.row(status,title=status)
                self.assertEqual(events.restore_inferred_occurrence(self.conn),0)
                stored=events.get_by_id(self.conn,row.id)
                self.assertEqual(stored.status,status)
                self.assertIn('attendance unconfirmed',stored.state_text())
                self.assertIn('attendance unknown',stored.one_line())

    def test_repair_retains_identity_and_audits_old_status(self):
        for status in ('confirmed','tentative'):
            row=self.row(status,title=status)
            self.old_clock(row,status)
            self.assertEqual(events.restore_inferred_occurrence(self.conn),1)
            stored=events.get_by_id(self.conn,row.id)
            self.assertEqual((stored.key,stored.status),(row.key,status))
            self.assertEqual(events.restore_inferred_occurrence(self.conn),0)
            history=events.history(self.conn,row.id)
            self.assertEqual(history[-1]['written_by'],'code:restore-plan')
            self.assertEqual(history[-1]['new_value'],status)

    def test_explicit_attendance_and_declines_are_preserved(self):
        for status in ('happened','declined'):
            row=self.row(status,title=status)
            events.restore_inferred_occurrence(self.conn)
            self.assertEqual(events.get_by_id(self.conn,row.id).status,status)

    def test_observations_are_not_repaired(self):
        row=self.row(kind='observed')
        self.old_clock(row)
        events.restore_inferred_occurrence(self.conn)
        self.assertEqual(events.get_by_id(self.conn,row.id).status,'happened')

    def test_later_attendance_evidence_outranks_old_clock(self):
        row=self.row()
        self.old_clock(row)
        self.conn.execute("INSERT INTO event_history(event_id,field,old_value,new_value,changed_at,evidence_ts,written_by) VALUES(?,'status','happened','happened',?,?,'live')",(row.id,db.now(),db.now()))
        self.conn.commit()
        self.assertEqual(events.restore_inferred_occurrence(self.conn),0)
        self.assertEqual(events.get_by_id(self.conn,row.id).state_text(),'Already happened')

    def test_todays_plan_keeps_normal_presentation(self):
        row=self.row(date=self.d(0))
        self.assertEqual(row.state_text(),"You're going")
        self.assertEqual(row.plain_state(),'confirmed')

    def test_repaired_plan_can_be_rescheduled(self):
        row=self.row()
        self.old_clock(row)
        events.restore_inferred_occurrence(self.conn)
        stored,_=events.upsert(self.conn,{'key':row.key,'date':self.d(2)},written_by='live')
        self.assertEqual((stored.key,stored.date,stored.status),(row.key,self.d(2),'confirmed'))

    def test_elapsed_plans_do_not_count_as_person_encounters(self):
        from memcal import wiki
        wiki.set_slot(self.cfg.wiki_dir,'jordan','relationship','friend',conn=self.conn)
        for status in ('confirmed','tentative','declined','happened'):
            self.row(status,title=status,participants=['Jordan'])
        profile=wiki.profile(self.conn,self.cfg.wiki_dir,'jordan')
        self.assertEqual(profile['encounters']['count'],1)
        self.assertEqual(profile['encounters']['recent'][0]['title'],'happened')


if __name__ == "__main__":
    import unittest
    unittest.main()
