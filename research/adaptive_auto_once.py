"""One scheduled, user-authorized AI attempt; never schedules another call."""
import base64,datetime as dt,hashlib,json,os,shutil,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from research.adaptive_budget_resilience import migrate
from research.adaptive_ai_executor import run,recover
from research.adaptive_ai_export import export
from research.adaptive_shadow import collect,PERIOD
ROOT=Path(__file__).resolve().parents[1];WORK=ROOT.parent
STATE=Path.home()/'.local/state/tradingbot-ai'
def code_hash():
 paths=[ROOT/'research'/x for x in ('adaptive_auto_once.py','adaptive_shadow.py','adaptive_ai_executor.py','adaptive_budget_resilience.py','adaptive_ai_export.py')]
 paths += [ROOT/'tools/read_adaptive_windows_credential.py',WORK/'vm_run_pinned.ps1']
 paths += [WORK/'ai-market-observer'/'trading_bot'/x for x in ('__init__.py','research_api_budget.py','market_research.py','openai_research.py')]
 h=hashlib.sha256()
 for p in paths:h.update(p.name.encode()+b'\0'+hashlib.sha256(p.read_bytes()).digest())
 return h.hexdigest()
def secret_from_credential_manager():
 py=shutil.which('python.exe');wslpath=shutil.which('wslpath')
 if not py or not wslpath:raise ValueError('windows_credential_helper_unavailable')
 helper=subprocess.run([wslpath,'-w',str(ROOT/'tools/read_adaptive_windows_credential.py')],capture_output=True,text=True,timeout=10,check=True).stdout.strip()
 result=subprocess.run([py,'-B',helper],capture_output=True,timeout=30)
 if result.returncode or not result.stdout or len(result.stdout)>8192:raise ValueError('windows_credential_unavailable')
 try:value=result.stdout.decode('utf-8')
 except UnicodeError:raise ValueError('windows_credential_invalid') from None
 if not value.strip() or '\x00' in value:raise ValueError('windows_credential_invalid')
 return value
def check_budget(state=STATE):
 p=Path(state)
 if not (p/'interactive-once.lock').is_file() or not (p/'approved-followup-once.lock').is_file() or (p/'adaptive-next-once.lock').exists():raise ValueError('budget_locks_not_ready')
 db=sqlite3.connect((p/'experiment-budget.db').resolve().as_uri()+'?mode=ro',uri=True)
 try:
  if db.execute('select count(*),sum(reserved_micro_usd),sum(result_json is null) from api_attempts').fetchone()!=(2,5718,2):raise ValueError('budget_state_changed')
 finally:db.close()
def check_window(boundary,now):
 if type(boundary) is not int or boundary%PERIOD or not 120000<=now-boundary<=300000:raise ValueError('decision_window_missed')
def publish_report(report_path,*,runner=subprocess.run):
 raw=Path(report_path).read_bytes();d=json.loads(raw)
 if d.get('complete') is not True or d.get('diagnostic_only') is True or 'baseline того же среза:' not in d.get('summary',''):raise ValueError('confirmed_comparison_missing')
 data=base64.b64encode(raw).decode('ascii')
 script="""import base64,datetime as dt,grp,json,os,shutil,subprocess,time\nfrom pathlib import Path\np=Path('/var/lib/tradingbot-ai-public');q=p/'report.json';root='/opt/trading-bot-adaptivediag-b4f7728'\nif not p.is_dir() or p.is_symlink() or not q.is_file() or q.is_symlink():raise ValueError('existing report unavailable')\nif subprocess.check_output(['git','-C',root,'rev-parse','HEAD'],text=True).strip()!='b4f772895c975e9eae8a53a57606380453a1fff4':raise ValueError('installed version changed')\nraw=base64.b64decode(DATA,validate=True);d=json.loads(raw);now=dt.datetime.now(dt.timezone.utc).timestamp()\nif d.get('complete') is not True or d.get('diagnostic_only') is True or dt.datetime.fromisoformat(d['expires_utc']).timestamp()<=now or 'baseline того же среза:' not in d.get('summary',''):raise ValueError('report unsafe or stale')\nold=json.loads(q.read_text());newtime=dt.datetime.fromisoformat(d['data_utc']);oldtime=dt.datetime.fromisoformat(old['data_utc'])\nif newtime<=oldtime:raise ValueError('report is not newer')\narchive=p/('report-before-'+str(int(time.time()*1000))+'.json');shutil.copyfile(q,archive);gid=grp.getgrnam('tradingbot').gr_gid;os.chown(archive,0,gid);os.chmod(archive,0o640)\nt=p/'report.next';t.write_bytes(raw);os.chown(t,0,gid);os.chmod(t,0o640);os.replace(t,q)\nok=False\ntry:\n r=subprocess.run(['runuser','-u','tradingbot','--','env','PYTHONPATH='+root,'python3','-B','-c','from trading_bot.ai_view import render; print(render())'],capture_output=True,text=True,timeout=20,check=True)\n ok='Вывод модели:' in r.stdout and 'baseline того же среза:' in r.stdout and 'УСТАРЕЛО' not in r.stdout\nfinally:\n if not ok:shutil.copyfile(archive,t);os.chown(t,0,gid);os.chmod(t,0o640);os.replace(t,q)\nif not ok:raise ValueError('display verification failed')\nprint('AI_COMPARISON_DISPLAY_VERIFIED')\n"""
 script='DATA='+repr(data)+'\n'+script
 pwsh=shutil.which('pwsh.exe');wslpath=shutil.which('wslpath')
 if not pwsh or not wslpath:raise ValueError('vm_transport_unavailable')
 path=None
 try:
  with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',suffix='.py',prefix='adaptive-publish-',dir=WORK,delete=False) as f:path=Path(f.name);f.write(script)
  win=lambda p:subprocess.run([wslpath,'-w',str(p)],capture_output=True,text=True,timeout=10,check=True).stdout.strip()
  result=runner([pwsh,'-NoProfile','-NonInteractive','-File',win(WORK/'vm_run_pinned.ps1'),'-ScriptPath',win(path)],capture_output=True,text=True,timeout=120)
  if result.returncode or 'AI_COMPARISON_DISPLAY_VERIFIED' not in result.stdout:raise ValueError('vm_publish_not_verified')
 finally:
  if path is not None:path.unlink(missing_ok=True)
def one_shot(boundary,*,clock=lambda:int(time.time()*1000),fetch=collect,credential=secret_from_credential_manager,publish=publish_report):
 check_window(boundary,clock());check_budget();migrate(STATE)
 key=credential();snapshot,baseline=fetch();check_window(boundary,clock())
 if snapshot.get('latest_4h_ms')!=boundary-PERIOD:raise ValueError('wrong_4h_candle')
 old=os.environ.pop('OPENAI_API_KEY',None);old_binding=os.environ.get('TRADING_BOT_AI');os.environ['TRADING_BOT_AI']=key;del key
 try:
  try:result=run(snapshot,baseline,STATE,next_authorized=True)
  except Exception:
   rid='adaptive:'+str(boundary);db=sqlite3.connect((STATE/'experiment-budget.db').resolve().as_uri()+'?mode=ro',uri=True)
   try:saved=db.execute('select 1 from adaptive_responses where request_id=?',(rid,)).fetchone()
   finally:db.close()
   if not saved:raise
   result=recover(STATE,rid)
 finally:
  os.environ.pop('TRADING_BOT_AI',None)
  if old_binding is not None:os.environ['TRADING_BOT_AI']=old_binding
  if old is not None:os.environ['OPENAI_API_KEY']=old
 report=STATE/'adaptive-telegram-report.json';export(STATE/'experiment-budget.db',report);publish(report)
 return {'status':'completed','mode':result['response']['mode'],'baseline':baseline,'reserved_micro_usd':result['reserved_micro_usd'],'usage':result.get('usage')}
def main():
 if sys.argv[1:]==['--publish-only']:
  report=STATE/'adaptive-telegram-report.json';export(STATE/'experiment-budget.db',report);publish_report(report);print('AI_COMPARISON_DISPLAY_VERIFIED');return
 if len(sys.argv)==3 and sys.argv[1]=='--recover-saved' and sys.argv[2].isdigit():
  recover(STATE,'adaptive:'+sys.argv[2]);report=STATE/'adaptive-telegram-report.json';export(STATE/'experiment-budget.db',report);publish_report(report);print('AI_LOCAL_RECOVERY_AND_DISPLAY_VERIFIED');return
 if len(sys.argv)!=3 or not sys.argv[1].isdigit() or len(sys.argv[2])!=64:raise SystemExit('boundary_and_code_hash_required')
 status=STATE/'adaptive-one-shot-status.json'
 try:
  if code_hash()!=sys.argv[2]:raise ValueError('scheduled_code_changed')
  result=one_shot(int(sys.argv[1]))
 except Exception as e:
  safe=str(e) if isinstance(e,ValueError) and str(e).replace('_','').isalnum() else type(e).__name__
  result={'status':'not_completed','reason':safe,'automatic_retry':False}
 STATE.mkdir(mode=0o700,parents=True,exist_ok=True);tmp=status.with_suffix('.tmp');tmp.write_text(json.dumps(result,sort_keys=True)+'\n');os.chmod(tmp,0o600);os.replace(tmp,status)
if __name__=='__main__':main()
