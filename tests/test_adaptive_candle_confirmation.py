import copy,hashlib,json,sqlite3,unittest
from research.adaptive_candle_confirmation import compare,describe,record_event,confirmed_for_decision,record_postclose,record_postclose_gap,PERIOD

def sample(received=200*PERIOD+600000):
 rows=[[i*PERIOD,'1','2','1','1','3',i*PERIOD+PERIOD-1] for i in range(199)]
 return {'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','raw_bars':rows,'received_ms':received,'server_ms':received-1,'bars':[(r[0],1.,2.,1.,1.) for r in rows[:-1]],'skip_bar':(rows[-1][0],1.,2.,1.,1.)}
class ConfirmationTests(unittest.TestCase):
 def test_accept_after_separation(self):
  a=sample();b=sample(a['received_ms']+10000);self.assertEqual(compare(a,b)['state'],'accepted')
 def test_pending_revision_and_identity(self):
  a=sample();b=sample(a['received_ms']+9999);self.assertEqual(compare(a,b)['state'],'awaiting_confirmation')
  b=sample(a['received_ms']+10000);b['raw_bars'][0][4]='1.1';b['bars'][0]=(b['bars'][0][0],1.,2.,1.,1.1);self.assertEqual(compare(a,b)['state'],'preaccept_revision')
  b['symbol']='ETHUSDT';self.assertRaisesRegex(ValueError,'identity',describe,b)
 def test_open_and_gap_rejected(self):
  a=sample();a['server_ms']=a['raw_bars'][-1][6];a['received_ms']=a['server_ms'];self.assertRaisesRegex(ValueError,'open_or_clock',describe,a)
  a=sample();a['raw_bars'][1][0]+=PERIOD;self.assertRaisesRegex(ValueError,'gap',describe,a)
 def test_persist_acceptance_replay_and_changed_after_acceptance(self):
  db=sqlite3.connect(':memory:',isolation_level=None);db.execute('CREATE TABLE epoch_conflicts(epoch_id INTEGER,start_ms INTEGER,payload TEXT,observed_ms INTEGER,UNIQUE(epoch_id,start_ms,payload))')
  a=sample();b=sample(a['received_ms']+10000);c=compare(a,b);event={'state':'accepted','sample':c['second'],'comparison':c}
  record_event(db,1,event,clock=lambda:c['accepted_ms']);record_event(db,1,event,clock=lambda:c['accepted_ms']);self.assertEqual(db.execute('select count(*) from candle_confirmations').fetchone()[0],1)
  d=copy.deepcopy(b);d['raw_bars'][0][4]='1.1';d['bars'][0]=(d['bars'][0][0],1.,2.,1.,1.1);d['received_ms']+=10000;d['server_ms']+=10000;e=copy.deepcopy(d);e['received_ms']+=10000;e['server_ms']+=10000
  revised=compare(d,e);record_event(db,1,{'state':'accepted','sample':revised['second'],'comparison':revised},clock=lambda:revised['accepted_ms'])
  self.assertEqual(db.execute("select count(*) from candle_lifecycle where state='contradiction_after_acceptance'").fetchone()[0],1)
  self.assertEqual(db.execute('select count(*) from epoch_conflicts').fetchone()[0],1);db.close()
 def test_confirmation_must_exist_before_signal_and_match_raw(self):
  db=sqlite3.connect(':memory:',isolation_level=None);a=sample();b=sample(a['received_ms']+10000);c=compare(a,b);record_event(db,1,{'state':'accepted','sample':c['second'],'comparison':c},clock=lambda:c['accepted_ms'])
  last=b['raw_bars'][-1][0];skip=[last+PERIOD,1.,2.,1.,1.];bars=[(x[0],1.,2.,1.,1.) for x in b['raw_bars']]
  row=[skip[0],'1','2','1','1','3',skip[0]+PERIOD-1]
  payload={'warm_confirmation':c,'bars':bars,'skip_bar':skip,'raw_bars':b['raw_bars']+[row]}
  # Move the synthetic decision boundary after its confirmation.
  payload['skip_bar'][0]=c['accepted_ms']//PERIOD*PERIOD
  self.assertTrue(confirmed_for_decision(db,1,payload))
  db.execute('UPDATE candle_confirmations SET recorded_ms=?',(payload['skip_bar'][0]+PERIOD,))
  self.assertFalse(confirmed_for_decision(db,1,payload))
  payload['raw_bars'][0][4]='1.1';self.assertFalse(confirmed_for_decision(db,1,payload));db.close()
 def test_crash_after_commit_cannot_create_durable_time(self):
  db=sqlite3.connect(':memory:',isolation_level=None);a=sample();b=sample(a['received_ms']+10000);c=compare(a,b);event={'state':'accepted','sample':c['second'],'comparison':c}
  def crash():raise RuntimeError('crash after acceptance commit')
  with self.assertRaisesRegex(RuntimeError,'after acceptance'):record_event(db,1,event,clock=crash)
  self.assertIsNone(db.execute('SELECT recorded_ms FROM candle_confirmations').fetchone()[0])
  record_event(db,1,event,clock=lambda:c['accepted_ms']+5000)
  self.assertEqual(db.execute('SELECT recorded_ms FROM candle_confirmations').fetchone()[0],c['accepted_ms']+5000);db.close()
 def test_postclose_revisions_before_and_after_acceptance(self):
  db=sqlite3.connect(':memory:',isolation_level=None);db.execute('CREATE TABLE epoch_conflicts(epoch_id INTEGER,start_ms INTEGER,payload TEXT,observed_ms INTEGER,UNIQUE(epoch_id,start_ms,payload))');db.execute('CREATE TABLE epoch_provisional(epoch_id INTEGER,start_ms INTEGER,payload TEXT,sha256 TEXT,raw_payload TEXT,raw_sha256 TEXT,received_ms INTEGER)')
  v=sample();open_ms=v['raw_bars'][-1][0];close=open_ms+PERIOD;v['received_ms']=close+30000;v['server_ms']=v['received_ms']-1
  raw=json.dumps(v['raw_bars'][-1],separators=(',',':'));sha=hashlib.sha256(raw.encode()).hexdigest();db.execute('INSERT INTO epoch_provisional VALUES(?,?,?,?,?,?,?)',(1,open_ms,'[]','x',raw,sha,close+1000))
  v['raw_bars'][-1][4]='1.1';v['skip_bar']=(open_ms,1.,2.,1.,1.1);self.assertEqual(record_postclose(db,1,v)['state'],'awaiting_confirmation');self.assertEqual(db.execute('select count(*) from epoch_conflicts').fetchone()[0],0)
  v['received_ms']+=30000;v['server_ms']+=30000;self.assertEqual(record_postclose(db,1,v)['state'],'accepted')
  v['raw_bars'][-1][4]='1.2';v['skip_bar']=(open_ms,1.,2.,1.,1.2);v['received_ms']+=30000;v['server_ms']+=30000;self.assertEqual(record_postclose(db,1,v)['state'],'contradiction_after_acceptance')
  self.assertEqual(db.execute('select count(*) from epoch_conflicts').fetchone()[0],1);db.close()
 def test_missing_preliminary_is_gap_but_later_confirmation_is_possible(self):
  db=sqlite3.connect(':memory:',isolation_level=None);db.execute('CREATE TABLE epoch_conflicts(epoch_id INTEGER,start_ms INTEGER,payload TEXT,observed_ms INTEGER,UNIQUE(epoch_id,start_ms,payload))');db.execute('CREATE TABLE epoch_provisional(epoch_id INTEGER,start_ms INTEGER,payload TEXT,sha256 TEXT,raw_payload TEXT,raw_sha256 TEXT,received_ms INTEGER)')
  v=sample();open_ms=v['raw_bars'][-1][0];v['received_ms']=open_ms+PERIOD+30000;v['server_ms']=v['received_ms']-1
  self.assertEqual(record_postclose(db,1,v)['state'],'awaiting_confirmation');v['received_ms']+=30000;v['server_ms']+=30000
  self.assertEqual(record_postclose(db,1,v)['state'],'accepted');self.assertEqual(db.execute("select count(*) from candle_close_observations where state='gap'").fetchone()[0],1);db.close()
 def test_three_successive_closed_boundaries_are_independent(self):
  db=sqlite3.connect(':memory:',isolation_level=None);db.execute('CREATE TABLE epoch_conflicts(epoch_id INTEGER,start_ms INTEGER,payload TEXT,observed_ms INTEGER,UNIQUE(epoch_id,start_ms,payload))');db.execute('CREATE TABLE epoch_provisional(epoch_id INTEGER,start_ms INTEGER,payload TEXT,sha256 TEXT,raw_payload TEXT,raw_sha256 TEXT,received_ms INTEGER)')
  for i in range(3):
   v=sample();shift=i*PERIOD
   for r in v['raw_bars']:r[0]+=shift;r[6]+=shift
   v['bars']=[(r[0],1.,2.,1.,1.) for r in v['raw_bars'][:-1]];open_ms=v['raw_bars'][-1][0];v['skip_bar']=(open_ms,1.,2.,1.,1.);v['received_ms']=open_ms+PERIOD+30000;v['server_ms']=v['received_ms']-1
   raw=json.dumps(v['raw_bars'][-1],separators=(',',':'));sha=hashlib.sha256(raw.encode()).hexdigest();db.execute('INSERT INTO epoch_provisional VALUES(?,?,?,?,?,?,?)',(1,open_ms,'[]','x',raw,sha,open_ms+PERIOD+1000))
   self.assertEqual(record_postclose(db,1,v)['state'],'accepted')
  self.assertEqual(db.execute("select count(distinct open_ms) from candle_close_observations where state='accepted'").fetchone()[0],3);db.close()
if __name__=='__main__':unittest.main()
