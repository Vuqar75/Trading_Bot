import csv
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trading_bot.core import Config, Engine, Store, signal_details
from trading_bot.execution import PaperCosts
from trading_bot.replay import Candle, ReplayEngine, ReplayExchange, Variant, compare, load_candles, run_replay


def candles(count=200):
    closes = [100 + 2*math.sin(i/7) for i in range(count)]
    return [Candle(i*60000,closes[max(0,i-1)],max(closes[max(0,i-1)],closes[i])+1,
                   min(closes[max(0,i-1)],closes[i])-1,closes[i],1,i*60000+59999) for i in range(count)]


class CostJournalTests(unittest.TestCase):
    def make_engine(self, **kwargs):
        self.now = 1000
        store = Store(':memory:',clock=lambda:self.now)
        self.addCleanup(store.db.close)
        engine = Engine(Config(**kwargs),store,ReplayExchange())
        engine.change_market('futures')
        store.set('paused',False)
        return engine,store

    def test_known_three_trades_reconcile(self):
        engine,store = self.make_engine()
        known = [(77012.02,76740.53,-0.055217685498965),
                 (77250,77218,-0.024138252427183815),
                 (77290,77175.99,-0.034736187087592185)]
        for i,(entry,exit_price,net) in enumerate(known):
            engine.futures_trade('LONG',entry,i*2)
            self.now += 120
            engine.close_position(exit_price,i*2+1)
            self.assertAlmostEqual(store.history()[0][5],net,places=12)
        closes=store.db.execute("SELECT gross_pnl,entry_fee,exit_fee,net_pnl FROM execution_journal WHERE event='CLOSE'").fetchall()
        self.assertAlmostEqual(sum(r[3] for r in closes),-0.114092125013741,places=12)
        self.assertAlmostEqual(sum(r[1]+r[2] for r in closes),0.05994585372871497,places=12)
        for gross,entry,exit_fee,net in closes:self.assertAlmostEqual(net,gross-entry-exit_fee)

    def test_adverse_slippage_long_and_short_and_no_double_count(self):
        for direction in ('LONG','SHORT'):
            engine,store=self.make_engine(entry_fee_bps=2,exit_fee_bps=4,slippage_bps=10)
            engine.futures_trade(direction,100,1)
            pos=store.get('position')
            self.assertAlmostEqual(pos['price'],100.1 if direction=='LONG' else 99.9)
            self.now+=60
            engine.close_position(100,2)
            row=store.db.execute("SELECT gross_pnl,entry_fee,exit_fee,net_pnl,holding_seconds FROM execution_journal WHERE event='CLOSE'").fetchone()
            self.assertLess(row[0],0)
            self.assertAlmostEqual(row[3],row[0]-row[1]-row[2])
            self.assertEqual(row[4],60)
            self.assertAlmostEqual(store.get('futures_balance'),1000+row[3])
            self.assertEqual(store.db.execute('SELECT COUNT(DISTINCT position_id) FROM execution_journal').fetchone()[0],1)

    def test_reject_invalid_costs(self):
        for value in (-1,float('nan'),float('inf'),10000):
            with self.assertRaises(ValueError):PaperCosts(slippage_bps=value)

    def test_atomic_journal_failure_rolls_back_trade_and_balance(self):
        engine,store=self.make_engine()
        store.db.execute("CREATE TRIGGER reject_journal BEFORE INSERT ON execution_journal BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):engine.futures_trade('LONG',100,1)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM futures_trades').fetchone()[0],0)
        self.assertIsNone(store.get('position'))
        self.assertIsNone(store.get('futures_balance'))

    def test_legacy_position_close_marks_missing_metadata(self):
        engine,store=self.make_engine()
        store.set('position',dict(market='futures',direction='LONG',price=100,quantity=.1,cost=10,margin=10,leverage=1,entry_fee=.01))
        store.set('futures_balance',990)
        engine.close_position(101,2)
        row=store.db.execute('SELECT legacy_position,holding_seconds,costs_json FROM execution_journal').fetchone()
        self.assertEqual(row[0],1)
        self.assertIsNone(row[1])
        self.assertIsNone(json.loads(row[2])['entry'])

    def test_loss_cap_reconciliation(self):
        engine,store=self.make_engine()
        engine.change_leverage(10);store.set('paused',False)
        engine.futures_trade('LONG',100,1)
        engine.close_position(1,2)
        gross,fees,net,adjustment=store.db.execute("SELECT gross_pnl,entry_fee+exit_fee,net_pnl,loss_cap_adjustment FROM execution_journal WHERE event='CLOSE'").fetchone()
        self.assertAlmostEqual(net,-10)
        self.assertAlmostEqual(net,gross-fees+adjustment)


class ReplayTests(unittest.TestCase):
    def test_network_is_never_used_and_no_config_load(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')),patch.object(Config,'load',side_effect=AssertionError('env')):
            result=run_replay(candles(),31*60000,199*60000)
        self.assertGreater(result['trades'],0)
        self.assertAlmostEqual(result['balance_error'],0,places=9)
        self.assertAlmostEqual(result['reconciliation_error'],0,places=9)

    def test_future_ohlc_and_future_rows_do_not_change_prefix(self):
        original=candles()
        changed=list(original)
        # t=100 open remains identical; everything not available at t=100 changes.
        changed[100]=replace(changed[100],high=9999,low=1,close=9999)
        for i in range(101,len(changed)):
            changed[i]=replace(changed[i],open=9999,high=10000,low=1,close=2)
        for variant in (Variant(),Variant('combined',True,0,True,True)):
            a=run_replay(original,31*60000,100*60000,variant,capture=True)
            b=run_replay(changed,31*60000,100*60000,variant,capture=True)
            for key in ('net_pnl','max_drawdown','trades','equity'):self.assertEqual(a[key],b[key])
            for left,right in zip(a['journal'],b['journal']):
                self.assertEqual({k:v for k,v in left.items() if k!='position_id'},
                                 {k:v for k,v in right.items() if k!='position_id'})

    def test_exchange_rejects_unclosed_candle(self):
        data=candles()
        with self.assertRaises(ValueError):ReplayExchange().advance(data[10],data[:11])

    def test_confirmation_waits_one_closed_candle_and_cancels_reversal(self):
        store=Store(':memory:');self.addCleanup(store.db.close)
        engine=ReplayEngine(Config(),store,ReplayExchange(),Variant('confirm',confirm=True))
        buy=dict(action='BUY',fast=101,slow=100,old_fast=99,old_slow=100)
        hold={**buy,'action':'HOLD'}
        self.assertEqual(engine.resolve_action(buy,60000,None),'HOLD')
        self.assertEqual(engine.resolve_action(hold,120000,None),'BUY')
        self.assertEqual(engine.resolve_action(buy,180000,None),'HOLD')
        self.assertEqual(engine.resolve_action({**buy,'action':'SELL','fast':99},240000,None),'HOLD')

    def test_slope_blocks_up_cross_with_falling_fast_sma_but_not_exit(self):
        store=Store(':memory:');self.addCleanup(store.db.close)
        engine=ReplayEngine(Config(),store,ReplayExchange(),Variant('slope',slope=True))
        detail=dict(action='BUY',fast=100,slow=99,old_fast=101,old_slow=102)
        self.assertEqual(engine.resolve_action(detail,60000,None),'HOLD')
        self.assertEqual(engine.resolve_action(detail,60000,{'direction':'SHORT'}),'BUY')

    def test_opposite_entry_is_delayed_and_changes_direction(self):
        data=candles()
        base=run_replay(data,31*60000,199*60000,Variant(),capture=True)
        opposite=run_replay(data,31*60000,199*60000,Variant('opposite',opposite=True),capture=True)
        self.assertEqual(min(base['long_trades'],base['short_trades']),0)
        self.assertGreater(opposite['long_trades'],0)
        self.assertGreater(opposite['short_trades'],0)
        journal=opposite['journal']
        for previous,next_event in zip(journal,journal[1:]):
            if previous['event']=='CLOSE' and next_event['event']=='OPEN':
                self.assertGreater(next_event['ts'],previous['ts'])

    def test_validation_rejects_gap_duplicate_and_bad_price(self):
        for change in ('gap','duplicate','price'):
            data=candles(40)
            if change=='gap':del data[10]
            if change=='duplicate':data[10]=data[9]
            if change=='price':data[10]=replace(data[10],close=float('nan'))
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'candles.csv'
                with p.open('w',newline='') as f:
                    writer=csv.writer(f);writer.writerow(Candle.__dataclass_fields__)
                    writer.writerows(vars(c).values() for c in data)
                with self.assertRaises(ValueError):load_candles(p)

    def test_warmup_cannot_trade_and_end_closes_position(self):
        result=run_replay(candles(),40*60000,190*60000,capture=True)
        self.assertTrue(all(40*60 <= r['ts'] <= 190*60 for r in result['journal']))
        self.assertEqual(sum(r['event']=='OPEN' for r in result['journal']),result['trades'])

    def test_holdout_changes_cannot_change_training_selection(self):
        original=candles(150)
        modified=list(original)
        for i in range(100,len(modified)):
            modified[i]=replace(modified[i],open=200+i,high=500,low=1,close=200+i)
        a=compare(original,100*60000)
        b=compare(modified,100*60000)
        self.assertEqual(a['train_candidates'],b['train_candidates'])
        self.assertEqual(a['selected_on_train'],b['selected_on_train'])
        self.assertEqual(a['winner_on_train'],b['winner_on_train'])

    def test_drawdown_includes_open_position_and_costs(self):
        data=candles()
        result=run_replay(data,31*60000,199*60000,capture=True)
        peak=1000
        dd=0
        for row in result['equity']:
            peak=max(peak,row['equity']);dd=max(dd,peak-row['equity'])
        self.assertAlmostEqual(dd,result['max_drawdown'])
        self.assertGreater(dd,0)


if __name__=='__main__':unittest.main()
