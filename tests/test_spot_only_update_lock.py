import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from trading_bot.spot_only_update_lock import hold_stopped_spot_locks


class SpotOnlyUpdateLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'finance.db'
        self.db.touch()
        for name in ('.spot.lock', '.delivery.lock', '.futures.lock', '.cross.lock', '.margin.lock'):
            Path(str(self.db) + name).touch()

    def test_other_market_locks_may_remain_held(self):
        with open(str(self.db) + '.cross.lock', 'r+') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with hold_stopped_spot_locks(self.db):
                with open(str(self.db) + '.spot.lock', 'r+') as spot:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(spot, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_running_spot_or_relay_refuses_and_releases_prior_lock(self):
        for suffix in ('.spot.lock', '.delivery.lock'):
            with self.subTest(suffix=suffix), open(str(self.db) + suffix, 'r+') as active:
                fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'still running'):
                    with hold_stopped_spot_locks(self.db):
                        pass
            with hold_stopped_spot_locks(self.db):
                pass

    def test_missing_and_symlink_lock_refused(self):
        lock = Path(str(self.db) + '.spot.lock')
        lock.unlink()
        with self.assertRaises(FileNotFoundError):
            with hold_stopped_spot_locks(self.db):
                pass
        lock.symlink_to(self.db)
        with self.assertRaises(OSError):
            with hold_stopped_spot_locks(self.db):
                pass
