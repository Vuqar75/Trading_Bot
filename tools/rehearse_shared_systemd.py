"""Read-only snapshots and isolated real-systemd paper worker rehearsal.

Run as root with --execute on the existing paper VM. The live DBs are opened
read-only, all test mutations are under a private temporary directory, and
worker sockets are disabled. No Telegram credentials are read. Temporary
systemd units have bounded restart/time limits and are stopped on normal exit.
"""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
if os.geteuid() != 0 or sys.argv[1:] != ['--execute']:
    raise SystemExit('Root and explicit --execute required')
home = Path('/var/lib/tradingbot')
sources = [home/'Trading_Bot_paper_state/runtime-paper.db', home/'Trading_Bot_futures_state/futures-paper.db']
units = ['wallet-rehearsal-spot-' + str(os.getpid()), 'wallet-rehearsal-futures-' + str(os.getpid())]
def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
def wait(predicate, timeout=35):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        if predicate(): return
        time.sleep(.5)
    raise RuntimeError('Timed out waiting for temporary service')

with tempfile.TemporaryDirectory(prefix='wallet-systemd-check-') as directory:
    root = Path(directory); repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(repo))
    from trading_bot.paper_consolidation import consolidate_snapshots
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.unified_queue import UnifiedQueue
    from tools.vm_install_shared_paper import release_entries
    readers=[]; deadline=time.monotonic()+2
    try:
        for path in sources:
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=1);readers.append(db)
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':raise RuntimeError('Unexpected journal mode')
            db.execute('BEGIN');db.execute('SELECT count(*) FROM sqlite_master').fetchone()
            if time.monotonic()>deadline:raise TimeoutError('Snapshot lock budget exceeded')
        for db,name in zip(readers,('spot.db','futures.db')):
            with sqlite3.connect(root/name) as out:
                def progress(status,left,total):
                    if time.monotonic()>deadline:raise TimeoutError('Snapshot lock budget exceeded')
                db.backup(out,pages=256,progress=progress)
    finally:
        for db in reversed(readers):db.rollback();db.close()
    for name,table in [('spot.db','runtime_meta'),('futures.db','futures_meta')]:
        with sqlite3.connect(root/name) as db:db.execute('UPDATE '+table+" SET value='true' WHERE key='paused'")
    target=root/'shared.db';queue=root/'queue.db';consolidate_snapshots(root/'spot.db',root/'futures.db',target,captured_at=time.time())
    q=UnifiedQueue(queue);q.close()
    shared=SharedPaperStore(target);shared.set_deployment_hold(True)
    before=shared.snapshot('futures')['position']
    tree=ast.parse((repo/'tools/rehearse_shared_processes.py').read_text())
    child=next(n.value.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='child' for x in n.targets))
    worker=root/'worker.py';worker.write_text(child)
    def active(unit):return subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0
    def paused(mode):
        with sqlite3.connect(target) as db:
            table='runtime_meta' if mode=='spot' else 'futures_meta'
            return json.loads(db.execute('SELECT value FROM '+table+" WHERE key='paused'").fetchone()[0])
    try:
        for unit,mode in zip(units,('spot','futures')):
            run('systemd-run','--unit='+unit,'--setenv=PYTHONPATH='+str(repo),'--property=WorkingDirectory='+str(repo),'--property=Restart=on-failure',
                '--property=RestartSec=1','--property=RuntimeMaxSec=60','--property=StartLimitIntervalSec=600','--property=StartLimitBurst=2','--property=MemoryMax=160M',sys.executable,'-B',str(worker),mode,str(target),str(queue))
        wait(lambda:all(active(u) for u in units) and paused('spot') and paused('futures'))
        wait(lambda:all((root/f).exists() for f in ('runtime-health.json','futures-health.json')))
        release_entries(shared,queue,0,{'spot':False,'futures':False})
        wait(lambda:not paused('spot') and not paused('futures'))
        oldpid=run('systemctl','show','-p','MainPID','--value',units[0]).stdout.strip()
        run('systemctl','kill','--signal=KILL',units[0])
        wait(lambda:active(units[0]) and run('systemctl','show','-p','MainPID','--value',units[0]).stdout.strip() not in (oldpid,'0') and paused('spot'))
        after=shared.snapshot('futures')['position']
        fields=('id','direction','quantity','entry','margin','leverage','funding')
        if (before is None)!=(after is None) or (before and any(before.get(k)!=after.get(k) for k in fields)):
            raise RuntimeError('Temporary position changed')
        if not shared.ledger.reconcile()['balanced']:raise RuntimeError('Temporary journal mismatch')
        print('SYSTEMD_TEMPORARY_NETWORK_BLOCKED_WORKERS_OK; CRASH_RESTART_SPOT_PAUSED; POSITION_AND_FUNDING_PRESERVED',flush=True)
    finally:
        for unit in units:subprocess.run(['systemctl','stop',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shared.close()
    print('TEMPORARY_SERVICES_STOPPED',flush=True)
