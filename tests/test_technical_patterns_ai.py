"""Temporal boundaries and model/LLM admission boundaries, not network tests."""
import copy,hashlib,json,unittest
from unittest.mock import patch
import numpy as np
from research import technical_patterns as t
from research.pattern_ai_analyze import analyze

class PatternAIContract(unittest.TestCase):
    def bundle(self):
        leaf=lambda value:[{'feature':[-2],'threshold':[-2.],'left':[-1],'right':[-1],'value':[value]}]
        return {'feature_version':t.VERSION,'features':t.FEATURES,'venue':'binance-usdm','valid_from_ms':1000,'valid_until_ms':10000,
                'train_label_end_ms':999,'classifier':leaf(.7),'regressor':leaf(.001),'model_id':'unit-model'}
    def test_inference_never_grants_trading(self):
        z=t.infer(self.bundle(),np.zeros(34),symbol='BTCUSDT',signal_ms=2000,now_ms=2001)
        self.assertEqual(z['action'],'CANDIDATE');self.assertFalse(z['trade_allowed'])
    def test_stale_model_rejected(self):
        with self.assertRaisesRegex(ValueError,'model_stale'):t.infer(self.bundle(),np.zeros(34),symbol='BTCUSDT',signal_ms=10000,now_ms=10000)
    def test_future_training_rejected(self):
        b=self.bundle();b['train_label_end_ms']=2000
        with self.assertRaisesRegex(ValueError,'model_stale'):t.infer(b,np.zeros(34),symbol='BTCUSDT',signal_ms=2000,now_ms=2000)
    def test_stale_market_analysis_rejected(self):
        with self.assertRaisesRegex(ValueError,'analysis_stale'):t.infer(self.bundle(),np.zeros(34),symbol='BTCUSDT',signal_ms=2000,now_ms=902000)
    def test_nan_input_rejected(self):
        x=np.zeros(34);x[0]=np.nan
        with self.assertRaisesRegex(ValueError,'features_invalid'):t.infer(self.bundle(),x,symbol='BTCUSDT',signal_ms=2000,now_ms=2001)
    def test_spot_model_cannot_be_used_as_futures(self):
        b=self.bundle();b['venue']='binance-spot'
        with self.assertRaisesRegex(ValueError,'model_market'):t.infer(b,np.zeros(34),symbol='BTCUSDT',signal_ms=2000,now_ms=2001)
    def test_future_truncation_invariance(self):
        n=4800;rng=np.random.default_rng(9);c=100*np.exp(np.cumsum(rng.normal(0,.0001,n)))
        a=np.column_stack([c,c*1.001,c*.999,c]);v=np.ones(n)
        full,_,_=t.frame(a,v,0);short,_,_=t.frame(a[:2400],v[:2400],0)
        np.testing.assert_allclose(full[:160],short,equal_nan=True)
    def enriched_snapshot(self):
        s={'snapshot_id':'unit','symbol':'BTCUSDT','venue':'binance-usdm','as_of_ms':900000,'latest_4h_ms':0,'latest_1h_ms':0,'latest_15m_ms':0,'features':{},'book':{},'news':[],'coverage':'unit'}
        s['technical_context']={'venue':'binance-usdm','symbol':'BTCUSDT','as_of_ms':900000,'signal_ms':900000,'feature_version':t.VERSION,'limits':{'orders_enabled':False},'detected_patterns':['pin_long']}
        return s
    def test_actual_prompt_includes_computed_patterns(self):
        from research.adaptive_ai_executor import request
        r=request(self.enriched_snapshot());self.assertIn('pin_long',r['input'][-1]['content']);self.assertIn('Do not invent patterns',r['input'][0]['content'])
    def test_packet_cannot_authorize_orders(self):
        from research.adaptive_ai_executor import request
        s=self.enriched_snapshot();s['technical_context']['limits']['orders_enabled']=True
        with self.assertRaisesRegex(ValueError,'technical_snapshot_contract'):request(s)
    def test_context_candle_cutoff_must_match(self):
        from research.adaptive_ai_executor import request
        s=self.enriched_snapshot();s['technical_context']['signal_ms']+=900000
        with self.assertRaisesRegex(ValueError,'technical_snapshot_time'):request(s)
    def test_context_asof_must_match(self):
        from research.adaptive_ai_executor import request
        s=self.enriched_snapshot();s['technical_context']['as_of_ms']+=1000
        with self.assertRaisesRegex(ValueError,'technical_snapshot_time'):request(s)
    def test_request_build_never_sends_http(self):
        from research.adaptive_ai_executor import request
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')):req=request(self.enriched_snapshot())
        self.assertEqual(req['model'],'gpt-6-luna');self.assertEqual(req['max_output_tokens'],2000)

class LegacyTechnicalRequest(unittest.TestCase):
    def snapshot(self):
        s={'snapshot_id':'unit','symbol':'BTCUSDT','venue':'binance-usdm','as_of_ms':900000,'latest_4h_ms':0,'latest_1h_ms':0,'latest_15m_ms':0,'features':{},'book':{},'news':[],'coverage':'unit'}
        return s
    def test_old_request_stays_without_technical_message(self):
        from research.adaptive_ai_executor import request
        self.assertEqual(len(request(self.snapshot())['input']),2)
    def test_enriched_request_preserves_original_response_schema(self):
        from research.adaptive_ai_executor import request,SCHEMA
        s=self.snapshot();s['technical_context']={'venue':'binance-usdm','symbol':'BTCUSDT','as_of_ms':900000,'signal_ms':900000,'feature_version':t.VERSION,'limits':{'orders_enabled':False}}
        r=request(s);self.assertEqual(len(r['input']),3);self.assertEqual(r['text']['format']['schema'],SCHEMA)
    def test_cross_venue_context_rejected(self):
        from research.adaptive_ai_executor import request
        s=self.snapshot();s['technical_context']={'venue':'binance-spot','symbol':'BTCUSDT'}
        with self.assertRaisesRegex(ValueError,'technical_snapshot_market'):request(s)

class ExistingBudgetTechnicalIntegration(unittest.TestCase):
    def test_same_journal_usage_and_one_attempt_with_enriched_request(self):
        from tests.test_adaptive_regular import RegularTests
        from research.adaptive_ai_executor import execute_reserved
        from research.adaptive_shadow import canonical
        fixture=RegularTests('test_success_same_snapshot_and_idempotent');fixture.setUp();self.addCleanup(fixture.doCleanups)
        s=fixture.snap;s['venue']='binance-usdm';signal=s['latest_15m_ms']+900000
        s['technical_context']={'venue':'binance-usdm','symbol':'BTCUSDT','as_of_ms':s['as_of_ms'],'signal_ms':signal,
                                'feature_version':t.VERSION,'limits':{'orders_enabled':False}}
        s.pop('snapshot_id');s['snapshot_id']=hashlib.sha256(canonical(s).encode()).hexdigest()
        def send(db,rid,req,clock):
            self.assertEqual(len(req['input']),3)
            return execute_reserved(db,rid,req,transport=fixture.fake,clock=clock)
        first=fixture.tick(send=send);self.assertEqual(first['status'],'completed')
        self.assertEqual(fixture.tick(send=send)['status'],'completed');self.assertEqual(len(fixture.calls),1)
        import sqlite3
        with sqlite3.connect(fixture.p/'experiment-budget.db') as db:
            self.assertEqual(db.execute('select count(*) from api_attempts').fetchone()[0],3)
            self.assertEqual(db.execute('select count(*) from api_usage').fetchone()[0],1)
            self.assertEqual(db.execute("select reserved_micro_usd from api_attempts where request_id='old'").fetchone()[0],3630)
        self.assertEqual((fixture.p/'interactive-once.lock').read_text(),'old')

class TechnicalTelegramPanel(unittest.TestCase):
    def packet(self):
        return {'feature_version':t.VERSION,'venue':'binance-usdm','symbol':'BTCUSDT','signal_ms':900000,'as_of_ms':901000,'expires_ms':1800000,
                'features':{'rsi':.55,'adx':.3,'atr_pct':.004,'volume_ratio':1.2},'detected_patterns':['pin_long'],
                'ml':{'action':'NO_TRADE','trade_allowed':False,'p_win_estimate':.4,'expected_stress_net_return':-.001}}
    def render(self,packet,now=902):
        import tempfile
        from pathlib import Path
        from trading_bot.technical_ai_view import render
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'technical.json';p.write_text(json.dumps(packet));return render(p,now)
    def test_model_named_separately_from_gpt(self):
        text=self.render(self.packet());self.assertIn('не ответ GPT',text);self.assertIn('бычий пинбар',text);self.assertIn('вход не подтверждён',text)
    def test_stale_panel_cannot_look_current(self):
        self.assertIn('УСТАРЕЛО',self.render(self.packet(),1801))
    def test_any_trading_grant_rejected(self):
        p=self.packet();p['ml']['trade_allowed']=True;self.assertIn('проверенного снимка нет',self.render(p))
    def test_unknown_pattern_rejected(self):
        p=self.packet();p['detected_patterns']=['invented'];self.assertIn('проверенного снимка нет',self.render(p))


class PatternLabelBoundaries(unittest.TestCase):
    def run_label(self,trade,funding=None):
        from research import pattern_ai_experiment as e
        with patch.object(e.r,'START',0),patch.object(e.r,'END',len(trade)*60000):
            return e.outcome(0,1,.001,'BTCUSDT',trade,trade,[] if funding is None else funding,False)
    def test_same_minute_stop_before_profit(self):
        a=np.tile([74000.,77000.,71000.,74000.],(300,1));z=self.run_label(a)
        self.assertEqual(z['reason'],'stop');self.assertLess(z['net'],0)
    def test_gap_uses_adverse_open(self):
        a=np.tile([74000.,74100.,73900.,74000.],(300,1));a[2]=[72000.,72100.,71900.,72000.]
        z=self.run_label(a);self.assertEqual(z['reason'],'stop');self.assertEqual(z['exit_reference'],72000)
    def test_uncertain_funding_credit_not_invented(self):
        a=np.tile([74000.,74001.,73999.,74000.],(300,1));z=self.run_label(a,[(241*60000,-.0001)])
        self.assertEqual(z['reason'],'time');self.assertEqual(z['funding'],0)

class TechnicalPublication(unittest.TestCase):
    packet=TechnicalTelegramPanel.packet
    def test_publication_freshness_and_monotonicity(self):
        import tempfile,os
        from pathlib import Path
        from research.adaptive_server_publish import publish_technical
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'source.json';target=p/'target.json'
            source.write_text(json.dumps(self.packet()))
            self.assertTrue(publish_technical(source,target,now=902,gid=os.getgid()))
            self.assertFalse(publish_technical(source,target,now=902,gid=os.getgid()))
            old=self.packet();old['as_of_ms']=900999;source.write_text(json.dumps(old))
            with self.assertRaisesRegex(ValueError,'not_newer'):publish_technical(source,target,now=902,gid=os.getgid())
    def test_stale_technical_publication_rejected(self):
        import tempfile,os
        from pathlib import Path
        from research.adaptive_server_publish import publish_technical
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'source.json';source.write_text(json.dumps(self.packet()))
            with self.assertRaisesRegex(ValueError,'invalid_or_stale'):publish_technical(source,p/'target.json',now=1801,gid=os.getgid())

if __name__=='__main__':unittest.main()
