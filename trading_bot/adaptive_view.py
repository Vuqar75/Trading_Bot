"""Telegram presentation of the isolated, precomputed public study snapshot."""
import datetime,json,time
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .display_amount import display_amount

SNAPSHOT=Path('/var/lib/adaptive-study-telegram/snapshot.json')
def stamp(ms):
 return datetime.datetime.fromtimestamp(ms/1000,datetime.timezone(datetime.timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S Баку')
def amount(value,unit='USDT'):
 try:return display_amount(value,unit) if value is not None else 'неизвестно'
 except (ValueError,TypeError,InvalidOperation):return 'неизвестно'
def render(view='summary',path=SNAPSHOT,now=None):
 now=time.time() if now is None else now
 try:
  raw=Path(path).read_bytes()
  if len(raw)>100000:raise ValueError('too_large')
  d=json.loads(raw);generated=d['generated_ms'];checked=d['checked_ms'];start=d['start_ms'];experiments=d['experiments']
  if d['schema']!=1 or any(type(x) is not int for x in (generated,checked,start)) or not 0<start<=checked<=generated<=now*1000+5000 or set(experiments)!={'ai','baseline'}:raise ValueError('invalid_snapshot')
  policy=d.get('route_policy','isolated-router-policy-v1')
  if policy not in ('isolated-router-policy-v1','isolated-futures-two-sided-v2'):raise ValueError('invalid_route_policy')
  for name in ('ai','baseline'):
   v=experiments[name]
   if not isinstance(v.get('positions'),list) or len(v['positions'])>1 or not isinstance(v.get('route'),dict) or not isinstance(v.get('cycle'),dict):raise ValueError('invalid_account')
   if any(not isinstance(p,dict) for p in v['positions']):raise ValueError('invalid_position')
   if v.get('decision') is not None and not isinstance(v['decision'],dict):raise ValueError('invalid_decision')
  if not isinstance(d.get('data_errors',{}),dict) or not isinstance(d.get('watermarks',{}),dict):raise ValueError('invalid_health')
  for w in d.get('watermarks',{}).values():
   if not isinstance(w,dict):raise ValueError('invalid_watermark')
   x=w.get('last_closed_boundary_ms')
   if x is not None and (type(x) is not int or not 0<x<=generated):raise ValueError('invalid_candle_time')
  if now*1000-checked>180000 or now*1000-generated>180000:
   lines=['[Adaptive] Текущая оценка недоступна: снимок устарел.', 'Последняя оценка: '+stamp(checked)+'.']
   for name in ('ai','baseline'):
    tests=experiments[name].get('operator_tests') or []
    if not isinstance(tests,list) or len(tests)>8:continue
    for t in tests:
     if not isinstance(t,dict) or type(t.get('closed_ms')) is not int:continue
     lines.append(f"{name}: проверка {t.get('direction','неизвестно')}, завершена {stamp(t['closed_ms'])}; net {amount(t.get('net'))} USDT.")
   return '\n'.join(lines)
 except (OSError,ValueError,TypeError,KeyError,OverflowError,json.JSONDecodeError):
  return '[Adaptive] Оценка недоступна: снимок отсутствует или повреждён.'
 lines=['[Adaptive] Адаптивный стенд • исследование • виртуальные средства',f"Старт: {stamp(start)}; дней: {max(0,int((checked-start)//86400000))}.",f'Оценка: {stamp(checked)}.']
 labels={'market_history_conflict':'расхождение истории рынка','funding_coverage_missing':'funding не подтверждён','funding_conflict_requires_review':'противоречие funding','decision_unavailable':'решение недоступно или просрочено','no_position':'позиций нет','entry_blocked':'вход заблокирован','waiting_for_signal_window':'ожидание свечи','conflict':'расхождение данных','unavailable':'источник недоступен','invalid':'некорректные данные'}
 for name,label in (('ai','AI'),('baseline','Baseline')):
  v=experiments[name];route=v['route'];cycle=v['cycle'];decision=v.get('decision') or {};positions=v['positions'];mode=decision.get('mode') or 'ожидание решения'
  if decision.get('mode') and route.get('decision_fresh') is not True:mode+=' (срок истёк или не подтверждён)'
  pos=', '.join(str(p.get('direction'))+' '+str(p.get('market')) for p in positions) if positions else 'позиций нет'
  net=amount(v.get('net_pnl_usdt')) if v.get('ledger_balanced') is True else 'не подтверждён'
  proposed={'TREND_UP':'Spot • тренд LONG','TREND_DOWN':'isolated • тренд SHORT','RANGE':'Spot • диапазон','NO_TRADE':'нет входа'}.get(decision.get('mode'),'не выбран')
  if policy=='isolated-futures-two-sided-v2':
   proposed={'TREND_UP':'isolated • тренд LONG','TREND_DOWN':'isolated • тренд SHORT','RANGE':'вход запрещён: стратегия диапазона не допущена','NO_TRADE':'нет входа'}.get(decision.get('mode'),'не выбран')
  if route.get('decision_fresh') is not True:proposed='нет действующего решения'
  lines.append(f"{label}: режим {mode}; маршрут {route.get('strategy') or proposed}; {pos}; net {net} USDT; закрыто {v.get('closed_trades','неизвестно')}.")
  tests=v.get('operator_tests') or [];pending=v.get('operator_pending')
  if not isinstance(tests,list) or len(tests)>8:tests=[]
  if tests:lines.append(f"  Проверка исполнения: завершено {len(tests)}; результат не включён в статистику стратегии.")
  if isinstance(pending,dict):lines.append('  Проверочное намерение: '+('вход' if pending.get('phase')=='entry' else 'выход')+'; исполнение ещё не подтверждено.')
  if view=='positions':
   for p in positions:lines.append(f"  {p.get('strategy') or 'стратегия неизвестна'}: {amount(p.get('quantity'),'BTC')} BTC, вход {amount(p.get('entry'))} USDT; защитный уровень {amount(p.get('protective_level'))} USDT (модель, не биржевой стоп).")
   phase={'IDLE':'нет ожидающего исполнения','ENTRY_PENDING':'ожидается вход','EXIT_PENDING':'ожидается выход','OPEN':'позиция открыта','RECONCILE':'ожидается сверка'}.get(route.get('phase'),'неизвестно')
   lines.append(f"  Действие: {phase}; ожидающих заданий: {route.get('pending_jobs','неизвестно')}.")
  elif view=='results':
   lines.append(f"  Реализовано {amount(v.get('realised_pnl_usdt'))}; открытый PnL {amount(v.get('open_pnl_usdt'))}; комиссии {amount(v.get('fees_usdt'))}; funding {amount(v.get('funding_usdt'))} USDT.")
   if name=='ai':lines.append(f"  После учтённой стоимости API: {amount(v.get('net_after_known_api_usd'))} USDT; оценка стоимости API: {amount(v.get('known_estimated_api_usd'))} USDT.")
   for t in tests:
    if not isinstance(t,dict):continue
    lines.append(f"  Проверка {t.get('direction','неизвестно')}: {amount(t.get('quantity'),'BTC')} BTC; вход {amount(t.get('entry'))}, выход {amount(t.get('exit'))}; комиссии {amount(t.get('fees'))}, funding {amount(t.get('funding'))}, net {amount(t.get('net'))} USDT.")
    if type(t.get('opened_ms')) is int and type(t.get('closed_ms')) is int:
     lines.append(f"  Вход {stamp(t['opened_ms'])}, выход {stamp(t['closed_ms'])}; ID {str(t.get('entry_action','неизвестно'))[:100]} / {str(t.get('exit_action','неизвестно'))[:100]}.")
   lines.append('  Сверка: '+('сходится' if v.get('ledger_balanced') is True else 'не подтверждена')+'.')
  elif view=='why':
   reason=cycle.get('reason') or cycle.get('status') or route.get('entry_reason') or 'неизвестно';protection=cycle.get('protection') or {}
   lines.append('  Причина: '+labels.get(reason,str(reason)[:100])+'.')
   if protection.get('funding_wait'):lines.append('  Защитный сигнал сохранён; требуется сверка funding и актуальное условие выхода. Позиция не объявлена закрытой.')
  elif view=='health':
   protection=cycle.get('protection') or {};state=protection.get('state')
   label_state={'no_position':'позиции нет','checked':'проверено','partial':'проверено частично','unavailable':'недоступно'}.get(state,'неизвестно')
   lines.append('  Сопровождение: '+label_state+'; funding: '+('есть противоречие' if v.get('funding_conflict') else 'противоречие не отмечено')+'.')
 if view=='health':
  errors=d.get('data_errors') or {};lines.append('Данные: '+('ошибок не отмечено' if not errors else ', '.join(labels.get(str(x),str(x)[:80]) for x in errors.values()))+'.')
  for name in ('spot','futures'):
   w=(d.get('watermarks') or {}).get(name) or {};lines.append(f"{name}: пропущено окон {w.get('missed_windows',0)}; последняя свеча: {stamp(w['last_closed_boundary_ms']) if type(w.get('last_closed_boundary_ms')) is int else 'нет данных'}.")
  state=(d.get('backup') or {}).get('state');lines.append('Резервная копия: '+{'fresh':'актуальна','stale':'устарела','unavailable':'недоступна'}.get(state,'неизвестно')+'.')
 elif view=='summary':
  lines.append('Данные: '+('есть ошибки или блокировки' if d.get('data_errors') else 'снимок актуален')+'.')
  reasons=[v['cycle'].get('reason') for v in experiments.values() if v['cycle'].get('reason')]
  if reasons:lines.append('Блокер: '+labels.get(reasons[0],str(reasons[0])[:100])+'.')
 lines.append('Рабочие кошельки не используются; прибыльность не доказана.')
 return '\n'.join(lines)
