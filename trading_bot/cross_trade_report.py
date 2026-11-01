"""Offline cross trading report: closed positions, Baku day and cumulative."""
import json
import time
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo
from .paper_wallet_ledger import amount, encode


def cross_trade_report(shared, *, now=None):
    now=time.time() if now is None else now
    local=datetime.fromtimestamp(now,ZoneInfo('Asia/Baku'))
    start=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    with shared.transaction(), localcontext() as ctx:
        ctx.prec=120
        if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
        opening={}
        for row in shared.db.execute("SELECT e.id,e.details,p.amount FROM ledger_events e JOIN ledger_postings p ON p.event_id=e.id WHERE e.kind='CROSS_OPEN' AND p.account='external:cross_fees'"):
            p=json.loads(row['details']);ident=p['id']
            if ident in opening:raise ValueError('Duplicate cross opening identity')
            opening[ident]=amount(row['amount'])
        periods={'today':{'count':0,'net':Decimal(0),'fees':Decimal(0),'funding':Decimal(0),'known':True,'adjustment':Decimal(0)},
                 'all':{'count':0,'net':Decimal(0),'fees':Decimal(0),'funding':Decimal(0),'known':True,'adjustment':Decimal(0)}}
        for row in shared.db.execute("SELECT kind,created,details FROM ledger_events WHERE kind IN ('CROSS_CLOSE','CROSS_LIQUIDATION_ADJUSTMENT') AND created<=?",(now,)):
            details=json.loads(row['details'])
            for name,out in periods.items():
                if name=='today' and row['created']<start:continue
                if row['kind']=='CROSS_LIQUIDATION_ADJUSTMENT':
                    out['adjustment']+=amount(details['quantity']);continue
                out['count']+=1
                fee=opening.get(details['position_id'])
                if fee is None:
                    out['known']=False;continue
                fees=fee+amount(details['exit_fee']);funding=amount(details['funding']);gross=amount(details['gross'])
                out['fees']+=fees;out['funding']+=funding;out['net']+=gross+funding-fees
        lines=['Futures cross • PAPER • торговый отчёт']
        for name,label in [('today','Сегодня по Баку ('+local.strftime('%d.%m.%Y')+')'),('all','За всё время')]:
            v=periods[name]
            if v['known']:
                lines.append(label+': закрыто '+str(v['count'])+', net '+encode(v['net'])+', комиссии '+encode(v['fees'])+', funding '+encode(v['funding'])+', модельная корректировка '+encode(v['adjustment'])+' USDT.')
            else:
                lines.append(label+': закрыто '+str(v['count'])+'; результат и комиссии неполные — не найдена комиссия входа.')
        lines.append('Результат относится к закрытым позициям; модельная корректировка показана отдельно.')
        return '\n'.join(lines)
