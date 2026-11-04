"""Host-local fence for the stopped Spot worker and its delivery relay.

This is one prerequisite for a Spot-only cutover, not a complete installer.
Other market worker locks must remain owned by their live processes.
"""
import fcntl
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def hold_stopped_spot_locks(financial):
    path = Path(financial)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Invalid financial database path')
    path = path.resolve(strict=True)
    owner = path.stat()
    with ExitStack() as stack:
        for suffix in ('.spot.lock', '.delivery.lock'):
            lock = Path(str(path) + suffix)
            fd = os.open(lock, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    (info.st_uid, info.st_gid) != (owner.st_uid, owner.st_gid)):
                raise ValueError('Invalid Spot or delivery lock inode')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Spot worker or delivery relay is still running') from None
        yield
