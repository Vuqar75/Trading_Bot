"""Explicit read-only source snapshot and isolated cross rehearsal on the VM.

This script never stops services, changes their configuration, sends orders, or
modifies their databases. SQLite locks are held briefly for coordinated backup.
Usage as root: python3 -B tools/rehearse_cross_vm_copies.py COMMIT_SHA
The copied financial data is private to a root-only temporary directory.
"""
import json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
if os.geteuid()!=0 or len(sys.argv)!=2 or not re.fullmatch('[0-9a-f]{40}',sys.argv[1]):
    raise SystemExit('Root and an explicit 40-character commit SHA are required')
if subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':
    raise SystemExit('Unexpected VM')
home=Path('/var/lib/tradingbot');expected=sys.argv[1]
with tempfile.TemporaryDirectory(prefix='cross-copy-rehearsal-') as temp:
    root=Path(temp);repo=root/'repo'
    subprocess.run(['git','clone','--quiet','--branch','research/futures-cross-paper','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=60)
    subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',expected],check=True,timeout=60)
    head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    if head!=expected:raise RuntimeError('Unexpected rehearsal code')
    sys.path.insert(0,str(repo))
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.cross_backup_verifier import verify_restorable_backup
    from trading_bot.cross_paper_book import CrossPaperBook
    from trading_bot.cross_rollout import freeze_entries,allocate_initial_cross,release_entries_after_cutover
    from trading_bot.cross_snapshot_guard import financial_history_unchanged
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.paper_wallet_live_report import live_report
    from trading_bot.unified_queue import UnifiedQueue
    source=root/'source'
    coordinated_backup(home/'paper-financial/shared-paper.db',home/'Trading_Bot_paper_state/unified-commands.db',
        home/'Trading_Bot_paper_state/monitor/observation.db',source,timeout=8)
    active=root/'active';shutil.copytree(source,active)
    financial=active/'financial.db';queue=active/'queue.db';observer=active/'observation.db'
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        CrossPaperBook(shared)
        original={m:shared.snapshot(m)['position'] for m in ('spot','futures')}
        frozen=freeze_entries(shared,q,'cross:freeze:vm-copy')
    finally:q.close();shared.close()
    with (root/'once.log').open('w') as out:
        run=subprocess.run([sys.executable,'-B','-m','trading_bot.cross_app','--db',str(financial),'--queue',str(queue),'--once'],cwd=repo,stdout=out,stderr=subprocess.STDOUT,timeout=90)
    if run.returncode:raise RuntimeError('Public cross data unavailable in isolated rehearsal')
    health=json.loads((active/'cross-health.json').read_text())
    if not health['paused'] or health['error'] or not health['data_complete']:raise RuntimeError('Copy cross health not ready')
    before=root/'before-allocation'
    coordinated_backup(financial,queue,observer,before,timeout=8)
    verified=verify_restorable_backup(before,freeze_id='cross:freeze:vm-copy',scratch_parent=root)
    if verified['pending_commands']:raise RuntimeError('Copied queue contains pending commands')
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        now=time.time();before_report=live_report(shared,now=now)
        if before_report['total'] is None:raise RuntimeError('Incomplete source valuation')
        prior=shared.ledger.balance('futures_isolated:free')
        transfer=allocate_initial_cross(shared,q,'cross:initial:vm-copy',now=now)
        after_report=live_report(shared,now=now)
        if before_report['total']!=after_report['total']:raise RuntimeError('Valuation changed at fixed prices')
        if any(before_report[k]!=after_report[k] for k in ('spot_realized','futures_realized','spot_open','futures_open')):
            raise RuntimeError('Trading PnL changed from transfer')
        if prior-shared.ledger.balance('futures_isolated:free')!=50 or shared.ledger.balance('futures_cross:free')!=50:
            raise RuntimeError('Transfer amounts disagree')
        if any(shared.snapshot(m)['position']!=original[m] for m in original):raise RuntimeError('Position changed')
        if not allocate_initial_cross(shared,q,'cross:initial:vm-copy',now=now)['replayed']:
            raise RuntimeError('Repeated allocation did not replay')
        if shared.db.execute("SELECT count(*) FROM transfer_proposals WHERE request_id='cross:initial:vm-copy' AND status='DONE'").fetchone()[0]!=1:
            raise RuntimeError('Allocation is not unique')
        total=str(before_report['total']);balanced=shared.ledger.reconcile()['balanced']
    finally:q.close();shared.close()
    after=root/'after-allocation';coordinated_backup(financial,queue,observer,after,timeout=8)
    if financial_history_unchanged(before/'financial.db',after/'financial.db'):raise RuntimeError('Old financial snapshot was not rejected')
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        release=release_entries_after_cutover(shared,q,'cross:release:vm-copy')
        expected_enabled={m for m in ('spot','futures') if not frozen['paused'][m]}|{'futures_cross'}
        if set(release['enabled'])!=expected_enabled:raise RuntimeError('Unexpected restored pause state')
        with shared.transaction():
            for market in ('spot','futures'):shared._set(market,'paused',True)
            shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
        if not release_entries_after_cutover(shared,q,'cross:release:vm-copy')['replayed']:
            raise RuntimeError('Release did not replay safely')
        if any(shared.snapshot(m)['paused'] is not True for m in ('spot','futures')) or shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]!=1:
            raise RuntimeError('Replay incorrectly resumed entries')
    finally:q.close();shared.close()
    print(json.dumps({'candidate':head,'rehearsal':'temporary copies only','public_cross_paused_healthy':True,
      'restored_three_database_backup':True,'prior_pauses':frozen['paused'],'fixed_price_capital_before_after':total,
      'virtual_transfer':'50 isolated to cross, exactly once','positions_preserved':True,
      'pnl_unchanged':True,'ledger_balanced':balanced,'old_snapshot_rejected':True,
      'release_replay_keeps_simulated_restart_paused':True},sort_keys=True),flush=True)
