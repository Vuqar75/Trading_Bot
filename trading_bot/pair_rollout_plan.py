"""Stage pair-enabled unit files in a new directory; never control services."""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


MODULES={'tradingbot-spot.service':'deploy/cloud_paper_launch.py',
         'tradingbot-futures.service':'trading_bot.futures_app',
         'tradingbot-cross.service':'trading_bot.cross_app',
         'tradingbot-margin.service':'trading_bot.margin_app',
         'tradingbot-monitor.service':'trading_bot.paper_monitor',
         'tradingbot-paper-backup.service':'deploy/paper_vm_backup.py',
         'tradingbot-external-backup@.service':'deploy/paper_external_backup.py',
         'tradingbot-watchdog.service':'deploy/paper_watchdog.py',
         'tradingbot-heartbeat.service':'deploy/paper_heartbeat.py'}
TRADING={'tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service'}


def stage_units(source,destination,*,installed_root,candidate_root):
    source=Path(source);destination=Path(destination)
    old=Path(installed_root);new=Path(candidate_root)
    if (not source.is_absolute() or source.is_symlink() or not source.is_dir() or
        not destination.is_absolute() or destination.exists() or destination.is_symlink() or
        not destination.parent.is_dir() or destination.parent.is_symlink() or
        not old.is_absolute() or not new.is_absolute() or old==new or
        source.resolve() in destination.parent.resolve().parents or source.resolve()==destination.parent.resolve()):
        raise ValueError('Distinct absolute unit paths and a new staging directory required')
    entries={}
    for name,module in MODULES.items():
        path=source/name
        if path.is_symlink() or not path.is_file():raise ValueError('Missing regular unit file')
        data=path.read_bytes()
        try:text=data.decode('utf-8')
        except UnicodeError:raise ValueError('Invalid unit encoding') from None
        lines=text.splitlines(keepends=True)
        work=[i for i,line in enumerate(lines) if line.startswith('WorkingDirectory=')]
        starts=[i for i,line in enumerate(lines) if line.startswith('ExecStart=')]
        if (len(work)!=1 or lines[work[0]].strip()!='WorkingDirectory='+str(old) or len(starts)!=1 or
            text.count(str(old))!=1 or str(new) in text):
            raise ValueError('Unexpected unit structure')
        start=lines[starts[0]]
        if module not in start or '--experimental-pair' in start or '\\' in start:
            raise ValueError('Unexpected unit command')
        if name!='tradingbot-spot.service' and '--telegram' in start:
            raise ValueError('Additional Telegram polling is forbidden')
        if name=='tradingbot-spot.service' and not all(x in text for x in ('SHARED_PAPER_DB','UNIFIED_PAPER_QUEUE','FUTURES_OUTBOX_DB')):
            raise ValueError('Spot shared queue and relay required')
        if name=='tradingbot-futures.service' and ('--shared-paper' not in start or 'UNIFIED_PAPER_QUEUE' not in text):
            raise ValueError('Isolated shared queue required')
        if name in ('tradingbot-cross.service','tradingbot-margin.service') and ('--db ' not in start or '--queue ' not in start):
            raise ValueError('Shared financial and queue paths required')
        ending='\r\n' if start.endswith('\r\n') else '\n' if start.endswith('\n') else ''
        if name in TRADING:lines[starts[0]]=start.rstrip('\r\n')+' --experimental-pair'+ending
        lines[work[0]]='WorkingDirectory='+str(new)+('\r\n' if lines[work[0]].endswith('\r\n') else '\n' if lines[work[0]].endswith('\n') else '')
        staged=''.join(lines).encode('utf-8')
        entries[name]={'old':data,'new':staged}
    stage=Path(tempfile.mkdtemp(prefix='.pair-units-',dir=destination.parent))
    try:
        os.chmod(stage,0o700);manifest={}
        for name,value in entries.items():
            path=stage/name;path.write_bytes(value['new']);os.chmod(path,0o600)
            manifest[name]={'source_sha256':hashlib.sha256(value['old']).hexdigest(),
                            'staged_sha256':hashlib.sha256(value['new']).hexdigest()}
        path=stage/'manifest.json';path.write_text(json.dumps(manifest,sort_keys=True),encoding='utf-8');os.chmod(path,0o600)
        os.rename(stage,destination)
        return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)


def main():
    parser=argparse.ArgumentParser(description='Stage unit files only; does not start or stop services')
    parser.add_argument('--source',required=True);parser.add_argument('--destination',required=True)
    parser.add_argument('--installed-root',required=True);parser.add_argument('--candidate-root',required=True)
    args=parser.parse_args()
    print(json.dumps(stage_units(args.source,args.destination,installed_root=args.installed_root,candidate_root=args.candidate_root),sort_keys=True))


if __name__=='__main__':main()
