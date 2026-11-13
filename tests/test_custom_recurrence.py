"""One rule spans multiple weekdays, dated changes, and native-calendar exceptions."""

import json
import unittest
from unittest import mock

from tests._support import Base
from memcal import db, events, live, mcp_server, series
from memcal.sources import base, ical
from memcal.dream import apply, bundle


class _RecurringBase(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2032-03-01T09:00:00-05:00')

    def create(self, **extra):
        return live.set_schedule(self.conn, self.cfg, 'Studio practice', cadence='weekly',
                                 weekdays=['monday', 'wednesday', 'friday'], time='17:00', **extra)[0]

    def dates(self, until='2032-03-28'):
        return [day.isoformat() for day in series.occurrences(None, '2032-03-01', until,
                                                              series=series.get(self.conn, 'studio-practice'))]


class TestCustomRecurringLifecycle(_RecurringBase):
    def test_three_weekdays_are_one_rule_and_one_projection_with_no_duplicate_on_replay(self):
        self.create(location='https://video.example/studio')
        self.create(location='https://video.example/studio')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM series').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertEqual(self.dates('2032-03-07'), ['2032-03-01', '2032-03-03', '2032-03-05'])
        self.assertEqual(series.get(self.conn, 'studio-practice').days, [0, 2, 4])

    def test_a_day_lookup_materializes_the_requested_recurring_day_with_a_stable_handle(self):
        self.create()
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = 'test', ''
        text = server.call('memcal_list_days', {'when': '2032-03-05'})
        self.assertIn('Studio practice', text)
        self.assertNotIn('free as far', text)
        before = self.conn.execute('SELECT count(*) FROM events').fetchone()[0]
        self.assertEqual(server.call('memcal_list_days', {'when': '2032-03-05'}), text)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], before)

    def test_future_change_preserves_every_earlier_occurrence(self):
        self.create()
        live.set_schedule(self.conn, self.cfg, 'Studio practice', weekdays=['tuesday', 'thursday'],
                          starting='2032-03-15', time='18:00')
        self.assertEqual(series.get(self.conn, 'studio-practice').days, [0, 2, 4])
        self.assertEqual(self.dates('2032-03-21'), ['2032-03-01', '2032-03-03', '2032-03-05',
                         '2032-03-08', '2032-03-10', '2032-03-12', '2032-03-16', '2032-03-18'])
        db.set_today('2032-03-15')
        self.assertEqual(series.get(self.conn, 'studio-practice').days, [1, 3])
        series.roll_forward(self.conn)
        future = [event for event in events.by_series(self.conn, 'studio-practice') if event.date >= '2032-03-15']
        self.assertTrue(future)
        self.assertTrue(all(event.time == '18:00' for event in future))

    def test_pause_resume_and_future_stop_keep_one_series_and_old_history(self):
        self.create()
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='paused', starting='2032-03-08')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='active', starting='2032-03-22')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='ended', starting='2032-03-26')
        self.assertEqual(self.dates(), ['2032-03-01', '2032-03-03', '2032-03-05', '2032-03-22', '2032-03-24'])
        self.assertEqual(series.get(self.conn, 'studio-practice', on='2032-03-10').status, 'paused')
        self.assertEqual(series.get(self.conn, 'studio-practice', on='2032-03-23').status, 'active')

    def test_one_unmaterialized_occurrence_can_be_cancelled_or_postponed(self):
        self.create()
        cancelled, _ = live.move_one_occurrence(self.conn, self.cfg, 'Studio practice',
                                               on='2032-03-03', cancelled=True)
        moved, _ = live.move_one_occurrence(self.conn, self.cfg, 'Studio practice',
                                           on='2032-03-05', to='2032-03-06')
        live.move_one_occurrence(self.conn, self.cfg, 'Studio practice', on='2032-03-03', cancelled=True)
        self.assertEqual(cancelled.status, 'declined')
        self.assertEqual((moved.date, moved.instead_of), ('2032-03-06', '2032-03-05'))
        self.assertEqual(series.get(self.conn, 'studio-practice').days, [0, 2, 4])
        self.assertEqual(series.covered(self.conn, 'studio-practice'), {'2032-03-03', '2032-03-05'})
        series.roll_forward(self.conn, ahead=10)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE series='studio-practice' AND date='2032-03-03'").fetchone()[0], 1)

    def test_custom_interval_preserves_week_alignment_across_query_windows(self):
        self.create(interval=3)
        self.assertEqual(self.dates(), ['2032-03-01', '2032-03-03', '2032-03-05', '2032-03-22', '2032-03-24', '2032-03-26'])
        rule = series.get(self.conn, 'studio-practice')
        self.assertEqual([day.isoformat() for day in series.occurrences(None, '2032-03-04', '2032-03-25', series=rule)],
                         ['2032-03-05', '2032-03-22', '2032-03-24'])

    def test_future_location_change_and_pause_do_not_shift_a_custom_interval(self):
        self.create(interval=3)
        live.set_schedule(self.conn, self.cfg, 'Studio practice', location='South room', starting='2032-03-10')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='paused', starting='2032-03-08')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='active', starting='2032-03-17')
        self.assertEqual(self.dates(), ['2032-03-01', '2032-03-03', '2032-03-05', '2032-03-22', '2032-03-24', '2032-03-26'])

    def test_stopping_a_series_cancels_an_older_planned_resume(self):
        self.create()
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='paused', starting='2032-03-08')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='active', starting='2032-03-22')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', status='ended', starting='2032-03-10')
        self.assertEqual(self.dates(), ['2032-03-01', '2032-03-03', '2032-03-05'])

    def test_requested_distant_occurrences_survive_a_normal_projection_sweep(self):
        self.create()
        series.materialize_between(self.conn, '2035-03-01', '2035-03-07')
        before = self.conn.execute("SELECT count(*) FROM events WHERE date>='2035-03-01'").fetchone()[0]
        self.assertGreater(before, 0)
        series.roll_forward(self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE date>='2035-03-01'").fetchone()[0], before)

    def test_ingested_dream_rule_replay_does_not_duplicate_series_or_undo_newer_days(self):
        report = base.IngestReport(channel='imessage')
        aid = base.deliver(self.conn, report, channel='imessage', external_id='practice-rule',
                           ts='2032-03-01T08:00:00-05:00', thread='studio',
                           text='Studio practice is every Monday, Wednesday and Friday at 5pm')
        self.conn.commit()
        row = self.conn.execute('SELECT * FROM archive WHERE id=?', (aid,)).fetchone()
        source = bundle.Bundle(entity='thread:imessage:studio', items=[row])
        diff = {'series': [{'slug': 'studio-practice', 'title': 'Studio practice', 'cadence': 'weekly',
                           'weekdays': [0, 2, 4], 'time': '17:00', 'effective_on': '2032-03-01', 'cites': ['L1']}]}
        for _ in range(2):
            apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        live.set_schedule(self.conn, self.cfg, 'Studio practice', weekdays=['tuesday', 'thursday'])
        apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by='dream:test')
        self.assertEqual(series.get(self.conn, 'studio-practice').days, [1, 3])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM series').fetchone()[0], 1)


class TestNativeCustomRecurrence(_RecurringBase):
    def backend(self):
        self.cfg.publish_calendar = 'Synthetic test calendar'
        calls = []
        def call(verb, name, payload, **kwargs):
            spec = json.loads(payload); calls.append((verb, spec))
            if verb == 'unrepeat': return {'removed': True}
            if verb == 'exception': return {'uid': 'exception-id'}
            return {'uid': spec.get('uid') or f'native-{len(calls)}', 'calendar': name, 'created': not spec.get('uid')}
        self.patch = mock.patch.object(ical, '_account_call', side_effect=call)
        self.patch.start(); self.addCleanup(self.patch.stop)
        return calls

    def test_multiweekday_rule_is_one_native_repeat_and_replay_makes_no_write(self):
        calls = self.backend(); self.create()
        self.assertEqual([verb for verb, _ in calls], ['repeat'])
        self.assertEqual(calls[0][1]['weekdays'], [0, 2, 4])
        self.assertEqual(calls[0][1]['interval'], 1)
        ical.publish_schedules(self.conn, self.cfg)
        self.assertEqual(len(calls), 1)

    def test_dated_changes_bound_the_old_native_rule_and_create_a_recurring_successor(self):
        calls = self.backend(); self.create()
        live.set_schedule(self.conn, self.cfg, 'Studio practice', weekdays=['tuesday', 'thursday'], starting='2032-03-15')
        repeats = [spec for verb, spec in calls if verb == 'repeat']
        self.assertEqual(repeats[-2]['weekdays'], [0, 2, 4])
        self.assertTrue(repeats[-2]['until'].startswith('2032-03-14T23:59:59'))
        self.assertEqual(repeats[-1]['weekdays'], [1, 3])
        self.assertTrue(repeats[-1]['start'].startswith('2032-03-16'))

    def test_cancel_one_occurrence_uses_native_exception_and_is_replay_safe(self):
        calls = self.backend(); self.create()
        live.move_one_occurrence(self.conn, self.cfg, 'Studio practice', on='2032-03-03', cancelled=True)
        self.assertEqual(calls[-1][0], 'exception')
        self.assertTrue(calls[-1][1]['cancelled'])
        before = len(calls)
        ical.publish_schedules(self.conn, self.cfg)
        self.assertEqual(len(calls), before)
        self.assertNotIn('unrepeat', [verb for verb, _ in calls])


class TestToolSurfaceAfterAudit(unittest.TestCase):
    def test_mcp_has_no_deprecated_open_or_join_field_and_supports_custom_days(self):
        tools = {tool['name']: tool for tool in mcp_server.TOOLS}
        self.assertEqual(len(tools), 22)
        self.assertNotIn('memcal_open_page', tools)
        for name in ('memcal_add', 'memcal_update', 'memcal_schedule'):
            self.assertNotIn('join_url', tools[name]['inputSchema']['properties'])
            self.assertIn('location', tools[name]['inputSchema']['properties'])
        schedule = tools['memcal_schedule']['inputSchema']['properties']
        self.assertIn('weekdays', schedule)
        self.assertIn('interval', schedule)
        self.assertNotIn('fortnightly', schedule['cadence']['enum'])
        self.assertEqual(tools['memcal_move_once']['inputSchema']['required'], ['which'])


if __name__ == '__main__':
    unittest.main()
