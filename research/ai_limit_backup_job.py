"""One bounded non-financial SQLite backup job; source is read-only.
Never share a trading connection/thread or hold a source BEGIN during the copy.
"""
from contextlib import closing
import os,sqlite3,threading,time
from pathlib import Path
from trading_bot.paper_wallet_ledger import PaperWalletLedger

class BackupJob:
    def __init__(self,root,*,timeout=20,action=None):
        self.root=Path(root);self.timeout=timeout;self.action=action or self.copy;self.thread=None;self.status={'state':'idle'}
    def submit(self,stamp):
        if self.thread is not None and self.thread.is_alive():return False
        self.status={'state':'running','requested_ms':stamp}
        self.thread=threading.Thread(target=self._run,args=(stamp,),daemon=True,name='paper-local-backup');self.thread.start();return True
    def _run(self,stamp):
        start=time.monotonic_ns()
        try:
            result=self.action(stamp);self.status={'state':'complete','requested_ms':stamp,'completed_ms':int(time.time()*1000),'wall_ns':time.monotonic_ns()-start,**result}
        except (OSError,ValueError,sqlite3.Error,TimeoutError):
            self.status={'state':'failed','requested_ms':stamp,'completed_ms':int(time.time()*1000),'wall_ns':time.monotonic_ns()-start,'reason':'bounded_local_backup_failed'}
    def copy(self,stamp):
        source=self.root/'paper.sqlite';folder=self.root/'backups';folder.mkdir(mode=0o700,exist_ok=True)
        if source.is_symlink() or folder.is_symlink():raise ValueError('private_backup_paths')
        target=folder/(str(stamp)+'.sqlite');pending=folder/(str(stamp)+'.pending')
        if target.exists() or pending.exists():raise ValueError('backup_target_exists')
        deadline=time.monotonic()+self.timeout
        def progress(status,remaining,total):
            if time.monotonic()>=deadline:raise TimeoutError('backup_deadline')
        # CREATE_EXCL establishes a known new temporary file; nothing existing
        # can be deleted/replaced by failure cleanup. No source transaction lock
        # spans all backup steps; each SQLite backup step is bounded to32pages.
        fd=os.open(pending,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        try:
            with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=.05)) as src:
                with closing(sqlite3.connect(pending)) as dst:src.backup(dst,pages=32,progress=progress,sleep=.001)
            with closing(sqlite3.connect(pending.resolve().as_uri()+'?mode=ro',uri=True)) as check:
                check.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
                if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup_integrity')
                check.row_factory=sqlite3.Row;ledger=object.__new__(PaperWalletLedger);ledger.db=check
                if not ledger.reconcile()['balanced']:raise ValueError('backup_reconciliation')
                events=check.execute('SELECT count(*) FROM ledger_events').fetchone()[0]
            os.link(pending,target);pending.unlink()
            fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            for old in sorted(folder.glob('[0-9]*.sqlite'))[:-24]:
                if old.stem.isdigit() and not old.is_symlink() and old.stat().st_uid==os.geteuid():old.unlink()
            return {'file':target.name,'events':events,'bytes':target.stat().st_size,'source_readonly':True,'restored_balanced':True}
        finally:
            if pending.exists():pending.unlink()
    def close(self):
        if self.thread is not None:self.thread.join(timeout=.1)
