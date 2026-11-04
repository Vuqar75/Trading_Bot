"""Independent acceptance probes: public command contract and exact-money oracle."""
import json
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.paper_wallet_ledger import PaperWalletLedger
from trading_bot.margin_interest_ledger import MarginInterestLedger, HOUR_MS


class IndependentCommands(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.q=UnifiedQueue(Path(self.tmp.name)/'commands.db');self.addCleanup(self.q.close)
        self.ident=0

    def receive(self,text,**kw):
        self.ident+=1
        self.q.receive(self.ident,True,text,now=100,**kw)
        return self.q.db.execute('SELECT market,command,payload FROM unified_jobs WHERE update_id=? ORDER BY market',(self.ident,)).fetchall()

    def test_read_commands_route_all_markets_and_replay_cannot_retarget(self):
        for market in ('spot','futures','futures_cross','margin_cross'):
            self.receive('/mode '+market)
            for command in ('/signal','/history','/report','/pause','/resume','/timeframe','/status '+market):
                with self.subTest(market=market,command=command):
                    rows=self.receive(command)
                    self.assertEqual([(r['market'],r['command']) for r in rows],[(market,command if not command.startswith('/status ') else '/status')])
                    ident=self.ident
                    self.receive('/mode '+('spot' if market!='spot' else 'futures'))
                    self.q.receive(ident,True,command,now=101)
                    self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_jobs WHERE update_id=?',(ident,)).fetchone()[0],1)
                    self.receive('/mode '+market)

    def test_unauthorized_all_commands_have_no_effect(self):
        for command in ('/mode futures','/pause_all','/resume','/close','/confirm T-x','/confirm R-x','/transfer spot reserve 1','/repay loan 1','/loans','/status'):
            with self.subTest(command=command):
                self.ident+=1
                self.q.receive(self.ident,False,command,now=100)
        self.assertEqual(self.q.state('mode'),'spot')
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_jobs').fetchone()[0],0)
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_outbox').fetchone()[0],0)

    def test_expired_action_and_confirmation_are_not_forwarded(self):
        for command in ('/resume','/close','/timeframe 5m','/transfer spot reserve 1','/confirm T-x','/confirm R-x'):
            with self.subTest(command=command):self.assertEqual(self.receive(command,fresh=False),[])
        self.receive('/mode futures_cross')
        rows=self.receive('/close ETHUSDT',positions={'futures_cross':{'ETHUSDT':{'id':'position-a','quantity':'0.25'}}})
        token=self.q.add_proposal(self.ident,'futures_cross','position-a','0.25',now=100,ttl=5)
        self.receive('/mode spot')
        self.ident+=1
        self.q.receive(self.ident,True,'/confirm '+token,now=105)
        self.assertFalse(self.q.db.execute('SELECT 1 FROM unified_jobs WHERE update_id=?',(self.ident,)).fetchone())

    def test_confirmation_routes_by_proposal_type_not_current_mode(self):
        self.receive('/mode margin_cross')
        self.receive('/close BTCUSDT',positions={'margin_cross':{'BTCUSDT':{'id':'margin-p','quantity':'0.00012'}}})
        token=self.q.add_proposal(self.ident,'margin_cross','margin-p','0.00012',now=100)
        self.receive('/mode futures')
        row=self.receive('/confirm '+token)[0]
        self.assertEqual((row['market'],row['command']),('margin_cross','/confirmed_close'))
        payload=json.loads(row['payload'])
        self.assertEqual((payload['position'],payload['quantity']),('margin-p','0.00012'))
        self.assertEqual(self.receive('/confirm T-example')[0]['market'],'wallet')
        self.assertEqual(self.receive('/confirm R-example')[0]['market'],'margin_cross')

    def test_pause_all_does_not_claim_success_before_all_acknowledgements(self):
        rows=self.receive('/pause_all')
        self.assertEqual({r['market'] for r in rows},{'spot','futures','futures_cross','margin_cross'})
        jobs=self.q.db.execute('SELECT id FROM unified_jobs ORDER BY id').fetchall()
        for index,row in enumerate(jobs):
            self.q.acknowledge(row['id'],'Пауза')
            texts='\n'.join(r[0] for r in self.q.db.execute('SELECT message FROM unified_outbox'))
            self.assertEqual('применены ко всем четырём' in texts,index==3)


class IndependentAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.l=PaperWalletLedger(Path(self.tmp.name)/'ledger.db');self.addCleanup(self.l.close)
        self.l.import_snapshot(spot_cash='1000',spot_btc='0',futures_cash='1000',futures_margin='0',funding='0',position_metadata={'spot':{'quantity':'0'},'futures':None},source='independent-fixture',captured_at=0)

    def test_exact_fraction_oracle_for_120_transfers_reserves_and_replays(self):
        rng=random.Random(20261104)
        free={'spot':Fraction(1000),'futures_isolated':Fraction(1000),'reserve':Fraction(0)}
        reserved={'spot':Fraction(0),'futures_isolated':Fraction(0)}
        for index in range(120):
            wallet=rng.choice(('spot','futures_isolated'))
            value=Fraction(rng.randrange(1,10000),1000)
            scaled=value*1000
            text=f'{scaled.numerator//1000}.{scaled.numerator%1000:03d}'
            kind=rng.randrange(3)
            if kind==0 and value<=free[wallet]:
                self.l.move_reserve('reserve:'+str(index),wallet,text)
                self.l.move_reserve('reserve:'+str(index),wallet,text)
                free[wallet]-=value;reserved[wallet]+=value
            elif kind==1 and value<=reserved[wallet]:
                self.l.move_reserve('release:'+str(index),wallet,text,release=True)
                free[wallet]+=value;reserved[wallet]-=value
            elif value<=free[wallet]:
                other=rng.choice([w for w in free if w!=wallet])
                p=self.l.propose_transfer('request:'+str(index),wallet,other,text,now=1000+index*2)
                event=self.l.confirm_transfer(p['id'],now=1001+index*2)
                self.assertEqual(event,self.l.confirm_transfer(p['id'],now=1001+index*2))
                free[wallet]-=value;free[other]+=value
            for w in free:self.assertEqual(Fraction(self.l.balance(w+':free')),free[w])
            for w in reserved:self.assertEqual(Fraction(self.l.balance(w+':reserved')),reserved[w])
            self.assertEqual(sum(free.values())+sum(reserved.values()),2000)
        by_event={}
        for row in self.l.db.execute('SELECT event_id,asset,amount FROM ledger_postings'):
            key=(row['event_id'],row['asset']);by_event[key]=by_event.get(key,Fraction(0))+Fraction(row['amount'])
        self.assertTrue(all(v==0 for v in by_event.values()))

    def test_negative_crypto_free_balance_is_rejected_by_global_reconciliation(self):
        for asset in ('USDT','BTC','ETH'):
            for bucket in ('free','reserved'):
                with self.subTest(asset=asset,bucket=bucket):
                    account='margin_cross:'+bucket
                    ident='negative:'+asset+':'+bucket
                    with self.l.transaction():
                        self.l._post(ident,'FIXTURE',{},[(account,asset,'-0.0001'),('external:fixture',asset,'0.0001')])
                    self.assertFalse(self.l.reconcile()['balanced'])
                    self.assertIn(account,self.l.reconcile()['negative_accounts'])
                    with self.l.transaction():
                        self.l._post('repair:'+ident,'FIXTURE',{},[(account,asset,'0.0001'),('external:fixture',asset,'-0.0001')])
                    self.assertTrue(self.l.reconcile()['balanced'])

    def test_hourly_interest_matches_fraction_oracle_after_replay(self):
        b=MarginInterestLedger(self.l)
        b.borrow('borrow','loan','position','BTC','0.000123456789',now_ms=1000)
        b.accrue('loan',now_ms=1000+7*HOUR_MS)
        b.accrue('loan',now_ms=1000+7*HOUR_MS)
        expected=Fraction('0.000123456789')*Fraction('0.00001')*8
        row=self.l.db.execute("SELECT interest FROM margin_loans WHERE id='loan'").fetchone()
        self.assertEqual(Fraction(row[0]),expected)
        self.assertEqual(self.l.db.execute("SELECT COUNT(*) FROM margin_interest_periods WHERE loan_id='loan'").fetchone()[0],8)
