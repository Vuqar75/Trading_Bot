import json,os,sys,time,threading,tempfile,sqlite3
from pathlib import Path
from tests.test_ai_limit_stream import StreamTests
from research.ai_limit_runtime import ResponseReader,Publisher,atomic,ingest,trusted
from research.ai_limit_plan import digest,canonical
mode=sys.argv[1]
if mode=='old':
 from research.ai_limit_book import Book,dec,text
 from research.ai_limit_runtime import Poller
 from decimal import Decimal
 def old_active(self):
  values=[(p,s) for p,s in self.plans() if s['status'] not in ('CLOSED','CANCELED')]
  if len(values)>1:raise ValueError('multiple_active_plans')
  return values[0] if values else None
 def old_verify(self):
  if not self.ledger.reconcile()['balanced']:raise ValueError('ledger_reconciliation_failed')
  expected=Decimal(1000)
  for p,state in self.plans():expected+=dec(state['gross'])-dec(state['fees'])+dec(state['funding'])
  actual=sum((self.balance(k) for k in ('free','reserved','margin','funding_buffer')),Decimal(0))
  if actual!=expected:raise ValueError('paper_capital_mismatch')
  if any(self.balance(k)<0 for k in ('reserved','margin','funding_buffer')):raise ValueError('negative_reserve_or_margin')
  self.active();return {'balanced':True,'equity_before_unrealized':text(actual)}
 Book.active=old_active;Book.verify=old_verify
 Poller.cycle=Poller._cycle
 # Old ingestion/publication and .02s sleep are selected below.
test=StreamTests();test.setUp()
from research.ai_limit_book import Book
root=test.book.root;test.book.close()
source=sqlite3.connect('file:/var/lib/ai-bracket-study/paper.sqlite?mode=ro',uri=True);target=sqlite3.connect(root/'paper.sqlite');source.backup(target);target.close();source.close()
test.book=Book(root,clock=lambda:test.now);f=test.stream();test.bootstrap(f)
try:
 env=json.load(open('/var/lib/tradingbot-ai-executor-public/bracket-response.json'));ident='model:'+env['analysis']['provenance']['response_id']
 test.book.db.execute('INSERT OR IGNORE INTO bracket_inputs VALUES(?,?,?)',(ident,digest(env),canonical(env)))
 response=Path(test.temp.name)/'response.json';atomic(response,env,mode=0o600);out=Path(test.temp.name)/'orders.json'
 reader=ResponseReader();publisher=Publisher();initial_gap=test.book.get('last_trade_gap');stop=threading.Event();start=time.monotonic();cpu=time.process_time();initial=test.now
 def producer():
  i=2
  while not stop.is_set():
   for j in range(100):test.send(f,i);i+=1
   stop.wait(.05)
 t=threading.Thread(target=producer);t.start();cycles=0;gaps=set();dur=[];phase={k:[] for k in ['poll','ingest','snapshot','publish']}
 while time.monotonic()-start<12:
  mark=time.monotonic();test.now=initial+int((mark-start)*1000)
  result=test.poller.cycle();now=time.monotonic();phase['poll'].append(now-mark);mark=now
  if mode=='old':ingest(test.book,trusted(response,os.geteuid()))
  else:reader.apply(test.book,response,os.geteuid())
  now=time.monotonic();phase['ingest'].append(now-mark);mark=now
  result=test.book.snapshot()|{k:v for k,v in result.items() if k in ('market_errors','funding_error','last_trade_gap','trade_feeds')}
  now=time.monotonic();phase['snapshot'].append(now-mark);mark=now
  if mode=='old':atomic(out,result)
  else:publisher.publish(out,result)
  now=time.monotonic();phase['publish'].append(now-mark)
  if result.get('last_trade_gap'):gaps.add(result['last_trade_gap']['at_ms'])
  cycles+=1;dur.append(now-(start if cycles==1 else prior));prior=now;time.sleep(.02 if mode=='old' else test.poller.sleep_after_cycle())
 stop.set();t.join();elapsed=time.monotonic()-start
 report={'mode':mode,'elapsed':elapsed,'cycles':cycles,'cpu_seconds':time.process_time()-cpu,'received':f.states['BTCUSDT']['received'],'queue':len(f.states['BTCUSDT']['queue']),'highwater':f.states['BTCUSDT']['highwater'],'gaps':len(gaps-{initial_gap['at_ms']} if initial_gap else gaps),'max_cycle_s':max(dur),'phase_total_s':{k:sum(v) for k,v in phase.items()}}
 print(json.dumps(report),flush=True)
finally:test.tearDown()
