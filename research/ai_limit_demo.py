"""Deterministic full-path verification, explicitly SYNTHETIC, no HTTP calls."""
import argparse
import datetime as dt
import itertools
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from .ai_limit_book import Book
from .ai_limit_plan import context,validate,dec,text,VERSION
from .ai_limit_executor import api
from .ai_limit_runtime import analyst,atomic,ingest,trusted,Poller
from .ai_limit_stream import StreamFeed
from .ai_limit_backup_job import prepare_journal
from trading_bot.ai_bracket_view import render


def capture(end):
    rows=[]
    price=lambda i:100+4*math.sin(i/7)+2*math.sin(i/23)
    for i in range(960):
        o,c=price(i-1),price(i)
        rows.append([end-(960-i)*900000,text(dec(o)),text(dec(max(o,c)+.25)),text(dec(min(o,c)-.25)),text(dec(c)),'100'])
    return {'symbol':'BTCUSDT','venue':'binance-usdm','candles':rows}


def book_quote(now,price='100',id='demo-quote'):
    return {'id':id,'symbol':'BTCUSDT','event_ms':now,'received_ms':now,'bid':price,'ask':text(dec(price)+dec('.1')),
            'bid_qty':'5','ask_qty':'5','mark':price}


def fixture_decision(snapshot,side,now):
    base={'snapshot_id':snapshot['snapshot_id'],'decision':side,'entry_level':None,'stop_level':None,'target_level':None,
        'valid_until_ms':snapshot['expires_ms']-1,'max_hold_minutes':60,
        'thesis':'Проверочный ответ: выбор подтверждённых уровней на синтетических свечах.','news_ids':[],
        'risks':['Это имитация ответа, не рекомендация модели и не результат стратегии.']}
    if side=='WAIT':return base
    for e,s,t in itertools.product(snapshot['levels'],repeat=3):
        d={**base,'entry_level':e['id'],'stop_level':s['id'],'target_level':t['id']}
        try:validate(snapshot,d,now)
        except ValueError:continue
        return d
    raise ValueError('fixture_levels_missing')


def demo(root):
    root=Path(root);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    now=[int(dt.datetime(2026,11,13,8,tzinfo=dt.timezone.utc).timestamp()*1000)+60000]
    state=root/'synthetic-paper';budget=root/'TEST_ONLY-budget';budget.mkdir(mode=0o700)
    (budget/'.TEST_ONLY_BUDGET').write_text('NEVER ACTIVE; NO HTTP TRANSPORT\n')
    for n in ('interactive-once.lock','approved-followup-once.lock'):(budget/n).write_text('synthetic-original-lock\n')
    (budget/'server-owner.json').write_text(json.dumps({'role':'ACTIVE_SERVER','migration_id':'SYNTHETIC_ONLY'}))
    with sqlite3.connect(budget/'experiment-budget.db') as db:
        db.execute('PRAGMA application_id='+str(api.APP_ID))
        db.execute('CREATE TABLE api_attempts(request_id TEXT PRIMARY KEY,reserved_micro_usd INTEGER NOT NULL,created_at REAL NOT NULL,result_json TEXT)')
        db.execute('INSERT INTO api_attempts VALUES(?,?,?,NULL)',('TEST-original',3630,1))
    public=root/'orders.json';response=root/'response.json';book=Book(state,clock=lambda:now[0]);book.pause(False)
    prepare_journal(book)
    reports=[];calls=[0]
    class FixtureFeed(StreamFeed):
        def __init__(self):
            super().__init__(symbols=['BTCUSDT'],quotes=True,clock=lambda:now[0])
            self.trade_id=0;self.depth_id=0
            self.states['BTCUSDT']['connected']=True
            for channel in ('book','market'):self.quote_connection('BTCUSDT',channel,True)
        def get(self,*a,**k):raise AssertionError('synthetic_demo_forbids_network')
        def filters(self,symbol,**kwargs):
            return {'as_of_ms':now[0],'tick_size':'.1','step_size':'.001','min_qty':'.001','min_notional':'50'}
        def funding(self,*a,**k):return []  # explicit synthetic funding history
        def snapshot(self,symbol):
            end=now[0]//900000*900000;c=capture(end)
            q=book_quote(now[0]);f=self.filters(symbol)
            return context(c,q,f,{'coverage':'limited','high_risk_until_ms':0,'items':[]},now[0]),c
        def emit_quote(self,price='100'):
            q=book_quote(now[0],price);previous=self.depth_id;self.depth_id+=1
            self.receive_quote('BTCUSDT',{'e':'depthUpdate','s':'BTCUSDT','U':self.depth_id,'u':self.depth_id,'pu':previous,
                'E':now[0],'T':now[0],'b':[[q['bid'],q['bid_qty']]],'a':[[q['ask'],q['ask_qty']]]},'book')
            self.receive_quote('BTCUSDT',{'e':'markPriceUpdate','s':'BTCUSDT','E':now[0],'p':price,'T':now[0]+8*3600000},'market')
        def emit_trade(self,price='100',qty='1',seller=True):
            self.trade_id+=1
            raw={'e':'aggTrade','s':'BTCUSDT','a':self.trade_id,'T':now[0],'p':price,'q':qty,'m':seller}
            self.receive('BTCUSDT',raw);return raw
    feed=FixtureFeed();poller=Poller(book,feed,symbols=['BTCUSDT'])
    # Funding cashflow is separately supplied below, never fetched from a service.
    poller.funding_ready=lambda:None
    def pump(price='100',trade=None):
        feed.emit_quote(price)
        if trade is not None:feed.emit_trade(*trade)
        value=poller.cycle()
        for future in poller.filter_jobs.values():future.result(timeout=2)
        atomic(public,value)
        return value
    for side,exit_reason in (('LONG','TP'),('SHORT','SL_gap'),('SHORT','TIME'),('WAIT',None)):
        book.funding(book.get('funding_through'),now[0],[])
        with book.transaction():book.set('next_due:BTCUSDT',now[0]+8*3600000)
        # Bootstrap and subsequent fresh continuity use the production parser,
        # queues, quote components and grouped financial transaction.
        now[0]+=100;pump(trade=('100','1',True))
        now[0]+=100;ready=pump(trade=('100','1',True))
        assert ready['market_errors']['BTCUSDT'] is None
        def transport(req):
            calls[0]+=1;s=json.loads(req['input'][1]['content']);d=fixture_decision(s,side,now[0])
            return {'status':'completed','usage':{'input_tokens':700,'output_tokens':250},'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(d)}]}]}
        result=analyst(budget,public,response,symbol='BTCUSDT',paper_uid=os.geteuid(),feed=feed,clock=lambda:now[0],transport=transport)
        outcome=ingest(book,trusted(response,os.geteuid()),allow_fixture=True)
        if side=='WAIT':
            reports.append({'direction':side,'result':outcome,'created_trade':False});break
        p,s=book.active();qty=dec(p['quantity']);first=(qty/2/dec('.001')).to_integral_value()*dec('.001');first=min(first,qty)
        # New quote is fresh at the actual limit before the later aggressor
        # print trades through; no candle-touch fills or phantom live orders.
        now[0]+=100;pump(p['entry'])
        for i,size in enumerate((first,qty-first)):
            if size<=0:continue
            now[0]+=100
            price=text(dec(p['entry'])+(-1 if side=='LONG' else 1)*dec('.1'))
            raw=feed.emit_trade(price,text(size),side=='LONG');pump(p['entry'])
            before=book.snapshot()['fills'];feed.receive('BTCUSDT',raw)
            now[0]+=1;pump(p['entry']);assert book.snapshot()['fills']==before
        if exit_reason=='TP':
            now[0]+=100;pump(p['target'],(text(dec(p['target'])+dec('.1')),p['quantity'],False))
        elif exit_reason=='SL_gap':
            # Persisted stop survives process restart; model is never called.
            book.close();book=Book(state,clock=lambda:now[0]);poller.book=book
            now[0]+=100;price=text(dec(p['stop'])+dec('1'))
            pump(price)
        else:
            # No synthetic stale-price exit: the deadline is latched during
            # a total quote outage and survives restart before fresh recovery.
            for channel in ('book','market'):feed.quote_connection('BTCUSDT',channel,False,'fixture_disconnect')
            now[0]=book.active()[1]['opened_ms']+p['hold_ms']
            poller.cycle();pending=book.active()[1]
            assert pending['status']=='EXIT_PENDING' and dec(pending['open_qty'])>0
            trigger=pending['protection_trigger'];assert trigger['reason']=='TIME'
            book.close();book=Book(state,clock=lambda:now[0]);poller.book=book
            for channel in ('book','market'):feed.quote_connection('BTCUSDT',channel,True)
            now[0]+=100;pump(text(dec(p['entry'])-dec('1')))
            assert book.active()[1]['protection_trigger']==trigger
        assert book.active()[1]['status']=='RECONCILING'
        book.funding(book.get('funding_through'),now[0],[])
        done=next(s for item,s in book.plans() if item['plan_id']==p['plan_id'])
        assert done['status']=='CLOSED' and dec(done['open_qty'])==0
        reports.append({'direction':side,'plan_id':p['plan_id'],'entry':p['entry'],'tp':p['target'],'sl':p['stop'],
            'gross':done['gross'],'fees':done['fees'],'funding':done['funding'],
            'net':text(dec(done['gross'])-dec(done['fees'])+dec(done['funding'])),'status':done['status'],'exit_reason':exit_reason})
        now[0]=((now[0]//3600000)+1)*3600000+60000
    snapshot=book.snapshot();atomic(public,snapshot)
    restored=root/'restore';restored.mkdir(mode=0o700);book.backup(restored/'paper.sqlite')
    clone=Book(restored,clock=lambda:now[0])
    try:assert clone.plans()==book.plans() and clone.verify()==book.verify()
    finally:clone.close()
    screenshot=render(public,now[0]/1000,symbol='BTCUSDT');(root/'telegram-orders.txt').write_text(screenshot)
    result={'status':'PASS','fixture_only':True,'real_model_calls':0,'live_or_testnet_orders':0,'simulated_model_responses':calls[0],
        'execution_path':'production_StreamFeed_Poller_Book',
        'checks':['websocket_depth_and_mark_components','websocket_trade_continuity','grouped_financial_transactions',
            'context_to_strict_request','original_budget_fixture_reservation','saved_response_and_usage','trusted_publisher',
            'source_level_recomputation','partial_long_and_short_limit_fills','idempotency','TP','gap_SL_after_restart',
            'TIME_during_quote_outage_then_restart_and_fresh_recovery','funding_finalization','SQLite_restore','Telegram_read_only_view'],
        'cycles':reports,'balances':snapshot['balances'],'fills':snapshot['fills'],'ledger_balanced':snapshot['ledger_balanced']}
    atomic(root/'result.json',result);poller.close();book.close();return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args();print(json.dumps(demo(args.output),ensure_ascii=False,indent=2))
