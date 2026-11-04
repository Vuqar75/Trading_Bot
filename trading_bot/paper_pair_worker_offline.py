"""Offline harness for pair commands; not enabled by any service entry point."""
import json
import time
from decimal import Decimal, localcontext

from .paper_pair_selection_offline import select_spot_pair_offline, select_isolated_pair_offline


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
        if shared.db.in_transaction or queue.db.in_transaction:raise ValueError('Network inside SQLite transaction')
        info_response=reader.get('/api/v3/exchangeInfo',{'symbol':target})
        symbols=info_response.get('symbols') if isinstance(info_response,dict) else None
        if not isinstance(symbols,list) or len(symbols)!=1:raise ValueError('Pair metadata unavailable')
        quote=reader.get('/api/v3/ticker/price',{'symbol':target});observed=clock()
        if not isinstance(quote,dict) or quote.get('symbol')!=target:raise ValueError('Pair quote mismatch')
        price=Decimal(str(quote['price']))
        if not price.is_finite() or price<=0:raise ValueError('Invalid pair price')
        with localcontext() as ctx:
            ctx.prec=120
            result=select_spot_pair_offline(shared,queue.db.execute('PRAGMA database_list').fetchone()[2],
                request_id='pair:'+payload['proposal'],expected_symbol=payload['previous'][0],
                expected_version=payload['version'],symbol=target,metadata=symbols[0],price=str(price),
                quantity=str(budget/price),observed_at=observed,clock=clock,confirm_job_id=job['id'])
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
        if shared.db.in_transaction or queue.db.in_transaction:raise ValueError('Network inside SQLite transaction')
        reader=reader_factory(target)
        if reader.pair_symbol!=target:raise ValueError('Pair reader mismatch')
        response=reader.get('/fapi/v1/exchangeInfo');symbols=response.get('symbols') if isinstance(response,dict) else None
        matches=[item for item in symbols if isinstance(item,dict) and item.get('symbol')==target] if isinstance(symbols,list) else []
        if len(matches)!=1:raise ValueError('Pair metadata unavailable')
        price,_=reader.mark();observed=clock();price=Decimal(str(price))
        if not price.is_finite() or price<=0:raise ValueError('Invalid pair price')
        with localcontext() as ctx:
            ctx.prec=120
            result=select_isolated_pair_offline(shared,queue.db.execute('PRAGMA database_list').fetchone()[2],
                request_id='pair:'+payload['proposal'],expected_symbol=payload['previous'][0],
                expected_version=payload['version'],symbol=target,metadata=matches[0],price=str(price),
                quantity=str(budget/price),observed_at=observed,clock=clock,confirm_job_id=job['id'])
        fault('after_financial_commit')
        queue.acknowledge(job['id'],'[Futures isolated] Пара '+result['symbol']+' сохранена; входы остаются на паузе.')
        return result
