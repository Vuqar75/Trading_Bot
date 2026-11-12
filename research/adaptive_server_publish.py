"""Fixed-path, read-only result publication; no trading or API access."""
import grp
import json
import os
from pathlib import Path
import tempfile
import time

from trading_bot.ai_view import render
from research.technical_assets import SYMBOLS,sidecar,symbol as asset_symbol,history_matches

SOURCE = Path('/var/lib/tradingbot-ai-executor-public/report.json')
TARGET = Path('/var/lib/tradingbot-ai-public/report.json')


def publish(source=SOURCE, target=TARGET, *, now=None, gid=None):
    source, target = Path(source), Path(target)
    now = time.time() if now is None else now
    if source.is_symlink() or source.parent.is_symlink() or target.is_symlink() or target.parent.is_symlink():
        raise ValueError('symlink_report_rejected')
    fd = os.open(source, os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f: raw = f.read(100001)
    if len(raw)>100000: raise ValueError('report_too_large')
    d = json.loads(raw)
    diagnostic=d.get('diagnostic_only') is True and d.get('complete') is False
    if not diagnostic and d.get('complete') is not True:raise ValueError('confirmed_comparison_required')
    # Validate this exact byte snapshot, not another read of mutable source.
    if target.exists() and target.read_bytes() == raw: return False
    parent = target.parent
    fd, tmp = tempfile.mkstemp(prefix='.ai-server-report-', dir=parent)
    tmp = Path(tmp)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        text = render(tmp, now=now)
        if 'УСТАРЕЛО' in text or (diagnostic and ('Состояние запроса:' not in text or 'Вывод модели:' in text)) or (not diagnostic and ('Вывод модели:' not in text or 'baseline того же среза:' not in text or 'НЕПОЛНЫЙ' in text)):
            raise ValueError('comparison_not_current_or_invalid')
        if target.exists():
            old = json.loads(target.read_bytes())
            from datetime import datetime
            if datetime.fromisoformat(d['data_utc']) <= datetime.fromisoformat(old['data_utc']):
                raise ValueError('report_not_newer')
        if gid is None: gid = grp.getgrnam('tradingbot').gr_gid
        os.chmod(tmp, 0o640)
        os.chown(tmp, os.geteuid(), gid)
        os.replace(tmp, target)
        fd = os.open(parent, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)
    return True



def publish_technical(source=None,target=None,*,now=None,gid=None,symbol='BTCUSDT'):
    """Publish an exact validated technical sidecar without reading budget."""
    from trading_bot.technical_ai_view import render as technical_render
    symbol=asset_symbol(symbol)
    source=Path(source) if source is not None else sidecar(SOURCE.parent,symbol)
    target=Path(target) if target is not None else sidecar(TARGET.parent,symbol)
    now=time.time() if now is None else now
    if any(p.is_symlink() for p in (source,source.parent,target,target.parent)):raise ValueError('symlink_report_rejected')
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:raw=f.read(30001)
    if len(raw)>30000:raise ValueError('technical_report_too_large')
    d=json.loads(raw)
    if d.get('symbol')!=symbol:raise ValueError('technical_report_market')
    if target.exists() and target.read_bytes()==raw:return False
    fd,tmp=tempfile.mkstemp(prefix='.technical-',dir=target.parent);tmp=Path(tmp)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        text=technical_render(tmp,now)
        if 'УСТАРЕЛО' in text or 'проверенного снимка нет' in text:raise ValueError('technical_report_invalid_or_stale')
        if target.exists():
            old=json.loads(target.read_bytes())
            if old.get('symbol')!=symbol:raise ValueError('technical_report_market')
            if (d['signal_ms'],d['as_of_ms'])<=(old['signal_ms'],old['as_of_ms']):raise ValueError('technical_report_not_newer')
        if gid is None:gid=grp.getgrnam('tradingbot').gr_gid
        os.chmod(tmp,0o640);os.chown(tmp,os.geteuid(),gid);os.replace(tmp,target)
        fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:tmp.unlink(missing_ok=True)
    return True


def publish_history(source=None,target=None,*,now=None,gid=None,symbol='BTCUSDT'):
    """Read-only price-level observations; separate from the AI budget/report."""
    from research.technical_plan_history import validate,MAX_BYTES
    symbol=asset_symbol(symbol)
    source=Path(source) if source is not None else sidecar(SOURCE.parent,symbol,'technical-history')
    target=Path(target) if target is not None else sidecar(TARGET.parent,symbol,'technical-history')
    now=time.time() if now is None else now
    if any(p.is_symlink() for p in (source,source.parent,target,target.parent)):raise ValueError('symlink_report_rejected')
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:raw=f.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('history_size')
    d=history_matches(validate(json.loads(raw)),symbol)
    if not -5000<=now*1000-d['checked_ms']<=1800000:raise ValueError('history_stale')
    if target.exists():
        old_raw=target.read_bytes()
        if old_raw==raw:return False
        old=history_matches(validate(json.loads(old_raw)),symbol)
        if old['started_ms']!=d['started_ms'] or old['checked_ms']>=d['checked_ms']:raise ValueError('history_reordered_or_reset')
    fd,tmp=tempfile.mkstemp(prefix='.technical-history-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        if gid is None:gid=grp.getgrnam('tradingbot').gr_gid
        os.chmod(tmp,0o640);os.chown(tmp,os.geteuid(),gid);os.replace(tmp,target)
        fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return True


if __name__ == '__main__':
    good=False;changed=False
    jobs=[(publish,{})]+[(function,{'symbol':symbol}) for symbol in SYMBOLS for function in (publish_technical,publish_history)]
    for function,kwargs in jobs:
        try:changed=function(**kwargs) or changed;good=True
        except (OSError,ValueError,KeyError,TypeError):pass
    if not good:raise SystemExit('AI_REPORT_NOT_PUBLISHED')
    print('AI_REPORT_PUBLISHED' if changed else 'AI_REPORT_UNCHANGED')
