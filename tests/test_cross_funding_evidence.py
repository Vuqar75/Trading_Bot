import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from trading_bot.cross_funding_evidence import FundingEvent,FundingPeriod,PositionLifetime,verify_period,initialize_receipts,record_verified


class FundingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.period=FundingPeriod('BTCUSDT',1000,1100)
        self.event=FundingEvent('BTCUSDT',1001,'0.0001','100.0123')
        self.position=PositionLifetime('p','BTCUSDT',999)
        self.kw={'history_proof':'fixture-complete','positions_proof':'fixture-complete',
                 'verify_history':lambda proof,period:proof=='fixture-complete',
                 'verify_positions':lambda proof,period:proof=='fixture-complete'}

    def check(self,events=None,positions=None,**kw):
        return verify_period(self.period,[self.event] if events is None else events,
            [self.position] if positions is None else positions,**(self.kw|kw))

    def test_shifted_event_retains_all_three_times_and_exact_values(self):
        result=self.check();self.assertEqual(result['period'].expected_due,1000)
        self.assertEqual(result['period'].next_due,1100);self.assertEqual(result['event'].funding_time,1001)
        self.assertEqual(result['event'].rate,'0.0001');self.assertEqual(result['event'].mark_price,'100.0123')
        self.assertEqual(result['position_id'],'p')

    def test_half_open_boundaries_and_multiple_candidates(self):
        for stamp,ok in ((999,False),(1000,True),(1099,True),(1100,False)):
            with self.subTest(stamp=stamp):
                event=FundingEvent('BTCUSDT',stamp,'0.0001','100')
                if ok:self.assertEqual(self.check([event])['event'],event)
                else:
                    with self.assertRaises(ValueError):self.check([event])
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.check([self.event,FundingEvent('BTCUSDT',1002,'0.0002','101')])
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.check([self.event,FundingEvent('BTCUSDT',1001,'0.0002','101','Special')])

    def test_actual_position_time_not_scheduled_deadline(self):
        self.assertEqual(self.check(positions=[PositionLifetime('p','BTCUSDT',1001)])['position_id'],'p')
        self.assertIsNone(self.check(positions=[PositionLifetime('p','BTCUSDT',1002)])['position_id'])
        self.assertIsNone(self.check(positions=[PositionLifetime('p','BTCUSDT',999,1001)])['position_id'])
        self.assertEqual(self.check(positions=[PositionLifetime('p','BTCUSDT',999,1002)])['position_id'],'p')
        with self.assertRaisesRegex(ValueError,'Ambiguous'):self.check(positions=[self.position,PositionLifetime('q','BTCUSDT',1000)])

    def test_single_record_without_complete_proof_is_unconfirmed(self):
        with self.assertRaisesRegex(ValueError,'completeness unproven'):self.check(history_proof=None)
        with self.assertRaisesRegex(ValueError,'completeness unproven'):self.check(positions_proof=None)
        with self.assertRaisesRegex(ValueError,'Missing'):self.check([])

    def test_changed_accepted_event_rejected_and_replay_has_same_identity(self):
        self.assertEqual(self.check(accepted=self.event),self.check(accepted=self.event))
        for changed in (FundingEvent('BTCUSDT',1001,'0.0002','100.0123'),
                        FundingEvent('BTCUSDT',1001,'0.0001','100.0124'),
                        FundingEvent('BTCUSDT',1002,'0.0001','100.0123')):
            with self.assertRaisesRegex(ValueError,'changed'):self.check([changed],accepted=self.event)

    def test_receipt_and_accounting_survive_real_process_death_without_duplicates(self):
        script='''import os,sqlite3,sys\nfrom trading_bot.cross_funding_evidence import FundingEvent,FundingPeriod,PositionLifetime,verify_period,initialize_receipts,record_verified\nd=sqlite3.connect(sys.argv[1],isolation_level=None);d.execute("BEGIN IMMEDIATE");initialize_receipts(d);d.execute("CREATE TABLE IF NOT EXISTS balance(value INTEGER)");d.execute("INSERT INTO balance SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM balance)");r=verify_period(FundingPeriod("BTCUSDT",1000,1100),[FundingEvent("BTCUSDT",1001,"0.0001","100")],[PositionLifetime("p","BTCUSDT",1001)],history_proof="h",positions_proof="p",verify_history=lambda x,p:x=="h",verify_positions=lambda x,p:x=="p");new=record_verified(d,r)\nif new:d.execute("UPDATE balance SET value=value+1")\nif sys.argv[2]=="before":os._exit(71)\nd.commit()\nif sys.argv[2]=="after":os._exit(72)\nd.close()'''
        with tempfile.TemporaryDirectory() as tmp:
            for crash in ('before','after'):
                path=Path(tmp)/(crash+'.db')
                result=subprocess.run([sys.executable,'-B','-c',script,str(path),crash],timeout=20)
                self.assertEqual(result.returncode,71 if crash=='before' else 72)
                for _ in range(2):self.assertEqual(subprocess.run([sys.executable,'-B','-c',script,str(path),'resume'],timeout=20).returncode,0)
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute('SELECT value FROM balance').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM cross_funding_evidence_receipts').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT expected_due,next_due,funding_time FROM cross_funding_evidence_receipts').fetchone(),(1000,1100,1001))

    def test_receipt_requires_transaction_and_rejects_changed_values(self):
        db=sqlite3.connect(':memory:',isolation_level=None);self.addCleanup(db.close)
        with self.assertRaisesRegex(ValueError,'transaction'):initialize_receipts(db)
        db.execute('BEGIN IMMEDIATE');initialize_receipts(db)
        result=self.check();self.assertTrue(record_verified(db,result));self.assertFalse(record_verified(db,result))
        changed=self.check([FundingEvent('BTCUSDT',1001,'0.0002','100.0123')])
        with self.assertRaisesRegex(ValueError,'changed'):record_verified(db,changed)
        db.commit()


if __name__=='__main__':unittest.main()
