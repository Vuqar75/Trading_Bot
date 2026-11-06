"""Run the offline adaptive routing checks with isolated home and no IP sockets."""
import os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MODULES=('tests.test_adaptive_router_comparison','tests.test_adaptive_atomic_spot','tests.test_adaptive_atomic_futures',
         'tests.test_adaptive_exposure_guard','tests.test_adaptive_route_policy','tests.test_adaptive_route_state','tests.test_adaptive_router_transition',
         'tests.test_runtime.RuntimeTests.test_process_crash_during_fill_commit','tests.test_runtime.RuntimeTests.test_process_crash_before_and_after_commit',
         'tests.test_isolated_futures.IsolatedTests.test_wallet_operations_atomic_on_crash')
SITECUSTOMIZE='''import socket\n_original=socket.socket\nclass OfflineSocket(_original):\n def __init__(self,family=socket.AF_INET,*args,**kwargs):\n  if family in (socket.AF_INET,socket.AF_INET6): raise OSError("offline bench: IP sockets forbidden")\n  super().__init__(family,*args,**kwargs)\nsocket.socket=OfflineSocket\n'''
def main():
 if os.name!='posix':raise SystemExit('Linux is required for SQLite/process semantics.')
 with tempfile.TemporaryDirectory(prefix='adaptive-router-bench-') as tmp:
  Path(tmp,'sitecustomize.py').write_text(SITECUSTOMIZE)
  env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':tmp,'TMPDIR':tmp,'LANG':'C.UTF-8',
       'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':tmp+os.pathsep+str(ROOT)}
  args=['discover','-s','tests'] if sys.argv[1:]==['--full'] else list(MODULES)
  if sys.argv[1:] and sys.argv[1:]!=['--full']:raise SystemExit('Usage: run_adaptive_router_bench.py [--full]')
  return subprocess.run([sys.executable,'-B','-m','unittest',*args],cwd=ROOT,env=env).returncode
if __name__=='__main__':raise SystemExit(main())
