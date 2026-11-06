"""Read the sole budget journal and prepare a non-secret /ai report."""
import datetime as dt,json,os,sqlite3,time
from pathlib import Path
def stamp(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()
def export(budget,target,now_ms=None):
 now_ms=int(time.time()*1000) if now_ms is None else now_ms
 db=sqlite3.connect(Path(budget).resolve().as_uri()+'?mode=ro',uri=True)
 try:row=db.execute('select a.snapshot_json,a.decision_json,a.baseline,d.details_json from adaptive_decisions a left join api_diagnostics d on d.request_id=a.request_id order by a.rowid desc limit 1').fetchone()
 finally:db.close()
 if not row:raise ValueError('no_model_attempt')
 snap=json.loads(row[0]);record=json.loads(row[1]) if row[1] else None
 if record is None:
  diagnostic=json.loads(row[3]) if row[3] else {};observed=diagnostic.get('observed_ms')
  if type(observed) is not int or not snap['as_of_ms']<=observed<=now_ms:raise ValueError('no_confirmed_diagnostic')
  category=diagnostic.get('category');category=category if category in ('http_error','timeout','tls_error','network_error','transport_error','response_too_large','invalid_response_json','incomplete_or_invalid_response') else 'unknown'
  status=diagnostic.get('http_status');detail=category+(' HTTP '+str(status) if type(status) is int and 100<=status<=599 else '')
  expiry=snap['as_of_ms']+14400000
  if now_ms>=expiry:raise ValueError('model_diagnostic_expired')
  local=' В исходном журнале отсутствовала таблица usage; статус ответа провайдера неизвестен.' if diagnostic.get('local_reason')=='legacy_budget_missing_usage_table_response_status_unknown' else ''
  report={'schema':1,'data_utc':stamp(snap['as_of_ms']),'answer_utc':stamp(observed),'expires_utc':stamp(expiry),'diagnostic_only':True,'metrics':'Данные исходного среза сохранены в журнале.','coverage':str(snap.get('coverage','неизвестно'))[:800],'summary':'ИИ: ответа нет; ошибка '+detail+'; baseline того же среза: '+row[2]+'.','risks':'Резерв сохранён; автоматического повтора нет. Адаптивные входы запрещены.'+local,'sources':[],'complete':False}
  return write(target,report)
 decision=record['response'];expiry=decision['expires_ms']
 if not snap['as_of_ms']<=record['received_ms']<expiry<=snap['as_of_ms']+14400000 or now_ms>=expiry:raise ValueError('model_decision_expired')
 if decision['snapshot_id']!=snap['snapshot_id'] or decision['data_ms']!=snap['as_of_ms']:raise ValueError('model_snapshot_mismatch')
 features=snap['features'];metrics=', '.join(k+'='+str(features[k]) for k in ('close_4h','sma20_4h','volatility_20h_pct','spread_bps') if k in features)
 sources=[]
 for news in snap.get('news',[]):
  if len(sources)>=3:break
  if not isinstance(news,dict) or not str(news.get('url','')).startswith('https://') or type(news.get('published_ms')) is not int or type(news.get('observed_ms')) is not int:continue
  sources.append({'source':str(news.get('source','Источник не указан'))[:100],'title':str(news.get('title',''))[:250],'url':news['url'][:250],'published_utc':stamp(news['published_ms']),'observed_utc':stamp(news['observed_ms'])})
 reason='; '.join(decision['reasons']);uncertainty='; '.join(decision['uncertainties']);usage=record.get('usage') or {}
 report={'schema':1,'data_utc':stamp(snap['as_of_ms']),'answer_utc':stamp(record['received_ms']),'expires_utc':stamp(expiry),'metrics':metrics[:800] or 'нет метрик',
  'coverage':str(snap.get('coverage','неизвестно'))[:800], 'summary':('ИИ: '+decision['mode']+'; baseline того же среза: '+record['baseline']+'; '+reason)[:800],
  'risks':('Адаптивные входы запрещены; время события стакана не подтверждено. '+uncertainty+'; usage: '+json.dumps(usage,sort_keys=True)+'; резерв micro-USD: '+str(record['reserved_micro_usd'])+'; фактический счёт провайдера не подтверждён.')[:800],
  'sources':sources,'complete':True}
 report['shadow_details']={'model':decision['mode'],'baseline':record['baseline'],
  'reasons':decision['reasons'],'uncertainties':decision['uncertainties'],
  'features':{k:features[k] for k in ('close_4h','sma20_4h','volatility_20h_pct','spread_bps') if k in features},
  'sources_used_by_model':'не зафиксированы'}
 return write(target,report)
def write(target,report):
 target=Path(target);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700);tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,sort_keys=True)+'\n',encoding='utf-8');os.chmod(tmp,0o600);os.replace(tmp,target);return report
