"""Explicitly authorized one-shot follow-up; never run without owner approval."""
import argparse,hashlib,json,os,sqlite3,time,re
from decimal import Decimal,ROUND_CEILING
from pathlib import Path
from trading_bot.market_research import ResearchError,canonical
from trading_bot.openai_research import prepare_request
from trading_bot.research_api_budget import APP_ID,MODEL,paid_analysis

MAX_RESERVE=3640
def run(snapshot_path,state,*,approved=False,expected_sha256=None,transport=None):
    if approved is not True:raise ResearchError('explicit_approval_required')
    if transport is None and not os.environ.get('TRADING_BOT_AI'):raise ResearchError('trading_bot_ai_unavailable')
    state=Path(state);budget=state/'experiment-budget.db';old_lock=state/'interactive-once.lock';new_lock=state/'approved-followup-once.lock'
    if not budget.is_file() or not old_lock.is_file():raise ResearchError('original_budget_or_lock_missing')
    db=sqlite3.connect(budget.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if db.execute('pragma application_id').fetchone()[0]!=APP_ID:raise ResearchError('wrong_budget')
        count,reserved,unknown=db.execute('select count(*),coalesce(sum(reserved_micro_usd),0),sum(result_json is null) from api_attempts').fetchone()
        if (count,reserved,unknown)!=(1,3630,1):raise ResearchError('original_budget_changed_requires_review')
    finally:db.close()
    raw=Path(snapshot_path).read_bytes()
    if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256) or hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ResearchError('snapshot_hash_mismatch')
    snapshot=json.loads(raw)
    ident=snapshot.get('snapshot_id');body={k:v for k,v in snapshot.items() if k!='snapshot_id'}
    if not isinstance(ident,str) or hashlib.sha256(canonical(body).encode()).hexdigest()!=ident:raise ResearchError('snapshot_id_mismatch')
    captured=snapshot.get('as_of_ms');now=int(time.time()*1000)
    if type(captured) is not int or not 0<=now-captured<=900000:raise ResearchError('snapshot_stale_or_future')
    req=prepare_request(snapshot,model=MODEL);req['service_tier']='default'
    bound=len(canonical(req).encode())+4096;reserve=int((Decimal(bound)*Decimal('.1')+Decimal(req['max_output_tokens'])*Decimal('.5')).to_integral_value(rounding=ROUND_CEILING))
    if reserve>MAX_RESERVE:raise ResearchError('approved_cost_exceeded')
    fd=os.open(new_lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:f.write(hashlib.sha256(canonical(req).encode()).hexdigest()+'\n');f.flush();os.fsync(f.fileno())
    d=os.open(state,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(d)
    finally:os.close(d)
    old=os.environ.pop('OPENAI_API_KEY',None)
    try:return paid_analysis(snapshot,budget,enabled=True,transport=transport)
    finally:
        if old is not None:os.environ['OPENAI_API_KEY']=old

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',required=True);p.add_argument('--snapshot-sha256',required=True);p.add_argument('--approved-by-owner',action='store_true');a=p.parse_args()
    try:
        result=run(a.snapshot,Path.home()/'.local/state/tradingbot-ai',approved=a.approved_by_owner,expected_sha256=a.snapshot_sha256)
        from tools.export_ai_telegram import export
        try:export(json.loads(Path(a.snapshot).read_text(encoding='utf-8')),result,Path.home()/'.local/state/tradingbot-ai/telegram-report.json')
        except (ResearchError,ValueError,OSError,KeyError,TypeError):print('model_result_saved_but_view_not_exported; do not retry API')
        print(canonical(result))
    except Exception as exc:
        print('AI follow-up failed:',str(exc) if isinstance(exc,ResearchError) else type(exc).__name__)
        raise SystemExit(1)
if __name__=='__main__':main()
