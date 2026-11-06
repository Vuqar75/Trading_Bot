"""Verify and release the already-switched report-only deployment."""
import argparse,importlib.util,json,os,shutil,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');ARCHIVE=HOME/'adaptivediag-updates/20261106e'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
RELEASE='adaptivediag:release:20261106e'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=('ready','release','postcheck'));args=parser.parse_args()
    if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected host')
    prepared=json.loads((ARCHIVE/'prepared.json').read_text());candidate=prepared['candidate'];target=Path(prepared['target']).resolve(strict=True)
    if subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip()!=candidate:raise ValueError('Candidate mismatch')
    if json.loads((ARCHIVE/'switch-state').read_text())['stage']!='candidate_services_started_on_hold':raise ValueError('Switch not ready for restart')
    sys.path.insert(0,str(target))
    from trading_bot.cross_unit_switch import _write
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.report_update_guard import release_report_update
    from trading_bot.unified_queue import UnifiedQueue
    path=target/'tools/margin_verify_vm.py'
    spec=importlib.util.spec_from_file_location('installed_margin_verifier',path);verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
    verifier.ARCHIVE=ARCHIVE
    def checked_health(*,held):
        current=verifier._market_health(target,held=held);now=time.time()
        for name in ('runtime-health.json','futures-health.json'):
            h=json.loads((FINANCIAL.parent/name).read_text())
            if type(h.get('position_open')) is not bool:raise ValueError('Position protection status unavailable')
            if h['position_open'] and (not isinstance(h.get('protection_checked_at'),(int,float)) or not 0<=now-h['protection_checked_at']<=120):
                raise ValueError('Fresh position protection not confirmed')
        return current
    if args.phase=='ready':
        deadline=time.monotonic()+150;previous={};cycles=0;last='not checked'
        while time.monotonic()<deadline and cycles<3:
            try:
                current=checked_health(held=True);stamps=current['health_times']
                if not previous or all(stamps[k]>previous[k] for k in stamps):
                    cycles+=1;previous=stamps;print('ADAPTIVEDIAG_HEALTH_CYCLE',cycles,flush=True)
            except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as exc:last=type(exc).__name__+': '+str(exc)
            if cycles<3:time.sleep(2)
        if cycles<3:raise ValueError('Healthy cycles not proven: '+last)
        current.update({'candidate':candidate,'cycles':cycles})
        _write(ARCHIVE/'ready.json',json.dumps(current,sort_keys=True).encode(),0o600)
        print('ADAPTIVEDIAG_READY_ON_HOLD',flush=True);return
    if args.phase=='release':
        ready=json.loads((ARCHIVE/'ready.json').read_text())
        if ready.get('candidate')!=candidate or ready.get('cycles')!=3:raise ValueError('Paused health verification missing')
        current=checked_health(held=True)
        try:
            shared=SharedPaperStore(FINANCIAL);queue=UnifiedQueue(QUEUE)
            try:result=release_report_update(shared,queue,RELEASE,protection_checked_ms=current['protection_checked_ms'],namespace='adaptivediag')
            finally:queue.close();shared.close()
            _write(ARCHIVE/'release-proof.json',json.dumps(result,sort_keys=True).encode(),0o600)
        except BaseException:
            verifier._emergency_hold();raise
        print(json.dumps({'enabled':result['enabled'],'manual_pauses':result['manual_pauses']},sort_keys=True),flush=True);return
    if not (ARCHIVE/'release-proof.json').is_file():raise ValueError('Release has not completed')
    deadline=time.monotonic()+90;last='not checked'
    while True:
        try:current=checked_health(held=False);break
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as exc:
            last=type(exc).__name__+': '+str(exc)
            if time.monotonic()>=deadline:verifier._emergency_hold();raise ValueError('Post-release health not proven: '+last)
            time.sleep(2)
    backup=ARCHIVE/'post-release'
    try:
        if not backup.exists():coordinated_backup(FINANCIAL,QUEUE,OBSERVER,backup,timeout=15)
        with tempfile.TemporaryDirectory(prefix='report-restore-check-',dir=ARCHIVE) as tmp:
            manifest=json.loads((backup/'manifest.json').read_text())
            if {e['name'] for e in manifest['files']}!={'financial.db','queue.db','observation.db'}:raise ValueError('Backup incomplete')
            for entry in manifest['files']:
                import hashlib
                dest=Path(tmp)/entry['name'];shutil.copyfile(backup/entry['name'],dest)
                with dest.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=entry['sha256']:raise ValueError('Backup hash mismatch')
                with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Backup integrity failure')
            from trading_bot.cross_paper_book import CrossPaperBook
            from trading_bot.margin_paper_book import MarginPaperBook
            shared=SharedPaperStore(Path(tmp)/'financial.db')
            try:
                cross=CrossPaperBook(shared);margin=MarginPaperBook(shared.ledger);cross._check();margin._check()
                if not shared.ledger.reconcile()['balanced']:raise ValueError('Backup journal mismatch')
            finally:shared.close()
    except BaseException:
        verifier._emergency_hold();raise
    print(json.dumps({'candidate':candidate,'paused':current['paused'],'available_kib':current['available_kib'],
        'history_and_positions':'verified','backup':'post-release','poller_and_relay':'healthy','owner_commands':'awaiting_owner'},sort_keys=True),flush=True)

if __name__=='__main__':main()
