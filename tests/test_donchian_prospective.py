import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from research import donchian_prospective as p

class ProspectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'observer.db';self.boundary=20000*p.DAY
        p.init(self.path,'v1',self.boundary-p.DAY+1000);self.db=p.connect(self.path);self.t=self.boundary+1000
    def tearDown(self):self.db.close();self.temp.cleanup()
    def clock(self):self.t+=10;return self.t
    def fake_fetch(self,path,params=None):
        if path.endswith('/time'):return {'serverTime':self.clock()}
        start=params['startTime'];count=min(1000,(params['endTime']-start+1)//p.DAY)
        return [[start+i*p.DAY,str(100+i),str(100+i),str(100+i),str(100+i),'0',start+(i+1)*p.DAY-1] for i in range(count)]
    def quote(self,s):
        x=self.clock();return {'symbol':s,'price':'200','request_ms':x,'observed_ms':self.clock(),'server_before_ms':x,'server_after_ms':x}
    def cycle(self):
        with patch.object(p,'now',self.clock):p.cycle(self.db,'v1',fetch=self.fake_fetch,quote=self.quote,clock=self.clock)
    def test_decision_strict_exclusion_and_non_decreasing(self):
        self.assertEqual(p.decision(['100']*61,False,None),('HOLD',None))
        self.assertEqual(p.decision(['100']*60+['110'],False,None),('BUY','105'))
        self.assertEqual(p.decision(['100']*60+['104'],True,'105'),('SELL','105'))
        self.assertEqual(p.decision(['100']*60+['110'],True,'108'),('HOLD','108'))
    def test_warmup_parse_rejects_gaps_and_unclosed(self):
        raw=self.fake_fetch('/api/v3/klines',{'startTime':self.boundary-90*p.DAY,'endTime':self.boundary-1});days=p.parse_days(raw,self.t)
        self.assertEqual(len(days),90);raw.pop(4)
        with self.assertRaises(ValueError):p.parse_days(raw,self.t)
    def test_live_decision_then_quote_idempotent_and_restart(self):
        self.cycle();self.assertFalse(p.meta(self.db,'halted'));self.assertEqual(self.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],6)
        events=self.db.execute("SELECT id,at_ms,payload FROM events WHERE id LIKE ?",(str(self.boundary)+'%',)).fetchall();self.assertEqual(len(events),6)
        for e in events:
            v=json.loads(e['payload']);self.assertGreater(v['quote']['request_ms'],v['signal_commit_observed_ms']);self.assertGreaterEqual(v['quote']['request_ms'],self.boundary)
        self.db.close();self.db=p.connect(self.path);self.cycle();self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],12)
    def test_ambiguous_crash_quote_claim_halts_without_fill(self):
        self.assertTrue(p.quote_claim(self.db,'test_claim',self.clock()));self.db.close();self.db=p.connect(self.path);self.cycle()
        self.assertTrue(p.meta(self.db,'halted'));self.assertEqual(p.meta(self.db,'error'),'unfinished_quote_claim');self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
    def test_revision_keeps_original_and_stops(self):
        c={'open_ms':0,'close_ms':p.DAY-1,'open':'1','high':'1','low':'1','close':'1'}
        self.db.execute('BEGIN IMMEDIATE');p.ingest(self.db,'BTCUSDT',[c],1);self.db.commit();c2=dict(c,close='2',high='2')
        self.db.execute('BEGIN IMMEDIATE');p.ingest(self.db,'BTCUSDT',[c2],2);self.db.commit()
        self.assertTrue(p.meta(self.db,'halted'));self.assertEqual(self.db.execute('SELECT COUNT(*) FROM candle_versions').fetchone()[0],2)
        self.assertEqual(json.loads(self.db.execute('SELECT payload FROM candles').fetchone()[0])['close'],'1')
    def test_next_cycle_rechecks_earliest_accepted_day(self):
        self.cycle();events=self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0];original=self.fake_fetch
        def revised(path,params=None):
            result=original(path,params)
            if params and params.get('symbol')=='BTCUSDT':result[0][1:5]=['101','101','101','101']
            return result
        with patch.object(p,'now',self.clock):p.cycle(self.db,'v1',fetch=revised,quote=self.quote,clock=self.clock)
        self.assertTrue(p.meta(self.db,'halted'));self.assertEqual(p.meta(self.db,'error'),'source_history_changed');self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],events)
    def test_late_data_never_creates_strategy_trades(self):
        self.t=self.boundary+p.WINDOW+1000;self.cycle();self.assertTrue(p.meta(self.db,'halted'));self.assertEqual(p.meta(self.db,'error'),'missed_observation_window')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],0)
    def test_backup_restore_integrity_and_state(self):
        self.cycle();target=Path(self.temp.name)/'restore.db';p.backup(self.db,target)
        with sqlite3.connect(target) as r:
            self.assertEqual(r.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(r.execute('SELECT COUNT(*) FROM events').fetchone()[0],12)
        restored=p.connect(target)
        try:
            with patch.object(p,'now',self.clock):p.cycle(restored,'v1',fetch=self.fake_fetch,quote=self.quote,clock=self.clock)
            self.assertFalse(p.meta(restored,'halted'));self.assertEqual(restored.execute('SELECT COUNT(*) FROM events').fetchone()[0],12)
        finally:restored.close()
    def test_crash_before_decision_commit_rolls_back(self):
        self.db.execute('BEGIN IMMEDIATE');self.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,NULL,?,?,?)',(self.boundary,'BTCUSDT','base','BUY','90',self.clock(),'a','b','v1'))
        self.db.close();self.db=p.connect(self.path)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],0)
        self.cycle();self.assertEqual(self.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],6)
    def test_decimal_costs_and_minimum(self):
        a={'cash':'1000','qty':'0','cost':'0','level':None,'realized':'0'};q={'price':'100'}
        fill=p.apply_fill(a,'ETHUSDT','base','BUY','90',q);self.assertEqual(fill['quantity'],'0.0999');self.assertEqual(fill['fill_price'],'100.0500')
        self.assertEqual(p.apply_fill(a,'ETHUSDT','base','SELL',None,q)['fill_price'],'99.9500');self.assertLess(p.D(a['realized']),0)
    def test_delayed_or_skewed_quote_rejected(self):
        with patch.object(p,'now',side_effect=[100000,106001]),patch.object(p,'server_time',side_effect=[100000,106001]),patch.object(p,'request',return_value={'symbol':'BTCUSDT','price':'100'}):
            with self.assertRaisesRegex(ValueError,'freshness'):p.public_quote('BTCUSDT')
    def test_stale_open_position_valuation_is_unknown(self):
        self.cycle()
        with patch.object(p,'now',return_value=self.t+4000000):out=p.status(self.db)
        holdings=[a for a in out['accounts'] if a['kind']=='hold'];self.assertTrue(all(a['equity'] is None for a in holdings))
if __name__=='__main__':unittest.main()
