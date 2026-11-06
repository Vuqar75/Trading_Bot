from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research import adaptive_ai_executor as executor
from research import adaptive_server_once as server
from research.adaptive_budget_move import freeze_export, import_original
from research.adaptive_shadow import canonical, PERIOD
from research.adaptive_server_publish import publish
from research.adaptive_server_provision import provision


class ServerOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'wsl'; self.source.mkdir(mode=0o700)
        for name in ('interactive-once.lock', 'approved-followup-once.lock'):
            (self.source/name).write_text('original-lock-contents')
        with closing(sqlite3.connect(self.source/'experiment-budget.db')) as db:
            db.execute(f'PRAGMA application_id={executor.APP_ID}')
            db.execute('CREATE TABLE api_attempts(request_id TEXT PRIMARY KEY,reserved_micro_usd INTEGER NOT NULL,created_at REAL NOT NULL,result_json TEXT)')
            db.executemany('INSERT INTO api_attempts VALUES(?,?,?,NULL)', [('original',3630,1),('adaptive:'+str(9*PERIOD),2088,2)])
            db.commit()
        executor.migrate(self.source)
        self.archive = freeze_export(self.source, source_stopped=True)
        self.state = self.root/'server'
        import_original(self.archive, self.state)
        self.public = self.root/'public'/'report.json'
        self.now = 10*PERIOD+120001
        self.snapshot = {'symbol':'BTCUSDT','as_of_ms':self.now-1000,'received_ms':self.now-1000,
                         'latest_4h_ms':9*PERIOD,'latest_1h_ms':0,'latest_15m_ms':0,
                         'features':{},'book':{},'news':[],'coverage':'incomplete'}
        self.snapshot['snapshot_id'] = hashlib.sha256(canonical(self.snapshot).encode()).hexdigest()

    def response(self, req):
        decision = {'mode':'TREND_DOWN','snapshot_id':self.snapshot['snapshot_id'],
                    'data_ms':self.snapshot['as_of_ms'],'expires_ms':self.now+100000,
                    'reasons':['trend'],'uncertainties':['book event time unproven']}
        return {'status':'completed','usage':{'input_tokens':100,'output_tokens':20},
                'output':[{'type':'message','role':'assistant','status':'completed',
                           'content':[{'type':'output_text','text':json.dumps(decision)}]}]}

    def execute(self, **changes):
        args = dict(clock=lambda:self.now,fetch=lambda:(self.snapshot,'RANGE'),secret=lambda:'fake-server-key')
        args.update(changes)
        return server.execute(self.state,self.public,**args)

    def test_import_preserves_original_rows_locks_and_freezes_source(self):
        with closing(sqlite3.connect(self.state/'experiment-budget.db')) as db:
            self.assertEqual(db.execute('SELECT count(*),sum(reserved_micro_usd),sum(result_json IS NULL) FROM api_attempts').fetchone(),(2,5718,2))
        self.assertEqual((self.state/'interactive-once.lock').read_text(),'original-lock-contents')
        self.assertTrue((self.source/'migration-source-frozen.json').exists())
        self.assertEqual(server.owner(self.state)['role'],'ACTIVE_SERVER')
        with self.assertRaisesRegex(ValueError,'source_disabled'):
            executor.run(self.snapshot,'RANGE',self.source,transport=self.response,clock=lambda:self.now)
        with self.assertRaisesRegex(ValueError,'target_must_not_exist'):
            import_original(self.archive,self.state)

    def test_export_requires_source_shutdown_and_import_requires_fence(self):
        with self.assertRaisesRegex(ValueError,'disable_all_source'):
            freeze_export(self.source)
        path=self.archive/'manifest.json'
        data=json.loads(path.read_text()); data.pop('migration'); path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'source_not_frozen'):
            import_original(self.archive,self.root/'other')

    def test_corrupt_or_traversal_archive_never_activates(self):
        p=self.archive/'manifest.json';data=json.loads(p.read_text())
        data['sha256']['../outside']='a'*64;p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'unexpected_archive_files'):
            import_original(self.archive,self.root/'bad')
        data['sha256'].pop('../outside');p.write_text(json.dumps(data))
        (self.archive/'interactive-once.lock').write_text('changed')
        with self.assertRaisesRegex(ValueError,'backup_hash'):
            import_original(self.archive,self.root/'bad')
        self.assertFalse((self.root/'bad').exists())

    def test_portable_import_does_not_require_sibling_checkout(self):
        root=Path(__file__).resolve().parents[1]
        env={'PYTHONPATH':str(root),'PATH':os.environ['PATH']}
        result=subprocess.run([sys.executable,'-B','-c',
            'from research.adaptive_ai_executor import MODEL; assert MODEL == "gpt-6-luna"'],
            cwd=self.root,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_success_then_repeated_tick_has_no_collection_credential_or_send(self):
        with patch.object(executor,'_send',side_effect=self.response) as send:
            result=self.execute()
            self.assertEqual(result['mode'],'TREND_DOWN')
            self.assertEqual(result['baseline'],'RANGE')
            self.assertEqual(send.call_count,1)
            def forbidden():self.fail('Repeated tick attempted external work')
            result=self.execute(fetch=forbidden,secret=forbidden)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(send.call_count,1)
        self.assertNotEqual(os.environ.get('TRADING_BOT_AI'),'fake-server-key')
        self.assertNotIn('fake-server-key',self.public.read_text())

    def test_unknown_response_retains_reservation_and_blocks_all_retries(self):
        with patch.object(executor,'_send',side_effect=TimeoutError('arbitrary transport text')) as send:
            with self.assertRaises(TimeoutError):self.execute()
            result=self.execute(fetch=lambda:self.fail('must not collect'))
            self.assertEqual(result['status'],'attempt_uncertain_or_gate_consumed')
            self.assertEqual(send.call_count,1)
        with closing(sqlite3.connect(self.state/'experiment-budget.db')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM api_attempts').fetchone()[0],3)
            self.assertGreater(db.execute('SELECT sum(reserved_micro_usd) FROM api_attempts').fetchone()[0],5718)

    def test_saved_response_failure_recovers_without_http_repeat(self):
        def fault(stage):
            if stage=='before_usage':raise RuntimeError('local failure')
        def send(s,b,state,**kwargs):
            return executor.run(s,b,state,fault=fault,**kwargs)
        with patch.object(executor,'_send',side_effect=self.response) as http:
            result=self.execute(send=send)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(http.call_count,1)
        with closing(sqlite3.connect(self.state/'experiment-budget.db')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM api_usage').fetchone()[0],1)

    def test_no_key_or_wrong_window_never_collects_or_reserves(self):
        def forbidden():self.fail('unexpected external collection')
        result=self.execute(clock=lambda:10*PERIOD+1,fetch=forbidden,secret=forbidden)
        self.assertEqual(result['status'],'waiting_for_next_boundary')
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'credential_not_bound'):
                self.execute(secret=server.credential,fetch=forbidden)
        with closing(sqlite3.connect(self.state/'experiment-budget.db')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM api_attempts').fetchone()[0],2)

    def test_server_credential_has_no_tty_or_windows_dependency(self):
        directory=self.root/'creds';directory.mkdir();(directory/'TRADING_BOT_AI').write_text('fake-loaded-key\n')
        with patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':str(directory)}):
            self.assertEqual(server.credential(),'fake-loaded-key')
            (directory/'TRADING_BOT_AI').write_text('fake\nheader')
            with self.assertRaisesRegex(ValueError,'credential_invalid'):server.credential()

    def test_concurrent_tick_and_source_fence_fail_before_network(self):
        with server.exclusive(self.state):
            with self.assertRaisesRegex(ValueError,'already_running'):self.execute()
        (self.state/'migration-source-frozen.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'source_disabled'):self.execute()

    def test_changed_budget_does_not_consume_new_gate(self):
        with closing(sqlite3.connect(self.state/'experiment-budget.db')) as db:
            db.execute('UPDATE api_attempts SET reserved_micro_usd=1');db.commit()
        with self.assertRaisesRegex(ValueError,'budget_state_changed'):self.execute()
        self.assertFalse((self.state/'adaptive-next-once.lock').exists())

    def test_publish_is_read_only_for_budget_and_idempotent(self):
        with patch.object(executor,'_send',side_effect=self.response):self.execute()
        before=(self.state/'experiment-budget.db').read_bytes()
        target=self.root/'telegram';target.mkdir();target=target/'report.json'
        self.assertTrue(publish(self.public,target,now=self.now/1000,gid=os.getgid()))
        self.assertFalse(publish(self.public,target,now=self.now/1000,gid=os.getgid()))
        self.assertIn('baseline того же среза',target.read_text())
        self.assertEqual(before,(self.state/'experiment-budget.db').read_bytes())

    def test_expired_or_incomplete_report_not_published(self):
        with patch.object(executor,'_send',side_effect=self.response):self.execute()
        target=self.root/'report.json'
        with self.assertRaisesRegex(ValueError,'not_current'):
            publish(self.public,target,now=(self.now+100001)/1000,gid=os.getgid())
        self.assertFalse(target.exists())
        data=json.loads(self.public.read_text());data['complete']=False;self.public.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'confirmed_comparison'):
            publish(self.public,target,now=self.now/1000,gid=os.getgid())

    def test_one_time_relay_uses_only_stdin_and_existing_pinned_ssh(self):
        calls=[]
        def runner(args,**kwargs):
            calls.append((args,kwargs))
            self.assertNotIn('fake-key-for-stdin',str(args))
            self.assertEqual(kwargs['input'],b'fake-key-for-stdin')
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('BatchMode=yes',args)
            return subprocess.CompletedProcess(args,0,b'SERVER_CREDENTIAL_PROVISIONED',b'')
        provision('existing-vm-alias',credential=lambda:'fake-key-for-stdin',runner=runner)
        self.assertEqual(len(calls),1)

    def test_relay_does_not_expose_remote_error_or_start_login(self):
        def runner(args,**kwargs):
            return subprocess.CompletedProcess(args,1,b'',b'fake-secret-remote-body')
        with self.assertRaisesRegex(ValueError,'^server_credential_not_provisioned$'):
            provision('existing-vm-alias',credential=lambda:'fake-key',runner=runner)
        with self.assertRaisesRegex(ValueError,'trusted_ssh_alias'):
            provision('-oUnsafeOption',credential=lambda:self.fail('should not read credential'))
