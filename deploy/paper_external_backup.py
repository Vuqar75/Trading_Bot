"""Explicit external upload; install/enable only after bucket authorization."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_external_backup import inspect_restore,prepare,upload
from trading_bot.paper_gcs_transport import upload_verified


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bucket',required=True);parser.add_argument('--kind',choices=('hourly','daily'),required=True)
    parser.add_argument('--home',default='/var/lib/tradingbot');args=parser.parse_args()
    home=Path(args.home).resolve(strict=True);state=home/'paper-external-backups';state.mkdir(mode=0o700,exist_ok=True)
    if state.is_symlink() or not state.is_dir():raise ValueError('Invalid state directory')
    repo=Path(__file__).resolve().parents[1]
    commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True,timeout=15).strip()
    with tempfile.TemporaryDirectory(prefix='external-backup-',dir=state) as tmp:
        stage=Path(tmp)/'stage';stage.mkdir(mode=0o700)
        result=prepare(home/'paper-financial/shared-paper.db',home/'Trading_Bot_paper_state/unified-commands.db',
                       home/'Trading_Bot_paper_state/monitor/observation.db',stage,commit=commit)
        inspect_restore(result['path'],Path(tmp)/'restore',expected_sha256=result['sha256'])
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
        upload(result,lambda p,d:upload_verified(p,d,bucket=args.bucket,kind=args.kind,captured_at=result['captured_at']),record)
        print('Verified external paper backup created',flush=True)

if __name__=='__main__':main()
