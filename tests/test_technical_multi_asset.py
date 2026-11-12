import contextlib,copy,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_technical_plan_history as fixtures
from research.technical_assets import SYMBOLS,sidecar,symbol,history_matches
from research.technical_trade_scenarios import build as plan
from research.technical_plan_history import advance


class MultiAssetTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.model=self.root/'model.json';self.model.write_text(json.dumps({'model_id':'model-1'}))
        self.now=1020000;self.fixture=fixtures.PlanHistoryTest()

    def raw(self,asset):
        value=self.fixture.capture();value['symbol']=asset
        return value

    def packet(self,asset):
        from research.technical_plan_history import digest
        p=self.fixture.packet();p['symbol']=asset;p['trade_scenarios']=plan(p)
        p['packet_id']=digest(p)
        return p

    def run_server(self,extras=True,fail=None,paid=False,repeat=1):
        from research import pattern_ai_server as s
        argv=['server','--model',str(self.model),'--technical-output',str(self.root/'technical.json')]
        if extras:argv+=['--extra-symbols','ETHUSDT','SOLUSDT']
        if paid:argv+=['--paid-existing-budget','--paid-boundary-ms','0','--state','existing-only','--gpt-output',str(self.root/'report.json')]
        calls=[];paid_assets=[];events=[]
        def capture(asset,**kwargs):
            calls.append(asset);events.append('capture:'+asset)
            if fail and asset in fail:raise ValueError(fail[asset])
            return self.raw(asset)
        def build(raw,bundle,now):return {'technical_context':self.packet(raw['symbol'])},'RANGE'
        def tick(state,out,*,fetch):
            self.assertEqual(state,'existing-only');events.append('paid')
            snapshot,_=fetch();paid_assets.append(snapshot['technical_context']['symbol'])
            return {'status':'completed'}
        output=io.StringIO()
        with patch('sys.argv',argv),patch.object(s.time,'time',return_value=self.now/1000),patch.object(s,'capture',side_effect=capture),patch.object(s,'build',side_effect=build),patch('research.adaptive_regular.tick',side_effect=tick) as budget,patch.object(s,'paid_boundary_allowed',return_value=paid),contextlib.redirect_stdout(output):
            for _ in range(repeat):s.main()
        return [json.loads(x) for x in output.getvalue().splitlines()],calls,paid_assets,events,budget.call_count

    def test_only_allowlisted_symbols_and_compatible_btc_paths(self):
        self.assertEqual(sidecar(self.root,'BTC'),self.root/'technical.json')
        self.assertEqual(sidecar(self.root,'ETH'),self.root/'technical-ETHUSDT.json')
        self.assertEqual(sidecar(self.root,'SOL','technical-history'),self.root/'technical-history-SOLUSDT.json')
        self.assertEqual(symbol('ethusdt'),'ETHUSDT')
        for value in ('XRPUSDT','../ETHUSDT','ETH/USDT',None):
            with self.assertRaises(ValueError):sidecar(self.root,value)

    def test_default_stays_btc_and_same_bar_all_assets_cached(self):
        results,calls,paid,events,count=self.run_server(extras=False,repeat=2)
        self.assertEqual(calls,['BTCUSDT']);self.assertEqual(paid,[]);self.assertEqual(count,0)
        self.assertNotIn('extra_local',results[0])
        self.assertFalse(sidecar(self.root,'ETH').exists())
        results,calls,paid,events,count=self.run_server(repeat=2)
        self.assertEqual(calls,['ETHUSDT','SOLUSDT']);self.assertEqual(count,0)
        for asset in SYMBOLS:
            self.assertEqual(json.loads(sidecar(self.root,asset).read_text())['symbol'],asset)
            h=json.loads(sidecar(self.root,asset,'technical-history').read_text())
            self.assertEqual(h['symbol'],asset);self.assertEqual(len(h['records']),1)
            self.assertEqual(h['records'][0]['packet']['symbol'],asset)

    def test_symbol_failure_does_not_fall_back_or_block_other_assets(self):
        results,calls,paid,events,count=self.run_server(fail={'ETHUSDT':'candle_gap'})
        self.assertEqual(calls,list(SYMBOLS));self.assertTrue(sidecar(self.root,'SOL').exists())
        self.assertFalse(sidecar(self.root,'ETH').exists())
        self.assertEqual(results[0]['extra_local']['ETHUSDT']['status'],'local_analysis_unavailable')
        self.assertEqual(count,0)

    def test_btc_data_failure_keeps_free_extras_without_paid_retry(self):
        results,calls,paid,events,count=self.run_server(fail={'BTCUSDT':'candle_gap'},paid=True)
        self.assertEqual(calls,list(SYMBOLS));self.assertEqual(count,0)
        self.assertEqual(results[0]['gpt']['reason'],'btc_analysis_unavailable')
        self.assertTrue(sidecar(self.root,'SOL').exists())

    def test_host_denial_stops_remaining_requests(self):
        for asset,expected in (('BTCUSDT',['BTCUSDT']),('ETHUSDT',['BTCUSDT','ETHUSDT'])):
            # Isolated directory so the cache cannot conceal a denied capture.
            for p in self.root.glob('technical*.json'):p.unlink()
            results,calls,paid,events,count=self.run_server(fail={asset:'public_market_unavailable_http451'})
            self.assertEqual(calls,expected)
            self.assertEqual(results[0]['extra_local']['SOLUSDT']['status'],'source_access_denied')
            self.assertEqual(count,0)

    def test_paid_path_remains_single_btc_and_precedes_extra_capture(self):
        results,calls,paid,events,count=self.run_server(paid=True)
        self.assertEqual(count,1);self.assertEqual(paid,['BTCUSDT'])
        self.assertEqual(calls,list(SYMBOLS))
        self.assertLess(events.index('paid'),events.index('capture:ETHUSDT'))
        self.assertFalse(results[0]['extra_local']['ETHUSDT']['gpt_called'])

    def test_wrong_market_capture_rejected_without_output(self):
        from research import pattern_ai_server as s
        from types import SimpleNamespace
        args=SimpleNamespace(technical_output=str(self.root/'technical.json'),derivatives_context=False)
        with patch.object(s.time,'time',return_value=self.now/1000),patch.object(s,'capture',return_value=self.raw('BTCUSDT')):
            with self.assertRaisesRegex(ValueError,'capture_market'):s.extra_local('ETHUSDT',args,{'model_id':'model-1'})
        self.assertFalse(sidecar(self.root,'ETH').exists())

    def test_btc_legacy_history_keeps_start_and_cannot_become_eth(self):
        p=self.packet('BTCUSDT');old=advance(None,p,self.raw('BTCUSDT'),self.now);old.pop('symbol')
        self.assertEqual(history_matches(old,'BTCUSDT'),old)
        resumed=advance(old,p,self.raw('BTCUSDT'),self.now)
        self.assertEqual(resumed['started_ms'],old['started_ms'])
        self.assertEqual(len(resumed['records']),1)
        with self.assertRaisesRegex(ValueError,'history_market'):advance(old,self.packet('ETHUSDT'),self.raw('ETHUSDT'),self.now)

    def test_read_only_views_select_asset_and_never_reuse_btc_file(self):
        from trading_bot.ai_screens import render_screen
        sidecar(self.root,'BTC').write_text(json.dumps(self.packet('BTCUSDT')))
        sidecar(self.root,'ETH').write_text(json.dumps(self.packet('ETHUSDT')))
        report=self.root/'report.json'
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            self.assertIn('ETHUSDT',render_screen('patterns',report,self.now/1000,symbol='ETH'))
            self.assertIn('ETHUSDT',render_screen('plan',report,self.now/1000,symbol='ETH'))
            self.assertIn('проверенного снимка нет',render_screen('patterns',report,self.now/1000,symbol='SOL'))
            self.assertIn('только к BTCUSDT',render_screen('main',report,self.now/1000,symbol='ETH'))
            self.assertIn('не поддерживается',render_screen('patterns',report,self.now/1000,symbol='../../BTC'))
        sidecar(self.root,'ETH').write_text(json.dumps(self.packet('BTCUSDT')))
        self.assertIn('проверенного снимка нет',render_screen('patterns',report,self.now/1000,symbol='ETH'))

    def test_publisher_market_binding_and_separate_paths(self):
        from research.adaptive_server_publish import publish_technical,publish_history
        private=self.root/'private';public=self.root/'public';private.mkdir();public.mkdir()
        for asset in SYMBOLS:
            p=self.packet(asset);source=sidecar(private,asset);target=sidecar(public,asset);source.write_text(json.dumps(p))
            self.assertTrue(publish_technical(source,target,now=self.now/1000,gid=os.getgid(),symbol=asset))
            h=advance(None,p,self.raw(asset),self.now)
            src=sidecar(private,asset,'technical-history');out=sidecar(public,asset,'technical-history');src.write_text(json.dumps(h))
            self.assertTrue(publish_history(src,out,now=self.now/1000,gid=os.getgid(),symbol=asset))
        with self.assertRaisesRegex(ValueError,'market'):publish_technical(sidecar(private,'BTC'),sidecar(public,'ETH'),now=self.now/1000,gid=os.getgid(),symbol='ETHUSDT')
        with self.assertRaisesRegex(ValueError,'market'):publish_history(sidecar(private,'BTC','technical-history'),sidecar(public,'ETH','technical-history'),now=self.now/1000,gid=os.getgid(),symbol='ETHUSDT')

    def test_duplicate_and_unknown_symbols_fail_before_capture(self):
        from research import pattern_ai_server as s
        for symbols in (['ETHUSDT','ETHUSDT'],['XRPUSDT']):
            argv=['server','--model','absent','--technical-output','absent','--extra-symbols',*symbols]
            with patch('sys.argv',argv),patch.object(s,'capture',side_effect=AssertionError('network')),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):s.main()

    def test_existing_model_asset_features_distinguish_all_three_markets(self):
        import numpy as np
        from research.technical_patterns import vector
        for index,asset in enumerate(SYMBOLS):
            x=vector(np.zeros(30),-1,asset)
            self.assertEqual(x[-4],-1)
            self.assertEqual(x[-3:].tolist(),[float(i==index) for i in range(3)])

    def test_bad_cache_shape_is_recollected_without_cross_asset_fallback(self):
        sidecar(self.root,'ETH').write_text('[]')
        results,calls,paid,events,count=self.run_server()
        self.assertEqual(calls,list(SYMBOLS))
        self.assertEqual(json.loads(sidecar(self.root,'ETH').read_text())['symbol'],'ETHUSDT')
        self.assertEqual(count,0)

    def test_maturation_window_defers_all_assets_without_collection(self):
        self.now=930000
        results,calls,paid,events,count=self.run_server()
        self.assertEqual(calls,[]);self.assertEqual(count,0)
        for item in results[0]['extra_local'].values():self.assertEqual(item['status'],'waiting_for_mature_closed_bar')
