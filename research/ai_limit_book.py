"""Isolated, transactional paper limit/bracket lifecycle. No network or API keys."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN

from trading_bot.paper_wallet_ledger import PaperWalletLedger
from .ai_limit_plan import canonical,digest,dec,positive,text,integer,quote,fresh,filters,validate,context,VERSION

APP_ID=0x41494252
CUSTOM={'bracket_meta','bracket_plans','bracket_inputs','bracket_fills','bracket_funding','bracket_messages','bracket_sources'}
BASE={'ledger_meta','wallet_state','ledger_events','ledger_postings','transfer_proposals'}
B='ai_bracket:'


class Book:
    def __init__(self,directory,*,clock=lambda:int(time.time()*1000),fault=lambda stage:None):
        self.root=Path(directory);self.clock=clock;self.fault=fault
        if self.root.is_symlink():raise ValueError('private_directory_required')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.stat().st_uid!=os.geteuid() or self.root.stat().st_mode&0o077:raise ValueError('private_directory_required')
        path=self.root/'paper.sqlite'
        if path.is_symlink():raise ValueError('private_database_required')
        if path.exists():
            self.db=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=15)
            try:
                if self.db.execute('PRAGMA application_id').fetchone()[0]!=APP_ID:raise ValueError('wrong_paper_database')
                names={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if names!=BASE|CUSTOM:raise ValueError('paper_schema_changed')
                self.ledger=object.__new__(PaperWalletLedger);self.ledger.db=self.db;self.ledger.fault=lambda stage:None
                self.db.row_factory=sqlite3.Row
                if self.get('version')!=VERSION:raise ValueError('paper_policy_changed')
            except BaseException:self.db.close();raise
        else:
            self.ledger=PaperWalletLedger(path);self.db=self.ledger.db
            os.chmod(path,0o600)
            with self.ledger.transaction():
                self.db.execute('PRAGMA application_id='+str(APP_ID))
                self.db.execute('CREATE TABLE bracket_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE bracket_plans(id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL)')
                self.db.execute('CREATE TABLE bracket_inputs(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,payload TEXT NOT NULL)')
                self.db.execute('CREATE TABLE bracket_fills(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES bracket_plans(id),kind TEXT NOT NULL,stamp INTEGER NOT NULL,qty TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,reason TEXT NOT NULL)')
                self.db.execute('CREATE TABLE bracket_funding(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,payload TEXT NOT NULL)')
                self.db.execute('CREATE TABLE bracket_sources(id TEXT PRIMARY KEY,capture TEXT NOT NULL)')
                self.db.execute('CREATE TABLE bracket_messages(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                self.db.execute("CREATE TRIGGER immutable_bracket_plan_payload BEFORE UPDATE OF id,payload ON bracket_plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END")
                self.db.execute("CREATE TRIGGER immutable_bracket_plan_delete BEFORE DELETE ON bracket_plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END")
                for table in ('bracket_inputs','bracket_fills','bracket_funding','bracket_messages','bracket_sources'):
                    for verb in ('UPDATE','DELETE'):
                        self.db.execute(f"CREATE TRIGGER immutable_{table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable bracket evidence'); END")
                now=integer(clock());self.set('version',VERSION);self.set('paused',True);self.set('blocked',None);self.set('started_ms',now)
                self.set('funding_through',now-1);self.set('last_market',{})
                self.ledger._post('bracket:opening','RESEARCH_OPENING',{'virtual_only':True,'version':VERSION},
                    [(B+'free','USDT',Decimal(1000)),('external:research_capital','USDT',Decimal(-1000))],created=now/1000)
        self.db.execute('PRAGMA foreign_keys=ON');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA busy_timeout=15000')
        self.verify()

    def close(self):self.db.close()
    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM bracket_meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,key,value):self.db.execute('INSERT OR REPLACE INTO bracket_meta VALUES(?,?)',(key,canonical(value)))
    def balance(self,name):return self.ledger.balance(B+name)
    def plans(self):return [(json.loads(r['payload']),json.loads(r['state'])) for r in self.db.execute('SELECT * FROM bracket_plans ORDER BY rowid')]
    def active(self):
        values=[(p,s) for p,s in self.plans() if s['status'] not in ('CLOSED','CANCELED')]
        if len(values)>1:raise ValueError('multiple_active_plans')
        return values[0] if values else None
    def save(self,p,s):self.db.execute('UPDATE bracket_plans SET state=? WHERE id=?',(canonical(s),p['plan_id']))
    def message(self,id,value):self.db.execute('INSERT OR IGNORE INTO bracket_messages VALUES(?,?)',(id,canonical(value)))

    @contextmanager
    def transaction(self):
        with self.ledger.transaction():
            yield
            if self.balance('free')<0:self.set('blocked','negative_paper_equity')
            self.verify();self.fault('before_commit')
        self.fault('after_commit')

    def post(self,id,kind,details,changes,now):
        entries=[(account if account.startswith('external:') else B+account,'USDT',v) for account,v in changes if v]
        if entries:self.ledger._post(id,kind,details,entries,created=now/1000)

    def verify(self):
        if not self.ledger.reconcile()['balanced']:raise ValueError('ledger_reconciliation_failed')
        expected=Decimal(1000)
        for p,s in self.plans():expected+=dec(s['gross'])-dec(s['fees'])+dec(s['funding'])
        actual=sum((self.balance(k) for k in ('free','reserved','margin','funding_buffer')),Decimal(0))
        if actual!=expected:raise ValueError('paper_capital_mismatch')
        if any(self.balance(k)<0 for k in ('reserved','margin','funding_buffer')):raise ValueError('negative_reserve_or_margin')
        self.active()
        return {'balanced':True,'equity_before_unrealized':text(actual)}

    def pause(self,enabled=True):
        if type(enabled) is not bool:raise ValueError('invalid_pause')
        with self.transaction():
            self.set('paused',enabled)
            active=self.active()
            if enabled and active:
                p,s=active;self.cancel_entry(p,s,'PAUSE',self.clock());self.finalize(p,s,self.clock());self.save(p,s)

    def register_source(self,snapshot,capture):
        expected=context(capture,snapshot['quote'],snapshot['filters'],snapshot['news'],snapshot['as_of_ms'])
        if expected!=snapshot:raise ValueError('source_context_recomputation_failed')
        with self.transaction():
            prior=self.db.execute('SELECT capture FROM bracket_sources WHERE id=?',(snapshot['snapshot_id'],)).fetchone()
            if prior:
                if json.loads(prior[0])!=capture:raise ValueError('source_capture_conflict')
            else:self.db.execute('INSERT INTO bracket_sources VALUES(?,?)',(snapshot['snapshot_id'],canonical(capture)))

    def accept(self,snapshot,decision,*,provenance,execution_quote=None):
        if not isinstance(provenance,dict) or set(provenance)!={'kind','response_id','model'} or provenance['kind'] not in ('fixture','budget_response'):raise ValueError('analyst_provenance_required')
        if not all(isinstance(provenance[k],str) and 1<=len(provenance[k])<=150 for k in ('response_id','model')):raise ValueError('analyst_provenance_required')
        with self.transaction():
            now=self.clock();ident=digest([snapshot.get('snapshot_id'),decision])
            prior=self.db.execute('SELECT payload,state FROM bracket_plans WHERE id=?',(ident,)).fetchone()
            if prior:
                oldplan=json.loads(prior[0])
                if oldplan['provenance']!=provenance or oldplan['snapshot']!=snapshot or oldplan['decision']!=decision:raise ValueError('plan_provenance_conflict')
                return json.loads(prior[1])
            p=validate(snapshot,decision,now)
            if provenance['kind']=='budget_response' and not self.db.execute('SELECT 1 FROM bracket_sources WHERE id=?',(snapshot['snapshot_id'],)).fetchone():raise ValueError('source_capture_not_registered')
            if p['direction']=='WAIT':
                self.set('analyst_last',{'status':'WAIT','symbol':p['symbol'],'thesis':decision['thesis'],'as_of_ms':now,'provenance':provenance})
                self.message(ident,{'status':'WAIT','thesis':decision['thesis'],'symbol':p['symbol']})
                return {'status':'WAIT','plan_id':ident}
            current=execution_quote or self.get('quote:'+p['symbol']) or snapshot['quote']
            quote(current,p['symbol'],now,entry=True);filters(snapshot['filters'],now)
            if not (dec(p['entry'])<dec(current['ask']) if p['direction']=='LONG' else dec(p['entry'])>dec(current['bid'])):raise ValueError('limit_would_take_now')
            if self.get('paused') or self.get('blocked') or self.active():raise ValueError('entry_paused_blocked_or_busy')
            due=self.get('next_due:'+p['symbol'])
            if now-self.get('funding_through')>120000 or (due is not None and due<=now and due>self.get('funding_through')) or (provenance['kind']=='budget_response' and due is None):raise ValueError('funding_coverage_missing')
            if self.balance('free')<dec(p['reserve']):raise ValueError('insufficient_paper_cash')
            p['provenance']=provenance
            self.set('analyst_last',{'status':'PLAN','symbol':p['symbol'],'thesis':decision['thesis'],'as_of_ms':now,'provenance':provenance})
            s={'status':'WORKING','armed_ms':now,'remaining':p['quantity'],'open_qty':'0','reserved':p['reserve'],
                'buffer':'0','gross':'0','fees':'0','funding':'0','opened_ms':None,'last_fill_ms':None,'close_reason':None}
            self.db.execute('INSERT INTO bracket_plans VALUES(?,?,?)',(ident,canonical(p),canonical(s)))
            self.post(ident+':reserve','LIMIT_RESERVE',{'plan_id':ident},[('free',-dec(p['reserve'])),('reserved',dec(p['reserve']))],now)
            self.set('quote:'+p['symbol'],current);self.set('filters:'+p['symbol'],snapshot['filters']);self.message(ident,{'status':'WORKING','plan_id':ident,'symbol':p['symbol']})
            return dict(s)

    def cancel_entry(self,p,s,reason,now):
        value=dec(s['reserved'])
        if value:
            self.post(p['plan_id']+':cancel-entry','CANCEL_REMAINDER',{'reason':reason},[('reserved',-value),('free',value)],now)
        s['remaining']='0';s['reserved']='0'
        if dec(s['open_qty'])==0 and s['opened_ms'] is None:s['status']='CANCELED'

    def finalize(self,p,s,now):
        if dec(s['remaining']) or dec(s['open_qty']):return
        if s['opened_ms'] is None:s['status']='CANCELED';return
        if self.get('blocked') or self.get('funding_through')<s['last_fill_ms']:
            s['status']='RECONCILING';return
        buffer=dec(s['buffer'])
        if buffer:self.post(p['plan_id']+':release-buffer','FUNDING_FINALIZED',{'through_ms':self.get('funding_through')},[('funding_buffer',-buffer),('free',buffer)],now)
        s['buffer']='0';s['status']='CLOSED';s['closed_ms']=now
        self.message(p['plan_id']+':closed',{'status':'CLOSED','plan_id':p['plan_id'],'net':text(dec(s['gross'])-dec(s['fees'])+dec(s['funding']))})

    def fill(self,p,s,kind,qty,price,stamp,key,reason):
        entry=dec(p['entry']);fee=qty*price*dec(p['fee']);side=1 if p['direction']=='LONG' else -1
        if kind=='ENTRY':
            value=qty*entry;buffer=value*dec(p['funding_buffer_rate']);reserved=value+fee+buffer
            s['remaining']=text(dec(s['remaining'])-qty);s['reserved']=text(dec(s['reserved'])-reserved)
            s['open_qty']=text(dec(s['open_qty'])+qty);s['buffer']=text(dec(s['buffer'])+buffer)
            if s['opened_ms'] is None:s['opened_ms']=stamp
            changes=[('reserved',-reserved),('margin',value),('fees',fee),('funding_buffer',buffer)]
            s['status']='PARTIAL' if dec(s['remaining']) else 'OPEN'
        else:
            self.cancel_entry(p,s,reason,stamp)
            value=qty*entry;gross=side*qty*(price-entry)
            s['open_qty']=text(dec(s['open_qty'])-qty);s['gross']=text(dec(s['gross'])+gross)
            changes=[('margin',-value),('free',value+gross-fee),('fees',fee),('external:price_pnl',-gross)]
        s['fees']=text(dec(s['fees'])+fee);s['last_fill_ms']=stamp
        self.db.execute('INSERT INTO bracket_fills VALUES(?,?,?,?,?,?,?,?)',(key,p['plan_id'],kind,stamp,text(qty),text(price),text(fee),reason))
        self.post(key,'PAPER_'+kind,{'plan_id':p['plan_id'],'reason':reason,'stamp':stamp,'qty':text(qty),'price':text(price)},changes,stamp)
        self.message(key,{'kind':kind,'reason':reason,'symbol':p['symbol'],'qty':text(qty),'price':text(price),'paper_only':True})
        if kind!='ENTRY':self.finalize(p,s,stamp)

    def protect(self,p,s,now):
        if not dec(s['open_qty']):return
        book=self.get('quote:'+p['symbol'])
        try:quote(book,p['symbol'],now)
        except (ValueError,TypeError,KeyError):return
        side=1 if p['direction']=='LONG' else -1;mark=dec(book['mark'])
        if side*(mark-dec(p['stop']))<=0 or side*(dec(book['bid' if side==1 else 'ask'])-dec(p['stop']))<=0:s['close_reason']='SL'
        if s['opened_ms'] is not None and now>=s['opened_ms']+p['hold_ms'] and not s['close_reason']:s['close_reason']='TIME'
        if not s['close_reason']:return
        s['status']='EXIT_PENDING';self.cancel_entry(p,s,s['close_reason'],now)
        sidekey='bid' if side==1 else 'ask';consumed=self.get('used-quote:'+p['symbol']+':'+book.get('liquidity_id',book['id'])+':'+sidekey,'0')
        available=max(Decimal(0),dec(book[sidekey+'_qty'])-dec(consumed));step=dec(p['snapshot']['filters']['step_size'])
        qty=(min(dec(s['open_qty']),available)/step).to_integral_value(rounding=ROUND_DOWN)*step
        if qty<=0:return
        price=dec(book[sidekey])*(1-side*dec(p['stop_slippage']))
        key='market:'+p['plan_id']+':'+book['id']+':'+s['open_qty']
        self.fill(p,s,'EXIT',qty,price,now,key,s['close_reason'])
        self.set('used-quote:'+p['symbol']+':'+book.get('liquidity_id',book['id'])+':'+sidekey,text(dec(consumed)+qty))

    def trade_gap(self,symbol,detail):
        with self.transaction():
            now=self.clock();self.message('trade-gap:'+symbol+':'+str(now),{'kind':'DATA_GAP','symbol':symbol,'detail':str(detail)[:160],'at_ms':now})
            self.set('last_trade_gap',{'symbol':symbol,'at_ms':now})
            active=self.active()
            if active and active[0]['symbol']==symbol:
                p,s=active;self.cancel_entry(p,s,'TRADE_GAP',now);self.protect(p,s,now);self.finalize(p,s,now);self.save(p,s)

    def refresh_filters(self,symbol,value):
        with self.transaction():
            filters(value,self.clock())
            old=self.get('filters:'+symbol)
            changed=old and {k:v for k,v in old.items() if k!='as_of_ms'}!={k:v for k,v in value.items() if k!='as_of_ms'}
            self.set('filters:'+symbol,value)
            active=self.active()
            if changed and active and active[0]['symbol']==symbol:
                p,s=active;self.cancel_entry(p,s,'FILTERS_CHANGED',self.clock());self.finalize(p,s,self.clock());self.save(p,s)

    def pulse(self):
        with self.transaction():
            now=self.clock();active=self.active()
            if not active:return
            p,s=active
            if now>=p['expires_ms'] or self.get('paused') or self.get('blocked'):self.cancel_entry(p,s,'EXPIRED_OR_PAUSED',now)
            if dec(s['open_qty']) and s['opened_ms'] is not None and now>=s['opened_ms']+p['hold_ms']:
                s['close_reason']=s['close_reason'] or 'TIME';s['status']='EXIT_PENDING';self.cancel_entry(p,s,s['close_reason'],now)
            self.protect(p,s,now);self.finalize(p,s,now);self.save(p,s)

    def request_close(self,plan_id):
        with self.transaction():
            active=self.active()
            if not active or active[0]['plan_id']!=plan_id:raise ValueError('active_plan_missing')
            p,s=active;now=self.clock();self.cancel_entry(p,s,'MANUAL',now)
            if dec(s['open_qty']):s['close_reason']='MANUAL';s['status']='EXIT_PENDING';self.protect(p,s,now)
            self.finalize(p,s,now);self.save(p,s)

    def market(self,kind,event):
        if kind not in ('quote','trade'):raise ValueError('market_kind')
        symbol=event.get('symbol')
        if symbol not in ('BTCUSDT','ETHUSDT','SOLUSDT') or not isinstance(event.get('id'),str) or not 1<=len(event['id'])<=120:raise ValueError('market_identity')
        key=kind+':'+symbol+':'+event['id'];fingerprint=digest({k:v for k,v in event.items() if k!='received_ms'})
        with self.transaction():
            old=self.db.execute('SELECT fingerprint FROM bracket_inputs WHERE id=?',(key,)).fetchone()
            if old:
                if old[0]!=fingerprint:raise ValueError('market_event_revision')
                return {'status':'duplicate'}
            now=self.clock();fresh(event,now)
            if kind=='quote':quote(event,symbol,now)
            else:
                if set(event)!={'id','symbol','event_ms','received_ms','price','qty','buyer_maker'} or type(event['buyer_maker']) is not bool:raise ValueError('trade_shape')
                positive(event['price']);positive(event['qty'])
            times=self.get('last_market');stream=kind+':'+symbol
            if event['event_ms']<times.get(stream,0):raise ValueError('market_out_of_order')
            times[stream]=event['event_ms'];self.set('last_market',times)
            self.db.execute('INSERT INTO bracket_inputs VALUES(?,?,?)',(key,fingerprint,canonical(event)))
            if kind=='quote':self.set('quote:'+symbol,event)
            active=self.active()
            if not active or active[0]['symbol']!=symbol:return {'status':'recorded'}
            p,s=active
            if now>=p['expires_ms'] or self.get('paused') or self.get('blocked'):self.cancel_entry(p,s,'EXPIRED_OR_PAUSED',now)
            # Protection has priority over discretionary TP and new entry.
            self.protect(p,s,now)
            if kind=='trade' and event['event_ms']>s['armed_ms'] and not s['close_reason']:
                side=1 if p['direction']=='LONG' else -1;price=dec(event['price']);step=dec(p['snapshot']['filters']['step_size'])
                volume=(dec(event['qty'])/step).to_integral_value(rounding=ROUND_DOWN)*step
                entry_cross=price<dec(p['entry']) if side==1 else price>dec(p['entry'])
                target_cross=price>dec(p['target']) if side==1 else price<dec(p['target'])
                if dec(s['open_qty']) and target_cross and event['buyer_maker']==(side==-1):
                    qty=min(dec(s['open_qty']),volume)
                    if qty>0:self.fill(p,s,'EXIT',qty,dec(p['target']),event['event_ms'],key+':target','TP')
                elif dec(s['remaining']) and entry_cross and event['buyer_maker']==(side==1) and event['event_ms']<p['expires_ms']:
                    try:
                        quote(self.get('quote:'+symbol),symbol,now,entry=True);filters(self.get('filters:'+symbol),now)
                        due=self.get('next_due:'+symbol)
                        if now-self.get('funding_through')>120000 or (due is not None and due<=now and due>self.get('funding_through')):raise ValueError('funding_coverage_stale')
                    except (ValueError,TypeError,KeyError):
                        self.cancel_entry(p,s,'ENTRY_EVIDENCE_STALE',now);volume=Decimal(0)
                    qty=min(dec(s['remaining']),volume)
                    if qty>0:self.fill(p,s,'ENTRY',qty,dec(p['entry']),event['event_ms'],key+':entry','LIMIT_TRADE_THROUGH')
                    self.protect(p,s,now)
            self.finalize(p,s,now);self.save(p,s)
            return {'status':s['status']}

    def quantity_at(self,plan_id,stamp):
        fills=self.db.execute('SELECT * FROM bracket_fills WHERE plan_id=? AND stamp<=? ORDER BY rowid',(plan_id,stamp)).fetchall()
        qty=sum((dec(f['qty'])*(1 if f['kind']=='ENTRY' else -1) for f in fills if f['kind']=='ENTRY' or f['stamp']<stamp),Decimal(0))
        return qty,any(f['stamp']==stamp for f in fills)

    def funding(self,start_ms,through_ms,events):
        with self.transaction():
            now=self.clock();integer(start_ms);integer(through_ms)
            if not start_ms<=self.get('funding_through')+1<=through_ms+1 or through_ms>now:raise ValueError('funding_coverage_gap_or_future')
            if not isinstance(events,list) or len(events)>100:raise ValueError('funding_shape')
            seen=set()
            for event in events:
                if set(event)!={'symbol','event_ms','rate','mark'} or event['symbol'] not in ('BTCUSDT','ETHUSDT','SOLUSDT'):raise ValueError('funding_shape')
                stamp=integer(event['event_ms']);rate=dec(event['rate']);positive(event['mark'])
                if not start_ms<=stamp<=through_ms or abs(rate)>1:raise ValueError('funding_window')
                key=event['symbol']+':'+str(stamp)
                if key in seen:raise ValueError('duplicate_funding')
                seen.add(key);old=self.db.execute('SELECT fingerprint FROM bracket_funding WHERE id=?',(key,)).fetchone()
                if old and old[0]!=digest(event):
                    self.set('blocked','funding_history_conflict');self.message('funding-conflict:'+digest(event),{'status':'BLOCKED','reason':'funding_history_conflict'})
                    return {'status':'funding_history_conflict'}
                if old:continue
                if stamp<=self.get('funding_through') and any(p['symbol']==event['symbol'] and self.quantity_at(p['plan_id'],stamp)[0]>0 for p,s in self.plans()):
                    self.set('blocked','late_missing_funding_event');return {'status':'late_missing_funding_event'}
                self.db.execute('INSERT INTO bracket_funding VALUES(?,?,?)',(key,digest(event),canonical(event)))
                for p,s in self.plans():
                    if p['symbol']!=event['symbol']:continue
                    qty,ambiguous=self.quantity_at(p['plan_id'],stamp)
                    if qty<=0:continue
                    side=1 if p['direction']=='LONG' else -1;value=-side*qty*dec(event['mark'])*rate
                    if ambiguous:value=min(value,Decimal(0))
                    if value:self.post('funding:'+key+':'+p['plan_id'],'FUNDING',event,[('free',value),('external:funding',-value)],now)
                    s['funding']=text(dec(s['funding'])+value);self.save(p,s)
            self.set('funding_through',max(through_ms,self.get('funding_through')))
            for p,s in self.plans():
                if s['status']=='RECONCILING':self.finalize(p,s,now);self.save(p,s)
            if self.balance('free')<0:self.set('blocked','negative_paper_equity')
            return {'status':'covered','through_ms':self.get('funding_through')}

    def snapshot(self):
        with self.ledger.transaction():
            self.verify();now=self.clock();items=[]
            for p,s in self.plans():
                items.append({'plan_id':p['plan_id'],'symbol':p['symbol'],'direction':p['direction'],'entry':p['entry'],'stop':p['stop'],'target':p['target'],
                    'provenance':p['provenance'],'thesis':p['decision']['thesis'],'expires_ms':p['expires_ms'],**s,
                    'net_provisional':text(dec(s['gross'])-dec(s['fees'])+dec(s['funding']))})
            return {'version':VERSION,'paper_only':True,'as_of_ms':now,'paused':self.get('paused'),'blocked':self.get('blocked'),
                'quote_asof_ms':{sym:(self.get('quote:'+sym) or {}).get('event_ms') for sym in ('BTCUSDT','ETHUSDT','SOLUSDT')},'api_summary':self.get('last_api_summary'),'funding_through_ms':self.get('funding_through'),'analyst_last':self.get('analyst_last'),'balances':{k:text(self.balance(k)) for k in ('free','reserved','margin','funding_buffer')},
                'plans':items[-30:],'fills':self.db.execute('SELECT count(*) FROM bracket_fills').fetchone()[0],'ledger_balanced':True,
                'execution_model':'strict_trade_through_and_book_market_not_exchange_fills'}

    def backup(self,target):
        target=Path(target)
        if target.exists():raise ValueError('backup_target_exists')
        with sqlite3.connect(target) as dst:self.db.backup(dst)
        with sqlite3.connect(target) as check:
            if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup_integrity')
        os.chmod(target,0o600)
        return target
