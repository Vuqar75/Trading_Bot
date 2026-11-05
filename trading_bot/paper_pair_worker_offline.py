"""Offline harness for pair commands; not enabled by any service entry point."""
import json
import time
from decimal import Decimal, localcontext

from .paper_pair_catalog import PairMetadataError
from .paper_pair_selection_offline import (select_spot_pair_offline, select_isolated_pair_offline,
                                           select_cross_pairs_offline, select_margin_pairs_offline)


def _select_or_expire(queue,job,select):
    try:return select()
    except PairMetadataError:
        queue.acknowledge(job['id'],'Пара не проходит проверку рыночных фильтров.',applied=False)
        return None
    except ValueError as exc:
        final={'Pair confirmation expired','Stale pair selection','Stale cross pair selection',
               'Stale margin pair selection','Spot must be paused','Isolated futures must be paused',
               'Cross must be paused','Margin must be paused','Existing futures position prevents selection',
               'Spot position or unknown fees prevent selection','Existing cross positions prevent selection',
               'Margin position or debt prevents selection','Spot order unresolved',
               'Spot command pending','Futures command pending','Cross command pending','Margin command pending',
               'Spot confirmation pending','Futures confirmation pending','Cross confirmation pending','Margin confirmation pending',
               'Spot transfer pending','Futures transfer pending','Cross transfer pending','Margin transfer pending',
               'Cross reserve or liability prevents selection','Margin reserve prevents selection'}
        if str(exc) not in final:raise
        queue.acknowledge(job['id'],'Смена пары отклонена: условия изменились или подтверждение истекло. Проверьте состояние и отправьте новую команду.',applied=False)
        return None


def _unsupported(queue,job):
    queue.acknowledge(job['id'],'Пара отсутствует в полученных рыночных данных.',applied=False)


def _exposure_preflight(queue,shared,job,payload,market):
    """Reject risk-bearing selection before network; selection rechecks under lock."""
    tables={'spot':'pair_spot_selection_events','futures':'pair_isolated_selection_events',
            'futures_cross':'pair_cross_selection_events','margin_cross':'pair_margin_selection_events'}
    with shared.transaction():
        table=tables[market]
        if (shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() and
            shared.db.execute('SELECT 1 FROM '+table+' WHERE request_id=?',('pair:'+payload['proposal'],)).fetchone()):
            return False  # A previously committed selection must replay its acknowledgement.
        if market=='spot':
            position=shared._get('spot','position')
            if not isinstance(position,dict):raise ValueError('Spot position unavailable')
            blocked=Decimal(str(position['quantity']))!=0 or not position.get('fees_known')
        elif market=='futures':blocked=shared._get('futures','position') is not None
        else:
            table='cross_positions' if market=='futures_cross' else 'margin_positions'
            if not shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                raise ValueError('Market position state unavailable')
            blocked=bool(shared.db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone())
            if market=='margin_cross':
                blocked=blocked or bool(shared.db.execute("SELECT 1 FROM margin_loans WHERE status='OPEN' LIMIT 1").fetchone())
    if blocked:
        queue.acknowledge(job['id'],'Смена пары отклонена: открытая позиция или долг требуют контроля.',applied=False)
    return blocked


def process_spot_pair(queue, shared, reader, *, budget='10', clock=time.time,
                      fault=lambda stage:None):
    if not queue.allow_pair_experiment:raise ValueError('Offline pair queue required')
    budget=Decimal(str(budget))
    if not budget.is_finite() or not 0<budget<=10:raise ValueError('Invalid paper budget')
    for job in queue.pending('spot'):
        if job['command']=='/pair_status':
            identity=shared._get('spot','identity')
            queue.acknowledge(job['id'],'[SPOT] Пара: '+identity['symbol'],notify=True)
            continue
        if job['command']!='/confirmed_pair':continue
        payload=json.loads(job['payload']);target=payload['target'][0]
        if _exposure_preflight(queue,shared,job,payload,'spot'):continue
        if shared.db.in_transaction or queue.db.in_transaction:raise ValueError('Network inside SQLite transaction')
        info_response=reader.get('/api/v3/exchangeInfo',{'symbol':target})
        symbols=info_response.get('symbols') if isinstance(info_response,dict) else None
        if isinstance(symbols,list) and not symbols:_unsupported(queue,job);continue
        if not isinstance(symbols,list) or len(symbols)!=1:raise ValueError('Pair metadata unavailable')
        quote=reader.get('/api/v3/ticker/price',{'symbol':target});observed=clock()
        if not isinstance(quote,dict) or quote.get('symbol')!=target:raise ValueError('Pair quote mismatch')
        price=Decimal(str(quote['price']))
        if not price.is_finite() or price<=0:raise ValueError('Invalid pair price')
        with localcontext() as ctx:
            ctx.prec=120
            result=_select_or_expire(queue,job,lambda:select_spot_pair_offline(shared,queue.db.execute('PRAGMA database_list').fetchone()[2],
                request_id='pair:'+payload['proposal'],expected_symbol=payload['previous'][0],
                expected_version=payload['version'],symbol=target,metadata=symbols[0],price=str(price),
                quantity=budget/price,observed_at=observed,clock=clock,confirm_job_id=job['id']))
        if result is None:continue
        fault('after_financial_commit')
        queue.acknowledge(job['id'],'[SPOT] Пара '+result['symbol']+' сохранена; входы остаются на паузе.')
        return result


def process_isolated_pair(queue, shared, reader_factory, *, budget='10', clock=time.time,
                          fault=lambda stage:None):
    if not queue.allow_pair_experiment:raise ValueError('Offline pair queue required')
    budget=Decimal(str(budget))
    if not budget.is_finite() or not 0<budget<=10:raise ValueError('Invalid paper budget')
    for job in queue.pending('futures'):
        if job['command']=='/pair_status':
            identity=shared._get('futures','identity')
            queue.acknowledge(job['id'],'[Futures isolated] Пара: '+identity['symbol'])
            continue
        if job['command']!='/confirmed_pair':continue
        payload=json.loads(job['payload']);target=payload['target'][0]
        if _exposure_preflight(queue,shared,job,payload,'futures'):continue
        if shared.db.in_transaction or queue.db.in_transaction:raise ValueError('Network inside SQLite transaction')
        reader=reader_factory(target)
        if reader.pair_symbol!=target:raise ValueError('Pair reader mismatch')
        response=reader.get('/fapi/v1/exchangeInfo');symbols=response.get('symbols') if isinstance(response,dict) else None
        if isinstance(symbols,list) and any(not isinstance(r,dict) or not isinstance(r.get('symbol'),str) for r in symbols):
            raise ValueError('Invalid pair metadata')
        matches=[item for item in symbols if isinstance(item,dict) and item.get('symbol')==target] if isinstance(symbols,list) else []
        if isinstance(symbols,list) and not matches:_unsupported(queue,job);continue
        if len(matches)!=1:raise ValueError('Pair metadata unavailable')
        price,_=reader.mark();observed=clock();price=Decimal(str(price))
        if not price.is_finite() or price<=0:raise ValueError('Invalid pair price')
        with localcontext() as ctx:
            ctx.prec=120
            result=_select_or_expire(queue,job,lambda:select_isolated_pair_offline(shared,queue.db.execute('PRAGMA database_list').fetchone()[2],
                request_id='pair:'+payload['proposal'],expected_symbol=payload['previous'][0],
                expected_version=payload['version'],symbol=target,metadata=matches[0],price=str(price),
                quantity=budget/price,observed_at=observed,clock=clock,confirm_job_id=job['id']))
        if result is None:continue
        fault('after_financial_commit')
        queue.acknowledge(job['id'],'[Futures isolated] Пара '+result['symbol']+' сохранена; входы остаются на паузе.')
        return result


def process_cross_pair(queue, shared, reader_factory, *, budget='5', clock=time.time,
                       fault=lambda stage:None):
    if not queue.allow_pair_experiment:raise ValueError('Offline pair queue required')
    budget=Decimal(str(budget))
    if not budget.is_finite() or not 0<budget<=5:raise ValueError('Invalid paper margin')
    for job in queue.pending('futures_cross'):
        if job['command']=='/pair_status':
            from .cross_paper_book import CrossPaperBook
            book=object.__new__(CrossPaperBook);book.db=shared.db;book.shared=shared;book.ledger=shared.ledger
            queue.acknowledge(job['id'],'[Futures cross] Пары: '+', '.join(book._symbols()))
            continue
        if job['command']!='/confirmed_pair':continue
        payload=json.loads(job['payload']);targets=payload['target']
        if _exposure_preflight(queue,shared,job,payload,'futures_cross'):continue
        if shared.db.in_transaction or queue.db.in_transaction:raise ValueError('Network inside SQLite transaction')
        reader=reader_factory(targets)
        response=reader.get('/fapi/v1/exchangeInfo');rows=response.get('symbols') if isinstance(response,dict) else None
        if not isinstance(rows,list):raise ValueError('Pair metadata unavailable')
        if any(not isinstance(r,dict) or not isinstance(r.get('symbol'),str) for r in rows):raise ValueError('Invalid pair metadata')
        metadata={};prices={};quantities={};observations=[];unsupported=False
        for symbol in targets:
            matches=[r for r in rows if isinstance(r,dict) and r.get('symbol')==symbol]
            if not matches:unsupported=True;break
            if len(matches)!=1:raise ValueError('Pair metadata unavailable')
            schedule=reader.schedule(symbol);observations.append(clock())
            if schedule.get('symbol')!=symbol:raise ValueError('Pair quote mismatch')
            price=Decimal(str(schedule['mark']))
            if not price.is_finite() or price<=0:raise ValueError('Invalid pair price')
            metadata[symbol]=matches[0];prices[symbol]=str(price)
            with localcontext() as ctx:
                ctx.prec=120;quantities[symbol]=budget/price
        if unsupported:_unsupported(queue,job);continue
        result=_select_or_expire(queue,job,lambda:select_cross_pairs_offline(shared,queue.db.execute('PRAGMA database_list').fetchone()[2],
            request_id='pair:'+payload['proposal'],expected_symbols=payload['previous'],
            expected_version=payload['version'],symbols=targets,metadata=metadata,prices=prices,
            quantities=quantities,observed_at=min(observations),clock=clock,confirm_job_id=job['id']))
        if result is None:continue
        fault('after_financial_commit')
        queue.acknowledge(job['id'],'[Futures cross] Пары '+', '.join(result['symbols'])+' сохранены; входы остаются на паузе.')
        return result


def process_margin_pair(queue, shared, reader_factory, *, budget='5', clock=time.time,
                        fault=lambda stage:None):
    if not queue.allow_pair_experiment:raise ValueError('Offline pair queue required')
    budget=Decimal(str(budget))
    if not budget.is_finite() or not 0<budget<=5:raise ValueError('Invalid paper margin')
    for job in queue.pending('margin_cross'):
        if job['command']=='/pair_status':
            from .paper_pair_runtime_offline import restore_margin_book
            book=restore_margin_book(shared)
            queue.acknowledge(job['id'],'[Spot margin cross] Пары: '+', '.join(sorted(book.tradable)))
            continue
        if job['command']!='/confirmed_pair':continue
        payload=json.loads(job['payload']);targets=payload['target']
        if _exposure_preflight(queue,shared,job,payload,'margin_cross'):continue
        if shared.db.in_transaction or queue.db.in_transaction:raise ValueError('Network inside SQLite transaction')
        reader=reader_factory(targets);metadata={};prices={};quantities={};observations=[];unsupported=False
        for symbol in targets:
            response=reader.get('/api/v3/exchangeInfo',{'symbol':symbol})
            rows=response.get('symbols') if isinstance(response,dict) else None
            if isinstance(rows,list) and not rows:unsupported=True;break
            if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict) or rows[0].get('symbol')!=symbol:raise ValueError('Pair metadata unavailable')
            quote=reader.snapshot(symbol);observed=clock();observations.append(observed)
            if (not isinstance(quote,dict) or type(quote.get('time_ms')) is not int or
                not 0<=observed*1000-quote['time_ms']<=120000):raise ValueError('Pair quote stale')
            price=Decimal(str(quote['price']))
            if not price.is_finite() or price<=0:raise ValueError('Invalid pair price')
            metadata[symbol]=rows[0];prices[symbol]=str(price)
            with localcontext() as ctx:
                ctx.prec=120;quantities[symbol]=budget/price
        if unsupported:_unsupported(queue,job);continue
        result=_select_or_expire(queue,job,lambda:select_margin_pairs_offline(shared,queue.db.execute('PRAGMA database_list').fetchone()[2],
            request_id='pair:'+payload['proposal'],expected_symbols=payload['previous'],
            expected_version=payload['version'],symbols=targets,metadata=metadata,prices=prices,
            quantities=quantities,observed_at=min(observations),clock=clock,confirm_job_id=job['id']))
        if result is None:continue
        fault('after_financial_commit')
        queue.acknowledge(job['id'],'[Spot margin cross] Пары '+', '.join(result['symbols'])+' сохранены; входы остаются на паузе.')
        return result
