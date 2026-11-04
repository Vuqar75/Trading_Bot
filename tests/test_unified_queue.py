import tempfile
import unittest
from pathlib import Path
from trading_bot.unified_queue import UnifiedQueue


class UnifiedQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'queue.db'
        self.q = UnifiedQueue(self.path)
        self.addCleanup(lambda: self.q.close())

    def test_isolated_futures_responses_identify_market(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(2, True, '/pause')
        self.q.acknowledge(self.q.pending('futures')[0]['id'], 'Пауза')
        messages = [r[0].lower() for r in self.q.db.execute('SELECT message FROM unified_outbox')]
        self.assertTrue(all('isolated' in message for message in messages))

    def test_command_routing_matrix_for_all_four_paper_markets(self):
        markets=('spot','futures','futures_cross','margin_cross')
        ident=1
        for market in markets:
            self.q.receive(ident,True,'/mode '+market,now=100);ident+=1
            for command in ('/signal','/history','/report','/pause','/resume','/timeframe'):
                self.q.receive(ident,True,command,now=100)
                row=self.q.db.execute('SELECT market,command FROM unified_jobs WHERE update_id=?',(ident,)).fetchone()
                self.assertEqual(tuple(row),(market,command));ident+=1
            self.q.receive(ident,True,'/leverage',now=100)
            rows=self.q.db.execute('SELECT market FROM unified_jobs WHERE update_id=?',(ident,)).fetchall()
            self.assertEqual([r[0] for r in rows],[] if market=='spot' else [market]);ident+=1
            snapshot={'id':market+'-position','quantity':'0.125'}
            positions={market:({'BTCUSDT':snapshot} if market in ('futures_cross','margin_cross') else snapshot)}
            self.q.receive(ident,True,'/close',positions=positions,now=100)
            job=self.q.db.execute('SELECT market,payload FROM unified_jobs WHERE update_id=?',(ident,)).fetchone()
            self.assertEqual(job['market'],market)
            self.assertIn(market+'-position',job['payload']);ident+=1
            self.q.receive(ident,True,'/status '+market,now=100)
            row=self.q.db.execute('SELECT market,command FROM unified_jobs WHERE update_id=?',(ident,)).fetchone()
            self.assertEqual(tuple(row),(market,'/status'));ident+=1
        self.q.receive(ident,True,'/status',now=100)
        rows=self.q.db.execute('SELECT market FROM unified_jobs WHERE update_id=?',(ident,)).fetchall()
        self.assertEqual({r[0] for r in rows},set(markets))

    def test_market_frozen_at_accept_and_duplicate(self):
        self.q.receive(10, True, '/pause')
        self.q.receive(11, True, '/mode futures')
        self.q.receive(10, True, '/pause')
        self.assertEqual(len(self.q.pending('spot')), 1)
        self.assertEqual(len(self.q.pending('futures')), 0)
        self.assertEqual(self.q.state('mode'), 'futures')
        self.assertEqual(self.q.state('offset'), '12')

    def test_margin_close_and_repay_confirmations_keep_their_types_and_market(self):
        self.q.receive(1,True,'/mode margin_cross',now=10)
        self.q.receive(2,True,'/close ETHUSDT',now=10,positions={'margin_cross':{'ETHUSDT':{'id':'p','quantity':'0.04'}}})
        token=self.q.add_proposal(2,'margin_cross','p','0.04',now=10)
        self.q.receive(3,True,'/mode spot',now=11)
        self.q.receive(4,True,'/confirm '+token,now=11)
        self.q.receive(5,True,'/confirm R-example',now=11)
        jobs=self.q.pending('margin_cross')
        self.assertEqual([j['command'] for j in jobs],['/confirmed_close','/confirm_repay'])
        self.assertIn('ETHUSDT',jobs[0]['payload']);self.assertIn('0.04',jobs[0]['payload'])
        self.q.receive(6,False,'/confirm R-example',now=11)
        self.assertEqual(len(self.q.pending('margin_cross')),2)

    def test_report_bound_to_selected_market_balance_report_is_global(self):
        self.q.receive(1,True,'/mode futures_cross')
        self.q.receive(2,True,'/report')
        self.q.receive(3,True,'/balance_report today')
        self.q.receive(4,True,'/mode spot')
        self.q.receive(5,True,'/report')
        self.assertEqual(self.q.pending('futures_cross')[0]['command'],'/report')
        self.assertEqual(self.q.pending('spot')[0]['command'],'/report')
        self.assertEqual(self.q.pending('wallet')[0]['command'],'/balance_report today')
        self.q.receive(6,True,'/help')
        text=self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id DESC').fetchone()[0]
        self.assertIn('/report',text);self.assertIn('/balance_report',text)

    def test_full_help_is_durable_and_read_only_in_every_market(self):
        for index, market in enumerate(('spot','futures','futures_cross','margin_cross')):
            ident=100+index*3
            self.q.receive(ident,True,'/mode '+market)
            before=self.q.db.execute('SELECT COALESCE(MAX(id),0) FROM unified_outbox').fetchone()[0]
            self.q.receive(ident+1,True,'/help')
            messages=[r[0] for r in self.q.db.execute('SELECT message FROM unified_outbox WHERE id>? ORDER BY id',(before,))]
            self.assertEqual(len(messages),3)
            self.assertIn('Выбранный рынок:',messages[0])
            self.assertIn('/pair из старого приложения',messages[0])
            self.assertIn('пока не поддерживается',messages[0])
            self.assertIn('/confirm ID',messages[1])
            self.assertIn('/stop не является',messages[1])
            self.assertIn('/repay ID_ЗАЙМА КОЛИЧЕСТВО',messages[2])
            self.assertTrue(all(len(m.encode('utf-16-le'))//2<4096 for m in messages))
            self.assertEqual(self.q.state('mode'),market)
            self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_jobs').fetchone()[0],0)
            self.q.receive(ident+1,True,'/help')
            self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_outbox WHERE id>?',(before,)).fetchone()[0],3)

    def test_help_start_and_after_commit_replay_do_not_lose_or_duplicate_pages(self):
        def crash(point):
            if point=='after_commit':raise RuntimeError('fixture crash')
        self.q.fault=crash
        with self.assertRaises(RuntimeError):self.q.receive(1,True,'/help@TradingBot')
        self.q.fault=lambda _:None
        self.q.receive(1,True,'/help@TradingBot')
        help_pages=[r[0] for r in self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id')]
        self.assertEqual(len(help_pages),3)
        self.q.receive(2,True,'/start@TradingBot')
        start_pages=[r[0] for r in self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id')][3:]
        self.assertEqual(len(start_pages),1)
        self.assertIn('paper',start_pages[0].lower())
        self.assertIn('/help',start_pages[0])
        self.assertNotIn('[Справка',start_pages[0])
        self.q.receive(2,True,'/start@TradingBot')
        self.q.receive(3,False,'/help')
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_outbox').fetchone()[0],4)
        self.assertEqual(self.q.state('mode'),'spot')
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_jobs').fetchone()[0],0)

    def test_short_start_preserves_selected_mode_and_creates_no_trade_jobs(self):
        for index, market in enumerate(('spot','futures','futures_cross','margin_cross')):
            ident=100+index*3
            self.q.receive(ident,True,'/mode '+market)
            before=self.q.db.execute('SELECT COALESCE(MAX(id),0) FROM unified_outbox').fetchone()[0]
            self.q.receive(ident+1,True,'/start@TradingBot')
            self.q.receive(ident+1,True,'/start@TradingBot')
            messages=[r[0] for r in self.q.db.execute('SELECT message FROM unified_outbox WHERE id>? ORDER BY id',(before,))]
            self.assertEqual(len(messages),1)
            self.assertIn('paper',messages[0].lower())
            self.assertIn('/help',messages[0])
            self.assertLess(len(messages[0].encode('utf-16-le'))//2,4096)
            self.assertEqual(self.q.state('mode'),market)
            self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_jobs').fetchone()[0],0)
            self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM unified_proposals').fetchone()[0],0)

    def test_foreign_sender_cannot_change_mode_or_enqueue(self):
        self.q.receive(1, False, '/mode futures')
        self.assertEqual(self.q.state('mode'), 'spot')
        self.assertFalse(self.q.pending('futures'))
        self.assertEqual(self.q.db.execute('select count(*) from unified_outbox').fetchone()[0], 0)

    def test_pause_all_partial_and_failed_does_not_claim_completion(self):
        self.q.receive(1, True, '/pause_all')
        a, b = self.q.pending('spot')[0], self.q.pending('futures')[0]
        self.q.acknowledge(a['id'], 'На паузе')
        self.q.acknowledge(b['id'], 'Не удалось', applied=False)
        messages = [r[0] for r in self.q.db.execute('select message from unified_outbox')]
        self.assertFalse(any('применены к обоим' in m for m in messages))
        self.assertTrue(any('futures' in m for m in messages))

    def test_market_unavailable_does_not_block_other_jobs(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(2, True, '/pause')
        self.q.receive(3, True, '/mode spot')
        self.q.receive(4, True, '/pause')
        self.q.acknowledge(self.q.pending('spot')[0]['id'], 'На паузе')
        self.assertEqual(len(self.q.pending('futures')), 1)
        self.assertFalse(self.q.pending('spot'))

    def test_close_confirmation_uses_proposal_market_and_identity(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(2, True, '/close', positions={'futures':{'id':'position-a','quantity':'0.123'}})
        token = self.q.add_proposal(2, 'futures', 'position-a', '0.123', now=10)
        self.q.receive(3, True, '/mode spot')
        self.q.receive(4, True, '/confirm '+token, now=11)
        job = self.q.pending('futures')[0]
        self.assertEqual(job['command'], '/confirmed_close')
        self.assertIn('position-a', job['payload'])
        self.assertIn('0.123', job['payload'])
        self.q.receive(5, True, '/confirm '+token, now=12)
        self.assertEqual(len(self.q.pending('futures')), 1)

    def test_close_display_rounding_preserves_exact_identity(self):
        quantity='0.0001200000123456789'
        self.q.receive(1,True,'/mode futures_cross',now=10)
        self.q.receive(2,True,'/close ETHUSDT',positions={'futures_cross':{'ETHUSDT':{'id':'position','quantity':quantity}}},now=10)
        token=self.q.add_proposal(2,'futures_cross','position',quantity,now=10)
        message=self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id DESC').fetchone()[0]
        self.assertIn('0.00012 ETH',message);self.assertNotIn(quantity,message)
        self.assertEqual(self.q.db.execute('SELECT quantity FROM unified_proposals WHERE id=?',(token,)).fetchone()[0],quantity)
        self.q.receive(3,True,'/confirm '+token,now=11)
        self.assertIn(quantity,self.q.pending('futures_cross')[0]['payload'])

    def test_expired_and_canceled_confirmation_are_rejected(self):
        self.q.receive(1, True, '/close', positions={'spot':{'id':'p','quantity':'1'}})
        token = self.q.add_proposal(1, 'spot', 'p', '1', now=10, ttl=5)
        self.q.receive(2, True, '/confirm '+token, now=15)
        self.assertFalse(self.q.pending('spot'))
        self.q.receive(3, True, '/close', positions={'spot':{'id':'p','quantity':'1'}})
        token = self.q.add_proposal(3, 'spot', 'p', '1', now=20)
        self.q.receive(4, True, '/cancel '+token, now=21)
        self.q.receive(5, True, '/confirm '+token, now=22)
        self.assertFalse(self.q.pending('spot'))

    def test_reopen_preserves_mode_and_job(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(5, True, '/pause')
        self.q.close()
        self.q = UnifiedQueue(self.path)
        self.assertEqual(self.q.state('mode'), 'futures')
        self.assertEqual(self.q.pending('futures')[0]['update_id'], 5)

    def test_rollback_does_not_advance_offset_or_store_snapshot(self):
        def fault(where):
            if where == 'before_commit': raise RuntimeError('crash')
        self.q.fault = fault
        with self.assertRaises(RuntimeError): self.q.receive(1, True, '/mode futures')
        self.q.fault = lambda _: None
        self.assertEqual(self.q.state('offset'), '0')
        self.assertEqual(self.q.state('mode'), 'spot')
        self.q.receive(1, True, '/mode futures')
        self.assertEqual(self.q.state('offset'), '2')

    def test_conflicting_duplicate_rejected(self):
        self.q.receive(7, True, '/pause')
        with self.assertRaises(ValueError): self.q.receive(7, True, '/resume')

    def test_retry_after_freshness_changes_does_not_change_snapshot(self):
        self.q.receive(1, True, '/resume', fresh=True)
        self.q.receive(1, True, '/resume', fresh=False)
        self.assertEqual(len(self.q.pending('spot')), 1)
