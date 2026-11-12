"""Bounded observations of saved price levels. No orders, fills, PnL or API IO."""
import copy,hashlib,json,math,os,tempfile
from pathlib import Path
from .technical_trade_scenarios import validate as validate_plan

BAR=900000
LIMIT=64
MAX_BYTES=512000
VERSION='technical-plan-history-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate(value):
    if not isinstance(value,dict) or value.get('version')!=VERSION or value.get('orders_enabled') is not False or value.get('fills_confirmed') is not False:
        raise ValueError('history_contract')
    start=value.get('started_ms');checked=value.get('checked_ms');records=value.get('records');discarded=value.get('discarded')
    if type(start) is not int or type(checked) is not int or not 0<start<=checked or type(discarded) is not int or discarded<0 or not isinstance(records,list) or len(records)>LIMIT:
        raise ValueError('history_shape')
    ids=set()
    for r in records:
        if not isinstance(r,dict) or not isinstance(r.get('packet'),dict):raise ValueError('history_record')
        p=r['packet']
        if any(not isinstance(p.get(k),dict) for k in ('limits','features','ml','trade_scenarios')):raise ValueError('history_record')
        plan=validate_plan(p['trade_scenarios'],p)
        if not isinstance(p['ml'].get('model_id'),str) or not 1<=len(p['ml']['model_id'])<=100:raise ValueError('history_model_identity')
        if plan['status']!='conditional_only' or type(r.get('recorded_ms')) is not int or not start<=r['recorded_ms']<=checked or not p['as_of_ms']<=r['recorded_ms']<p['expires_ms']:
            raise ValueError('history_record_time')
        identity=digest([p['symbol'],p['signal_ms'],p['ml']['model_id']])
        if r.get('id')!=identity or identity in ids:raise ValueError('history_identity')
        reference=r.get('source_packet_id')
        if reference is not None and (not isinstance(reference,str) or len(reference)!=64 or any(x not in '0123456789abcdef' for x in reference)):raise ValueError('history_packet_reference')
        ids.add(identity)
        if r.get('audit_start_ms')!=p['signal_ms']+BAR or r.get('audit_end_ms')!=p['signal_ms']+16*BAR:
            raise ValueError('history_window')
        if r.get('state') not in ('watching','complete','data_gap','source_changed') or r.get('pnl') is not None:
            raise ValueError('history_state')
        seen=r.get('path_seen')
        if not isinstance(seen,dict) or len(seen)>15:raise ValueError('history_proofs')
        for k,h in seen.items():
            t=int(k)
            if not r['audit_start_ms']<=t<r['audit_end_ms'] or t%BAR or t+BAR>checked or not isinstance(h,str) or len(h)!=64:raise ValueError('history_proofs')
        if not isinstance(r.get('anchor_hash'),str) or len(r['anchor_hash'])!=64:raise ValueError('history_anchor')
        results=r.get('results')
        if not isinstance(results,dict) or set(results)!={'base','stress'}:raise ValueError('history_results')
        for item in results.values():
            if not isinstance(item,dict):raise ValueError('history_result')
            if item.get('level') not in ('target','stop','ambiguous','none') or type(item.get('observed_bars')) is not int or not 0<=item['observed_bars']<=15:raise ValueError('history_result')
            t=item.get('first_bar_ms')
            if (item['level']=='none')!=(t is None) or (t is not None and (type(t) is not int or not r['audit_start_ms']<=t<r['audit_end_ms'] or str(t) not in seen)):
                raise ValueError('history_result_time')
        if r['state']=='complete' and (checked<r['audit_end_ms'] or len(seen)!=15):raise ValueError('history_premature_completion')
    return value


def _rows(capture,now):
    result={};previous=None
    for raw in capture['candles']:
        if not isinstance(raw,(list,tuple)) or len(raw)!=6 or type(raw[0]) is not int or raw[0]%BAR or raw[0]+BAR>now or (previous is not None and raw[0]<=previous):
            raise ValueError('history_candle_time')
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in raw[1:]):raise ValueError('history_candle_value')
        o,h,l,c,v=map(float,raw[1:])
        if not 0<l<=min(o,c)<=max(o,c)<=h or v<0:raise ValueError('history_candle_value')
        result[raw[0]]=[raw[0],o,h,l,c,v];previous=raw[0]
    return result


def advance(previous,packet,capture,now_ms):
    """Use only currently closed bars; first 15m entry interval is excluded."""
    if type(now_ms) is not int or now_ms<=0:raise ValueError('history_clock')
    plan=validate_plan(packet['trade_scenarios'],packet)
    if not packet['as_of_ms']<=now_ms<packet['expires_ms']:raise ValueError('history_current_packet_stale')
    if (capture.get('symbol'),capture.get('venue'))!=(packet['symbol'],'binance-usdm'):raise ValueError('history_market')
    rows=_rows(capture,now_ms);anchor=rows.get(packet['signal_ms']-BAR)
    if anchor is None or anchor[4]!=float(packet['close']):raise ValueError('history_current_anchor')
    state=copy.deepcopy(validate(previous)) if previous is not None else {'version':VERSION,'started_ms':now_ms,'checked_ms':now_ms,'discarded':0,'orders_enabled':False,'fills_confirmed':False,'records':[]}
    if state['checked_ms']>now_ms:raise ValueError('history_reordered')
    if plan['status']=='conditional_only':
        core={k:copy.deepcopy(packet[k]) for k in ('feature_version','symbol','venue','signal_ms','as_of_ms','expires_ms','side','close','detected_patterns','limits','trade_scenarios')}
        core['features']={'atr_pct':packet['features']['atr_pct']}
        core['ml']={k:v for k,v in packet['ml'].items() if k in ('model_id','action','trade_allowed','p_win_estimate','expected_stress_net_return')}
        model=core['ml'].get('model_id')
        if not isinstance(model,str) or not 1<=len(model)<=100:raise ValueError('history_model_identity')
        identity=digest([packet['symbol'],packet['signal_ms'],model])
        found=next((r for r in state['records'] if r['id']==identity),None)
        if found:
            if found['packet']['trade_scenarios']!=plan or found['anchor_hash']!=digest(anchor):found['state']='source_changed'
        else:
            state['records'].append({'id':identity,'source_packet_id':packet.get('packet_id'),'recorded_ms':now_ms,'packet':core,'anchor_hash':digest(anchor),
                'audit_start_ms':packet['signal_ms']+BAR,'audit_end_ms':packet['signal_ms']+16*BAR,
                'state':'watching','path_seen':{},'results':{k:{'level':'none','first_bar_ms':None,'observed_bars':0} for k in ('base','stress')},'pnl':None})
    for record in state['records']:
        if record['packet']['symbol']!=packet['symbol'] or record['state']=='source_changed':continue
        # Old completed observations beyond this capture are preserved, not re-created.
        if rows and record['audit_end_ms']<=min(rows):continue
        anchor=rows.get(record['packet']['signal_ms']-BAR)
        if anchor is not None and digest(anchor)!=record['anchor_hash']:
            record['state']='source_changed';continue
        expected=list(range(record['audit_start_ms'],min(record['audit_end_ms'],now_ms//BAR*BAR),BAR))
        conflict=False
        for t in expected:
            if t not in rows:continue
            hashed=digest(rows[t]);old=record['path_seen'].get(str(t))
            if old is not None and old!=hashed:conflict=True;break
            record['path_seen'][str(t)]=hashed
        if conflict:record['state']='source_changed';continue
        if any(t not in rows for t in expected):record['state']='data_gap';continue
        record['state']='complete' if now_ms>=record['audit_end_ms'] else 'watching'
        direction=record['packet']['side']
        for scenario in record['packet']['trade_scenarios']['scenarios']:
            outcome={'level':'none','first_bar_ms':None,'observed_bars':len(expected)}
            stop=float(scenario['stop_reference']);target=float(scenario['target_reference'])
            for t in expected:
                _,o,h,l,c,v=rows[t]
                stop_seen=l<=stop if direction=='LONG' else h>=stop
                target_seen=h>=target if direction=='LONG' else l<=target
                if stop_seen or target_seen:
                    outcome.update(level='ambiguous' if stop_seen and target_seen else 'stop' if stop_seen else 'target',first_bar_ms=t);break
            record['results'][scenario['name']]=outcome
    if len(state['records'])>LIMIT:
        removed=len(state['records'])-LIMIT;state['discarded']+=removed;state['records']=state['records'][-LIMIT:]
    state['checked_ms']=now_ms
    return validate(state)


def update(path,packet,capture,now_ms):
    """Caller holds the existing technical-analysis writer lock. Atomic replace."""
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():raise ValueError('history_path')
    previous=None
    if path.exists():
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:raw=f.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('history_size')
        previous=json.loads(raw)
    value=advance(previous,packet,capture,now_ms)
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>MAX_BYTES:raise ValueError('history_size')
    fd,tmp=tempfile.mkstemp(prefix='.plan-history-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);folder=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(folder)
        finally:os.close(folder)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return value
