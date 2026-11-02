"""Read-only valuation of the single-file paper journal and runtime shadows."""
import json
import time
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo
from .paper_wallet_ledger import amount
from .telegram_reports import when
from .display_amount import display_amount


def _fmt(value, unit='USDT'):
    return display_amount(value,unit)


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
        close_cost=Decimal(0);isolated_floor=Decimal(0)
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
                isolated_raw=margin+funding_open-entry_fee+future_open-close_cost
                isolated=max(Decimal(0),isolated_raw)
                isolated_floor=isolated-isolated_raw
        start=datetime.fromtimestamp(now,ZoneInfo('Asia/Baku')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        cross=None;cross_value=Decimal(0)
        if shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_positions'").fetchone():
            from .cross_paper_book import CrossPaperBook
            book=object.__new__(CrossPaperBook);book.shared=shared;book.db=shared.db;book.ledger=shared.ledger
            book._check()
            positions_cross=book._positions()
            balances['futures_cross']={'free':shared.ledger.balance('futures_cross:free'),'reserved':shared.ledger.balance('futures_cross:reserved')}
            if positions_cross:
                try:
                    book._require_coverage(now)
                    marks=book._marks(book.stored_quotes(),now)
                    cross=book._risk(marks)
                    cross_value=cross['margin']+cross['funding']+cross['gross']+cross['liability']-cross['exit_cost']
                except ValueError:
                    incomplete.append('Futures cross: оценка или funding неполны')
                    cross={'positions':positions_cross,'free':balances['futures_cross']['free'],
                           'reserved':balances['futures_cross']['reserved'],
                           'margin':shared.ledger.balance('futures_cross:margin'),
                           'funding':shared.ledger.balance('futures_cross:funding'),
                           'liability':shared.ledger.balance('futures_cross:liability'),
                           'gross':None,'equity':None,'maintenance':None,'exit_cost':None,'buffer':None,'available':None}
            else:
                free=balances['futures_cross']['free'];reserved=balances['futures_cross']['reserved'];liability=shared.ledger.balance('futures_cross:liability')
                cross_value=liability
                cross={'positions':[],'free':free,'reserved':reserved,'margin':Decimal(0),'funding':Decimal(0),
                       'liability':liability,'gross':Decimal(0),'equity':free+reserved+liability,'maintenance':Decimal(0),'exit_cost':Decimal(0),'buffer':free+reserved+liability,'available':free if liability==0 else Decimal(0)}
            cross.update(realized=Decimal(0),fees=Decimal(0),funding_total=Decimal(0),adjustment=Decimal(0))
            for row in shared.db.execute("SELECT e.kind,e.created,e.details,p.account,p.amount FROM ledger_events e JOIN ledger_postings p ON p.event_id=e.id WHERE p.account IN ('external:cross_pnl','external:cross_fees','external:cross_funding','external:cross_liquidation_adjustment')"):
                timestamp=row['created']
                if row['kind'] in ('CROSS_FUNDING','CROSS_LATE_FUNDING'):
                    details=json.loads(row['details']);timestamp=details['time']/1000
                if period=='today' and not start<=timestamp<=now:continue
                value=amount(row['amount'])
                if row['account']=='external:cross_pnl':cross['realized']-=value
                elif row['account']=='external:cross_fees':cross['fees']+=value
                elif row['account']=='external:cross_funding':cross['funding_total']-=value
                else:cross['adjustment']-=value
        total=None if incomplete else sum((v['free']+v['reserved'] for v in balances.values()),Decimal(0)) + (btc*sp if btc else 0) + isolated + cross_value
        # Separate terms preserve the existing valuation exactly. Entry fees
        # and estimated exit costs are not removed from the journal balances.
        cash=sum((v['free']+v['reserved'] for v in balances.values()),Decimal(0))
        spot_value=btc*sp if btc and sp is not None else (Decimal(0) if not btc else None)
        iso_known=not pos or fp is not None
        terms={'cash':cash,'spot_asset':spot_value,'isolated_margin':margin,
               'isolated_funding':funding_open,'isolated_open':future_open if iso_known else None,
               'isolated_entry_fee':-entry_fee,'isolated_exit_cost':-close_cost if iso_known else None,
               'isolated_floor':isolated_floor if iso_known else None,
               'cross_margin':cross['margin'] if cross else Decimal(0),
               'cross_funding':cross['funding'] if cross else Decimal(0),
               'cross_open':cross['gross'] if cross else Decimal(0),
               'cross_liability':cross['liability'] if cross else Decimal(0),
               'cross_exit_cost':-cross['exit_cost'] if cross and cross['exit_cost'] is not None else (None if cross else Decimal(0))}
        valuation_ok=None if total is None or any(v is None for v in terms.values()) else sum(terms.values(),Decimal(0))==total
        available={w:None for w in balances}
        if not shared.deployment_hold():
            available['reserve']=balances['reserve']['free']
            if spot['paused'] and positions['spot']['fees_known'] and not shared._get('spot','error') and not shared._get('spot','blocked') and not shared.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
                available['spot']=balances['spot']['free']
            if future['paused'] and not shared._get('futures','error') and not shared._get('futures','blocked'):
                available['futures_isolated']=balances['futures_isolated']['free']
            if cross and shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0] and not book.runtime_error():
                try:
                    book._require_coverage(now)
                    available['futures_cross']=book._risk(book._marks(book.stored_quotes(),now))['available']
                except ValueError:pass
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
              'futures_quote_time':(shared._get('futures','quote') or {}).get('time'),'period':period,'cross':cross,
              'valuation_terms':terms,'valuation_ok':valuation_ok,'available':available}
        return data


def render_wallets(data):
    labels={'reserve':'Общий резерв','spot':'Spot','futures_isolated':'Futures isolated','futures_cross':'Futures cross'}
    lines=['PAPER: свободно | резерв ордеров | маржа позиций (USDT)']
    for wallet,values in data['wallets'].items():
        margin=data['margin'] if wallet=='futures_isolated' else (data['cross']['margin'] if wallet=='futures_cross' and data.get('cross') else Decimal(0))
        lines.append(labels.get(wallet,wallet)+': '+_fmt(values['free'])+' | '+_fmt(values['reserved'])+' | '+_fmt(margin))
    lines.append('Резерв ордеров не включает маржу позиций. /wallet ИМЯ — доступность перевода; /balance_report — оценка капитала.')
    return '\n'.join(lines)


def render_live_report(data):
    title='сегодня по Баку' if data['period']=='today' else 'за всю историю'
    terms=data['valuation_terms']
    lines=['Paper-отчёт по средствам: '+title,'Общий капитал (оценка): '+_fmt(data['total'])+' USDT']
    for wallet,values in data['wallets'].items():
        label={'reserve':'Общий резерв','spot':'Spot','futures_isolated':'Futures isolated','futures_cross':'Futures cross'}.get(wallet,wallet)
        lines.append(label+': свободно '+_fmt(values['free'])+' USDT; резерв ордеров '+_fmt(values['reserved'])+' USDT')
    lines.append('Свободно — средства без резерва ордеров и маржи позиций. Доступность перевода проверяется с учётом паузы, риска и получателя: /wallet ИМЯ.')
    lines += ['Расчёт оценки капитала (USDT; суммы без повторного учёта):',
              'Свободные средства + резервы ордеров: '+_fmt(terms['cash']),
              '+ рыночная стоимость Spot BTC: '+_fmt(terms['spot_asset']),
              '+ isolated: маржа '+_fmt(terms['isolated_margin'])+'; funding '+_fmt(terms['isolated_funding'])+'; открытый PnL '+_fmt(terms['isolated_open']),
              '− комиссия входа isolated: '+_fmt(terms['isolated_entry_fee'].copy_negate())+'; − оценочные издержки выхода: '+_fmt(terms['isolated_exit_cost'].copy_negate() if terms['isolated_exit_cost'] is not None else None)+'; корректировка ограничения нулём: '+_fmt(terms['isolated_floor']),
              '+ cross: маржа '+_fmt(terms['cross_margin'])+'; funding '+_fmt(terms['cross_funding'])+'; открытый PnL '+_fmt(terms['cross_open'])+'; обязательства '+_fmt(terms['cross_liability'])+'; − оценочные издержки выхода '+_fmt(terms['cross_exit_cost'].copy_negate() if terms['cross_exit_cost'] is not None else None),
              'Учтённые комиссии и реализованный PnL отражены в остатках или отдельных строках выше; повторно не вычитаются. Округлены только показанные числа; сумма строк может отличаться из-за округления.',
              'Проверка расчёта оценки: '+('пройдена' if data['valuation_ok'] is True else 'расхождение' if data['valuation_ok'] is False else 'неполные данные')+'.']
    lines += ['Spot актив: '+_fmt(data['btc'],'BTC')+' BTC; результат к себестоимости: '+_fmt(data['spot_open'])+' USDT',
              'Futures isolated маржа: '+_fmt(data['margin'])+' USDT; открытый результат до выхода: '+_fmt(data['futures_open'])+' USDT',
              'Реализованный результат с учтёнными издержками: Spot '+_fmt(data['spot_realized'])+' USDT; futures isolated '+_fmt(data['futures_realized'])+' USDT',
              'Spot комиссии по активам: '+('неизвестно за день' if data['spot_fees'] is None else (', '.join(_fmt(v,k)+' '+k for k,v in sorted(data['spot_fees'].items())) or 'нет зафиксированных')),
              'Futures isolated комиссии: '+_fmt(data['future_fees'])+' USDT; funding: '+_fmt(data['funding_total'])+' USDT',
              'Внешние движения: история не подтверждена; внутренних переводов: '+str(len(data['transfers'])),
              'Проверка проводок и остатков: пройдена. Цена Spot: '+(when(data['spot_quote_time']) if data['spot_quote_time'] else 'нет данных')+'; Futures isolated: '+(when(data['futures_quote_time']) if data['futures_quote_time'] else 'нет данных')]
    cross=data.get('cross')
    if cross is None:lines.append('futures_cross и margin_cross: недоступны.')
    else:
        lines.append('Futures cross • PAPER: капитал '+_fmt(cross['equity'])+' USDT; маржа '+_fmt(cross['margin'])+' USDT; обязательства '+_fmt(-cross['liability'])+' USDT; запас риска '+_fmt(cross['buffer'])+' USDT; риск-лимит перевода '+_fmt(cross['available'])+' USDT (нужна пауза и проверка получателя).')
        lines.append('Cross результат открытых позиций: '+_fmt(cross['gross'])+'; реализованный до комиссий: '+_fmt(cross['realized'])+'; комиссии: '+_fmt(cross['fees'])+'; funding: '+_fmt(cross['funding_total'])+'; модельная корректировка: '+_fmt(cross['adjustment'])+' USDT.')
        lines.append('Оценка cross использует доступные данные funding; окончательность публикации источником не гарантирована.')
        for pos in cross['positions']:
            lines.append(pos['symbol']+' '+pos['direction']+' — '+_fmt(pos['quantity'],pos['symbol'][:-4])+' '+pos['symbol'][:-4]+'; вход '+_fmt(pos['entry'])+' USDT; маржа '+_fmt(pos['margin'])+' USDT.')
        lines.append('margin_cross: недоступен.')
    if data['incomplete']:lines.append('Неполные данные: '+ '; '.join(data['incomplete']))
    if data['period']=='today':lines.append('Суточные Spot-комиссии не подтверждены временными метками старой истории.')
    return '\n'.join(lines)
