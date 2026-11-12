"""The identity list follows incoming activity without deleting history."""

import contextlib
import io
import unittest
from unittest import mock

from _support import Base
from memcal import archive, cli, db, identity, whois


class TestWhoDropsInactiveSenders(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-12T12:00:00-05:00')
        self.enterContext(mock.patch.object(cli, 'open_ctx', return_value=(self.cfg, self.conn)))
        self.serial = 0

    def message(self, handle, *, person=None, recent=False, from_me=False):
        self.serial += 1
        archive.append(self.conn, channel='imessage', external_id=str(self.serial),
                       handle=handle, person=person, text='Synthetic conversation',
                       thread=handle, from_me=from_me,
                       ts=db.now() if recent else '2026-08-01T12:00:00-04:00')
        self.conn.commit()

    def output(self):
        args = cli.build_parser().parse_args(['who'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_who(args), 0)
        return out.getvalue()

    def test_unnamed_sender_returns_only_on_new_incoming_activity(self):
        handle = '+12025550131'
        self.message(handle)
        identity.note_unresolved(self.conn, handle, 'imessage')
        self.message(handle, recent=True, from_me=True)
        self.assertNotIn(handle, self.output())
        self.message(handle, recent=True)
        self.assertIn(handle, self.output())
        db.set_today('2027-02-12T12:00:00-05:00')
        self.assertNotIn(handle, self.output())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM archive').fetchone()[0], 3)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM unresolved').fetchone()[0], 1)

    def test_name_doubts_and_assumptions_expire_and_return(self):
        handle = '+12025550132'
        self.message(handle)
        number = whois.doubt(self.conn, 'name', handle, 'Mira Flint', 'Synthetic doubt')
        for state in ('unsure', 'assumed'):
            with self.subTest(state=state):
                self.conn.execute('UPDATE identity_assumptions SET state = ? WHERE id = ?',
                                  (state, number))
                self.conn.commit()
                self.assertNotIn(handle, self.output())
                self.assertEqual(len(whois.assumptions(self.conn, state)), 1)
        self.message(handle, recent=True)
        self.assertIn(handle, self.output())
        db.set_today('2027-02-12T12:00:00-05:00')
        self.assertNotIn(handle, self.output())
        # Expiring the display does not prevent an explicit decision by id.
        self.assertTrue(whois.confirm(self.conn, number))
        self.assertEqual(identity.resolve(self.conn, handle), 'Mira Flint')

    def test_duplicate_name_pairs_require_recent_incoming_activity_on_both_sides(self):
        self.message('+12025550133', person='Liora', recent=True)
        self.message('+12025550134', person='Liora Flint')
        self.assertNotIn('possibly one person', self.output())
        self.message('+12025550134', person='Liora Flint', recent=True)
        self.assertIn('Liora Flint', self.output())
        self.assertIn('possibly one person', self.output())

    def test_inactive_namesake_still_prevents_an_ambiguous_pair(self):
        self.message('+12025550135', person='Liora', recent=True)
        self.message('+12025550136', person='Liora Flint', recent=True)
        identity.link(self.conn, '+12025550137', 'Liora Thorn', source='cli')
        self.assertNotIn('possibly one person', self.output())

    def test_merge_doubts_drop_inactive_people_and_respect_configured_window(self):
        self.message('+12025550138', person='Dorian Vale', recent=True)
        self.message('+12025550139', person='Dory Vale')
        number = whois.doubt(self.conn, 'merge', 'Dory Vale', 'Dorian Vale', 'Synthetic doubt')
        self.assertNotIn('not sure', self.output())
        self.cfg.unresolved_active_days = 180
        self.assertIn('not sure', self.output())
        self.assertEqual(whois.assumptions(self.conn, 'unsure')[0]['id'], number)

    def test_assumed_merge_tracks_the_moved_handle_after_names_are_folded(self):
        identity.link(self.conn, '+12025550140', 'Dorian Vale', source='cli')
        identity.link(self.conn, '+12025550141', 'Dory Vale', source='cli')
        self.message('+12025550140', person='Dorian Vale', recent=True)
        self.message('+12025550141', person='Dory Vale')
        number = whois.assume(self.conn, 'Dorian Vale', 'Dory Vale')
        self.assertIsNotNone(number)
        self.assertNotIn('already merged', self.output())
        self.message('+12025550141', person='Dorian Vale', recent=True)
        self.assertIn('Dory Vale', self.output())
        db.set_today('2027-02-12T12:00:00-05:00')
        self.assertNotIn('already merged', self.output())
        self.assertTrue(whois.split(self.conn, number))
        self.assertEqual(identity.resolve(self.conn, '+12025550141'), 'Dory Vale')


if __name__ == '__main__':
    unittest.main()
