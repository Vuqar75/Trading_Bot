"""Read-only presentation of the NEW isolated AI bracket account, not legacy ML."""
import datetime as dt
import json
from pathlib import Path
import re
import time
from decimal import Decimal

REPORT=Path('/var/lib/ai-bracket-public/orders.json')
from .ai_bracket_cancellation import label as cancellation_label

STATES={'WORKING':'лимитный вход ожидает исполнения','PARTIAL':'вход исполнен частично','OPEN':'позиция открыта',
        'EXIT_PENDING':'защитный выход ещё не исполнен полностью','RECONCILING':'рыночная позиция закрыта; funding ещё сверяется',
        'CLOSED':'позиция закрыта, расчёт завершён','CANCELED':'вход отменён, позиции нет'}


def render(path=None,now=None,*,symbol='BTCUSDT'):
    path=REPORT if path is None else Path(path);now=time.time() if now is None else now
    try:
        if path.is_symlink() or path.stat().st_size>200000:raise ValueError('snapshot_size')
        value=json.loads(path.read_text())
        if value.get('version')!='ai-limit-brackets-v1' or value.get('paper_only') is not True or value.get('ledger_balanced') is not True:raise ValueError('snapshot_contract')
        age=now*1000-value['as_of_ms']
        if age<0:raise ValueError('future_snapshot')
        items=[p for p in value['plans'] if p['symbol']==symbol]
        if len(items)>30:raise ValueError('snapshot_plan_count')
        lines=['[ИИ • '+symbol+' • отдельный paper-счёт TP/SL]',
            'Виртуальные деньги. Лимитные исполнения моделируются, это не биржевые ордера.']
        if age>15000:lines.append('ДАННЫЕ УСТАРЕЛИ — текущее состояние не подтверждено.')
        lines.append('Новые входы: '+('пауза' if value['paused'] else 'разрешены только этому paper-контуру'))
        if value.get('market_errors',{}).get(symbol):lines.append('Свежие рыночные данные не подтверждены.')
        if value.get('funding_error'):lines.append('Funding пока не подтверждён; расчёт может измениться.')
        last=value.get('analyst_last')
        if last and last.get('symbol')==symbol:
            label={'WAIT':'ждать','PLAN':'план получен','REJECTED':'план отклонён проверками','UNCONFIRMED':'ответ не подтверждён'}.get(last['status'],last['status'])
            lines.append('Последний анализ: '+label+'.')
        if value['blocked']:lines.append('Блокировка входов: '+str(value['blocked'])[:100])
        active=[p for p in items if p['status'] not in ('CLOSED','CANCELED')]
        chosen=active[-1] if active else items[-1] if items else None
        if chosen:
            state=chosen['status']
            if state not in STATES:raise ValueError('unknown_state')
            for k in ('open_qty','remaining','entry','stop','target','net_provisional'):
                n=Decimal(chosen[k])
                if not n.is_finite():raise ValueError('invalid_amount')
            if Decimal(chosen['open_qty'])>0 and (value.get('quote_asof_ms',{}).get(symbol) is None or now*1000-value['quote_asof_ms'][symbol]>5000):lines.append('Нет свежей котировки: исполнение защитного выхода пока не подтверждено.')
            lines+=['План: '+chosen['direction']+' — '+STATES[state]+'.',
                'Лимит входа '+chosen['entry']+'; TP '+chosen['target']+'; SL '+chosen['stop']+' USDT.',
                'Открыто: '+chosen['open_qty']+' '+symbol[:-4]+'. Остаток входа: '+chosen['remaining']+'.',
                ('Итог net: ' if state=='CLOSED' else 'Учтённый net, предварительно: ')+chosen['net_provisional']+' USDT.']
            cancellation=chosen.get('entry_cancellation')
            if cancellation is not None or state=='CANCELED':
                title='Причина отмены входа: ' if chosen.get('opened_ms') is None else 'Причина отмены остатка входа: '
                lines.append(title+cancellation_label(cancellation)+'.')
                if cancellation and type(cancellation.get('at_ms')) is int:
                    zone=dt.timezone(dt.timedelta(hours=4))
                    when=dt.datetime.fromtimestamp(cancellation['at_ms']/1000,zone).strftime('%d.%m %H:%M:%S Баку')
                    lines.append('Отмена: '+when+'.')
            if chosen['provenance']['kind']=='fixture':lines.append('ПРОВЕРОЧНЫЙ ПРИМЕР: ответ модели и рыночный поток имитированы.')
            else:lines.append('Аналитик: '+chosen['provenance']['model']+'.')
            thesis=' '.join(chosen['thesis'].split());thesis=re.sub(r'https?://\S+','[источник]',thesis)
            lines.append('Идея: '+thesis[:280])
        else:
            last=value.get('analyst_last')
            if last and last.get('symbol')==symbol:
                lines.append('Последний анализ: '+str(last['status'])+'.')
                if last.get('reason'):lines.append('Причина отказа: '+str(last['reason'])[:150])
                if last.get('thesis'):lines.append(re.sub(r'https?://\S+','[источник]',' '.join(last['thesis'].split()))[:280])
            else:lines.append('Плана ИИ для этого актива пока нет. Позиции нет.')
        lines.append('Свободно: '+value['balances']['free']+' USDT; в резерве входа: '+value['balances']['reserved']+'.')
        lines.append('SL работает без ИИ по mark/bid–ask; гэп и недостаток ликвидности могут увеличить убыток.')
        api=value.get('api_summary')
        fixture=(chosen is not None and chosen['provenance']['kind']=='fixture') or (last is not None and last.get('provenance',{}).get('kind')=='fixture')
        if fixture:lines.append('API-расходы в этом примере имитированы; реального вызова не было.')
        if not fixture and api and api.get('symbol')==symbol and api.get('estimated_cost_micro_usd') is not None:
            lines.append('Последний ИИ-анализ: оценка $'+format(Decimal(api['estimated_cost_micro_usd'])/1000000,'f')+'; счёт провайдера не сверялся.')
        lines.append('Обновление экрана не вызывает ИИ и не создаёт сделки.')
        return '\n'.join(lines)
    except FileNotFoundError:return '[ИИ • paper TP/SL] Исполнитель пока не подключён. Отсутствие снимка не означает отсутствие позиции.'
    except (ValueError,KeyError,TypeError,OSError,ArithmeticError):return '[ИИ • paper TP/SL] Состояние не подтверждено: снимок недоступен или повреждён.'
