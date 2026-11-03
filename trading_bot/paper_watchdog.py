"""Offline health assessment and transition suppression; no Telegram polling."""
import math

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
        if h.get('paused') is True and h.get('pause_source') not in ('manual','deployment'):
            put('pause:'+market,market+(': защитная пауза' if h.get('pause_source')=='protective' else ': причина паузы не подтверждена'))
        if market=='spot' and h.get('telegram_error'):put('telegram','Spot: ошибка Telegram-поллера')
        if market in ('cross','margin'):
            if h.get('quotes_fresh') is not True:put('quotes:'+market,market+': котировки не подтверждены',stamp)
            protection=h.get('protection_checked_at')
            protection=(protection/1000 if market=='margin' else protection) if isinstance(protection,(int,float)) else None
            if h.get('protection_fresh') is not True or not fresh(protection,120):
                put('protection:'+market,market+': защита не подтверждена',protection)
        else:
            quote=h.get('quote') or {}
            if not fresh(quote.get('time'),120):put('quotes:'+market,market+': устарела котировка',quote.get('time'))
    q=snapshot.get('queue',{})
    if not isinstance(q.get('pending'),int) or q['pending']<0:put('queue:unknown','Очередь: состояние не подтверждено')
    elif q['pending'] and (not fresh(q.get('oldest_received'),300) or q['pending']>50):put('queue:stuck','Очередь: команды ожидают слишком долго или очередь растёт',q.get('last_completed'))
    for kind,limit in (('local',7200),('external',7200)):
        last=snapshot.get('backups',{}).get(kind)
        if not fresh(last,limit):put('backup:'+kind,kind+': успешная копия отсутствует или устарела',last)
    resources=snapshot.get('resources',{})
    if not isinstance(resources.get('memory_available_bytes'),int) or resources['memory_available_bytes']<100*1024*1024:put('memory','Мало свободной памяти или она неизвестна')
    if not isinstance(resources.get('disk_free_bytes'),int) or resources['disk_free_bytes']<500*1024*1024:put('disk','Мало свободного места или оно неизвестно')
    return problems


def transitions(current,previous,now,*,reminder_seconds=3600):
    """Events for new faults, one bounded reminder, and explicit recovery."""
    if reminder_seconds<60:raise ValueError('Reminder interval too short')
    state={};events=[]
    for key,item in current.items():
        old=previous.get(key)
        if old is None or now-old['last_notice']>=reminder_seconds:
            events.append({'key':key,'kind':'failure','at':now,**item});last=now
        else:last=old['last_notice']
        state[key]={'last_notice':last,'message':item['message'],'last_success':item.get('last_success')}
    for key,old in previous.items():
        if key not in current:events.append({'key':key,'kind':'recovered','at':now,'message':old['message'],'last_success':now})
    return state,events
