"""Build a read-only Telegram artifact from an already completed model result."""
import datetime as dt,json,os,tempfile
from pathlib import Path
from trading_bot.market_research import ResearchError
from trading_bot.openai_research import validate_analysis

def export(snapshot,result,path):
 if result.get('mode')!='model_observation' or result.get('snapshot_id')!=snapshot.get('snapshot_id') or type(result.get('response_received_ms')) is not int:raise ResearchError('result_provenance_unconfirmed')
 analysis=validate_analysis(result['analysis'],snapshot)
 def stamp(x):return dt.datetime.fromtimestamp(x/1000,dt.timezone.utc).isoformat()
 metrics='; '.join(s+': close '+str(m['technical']['last_close'])+', RSI14 '+str(m['technical']['rsi14_simple']) for s,m in snapshot['markets'].items())
 source=[]
 for x in (snapshot['news']+snapshot['fundamentals'])[:3]:
  source.append({'title':x['title'][:250],'url':x['url'],'published_utc':stamp(x['published_at_ms']),'observed_utc':stamp(x['first_seen_at_ms'])})
 summary='Режим: '+analysis['market_regime']+'. '+analysis['summary']+' Технический комментарий: '+analysis['technical_commentary']+' Исследовательские гипотезы: '+(', '.join(analysis['strategy_candidates']) or 'не предложены')+'.'
 doc={'schema':1,'data_utc':stamp(snapshot['as_of_ms']),'answer_utc':stamp(result['response_received_ms']),'metrics':metrics,'coverage':f"Заголовков: {len(snapshot['news'])}; макроисточников: {len(snapshot['fundamentals'])}; полнота не подтверждена.",'summary':summary,'risks':'; '.join(analysis['risk_flags']+analysis['limitations']) or 'Модель не указала риски; полнота анализа не подтверждена.','sources':source,'complete':True}
 if any(not isinstance(doc[k],str) or not 0<len(doc[k])<=800 for k in ('metrics','coverage','summary','risks')):raise ResearchError('view_length_not_supported')
 path=Path(path);fd,name=tempfile.mkstemp(prefix='.ai-report.',dir=path.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(doc,f,ensure_ascii=False,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
  os.chmod(name,0o600);os.replace(name,path)
 finally:
  if os.path.exists(name):os.unlink(name)
