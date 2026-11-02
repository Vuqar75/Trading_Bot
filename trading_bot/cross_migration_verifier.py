"""Compare coordinated financial snapshots at the same saved market prices.

Only temporary copies are modified. Any intervening trade, funding change or
strategy-parameter change is conservatively rejected for manual reconciliation.
"""
import json
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from .paper_shared_store import SharedPaperStore
from .paper_wallet_live_report import live_report


def _copy(source,target):
    path=Path(source)
    if path.is_symlink() or not path.is_file():raise ValueError('Invalid snapshot')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or dst.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Snapshot integrity failure')


def _position(value):
    return {k:v for k,v in value.items() if k!='funding_cursor'} if isinstance(value,dict) else value


def _parameters(shared):
    return {'spot_identity':shared._get('spot','identity'),
            'spot_interval':shared._get('spot','selected_interval'),
            'futures_identity':shared._get('futures','identity'),
            'futures_interval':shared._get('futures','interval'),
            'futures_leverage':shared._get('futures','leverage')}


def verify_same_price_migration(before,after,*,scratch_parent):
    """Return equal before/after totals or fail closed without mutating inputs."""
    if Path(before).resolve(strict=True)==Path(after).resolve(strict=True):raise ValueError('Distinct snapshots required')
    parent=Path(scratch_parent).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='cross-price-check-',dir=parent) as temp:
        root=Path(temp);old_path=root/'before.db';new_path=root/'after.db'
        _copy(before,old_path);_copy(after,new_path)
        with closing(SharedPaperStore(old_path)) as old,closing(SharedPaperStore(new_path)) as new:
            old_states={market:old.snapshot(market) for market in ('spot','futures')}
            new_states={market:new.snapshot(market) for market in ('spot','futures')}
            quotes={market:old._get(market,'quote') for market in ('spot','futures')}
            if any(not isinstance(quotes[m],dict) or not quotes[m].get('time') for m in quotes):raise ValueError('Source quotes unavailable')
            when=max(quotes[m]['time'] for m in quotes)
            if _parameters(old)!=_parameters(new):raise ValueError('Strategy parameters changed')
            for market in ('spot','futures'):
                if _position(old_states[market]['position'])!=_position(new_states[market]['position']) or old_states[market]['pnl']!=new_states[market]['pnl']:
                    raise ValueError('Position, funding or realized PnL changed')
                expected=Decimal(old_states[market]['cash'])-(Decimal(50) if market=='futures' else Decimal(0))
                if Decimal(new_states[market]['cash'])!=expected:raise ValueError('Cash delta disagrees with 50 USDT transfer')
                if old_states[market]['paused'] is not True or new_states[market]['paused'] is not True:
                    raise ValueError('Both snapshots must have paused entries')
            if not old.deployment_hold() or not new.deployment_hold():raise ValueError('Deployment hold is required in both snapshots')
            old_events={row['id']:row['fingerprint'] for row in old.db.execute('SELECT id,fingerprint FROM ledger_events')}
            new_events={row['id']:row['fingerprint'] for row in new.db.execute('SELECT id,fingerprint FROM ledger_events')}
            if any(new_events.get(ident)!=fingerprint for ident,fingerprint in old_events.items()):raise ValueError('Original financial history changed')
            added=set(new_events)-set(old_events);transfers=[]
            for ident in added:
                event=new.db.execute('SELECT kind,details FROM ledger_events WHERE id=?',(ident,)).fetchone()
                if event['kind']=='TRANSFER':
                    details=json.loads(event['details'])
                    posts=[(row['account'],row['asset'],Decimal(row['amount'])) for row in new.db.execute('SELECT account,asset,amount FROM ledger_postings WHERE event_id=? ORDER BY ordinal',(ident,))]
                    if details.get('source')!='futures_isolated' or details.get('destination')!='futures_cross' or Decimal(details.get('quantity','0'))!=50 or posts!=[('futures_isolated:free','USDT',Decimal(-50)),('futures_cross:free','USDT',Decimal(50))]:
                        raise ValueError('Unexpected transfer during migration')
                    transfers.append(ident)
                elif event['kind']!='FINANCIAL_METADATA':raise ValueError('Unexpected financial event during migration')
            if len(transfers)!=1:raise ValueError('Exactly one initial transfer required')
            if old.ledger.balance('futures_cross:free')!=0 or new.ledger.balance('futures_cross:free')!=50:
                raise ValueError('Cross allocation disagrees')
            accounts=set()
            for shared in (old,new):
                accounts.update((row['account'],row['asset']) for row in shared.db.execute("SELECT DISTINCT account,asset FROM ledger_postings WHERE account LIKE 'reserve:%' OR account LIKE 'spot:%' OR account LIKE 'futures_isolated:%' OR account LIKE 'futures_cross:%'"))
            for account,asset in accounts:
                if account.startswith('futures_cross:') and (asset!='USDT' or account not in ('futures_cross:free','futures_cross:reserved','futures_cross:margin','futures_cross:funding','futures_cross:liability')):
                    if old.ledger.balance(account,asset)!=0 or new.ledger.balance(account,asset)!=0:raise ValueError('Unexpected cross asset or account')
                if (account,asset) not in (('futures_isolated:free','USDT'),('futures_cross:free','USDT')) and old.ledger.balance(account,asset)!=new.ledger.balance(account,asset):
                    raise ValueError('Unrelated wallet balance or obligation changed')
            for bucket in ('reserved','margin','funding','liability'):
                if old.ledger.balance('futures_cross:'+bucket)!=0 or new.ledger.balance('futures_cross:'+bucket)!=0:
                    raise ValueError('Unexpected cross obligations or exposure')
            if old.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_positions'").fetchone() and old.db.execute('SELECT 1 FROM cross_positions LIMIT 1').fetchone():
                raise ValueError('Unexpected source cross position')
            if not new.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_positions'").fetchone() or new.db.execute('SELECT 1 FROM cross_positions LIMIT 1').fetchone():
                raise ValueError('Unexpected new cross position')
            settings=new.db.execute('SELECT leverage,interval FROM cross_settings WHERE id=1').fetchone()
            if not settings or tuple(settings)!=(1,'15m'):raise ValueError('Unexpected cross parameters')
            before_report=live_report(old,now=when)
            with new.transaction():
                for market in quotes:new._set(market,'quote',quotes[market])
            after_report=live_report(new,now=when)
            if before_report['total'] is None or after_report['total'] is None or before_report['total']!=after_report['total']:
                raise ValueError('Capital differs at fixed prices')
            return {'capital_before':str(before_report['total']),'capital_after':str(after_report['total']),
                    'spot_price':quotes['spot']['price'],'futures_price':quotes['futures']['price'],
                    'spot_position':old_states['spot']['position'],'futures_position':old_states['futures']['position'],
                    'spot_pnl':old_states['spot']['pnl'],'futures_pnl':old_states['futures']['pnl']}
