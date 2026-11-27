"""Explicit external upload; install/enable only after bucket authorization."""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_external_backup import inspect_restore,prepare,upload
from trading_bot.paper_gcs_transport import upload_verified
from trading_bot.paper_coordinated_backup import backup_phase, require_workspace, terminate_backup


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bucket',required=True);parser.add_argument('--kind',choices=('hourly','daily'),required=True)
    parser.add_argument('--home',default='/var/lib/tradingbot');args=parser.parse_args()
    home=Path(args.home).resolve(strict=True);state=home/'paper-external-backups';state.mkdir(mode=0o700,exist_ok=True)
    if state.is_symlink() or not state.is_dir():raise ValueError('Invalid state directory')
    repo=Path(__file__).resolve().parents[1]
    commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True,timeout=15).strip()
    sources=(home/'paper-financial/shared-paper.db',home/'Trading_Bot_paper_state/unified-commands.db',
             home/'Trading_Bot_paper_state/monitor/observation.db')
    require_workspace(state,sources,4)
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='external-backup-',dir=state) as tmp:
        work=Path(tmp)
        stage=work/'stage';stage.mkdir(mode=0o700)
        result=prepare(*sources,stage,commit=commit)
        backup_phase('packaged',work,started)
        def transfer(path,digest):
            downloaded=work/'download.tar.gz'
            receipt=upload_verified(path,digest,bucket=args.bucket,kind=args.kind,
                                    captured_at=result['captured_at'],downloaded=downloaded)
            backup_phase('uploaded_and_downloaded',work,started)
            inspect_restore(downloaded,work/'restore',expected_sha256=digest)
            backup_phase('download_restored',work,started)
            return receipt
        def record(value):
            fd,path=tempfile.mkstemp(prefix='.success-',dir=state)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as f:
                    json.dump(value,f,sort_keys=True);f.flush();os.fsync(f.fileno())
                os.replace(path,state/(args.kind+'-success.json'))
                fd=os.open(state,os.O_RDONLY)
                try:os.fsync(fd)
                finally:os.close(fd)
            finally:
                if os.path.exists(path):os.unlink(path)
        upload(result,transfer,record)
        print('Verified external paper backup created',flush=True)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,terminate_backup)
    main()
