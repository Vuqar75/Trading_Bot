"""Hourly same-disk coordinated backups; retains up to 48 validated sets."""
import fcntl
import re
import shutil
from datetime import datetime,timezone
from pathlib import Path
from trading_bot.paper_coordinated_backup import coordinated_backup

home=Path('/var/lib/tradingbot');root=home/'paper-coordinated-backups'
root.mkdir(mode=0o700,parents=True,exist_ok=True)
if root.is_symlink():raise SystemExit('Backup root must not be a symlink')
root=root.resolve(strict=True)
with open(root/'.backup.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    name='paper-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest=root/name
    coordinated_backup(home/'paper-financial/shared-paper.db',
        home/'Trading_Bot_paper_state/unified-commands.db',
        home/'Trading_Bot_paper_state/monitor/observation.db',dest)
    candidates=[]
    for item in root.iterdir():
        if item.is_symlink() or not item.is_dir() or not re.fullmatch(r'paper-\d{8}T\d{12}Z',item.name):continue
        if item.resolve().parent!=root:raise RuntimeError('Backup retention path escaped root')
        if {x.name for x in item.iterdir()}!={'financial.db','queue.db','observation.db','manifest.json'}:continue
        candidates.append(item)
    for old in sorted(candidates,key=lambda p:p.name)[:-48]:
        shutil.rmtree(old)
    print('Coordinated backup created:',name,flush=True)
