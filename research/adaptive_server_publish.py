"""Fixed-path, read-only result publication; no trading or API access."""
import grp
import json
import os
from pathlib import Path
import tempfile
import time

from trading_bot.ai_view import render

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


if __name__ == '__main__':
    try:
        print('AI_REPORT_PUBLISHED' if publish() else 'AI_REPORT_UNCHANGED')
    except Exception:
        raise SystemExit('AI_REPORT_NOT_PUBLISHED') from None
