"""Initialize one research experiment exactly once in a temporary-root sandbox.

An incomplete creation is left for inspection and fails closed. This helper
does not start a process, enable a market or authorize a trading strategy.
"""
import json,os,sqlite3,tempfile
from decimal import Decimal
from pathlib import Path
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.unified_queue import UnifiedQueue
from .adaptive_experiment_fence import install
from .adaptive_route_state import RouteState

def initialize(path,*,source,start_ms,fault=lambda point:None):
 path=Path(path);base=Path(tempfile.gettempdir()).resolve()
 if source not in ('AI','baseline') or type(start_ms) is not int or start_ms<0:raise ValueError('invalid_experiment_identity')
 if path.is_symlink() or not path.resolve().is_relative_to(base) or not path.parent.is_dir():raise ValueError('sandbox_path_required')
 try:path.mkdir(mode=0o700)
 except FileExistsError:
  manifest=path/'manifest.json'
  if manifest.is_symlink() or not manifest.is_file():raise ValueError('incomplete_initialization_requires_review')
  info=json.loads(manifest.read_text())
  if info.get('source')!=source or info.get('schema')!=1:raise ValueError('experiment_identity_conflict')
  uri=(path/'financial.db').resolve().as_uri()+'?mode=ro'
  db=sqlite3.connect(uri,uri=True)
  try:
   if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('financial_integrity_failed')
   names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
   if not {'adaptive_spot_fence','adaptive_spot_consume','adaptive_futures_fence','adaptive_futures_consume','adaptive_cross_positions_fence','adaptive_margin_positions_fence'}<=names:raise ValueError('fence_missing')
  finally:db.close()
  for name in ('queue.db','route.db'):
   if (path/name).is_symlink() or not (path/name).is_file():raise ValueError('experiment_state_missing')
  return {**info,'existing':True}
 try:
  with tempfile.TemporaryDirectory(prefix='initial-',dir=path) as temp:
   spot=Path(temp)/'spot';futures=Path(temp)/'futures'
   a=RuntimeStore(spot,{'mode':'paper','symbol':'BTCUSDT'});b=FuturesStore(futures,fee_bps='10',slippage_bps='5')
   try:
    with a.transaction():a.set('cash','500')
    with b.transaction():b.set('cash','500')
   finally:a.close();b.close()
   consolidate_snapshots(spot,futures,path/'financial.db',captured_at=start_ms/1000)
  fault('after_financial')
  shared=SharedPaperStore(path/'financial.db')
  try:
   CrossPaperBook(shared);MarginPaperBook(shared.ledger);install(shared.db)
   if shared.ledger.balance('spot:free','USDT')!=Decimal('500') or shared.ledger.balance('futures_isolated:free','USDT')!=Decimal('500') or not shared.ledger.reconcile()['balanced']:raise ValueError('initial_capital_invalid')
  finally:shared.close()
  # The isolated study begins active; no working-market pause is touched.
  # This occurs only during fresh initialization, never on a restart.
  spot_store=RuntimeStore(path/'financial.db',{'mode':'paper','symbol':'BTCUSDT'},shared=True)
  futures_store=FuturesStore(path/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
  try:
   spot_store.command(1,True,'/resume');futures_store.command(1,True,'/resume')
  finally:spot_store.close();futures_store.close()
  q=UnifiedQueue(path/'queue.db');q.close()
  route=RouteState(path/'route.db');route.close()
  fault('before_manifest')
  info={'schema':1,'source':source,'start_ms':start_ms,'total_usdt':'1000','spot_initial_usdt':'500','isolated_initial_usdt':'500'}
  with open(path/'manifest.json','x',encoding='utf-8') as stream:
   json.dump(info,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
  descriptor=os.open(path,os.O_RDONLY)
  try:os.fsync(descriptor)
  finally:os.close(descriptor)
  return {**info,'existing':False}
 except BaseException:
  # Never silently overwrite or re-seed an interrupted initialization.
  raise
