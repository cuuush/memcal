import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from memcal import db, update, update_safety as safety


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()

    def snapshot(self):
        return safety.snapshot(self.home, version='0.8.10', installation={})

    def test_wal_and_private_restore(self):
        conn = sqlite3.connect(self.home / 'store.db')
        self.addCleanup(conn.close)
        conn.execute('pragma journal_mode=wal')
        conn.execute('create table evidence(value text)')
        conn.execute("insert into evidence values ('synthetic')")
        conn.commit()
        (self.home / 'config.toml').write_text('private config')
        backup = self.snapshot()
        restored = Path(self.tmp.name) / 'restored'
        safety.restore(backup, restored)
        with sqlite3.connect(restored / 'store.db') as result:
            self.assertEqual(result.execute('select value from evidence').fetchone()[0], 'synthetic')
        self.assertEqual((restored / 'config.toml').stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(ValueError):
            safety.restore(backup, self.home)

    def test_corruption_rejected_before_writing(self):
        (self.home / 'config.toml').write_text('original')
        backup = self.snapshot()
        (backup / 'store/config.toml').write_text('changed')
        dest = Path(self.tmp.name) / 'restore'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            safety.restore(backup, dest)
        self.assertFalse(dest.exists())

    def test_symlink_rejected_and_incomplete_marked(self):
        (self.home / 'link').symlink_to('/tmp')
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertTrue(next((self.home / 'backups').iterdir()).joinpath('INCOMPLETE').exists())

    def test_open_connection_blocks_maintenance(self):
        from memcal import update_safety as safety
        conn = db.connect(self.home / 'store.db')
        with self.assertRaises(safety.MaintenanceBusy):
            safety.acquire(self.home, exclusive=True)
        conn.close()
        fd = safety.acquire(self.home, exclusive=True)
        try:
            with self.assertRaises(safety.MaintenanceBusy):
                db.connect(self.home / 'store.db')
        finally:
            safety.release(fd)
        db.connect(self.home / 'store.db').close()

    def test_cross_thread_collection_releases_lock_without_sqlite_close(self):
        import gc
        import sys
        import threading
        from memcal import db as current_db, update_safety as current_safety
        connections = []
        thread = threading.Thread(target=lambda: connections.append(
            current_db.connect(self.home / "store.db")))
        thread.start()
        thread.join()
        with mock.patch.object(sys, "unraisablehook") as unraisable:
            connections.clear()
            gc.collect()
            unraisable.assert_not_called()
        fd = current_safety.acquire(self.home, exclusive=True)
        current_safety.release(fd)

    def test_stable_selection_ignores_prerelease_and_yanked(self):
        payload = {'releases': {'1.2.0': [{'yanked': False}], '2.0.0rc1': [{'yanked': False}],
                                '3.0.0': [{'yanked': True}], '1.1.0': [{'yanked': False}]}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch('urllib.request.urlopen', return_value=response):
            self.assertEqual(update._stable_version(), '1.2.0')

    def test_restore_cli(self):
        backup = self.snapshot()
        dest = Path(self.tmp.name) / 'recovered'
        self.assertEqual(update.main(['update', '--restore-backup', str(backup), '--restore-to', str(dest)]), 0)
        self.assertTrue(dest.is_dir())

    def test_source_fetches_tag_not_branch(self):
        root = Path(self.tmp.name) / 'checkout'
        (root / '.git').mkdir(parents=True)
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == 'HEAD': return 'old'
            if argv[-1] == 'remote': return 'origin'
            if argv[-1] == 'v1.2.0^{commit}': return 'new'
            return ''
        with mock.patch.object(update, '_run', side_effect=run), mock.patch.object(update, '_verify'):
            update._stable_source_update(root, '1.2.0')
        flat = [' '.join(c) for c in calls]
        self.assertTrue(any('refs/tags/v1.2.0:refs/tags/v1.2.0' in c for c in flat))
        self.assertFalse(any('origin/main' in c for c in flat))
        self.assertTrue(any('checkout --detach --no-overwrite-ignore new' in c for c in flat))

    def test_failed_source_install_restores_source_commit(self):
        root = Path(self.tmp.name) / 'checkout'
        (root / '.git').mkdir(parents=True)
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'sh': raise update.UpdateError('installer failed')
            if argv[-1] == 'HEAD': return 'old'
            if argv[-1] == 'remote': return 'origin'
            if argv[-1] == 'v1.2.0^{commit}': return 'new'
            return ''
        with mock.patch.object(update, '_run', side_effect=run):
            with self.assertRaisesRegex(update.UpdateError, 'source restored'):
                update._stable_source_update(root, '1.2.0')
        self.assertEqual(calls[-1][-4:], ['checkout', '--detach', '--no-overwrite-ignore', 'old'])


if __name__ == "__main__":
    unittest.main()
