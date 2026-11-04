"""Durable, market-bound Telegram command queue; transport independent."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from .display_amount import display_amount
from .telegram_help import unified_help_messages, unified_start_message
from contextlib import contextmanager


def market_label(market):
    return {'futures':'Futures isolated • PAPER','futures_cross':'Futures cross • PAPER','margin_cross':'Spot margin cross • PAPER'}.get(market,market.upper())


class UnifiedQueue:
    outbox_table = 'unified_outbox'
    def __init__(self, path, fault=lambda point: None, *, allow_pair_experiment=False):
        self.allow_pair_experiment=allow_pair_experiment
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.fault = fault
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        with self.transaction():
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            expected = {'unified_state', 'unified_updates', 'unified_jobs', 'unified_proposals', 'unified_outbox'}
            if tables and tables != expected:
                raise ValueError('Dedicated unified queue database required')
            self.db.execute('CREATE TABLE IF NOT EXISTS unified_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS unified_updates(id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL,command TEXT NOT NULL,market TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS unified_jobs(id INTEGER PRIMARY KEY,update_id INTEGER NOT NULL REFERENCES unified_updates(id),market TEXT NOT NULL,command TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT \'PENDING\',result TEXT,UNIQUE(update_id,market))')
            self.db.execute('CREATE TABLE IF NOT EXISTS unified_proposals(id TEXT PRIMARY KEY,market TEXT NOT NULL,position TEXT NOT NULL,quantity TEXT NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS unified_outbox(id INTEGER PRIMARY KEY,message TEXT NOT NULL)')
            self.db.execute("INSERT OR IGNORE INTO unified_state VALUES('mode','spot')")
            self.db.execute("INSERT OR IGNORE INTO unified_state VALUES('offset','0')")

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.fault('before_commit')
            self.db.commit()
            self.fault('after_commit')
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def state(self, key):
        return self.db.execute('SELECT value FROM unified_state WHERE key=?', (key,)).fetchone()[0]

    def pair_proposal(self,token):
        row=self.db.execute('SELECT value FROM unified_state WHERE key=?',('pair-proposal:'+token,)).fetchone()
        return json.loads(row[0]) if row else None

    def pair_proposals(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT value FROM unified_state WHERE key LIKE 'pair-proposal:%' ORDER BY key")]

    def _save_pair_proposal(self,proposal):
        self.db.execute('INSERT OR REPLACE INTO unified_state VALUES(?,?)',
                        ('pair-proposal:'+proposal['id'],json.dumps(proposal,sort_keys=True)))

    def _notify(self, text):
        if not text.startswith('['):
            text = '[Управление] ' + text
        self.db.execute('INSERT INTO unified_outbox(message) VALUES(?)', (text,))

    def _close_symbol_valid(self,symbol):
        if symbol in ('BTCUSDT','ETHUSDT'):return True
        return bool(self.allow_pair_experiment and isinstance(symbol,str) and
                    re.fullmatch(r'[A-Z0-9]{2,20}USDT',symbol) and symbol!='USDTUSDT')

    def seed_offset(self, offset):
        with self.transaction():
            if not self.db.execute('SELECT 1 FROM unified_updates LIMIT 1').fetchone():
                self.db.execute("UPDATE unified_state SET value=? WHERE key='offset'", (str(max(int(self.state('offset')), int(offset))),))

    def receive(self, ident, authorized, text, *, fresh=True, now=None, positions=None, pairs=None):
        if type(ident) is not int or not 0 <= ident < 2**63-1 or type(authorized) is not bool:
            raise ValueError('Invalid update')
        text = text if authorized and isinstance(text, str) else ''
        # Freshness is evaluated at first committed receipt, not on a replay.
        fingerprint = hashlib.sha256(json.dumps([authorized, text], ensure_ascii=False).encode()).hexdigest()
        now = time.time() if now is None else now
        with self.transaction():
            old = self.db.execute('SELECT fingerprint FROM unified_updates WHERE id=?', (ident,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError('Update identity conflict')
                return
            if ident < int(self.state('offset')):
                raise ValueError('Update precedes committed offset')
            parts = text.split()
            command = parts[0].split('@')[0] if parts else ''
            mode = self.state('mode')
            market = mode
            payload = {}
            jobs = []
            if authorized:
                if command == '/start' and len(parts) == 1:
                    self._notify(unified_start_message())
                elif command == '/help' and len(parts) == 1:
                    for message in unified_help_messages(market_label(mode),experimental_pair=self.allow_pair_experiment):
                        self._notify(message)
                elif command == '/mode' and len(parts) <= 2:
                    if len(parts) == 2 and parts[1] in ('spot', 'futures', 'futures_cross','margin_cross'):
                        mode = parts[1]
                        self.db.execute("UPDATE unified_state SET value=? WHERE key='mode'", (mode,))
                    elif len(parts) == 2:
                        self._notify('Режим: /mode spot, /mode futures, /mode futures_cross или /mode margin_cross.')
                    self._notify('Выбранный рынок: ' + market_label(mode))
                elif command == '/pause_all' and len(parts) == 1:
                    jobs = [('spot', '/pause'), ('futures', '/pause'), ('futures_cross','/pause'),('margin_cross','/pause')]
                    self._notify('Пауза всех четырёх рынков запрошена; ожидаются подтверждения.')
                elif command == '/status' and len(parts) == 1:
                    jobs = [('spot', '/brief_status'), ('futures', '/brief_status'), ('futures_cross','/brief_status'),('margin_cross','/brief_status')]
                elif command == '/status' and len(parts) == 2 and parts[1] in ('spot', 'futures', 'futures_cross','margin_cross'):
                    market = parts[1]; jobs = [(market, '/status')]
                elif command == '/pair' and self.allow_pair_experiment:
                    snapshot=(pairs or {}).get(market)
                    if len(parts)==1:
                        jobs=[(market,'/pair_status')]
                    elif not fresh or not isinstance(snapshot,dict):
                        self._notify('['+market_label(market)+'] Выбор пары устарел или текущее состояние неизвестно.')
                    else:
                        target=sorted(parts[1:]);previous=snapshot.get('symbols');version=snapshot.get('version')
                        limit=1 if market in ('spot','futures') else 2
                        valid=(isinstance(previous,list) and 1<=len(previous)<=limit and
                               all(isinstance(s,str) and re.fullmatch(r'[A-Z0-9]{2,20}USDT',s) for s in previous) and
                               len(set(previous))==len(previous) and type(version) is int and version>=0 and
                               1<=len(target)<=limit and len(set(target))==len(target) and
                               all(re.fullmatch(r'[A-Z0-9]{2,20}USDT',s) and s!='USDTUSDT' for s in target))
                        if not valid:self._notify('['+market_label(market)+'] Некорректная пара или неизвестная версия.')
                        else:
                            token='P-'+secrets.token_hex(8)
                            self._save_pair_proposal({'id':token,'update_id':ident,'market':market,
                                'previous':sorted(previous),'version':version,'target':target,'expires':now+120,'status':'OPEN'})
                            self._notify('['+market_label(market)+'] Сменить '+', '.join(sorted(previous))+' на '+', '.join(target)+'? /confirm '+token+' или /cancel '+token+'.')
                elif command in ('/signal', '/history', '/report', '/pause', '/resume', '/timeframe', '/leverage', '/close'):
                    if not fresh and command in ('/resume', '/close', '/timeframe', '/leverage'):
                        self._notify('['+market_label(market)+'] Команда устарела.')
                    elif command == '/leverage' and market not in ('futures','futures_cross','margin_cross'):
                        self._notify('[SPOT] Плечо доступно только в futures paper.')
                    elif command == '/close' and market in ('futures_cross','margin_cross'):
                        snapshots=(positions or {}).get(market) or {}
                        if not isinstance(snapshots,dict):snapshots={}
                        symbol=parts[1] if len(parts)==2 else (next(iter(snapshots)) if len(parts)==1 and len(snapshots)==1 else None)
                        if not self._close_symbol_valid(symbol) or symbol not in snapshots:
                            self._notify('['+market_label(market)+'] Выберите существующую позицию: /close СИМВОЛ.')
                        else:
                            pos=snapshots[symbol]
                            if not isinstance(pos,dict) or not pos.get('id') or not pos.get('quantity'):
                                self._notify('['+market_label(market)+'] Снимок позиции неизвестен.')
                            else:
                                payload={'symbol':symbol,'position':pos['id'],'quantity':str(pos['quantity']),'received_at':now,'expires':now+120}
                                jobs=[(market,'/close')]
                    elif len(parts) > (2 if command in ('/timeframe', '/leverage') else 1):
                        self._notify('['+market_label(market)+'] Некорректные параметры.')
                    elif command == '/close' and (not positions or not isinstance(positions.get(market), dict) or not positions[market].get('id') or not positions[market].get('quantity')):
                        self._notify('['+market_label(market)+'] Закрытие недоступно: снимок позиции не подтверждён.')
                    else:
                        if command == '/close':
                            pos = positions[market]
                            symbol=pos.get('symbol','BTCUSDT')
                            if not self._close_symbol_valid(symbol):raise ValueError('Invalid position symbol')
                            payload = {'position': pos['id'], 'quantity': str(pos['quantity']), 'symbol':symbol, 'received_at': now, 'expires': now + 120}
                        if command in ('/resume', '/timeframe', '/leverage'):
                            payload = {'received_at': now, 'expires': now + 120}
                        jobs = [(market, ' '.join([command] + parts[1:]))]
                elif command in ('/loans','/interest','/repay'):
                    if market!='margin_cross' or len(parts) not in ({'/loans':(1,),'/interest':(1,),'/repay':(3,)}[command]) or (command=='/repay' and not fresh):
                        self._notify('[Spot margin cross • PAPER] Используйте /loans, /interest или /repay ID_займа количество в режиме margin_cross.')
                    else:
                        payload={'received_at':now,'expires':now+120}
                        jobs=[(market,' '.join([command]+parts[1:]))]
                elif command in ('/wallets','/wallet','/transfer','/transfers','/balance_report'):
                    lengths={'/wallets':(1,),'/wallet':(2,),'/transfer':(4,),'/transfers':(1,),'/balance_report':(1,2)}
                    if len(parts) not in lengths[command]:
                        self._notify('[Кошельки] Некорректные параметры команды.')
                    elif command=='/transfer' and not fresh:
                        self._notify('[Кошельки] Команда устарела; отправьте заново.')
                    else:
                        market='wallet';payload={'received_at':now,'expires':now+120} if command=='/transfer' else {}
                        jobs=[('wallet',' '.join([command]+parts[1:]))]
                elif command in ('/confirm','/cancel') and len(parts)==2 and parts[1].startswith('P-') and fresh and self.allow_pair_experiment:
                    proposal=self.pair_proposal(parts[1])
                    if not proposal or proposal['status']!='OPEN' or proposal['expires']<=now:
                        self._notify('Подтверждение пары не найдено или устарело.')
                    elif command=='/cancel':
                        proposal['status']='CANCELED';self._save_pair_proposal(proposal)
                        self._notify('['+market_label(proposal['market'])+'] Смена пары отменена.')
                    else:
                        market=proposal['market']
                        payload={'proposal':proposal['id'],'previous':proposal['previous'],
                                 'version':proposal['version'],'target':proposal['target'],
                                 'expires':proposal['expires'],'received_at':now}
                        proposal['status']='CONFIRMED';self._save_pair_proposal(proposal)
                        jobs=[(market,'/confirmed_pair')]
                elif command in ('/confirm','/cancel') and len(parts)==2 and parts[1].startswith('T-') and fresh:
                    market='wallet';payload={'received_at':now,'token':parts[1]}
                    jobs=[('wallet','/confirm_transfer' if command=='/confirm' else '/cancel_transfer')]
                elif command in ('/confirm','/cancel') and len(parts)==2 and parts[1].startswith('R-') and fresh:
                    market='margin_cross';payload={'received_at':now,'token':parts[1]}
                    jobs=[(market,'/confirm_repay' if command=='/confirm' else '/cancel_repay')]
                elif command in ('/confirm', '/cancel') and len(parts) == 2 and fresh:
                    proposal = self.db.execute('SELECT * FROM unified_proposals WHERE id=?', (parts[1],)).fetchone()
                    if proposal is None or proposal['status'] != 'OPEN' or proposal['expires'] <= now:
                        self._notify('Подтверждение не найдено или устарело.')
                    elif command == '/cancel':
                        self.db.execute("UPDATE unified_proposals SET status='CANCELED' WHERE id=?", (parts[1],))
                        self._notify('['+market_label(proposal['market'])+'] Предложение отменено.')
                    else:
                        market = proposal['market']
                        payload = {'proposal': proposal['id'], 'position': proposal['position'], 'quantity': proposal['quantity'], 'expires': proposal['expires'], 'received_at': now}
                        if market in ('futures_cross','margin_cross'):
                            symbol=proposal['id'].split('-',1)[0]
                            if not self._close_symbol_valid(symbol):raise ValueError('Invalid cross proposal identity')
                            payload['symbol']=symbol
                        self.db.execute("UPDATE unified_proposals SET status='CONFIRMED' WHERE id=?", (parts[1],))
                        jobs = [(market, '/confirmed_close')]
                else:
                    self._notify('Рынок: '+market_label(mode)+'. Выбор: /mode spot, /mode futures (isolated), /mode futures_cross, /mode margin_cross (paper). Команды выбранного рынка: /signal, /history, /report (сегодня по Баку и за всё время), /pause, /resume, /timeframe, /leverage (futures и margin), /close. Для margin: /loans, /interest, /repay ID_займа количество. /status — состояние; /confirm ID, /cancel ID, /pause_all. /wallets — краткие остатки, /wallet ИМЯ — доступность перевода, /balance_report [today|all] — подробный общий капитал; /transfer и /transfers — виртуальные переводы.')
            self.db.execute('INSERT INTO unified_updates VALUES(?,?,?,?)', (ident, fingerprint, command, market))
            for destination, action in jobs:
                self.db.execute('INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(?,?,?,?)', (ident, destination, action, json.dumps(payload, sort_keys=True)))
            self.db.execute("UPDATE unified_state SET value=? WHERE key='offset'", (str(ident+1),))

    def add_proposal(self, update_id, market, position, quantity, *, now=None, ttl=120):
        now = time.time() if now is None else now
        if market not in ('spot', 'futures','futures_cross','margin_cross') or not position or not quantity or ttl <= 0:
            raise ValueError('Invalid close proposal')
        with self.transaction():
            job = self.db.execute("SELECT * FROM unified_jobs WHERE update_id=? AND market=? AND command='/close'", (update_id, market)).fetchone()
            if not job:
                raise ValueError('Missing close request')
            if job['status'] == 'DONE':
                return job['result']
            payload=json.loads(job['payload'])
            if payload.get('position')!=position or payload.get('quantity')!=quantity:
                raise ValueError('Close proposal does not match accepted position')
            ttl=min(ttl,payload.get('expires',0)-now)
            if ttl<=0:raise ValueError('Close request expired')
            symbol=payload.get('symbol','BTCUSDT')
            if not self._close_symbol_valid(symbol):raise ValueError('Invalid position symbol')
            ident = (symbol+'-' if market in ('futures_cross','margin_cross') else '')+secrets.token_hex(6)
            self.db.execute('INSERT INTO unified_proposals VALUES(?,?,?,?,?,?)', (ident, market, position, quantity, now+ttl, 'OPEN'))
            self.db.execute("UPDATE unified_jobs SET status='DONE',result=? WHERE id=?", (ident, job['id']))
            asset=symbol[:-4] if symbol else 'BTC'
            self._notify('['+market_label(market)+'] Закрыть '+(symbol+' ' if symbol else '')+'позицию '+position+', объём '+display_amount(quantity,asset)+' '+asset+'? /confirm '+ident+' или /cancel '+ident+'.')
            return ident

    def pending(self, market):
        return self.db.execute("SELECT * FROM unified_jobs WHERE market=? AND status='PENDING' ORDER BY id", (market,)).fetchall()

    def acknowledge(self, job_id, result, *, applied=True, notify=True):
        with self.transaction():
            row = self.db.execute('SELECT * FROM unified_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise ValueError('Unknown job')
            if row['status'] == 'DONE':
                return
            self.db.execute("UPDATE unified_jobs SET status=?,result=? WHERE id=?", ('DONE' if applied else 'FAILED', result, job_id))
            label='Кошельки' if row['market']=='wallet' else market_label(row['market'])
            if notify:self._notify('['+label+'] '+result)
            update = self.db.execute('SELECT command FROM unified_updates WHERE id=?', (row['update_id'],)).fetchone()
            if update['command'] == '/pause_all':
                waiting = [r[0] for r in self.db.execute("SELECT market FROM unified_jobs WHERE update_id=? AND status!='DONE'", (row['update_id'],))]
                self._notify('Пауза рынков: ожидается '+', '.join(waiting) if waiting else 'Команды паузы применены ко всем четырём рынкам; текущее состояние: /status.')
