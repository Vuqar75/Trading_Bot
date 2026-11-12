import contextlib,copy,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_technical_trade_scenarios as fixtures
from research.technical_trade_scenarios import build
from research.technical_plan_history import advance,validate,update,BAR,LIMIT


class PlanHistoryTest(unittest.TestCase):
    def packet(self,side='LONG',signal=BAR):
        p=fixtures.ScenarioTest().packet(side)
        p.update(signal_ms=signal,as_of_ms=signal+120000,expires_ms=signal+BAR)
        p['ml']['model_id']='model-1';p['trade_scenarios']=build(p)
        return p

    def capture(self,signal=BAR,edits=None):
        rows=[[t,100.,100.2,99.8,100.,1.] for t in range(0,signal,BAR)]
        for row in rows:
            if edits and row[0] in edits:row[1:]=edits[row[0]]
        return {'venue':'binance-usdm','symbol':'BTCUSDT','candles':rows}

    def step(self,state,signal,edits=None):
        return advance(state,self.packet('WAIT',signal),self.capture(signal,edits),signal+120000)

    def start(self,side='LONG'):
        return advance(None,self.packet(side),self.capture(),BAR+120000)

    def test_no_pattern_still_updates_existing_history(self):
        empty=advance(None,self.packet('WAIT'),self.capture(),BAR+120000)
        self.assertEqual(empty['records'],[])
        first=self.start();later=self.step(first,2*BAR)
        self.assertEqual(len(later['records']),1)
        self.assertEqual(later['records'][0]['packet']['side'],'LONG')
        self.assertGreater(later['checked_ms'],first['checked_ms'])

    def test_first_fifteen_minutes_excluded_then_target_observed(self):
        edits={BAR:[100.,105.,95.,100.,1.],2*BAR:[100.,101.3,99.8,100.,1.]}
        early=self.step(self.start(),2*BAR,edits)
        self.assertEqual(early['records'][0]['results']['base']['level'],'none')
        later=self.step(early,3*BAR,edits);record=later['records'][0]
        self.assertEqual(record['state'],'watching')
        self.assertEqual(record['results']['base']['level'],'target')
        self.assertEqual(record['results']['base']['first_bar_ms'],2*BAR)
        self.assertFalse(later['fills_confirmed']);self.assertIsNone(record['pnl'])

    def test_both_barriers_in_same_bar_stay_ambiguous(self):
        edits={2*BAR:[100.,102.,98.,100.,1.]}
        first=self.step(self.start(),3*BAR,edits)
        final=self.step(first,17*BAR,edits)
        self.assertEqual(final['records'][0]['state'],'complete')
        self.assertEqual(final['records'][0]['results']['base']['level'],'ambiguous')

    def test_long_and_short_opposite_barriers(self):
        for side,edit,level in (('LONG',[100.,100.2,99.3,100.,1.],'stop'),
                                ('SHORT',[100.,100.2,98.5,100.,1.],'target')):
            state=self.step(self.start(side),3*BAR,{2*BAR:edit})
            self.assertEqual(state['records'][0]['results']['base']['level'],level)

    def test_first_barrier_precedes_later_opposite_barrier(self):
        edits={2*BAR:[100.,100.2,99.3,100.,1.],3*BAR:[100.,102.,99.8,100.,1.]}
        state=self.step(self.start(),4*BAR,edits)
        self.assertEqual(state['records'][0]['results']['base']['level'],'stop')

    def test_full_window_neither_is_not_profit_or_trade(self):
        final=self.step(self.start(),17*BAR);r=final['records'][0]
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['results']['base'],{'level':'none','first_bar_ms':None,'observed_bars':15})
        self.assertIsNone(r['pnl']);self.assertFalse(final['orders_enabled'])

    def test_anchor_correction_freezes_record(self):
        state=self.step(self.start(),3*BAR,{0:[100.,100.3,99.8,100.,1.]})
        self.assertEqual(state['records'][0]['state'],'source_changed')
        self.assertEqual(self.step(state,4*BAR)['records'][0]['state'],'source_changed')

    def test_observed_path_correction_freezes_record(self):
        first=self.step(self.start(),3*BAR,{2*BAR:[100.,101.3,99.8,100.,1.]})
        later=self.step(first,4*BAR)
        self.assertEqual(later['records'][0]['state'],'source_changed')

    def test_missing_bar_blocks_conclusion_without_filling_prices(self):
        p=self.packet('WAIT',4*BAR);raw=self.capture(4*BAR);raw['candles'].pop(2)
        state=advance(self.start(),p,raw,p['as_of_ms'])
        self.assertEqual(state['records'][0]['state'],'data_gap')
        resumed=self.step(state,5*BAR)
        self.assertEqual(resumed['records'][0]['state'],'watching')
        self.assertIsNone(resumed['records'][0]['pnl'])

    def test_same_snapshot_idempotence_and_model_identity(self):
        p=self.packet();raw=self.capture();first=advance(None,p,raw,p['as_of_ms'])
        self.assertEqual(advance(first,p,raw,p['as_of_ms']),first)
        other=copy.deepcopy(p);other['ml']['model_id']='other'
        second=advance(first,other,raw,p['as_of_ms'])
        self.assertEqual(len(second['records']),2)

    def test_all_original_features_retained_without_backfilling_old_records(self):
        p=self.packet();state=advance(None,p,self.capture(),p['as_of_ms'])
        self.assertEqual(state['records'][0]['packet']['features'],p['features'])
        old=copy.deepcopy(state);old['records'][0]['packet']['features']={'atr_pct':p['features']['atr_pct']}
        resumed=advance(old,p,self.capture(),p['as_of_ms'])
        self.assertEqual(resumed['records'][0]['packet']['features'],{'atr_pct':p['features']['atr_pct']})

    def test_no_backdated_registration_future_candles_or_reordering(self):
        p=self.packet()
        with self.assertRaisesRegex(ValueError,'stale'):advance(None,p,self.capture(),p['expires_ms'])
        raw=self.capture(2*BAR)
        with self.assertRaisesRegex(ValueError,'candle_time'):advance(None,p,raw,p['as_of_ms'])
        state=self.step(self.start(),2*BAR)
        with self.assertRaisesRegex(ValueError,'reordered'):advance(state,p,self.capture(),p['as_of_ms'])

    def test_bounded_retention_is_explicit(self):
        state=None
        for i in range(1,LIMIT+2):
            p=self.packet(signal=i*BAR);state=advance(state,p,self.capture(i*BAR),p['as_of_ms'])
        self.assertEqual(len(state['records']),LIMIT);self.assertEqual(state['discarded'],1)

    def test_atomic_write_restore_and_corrupt_file_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'technical-history.json';p=self.packet();raw=self.capture()
            first=update(path,p,raw,p['as_of_ms']);before=path.read_bytes()
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            restored=Path(tmp)/'restored.json';restored.write_bytes(before)
            q=self.packet('WAIT',3*BAR)
            with patch('research.technical_plan_history.os.replace',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):update(path,q,self.capture(3*BAR),q['as_of_ms'])
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(update(restored,q,self.capture(3*BAR),q['as_of_ms'])['records'][0]['id'],first['records'][0]['id'])
            path.write_text('{')
            with self.assertRaises(ValueError):update(path,q,self.capture(3*BAR),q['as_of_ms'])
            self.assertEqual(path.read_text(),'{')
            malformed=copy.deepcopy(first);malformed['records'][0]['packet']['limits']=[]
            path.write_text(json.dumps(malformed));corrupt=path.read_bytes()
            with self.assertRaisesRegex(ValueError,'history_record'):update(path,q,self.capture(3*BAR),q['as_of_ms'])
            self.assertEqual(path.read_bytes(),corrupt)

    def test_publication_and_display_are_read_only_and_not_pnl(self):
        from research.adaptive_server_publish import publish_history
        from trading_bot.technical_ai_view import render_history
        state=self.step(self.start(),17*BAR,{2*BAR:[100.,102.,98.,100.,1.]});now=state['checked_ms']/1000
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'private.json';target=Path(tmp)/'technical-history.json';src.write_text(json.dumps(state))
            self.assertTrue(publish_history(src,target,now=now,gid=os.getgid()))
            self.assertFalse(publish_history(src,target,now=now,gid=os.getgid()))
            before=target.read_bytes()
            with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
                text=render_history(target,now)
            self.assertIn('порядок неизвестен',text);self.assertIn('не исполнения',text)
            self.assertIn('УСТАРЕЛО',render_history(target,now+1801))
            self.assertEqual(target.read_bytes(),before)
            reset=copy.deepcopy(state);reset['started_ms']+=1;reset['records']=[];reset['checked_ms']+=1;src.write_text(json.dumps(reset))
            with self.assertRaisesRegex(ValueError,'reset'):publish_history(src,target,now=now+1,gid=os.getgid())

    def test_premature_completion_or_claimed_pnl_rejected(self):
        first=self.start();bad=copy.deepcopy(first);bad['records'][0]['state']='complete'
        with self.assertRaisesRegex(ValueError,'premature'):validate(bad)
        bad=copy.deepcopy(first);bad['records'][0]['pnl']=1
        with self.assertRaisesRegex(ValueError,'history_state'):validate(bad)

    def test_existing_free_server_records_once_and_cache_does_not_collect_again(self):
        from research import pattern_ai_server as server
        from research.technical_plan_history import digest
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);model=folder/'model.json';model.write_text(json.dumps({'model_id':'model-1'}))
            p=self.packet();p['packet_id']=digest(p);output=folder/'technical.json'
            argv=['pattern-ai','--model',str(model),'--technical-output',str(output)]
            with patch('sys.argv',argv),patch.object(server.time,'time',return_value=p['as_of_ms']/1000),patch.object(server,'capture',return_value=self.capture()) as capture,patch.object(server,'build',return_value=({'technical_context':p},'RANGE')),patch('research.adaptive_regular.tick',side_effect=AssertionError('paid forbidden')),contextlib.redirect_stdout(io.StringIO()):
                server.main();server.main()
            self.assertEqual(capture.call_count,1)
            history=json.loads((folder/'technical-history.json').read_text())
            self.assertEqual(len(history['records']),1)
            self.assertEqual(history['records'][0]['source_packet_id'],p['packet_id'])
            self.assertEqual(json.loads(output.read_text()),p)

    def test_corrupt_history_is_reported_without_reset_or_paid_fallback(self):
        from research import pattern_ai_server as server
        from research.technical_plan_history import digest
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);model=folder/'model.json';model.write_text(json.dumps({'model_id':'model-1'}))
            history=folder/'technical-history.json';history.write_text('{')
            p=self.packet();p['packet_id']=digest(p);output=folder/'technical.json';stdout=io.StringIO()
            argv=['pattern-ai','--model',str(model),'--technical-output',str(output)]
            with patch('sys.argv',argv),patch.object(server.time,'time',return_value=p['as_of_ms']/1000),patch.object(server,'capture',return_value=self.capture()),patch.object(server,'build',return_value=({'technical_context':p},'RANGE')),patch('research.adaptive_regular.tick',side_effect=AssertionError('paid forbidden')),contextlib.redirect_stdout(stdout):
                server.main()
            self.assertEqual(json.loads(stdout.getvalue())['plan_history'],'unavailable')
            self.assertEqual(history.read_text(),'{');self.assertTrue(output.exists())
