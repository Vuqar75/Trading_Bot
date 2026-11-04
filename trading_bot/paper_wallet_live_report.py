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
        spot_asset=shared._spot_asset(positions['spot'])
        btc=shared.ledger.balance('spot:position',spot_asset)+shared.ledger.balance('spot:reserved',spot_asset)
        margin=shared.ledger.balance('futures_isolated:margin');funding_open=shared.ledger.balance('futures_isolated:funding')
        entry_fee=-shared.ledger.balance('futures_isolated:entry_fee')
        incomplete=[]
        def quote(market):
            q=shared._get(market,'quote')
            if not isinstance(q,dict):return None
            if market=='spot' and q.get('symbol','BTCUSDT')!=spot_asset+'USDT':return None
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
        margin_cross=None; margin_cross_value=Decimal(0)
        if shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='margin_positions'").fetchone():
            from .margin_paper_book import MarginPaperBook
            from .margin_interest_ledger import MarginInterestLedger, HOUR_MS
            mb=object.__new__(MarginPaperBook);mb.ledger=shared.ledger;mb.db=shared.db
            mb.loans=object.__new__(MarginInterestLedger);mb.loans.ledger=shared.ledger;mb.loans.db=shared.db
            mb._check()
            balances['margin_cross']={'free':shared.ledger.balance('margin_cross:free'),'reserved':shared.ledger.balance('margin_cross:reserved')}
            assets={a:{k:shared.ledger.balance('margin_cross:'+k,a) for k in ('free','reserved','principal','interest')} for a in ('USDT','BTC','ETH')}
            loans=[dict(r) for r in shared.db.execute("SELECT * FROM margin_loans WHERE status='OPEN' ORDER BY id")]
            now_ms=int(now*1000);due=False
            for loan in loans:
                last_row=shared.db.execute('SELECT period,at_ms FROM margin_interest_periods WHERE loan_id=? ORDER BY period DESC LIMIT 1',(loan['id'],)).fetchone()
                last=last_row['period'] if last_row else None
                loan['last_accrual_ms']=last_row['at_ms'] if last_row else None
                loan['next_accrual_ms']=loan['opened_ms']+(0 if last is None else last+1)*HOUR_MS
                if now_ms<loan['opened_ms'] or last is None or last<(now_ms-loan['opened_ms'])//HOUR_MS:due=True
            if due:incomplete.append('Margin cross: не начислены все причитающиеся периоды процентов')
            marks=None
            try:marks=mb._marks(mb.stored_quotes(),now_ms)
            except ValueError:
                if any(any(assets[a][k] for k in assets[a]) for a in ('BTC','ETH')):
                    incomplete.append('Margin cross: цены отсутствуют или устарели')
            margin_assets=Decimal(0);margin_debt=assets['USDT']['principal']+assets['USDT']['interest']
            if marks:
                for asset in ('BTC','ETH'):
                    margin_assets+=(assets[asset]['free']+assets[asset]['reserved'])*marks[asset]
                    margin_debt+=(assets[asset]['principal']+assets[asset]['interest'])*marks[asset]
            elif any(any(assets[a][k] for k in assets[a]) for a in ('BTC','ETH')):
                margin_assets=None;margin_debt=None
            margin_cross_value=None if margin_assets is None or margin_debt is None else margin_assets+margin_debt
            margin_positions=mb._positions()
            settings=shared.db.execute('SELECT leverage,interval FROM margin_settings WHERE id=1').fetchone()
            state=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()
            error_row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
            if not settings or not state or not error_row:raise ValueError('Margin status unavailable')
            rates={}
            for asset in ('USDT','BTC','ETH'):
                rate=shared.db.execute('SELECT rate FROM margin_loan_rates WHERE asset=? AND effective_ms<=? ORDER BY effective_ms DESC LIMIT 1',(asset,now_ms)).fetchone()
                rates[asset]=amount(rate['rate']) if rate else None
            margin_cross={'assets':assets,'loans':loans,'positions':margin_positions,'asset_value':margin_assets,'debt_value':margin_debt,'marks':marks,'due':due,
                          'quotes':mb.stored_quotes(),'rates':rates,'paused':bool(state['paused']),'error':json.loads(error_row['value']),
                          'leverage':settings['leverage'],'interval':settings['interval'],
                          'locked':sum((amount(pos['own']) for pos in margin_positions),Decimal(0)),
                          'equity':None if due or margin_cross_value is None else balances['margin_cross']['free']+balances['margin_cross']['reserved']+margin_cross_value}
            closes=[json.loads(r['details']) for r in shared.db.execute("SELECT created,details FROM ledger_events WHERE kind='FINANCIAL_METADATA' AND id LIKE 'margin:position-close:%'") if period=='all' or start<=r['created']<=now]
            margin_cross['realized']=None if any(r['net'] is None for r in closes) else sum((amount(r['net']) for r in closes),Decimal(0))
            margin_cross['fees']=Decimal(0);margin_cross['interest_total']={a:Decimal(0) for a in ('USDT','BTC','ETH')}
            for row in shared.db.execute("SELECT e.created,p.account,p.asset,p.amount FROM ledger_events e JOIN ledger_postings p ON p.event_id=e.id WHERE p.account IN ('external:margin_fee','external:margin_interest')"):
                if period=='today' and not start<=row['created']<=now:continue
                if row['account']=='external:margin_fee':margin_cross['fees']+=amount(row['amount'])
                else:margin_cross['interest_total'][row['asset']]+=amount(row['amount'])
        total=None if incomplete else sum((v['free']+v['reserved'] for v in balances.values()),Decimal(0)) + (btc*sp if btc else 0) + isolated + cross_value + margin_cross_value
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
               'cross_exit_cost':-cross['exit_cost'] if cross and cross['exit_cost'] is not None else (None if cross else Decimal(0)),
               'margin_assets':margin_cross['asset_value'] if margin_cross else Decimal(0),
               'margin_debt':margin_cross['debt_value'] if margin_cross else Decimal(0)}
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
            if margin_cross and not margin_cross['due'] and margin_cross['marks'] and shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]:
                error=shared.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
                orphan=shared.db.execute("SELECT 1 FROM margin_loans l WHERE l.status='OPEN' AND NOT EXISTS (SELECT 1 FROM margin_positions p WHERE p.id=l.position_id)").fetchone()
                if error and json.loads(error[0]) is None and not orphan:
                    available['margin_cross']=mb._risk(margin_cross['marks'])['available']
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
        data={'wallets':balances,'btc':btc,'spot_asset':spot_asset,'margin':margin,'funding_open':funding_open,'entry_fee':entry_fee,
              'spot_open':spot_open if sp is not None or not btc else None,'futures_open':future_open if fp is not None or not pos else None,
              'spot_realized':spot_realized,'futures_realized':futures_realized,'spot_fees':spot_fees if period=='all' else None,
              'future_fees':future_fees,'funding_total':funding_total,'transfers':transfers,'total':total,
              'incomplete':incomplete,'spot_quote_time':(shared._get('spot','quote') or {}).get('time'),
              'futures_quote_time':(shared._get('futures','quote') or {}).get('time'),'period':period,'cross':cross,
              'valuation_terms':terms,'valuation_ok':valuation_ok,'available':available,'margin_cross':margin_cross}
        return data


def render_wallets(data):
    labels={'reserve':'Общий резерв','spot':'Spot','futures_isolated':'Futures isolated','futures_cross':'Futures cross','margin_cross':'Spot margin cross'}
    lines=['PAPER: свободно | резерв ордеров | маржа позиций (USDT)']
    for wallet,values in data['wallets'].items():
        margin=data['margin'] if wallet=='futures_isolated' else (data['cross']['margin'] if wallet=='futures_cross' and data.get('cross') else (data['margin_cross']['locked'] if wallet=='margin_cross' and data.get('margin_cross') else Decimal(0)))
        lines.append(labels.get(wallet,wallet)+': '+_fmt(values['free'])+' | '+_fmt(values['reserved'])+' | '+_fmt(margin))
    lines.append('Резерв ордеров не включает маржу позиций. Обеспечение margin cross отражено в свободном остатке для учёта капитала; обеспечение позиции нельзя переводить. Доступность остатка проверяется отдельно. /wallet ИМЯ — доступность перевода; /balance_report — оценка капитала.')
    return '\n'.join(lines)


def render_live_report(data):
    title='сегодня по Баку' if data['period']=='today' else 'за всю историю'
    terms=data['valuation_terms']
    lines=['Paper-отчёт по средствам: '+title,'Общий капитал (оценка): '+_fmt(data['total'])+' USDT']
    for wallet,values in data['wallets'].items():
        label={'reserve':'Общий резерв','spot':'Spot','futures_isolated':'Futures isolated','futures_cross':'Futures cross'}.get(wallet,wallet)
        lines.append(label+': свободно '+_fmt(values['free'])+' USDT; резерв ордеров '+_fmt(values['reserved'])+' USDT')
    lines.append('Свободно — средства без резерва ордеров; в margin cross сумма включает обеспечение позиции, которое нельзя переводить. Доступность перевода проверяется с учётом паузы, риска и получателя: /wallet ИМЯ.')
    lines += ['Расчёт оценки капитала (USDT; суммы без повторного учёта):',
              'Свободные средства + резервы ордеров: '+_fmt(terms['cash']),
              '+ рыночная стоимость Spot '+data.get('spot_asset','BTC')+': '+_fmt(terms['spot_asset']),
              '+ isolated: маржа '+_fmt(terms['isolated_margin'])+'; funding '+_fmt(terms['isolated_funding'])+'; открытый PnL '+_fmt(terms['isolated_open']),
              '− комиссия входа isolated: '+_fmt(terms['isolated_entry_fee'].copy_negate())+'; − оценочные издержки выхода: '+_fmt(terms['isolated_exit_cost'].copy_negate() if terms['isolated_exit_cost'] is not None else None)+'; корректировка ограничения нулём: '+_fmt(terms['isolated_floor']),
              '+ cross: маржа '+_fmt(terms['cross_margin'])+'; funding '+_fmt(terms['cross_funding'])+'; открытый PnL '+_fmt(terms['cross_open'])+'; обязательства '+_fmt(terms['cross_liability'])+'; − оценочные издержки выхода '+_fmt(terms['cross_exit_cost'].copy_negate() if terms['cross_exit_cost'] is not None else None),
              '+ margin cross: стоимость криптоактивов '+_fmt(terms['margin_assets'])+'; обязательства '+_fmt(terms['margin_debt']),
              'Учтённые комиссии и реализованный PnL отражены в остатках или отдельных строках выше; повторно не вычитаются. Округлены только показанные числа; сумма строк может отличаться из-за округления.',
              'Проверка расчёта оценки: '+('пройдена' if data['valuation_ok'] is True else 'расхождение' if data['valuation_ok'] is False else 'неполные данные')+'.']
    lines += ['Spot актив: '+_fmt(data['btc'],data.get('spot_asset','BTC'))+' '+data.get('spot_asset','BTC')+'; результат к себестоимости: '+_fmt(data['spot_open'])+' USDT',
              'Futures isolated маржа: '+_fmt(data['margin'])+' USDT; открытый результат до выхода: '+_fmt(data['futures_open'])+' USDT',
              'Реализованный результат с учтёнными издержками: Spot '+_fmt(data['spot_realized'])+' USDT; futures isolated '+_fmt(data['futures_realized'])+' USDT',
              'Spot комиссии по активам: '+('неизвестно за день' if data['spot_fees'] is None else (', '.join(_fmt(v,k)+' '+k for k,v in sorted(data['spot_fees'].items())) or 'нет зафиксированных')),
              'Futures isolated комиссии: '+_fmt(data['future_fees'])+' USDT; funding: '+_fmt(data['funding_total'])+' USDT',
              'Внешние движения: история не подтверждена; внутренних переводов: '+str(len(data['transfers'])),
              'Проверка проводок и остатков: пройдена. Цена Spot: '+(when(data['spot_quote_time']) if data['spot_quote_time'] else 'нет данных')+'; Futures isolated: '+(when(data['futures_quote_time']) if data['futures_quote_time'] else 'нет данных')]
    cross=data.get('cross')
    if cross is None:lines.append('futures_cross: недоступен.')
    else:
        lines.append('Futures cross • PAPER: капитал '+_fmt(cross['equity'])+' USDT; маржа '+_fmt(cross['margin'])+' USDT; обязательства '+_fmt(-cross['liability'])+' USDT; запас риска '+_fmt(cross['buffer'])+' USDT; риск-лимит перевода '+_fmt(cross['available'])+' USDT (нужна пауза и проверка получателя).')
        lines.append('Cross результат открытых позиций: '+_fmt(cross['gross'])+'; реализованный до комиссий: '+_fmt(cross['realized'])+'; комиссии: '+_fmt(cross['fees'])+'; funding: '+_fmt(cross['funding_total'])+'; модельная корректировка: '+_fmt(cross['adjustment'])+' USDT.')
        lines.append('Оценка cross использует доступные данные funding; окончательность публикации источником не гарантирована.')
        for pos in cross['positions']:
            lines.append(pos['symbol']+' '+pos['direction']+' — '+_fmt(pos['quantity'],pos['symbol'][:-4])+' '+pos['symbol'][:-4]+'; вход '+_fmt(pos['entry'])+' USDT; маржа '+_fmt(pos['margin'])+' USDT.')
    mc=data.get('margin_cross')
    if mc is None:lines.append('margin_cross: недоступен.')
    else:
        lines.append('Spot margin cross • PAPER: капитал после обязательств '+_fmt(mc['equity'])+' USDT; обеспечение позиций '+_fmt(mc['locked'])+' USDT. Котировки — последняя публичная Spot-сделка; активы и обязательства:')
        for asset,v in mc['assets'].items():
            lines.append(asset+': свободно '+_fmt(v['free'],asset)+'; резерв '+_fmt(v['reserved'],asset)+'; основной долг '+_fmt(-v['principal'],asset)+'; проценты '+_fmt(-v['interest'],asset)+' '+asset+'.')
        lines.append('Результат закрытых позиций (paper-оценка процентов по цене выхода): '+_fmt(mc['realized'])+' USDT; комиссии '+_fmt(mc['fees'])+' USDT; начисленные проценты: '+', '.join(_fmt(v,a)+' '+a for a,v in mc['interest_total'].items())+'. Повторно из капитала они не вычитаются.')
    if data['incomplete']:lines.append('Неполные данные: '+ '; '.join(data['incomplete']))
    if data['period']=='today':lines.append('Суточные Spot-комиссии не подтверждены временными метками старой истории.')
    return '\n'.join(lines)
