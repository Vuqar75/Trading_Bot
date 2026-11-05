"""Read-only local control and deterministic reports; never sends notifications."""
import argparse,datetime as dt,hashlib,json,os,re,sqlite3,subprocess,tempfile,time
from decimal import Decimal,InvalidOperation
from pathlib import Path

DAY=86400000;START=1793864747836;WINDOW=300000;HOUR=3600000;SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT');SCENARIOS=('base','stress');KINDS=('strategy','hold','cash')
STRATEGY='4ec16a367bc5a7e7fa5c80f9a1dbb33ec1fdf0283b949cf1dc91c59eb8a217da'
DB=Path('/var/lib/donchian-observer/observer.sqlite');CONTROL=Path('/var/lib/donchian-control');BACKUP=Path('/var/lib/donchian-backup/success.json');CODE=Path('/opt/donchian-observer/research')
def iso(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat() if ms is not None else None
def unpack(row):return json.loads(row[0]) if row else None
def meta(db,key):return unpack(db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def quote_recent(q,now):
    try:
        price=Decimal(q['price']);observed=q['observed_ms'];source=q['source_trade_ms']
        return price.is_finite() and price>0 and type(observed) is int and type(source) is int and q.get('quote_protocol')=='trade-time-v2' and 0<=observed-source<=5000 and 0<=now-observed<=3700000
    except (KeyError,TypeError,ValueError,InvalidOperation):return False
def process_state():
    r=subprocess.run(['systemctl','show','donchian-observer.service','-p','ActiveState','-p','MainPID','-p','WorkingDirectory','-p','User'],capture_output=True,text=True,timeout=5)
    values=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line);active=r.returncode==0 and values.get('ActiveState')=='active';valid=False
    if active:
        try:
            pid=int(values['MainPID']);args=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
            valid=pid>1 and values.get('User')=='donchian' and values.get('WorkingDirectory')=='/opt/donchian-observer' and b'research.donchian_service' in args and b'/var/lib/donchian-observer/observer.sqlite' in args
        except (OSError,KeyError,ValueError):valid=False
    return active,valid
def backup_state(marker,now):
    # Daily at 03:15 UTC with two minutes jitter and one hour grace.
    threshold=(now//DAY)*DAY+3*HOUR+15*60000
    if now<threshold+62*60000:threshold-=DAY
    try:
        m=json.loads(Path(marker).read_text());captured=int(m['manifest']['captured_utc_ms'])
        if m['manifest']['start_ms']!=START:return {'state':'invalid','reason':'wrong_start'}
        if captured>now+5000:return {'state':'invalid','reason':'future_timestamp'}
        return {'state':'fresh' if captured>=threshold else 'stale','captured_utc':iso(captured),'required_after_utc':iso(threshold),'sha256':m['sha256']}
    except (OSError,KeyError,ValueError,TypeError,json.JSONDecodeError):return {'state':'missing_or_invalid'}
def restore_state(marker,now):
    try:
        r=json.loads(Path(marker).read_text());age=now-int(r['checked_ms'])
        if age<0 or age>8*DAY:return {'state':'stale','checked_utc':iso(r['checked_ms'])}
        return {'state':r['state'],'checked_utc':iso(r['checked_ms']),'reason':r.get('reason'),'backup_sha256':r.get('backup_sha256')}
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):return {'state':'not_yet_verified'}
def collect(db,now,active,backup,restore,code=CODE,process_valid=True):
    start=meta(db,'start_ms');nxt=meta(db,'next_boundary');halted=bool(meta(db,'halted'));error=meta(db,'error');last=meta(db,'last_fetch_ms')
    if start!=START or not isinstance(nxt,int):raise ValueError('observer_identity_invalid')
    transition=unpack(db.execute("SELECT payload FROM events WHERE id='infra:quote-protocol-v2'").fetchone())
    version=meta(db,'version');expected=transition.get('adapter_sha256') if isinstance(transition,dict) else None
    try:versions_ok=version==STRATEGY and sha(code/'donchian_prospective.py')==version and expected is not None and sha(code/'donchian_service.py')==expected and transition.get('protocol')=='trade-time-v2' and meta(db,'quote_protocol')=='trade-time-v2'
    except OSError:versions_ok=False
    boundary=(now//DAY)*DAY;expected_day=boundary-DAY if now>=boundary+WINDOW else boundary-2*DAY
    latest={s:db.execute('SELECT MAX(open_ms) FROM candles WHERE symbol=?',(s,)).fetchone()[0] for s in SYMBOLS}
    per_symbol={s:(latest[s] is not None and latest[s]>=expected_day) for s in SYMBOLS}
    q={s:meta(db,'quote:'+s) for s in SYMBOLS}
    quote_fresh={s:quote_recent(q[s],now) for s in SYMBOLS}
    fetched=last is not None and 0<=now-last<=75*60000
    claims=[dict(x) for x in db.execute("SELECT id,claimed_ms FROM quotes WHERE status='claimed' ORDER BY claimed_ms")]
    # The currently open 5-minute protocol window is not classified as missed.
    due=now-WINDOW;begin=max(((start//DAY)+1)*DAY,((now-8*DAY)//DAY)*DAY)
    decisions=[dict(x) for x in db.execute('SELECT * FROM decisions WHERE boundary_ms>=? AND boundary_ms<=? ORDER BY boundary_ms,symbol,scenario',(begin,due))]
    evidence={};bad=[]
    for d in decisions:
        boundary_ms=d['boundary_ms'];seen=d['commit_seen_ms'];key=str(boundary_ms)
        entry=evidence.setdefault(key,{'count':0,'timely':True,'actions':{'HOLD':0,'BUY':0,'SELL':0},'executed':0,'rejected':0,'unconfirmed':0,'max_commit_delay_ms':0})
        entry['count']+=1;entry['actions'][d['action']]=entry['actions'].get(d['action'],0)+1
        if not isinstance(seen,int) or not boundary_ms<=seen<=boundary_ms+WINDOW:entry['timely']=False;bad.append('untimely_signal')
        else:entry['max_commit_delay_ms']=max(entry['max_commit_delay_ms'],seen-boundary_ms)
        if d['action'] in ('BUY','SELL'):
            event_id=f"{boundary_ms}:{d['symbol']}:{d['scenario']}";event=unpack(db.execute('SELECT payload FROM events WHERE id=?',(event_id,)).fetchone())
            if not event:entry['unconfirmed']+=1;bad.append('execution_unconfirmed')
            elif event.get('result')=='filled':
                quote=event.get('quote') or {};source=quote.get('source_trade_ms');observed=quote.get('observed_ms');requested=quote.get('request_ms')
                if event.get('side')!=d['action'] or not all(type(x) is int for x in (source,observed,requested,seen)) or source<=seen or requested<=seen or observed<requested or observed>boundary_ms+WINDOW or observed-source>5000 or observed<source or quote.get('quote_protocol')!='trade-time-v2' or quote.get('adapter_sha256')!=expected:bad.append('execution_time_invalid')
                entry['executed']+=1
            elif event.get('result')=='rejected_minimum_or_cash':entry['rejected']+=1
            else:entry['unconfirmed']+=1;bad.append('execution_unknown')
    first=((start//DAY)+1)*DAY;last_due=(due//DAY)*DAY
    expected_total=max(0,(last_due-first)//DAY+1)
    expected_recent=max(0,(last_due-begin)//DAY+1) if last_due>=begin else 0
    complete_recent=sum(1 for v in evidence.values() if v['count']==6 and v['timely'])
    if complete_recent<expected_recent:bad.append('missing_decision_windows')
    if any(not isinstance(c['claimed_ms'],int) or now-c['claimed_ms']>120000 for c in claims):bad.append('unfinished_quote_claim')
    if not all(per_symbol.values()):bad.append('expected_candle_missing')
    if now>nxt+WINDOW:bad.append('watermark_overdue')
    if halted:bad.append('protocol_halted')
    if error:bad.append('collector_error')
    if not versions_ok:bad.append('version_mismatch')
    account_rows=db.execute('SELECT symbol,scenario,kind,state FROM accounts').fetchall();actual_accounts={(x[0],x[1],x[2]) for x in account_rows};expected_accounts={(s,sc,k) for s in SYMBOLS for sc in SCENARIOS for k in KINDS}
    accounts_valid=actual_accounts==expected_accounts
    try:
        for row in account_rows:
            a=json.loads(row[3]);values={k:Decimal(a[k]) for k in ('cash','qty','cost','realized','peak','max_drawdown')}
            if any(not v.is_finite() for v in values.values()) or any(values[k]<0 for k in values if k!='realized'):accounts_valid=False
    except (KeyError,TypeError,ValueError,InvalidOperation):accounts_valid=False
    if not accounts_valid:bad.append('account_set_mismatch')
    if not fetched or not all(quote_fresh.values()):bad.append('data_stale_or_unconfirmed')
    if not active:bad.append('service_inactive')
    elif not process_valid:bad.append('service_process_unverified')
    if backup['state']!='fresh':bad.append('backup_'+backup['state'])
    if restore['state']!='verified':bad.append('restore_'+restore['state'])
    confirmed=min([last]+[q[s]['observed_ms'] for s in SYMBOLS]) if last is not None and not error and all(quote_fresh.values()) and fetched else None
    missed=db.execute("SELECT id,at_ms FROM events WHERE id>='missed:' AND id<'missed;' ORDER BY id DESC LIMIT 10").fetchall()
    return {'checked_utc':iso(now),'service_active':active,'service_process_verified':process_valid,'start_utc':iso(start),'strategy_sha256':version,'adapter_sha256':expected,'monitor_sha256':sha(Path(__file__)),'versions_ok':versions_ok,'accounts_valid':accounts_valid,'source_protocol':meta(db,'quote_protocol'),'halted':halted,'error':error,'last_input_received_utc':iso(last),'last_successful_collection_confirmed_utc':iso(confirmed),'inputs_recent':fetched,'latest_candle_utc':{s:iso(latest[s]) for s in SYMBOLS},'expected_candle_utc':iso(expected_day),'expected_candle_present':per_symbol,'quotes_recent':quote_fresh,'quote_utc':{s:iso(q[s].get('observed_ms')) if isinstance(q[s],dict) else None for s in SYMBOLS},'next_boundary_utc':iso(nxt),'missed_events':db.execute("SELECT COUNT(*) FROM events WHERE id>='missed:' AND id<'missed;'").fetchone()[0],'recent_missed_events':[{'id':x[0],'recorded_utc':iso(x[1])} for x in missed],'claims':claims,'expected_windows_total':expected_total,'expected_windows_last_8d':expected_recent,'complete_windows_last_8d':complete_recent,'recent_windows':evidence,'issues':sorted(set(bad)),'backup':backup,'restore':restore}

def account_report(db,now):
    result=[]
    for row in db.execute('SELECT * FROM accounts ORDER BY scenario,symbol,kind'):
        a=json.loads(row['state']);q=meta(db,'quote:'+row['symbol']);fresh=quote_recent(q,now)
        cash=Decimal(a['cash']);qty=Decimal(a['qty']);cost=Decimal(a['cost']);slip=Decimal('.0005') if row['scenario']=='base' else Decimal('.001')
        unreal=(qty*Decimal(q['price'])*(1-slip)*Decimal('.999')-cost) if qty and fresh else (Decimal(0) if not qty else None)
        result.append({'symbol':row['symbol'],'scenario':row['scenario'],'kind':row['kind'],'qty':str(qty),'cash':str(cash),'realized':a['realized'],'open_net':str(unreal) if unreal is not None else None,'net':str(Decimal(a['realized'])+unreal) if unreal is not None else None,'max_drawdown_pct':str(Decimal(a['max_drawdown'])*100),'exit_level':a['level']})
    return result

def historical_metrics(db,period_start=0):
    out={f'{sc}:{s}':{'closed':0,'fees':Decimal(0),'period_fees':Decimal(0),'period_closed':0,'period_realized':Decimal(0),'trades':[],'buy_open':None} for sc in SCENARIOS for s in SYMBOLS}
    for row in db.execute('SELECT id,at_ms,payload FROM events ORDER BY at_ms,id'):
        v=json.loads(row['payload']);kind=v.get('kind');parts=row['id'].split(':')
        if kind not in ('strategy','buy_and_hold') or len(parts)<3:continue
        s=parts[-2];sc=parts[-1]
        if (sc not in SCENARIOS) or (s not in SYMBOLS):continue
        # Fees and completed trades per scenario; do not pool replicated scenarios.
        key=f'{sc}:{s}';target=out[key];fee=Decimal(v.get('fee','0')) if v.get('result')=='filled' else Decimal(0)
        if kind=='strategy':
            target['fees']+=fee
            if row['at_ms']>=period_start:target['period_fees']+=fee
            if v.get('result')=='filled' and v.get('side')=='BUY':target['buy_open']=(Decimal(v['quantity'])*Decimal(v['fill_price'])+fee,row['at_ms'])
            elif v.get('result')=='filled' and v.get('side')=='SELL':
                if target['buy_open'] is None:target['unmatched_exit']=True;continue
                pnl=Decimal(v['quantity'])*Decimal(v['fill_price'])-fee-target['buy_open'][0];target['trades'].append({'net':str(pnl),'holding_ms':row['at_ms']-target['buy_open'][1]});target['closed']+=1
                if row['at_ms']>=period_start:target['period_closed']+=1;target['period_realized']+=pnl
                target['buy_open']=None
        else:
            target['hold_fees']=str(Decimal(target.get('hold_fees','0'))+fee)
            if row['at_ms']>=period_start:target['hold_period_fees']=str(Decimal(target.get('hold_period_fees','0'))+fee)
    for x in out.values():
        for k in ('fees','period_fees','period_realized'):x[k]=str(x[k])
        x.pop('buy_open',None)
    return out

def write_once(path,obj):
    path=Path(path);data=(json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode();fd,name=tempfile.mkstemp(prefix='.'+path.name+'.tmp.',dir=path.parent);tmp=Path(name)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        try:os.link(tmp,path);return True
        except FileExistsError:return False
    finally:tmp.unlink(missing_ok=True)
def replace(path,obj):
    path=Path(path);tmp=path.with_name('.'+path.name+'.tmp.'+str(os.getpid()));tmp.write_text(json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2)+'\n');os.chmod(tmp,0o600);os.replace(tmp,path)
def record_incidents(status,control,now):
    path=Path(control)/'incidents.json'
    try:data=json.loads(path.read_text())
    except FileNotFoundError:data={'open':{},'closed':[]}
    if not isinstance(data,dict) or not isinstance(data.get('open'),dict) or not isinstance(data.get('closed'),list):raise ValueError('incident_state_invalid')
    reasons=set(status['issues']);detail=status.get('error')
    if detail:reasons.add('collector_detail:'+(detail if isinstance(detail,str) and re.fullmatch('[a-zA-Z0-9_]{1,64}',detail) else 'unknown'))
    for reason in reasons:
        record=data['open'].setdefault(reason,{'reason':reason,'first_seen_utc':iso(now)})
        record['last_seen_utc']=iso(now)
    for reason in list(data['open']):
        if reason not in reasons:data['closed'].append({**data['open'].pop(reason),'resolved_observed_utc':iso(now)})
    replace(path,data)
def run(now=None,dbpath=DB,control=CONTROL,backup=BACKUP,code=CODE,active=None):
    now=int(time.time()*1000) if now is None else now;control=Path(control);control.mkdir(parents=True,exist_ok=True)
    b=backup_state(backup,now);restore=restore_state(control/'restore.json',now)
    db=sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True,timeout=.2,isolation_level=None);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        running,valid=process_state() if active is None else (active,active)
        status=collect(db,now,running,b,restore,code=code,process_valid=valid)
        current=dt.datetime.fromtimestamp(now/1000,dt.timezone.utc);y,w,_=current.isocalendar();week=control/f'week-{y}-W{w:02d}.json'
        checkpoints=[(90,control/'checkpoint-90.json'),(180,control/'checkpoint-180.json')]
        need=not week.exists() or any(now>=START+days*DAY and not path.exists() for days,path in checkpoints)
        if need:
            prior=[x for x in control.glob('week-????-W??.json') if x.name<week.name];previous=None
            if prior:
                try:previous=json.loads(max(prior).read_text())
                except (OSError,ValueError):previous=None
            period_start=int(dt.datetime.fromisoformat(previous['generated_utc']).timestamp()*1000) if previous and previous.get('generated_utc') else START
            if not START<=period_start<=now:raise ValueError('previous_report_time_invalid')
            accounts=account_report(db,now) if status['accounts_valid'] else [];metrics=historical_metrics(db,period_start)
            prior_accounts={(a['symbol'],a['scenario'],a['kind']):a for a in previous.get('accounts',[])} if previous else {}
            for a in accounts:
                prev=prior_accounts.get((a['symbol'],a['scenario'],a['kind']));old=prev.get('net') if prev else ('0' if not previous else None)
                a['period_net_change']=str(Decimal(a['net'])-Decimal(old)) if a['net'] is not None and old is not None else None
            coverage=[{'boundary_utc':iso(t),'count':n,'timely':n==6 and timely==6,'max_commit_delay_ms':delay} for t,n,timely,delay in db.execute('SELECT boundary_ms,COUNT(*),SUM(CASE WHEN commit_seen_ms>=boundary_ms AND commit_seen_ms<=boundary_ms+? THEN 1 ELSE 0 END),MAX(commit_seen_ms-boundary_ms) FROM decisions GROUP BY boundary_ms ORDER BY boundary_ms',(WINDOW,))]
            concentrations={}
            for scenario in SCENARIOS:
                trades=[Decimal(t['net']) for s in SYMBOLS for t in metrics[f'{scenario}:{s}']['trades']]
                market=[a for a in accounts if a['scenario']==scenario and a['kind']=='strategy'];nets=[Decimal(a['net']) for a in market if a['net'] is not None]
                total=sum(nets) if len(nets)==len(SYMBOLS) else None
                concentrations[scenario]={'closed_trades':len(trades),'sum_independent_accounts_net':str(total) if total is not None else None,'without_best_trade':str(total-max(trades)) if total is not None and trades else None,'without_best_asset':str(total-max(nets)) if total is not None else None,'worst_account_drawdown_pct':max((a['max_drawdown_pct'] for a in market),key=Decimal,default=None)}
                for s in SYMBOLS:
                    entry=metrics[f'{scenario}:{s}'];values=[Decimal(t['net']) for t in entry.pop('trades')]
                    entry['best_trade']=str(max(values)) if values else None;entry['worst_trade']=str(min(values)) if values else None
            report={'generated_utc':iso(now),'period_start_utc':iso(period_start),'period_end_utc':iso(now),'duration_days':(now-START)//DAY,'note':'Недельный результат не доказывает преимущества; недостаточно доказательств при малой выборке. Сценарии не суммируются. Период начинается с предыдущего доступного отчёта; текущий net — оценка, при блокировке не является подтверждённым результатом.','status':status,'all_observed_window_coverage':coverage,'accounts':accounts,'concentration_by_scenario':concentrations,'strategy_metrics_by_scenario_and_symbol':metrics,'checkpoint_90_utc':iso(START+90*DAY),'checkpoint_180_utc':iso(START+180*DAY)}
        db.rollback()
    finally:db.close()
    replace(control/'status.json',status)
    record_incidents(status,control,now)
    if need:
        write_once(week,report)
        for days,path in checkpoints:
            if now>=START+days*DAY:write_once(path,{**report,'checkpoint_days':days})
    return status
if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--state',default=str(DB));a.add_argument('--control',default=str(CONTROL));a.add_argument('--backup',default=str(BACKUP));args=a.parse_args();print(json.dumps(run(dbpath=args.state,control=args.control,backup=args.backup),ensure_ascii=False,sort_keys=True))
