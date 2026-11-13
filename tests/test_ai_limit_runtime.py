import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from research import ai_limit_plan as p
from research.ai_limit_feed import Feed
from research.ai_limit_runtime import atomic,trusted,ingest
from research.ai_limit_demo import capture,book_quote,demo
from trading_bot.ai_bracket_view import render
from trading_bot.unified_queue import UnifiedQueue
from tests.test_ai_limit_brackets import T,snapshot


class RuntimeTests(unittest.TestCase):
    def test_real_context_roundtrips_levels_and_rejects_unclosed_candles(self):
        cap=capture(T);f={'as_of_ms':T,'tick_size':'.1','step_size':'.001','min_qty':'.001','min_notional':'50'}
        s=p.context(cap,book_quote(T),f,{'coverage':'limited','high_risk_until_ms':0,'items':[]},T)
        self.assertGreater(len(s['levels']),5)
        self.assertGreater(len(s['candidate_plans']),0);self.assertLessEqual(len(s['candidate_plans']),8)
        for candidate in s['candidate_plans']:
            self.assertLessEqual(p.dec(candidate['quantity_cap'])*p.dec(candidate['entry']),100)
            self.assertLessEqual(p.dec(candidate['estimated_risk_usdt']),5)
            self.assertGreaterEqual(p.dec(candidate['net_reward_risk']),p.dec('1.5'))
        self.assertTrue(all(x['confirmed_ms']<=T for x in s['levels']))
        self.assertGreater(p.dec(s['timeframes']['15m']['atr14']),0)
        self.assertEqual(p.verify_context(s,T),s)
        cap['candles'].append([T,'100','101','99','100','1'])
        with self.assertRaisesRegex(ValueError,'candle'):p.context(cap,book_quote(T),f,{'coverage':'limited','high_risk_until_ms':0,'items':[]},T)

    def test_rest_book_without_update_id_and_funding_schedule_cannot_block_quote(self):
        def fetch(path,params):
            if path.endswith('bookTicker'):return {'symbol':'BTCUSDT','time':T,'bidPrice':'90','askPrice':'90.1','bidQty':'1','askQty':'1'}
            return {'symbol':'BTCUSDT','time':T,'markPrice':'100','nextFundingTime':T-1}
        q,due=Feed(fetch=fetch,clock=lambda:T).book('BTCUSDT')
        self.assertEqual(due,T-1);self.assertEqual(q['bid'],'90');self.assertIn('liquidity_id',q)
        self.assertEqual(p.quote(q,'BTCUSDT',T),q)
        with self.assertRaisesRegex(ValueError,'divergence'):p.quote(q,'BTCUSDT',T,entry=True)

    def test_denied_endpoint_is_not_retried_or_replaced_by_another_host(self):
        feed=Feed(clock=lambda:T)
        error=urllib.error.HTTPError('https://fapi.binance.com',451,'blocked',{},io.BytesIO(b''))
        with patch('urllib.request.urlopen',side_effect=error) as openurl:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,'451'):feed.get('/fapi/v1/time',{})
            self.assertEqual(openurl.call_count,1)

    def test_gapped_or_old_aggregate_trades_are_flagged_not_synthesized(self):
        raw=[{'a':10,'T':T-6000,'p':'100','q':'1','m':True},{'a':12,'T':T,'p':'100','q':'1','m':False}]
        events,last,gap=Feed(fetch=lambda path,params:raw,clock=lambda:T).trades('BTCUSDT',9)
        self.assertTrue(gap);self.assertEqual(last,12);self.assertEqual([e['id'] for e in events],['12'])

    def test_publication_and_telegram_buttons_do_not_create_financial_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'orders.json';atomic(path,{'bad':'fixture'})
            self.assertEqual(trusted(path,os.geteuid()),{'bad':'fixture'})
            os.chmod(path,0o666)
            with self.assertRaisesRegex(ValueError,'untrusted'):trusted(path,os.geteuid())
            q=UnifiedQueue(Path(temp)/'queue.sqlite')
            try:
                for i,symbol in enumerate(('BTC','ETH','SOL'),1):
                    with patch('trading_bot.ai_bracket_view.REPORT',path):q.receive(i,True,'/ai orders '+symbol,now=T/1000)
                self.assertEqual(q.db.execute('SELECT count(*) FROM unified_jobs').fetchone()[0],0)
                self.assertEqual(q.db.execute('SELECT count(*) FROM unified_updates').fetchone()[0],3)
            finally:q.close()

    def test_full_contract_including_budget_response_fills_restore_and_view(self):
        with tempfile.TemporaryDirectory() as temp:
            result=demo(Path(temp)/'demo')
            self.assertEqual(result['status'],'PASS');self.assertTrue(result['fixture_only'])
            self.assertEqual(result['real_model_calls'],0);self.assertEqual(result['live_or_testnet_orders'],0)
            self.assertEqual([x['direction'] for x in result['cycles']],['LONG','SHORT','WAIT'])
            view=(Path(temp)/'demo/telegram-orders.txt').read_text()
            self.assertIn('ПРОВЕРОЧНЫЙ',view)
            self.assertEqual(result['balances']['reserved'],'0');self.assertTrue(result['ledger_balanced'])
            envelope=trusted(Path(temp)/'demo/response.json',os.geteuid())
            self.assertEqual(envelope['analysis']['provenance']['kind'],'fixture')
            with self.assertRaisesRegex(ValueError,'fixture'):ingest(None,envelope)


if __name__=='__main__':unittest.main()
