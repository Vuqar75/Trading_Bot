"""One purpose-bound model attempt in the EXISTING server budget only."""
from contextlib import closing
from decimal import Decimal, ROUND_CEILING
import json
import os
import re
from pathlib import Path
import sqlite3
import time

from . import adaptive_api_transport as api
from .adaptive_budget_resilience import verify,backup
from .adaptive_server_once import exclusive,owner,credential
from .ai_limit_plan import canonical,digest,request,validate,verify_context


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS bracket_api_requests(id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,request TEXT NOT NULL,raw_response TEXT,received_ms INTEGER,result TEXT)')
    wanted=['id','snapshot','request','raw_response','received_ms','result']
    if [r[1] for r in db.execute('PRAGMA table_info(bracket_api_requests)')]!=wanted:raise ValueError('bracket_budget_schema')
    db.execute('CREATE TABLE IF NOT EXISTS api_usage(request_id TEXT PRIMARY KEY,usage_json TEXT NOT NULL)')
    if [r[1] for r in db.execute('PRAGMA table_info(api_usage)')]!=['request_id','usage_json']:raise ValueError('usage_schema_incompatible')
    db.execute('INSERT INTO api_usage VALUES(?,?)',('__bracket_write_probe__','{}'))
    db.execute('DELETE FROM api_usage WHERE request_id=?',('__bracket_write_probe__',))


def safe_response(value,secret=None):
    encoded=canonical(value)
    if len(encoded.encode())>1000000:raise ValueError('response_too_large')
    secrets=[secret,os.environ.get('TRADING_BOT_AI'),os.environ.get('OPENAI_API_KEY')]
    if any(s and s in encoded for s in secrets):raise ValueError('response_unsafe')
    def unsafe(x):
        if isinstance(x,dict):return any(str(k).strip().lower() in ('authorization','api_key','access_token') or unsafe(v) for k,v in x.items())
        if isinstance(x,list):return any(unsafe(v) for v in x)
        return isinstance(x,str) and 'authorization:' in x.lower()
    if unsafe(value):raise ValueError('response_unsafe')
    return encoded


def finish(db,rid,*,fault=lambda stage:None):
    row=db.execute('SELECT snapshot,raw_response,received_ms,result FROM bracket_api_requests WHERE id=?',(rid,)).fetchone()
    if not row or row[1] is None:raise ValueError('saved_bracket_response_missing_no_retry')
    if row[3] is not None:return json.loads(row[3])
    snapshot=json.loads(row[0]);reply=json.loads(row[1]);received=row[2]
    if not isinstance(reply,dict) or reply.get('status')!='completed':raise ValueError('model_response_incomplete')
    texts=[c.get('text') for m in reply.get('output',[]) if isinstance(m,dict) and m.get('type')=='message' and m.get('role')=='assistant' and m.get('status')=='completed'
           for c in m.get('content',[]) if isinstance(c,dict) and c.get('type')=='output_text']
    if len(texts)!=1 or not isinstance(texts[0],str) or len(texts[0])>20000:raise ValueError('model_response_shape')
    def unique_pairs(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('duplicate_model_key')
            result[k]=v
        return result
    try:
        decision=json.loads(texts[0],object_pairs_hook=unique_pairs)
        plan=validate(snapshot,decision,received)
        admission={'allowed':plan['direction']!='WAIT','reason':'validated_paper_plan' if plan['direction']!='WAIT' else 'model_wait'}
        plan_id=plan['plan_id']
    except (ValueError,TypeError,KeyError) as error:
        reason=str(error)
        if not re.fullmatch('[a-z_]{1,90}',reason):reason='model_response_rejected'
        decision=None;plan_id=None;admission={'allowed':False,'reason':reason}
    usage=reply.get('usage')
    if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0 for k in ('input_tokens','output_tokens')):raise ValueError('usage_missing')
    usage={k:usage[k] for k in ('input_tokens','output_tokens','total_tokens') if type(usage.get(k)) is int and usage[k]>=0}
    reserved=db.execute('SELECT reserved_micro_usd FROM api_attempts WHERE request_id=?',(rid,)).fetchone()[0]
    total=db.execute('SELECT COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts').fetchone()[0]
    estimated=Decimal(usage['input_tokens'])*Decimal('.1')+Decimal(usage['output_tokens'])*Decimal('.5')
    if estimated>reserved:raise ValueError('reported_usage_exceeds_reservation')
    result={'reserved_micro_usd':reserved,'total_reserved_micro_usd':total,'remaining_micro_usd':250000-total,'estimated_cost_micro_usd':str(estimated),'version':snapshot['version'],'snapshot':snapshot,'decision':decision,'plan_id':plan_id,'admission':admission,
            'received_ms':received,'usage':usage,'provenance':{'kind':'fixture' if (Path(db.execute('PRAGMA database_list').fetchone()[2]).parent/'.TEST_ONLY_BUDGET').exists() else 'budget_response','response_id':rid,'model':api.MODEL}}
    fault('before_usage');db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('INSERT OR REPLACE INTO api_usage VALUES(?,?)',(rid,canonical(usage)))
        db.execute('UPDATE bracket_api_requests SET result=? WHERE id=?',(canonical(result),rid))
        db.execute('UPDATE api_attempts SET result_json=? WHERE request_id=?',(canonical(result),rid));db.commit()
    except BaseException:db.rollback();raise
    return result


def once(state,snapshot,*,authorized=False,transport=None,secret=credential,clock=lambda:int(time.time()*1000),fault=lambda stage:None):
    if authorized is not True:raise ValueError('explicit_paper_plan_attempt_required')
    state=Path(state)
    if (state/'.TEST_ONLY_BUDGET').exists() and transport is None:raise ValueError('fixture_budget_cannot_send_http')
    # Same mutex as existing regular executor; no second active budget/writer.
    with exclusive(state):
        key_value=None
        owner(state);now=clock();verify_context(snapshot,now)
        if now-snapshot['as_of_ms']>120000:raise ValueError('request_snapshot_too_old')
        req=request(snapshot);rid='bracket:'+snapshot['snapshot_id']
        reserve=int(((Decimal(len(canonical(req).encode()))+4096)*Decimal('.1')+Decimal(2000)*Decimal('.5')).to_integral_value(rounding=ROUND_CEILING))
        if reserve>3640:raise ValueError('per_attempt_reserve_exceeded')
        with closing(sqlite3.connect(state/'experiment-budget.db',timeout=15,isolation_level=None)) as db:
            verify(db);db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
            try:
                schema(db)
                previous=db.execute('SELECT raw_response,result FROM bracket_api_requests WHERE id=?',(rid,)).fetchone()
                if previous:
                    db.rollback()
                    if previous[1] is not None:return json.loads(previous[1])
                    if previous[0] is not None:return finish(db,rid,fault=fault)
                    raise ValueError('bracket_attempt_uncertain_no_retry')
                if db.execute("SELECT 1 FROM bracket_api_requests WHERE result IS NULL").fetchone():raise ValueError('prior_bracket_attempt_unresolved_no_retry')
                if db.execute('SELECT COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts').fetchone()[0]+reserve>250000:raise ValueError('global_budget_exhausted')
                today=now//86400000*86400
                if db.execute('SELECT count(*) FROM api_attempts WHERE created_at>=?',(today,)).fetchone()[0]>=6:raise ValueError('daily_attempt_cap')
                prior=db.execute('SELECT max(created_at) FROM api_attempts WHERE created_at>?',(now/1000-3600,)).fetchone()[0]
                if prior is not None:raise ValueError('paid_attempt_cooldown')
                first=db.execute('SELECT reserved_micro_usd,result_json FROM api_attempts ORDER BY created_at LIMIT 1').fetchone()
                if first!=(3630,None):raise ValueError('original_reservation_changed')
                if transport is None:key_value=secret()
                db.execute('INSERT INTO api_attempts VALUES(?,?,?,NULL)',(rid,reserve,now/1000))
                db.execute('INSERT INTO bracket_api_requests VALUES(?,?,?,NULL,NULL,NULL)',(rid,canonical(snapshot),canonical(req)))
                db.commit()
            except BaseException:
                if db.in_transaction:db.rollback()
                raise
            # A durable reservation is also the single send claim. ANY crash
            # from here without a saved response permanently forbids retry.
            backup(state);fault('after_send_claim')
            value=key_value;key_value=None;previous_env=None
            try:
                if transport is None:
                    previous_env={k:os.environ.get(k) for k in ('OPENAI_API_KEY','TRADING_BOT_AI')}
                    os.environ.pop('OPENAI_API_KEY',None);os.environ['TRADING_BOT_AI']=value
                reply=(transport or api._send)(req);received=clock();fault('after_response')
                raw=safe_response(reply,value)
                db.execute('BEGIN IMMEDIATE');db.execute('UPDATE bracket_api_requests SET raw_response=?,received_ms=? WHERE id=?',(raw,received,rid));db.commit()
                backup(state);fault('after_response_saved')
                result=finish(db,rid,fault=fault);backup(state);return result
            finally:
                if previous_env is not None:
                    for k,v in previous_env.items():
                        if v is None:os.environ.pop(k,None)
                        else:os.environ[k]=v
                value=None


def recover(state,rid):
    with exclusive(state):
        owner(state)
        with closing(sqlite3.connect(Path(state)/'experiment-budget.db',isolation_level=None)) as db:
            verify(db);result=finish(db,rid)
        backup(state);return result
