"""Read-only valuation of the single-file paper journal and runtime shadows."""
import json
import time
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo
from .paper_wallet_ledger import amount, encode
from .telegram_reports import when


def _fmt(value):
    return 'неизвестно' if value is None else encode(value)


def live_report(shared, *, now=None, period='all', max_age=120):
    if period not in ('all','today'):raise ValueError('Unsupported report period')
    now=time.time() if now is None else now
    with shared.transaction(), localcontext() as ctx:
        ctx.prec=120
        reconciliation=shared.ledger.reconcile()
        if not reconciliation['balanced']:raise ValueError('Journal integrity mismatch')
        spot=shared._assert_consistent('spot');future=shared._assert_consistent('futures')
        positions={'spot':spot['position'],'futures':future['position']}
        balances={w:{'free':shared.ledger.balance(w+':free'),'reserved':shared.ledger.balance(w+':reserved')} for w in ('reserve','spot','futures_isolated')}
        btc=shared.ledger.balance('spot:position','BTC')+shared.ledger.balance('spot:reserved','BTC')
        margin=shared.ledger.balance('futures_isolated:margin');funding_open=shared.ledger.balance('futures_isolated:funding')
        entry_fee=-shared.ledger.balance('futures_isolated:entry_fee')
        incomplete=[]
        def quote(market):
            q=shared._get(market,'quote')
            if not isinstance(q,dict):return None
            try:
                age=now-float(q['time']); price=amount(q['price'],positive=True)
                return price if 0<=age<=max_age else None
            except (ValueError,KeyError,TypeError,OverflowError):return None
        sp=quote('spot');fp=quote('futures');spot_open=Decimal(0);future_open=Decimal(0);isolated=Decimal(0)
        if btc:
            if sp is None:incomplete.append('Spot-цена отсутствует или устарела')
            else:spot_open=btc*sp-amount(positions['spot']['cost'])
        if not positions['spot'].get('fees_known'):incomplete.append('часть Spot-комиссий не оценена')
        pos=positions['futures']
        if pos:
            if fp is None:incomplete.append('Futures mark отсутствует или устарел')
            else:
                qty=amount(pos['quantity'],positive=True);direction=pos.get('direction')
                if direction not in ('LONG','SHORT'):raise ValueError('Unknown futures direction')
                future_open=(fp-amount(pos['entry'],positive=True))*qty*(1 if direction=='LONG' else -1)
                identity=shared._get('futures','identity')
                fee=amount(identity['fee_bps']);slip=amount(identity['slippage_bps'])
                exit_price=fp*(1+(1 if direction=='SHORT' else -1)*slip/10000)
                close_cost=qty*exit_price*fee/10000+qty*fp*slip/10000
                isolated=max(Decimal(0),margin+funding_open-entry_fee+future_open-close_cost)
        total=None if incomplete else sum((v['free']+v['reserved'] for v in balances.values()),Decimal(0)) + (btc*sp if btc else 0) + isolated
        start=datetime.fromtimestamp(now,ZoneInfo('Asia/Baku')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        spot_closes=shared.db.execute('SELECT ts,pnl,known FROM runtime_closes').fetchall()
        future_closes=[(r['ts'],json.loads(r['payload'])) for r in shared.db.execute("SELECT ts,payload FROM futures_events WHERE event='CLOSE'")]
        selected_spot=[r for r in spot_closes if period=='all' or start<=r['ts']<=now]
        selected_future=[r for r in future_closes if period=='all' or start<=r[0]<=now]
        spot_realized=None if any(not r['known'] for r in selected_spot) or (period=='all' and not positions['spot'].get('fees_known')) else sum((Decimal(r['pnl']) for r in selected_spot),Decimal(0))
        futures_realized=sum((Decimal(p['net']) for _,p in selected_future),Decimal(0))
        if period=='all':
            # The pre-existing runtime accumulates PnL with the default Decimal
            # context; replay the same arithmetic before comparing its shadow.
            with localcontext() as legacy:
                legacy.prec=28
                spot_legacy=sum((Decimal(r['pnl']) for r in selected_spot),Decimal(0))
                future_legacy=sum((Decimal(p['net']) for _,p in selected_future),Decimal(0))
            if (spot_realized is not None and spot_legacy!=Decimal(spot['pnl'])) or future_legacy!=Decimal(future['pnl']):
                raise ValueError('Realized PnL disagrees with recorded history')
            if spot_realized is not None:spot_realized=Decimal(spot['pnl'])
            futures_realized=Decimal(future['pnl'])
        spot_fees={}
        if period=='all':
            for row in shared.db.execute('SELECT payload FROM runtime_fills'):
                fill=json.loads(row['payload']);asset=fill['commissionAsset'];spot_fees[asset]=spot_fees.get(asset,Decimal(0))+amount(fill['commission'])
        if period=='all':
            future_fees=sum((amount(p['fees']) for _,p in future_closes),Decimal(0))+entry_fee
            funding_total=sum((Decimal(p['funding']) for _,p in future_closes),Decimal(0))+funding_open
        else:
            future_fees=Decimal(0);funding_total=Decimal(0);opened={}
            for row in shared.db.execute("SELECT ts,event,payload FROM futures_events WHERE event IN ('OPEN','CLOSE') ORDER BY id"):
                event=json.loads(row['payload'])
                if row['event']=='OPEN':opened[event['id']]=event
                if start<=row['ts']<=now:
                    if row['event']=='OPEN':future_fees+=amount(event['entry_fee'])
                    else:future_fees+=amount(event['fees'])-amount(event['entry_fee'])
            with localcontext() as legacy:
                legacy.prec=28
                for row in shared.db.execute('SELECT position_id,ts,payload FROM futures_funding ORDER BY ts'):
                    if not start<=row['ts']/1000<=now:continue
                    origin=opened.get(row['position_id'])
                    if not origin or origin.get('direction') not in ('LONG','SHORT'):
                        funding_total=None;break
                    item=json.loads(row['payload'])
                    funding_total+=amount(origin['quantity'])*amount(item['mark'])*Decimal(item['rate'])*(-1 if origin['direction']=='LONG' else 1)
        transfers=[]
        for row in shared.db.execute("SELECT created,details FROM ledger_events WHERE kind='TRANSFER' ORDER BY created,id"):
            if period=='all' or start<=row['created']<=now:transfers.append(json.loads(row['details']))
        data={'wallets':balances,'btc':btc,'margin':margin,'funding_open':funding_open,'entry_fee':entry_fee,
              'spot_open':spot_open if sp is not None or not btc else None,'futures_open':future_open if fp is not None or not pos else None,
              'spot_realized':spot_realized,'futures_realized':futures_realized,'spot_fees':spot_fees if period=='all' else None,
              'future_fees':future_fees,'funding_total':funding_total,'transfers':transfers,'total':total,
              'incomplete':incomplete,'spot_quote_time':(shared._get('spot','quote') or {}).get('time'),
              'futures_quote_time':(shared._get('futures','quote') or {}).get('time'),'period':period}
        return data


def render_live_report(data):
    title='сегодня по Баку' if data['period']=='today' else 'за всю историю'
    lines=['[Кошельки] Paper-отчёт: '+title,'Общий капитал (оценка): '+_fmt(data['total'])+' USDT']
    for wallet,values in data['wallets'].items():
        lines.append(wallet+': свободно '+_fmt(values['free'])+' USDT; резерв '+_fmt(values['reserved'])+' USDT')
    lines += ['Spot актив: '+_fmt(data['btc'])+' BTC; результат к себестоимости: '+_fmt(data['spot_open'])+' USDT',
              'Futures маржа: '+_fmt(data['margin'])+' USDT; открытый результат до выхода: '+_fmt(data['futures_open'])+' USDT',
              'Реализованный результат с учтёнными издержками: Spot '+_fmt(data['spot_realized'])+' USDT; futures '+_fmt(data['futures_realized'])+' USDT',
              'Spot комиссии по активам: '+('неизвестно за день' if data['spot_fees'] is None else (', '.join(_fmt(v)+' '+k for k,v in sorted(data['spot_fees'].items())) or 'нет зафиксированных')),
              'Futures комиссии: '+_fmt(data['future_fees'])+' USDT; funding: '+_fmt(data['funding_total'])+' USDT',
              'Внешние движения: история не подтверждена; внутренних переводов: '+str(len(data['transfers'])),
              'Сверка проводок и остатков: пройдена. Цена Spot: '+(when(data['spot_quote_time']) if data['spot_quote_time'] else 'нет данных')+'; Futures: '+(when(data['futures_quote_time']) if data['futures_quote_time'] else 'нет данных'),
              'futures_cross и margin_cross: недоступны.']
    if data['incomplete']:lines.append('Неполные данные: '+ '; '.join(data['incomplete']))
    if data['period']=='today':lines.append('Суточные Spot-комиссии не подтверждены временными метками старой истории.')
    return '\n'.join(lines)
