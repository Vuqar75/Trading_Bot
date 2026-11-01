"""Offline exactly-once application of cross pause/settings commands."""
import hashlib
import json
import time
from .futures_data import INTERVALS


class CrossCommandReceiver:
    def __init__(self,book):
        self.book=book;self.shared=book.shared;self.db=book.db
        with self.shared.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_commands(update_id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')

    def apply(self, update_id, command, payload, *, now=None):
        if type(update_id) is not int or update_id<0 or not isinstance(command,str) or not isinstance(payload,dict):
            raise ValueError('Invalid cross command')
        now=time.time() if now is None else now
        fingerprint=hashlib.sha256(json.dumps([command,payload],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.shared.transaction():
            self.book._check()
            old=self.db.execute('SELECT fingerprint,result FROM cross_commands WHERE update_id=?',(update_id,)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise ValueError('Cross command identity conflict')
                return old['result']
            parts=command.split();name=parts[0] if parts else ''
            if name=='/pause' and len(parts)==1:
                self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
                result='Новые входы на паузе; контроль открытых позиций продолжается.'
            elif name=='/resume' and len(parts)==1:
                try:
                    if self.book.shared.deployment_hold() or self.book.runtime_error() or self.book.ledger.balance('futures_cross:liability')<0:
                        raise ValueError('Cross is blocked')
                    self.book._require_coverage(now)
                    marks=self.book._marks(self.book.stored_quotes(),now)
                    if self.book._risk(marks)['buffer']<=0:raise ValueError('Insufficient buffer')
                except ValueError:
                    self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
                    result='Входы остаются на паузе: данные, funding или обеспечение требуют проверки.'
                else:
                    self.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='futures_cross'")
                    result='Paper-входы разрешены.'
            elif name in ('/leverage','/timeframe') and len(parts) in (1,2):
                settings=self.db.execute('SELECT leverage,interval FROM cross_settings WHERE id=1').fetchone()
                current=settings['leverage'] if name=='/leverage' else settings['interval']
                if len(parts)==1:result=name+': '+str(current)
                else:
                    raw=parts[1]
                    valid=(raw.isdigit() and 1<=int(raw)<=10) if name=='/leverage' else raw in INTERVALS
                    if not valid:result='Некорректное значение настройки.'
                    elif not self.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0] or self.book._positions():
                        result='Сначала поставьте cross на паузу и закройте обе позиции.'
                    else:
                        column='leverage' if name=='/leverage' else 'interval'
                        self.db.execute('UPDATE cross_settings SET '+column+'=? WHERE id=1',(int(raw) if name=='/leverage' else raw,))
                        result='Настройка сохранена; входы остаются на паузе.'
            else:result='Команда cross не поддерживается.'
            self.db.execute('INSERT INTO cross_commands VALUES(?,?,?)',(update_id,fingerprint,result))
            return result
