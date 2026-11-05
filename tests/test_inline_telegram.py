import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from trading_bot.app import TelegramTransport
from trading_bot.unified_queue import UnifiedQueue


class FakeTelegram:
    def __init__(self):self.updates=[];self.calls=[];self.fail_edit=False
    def __call__(self,request):
        method=request.full_url.rsplit('/',1)[-1];data=json.loads(request.data);self.calls.append((method,data))
        if method=='getUpdates':return {'ok':True,'result':self.updates}
        if method=='editMessageReplyMarkup' and self.fail_edit:return {'ok':False}
        if method=='sendMessage':return {'ok':True,'result':{'message_id':9}}
        return {'ok':True,'result':True}


class InlineTelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'q.db'
        self.q=UnifiedQueue(self.path,allow_pair_experiment=True);self.addCleanup(lambda:self.q.close())
        self.api=FakeTelegram();self.t=TelegramTransport('fixture-token','100',transport=self.api,started=0)

    def keyboard(self):
        row=self.q.db.execute("SELECT value FROM unified_state WHERE key LIKE 'inline-outbox:%' ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0])['inline_keyboard']

    def callback(self,ident,data,*,sender=100,chat=100):
        return {'update_id':ident,'callback_query':{'id':'query-'+str(ident),'from':{'id':sender},'data':data,
                'message':{'message_id':9,'chat':{'id':chat,'type':'private'}}}}

    def test_close_buttons_auth_retry_restart_and_failed_edit(self):
        now=time.time();position={'id':'position-1','quantity':'0.25','symbol':'BTCUSDT'}
        self.q.receive(1,True,'/close',now=now,positions={'spot':position})
        self.q.add_proposal(1,'spot','position-1','0.25',now=now)
        keys=self.keyboard();confirm=keys[0][0]['callback_data'];self.assertLessEqual(len(confirm.encode()),64)
        self.api.updates=[self.callback(2,confirm,sender=101)];self.t.poll(None,self.q)
        self.assertEqual(self.q.pending('spot'),[])
        self.api.fail_edit=True;self.api.updates=[self.callback(3,confirm)];self.t.poll(None,self.q)
        jobs=self.q.pending('spot');self.assertEqual(len(jobs),1)
        self.assertEqual(json.loads(jobs[0]['payload'])['position'],'position-1')
        self.q.close();self.q=UnifiedQueue(self.path,allow_pair_experiment=True)
        self.t.poll(None,self.q)
        self.assertEqual(len(self.q.pending('spot')),1)
        self.assertTrue(any(method=='answerCallbackQuery' for method,_ in self.api.calls))

    def test_pair_menu_preserves_market_and_snapshot_across_mode_change(self):
        now=time.time();snap={'spot':{'symbols':['BTCUSDT'],'version':4}}
        self.q.receive(1,True,'/pair',now=now,pairs=snap)
        key=next(row[0]['callback_data'] for row in self.keyboard() if row[0]['text']=='SOLUSDT')
        self.q.receive(2,True,'/mode futures',now=now+1)
        self.q.receive(3,True,'',now=now+2,callback_data=key)
        proposal=self.q.pair_proposals()[0]
        self.assertEqual((proposal['market'],proposal['version'],proposal['target']),('spot',4,['SOLUSDT']))
        key=self.keyboard()[0][0]['callback_data'];self.q.receive(4,True,'',now=now+3,callback_data=key)
        self.assertEqual(len([j for j in self.q.pending('spot') if j['command']=='/confirmed_pair']),1)
        self.assertEqual(self.q.state('mode'),'futures')

    def test_expired_and_unknown_buttons_never_create_trade_job(self):
        now=time.time();pos={'id':'p','quantity':'1','symbol':'BTCUSDT'}
        self.q.receive(1,True,'/close',now=now,positions={'spot':pos});self.q.add_proposal(1,'spot','p','1',now=now,ttl=1)
        key=self.keyboard()[0][0]['callback_data'];self.q.receive(2,True,'',now=now+2,callback_data=key)
        self.q.receive(3,True,'',now=now+2,callback_data='b:untrusted')
        self.assertEqual(self.q.pending('spot'),[])

    def test_settings_menu_revision_fences_later_change_and_restart(self):
        now=time.time();self.q.receive(1,True,'/timeframe',now=now)
        job=self.q.pending('spot')[0];self.q.acknowledge(job['id'],'Выберите период',menu_setting='15m')
        old=self.keyboard()[0][0]['callback_data']
        self.q.receive(2,True,'/timeframe 5m',now=now+1)
        self.q.close();self.q=UnifiedQueue(self.path,allow_pair_experiment=True)
        self.q.receive(3,True,'',now=now+2,callback_data=old)
        self.assertEqual([j['command'] for j in self.q.pending('spot')],['/timeframe 5m'])

    def test_callback_expiry_is_checked_after_real_sqlite_lock_wait(self):
        now=time.time();pos={'id':'p','quantity':'1','symbol':'BTCUSDT'}
        self.q.receive(1,True,'/close',now=now,positions={'spot':pos});self.q.add_proposal(1,'spot','p','1',now=now,ttl=.25)
        token=self.keyboard()[0][0]['callback_data'];self.q.db.execute('BEGIN IMMEDIATE');errors=[]
        def work():
            q=UnifiedQueue(self.path,allow_pair_experiment=True)
            try:q.receive(2,True,'',callback_data=token)
            except Exception as exc:errors.append(exc)
            finally:q.close()
        thread=threading.Thread(target=work);thread.start();time.sleep(.4);self.q.db.commit();thread.join(10)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(self.q.pending('spot'),[])

    def test_setting_button_rechecks_actual_spot_and_isolated_state_at_apply(self):
        from trading_bot.runtime_store import RuntimeStore
        from trading_bot.futures_store import FuturesStore
        from trading_bot.unified_worker import process_market
        now=time.time();spot=RuntimeStore(Path(self.tmp.name)/'spot.db',{'mode':'paper','interval':'15m'})
        future=FuturesStore(Path(self.tmp.name)/'future.db')
        try:
            for i,(market,store,key) in enumerate((('spot',spot,'selected_interval'),('futures',future,'interval'))):
                uid=10+i*10;self.q.receive(uid,True,'/mode '+market,now=now)
                self.q.receive(uid+1,True,'/timeframe',now=now);process_market(self.q,None,store,market)
                button=next(k['callback_data'] for k in self.keyboard()[0] if k['text']=='1m')
                self.q.receive(uid+2,True,'',now=now+1,callback_data=button)
                with store.transaction():store.set(key,'5m')
                process_market(self.q,None,store,market)
                self.assertEqual(store.get(key),'5m')
                table=getattr(store,'outbox_table','runtime_outbox')
                message=store.db.execute('select message from '+table+' order by id desc').fetchone()[0]
                self.assertIn('изменилась',message)
        finally:spot.close();future.close()

    def test_spot_setting_checked_after_waiting_for_financial_sqlite_lock(self):
        import sqlite3
        from trading_bot.runtime_store import RuntimeStore
        from trading_bot.unified_worker import process_market
        now=time.time();path=Path(self.tmp.name)/'spot.db';spot=RuntimeStore(path,{'mode':'paper','interval':'15m'})
        try:
            self.q.receive(1,True,'/timeframe',now=now);process_market(self.q,None,spot,'spot')
            button=next(k['callback_data'] for k in self.keyboard()[0] if k['text']=='1m')
            self.q.receive(2,True,'',now=now+1,callback_data=button)
            lock=sqlite3.connect(path,isolation_level=None);lock.execute('BEGIN IMMEDIATE')
            lock.execute("INSERT OR REPLACE INTO runtime_meta VALUES('selected_interval',?)",(json.dumps('5m'),))
            script='''import sys\nfrom trading_bot.runtime_store import RuntimeStore\nfrom trading_bot.unified_queue import UnifiedQueue\nfrom trading_bot.unified_worker import process_market\ns=RuntimeStore(sys.argv[1],{'mode':'paper','interval':'15m'});q=UnifiedQueue(sys.argv[2],allow_pair_experiment=True)\ntry:process_market(q,None,s,'spot')\nfinally:s.close();q.close()\n'''
            child=subprocess.Popen([sys.executable,'-B','-c',script,str(path),str(self.path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                time.sleep(.4);self.assertIsNone(child.poll());lock.commit()
                _,err=child.communicate(timeout=10);self.assertEqual(child.returncode,0,err.decode())
            finally:
                if lock.in_transaction:lock.rollback()
                lock.close()
                if child.poll() is None:child.kill();child.wait()
            self.assertEqual(spot.get('selected_interval'),'5m')
            self.assertEqual(self.q.pending('spot'),[])
        finally:spot.close()

    def test_concurrent_confirm_and_cancel_commit_at_most_one_action(self):
        now=time.time();pos={'id':'p','quantity':'1','symbol':'BTCUSDT'}
        self.q.receive(1,True,'/close',now=now,positions={'spot':pos});self.q.add_proposal(1,'spot','p','1',now=now)
        keys=self.keyboard()[0];barrier=threading.Barrier(2);errors=[]
        def work(i,key):
            q=UnifiedQueue(self.path,allow_pair_experiment=True)
            try:barrier.wait();q.receive(i,True,'',now=now+1,callback_data=key)
            except ValueError as e:errors.append(str(e))
            finally:q.close()
        threads=[threading.Thread(target=work,args=(2+i,k['callback_data'])) for i,k in enumerate(keys)]
        for x in threads:x.start()
        for x in threads:x.join(10)
        self.assertTrue(all(not x.is_alive() for x in threads))
        self.assertLessEqual(len(self.q.pending('spot')),1)
        self.assertTrue(all(e=='Update precedes committed offset' for e in errors))

    def test_outbox_sends_markup_and_cleanup_preserves_actions(self):
        self.q.receive(1,True,'/mode',now=time.time())
        self.t.flush(self.q)
        sent=[payload for method,payload in self.api.calls if method=='sendMessage']
        self.assertEqual(len(sent),1);self.assertEqual(len(sent[0]['reply_markup']['inline_keyboard']),4)
        self.assertEqual(self.q.db.execute("select count(*) from unified_state where key like 'inline-outbox:%'").fetchone()[0],0)
        self.assertEqual(self.q.db.execute("select count(*) from unified_state where key like 'inline-action:%'").fetchone()[0],4)

    def test_expired_sent_menu_is_removed_even_after_restart(self):
        now=time.time();self.q.receive(1,True,'/mode',now=now-121);self.t.flush(self.q)
        self.q.close();self.q=UnifiedQueue(self.path,allow_pair_experiment=True)
        self.t.flush(self.q)
        self.assertTrue(any(method=='editMessageReplyMarkup' for method,_ in self.api.calls))
        self.assertEqual(self.q.db.execute("select count(*) from unified_state where key like 'inline-sent:%'").fetchone()[0],0)

    def test_callback_process_death_before_and_after_commit(self):
        now=time.time();position={'id':'p','quantity':'1','symbol':'BTCUSDT'}
        self.q.receive(1,True,'/close',now=now,positions={'spot':position});self.q.add_proposal(1,'spot','p','1',now=now)
        token=self.keyboard()[0][0]['callback_data']
        script='''import os,sys,time\nfrom trading_bot.unified_queue import UnifiedQueue\nq=UnifiedQueue(sys.argv[1],allow_pair_experiment=True)\ndef fault(point):\n if point==sys.argv[3]:os._exit(73)\nq.fault=fault\nq.receive(2,True,"",now=float(sys.argv[4]),callback_data=sys.argv[2])\n'''
        for point,expected in (('before_commit',0),('after_commit',1)):
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),token,point,str(now+1)],capture_output=True,timeout=20)
            self.assertEqual(child.returncode,73,child.stderr.decode());self.assertEqual(len(self.q.pending('spot')),expected)
        self.q.receive(2,True,'',now=now+2,callback_data=token)
        self.assertEqual(len(self.q.pending('spot')),1)
