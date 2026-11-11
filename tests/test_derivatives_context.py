import copy,unittest
import numpy as np
from research.derivatives_context import collect,series,validate,SPECS,STEP,LAG
from research.pattern_ai_analyze import analyze

class DerivativesContext(unittest.TestCase):
    def get(self,signal,fail=None):
        def fetch(path,params):
            name,spec=next((k,v) for k,v in SPECS.items() if v[0]==path)
            if name==fail:raise ValueError('public_market_unavailable_http401')
            field=spec[1];n=500 if name=='oi' else 12
            return [{'symbol':'BTCUSDT','timestamp':signal-(n-i)*STEP,field:str(1000+i if name=='oi' else 1.5)} for i in range(n)]
        return fetch
    def context(self,signal=900000000,fail=None):return collect(self.get(signal,fail),symbol='BTCUSDT',signal_ms=signal,now_ms=signal+120000)
    def test_complete_public_context(self):
        c=self.context();self.assertEqual(c['status'],'available');self.assertFalse(c['used_by_local_ml']);self.assertIn('oi_change_24h_pct',c['facts'])
        self.assertEqual(c['source_times_ms']['taker'],c['cutoff_ms'])
    def test_taker_start_time_normalized_to_period_end(self):
        s=series([{'timestamp':0,'buySellRatio':'1.2'}],'buySellRatio','BTCUSDT',600000,True)
        self.assertEqual(s,[(300000,1.2)])
    def test_unfinished_interval_excluded(self):
        self.assertEqual(series([{'timestamp':300000,'buySellRatio':'1.2'}],'buySellRatio','BTCUSDT',500000,True),[])
    def test_private_or_unavailable_component_not_fabricated(self):
        c=self.context(fail='top_positions');self.assertEqual(c['status'],'partial');self.assertNotIn('top_position_long_short_ratio',c['facts']);self.assertIn('top_positions',c['missing'])
    def test_all_failed_inputs_report_unavailable(self):
        def no(*args):raise OSError('failure')
        c=collect(no,symbol='BTCUSDT',signal_ms=900000000,now_ms=900120000);self.assertEqual(c['status'],'unavailable');self.assertEqual(c['facts'],{})
    def test_conflicting_metric_rejected(self):
        rows=[{'symbol':'BTCUSDT','timestamp':0,'longShortRatio':'1.2'},{'symbol':'BTCUSDT','timestamp':0,'longShortRatio':'1.3'}]
        with self.assertRaisesRegex(ValueError,'metric_conflict'):series(rows,'longShortRatio','BTCUSDT',1000)
    def test_future_source_rejected(self):
        c=self.context();c['source_times_ms']['oi']=c['cutoff_ms']+STEP
        with self.assertRaisesRegex(ValueError,'source_time'):validate(c,symbol='BTCUSDT',signal_ms=c['signal_ms'],as_of_ms=c['observed_ms'])
    def test_different_market_rejected(self):
        c=self.context();c['venue']='binance-spot'
        with self.assertRaisesRegex(ValueError,'market_schema'):validate(c,symbol='BTCUSDT',signal_ms=c['signal_ms'],as_of_ms=c['observed_ms'])
    def test_no_source_timestamp_means_no_fact(self):
        c=self.context();del c['source_times_ms']['oi']
        with self.assertRaisesRegex(ValueError,'source_missing'):validate(c,symbol='BTCUSDT',signal_ms=c['signal_ms'],as_of_ms=c['observed_ms'])
    def test_existing_ml_does_not_silently_consume_new_fields(self):
        from research import technical_patterns as t
        n=3200;rows=[]
        for i in range(n):
            p=100+i*.01;rows.append([i*900000,p,p+.1,p-.1,p+.02,10.])
        rows[-2][1]=rows[-2][4]+.05
        rows[-1][1]=rows[-2][4]-.1;rows[-1][4]=rows[-2][1]+.1;rows[-1][2]=rows[-1][4]+.1;rows[-1][3]=rows[-1][1]-.1
        signal=n*900000;now=signal+120000
        leaf=lambda v:[{'feature':[-2],'threshold':[-2.],'left':[-1],'right':[-1],'value':[v]}]
        b={'feature_version':t.VERSION,'features':t.FEATURES,'venue':'binance-usdm','valid_from_ms':0,'valid_until_ms':signal+900000,'train_label_end_ms':1,'classifier':leaf(.7),'regressor':leaf(.001),'model_id':'test'}
        c={'venue':'binance-usdm','symbol':'BTCUSDT','candles':rows}
        before=analyze(b,c,now);c['derivatives']=self.context(signal)
        after=analyze(b,c,now);self.assertEqual(before['ml'],after['ml']);self.assertIn('derivatives',after)
        self.assertFalse(after['ml']['trade_allowed'])

    def test_gpt_boundary_rejects_future_derivative_fact(self):
        from research.adaptive_ai_executor import request
        from research import technical_patterns as tech
        signal=900000000;now=signal+120000;c=self.context(signal);c['source_times_ms']['oi']=signal
        packet={'symbol':'BTCUSDT','venue':'binance-usdm','signal_ms':signal,'as_of_ms':now,'feature_version':tech.VERSION,'limits':{'orders_enabled':False},'derivatives':c}
        snapshot={'snapshot_id':'unit','symbol':'BTCUSDT','venue':'binance-usdm','as_of_ms':now,'latest_4h_ms':0,'latest_1h_ms':0,'latest_15m_ms':signal-900000,'features':{},'book':{},'news':[],'coverage':'unit','technical_context':packet}
        with self.assertRaisesRegex(ValueError,'source_time'):request(snapshot)


class DerivativesReadOnlyPanel(unittest.TestCase):
    def test_top_accounts_positions_and_absolute_oi_are_distinct(self):
        import json,tempfile
        from pathlib import Path
        from trading_bot.technical_ai_view import render
        signal=900000000;now=signal+120000;c=DerivativesContext().context(signal)
        packet={'feature_version':'technical-patterns-v1','venue':'binance-usdm','symbol':'BTCUSDT','signal_ms':signal,'expires_ms':signal+900000,'as_of_ms':now,
                'features':{'rsi':.55,'adx':.2,'atr_pct':.004,'volume_ratio':1.2},'detected_patterns':[],
                'ml':{'action':'NO_TRADE','trade_allowed':False},'derivatives':c}
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'technical.json';p.write_text(json.dumps(packet));text=render(p,now/1000)
        for label in ('Открытый интерес, BTC','L/S счетов топ-трейдеров','L/S позиций топ-трейдеров','Taker BUY/SELL'):
            self.assertIn(label,text)

    def test_partial_facts_are_visible_without_fabricated_ratio(self):
        import json,tempfile
        from pathlib import Path
        from trading_bot.technical_ai_view import render
        f=DerivativesContext();signal=900000000;now=signal+120000;c=f.context(signal,fail='top_positions')
        packet={'feature_version':'technical-patterns-v1','venue':'binance-usdm','symbol':'BTCUSDT','signal_ms':signal,'expires_ms':signal+900000,'as_of_ms':now,
                'features':{'rsi':.55,'adx':.2,'atr_pct':.004,'volume_ratio':1.2},'detected_patterns':[],
                'ml':{'action':'NO_TRADE','trade_allowed':False},'derivatives':c}
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'technical.json';p.write_text(json.dumps(packet));text=render(p,now/1000)
        self.assertIn('неполные',text);self.assertIn('OI за 24ч',text);self.assertNotIn('L/S позицийtop-трейдеров:',text)
        self.assertIn('ML-модель их не использует',text)

class FreeOnlyDerivativesFlag(unittest.TestCase):
    def test_context_flag_does_not_enable_paid_tick(self):
        import contextlib,io,json,tempfile
        from pathlib import Path
        from unittest.mock import patch
        from research import pattern_ai_server as s
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);model=p/'model.json';model.write_text(json.dumps({'model_id':'unit'}));target=p/'technical.json'
            snapshot={'technical_context':{'packet_id':'unit','ml':{'model_id':'unit'}}}
            argv=['server','--model',str(model),'--technical-output',str(target),'--derivatives-context']
            with patch('sys.argv',argv),patch.object(s.time,'time',return_value=900120),patch.object(s,'capture',return_value={}) as source,patch.object(s,'build',return_value=(snapshot,'RANGE')),patch('research.adaptive_regular.tick',side_effect=AssertionError('paid forbidden')) as paid,contextlib.redirect_stdout(io.StringIO()):
                s.main()
            source.assert_called_once_with('BTCUSDT',derivatives=True);paid.assert_not_called();self.assertTrue(target.exists())

if __name__=='__main__':unittest.main()
