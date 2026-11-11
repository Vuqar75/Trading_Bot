"""Conditional arithmetic checked independently and against existing label rules."""
import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from research.technical_trade_scenarios import build,validate


class ScenarioTest(unittest.TestCase):
    def packet(self,side='LONG',atr=.001):
        p={'feature_version':'technical-patterns-v1','symbol':'BTCUSDT','venue':'binance-usdm',
           'signal_ms':900000,'as_of_ms':901000,'expires_ms':1800000,'side':side,'close':100,
           'features':{'atr_pct':atr,'rsi':.55,'adx':.3,'volume_ratio':1.2},
           'detected_patterns':['pin_'+side.lower()] if side!='WAIT' else [],
           'limits':{'orders_enabled':False},'ml':{'action':'NO_TRADE','trade_allowed':False}}
        return p

    def test_long_prices_and_independent_net_arithmetic(self):
        p=self.packet();d=build(p);base=d['scenarios'][0]
        self.assertEqual(Decimal(base['assumed_entry']),Decimal('100.05'))
        self.assertEqual(Decimal(base['stop_reference']),Decimal('99.54975'))
        self.assertEqual(Decimal(base['target_reference']),Decimal('101.0505'))
        # Quantity=1 BTC only for this normalized synthetic 100-USDT example.
        exit_fill=Decimal('101.0505')*Decimal('.9995')
        fees=(Decimal('100.05')+exit_fill)*Decimal('.0005')
        expected=exit_fill-Decimal('100.05')-fees
        self.assertEqual(Decimal(base['net_at_target_usdt_per_100']),expected)
        self.assertLess(Decimal(base['net_reward_risk']),2)
        self.assertGreater(Decimal(base['two_outcome_breakeven_win_rate_pct']),Decimal(100)/3)
        self.assertFalse(d['funding_included']);self.assertFalse(d['notional_is_order_size'])
        self.assertFalse(d['orders_enabled']);self.assertFalse(d['model_candidate'])

    def test_short_prices_and_adverse_buyback(self):
        d=build(self.packet('SHORT'));x=d['scenarios'][0]
        entry=Decimal(x['assumed_entry']);stop=Decimal(x['stop_reference']);target=Decimal(x['target_reference'])
        self.assertLess(target,entry);self.assertGreater(stop,entry)
        exit_fill=target*Decimal('1.0005')
        self.assertEqual(Decimal(x['net_at_target_usdt_per_100']),entry-exit_fill-(entry+exit_fill)*Decimal('.0005'))
        self.assertLess(Decimal(x['net_at_stop_usdt_per_100']),0)

    def test_atr_stop_clip_matches_model_bounds(self):
        for atr,distance in ((.001,.5),(.004,.6),(.1,2)):
            self.assertEqual(Decimal(build(self.packet(atr=atr))['stop_distance_pct']),Decimal(str(distance)))

    def test_stress_costs_reduce_conditional_reward(self):
        for side in ('LONG','SHORT'):
            a,b=build(self.packet(side))['scenarios']
            self.assertLess(Decimal(b['net_at_target_usdt_per_100']),Decimal(a['net_at_target_usdt_per_100']))
            self.assertLess(Decimal(b['net_at_stop_usdt_per_100']),Decimal(a['net_at_stop_usdt_per_100']))

    def test_no_direction_does_not_invent_trade(self):
        p=self.packet('WAIT');self.assertEqual(build(p)['scenarios'],[])
        p['detected_patterns']=['pin_long','pin_short']
        self.assertEqual(build(p)['status'],'no_unambiguous_pattern')
        p['side']='LONG'
        with self.assertRaisesRegex(ValueError,'scenario_side'):build(p)

    def test_tampered_prices_costs_time_and_direction_rejected(self):
        p=self.packet();d=build(p)
        for key,value in (('reference_close','101'),('side','SHORT'),('orders_enabled',True)):
            bad=copy.deepcopy(d);bad[key]=value
            with self.assertRaisesRegex(ValueError,'scenario_binding'):validate(bad,p)
        bad=copy.deepcopy(d);bad['scenarios'][0]['net_at_target_usdt_per_100']='99'
        with self.assertRaisesRegex(ValueError,'scenario_binding'):validate(bad,p)
        for key,value in (('signal_ms',900001),('as_of_ms',1800000),('venue','binance-spot')):
            with self.assertRaises(ValueError):build(p|{key:value})

    def test_invalid_numbers_and_order_grants_rejected(self):
        for value in (True,'NaN','Infinity','1e1000000',0,-1):
            with self.assertRaises(ValueError):build(self.packet()|{'close':value})
        for value in (0,-.01,float('nan'),True):
            p=self.packet();p['features']['atr_pct']=value
            with self.assertRaises(ValueError):build(p)
        p=self.packet();p['ml']['trade_allowed']=True
        with self.assertRaisesRegex(ValueError,'scenario_admission'):build(p)
        p=self.packet();p['ml'].update(action='CANDIDATE',p_win_estimate=.5,expected_stress_net_return=.001)
        with self.assertRaisesRegex(ValueError,'scenario_model_gate'):build(p)

    def test_scenarios_match_existing_label_formula_both_sides(self):
        import numpy as np
        from research import pattern_ai_experiment as e
        for side in ('LONG','SHORT'):
            for stress in (False,True):
                plan=build(self.packet(side));x=plan['scenarios'][int(stress)]
                for reason,field in (('target','target_reference'),('stop','stop_reference')):
                    trade=np.tile([100.,100.01,99.99,100.],(300,1));mark=trade.copy()
                    index=(6 if stress else 1)+1
                    price=float(x[field]);column=1 if (side=='LONG')==(reason=='target') else 2
                    trade[index,column]=price*(1.00001 if column==1 else .99999)
                    with patch.object(e.r,'START',0),patch.object(e.data,'sizing',return_value=(1.,None)):
                        actual=e.outcome(0,1 if side=='LONG' else -1,.001,'BTCUSDT',trade,mark,[],stress)
                    self.assertEqual(actual['reason'],reason)
                    self.assertAlmostEqual(actual['net'],float(x['net_at_'+reason+'_usdt_per_100']),places=10)

    def test_render_is_read_only_and_stale_is_explicit(self):
        from trading_bot.technical_ai_view import render_plan
        p=self.packet();p['trade_scenarios']=build(p)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'technical.json';path.write_text(json.dumps(p));before=path.read_bytes()
            with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
                text=render_plan(path,now=902);stale=render_plan(path,now=1801)
            self.assertIn('LONG',text);self.assertIn('не открытая позиция',text)
            self.assertIn('100.05',text);self.assertIn('Funding не включён',text)
            self.assertIn('УСТАРЕЛО',stale);self.assertEqual(before,path.read_bytes())
            p['trade_scenarios']['orders_enabled']=True;path.write_text(json.dumps(p))
            self.assertIn('повреждён',render_plan(path,now=902))

    def test_old_snapshot_stays_without_fabricated_plan(self):
        from trading_bot.technical_ai_view import render_plan
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'technical.json';path.write_text(json.dumps(self.packet()))
            self.assertIn('не сохранён',render_plan(path,now=902))

    def test_llm_receives_validated_math_without_schema_change(self):
        from research.adaptive_ai_executor import request,SCHEMA
        from tests.test_technical_patterns_ai import LegacyTechnicalRequest
        snapshot=LegacyTechnicalRequest().snapshot();p=self.packet();snapshot['as_of_ms']=p['as_of_ms']
        p['trade_scenarios']=build(p);snapshot['technical_context']=p
        req=request(snapshot)
        self.assertEqual(req['text']['format']['schema'],SCHEMA)
        self.assertIn('net_reward_risk',req['input'][-1]['content'])
        self.assertIn('not approved sizing',req['input'][0]['content'])
        p['trade_scenarios']['scenarios'][0]['fee_bps_per_side']='0'
        with self.assertRaisesRegex(ValueError,'scenario_binding'):request(snapshot)

    def test_same_atr_and_close_scaling_keeps_normalized_returns(self):
        base=build(self.packet())
        other=build(self.packet()|{'close':75000})
        for a,b in zip(base['scenarios'],other['scenarios']):
            self.assertAlmostEqual(float(a['net_at_target_usdt_per_100']),float(b['net_at_target_usdt_per_100']),places=12)

    def test_full_capture_derivatives_request_and_publication(self):
        import os
        from tests.test_derivatives_context import DerivativesContext
        from research import technical_patterns as t
        from research.pattern_ai_snapshot import build as snapshot
        from research.adaptive_ai_executor import request
        from research.adaptive_server_publish import publish_technical
        from trading_bot.ai_screens import render_screen
        rows=[]
        for i in range(3200):
            p=100+i*.01;rows.append([i*900000,p,p+.1,p-.1,p+.02,10.])
        rows[-2][1]=rows[-2][4]+.05
        rows[-1][1]=rows[-2][4]-.1;rows[-1][4]=rows[-2][1]+.1
        rows[-1][2]=rows[-1][4]+.1;rows[-1][3]=rows[-1][1]-.1
        signal=len(rows)*900000;now=signal+120000
        leaf=lambda v:[{'feature':[-2],'threshold':[-2.],'left':[-1],'right':[-1],'value':[v]}]
        bundle={'feature_version':t.VERSION,'features':t.FEATURES,'venue':'binance-usdm','valid_from_ms':0,
                'valid_until_ms':signal+900000,'train_label_end_ms':1,'classifier':leaf(.7),'regressor':leaf(.001),'model_id':'fixture'}
        capture={'venue':'binance-usdm','symbol':'BTCUSDT','candles':rows,'derivatives':DerivativesContext().context(signal)}
        s,_=snapshot(capture,bundle,now);packet=s['technical_context'];req=request(s)
        self.assertEqual(packet['trade_scenarios']['side'],'LONG')
        self.assertIn('trade_scenarios',req['input'][-1]['content'])
        self.assertLess(len(req['input'][-1]['content']),10000)
        self.assertFalse(packet['ml']['trade_allowed'])
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);source=p/'source.json';target=p/'technical.json';source.write_text(json.dumps(packet))
            self.assertTrue(publish_technical(source,target,now=now/1000,gid=os.getgid()))
            self.assertIn('Net без funding',render_screen('plan',p/'report.json',now/1000))
            packet['as_of_ms']+=1;packet['trade_scenarios']['scenarios'][0]['assumed_entry']='1'
            source.write_text(json.dumps(packet))
            with self.assertRaisesRegex(ValueError,'invalid_or_stale'):publish_technical(source,target,now=(now+1)/1000,gid=os.getgid())

    def test_fake_budget_executor_keeps_old_locks_and_one_attempt(self):
        import hashlib
        from tests.test_adaptive_regular import RegularTests
        from research.adaptive_ai_executor import execute_reserved
        from research.adaptive_shadow import canonical
        fixture=RegularTests('test_success_same_snapshot_and_idempotent');fixture.setUp();self.addCleanup(fixture.doCleanups)
        s=fixture.snap;s['venue']='binance-usdm';s['latest_15m_ms']=fixture.b-900000;p=self.packet()
        p.update(signal_ms=s['latest_15m_ms']+900000,as_of_ms=s['as_of_ms'],expires_ms=s['latest_15m_ms']+1800000)
        p['trade_scenarios']=build(p);s['technical_context']=p
        s.pop('snapshot_id');s['snapshot_id']=hashlib.sha256(canonical(s).encode()).hexdigest()
        def send(db,rid,req,clock):
            self.assertIn('trade_scenarios',req['input'][-1]['content'])
            return execute_reserved(db,rid,req,transport=fixture.fake,clock=clock)
        self.assertEqual(fixture.tick(send=send)['status'],'completed')
        self.assertEqual(fixture.tick(send=send)['status'],'completed')
        self.assertEqual(len(fixture.calls),1)
        self.assertEqual((fixture.p/'interactive-once.lock').read_text(),'old')
