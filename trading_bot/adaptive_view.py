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
   return '[Adaptive] Оценка недоступна: снимок устарел. Последняя оценка: '+stamp(checked)+'.'
 except (OSError,ValueError,TypeError,KeyError,OverflowError,json.JSONDecodeError):
  return '[Adaptive] Оценка недоступна: снимок отсутствует или повреждён.'
 lines=['[Adaptive] Адаптивный стенд • исследование • виртуальные средства',f"Старт: {stamp(start)}; дней: {max(0,int((checked-start)//86400000))}.",f'Оценка: {stamp(checked)}.']
 labels={'market_history_conflict':'расхождение истории рынка','funding_coverage_missing':'funding не подтверждён','funding_conflict_requires_review':'противоречие funding','decision_unavailable':'решение недоступно или просрочено','no_position':'позиций нет','entry_blocked':'вход заблокирован','waiting_for_signal_window':'ожидание свечи','conflict':'расхождение данных','unavailable':'источник недоступен','invalid':'некорректные данные'}
 for name,label in (('ai','AI'),('baseline','Baseline')):
  v=experiments[name];route=v['route'];cycle=v['cycle'];decision=v.get('decision') or {};positions=v['positions'];mode=decision.get('mode') or 'ожидание решения'
  if decision.get('mode') and route.get('decision_fresh') is not True:mode+=' (срок истёк или не подтверждён)'
  pos=', '.join(str(p.get('direction'))+' '+str(p.get('market')) for p in positions) if positions else 'позиций нет'
  net=amount(v.get('net_pnl_usdt')) if v.get('ledger_balanced') is True else 'не подтверждён'
  lines.append(f"{label}: режим {mode}; маршрут {route.get('strategy') or 'не выбран'}; {pos}; net {net} USDT; закрыто {v.get('closed_trades','неизвестно')}.")
  if view=='positions':
   for p in positions:lines.append(f"  {p.get('strategy') or 'стратегия неизвестна'}: {amount(p.get('quantity'),'BTC')} BTC, вход {amount(p.get('entry'))} USDT; защитный уровень {amount(p.get('protective_level'))} USDT (модель, не биржевой стоп).")
   phase={'IDLE':'нет ожидающего исполнения','ENTRY_PENDING':'ожидается вход','EXIT_PENDING':'ожидается выход','OPEN':'позиция открыта','RECONCILE':'ожидается сверка'}.get(route.get('phase'),'неизвестно')
   lines.append(f"  Действие: {phase}; ожидающих заданий: {route.get('pending_jobs','неизвестно')}.")
  elif view=='results':
   lines.append(f"  Реализовано {amount(v.get('realised_pnl_usdt'))}; открытый PnL {amount(v.get('open_pnl_usdt'))}; комиссии {amount(v.get('fees_usdt'))}; funding {amount(v.get('funding_usdt'))} USDT.")
   if name=='ai':lines.append(f"  После учтённой стоимости API: {amount(v.get('net_after_known_api_usd'))} USDT; оценка стоимости API: {amount(v.get('known_estimated_api_usd'))} USDT.")
   lines.append('  Сверка: '+('сходится' if v.get('ledger_balanced') is True else 'не подтверждена')+'.')
  elif view=='why':
   reason=cycle.get('reason') or cycle.get('status') or route.get('entry_reason') or 'неизвестно';protection=cycle.get('protection') or {}
   lines.append('  Причина: '+labels.get(reason,str(reason)[:100])+'.')
   if protection.get('funding_wait'):lines.append('  Защитный сигнал сохранён; требуется сверка funding и актуальное условие выхода. Позиция не объявлена закрытой.')
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
