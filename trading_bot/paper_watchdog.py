"""Offline health assessment and transition suppression; no Telegram polling."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

MARKETS=('spot','futures','cross','margin')
SERVICES=('spot','futures','cross','margin','monitor','backup_timer')


def assess(snapshot,now):
    if not isinstance(now,(int,float)) or not math.isfinite(now):raise ValueError('Invalid observation time')
    problems={}
    def put(key,message,last=None):problems[key]={'message':message,'last_success':last}
    def fresh(value,limit):return isinstance(value,(int,float)) and math.isfinite(value) and 0<=now-value<=limit
    services=snapshot.get('services',{})
    for service in SERVICES:
        if services.get(service)!='active':put('service:'+service,service+': служба недоступна')
    health=snapshot.get('health',{})
    for market in MARKETS:
        h=health.get(market)
        if not isinstance(h,dict):put('health:'+market,market+': нет health');continue
        stamp=h.get('time')
        if not fresh(stamp,120):put('health:'+market,market+': устарел health')
        if h.get('error') or h.get('blocked'):put('error:'+market,market+': ошибка или финансовая блокировка')
        if type(h.get('paused')) is not bool:put('pause:'+market,market+': состояние паузы неизвестно')
        if h.get('paused') is True and h.get('pause_source') not in ('manual','deployment','agreed'):
            put('pause:'+market,market+(': защитная пауза' if h.get('pause_source')=='protective' else ': причина паузы не подтверждена'))
        if market=='spot' and h.get('telegram_error'):put('telegram','Spot: ошибка Telegram-поллера')
        if market in ('cross','margin'):
            if h.get('quotes_fresh') is not True or not fresh(h.get('data_at'),120):put('quotes:'+market,market+': котировки не подтверждены',h.get('data_at'))
            protection=h.get('protection_checked_at')
            protection=(protection/1000 if market=='margin' else protection) if isinstance(protection,(int,float)) else None
            if h.get('protection_fresh') is not True or not fresh(protection,120):
                put('protection:'+market,market+': защита не подтверждена',protection)
        else:
            quote=h.get('quote') or {}
            if not fresh(quote.get('time'),120):put('quotes:'+market,market+': устарела котировка',quote.get('time'))
            opened=h.get('position_open')
            if type(opened) is not bool:
                put('protection:'+market,market+': состояние позиции или защита не подтверждены')
            elif opened and not fresh(h.get('protection_checked_at'),120):
                put('protection:'+market,market+': защитная проверка позиции не подтверждена',h.get('protection_checked_at'))
    q=snapshot.get('queue',{})
    if not isinstance(q.get('pending'),int) or q['pending']<0:put('queue:unknown','Очередь: состояние не подтверждено')
    elif q['pending'] and (not fresh(q.get('oldest_received'),300) or q['pending']>50):put('queue:stuck','Очередь: команды ожидают слишком долго или очередь растёт',q.get('last_completed'))
    for kind,limit in (('local',7200),('external',7200)):
        last=snapshot.get('backups',{}).get(kind)
        if kind=='external' and snapshot.get('backups',{}).get('external_configured') is False:
            put('setup:external','Внешний backup ещё не настроен')
        elif not fresh(last,limit):put('backup:'+kind,kind+': успешная копия отсутствует или устарела',last)
    resources=snapshot.get('resources',{})
    if snapshot.get('financial_available') is False:put('ledger:unavailable','Финансовый журнал: проверки блокировок недоступны')
    if not isinstance(resources.get('memory_available_bytes'),int) or resources['memory_available_bytes']<100*1024*1024:put('memory','Мало свободной памяти или она неизвестна')
    if not isinstance(resources.get('disk_free_bytes'),int) or resources['disk_free_bytes']<500*1024*1024:put('disk','Мало свободного места или оно неизвестно')
    return problems


def recovery_proofs(snapshot,now):
    """Only explicit fresh observations can prove a prior issue resolved."""
    def fresh(value,limit):return isinstance(value,(int,float)) and math.isfinite(value) and 0<=now-value<=limit
    result=set();services=snapshot.get('services',{});health=snapshot.get('health',{})
    for service in SERVICES:
        if services.get(service)=='active':result.add('service:'+service)
    for market in MARKETS:
        h=health.get(market)
        if services.get(market)!='active' or not isinstance(h,dict) or not fresh(h.get('time'),120):continue
        result.add('health:'+market)
        if 'error' in h and h.get('error') is None and not h.get('blocked'):result.add('error:'+market)
        if type(h.get('paused')) is bool and (not h['paused'] or h.get('pause_source') in ('manual','deployment','agreed')):result.add('pause:'+market)
        if market=='spot' and 'telegram_error' in h and h['telegram_error'] is None:result.add('telegram')
        if market in ('cross','margin'):
            if h.get('quotes_fresh') is True and fresh(h.get('data_at'),120):result.add('quotes:'+market)
            value=h.get('protection_checked_at');value=value/1000 if market=='margin' and isinstance(value,(int,float)) else value
            if h.get('protection_fresh') is True and fresh(value,120) and not h.get('error') and not h.get('blocked'):result.add('protection:'+market)
        else:
            if fresh((h.get('quote') or {}).get('time'),120):result.add('quotes:'+market)
            if not h.get('error') and not h.get('blocked') and (h.get('position_open') is False or (h.get('position_open') is True and fresh(h.get('protection_checked_at'),120))):
                result.add('protection:'+market)
    q=snapshot.get('queue',{});pending=q.get('pending')
    if type(pending) is int and pending>=0:
        result.add('queue:unknown')
        if pending==0 or (pending<=50 and fresh(q.get('oldest_received'),300)):result.add('queue:stuck')
        result.add('queue:growing')
    for kind in ('local','external'):
        if fresh(snapshot.get('backups',{}).get(kind),7200):result.add('backup:'+kind)
    if snapshot.get('backups',{}).get('external_configured') is True and fresh(snapshot.get('backups',{}).get('external'),7200):result.add('setup:external')
    resources=snapshot.get('resources',{})
    if snapshot.get('financial_available') is True:result.add('ledger:unavailable')
    if type(resources.get('memory_available_bytes')) is int and resources['memory_available_bytes']>=100*1024*1024:result.add('memory')
    if type(resources.get('disk_free_bytes')) is int and resources['disk_free_bytes']>=500*1024*1024:result.add('disk')
    return result


def transitions(current,previous,now,*,reminder_seconds=3600,healthy_keys=None):
    """Keep identity durable; report only new incidents and proven recovery."""
    if reminder_seconds<60:raise ValueError('Reminder interval too short')
    if healthy_keys is None:healthy_keys=set(previous)-set(current)
    state={};events=[]
    for key,item in current.items():
        old=previous.get(key)
        first=old.get('first_seen',old.get('last_notice',now)) if old else now
        if old is None:events.append({'key':key,'kind':'failure','at':now,'first_seen':first,**item})
        state[key]={'last_notice':old.get('last_notice',now) if old else now,'first_seen':first,
                    'message':item['message'],'last_success':item.get('last_success'),'healthy_streak':0}
    for key,old in previous.items():
        if key in current:continue
        streak=old.get('healthy_streak',0)+1 if key in healthy_keys else 0
        if streak>=2:
            events.append({'key':key,'kind':'recovered','at':now,'first_seen':old.get('first_seen',old.get('last_notice',now)),
                           'message':old['message'],'last_success':now})
        else:state[key]=dict(old,healthy_streak=streak)
    return state,events


def format_event(event):
    def when(value):
        if not isinstance(value,(int,float)) or not math.isfinite(value):return None
        try:return datetime.fromtimestamp(value,ZoneInfo('Asia/Baku')).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError,OverflowError,OSError):return None
    first=when(event.get('first_seen'));last=when(event.get('last_success'))
    title='ВОССТАНОВЛЕНО' if event['kind']=='recovered' else ('НАСТРОЙКА' if event['key']=='setup:external' else 'ВНИМАНИЕ')
    key=event['key'];missing=('время последней защитной проверки не подтверждено' if key.startswith('protection:') else
                               'время последней успешной проверки не подтверждено')
    description=event['message']
    if event['kind']=='recovered':
        description='Внешний backup настроен и свежая копия подтверждена' if key=='setup:external' else 'Проверка снова успешна; ранее: '+description
    parts=[f"[Контроль системы] {title}: {description}.",
           'Возникло: '+(first+' Баку' if first else 'время возникновения не подтверждено')+'.',
           'Последний успешный контроль: '+last+' Баку.' if last else missing.capitalize()+'.']
    for label,field in (('Проверка наблюдателя','observed_at'),('Health рынка','health_at'),('Время источника','data_at'),('Получение котировки','received_at'),('Защитная проверка','protection_at')):
        if field in event:
            value=when(event[field]);parts.append(label+': '+(value+' Баку' if value else 'время не подтверждено')+'.')
    return ' '.join(parts)
