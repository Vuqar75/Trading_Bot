"""Guarded staged cutover for the explicitly authorized paper-only VM.

This is an operator entrypoint, not an unattended automatic deployment. Run
only after the candidate's full VM suite and copy rehearsals pass. The prepare
phase stops before reboot; finish requires a new boot and healthy paused state.
No real/Testnet order method or Telegram configuration is accessed here.
"""
import argparse
import fcntl
import os
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.cross_cutover_backend import CrossCutoverBackend
from trading_bot.cross_cutover_journal import cutover_journal
from trading_bot.cross_cutover_machine import run_cutover_phases
from trading_bot.cross_systemd_controller import SystemdPaperController
from trading_bot.cross_unit_switch import _write
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.unified_queue import UnifiedQueue

HOME=Path('/var/lib/tradingbot');CODE=Path('/opt/trading-bot-cross');OLD=Path('/opt/trading-bot-wallet')
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db'
OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db';UNITS=Path('/etc/systemd/system')
ROOT=HOME/'cross-cutovers';MARKER=ROOT/'service-start-interlock'
INSTALLED='aeea6f591afcbd17f8d649d639652bf5b3cbcbe4'
MAPPING={'tradingbot-spot.service':'vm-cross-spot-paper.service',
         'tradingbot-futures.service':'vm-cross-isolated-paper.service',
         'tradingbot-cross.service':'vm-cross-futures-paper.service',
         'tradingbot-monitor.service':'vm-cross-paper-monitor.service',
         'tradingbot-paper-backup.service':'vm-cross-paper-backup.service',
         'tradingbot-paper-backup.timer':'vm-shared-paper-backup.timer'}
OLD_MAPPING={'tradingbot-spot.service':'vm-shared-spot-paper.service',
             'tradingbot-futures.service':'vm-shared-futures-paper.service',
             'tradingbot-monitor.service':'vm-shared-paper-monitor.service',
             'tradingbot-paper-backup.service':'vm-shared-paper-backup.service',
             'tradingbot-paper-backup.timer':'vm-shared-paper-backup.timer'}


def _git(directory,*args):
    result=subprocess.run(['git','-C',str(directory),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
    if result.returncode:raise ValueError('Repository verification failed')
    return result.stdout.strip()


def _private_dir(path,*,create):
    if path.is_symlink():raise ValueError('Private directory is a symlink')
    if not path.exists():
        if not create:raise ValueError('Cutover archive missing')
        path.mkdir(mode=0o700)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    info=path.stat()
    if not path.is_dir() or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o077:raise ValueError('Invalid private cutover directory')


@contextmanager
def _host_lock():
    path=ROOT/'host.lock';fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1:raise ValueError('Invalid host cutover lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another cutover is running') from None
        yield
    finally:os.close(fd)


def _check_existing_units():
    if _git(OLD,'rev-parse','HEAD')!=INSTALLED or _git(OLD,'status','--porcelain','--untracked-files=all'):
        raise ValueError('Installed checkout differs from confirmed version')
    for target,source in OLD_MAPPING.items():
        path=UNITS/target;template=OLD/'deploy'/source
        if path.is_symlink() or template.is_symlink() or not path.is_file() or not template.is_file() or path.read_bytes()!=template.read_bytes():
            raise ValueError('Installed service file differs from confirmed template')
    if (UNITS/'tradingbot-cross.service').exists() or (UNITS/'tradingbot-cross.service').is_symlink():
        raise ValueError('An existing cross service requires inspection')


def _running_processes_match(controller):
    expected=((controller.SPOT,'trading_bot.app'),(controller.FUTURES,'trading_bot.futures_app'),(controller.MONITOR,'trading_bot.paper_monitor'))
    for unit,module in expected:
        try:
            first=int(controller._property(unit,'MainPID'));proc=Path('/proc')/str(first)
            if first<=1 or (proc/'cwd').resolve(strict=True)!=OLD.resolve(strict=True):return False
            args=(proc/'cmdline').read_bytes().split(b'\0')
            if module.encode() not in args or b'--db' not in args or str(FINANCIAL).encode() not in args:return False
            if unit==controller.SPOT and (b'--shared-paper' not in args or b'--telegram' not in args or b'paper' not in args):return False
            if unit==controller.FUTURES and (b'--shared-paper' not in args or b'--telegram' in args):return False
            if int(controller._property(unit,'MainPID'))!=first:return False
        except (OSError,ValueError,TypeError):return False
    return True


def _preflight(shared,queue,controller):
    _check_existing_units()
    if controller._healthy(cross=False,paused=False) is not True or not _running_processes_match(controller):
        raise ValueError('Existing runtime, poller or health not proven')
    if queue.db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ValueError('Pending commands require processing')
    if shared.deployment_hold() or not shared.ledger.reconcile()['balanced']:raise ValueError('Existing journal requires recovery')
    spot=shared._assert_consistent('spot');future=shared._assert_consistent('futures')
    if any(type(state['paused']) is not bool for state in (spot,future)):raise ValueError('Unknown pause state')
    identity=shared._get('spot','identity');fi=shared._get('futures','identity')
    if not isinstance(identity,dict) or identity.get('mode')!='paper' or identity.get('symbol')!='BTCUSDT' or identity.get('account')!='paper' or Decimal(identity.get('amount','0'))!=10:
        raise ValueError('Unexpected Spot paper parameters')
    if not isinstance(fi,dict) or fi.get('mode')!='futures-paper' or fi.get('symbol')!='BTCUSDT' or any(Decimal(fi.get(k,'0'))!=Decimal(v) for k,v in (('margin','10'),('fee_bps','5'),('slippage_bps','2'),('maintenance_bps','50'))):
        raise ValueError('Unexpected isolated paper parameters')
    if shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_positions'").fetchone():
        raise ValueError('Existing cross state requires inspection')
    if shared.ledger.balance('futures_cross:free') or shared.db.execute("SELECT 1 FROM ledger_events WHERE kind LIKE 'CROSS_%' LIMIT 1").fetchone():
        raise ValueError('Existing cross financial history requires inspection')


def _stage_templates(directory):
    _private_dir(directory,create=True)
    for target,source in MAPPING.items():
        origin=CODE/'deploy'/source;dest=directory/target
        if origin.is_symlink() or not origin.is_file() or dest.is_symlink():raise ValueError('Invalid staged service file')
        data=origin.read_bytes()
        if dest.exists():
            if not dest.is_file() or dest.read_bytes()!=data:raise ValueError('Staged service file changed')
        else:_write(dest,data,0o600)


def _run(args):
    suffix=args.operation.removeprefix('cross:freeze:');archive=ROOT/suffix
    for item in ROOT.iterdir():
        if item.name not in (suffix,MARKER.name,'host.lock'):raise SystemExit('Another cutover archive requires inspection')
    _private_dir(archive,create=args.phase=='prepare');journal_dir=archive/'journal';_private_dir(journal_dir,create=args.phase=='prepare')
    rollback_intent=archive/'rollback-intent'
    if args.phase!='rollback' and rollback_intent.exists():
        raise SystemExit('Rollback has begun; cutover cannot be resumed automatically')
    if args.phase!='prepare' and not (journal_dir/'journal.json').is_file():raise SystemExit('Existing cutover journal required')
    staged=archive/'candidate-units';_stage_templates(staged)
    shared=SharedPaperStore(FINANCIAL);queue=UnifiedQueue(QUEUE)
    try:
        controller=SystemdPaperController(FINANCIAL,UNITS,shared,timeout=55)
        backend=CrossCutoverBackend(shared,queue,OBSERVER,UNITS,staged,archive,MARKER,
                                    tuple(MAPPING),('tradingbot-cross.service',),args.operation,controller)
        with cutover_journal(journal_dir,args.operation,args.commit) as journal:
            if args.phase=='prepare':
                if journal.state['stage']=='created':_preflight(shared,queue,controller)
                state=run_cutover_phases(journal,backend.actions(),backend.probes())
                print('CROSS_CUTOVER_STATE',state,flush=True)
            elif args.phase=='finish':
                if journal.state['stage'] not in ('awaiting_reboot','released','complete'):
                    raise ValueError('Finish requires an existing reboot checkpoint')
                state=run_cutover_phases(journal,backend.actions(),backend.probes(),after_reboot=True,verify_reboot=backend.reboot_verified)
                print('CROSS_CUTOVER_STATE',state,flush=True)
            else:
                if rollback_intent.is_symlink():raise ValueError('Invalid rollback marker')
                if not rollback_intent.exists():_write(rollback_intent,args.operation.encode(),0o600)
                elif rollback_intent.read_text()!=args.operation:raise ValueError('Rollback identity mismatch')
                backend.rollback_old_code_paused();print('CROSS_OLD_CODE_RESTORED_PAUSED',flush=True)
    finally:queue.close();shared.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute',action='store_true');parser.add_argument('--commit',required=True)
    parser.add_argument('--operation',required=True);parser.add_argument('--phase',choices=('prepare','finish','rollback'),required=True)
    args=parser.parse_args()
    if not args.execute or os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':
        raise SystemExit('Root, expected VM and explicit --execute are required')
    if not re.fullmatch('[0-9a-f]{40}',args.commit) or not re.fullmatch(r'cross:freeze:[A-Za-z0-9_-]{1,64}',args.operation):
        raise SystemExit('Invalid commit or operation identity')
    if CODE.is_symlink() or Path(__file__).resolve()!=CODE/'tools/vm_cross_cutover.py' or _git(CODE,'rev-parse','HEAD')!=args.commit or _git(CODE,'status','--porcelain','--untracked-files=all'):
        raise SystemExit('Candidate checkout or exact commit mismatch')
    for p in (FINANCIAL,QUEUE,OBSERVER):
        if p.is_symlink() or not p.is_file() or HOME.resolve() not in p.resolve().parents:raise SystemExit('Unexpected active database path')
    # Ensure opening the stores cannot create a missing schema in the live DB.
    with sqlite3.connect(FINANCIAL.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_operations'").fetchone():raise SystemExit('Shared runtime schema unavailable')
    _private_dir(ROOT,create=args.phase=='prepare')
    with _host_lock():_run(args)


if __name__=='__main__':main()
