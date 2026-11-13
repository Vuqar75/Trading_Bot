"""Read-only cancellation evidence and labels; can run standalone on the VM.

No Book constructor, imports of transports, credentials, or budget access.
Legacy ledger events are read without rewriting plan state or financial history.
"""
import argparse
import datetime as dt
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3

LABELS={
    'EXPIRED':'истёк срок лимитной заявки',
    'PAUSE':'новые входы поставлены на паузу',
    'BLOCKED':'сработала блокировка новых входов',
    'TRADE_GAP':'разрыв или устаревание публичного потока сделок',
    'FILTERS_CHANGED':'изменились биржевые ограничения инструмента',
    'ENTRY_EVIDENCE_STALE':'данные для исполнения входа не подтверждены',
    'stale_or_future_market_data':'котировка устарела или имеет будущее время',
    'stale_filters':'срок данных биржевых ограничений истёк',
    'funding_coverage_stale':'funding не подтверждён для входа',
    'mark_price_divergence':'mark и рыночная котировка расходятся сверх допуска',
    'entry_book_liquidity_missing':'нет доступной ликвидности для входа',
    'entry_evidence_invalid':'неверная структура данных исполнения',
    'funding_history_conflict':'обнаружено расхождение истории funding',
    'late_missing_funding_event':'обнаружено ранее неучтённое событие funding',
    'negative_paper_equity':'недостаточный виртуальный капитал',
    'MANUAL':'ручная отмена/закрытие',
    'TIME':'достигнут предел удержания позиции',
    'SL':'сработал защитный стоп',
    'TP':'началось исполнение take-profit',
    'EXPIRED_OR_PAUSED':'срок, пауза или блокировка; точная ветка в старой записи не сохранена',
}


def label(value):
    if not isinstance(value,dict):return 'причина в сохранённых данных не указана'
    causes=value.get('causes')
    if not isinstance(causes,list) or not causes:causes=[value.get('reason')]
    result='; '.join(LABELS.get(k,'неизвестный код причины') for k in causes)
    detail=value.get('detail')
    if detail in LABELS and detail not in causes:result+=': '+LABELS[detail]
    return result


def describe(db,plan,state):
    saved=state.get('entry_cancellation')
    if isinstance(saved,dict):return saved
    row=db.execute("SELECT kind,created,details FROM ledger_events WHERE id=?",(plan['plan_id']+':cancel-entry',)).fetchone()
    if not row or row[0]!='CANCEL_REMAINDER':return None
    details=json.loads(row[2]);reason=details.get('reason')
    if not isinstance(reason,str):return None
    postings=db.execute("SELECT amount FROM ledger_postings WHERE event_id=? AND account='ai_bracket:reserved' AND asset='USDT'",(plan['plan_id']+':cancel-entry',)).fetchall()
    with localcontext() as context:
        context.prec=120
        released=-sum((Decimal(r[0]) for r in postings),Decimal(0))
        filled=sum((Decimal(r[0]) for r in db.execute("SELECT qty FROM bracket_fills WHERE plan_id=? AND kind='ENTRY'",(plan['plan_id'],))),Decimal(0))
        quantity=Decimal(plan['quantity'])-filled
    return {'reason':reason,'causes':[reason],'at_ms':round(row[1]*1000),
        'quantity':format(quantity,'f'),'reserve_released_usdt':format(released,'f'),
        'evidence':'legacy_ledger','precise':reason!='EXPIRED_OR_PAUSED'}


def audit(path,*,entry=None):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('existing_database_required')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        if db.execute('PRAGMA application_id').fetchone()[0]!=0x41494252:raise ValueError('wrong_bracket_database')
        output=[]
        for raw,state in db.execute('SELECT payload,state FROM bracket_plans ORDER BY rowid'):
            plan=json.loads(raw);state=json.loads(state)
            if entry is not None and Decimal(plan['entry'])!=Decimal(entry):continue
            evidence=describe(db,plan,state)
            output.append({'plan_id':plan['plan_id'],'symbol':plan['symbol'],'entry':plan['entry'],'status':state['status'],
                'direction':plan['direction'],'armed_ms':state.get('armed_ms'),'expires_ms':plan['expires_ms'],
                'open_qty':state['open_qty'],'remaining':state['remaining'],'entry_cancellation':evidence,
                'cancel_time_baku':dt.datetime.fromtimestamp(evidence['at_ms']/1000,dt.timezone(dt.timedelta(hours=4))).isoformat() if evidence else None,
                'explanation':label(evidence) if evidence else 'запись об отмене отсутствует'})
        return {'read_only':True,'plans':output}
    finally:db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--db',required=True);parser.add_argument('--entry')
    args=parser.parse_args();print(json.dumps(audit(args.db,entry=args.entry),ensure_ascii=False,indent=2))
