"""Idempotent offline application of margin paper control commands."""
import hashlib
import json
import time

from .futures_data import INTERVALS


class MarginCommandReceiver:
    def __init__(self,book,*,hold_resume=False):
        self.book=book;self.db=book.db;self.ledger=book.ledger;self.hold_resume=hold_resume;self.protection_checked_ms=None
        with self.ledger.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_commands(update_id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')

    def apply(self,update_id,command,payload,*,clock_ms=None):
        if type(update_id) is not int or update_id<0 or not isinstance(command,str) or not isinstance(payload,dict):
            raise ValueError('Invalid margin command')
        fingerprint=hashlib.sha256(json.dumps([command,payload],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.ledger.transaction():
            old=self.db.execute('SELECT fingerprint,result FROM margin_commands WHERE update_id=?',(update_id,)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise ValueError('Margin command identity conflict')
                return old['result']
            now_ms=(clock_ms or (lambda:int(time.time()*1000)))()
            parts=command.split();name=parts[0] if parts else ''
            if name=='/pause' and len(parts)==1:
                self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
                result='Новые входы на паузе; контроль позиций продолжается.'
            else:
                self.book._check()
                if payload.get('expires') is not None and now_ms>=payload['expires']*1000:
                    result='Команда устарела; отправьте заново.'
                elif payload.get('expected_setting') is not None and (name not in ('/timeframe','/leverage') or
                    self.db.execute('SELECT '+('leverage' if name=='/leverage' else 'interval')+' FROM margin_settings WHERE id=1').fetchone()[0]!=payload['expected_setting']):
                    result='Настройка изменилась; запросите новое меню.'
                elif name=='/resume' and len(parts)==1:
                    try:
                        hold=self.db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()
                        error=self.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
                        if (self.hold_resume or self.protection_checked_ms is None or
                            not 0<=now_ms-self.protection_checked_ms<=120000 or
                            (hold and hold[0]=='true') or not error or json.loads(error[0]) is not None):
                            raise ValueError('Margin requires recovery')
                        if self.db.execute("SELECT 1 FROM margin_loans l WHERE l.status='OPEN' AND NOT EXISTS (SELECT 1 FROM margin_positions p WHERE p.id=l.position_id)").fetchone():
                            raise ValueError('Unattached liability')
                        marks=self.book._current_marks(self.book.stored_quotes(),now_ms)
                        self.book._accrue_all(now_ms);self.book._check()
                        risk=self.book._risk(marks)
                        if risk['equity']<=risk['maintenance']:raise ValueError('Insufficient collateral')
                    except ValueError:
                        self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
                        result='Входы остаются на паузе: данные или учёт требуют проверки.'
                    else:
                        self.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
                        result='Paper-входы разрешены.'
                elif name in ('/timeframe','/leverage') and len(parts) in (1,2):
                    settings=self.db.execute('SELECT leverage,interval FROM margin_settings WHERE id=1').fetchone()
                    if len(parts)==1:result=name+': '+str(settings['leverage'] if name=='/leverage' else settings['interval'])
                    elif not self.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0] or self.book._positions():
                        result='Сначала поставьте margin на паузу и закройте позиции.'
                    else:
                        raw=parts[1];valid=(raw.isdigit() and 1<=int(raw)<=3) if name=='/leverage' else raw in INTERVALS
                        if not valid:result='Некорректное значение настройки.'
                        else:
                            column='leverage' if name=='/leverage' else 'interval'
                            self.db.execute('UPDATE margin_settings SET '+column+'=? WHERE id=1',(int(raw) if name=='/leverage' else raw,))
                            result='Настройка сохранена; входы остаются на паузе.'
                else:result='Команда margin не поддерживается.'
            self.db.execute('INSERT INTO margin_commands VALUES(?,?,?)',(update_id,fingerprint,result))
            return result
