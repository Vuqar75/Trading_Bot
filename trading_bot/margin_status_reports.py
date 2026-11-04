"""Russian margin paper reports from one read-only financial snapshot."""
from decimal import Decimal, localcontext
from .display_amount import display_amount as fmt
from .telegram_reports import when


def _at(ms):
    return when(ms/1000) if type(ms) is int else 'неизвестно'


def loans_report(margin):
    if not margin['loans']:
        return 'Активных займов нет. Долг и текущие проценты отсутствуют.'
    lines=['Активные paper-займы:']
    for loan in margin['loans']:
        asset=loan['asset']
        lines.append('Займ '+loan['id']+' — '+asset+': Основной долг '+fmt(loan['principal'],asset)+' '+asset+
                     '; проценты '+fmt(loan['interest'],asset)+' '+asset+
                     '; Последнее начисление: '+_at(loan['last_accrual_ms'])+'.')
    if margin['due']:lines.append('Не все положенные периоды начислены; суммы неполные.')
    return '\n'.join(lines)


def interest_report(margin,now):
    lines=['Ставки paper-модели за начатый час (не тариф биржи):']
    with localcontext() as context:
        context.prec=120
        for asset in ('USDT','BTC','ETH'):
            rate=margin['rates'][asset]
            lines.append(asset+': '+(fmt(rate*Decimal(100))+'% в час.' if rate is not None else 'ставка неизвестна.'))
    lines.append('Начислено за всё время: '+', '.join(fmt(margin['interest_total'][asset],asset)+' '+asset for asset in ('USDT','BTC','ETH'))+'.')
    if not margin['loans']:
        lines.append('Следующего начисления нет: активных займов нет.')
    for loan in margin['loans']:
        due=loan['next_accrual_ms'];overdue=type(due) is int and due<=int(now*1000)
        lines.append('Займ '+loan['id']+' ('+loan['asset']+'): Следующее начисление '+_at(due)+
                     ('; период ожидает начисления.' if overdue else '.'))
    if margin['due']:lines.append('Начислены не все положенные периоды; сумма неполная.')
    return '\n'.join(lines)


def status_report(data,now):
    m=data['margin_cross'];assets=m['assets'];free=assets['USDT']['free'];reserved=assets['USDT']['reserved']
    lines=[('Входы на паузе' if m['paused'] else 'Paper-входы разрешены')+'.',
           'Параметры: BTCUSDT и ETHUSDT; '+m['interval']+'; плечо '+str(m['leverage'])+'×; собственные средства до 5 USDT на позицию.',
           'Собственный капитал после долгов: '+fmt(m['equity'])+' USDT.',
           'Свободно: '+fmt(free)+' USDT; Резерв ордеров: '+fmt(reserved)+' USDT; обеспечение позиций: '+fmt(m['locked'])+' USDT.',
           'Обеспечение отражено в свободном остатке для учёта капитала; обеспечение позиции нельзя переводить. Доступность перевода проверяется отдельно через /wallet margin_cross.']
    for asset in ('BTC','ETH'):
        lines.append(asset+': свободно '+fmt(assets[asset]['free'],asset)+'; резерв ордеров '+fmt(assets[asset]['reserved'],asset)+' '+asset+'.')
    if not m['positions']:lines.append('Позиции: нет.')
    else:
        lines.append('Позиции:')
        for p in m['positions']:
            asset=p['symbol'][:-4]
            lines.append(p['symbol']+' '+p['direction']+' — '+fmt(p['quantity'],asset)+' '+asset+
                         '; вход '+fmt(p['entry'])+' USDT; обеспечение '+fmt(p['own'])+' USDT.')
    for title,key in (('Долг','principal'),('Проценты','interest')):
        parts=[fmt(-assets[a][key],a)+' '+a for a in ('USDT','BTC','ETH') if assets[a][key]]
        lines.append(title+': '+(', '.join(parts) if parts else 'нет')+'.')
    lines.append('Котировки: последняя публичная сделка Spot.')
    missing=False
    for symbol in ('BTCUSDT','ETHUSDT'):
        q=m['quotes'].get(symbol) or {};stamp=q.get('time_ms')
        fresh=type(stamp) is int and 0<=int(now*1000)-stamp<=120000
        missing=missing or not fresh
        lines.append(symbol+': '+(fmt(q.get('price'))+' USDT; '+_at(stamp) if q.get('price') is not None else 'нет данных')+
                     ('' if fresh else ' — устарело или отсутствует')+'.')
    errors=[]
    if m['error']:errors.append('ошибка процесса; входы требуют проверки')
    if m['due']:errors.append('не начислены все положенные проценты')
    if missing:errors.append('котировки устарели или отсутствуют')
    if m['equity'] is None:errors.append('оценка капитала неполная')
    lines.append('Ошибки: '+('; '.join(errors) if errors else 'нет')+'.')
    return '\n'.join(lines)
