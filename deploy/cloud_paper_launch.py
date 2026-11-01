"""Paper-only Cloud Shell launcher. Reads the separately protected legacy login."""
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

os.umask(0o077)
home = Path.home()
config = home / '.config/trading-bot/telegram.json'
if not config.is_file() or stat.S_IMODE(config.stat().st_mode) & 0o077:
    raise SystemExit('Protected Telegram configuration missing or insecure')
try:
    login = json.loads(config.read_text())
    token, owner = login['token'], login['owner']
    if not isinstance(token, str) or not token or not isinstance(owner, str) or not owner.isdigit():
        raise ValueError()
except (OSError, ValueError, TypeError, KeyError):
    raise SystemExit('Protected Telegram configuration invalid') from None
# Prevent the legacy launcher from restarting against its original SQLite DB.
legacy_lock = open(home / 'Trading_Bot/data/cloud-paper.db.lock', 'a')
try:
    fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('Legacy bot lock is held') from None
os.set_inheritable(legacy_lock.fileno(), True)
os.environ['RUNTIME_TELEGRAM_BOT_TOKEN'] = token
os.environ['RUNTIME_TELEGRAM_OWNER_ID'] = owner
shared_db = os.getenv('SHARED_PAPER_DB')
arguments = [sys.executable, '-B', '-m', 'trading_bot.app',
    '--mode', 'paper', '--interval', '15m', '--amount', '10', '--telegram',
    '--allow-paper-signal', '--db', shared_db or str(home / 'Trading_Bot_paper_state/runtime-paper.db')]
if shared_db:
    arguments += ['--shared-paper']
if os.getenv('FUTURES_OUTBOX_DB'):
    arguments += ['--futures-outbox-db', os.environ['FUTURES_OUTBOX_DB']]
os.execv(sys.executable, arguments)
