"""Offline test entry point. Uses isolated temporary homes and blocks IP sockets."""
import os,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULES=('tests.test_runtime','tests.test_fill_ledger','tests.test_spot_donchian',
 'tests.test_spot_donchian_app','tests.test_isolated_futures','tests.test_futures',
 'tests.test_cross_paper_book','tests.test_pair_cross_offline','tests.test_margin_paper_book',
 'tests.test_margin_interest_ledger','tests.test_paper_wallet_commands','tests.test_pair_selection_offline',
 'tests.test_pair_migration_offline','tests.test_paper_pair_migration','tests.test_pair_queue_offline',
 'tests.test_paper_coordinated_backup','tests.test_inline_telegram','tests.test_paper_wallet_ledger',
 'tests.test_adaptive_shadow','tests.test_four_market_shadow_gate','tests.test_four_market_arithmetic')
SITECUSTOMIZE='''import socket\n_original=socket.socket\nclass OfflineSocket(_original):\n def __init__(self,family=socket.AF_INET,*args,**kwargs):\n  if family in (socket.AF_INET,socket.AF_INET6): raise OSError("offline bench: IP sockets forbidden")\n  super().__init__(family,*args,**kwargs)\nsocket.socket=OfflineSocket\n'''

def main():
 if os.name!='posix':raise SystemExit('Use Linux: process and SQLite locking tests require POSIX.')
 with tempfile.TemporaryDirectory(prefix='tradingbot-four-market-') as tmp:
  Path(tmp,'sitecustomize.py').write_text(SITECUSTOMIZE)
  env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':tmp,'TMPDIR':tmp,
       'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','FOUR_MARKET_OFFLINE_BENCH':'1','PYTHONPATH':tmp+os.pathsep+str(ROOT)}
  result=subprocess.run([sys.executable,'-B','-m','unittest',*MODULES],cwd=ROOT,env=env)
  return result.returncode
if __name__=='__main__':raise SystemExit(main())
