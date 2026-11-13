import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.ai_limit_book import Book
from research.ai_limit_plan import dec
from trading_bot.ai_bracket_cancellation import audit,describe,label
from trading_bot.ai_bracket_view import render
from tests.test_ai_limit_brackets import snapshot,T


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=T;self.root=Path(self.temp.name)/'state'
        self.b=Book(self.root,clock=lambda:self.now);self.b.funding(T,T,[]);self.b.pause(False)
        s,d=snapshot();self.b.accept(s,d,provenance={'kind':'fixture','response_id':'test-cancel','model':'fixture'})
        self.p=self.b.active()[0]
    def tearDown(self):self.b.close();self.temp.cleanup()
    def state(self):return self.b.plans()[0][1]
    def test_expiry_reason_and_first_timestamp_survive_repeated_checks(self):
        self.now=self.p['expires_ms'];self.b.pulse();first=self.state()['entry_cancellation']
        self.assertEqual(first['causes'],['EXPIRED']);self.assertEqual(first['at_ms'],self.now)
        self.now+=1000;self.b.pulse();self.b.pause(True)
        self.assertEqual(self.state()['entry_cancellation'],first)
        self.assertEqual(self.b.balance('free'),1000);self.assertEqual(self.b.snapshot()['fills'],0)
        self.assertEqual(self.b.db.execute("SELECT count(*) FROM bracket_messages WHERE id=?",(self.p['plan_id']+':entry-canceled',)).fetchone()[0],1)
    def test_pause_and_simultaneous_guards_are_recorded_without_guessing(self):
        with self.b.transaction():self.b.set('paused',True);self.b.set('blocked','funding_history_conflict')
        self.now=self.p['expires_ms'];self.b.pulse()
        self.assertEqual(self.state()['entry_cancellation']['causes'],['EXPIRED','PAUSE','BLOCKED'])
    def test_partial_entry_trade_gap_preserves_position_and_explains_remainder(self):
        self.now+=100;self.b.market('trade',{'id':'partial','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.b.trade_gap('BTCUSDT','test-gap')
        state=self.state();self.assertEqual(state['entry_cancellation']['reason'],'TRADE_GAP')
        self.assertEqual(dec(state['open_qty']),dec('.2'));self.assertEqual(self.b.active()[0]['stop'],self.p['stop'])
        path=Path(self.temp.name)/'orders.json';path.write_text(json.dumps(self.b.snapshot()))
        result=render(path,self.now/1000)
        self.assertIn('Причина отмены остатка входа:',result);self.assertIn('потока сделок',result)
    def test_stale_filters_subreason_is_published(self):
        self.now+=120100;self.b.funding(T,self.now,[])
        self.b.market('quote',{'id':'fresh','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'bid':'100','ask':'100.1','bid_qty':'2','ask_qty':'2','mark':'100'})
        self.now+=100;self.b.market('trade',{'id':'cross','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        evidence=self.b.snapshot()['plans'][0]['entry_cancellation']
        self.assertEqual(evidence['detail'],'stale_filters');self.assertIn('биржевых ограничений',label(evidence))
        self.assertEqual(self.b.snapshot()['fills'],0);self.assertEqual(self.b.balance('free'),1000)
    def test_old_ledger_recovers_only_known_reason_and_audit_does_not_write(self):
        self.now=self.p['expires_ms'];self.b.pulse();state=self.state();state.pop('entry_cancellation')
        # Recreate an older JSON shape in an isolated fixture only.
        with self.b.transaction():self.b.save(self.p,state)
        before=hashlib.sha256((self.root/'paper.sqlite').read_bytes()).hexdigest()
        report=audit(self.root/'paper.sqlite',entry=self.p['entry']);e=report['plans'][0]['entry_cancellation']
        self.assertTrue(report['read_only']);self.assertEqual(e['evidence'],'legacy_ledger')
        self.assertEqual(e['reason'],'EXPIRED_OR_PAUSED');self.assertFalse(e['precise'])
        self.assertIn('точная ветка',report['plans'][0]['explanation'])
        self.assertEqual(before,hashlib.sha256((self.root/'paper.sqlite').read_bytes()).hexdigest())
        self.assertNotIn('entry_cancellation',self.state())
    def test_legacy_specific_ledger_reason_is_available_in_snapshot_and_screen(self):
        self.b.pause(True);state=self.state();state.pop('entry_cancellation')
        with self.b.transaction():self.b.save(self.p,state)
        e=self.b.snapshot()['plans'][0]['entry_cancellation'];self.assertEqual(e['reason'],'PAUSE');self.assertTrue(e['precise'])
        path=Path(self.temp.name)/'orders.json';path.write_text(json.dumps(self.b.snapshot()))
        result=render(path,self.now/1000)
        self.assertIn('Причина отмены входа: новые входы поставлены на паузу',result)
        self.assertIn('Баку',result)
    def test_failed_commit_does_not_leave_false_cancellation_or_message(self):
        self.b.fault=lambda point:(_ for _ in ()).throw(RuntimeError('fault')) if point=='before_commit' else None
        with self.assertRaises(RuntimeError):self.b.pause(True)
        self.b.fault=lambda point:None
        self.assertNotIn('entry_cancellation',self.state());self.assertEqual(self.state()['status'],'WORKING')
        self.assertEqual(self.b.db.execute("SELECT count(*) FROM bracket_messages WHERE id=?",(self.p['plan_id']+':entry-canceled',)).fetchone()[0],0)
    def test_missing_historical_record_is_not_inferred_from_current_time(self):
        self.now=self.p['expires_ms']+1000
        self.assertIsNone(describe(self.b.db,self.p,self.state()))
        self.assertIn('не указана',label(None))


if __name__=='__main__':unittest.main()
