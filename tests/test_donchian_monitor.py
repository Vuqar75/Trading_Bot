import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from research import donchian_prospective as p,donchian_service as service,donchian_monitor as m,donchian_external_backup as backup,donchian_restore_check as restore

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.dbpath=self.root/'observer.sqlite';p.init(self.dbpath,m.STRATEGY,m.START);self.db=p.connect(self.dbpath)
        self.code=Path(p.__file__).parent;service.upgrade(self.db,hashlib.sha256(Path(service.__file__).read_bytes()).hexdigest(),clock=lambda:m.START+10)
        self.control=self.root/'control';self.control.mkdir();self.marker=self.root/'backup.json';self.now=m.START+60000
        self.prepare_data(self.now,(m.START//m.DAY-1)*m.DAY)
        self.marker.write_text(json.dumps({'sha256':'abc','manifest':{'captured_utc_ms':self.now,'start_ms':m.START}}))
        (self.control/'restore.json').write_text(json.dumps({'state':'verified','checked_ms':self.now}))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def prepare_data(self,now,day):
        self.db.execute('BEGIN IMMEDIATE');p.setmeta(self.db,'last_fetch_ms',now-1000)
        for s in m.SYMBOLS:
            c={'open_ms':day,'close_ms':day+m.DAY-1,'open':'100','high':'100','low':'100','close':'100'};p.ingest(self.db,s,[c],now-1000)
            p.setmeta(self.db,'quote:'+s,{'price':'100','observed_ms':now-1000,'source_trade_ms':now-1100,'quote_protocol':'trade-time-v2'})
        self.db.commit()
    def collect(self,when,active=True):
        self.db.row_factory=sqlite3.Row
        return m.collect(self.db,when,active,{'state':'fresh'},{'state':'verified'},code=self.code)
    def test_before_deadline_candle_not_yet_required(self):
        boundary=((m.START//m.DAY)+1)*m.DAY;when=boundary+120000;self.prepare_data(when,(m.START//m.DAY-1)*m.DAY)
        x=self.collect(when);self.assertNotIn('expected_candle_missing',x['issues']);self.assertNotIn('missing_decision_windows',x['issues'])
    def test_missing_window_after_deadline(self):
        boundary=((m.START//m.DAY)+1)*m.DAY;when=boundary+m.WINDOW+1;self.prepare_data(when,(m.START//m.DAY-1)*m.DAY)
        x=self.collect(when);self.assertIn('expected_candle_missing',x['issues']);self.assertIn('missing_decision_windows',x['issues']);self.assertIn('watermark_overdue',x['issues'])
    def test_timely_window_and_executed_hold(self):
        boundary=((m.START//m.DAY)+1)*m.DAY;when=boundary+m.WINDOW+1;self.prepare_data(when,boundary-m.DAY)
        self.db.execute('BEGIN IMMEDIATE')
        for s in m.SYMBOLS:
            for sc in m.SCENARIOS:self.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)',(boundary,s,sc,'HOLD',None,boundary+1000,boundary+1010,'h','inputs',m.STRATEGY))
        p.setmeta(self.db,'next_boundary',boundary+m.DAY);self.db.commit()
        x=self.collect(when);self.assertNotIn('missing_decision_windows',x['issues']);self.assertEqual(x['complete_windows_last_8d'],1);self.assertEqual(x['recent_windows'][str(boundary)]['max_commit_delay_ms'],1010)
    def test_buy_requires_timed_execution_or_explicit_rejection(self):
        boundary=((m.START//m.DAY)+1)*m.DAY;when=boundary+m.WINDOW+1;self.prepare_data(when,boundary-m.DAY)
        self.db.execute('BEGIN IMMEDIATE')
        for s in m.SYMBOLS:
            for sc in m.SCENARIOS:self.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)',(boundary,s,sc,'BUY' if s=='BTCUSDT' and sc=='base' else 'HOLD',None,boundary+1000,boundary+1010,'h','inputs',m.STRATEGY))
        p.setmeta(self.db,'next_boundary',boundary+m.DAY);self.db.commit()
        self.assertIn('execution_unconfirmed',self.collect(when)['issues'])
        payload={'result':'filled','side':'BUY','quote':{'source_trade_ms':boundary+1011,'request_ms':boundary+1012,'observed_ms':boundary+1020,'quote_protocol':'trade-time-v2','adapter_sha256':hashlib.sha256(Path(service.__file__).read_bytes()).hexdigest()}}
        self.db.execute('INSERT INTO events VALUES(?,?,?)',(f'{boundary}:BTCUSDT:base',boundary+1020,json.dumps(payload)))
        self.assertNotIn('execution_unconfirmed',self.collect(when)['issues'])
        payload['quote']['source_trade_ms']=boundary+1000;self.db.execute('UPDATE events SET payload=? WHERE id=?',(json.dumps(payload),f'{boundary}:BTCUSDT:base'))
        self.assertIn('execution_time_invalid',self.collect(when)['issues'])
    def test_active_but_stale_and_inactive_are_distinct(self):
        x=self.collect(self.now+80*60000);self.assertTrue(x['service_active']);self.assertIn('data_stale_or_unconfirmed',x['issues'])
        x=self.collect(self.now,active=False);self.assertIn('service_inactive',x['issues'])
    def test_bad_quote_and_account_are_not_reported_as_valid(self):
        self.db.execute('BEGIN IMMEDIATE');p.setmeta(self.db,'quote:BTCUSDT',{'price':'NaN','observed_ms':self.now,'source_trade_ms':self.now,'quote_protocol':'trade-time-v2'});self.db.execute("UPDATE accounts SET state='{}' WHERE symbol='BTCUSDT' AND scenario='base' AND kind='strategy'");self.db.commit()
        x=self.collect(self.now);self.assertIn('data_stale_or_unconfirmed',x['issues']);self.assertIn('account_set_mismatch',x['issues'])
        m.run(self.now,self.dbpath,self.control,self.marker,self.code,active=True);self.assertTrue((self.control/'status.json').exists())
    def test_halted_conflict_and_ambiguous_claim_never_change_state(self):
        self.db.execute('BEGIN IMMEDIATE');p.setmeta(self.db,'halted',True);p.setmeta(self.db,'error','source_history_changed');self.db.execute('INSERT INTO quotes VALUES(?,?,?,NULL)',('pending','claimed',self.now-130000));self.db.commit()
        before=list(self.db.iterdump());x=self.collect(self.now);self.assertIn('protocol_halted',x['issues']);self.assertIn('unfinished_quote_claim',x['issues']);self.assertEqual(before,list(self.db.iterdump()))
    def test_old_or_missing_backup(self):
        self.assertEqual(m.backup_state(self.root/'absent',self.now)['state'],'missing_or_invalid')
        self.assertEqual(m.backup_state(self.marker,self.now+3*m.DAY)['state'],'stale')
    def test_report_idempotence_checkpoint_and_read_only(self):
        before=list(self.db.iterdump());m.run(self.now,self.dbpath,self.control,self.marker,self.code,active=True)
        first=next(self.control.glob('week-*.json'));saved=first.read_bytes();m.run(self.now+1000,self.dbpath,self.control,self.marker,self.code,active=True);self.assertEqual(saved,first.read_bytes());self.assertEqual(before,list(self.db.iterdump()))
        for days in (90,180):
            t=m.START+days*m.DAY;m.run(t,self.dbpath,self.control,self.marker,self.code,active=True);f=self.control/f'checkpoint-{days}.json';v=f.read_bytes();m.run(t+1000,self.dbpath,self.control,self.marker,self.code,active=True);self.assertEqual(v,f.read_bytes())
    def test_atomic_public_snapshot_is_read_only(self):
        before=list(self.db.iterdump());public=self.root/'snapshot.json'
        m.run(self.now,self.dbpath,self.control,self.marker,self.code,active=True,public=public)
        snap=json.loads(public.read_text());self.assertEqual(len(snap['accounts']),18);self.assertEqual(sum(snap['closed'].values()),0)
        self.assertTrue((self.root/'last-success.json').exists());self.assertEqual(before,list(self.db.iterdump()))
    def test_restore_corruption_failure_and_clean_success(self):
        manifest,raw=backup.snapshot(self.dbpath,self.root/'snapshot.sqlite',clock=lambda:3);blob=backup.package(manifest,raw);digest=hashlib.sha256(blob).hexdigest()
        marker={'sha256':digest,'object':backup.PREFIX+'test.tar.gz','manifest':manifest};mf=self.root/'restore-marker.json';mf.write_text(json.dumps(marker))
        result=restore.run(mf,self.control,clock=lambda:self.now,fetch=lambda _:blob);self.assertEqual(result['state'],'verified');self.assertEqual(result['detail']['counts']['accounts'],18)
        with self.assertRaises(ValueError):restore.run(mf,self.control,clock=lambda:self.now+1,fetch=lambda _:blob[:-4])
        self.assertEqual(json.loads((self.control/'restore.json').read_text())['state'],'failed')
    def test_missing_external_backup_records_failure(self):
        with self.assertRaises(ValueError):restore.run(self.root/'missing',self.control,clock=lambda:self.now,fetch=lambda _:b'')
        self.assertEqual(json.loads((self.control/'restore.json').read_text())['state'],'failed')
    def test_restore_detects_balances_inconsistent_with_events(self):
        row=self.db.execute("SELECT state FROM accounts WHERE symbol='BTCUSDT' AND scenario='base' AND kind='cash'").fetchone();a=json.loads(row[0]);a['cash']='1001';self.db.execute("UPDATE accounts SET state=? WHERE symbol='BTCUSDT' AND scenario='base' AND kind='cash'",(json.dumps(a),))
        manifest,raw=backup.snapshot(self.dbpath,self.root/'bad-account.sqlite',clock=lambda:3);blob=backup.package(manifest,raw);marker={'sha256':hashlib.sha256(blob).hexdigest(),'manifest':manifest}
        with self.assertRaisesRegex(ValueError,'account_event_reconciliation'):restore.verify_object(blob,marker,self.control)
if __name__=='__main__':unittest.main()
