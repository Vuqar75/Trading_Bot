import copy
import json
from pathlib import Path
import tempfile
import unittest

from research import ai_limit_plan as plan
from research.ai_limit_book import Book

T=1805101200000


def snapshot(side='LONG'):
    q={'id':'initial','symbol':'BTCUSDT','event_ms':T,'received_ms':T,'bid':'100','ask':'100.1','bid_qty':'1','ask_qty':'1','mark':'100'}
    levels=[{'id':i,'kind':k,'price':p,'timeframe':'15m','pivot_ms':T-9000000,'confirmed_ms':T-1800000} for i,k,p in
        [('e','support' if side=='LONG' else 'resistance','99' if side=='LONG' else '101'),('s','support' if side=='LONG' else 'resistance','97' if side=='LONG' else '103'),('t','resistance' if side=='LONG' else 'support','105' if side=='LONG' else '95')]]
    s={'version':plan.VERSION,'symbol':'BTCUSDT','venue':'binance-usdm','as_of_ms':T,'expires_ms':T+900000,'quote':q,
       'filters':{'as_of_ms':T,'tick_size':'.1','step_size':'.001','min_qty':'.001','min_notional':'50'},
       'timeframes':{'15m':{'atr14':'1'}},'levels':levels,'news':{'coverage':'limited','high_risk_until_ms':0,'items':[]}}
    s['snapshot_id']=plan.digest(s)
    d={'snapshot_id':s['snapshot_id'],'decision':side,'entry_level':'e','stop_level':'s','target_level':'t','valid_until_ms':T+600000,
       'max_hold_minutes':60,'thesis':'Вход от подтверждённого уровня, стоп за уровнем отмены идеи.','news_ids':[],'risks':['Новостное покрытие ограничено.']}
    return s,d


class BracketTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=T;self.root=Path(self.temp.name)/'state'
        self.b=Book(self.root,clock=lambda:self.now)
        self.b.funding(T,T,[]);self.b.pause(False)
    def tearDown(self):self.b.close();self.temp.cleanup()
    def accept(self,side='LONG'):
        s,d=snapshot(side);self.b.accept(s,d,provenance={'kind':'fixture','response_id':'fixture1','model':'fixture'})
        return self.b.active()[0]
    def trade(self,id,price,qty='.2',seller=True):
        self.now+=100
        return self.b.market('trade',{'id':id,'symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':price,'qty':qty,'buyer_maker':seller})
    def quote(self,id,price,qty='2'):
        self.now+=100
        return self.b.market('quote',{'id':id,'symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'bid':price,'ask':str(plan.dec(price)+plan.dec('.1')),
            'bid_qty':qty,'ask_qty':qty,'mark':price})
    def fill_entry(self,side='LONG'):
        p=self.accept(side);self.trade('entry','98.9' if side=='LONG' else '101.1',p['quantity'],side=='LONG')
        return p

    def test_limit_touch_is_not_fill_and_duplicate_trade_cannot_fill_twice(self):
        self.accept();self.trade('touch','99')
        self.assertEqual(self.b.snapshot()['fills'],0)
        self.trade('partial','98.9');self.assertEqual(self.b.active()[1]['open_qty'],'0.2')
        event=json.loads(self.b.db.execute("SELECT payload FROM bracket_inputs WHERE id='trade:BTCUSDT:partial'").fetchone()[0])
        self.assertEqual(self.b.market('trade',event)['status'],'duplicate')
        self.assertEqual(self.b.active()[1]['open_qty'],'0.2')
        changed={**event,'qty':'.3'}
        with self.assertRaisesRegex(ValueError,'revision'):self.b.market('trade',changed)

    def test_partial_tp_cancels_entry_and_never_reverses(self):
        p=self.accept();self.trade('entry','98.9','.2');self.trade('target','105.1','.1',False)
        state=self.b.active()[1];self.assertEqual(state['remaining'],'0');self.assertEqual(state['open_qty'],'0.1')
        self.trade('rest','105.1','5',False)
        self.assertEqual(self.b.active()[1]['status'],'RECONCILING')
        self.b.funding(T,self.now,[])
        self.assertIsNone(self.b.active());self.assertEqual(self.b.balance('margin'),0)
        self.assertEqual(self.b.balance('reserved'),0);self.assertEqual(self.b.balance('funding_buffer'),0)
        state=self.b.plans()[0][1];self.assertEqual(state['status'],'CLOSED');self.assertGreater(plan.dec(state['gross']),0)
        self.assertTrue(self.b.verify()['balanced'])

    def test_stop_gap_partial_liquidity_restart_protection_needs_no_model(self):
        p=self.fill_entry();qty=plan.dec(p['quantity']);self.quote('gap','94','.1')
        self.assertEqual(self.b.active()[1]['status'],'EXIT_PENDING');self.assertEqual(plan.dec(self.b.active()[1]['open_qty']),qty-plan.dec('.1'))
        after=self.b.snapshot()['fills'];self.b.pulse();self.assertEqual(self.b.snapshot()['fills'],after)
        self.b.close();self.b=Book(self.root,clock=lambda:self.now)
        self.quote('remaining','93','2')
        self.assertEqual(self.b.active()[1]['open_qty'],'0');self.assertEqual(self.b.active()[1]['status'],'RECONCILING')
        exits=self.b.db.execute("SELECT price FROM bracket_fills WHERE kind='EXIT'").fetchall()
        self.assertLess(plan.dec(exits[0][0]),plan.dec(p['stop']))
        self.b.funding(T,self.now,[]);self.assertIsNone(self.b.active())

    def test_short_entry_and_stop_or_target_geometry(self):
        p=self.fill_entry('SHORT');self.trade('short-target','94.9','3',True)
        self.b.funding(T,self.now,[]);state=self.b.plans()[0][1]
        self.assertEqual(state['status'],'CLOSED');self.assertGreater(plan.dec(state['gross']),0)

    def test_pause_cancels_remainder_but_does_not_remove_stop(self):
        self.accept();self.trade('part','98.9','.2');self.b.pause(True)
        self.assertEqual(self.b.active()[1]['remaining'],'0');self.assertEqual(self.b.active()[1]['open_qty'],'0.2')
        self.quote('stop','96');self.assertEqual(self.b.active()[1]['open_qty'],'0')

    def test_expiry_without_fill_releases_all_cash(self):
        p=self.accept();self.now=p['expires_ms'];self.b.pulse()
        self.assertIsNone(self.b.active());self.assertEqual(self.b.balance('free'),1000)
        self.assertEqual(self.b.snapshot()['fills'],0)

    def test_failed_transaction_rolls_back_financial_fill_and_recovers(self):
        self.accept();self.b.fault=lambda point:(_ for _ in ()).throw(RuntimeError('fault')) if point=='before_commit' else None
        with self.assertRaises(RuntimeError):self.trade('crash','98.9')
        self.b.fault=lambda point:None
        self.assertEqual(self.b.snapshot()['fills'],0);self.trade('crash','98.9')
        self.assertEqual(self.b.snapshot()['fills'],1)

    def test_late_funding_after_price_exit_is_charged_to_historical_position(self):
        p=self.fill_entry();stamp=self.now+50;qty=plan.dec(p['quantity'])
        self.trade('target','105.1','3',False)
        self.assertEqual(self.b.active()[1]['status'],'RECONCILING')
        event={'symbol':'BTCUSDT','event_ms':stamp,'rate':'.001','mark':'100'}
        self.b.funding(T,self.now,[event]);state=self.b.plans()[0][1]
        self.assertEqual(plan.dec(state['funding']),-qty*plan.dec('.1'));self.assertEqual(state['status'],'CLOSED')
        balance=self.b.balance('free');self.b.funding(T,self.now,[event]);self.assertEqual(self.b.balance('free'),balance)
        changed={**event,'rate':'.002'};result=self.b.funding(T,self.now,[changed])
        self.assertEqual(result['status'],'funding_history_conflict');self.assertEqual(self.b.balance('free'),balance)

    def test_zero_liquidity_stop_is_pending_and_fresh_mark_cannot_reuse_book_size(self):
        p=self.fill_entry();self.quote('empty-stop','95','0')
        self.assertEqual(self.b.active()[1]['status'],'EXIT_PENDING')
        self.assertEqual(plan.dec(self.b.active()[1]['open_qty']),plan.dec(p['quantity']))
        self.quote('now-liquid','95','2');self.assertEqual(plan.dec(self.b.active()[1]['open_qty']),0)

    def test_fresh_bid_can_stop_even_when_mark_temporarily_diverges(self):
        self.fill_entry();self.now+=100
        self.b.market('quote',{'id':'basis-gap','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,
            'bid':'90','ask':'90.1','bid_qty':'2','ask_qty':'2','mark':'100'})
        self.assertEqual(plan.dec(self.b.active()[1]['open_qty']),0)

    def test_after_commit_crash_and_new_received_time_do_not_duplicate_fill(self):
        self.accept();self.now+=100
        event={'id':'committed','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True}
        self.b.fault=lambda point:(_ for _ in ()).throw(RuntimeError('after')) if point=='after_commit' else None
        with self.assertRaises(RuntimeError):self.b.market('trade',event)
        self.b.fault=lambda point:None;self.now+=100
        self.assertEqual(self.b.market('trade',{**event,'received_ms':self.now})['status'],'duplicate')
        self.assertEqual(self.b.snapshot()['fills'],1)

    def test_context_mismatch_stale_and_invented_levels_rejected(self):
        s,d=snapshot();d['stop_level']='invented'
        with self.assertRaisesRegex(ValueError,'unknown_level'):plan.validate(s,d,self.now)
        s,d=snapshot();s['quote']['bid']='20'
        with self.assertRaisesRegex(ValueError,'hash'):plan.validate(s,d,self.now)
        s,d=snapshot();self.now+=10000
        with self.assertRaisesRegex(ValueError,'stale'):self.b.accept(s,d,provenance={'kind':'fixture','response_id':'stale','model':'fixture'})

    def test_model_latency_needs_new_execution_quote_not_impossible_old_quote(self):
        s,d=snapshot();self.now+=10000
        q={**s['quote'],'id':'fresh-after-model','event_ms':self.now,'received_ms':self.now}
        self.b.funding(T,self.now,[])
        result=self.b.accept(s,d,provenance={'kind':'fixture','response_id':'delayed','model':'fixture'},execution_quote=q)
        self.assertEqual(result['status'],'WORKING')
        self.now+=1000000
        repeated=self.b.accept(s,d,provenance={'kind':'fixture','response_id':'delayed','model':'fixture'})
        self.assertEqual(repeated['status'],'WORKING')

    def test_stop_continues_when_funding_history_conflict_blocks_entries(self):
        p=self.fill_entry();self.now+=100
        event={'symbol':'BTCUSDT','event_ms':self.now,'rate':'.001','mark':'100'}
        self.b.funding(T,self.now,[event]);self.b.funding(T,self.now,[{**event,'rate':'.002'}])
        self.assertIsNotNone(self.b.get('blocked'))
        self.quote('protect-conflict','95')
        self.assertEqual(plan.dec(self.b.active()[1]['open_qty']),0)
        self.assertEqual(self.b.active()[1]['status'],'RECONCILING')

    def test_risk_geometry_minimum_and_fundamental_veto(self):
        s,d=snapshot();s['news']['high_risk_until_ms']=T+1000;s['snapshot_id']=plan.digest({k:v for k,v in s.items() if k!='snapshot_id'});d['snapshot_id']=s['snapshot_id']
        with self.assertRaisesRegex(ValueError,'fundamental'):plan.validate(s,d,T)
        s,d=snapshot();s['filters']['min_notional']='150';s['snapshot_id']=plan.digest({k:v for k,v in s.items() if k!='snapshot_id'});d['snapshot_id']=s['snapshot_id']
        with self.assertRaisesRegex(ValueError,'minimum'):plan.validate(s,d,T)
        s,d=snapshot();p=plan.validate(s,d,T)
        self.assertLessEqual(plan.dec(p['quantity'])*plan.dec(p['entry']),100);self.assertLessEqual(plan.dec(p['risk_usdt']),5)

    def test_wait_is_not_an_order_and_global_exposure_is_not_duplicated(self):
        s,d=snapshot();d.update(decision='WAIT',entry_level=None,stop_level=None,target_level=None)
        result=self.b.accept(s,d,provenance={'kind':'fixture','response_id':'wait','model':'fixture'})
        self.assertEqual(result['status'],'WAIT');self.assertEqual(self.b.snapshot()['fills'],0)
        self.accept();s,d=snapshot('SHORT')
        with self.assertRaisesRegex(ValueError,'busy'):self.b.accept(s,d,provenance={'kind':'fixture','response_id':'other','model':'fixture'})

    def test_backup_can_restore_pending_orders_and_same_ids_without_reseeding(self):
        p=self.accept();self.trade('partial','98.9');target=Path(self.temp.name)/'backup.sqlite';self.b.backup(target)
        restored=Path(self.temp.name)/'restored';restored.mkdir(mode=0o700);target.rename(restored/'paper.sqlite')
        other=Book(restored,clock=lambda:self.now)
        try:
            self.assertEqual(other.active(),self.b.active());self.assertEqual(other.balance('free'),self.b.balance('free'));self.assertTrue(other.verify()['balanced'])
        finally:other.close()

    def test_time_exit_and_manual_close_remain_pending_without_fresh_book(self):
        p=self.fill_entry();self.now+=p['hold_ms'];self.b.pulse()
        self.assertEqual(self.b.active()[1]['close_reason'],'TIME');self.assertGreater(plan.dec(self.b.active()[1]['open_qty']),0)
        self.quote('time-close','100','3');self.assertEqual(self.b.active()[1]['open_qty'],'0')


if __name__=='__main__':unittest.main()
