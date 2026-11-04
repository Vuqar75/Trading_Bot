import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_store import FuturesStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class PairServiceWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        identity={'schema':1,'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10','account':'paper'}
        s=RuntimeStore(self.root/'spot.db',identity);s.close()
        f=FuturesStore(self.root/'futures.db');f.close()
        self.financial=self.root/'shared.db';self.queue=self.root/'queue.db'
        consolidate_snapshots(self.root/'spot.db',self.root/'futures.db',self.financial,captured_at=1)
        q=UnifiedQueue(self.queue,allow_pair_experiment=True);q.close()

    def confirmed(self,market):
        now=time.time();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        try:
            q.receive(1,True,'/mode '+market,now=now)
            q.receive(2,True,'/pair SOLUSDT',now=now,pairs={market:{'symbols':['BTCUSDT'],'version':0}})
            token=q.pair_proposals()[0]['id']
            q.receive(3,True,'/confirm '+token,now=now)
        finally:q.close()

    def test_protected_launcher_passes_only_explicit_pair_flag_in_shared_paper(self):
        home=self.root/'fixture-home';config=home/'.config/trading-bot';config.mkdir(parents=True)
        (home/'Trading_Bot/data').mkdir(parents=True)
        login=config/'telegram.json';login.write_text(json.dumps({'token':'FAKE','owner':'1'}));login.chmod(0o600)
        output=self.root/'exec-args.json';launcher=Path(__file__).resolve().parents[1]/'deploy/cloud_paper_launch.py'
        script='''import json,os,runpy,sys\nlauncher,output=sys.argv[1:3]\nsys.argv=[launcher,"--experimental-pair"]\ndef fake_exec(executable,args):\n open(output,"w").write(json.dumps(args))\nos.execv=fake_exec\nrunpy.run_path(launcher,run_name="__main__")\n'''
        env=dict(os.environ,HOME=str(home),SHARED_PAPER_DB=str(self.financial));env.pop('FUTURES_OUTBOX_DB',None)
        child=subprocess.run([sys.executable,'-B','-c',script,str(launcher),str(output)],env=env,capture_output=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr.decode())
        args=json.loads(output.read_text());self.assertIn('--shared-paper',args);self.assertIn('--experimental-pair',args)
        output.unlink();env.pop('SHARED_PAPER_DB')
        child=subprocess.run([sys.executable,'-B','-c',script,str(launcher),str(output)],env=env,capture_output=True,timeout=10)
        self.assertNotEqual(child.returncode,0);self.assertFalse(output.exists())

    def confirmed_set(self,market):
        now=time.time();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        try:
            q.receive(1,True,'/mode '+market,now=now)
            q.receive(2,True,'/pair BTCUSDT SOLUSDT',now=now,
                      pairs={market:{'symbols':['BTCUSDT','ETHUSDT'],'version':0}})
            token=q.pair_proposals()[0]['id']
            q.receive(3,True,'/confirm '+token,now=now)
        finally:q.close()

    def test_spot_main_processes_confirmed_pair_without_network(self):
        self.confirmed('spot')
        from trading_bot import app
        class Reader:
            def get(self,path,params=None):
                symbol=(params or {}).get('symbol','BTCUSDT');now=int(time.time()*1000)
                if path.endswith('/time'):return {'serverTime':now}
                if path.endswith('exchangeInfo'):return {'symbols':[spot_info(symbol[:-4])]}
                if path.endswith('ticker/price'):return {'symbol':symbol,'price':'100'}
                if path.endswith('/klines'):
                    period=900000;last=(now//period-1)*period
                    return [[last-(30-i)*period,0,0,0,'100',0,last-(29-i)*period-1] for i in range(31)]
                raise AssertionError('Unexpected request')
        class Telegram:
            def __init__(self,*args,**kwargs):pass
            def poll(self,*args):pass
            def flush(self,*args):pass
        args=['app','--shared-paper','--experimental-pair','--once','--telegram',
              '--db',str(self.financial),'--futures-outbox-db',str(self.financial)]
        with patch.dict(os.environ,{'UNIFIED_PAPER_QUEUE':str(self.queue)}),patch('sys.argv',args),\
             patch.object(app,'SpotTestnetReader',Reader),patch.object(app,'TelegramTransport',Telegram),\
             patch.object(app.signal,'signal',lambda *a:None):
            app.main();app.main()
        shared=SharedPaperStore(self.financial)
        try:
            self.assertEqual(shared._get('spot','identity')['symbol'],'SOLUSDT')
            self.assertEqual(shared._get('spot','quote')['symbol'],'SOLUSDT')
            self.assertTrue(shared.snapshot('spot')['paused']);self.assertTrue(shared.ledger.reconcile()['balanced'])
        finally:shared.close()
        q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        try:self.assertEqual(q.pending('spot'),[])
        finally:q.close()

    def test_fake_telegram_to_spot_main_and_restart(self):
        from trading_bot import app
        class Reader:
            @staticmethod
            def _http(request):raise AssertionError('Network is disabled in fixture')
            def get(self,path,params=None):
                symbol=(params or {}).get('symbol','BTCUSDT');now=int(time.time()*1000)
                if path.endswith('/time'):return {'serverTime':now}
                if path.endswith('exchangeInfo'):return {'symbols':[spot_info(symbol[:-4])]}
                if path.endswith('ticker/price'):return {'symbol':symbol,'price':'100'}
                if path.endswith('/klines'):
                    period=900000;last=(now//period-1)*period
                    return [[last-(30-i)*period,0,0,0,'100',0,last-(29-i)*period-1] for i in range(31)]
                raise AssertionError('Unexpected request')
        sends=[]
        def fake_call(transport,method,data):
            if method!='getUpdates':sends.append(method);return {}
            offset=data['offset']
            if offset==0:ident,text=1,'/pair SOLUSDT'
            elif offset==2:
                q=UnifiedQueue(self.queue,allow_pair_experiment=True)
                try:token=q.pair_proposals()[0]['id']
                finally:q.close()
                ident,text=2,'/confirm '+token
            else:return []
            return [{'update_id':ident,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},
                                                   'date':int(time.time()),'text':text}}]
        args=['app','--shared-paper','--experimental-pair','--once','--telegram',
              '--db',str(self.financial),'--futures-outbox-db',str(self.financial)]
        env={'UNIFIED_PAPER_QUEUE':str(self.queue),'RUNTIME_TELEGRAM_BOT_TOKEN':'FAKE','RUNTIME_TELEGRAM_OWNER_ID':'1'}
        with patch.dict(os.environ,env),patch('sys.argv',args),patch.object(app,'SpotTestnetReader',Reader),\
             patch.object(app.TelegramTransport,'call',fake_call),patch.object(app.signal,'signal',lambda *a:None):
            app.main();app.main();app.main();app.main()
        shared=SharedPaperStore(self.financial)
        try:
            self.assertEqual(shared._get('spot','identity')['symbol'],'SOLUSDT')
            self.assertEqual(shared._get('spot','quote')['symbol'],'SOLUSDT')
            self.assertTrue(shared.snapshot('spot')['paused']);self.assertTrue(shared.ledger.reconcile()['balanced'])
        finally:shared.close()
        self.assertTrue(sends)

    def test_isolated_main_processes_confirmed_pair_without_network(self):
        self.confirmed('futures')
        from trading_bot import futures_app
        class Reader:
            def __init__(self,*,symbol):self.pair_symbol=symbol
            def get(self,path):
                info=spot_info();info.update(contractType='PERPETUAL',marginAsset='USDT')
                return {'symbols':[info]}
            def mark(self):return Decimal('100'),int(time.time()*1000)
            def candles(self,interval,server):
                period=900000;last=(server//period-1)*period
                return [(last-(30-i)*period,Decimal('100')) for i in range(31)]
        args=['futures','--shared-paper','--experimental-pair','--once','--db',str(self.financial)]
        with patch.dict(os.environ,{'UNIFIED_PAPER_QUEUE':str(self.queue)}),patch('sys.argv',args),\
             patch.object(futures_app,'FuturesData',Reader),\
             patch.object(futures_app.signal,'signal',lambda *a:None):
            futures_app.main();futures_app.main()
        shared=SharedPaperStore(self.financial)
        try:
            self.assertEqual(shared._get('futures','identity')['symbol'],'SOLUSDT')
            self.assertEqual(shared._get('futures','quote')['symbol'],'SOLUSDT')
            self.assertTrue(shared.snapshot('futures')['paused']);self.assertTrue(shared.ledger.reconcile()['balanced'])
        finally:shared.close()
        q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        try:self.assertEqual(q.pending('futures'),[])
        finally:q.close()

    def test_cross_main_processes_confirmed_pair_without_network(self):
        shared=SharedPaperStore(self.financial);CrossPaperBook(shared);shared.close()
        self.confirmed_set('futures_cross')
        from trading_bot import cross_app
        class Reader:
            def __init__(self,*,symbols,allow_pair_experiment):self.symbols=symbols
            def get(self,path):
                rows=[]
                for base in ('BTC','SOL'):
                    info=spot_info(base);info.update(contractType='PERPETUAL',marginAsset='USDT');rows.append(info)
                return {'symbols':rows}
            def schedule(self,symbol):
                now=int(time.time()*1000)
                return {'symbol':symbol,'mark':'100','server_ms':now,'next_due_ms':now+28800000,'interval_ms':28800000}
            def candles(self,symbol,interval,server):
                period=900000;last=(server//period-1)*period
                return [(last-(30-i)*period,Decimal('100')) for i in range(31)]
        args=['cross','--experimental-pair','--once','--db',str(self.financial),'--queue',str(self.queue)]
        with patch('sys.argv',args),patch.object(cross_app,'CrossPublicData',Reader),\
             patch.object(cross_app.signal,'signal',lambda *a:None):
            cross_app.main()
        shared=SharedPaperStore(self.financial)
        try:
            from trading_bot.paper_pair_runtime_offline import restore_cross_book
            self.assertEqual(restore_cross_book(shared)._symbols(),('BTCUSDT','SOLUSDT'))
            self.assertEqual(shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        finally:shared.close()

    def test_margin_main_processes_confirmed_pair_without_network(self):
        shared=SharedPaperStore(self.financial);MarginPaperBook(shared.ledger);shared.close()
        self.confirmed_set('margin_cross')
        from trading_bot import margin_app
        class Reader:
            HOST='https://api.binance.com'
            def __init__(self,*,experimental_pairs):self.pairs=experimental_pairs
            def get(self,path,params):return {'symbols':[spot_info(params['symbol'][:-4])]}
            def snapshot(self,symbol,interval='15m'):
                now=int(time.time()*1000);period=900000;last=(now//period-1)*period
                filters={'lot_rules':[{'min':'0','max':'0','step':'.001'}],'min_notional':'0','max_notional':'0','observed_ms':now}
                return {'price':'100','time_ms':now,'server_ms':now,'candle_server_ms':now,'source':self.HOST,
                        'price_kind':'last_public_trade','filters':filters,
                        'candles':[(last-(30-i)*period,Decimal('100')) for i in range(31)]}
        args=['margin','--experimental-pair','--once','--db',str(self.financial),'--queue',str(self.queue)]
        with patch('sys.argv',args),patch.object(margin_app,'MarginSpotData',Reader),\
             patch.object(margin_app.signal,'signal',lambda *a:None):
            margin_app.main()
        shared=SharedPaperStore(self.financial)
        try:
            from trading_bot.paper_pair_runtime_offline import restore_margin_book
            self.assertEqual(restore_margin_book(shared).tradable,frozenset(('BTCUSDT','SOLUSDT')))
            self.assertEqual(shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        finally:shared.close()
