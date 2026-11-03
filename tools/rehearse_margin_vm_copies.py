"""Read-only production snapshot, margin rehearsal only on private VM copies.

No running services, source databases, exchange accounts or Telegram credentials
are modified. This is not an installer and does not establish live fencing.
"""
import json,os,pwd,re,shutil,subprocess,sys,tempfile,time
from pathlib import Path

if os.geteuid()!=0 or len(sys.argv)!=2 or not re.fullmatch('[0-9a-f]{40}',sys.argv[1]):
    raise SystemExit('Root and the exact candidate commit are required')
if subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected VM')
home=Path('/var/lib/tradingbot');expected=sys.argv[1]
with tempfile.TemporaryDirectory(prefix='margin-copy-rehearsal-') as temp:
    root=Path(temp);repo=root/'repo'
    subprocess.run(['git','clone','--quiet','--branch','research/spot-margin-cross-paper','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=90)
    subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',expected],check=True,timeout=30)
    if subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()!=expected:raise RuntimeError('Wrong candidate')
    sys.path.insert(0,str(repo))
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.paper_wallet_live_report import live_report
    from trading_bot.margin_paper_book import MarginPaperBook
    from trading_bot.margin_rollback_guard import may_restore_pre_margin_code
    from trading_bot.margin_rollout import freeze_margin_cutover,allocate_margin_once,release_margin_cutover
    from trading_bot.cross_unit_switch import hold_writer_locks,switch_unit_files,restore_unit_files
    from trading_bot.unified_queue import UnifiedQueue
    source=root/'source'
    coordinated_backup(home/'paper-financial/shared-paper.db',home/'Trading_Bot_paper_state/unified-commands.db',
        home/'Trading_Bot_paper_state/monitor/observation.db',source,timeout=8)
    active=root/'active';shutil.copytree(source,active)
    financial=active/'financial.db';queue=active/'queue.db';observer=active/'observation.db'
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        before_positions={m:shared.snapshot(m)['position'] for m in ('spot','futures')}
        frozen=freeze_margin_cutover(shared,q,'margin:freeze:vm-copy')
        MarginPaperBook(shared.ledger)
    finally:q.close();shared.close()
    service_user=pwd.getpwnam('tradingbot')
    for path in [root,*root.rglob('*')]:
        if not path.is_symlink():os.chown(path,service_user.pw_uid,service_user.pw_gid)
    with (root/'margin-once.log').open('w') as log:
        result=subprocess.run(['systemd-run','--quiet','--wait','--collect','--pipe','--unit=margin-copy-rehearsal-'+str(os.getpid()),
            '--property=User=tradingbot','--property=Group=tradingbot','--property=WorkingDirectory='+str(repo),
            '/usr/bin/python3','-B','-m','trading_bot.margin_app','--db',str(financial),'--queue',str(queue),'--once','--hold-resume'],
            cwd=repo,stdout=log,stderr=subprocess.STDOUT,timeout=120)
    if result.returncode:raise RuntimeError('Margin public-data rehearsal failed: '+(root/'margin-once.log').read_text(errors='replace')[-1000:])
    health=json.loads((active/'margin-health.json').read_text())
    if not health['paused'] or health['error'] or not health['quotes_fresh'] or not health['protection_fresh']:
        raise RuntimeError('Margin health not ready on copied data')
    before=root/'before-transfer';coordinated_backup(financial,queue,observer,before,timeout=8)
    # Exercise crash recovery against copied unit files only; this does not
    # claim that live services were stopped or fenced.
    unit_dir=root/'unit-copies';unit_dir.mkdir();candidate=root/'unit-candidates';candidate.mkdir();archive=root/'unit-archive';archive.mkdir()
    names=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-monitor.service','tradingbot-margin.service')
    for name in names[:-1]:
        fragment=subprocess.check_output(['systemctl','show','--property=FragmentPath','--value',name],text=True).strip()
        if not fragment or not Path(fragment).is_file():raise RuntimeError('Existing unit unavailable')
        data=Path(fragment).read_bytes();(unit_dir/name).write_bytes(data);(candidate/name).write_bytes(data+b'\n# simulated candidate\n')
    shutil.copyfile(repo/'deploy/vm-spot-margin-paper.service',candidate/names[-1])
    def verified():
        return may_restore_pre_margin_code(before/'financial.db',financial,before/'queue.db',queue)['safe_for_old_code']
    with hold_writer_locks(financial):
        def crash(point):
            if point=='switched:tradingbot-spot.service':raise RuntimeError('simulated interruption')
        try:switch_unit_files(unit_dir,candidate,archive,names,new_units=(names[-1],),assert_safe=lambda:True,fault=crash)
        except RuntimeError:pass
        else:raise RuntimeError('Switch interruption was not exercised')
        restore_unit_files(unit_dir,archive,names,assert_safe=lambda:True,verify_rollback=verified)
        switch_unit_files(unit_dir,candidate,archive,names,new_units=(names[-1],),assert_safe=lambda:True)
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        now=time.time();before_report=live_report(shared,now=now)
        if before_report['total'] is None:raise RuntimeError('Copied capital valuation incomplete')
        prior=shared.ledger.balance('futures_isolated:free')
        allocation=allocate_margin_once(shared,q,'margin:initial:vm-copy',now=now)
        if allocate_margin_once(shared,q,'margin:initial:vm-copy',now=now)['event']!=allocation['event']:raise RuntimeError('Transfer replay mismatch')
        after_report=live_report(shared,now=now)
        if after_report['total']!=before_report['total'] or not after_report['valuation_ok']:raise RuntimeError('Copied capital changed at fixed prices')
        if prior-shared.ledger.balance('futures_isolated:free')!=50 or shared.ledger.balance('margin_cross:free')!=50:raise RuntimeError('Copied transfer mismatch')
        if any(shared.snapshot(m)['position']!=before_positions[m] for m in before_positions):raise RuntimeError('Existing position changed')
        if not shared.ledger.reconcile()['balanced']:raise RuntimeError('Copied journal mismatch')
        capital=str(before_report['total'])
    finally:q.close();shared.close()
    after=root/'after-transfer';coordinated_backup(financial,queue,observer,after,timeout=8)
    guard=may_restore_pre_margin_code(before/'financial.db',after/'financial.db',before/'queue.db',after/'queue.db')
    if guard['safe_for_old_code'] or 'financial_history_changed' not in guard['reasons']:raise RuntimeError('Rollback guard did not reject new event')
    with hold_writer_locks(financial):
        try:restore_unit_files(unit_dir,archive,names,assert_safe=lambda:True,verify_rollback=lambda:guard['safe_for_old_code'])
        except ValueError:pass
        else:raise RuntimeError('Unit rollback ignored new financial event')
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        release=release_margin_cutover(shared,q,'margin:release:vm-copy',protection_checked_ms=health['protection_checked_at'])
        expected_enabled={m for m in ('spot','futures') if not frozen['paused'][m]}|{'margin_cross'}
        if set(release['enabled'])!=expected_enabled:raise RuntimeError('Original pause restoration mismatch')
        if not shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]:raise RuntimeError('Cross pause changed')
        with shared.transaction():shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
        if not release_margin_cutover(shared,q,'margin:release:vm-copy',protection_checked_ms=health['protection_checked_at'])['replayed'] or not shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]:raise RuntimeError('Release replay changed manual pause')
    finally:q.close();shared.close()
    print(json.dumps({'candidate':expected,'source':'coordinated private copies','capital_fixed_price_usdt':capital,
        'copy_transfer_usdt':'50','transfer_replay':'same_event','old_positions_preserved':True,
        'systemd_copy_once':True,'margin_health_paused':True,'quotes_fresh':True,'protection_fresh':True,'journal_balanced':True,
        'copied_unit_switch_interruption':'restored_and_replayed','rollback_after_new_event':'refused',
        'original_pauses_restored':'yes','release_replay_preserved_later_pause':True,'production_changed':False},sort_keys=True))
