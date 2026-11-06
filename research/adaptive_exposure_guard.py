"""Research-only read check; caller must own transaction across attached DBs.

This primitive does not authorize or submit orders. The caller must also
validate freshness and decision/strategy eligibility under the same locks.
"""
import json,sqlite3
from decimal import Decimal

def require_clear(financial):
 if not isinstance(financial,sqlite3.Connection) or not financial.in_transaction:raise ValueError('financial_transaction_required')
 attached={row[1] for row in financial.execute('PRAGMA database_list')}
 if 'route_queue' not in attached:raise ValueError('attached_queue_required')
 def meta(table,key,*,missing_ok=False):
  row=financial.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
  if not row:
   if missing_ok:return None
   raise ValueError('state_missing')
  return json.loads(row[0])
 if not isinstance(meta('futures_meta','identity'),dict):raise ValueError('state_missing')
 spot=meta('runtime_meta','position');futures=meta('futures_meta','position',missing_ok=True)
 if not isinstance(spot,dict) or Decimal(str(spot['quantity']))!=0 or futures is not None:raise ValueError('position_exists')
 if financial.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():raise ValueError('pending_spot_order')
 for table in ('cross_positions','margin_positions'):
  if financial.execute('SELECT 1 FROM '+table+" WHERE symbol='BTCUSDT' LIMIT 1").fetchone():raise ValueError('position_exists')
 if financial.execute("SELECT 1 FROM margin_loans WHERE status='OPEN' LIMIT 1").fetchone():raise ValueError('margin_debt_exists')
 balances={}
 for account,asset,amount in financial.execute("SELECT account,asset,amount FROM ledger_postings WHERE account LIKE '%:reserved'"):
  key=(account,asset);balances[key]=balances.get(key,Decimal(0))+Decimal(amount)
 if any(value!=0 for value in balances.values()):raise ValueError('financial_reserve_exists')
 if financial.execute("SELECT 1 FROM route_queue.unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ValueError('queued_action_exists')
 if financial.execute("SELECT 1 FROM route_queue.unified_proposals WHERE status='OPEN' LIMIT 1").fetchone():raise ValueError('confirmation_pending')
 for (value,) in financial.execute("SELECT value FROM route_queue.unified_state WHERE key LIKE 'pair-proposal:%'"):
  if json.loads(value).get('status')=='OPEN':raise ValueError('confirmation_pending')
 return True
