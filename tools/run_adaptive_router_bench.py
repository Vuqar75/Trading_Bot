"""Run the offline adaptive routing checks with isolated home and no IP sockets."""
import os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MODULES=('tests.test_adaptive_router_comparison','tests.test_adaptive_atomic_spot','tests.test_adaptive_atomic_futures',
         'tests.test_adaptive_two_sided_futures','tests.test_usdm_v2_atomic_study','tests.test_adaptive_view','tests.test_inline_telegram',
         'tests.test_adaptive_experiment_fence','tests.test_adaptive_experiment_init','tests.test_adaptive_experiment_cycle','tests.test_adaptive_experiment_recovery','tests.test_adaptive_snapshot_proof.SnapshotProofTest','tests.test_adaptive_funding_evidence','tests.test_adaptive_funding_tracker','tests.test_adaptive_market_proof','tests.test_adaptive_market_history','tests.test_adaptive_public_feed','tests.test_adaptive_decision_export','tests.test_adaptive_study_publisher','tests.test_adaptive_study_supervisor','tests.test_adaptive_study_backup','tests.test_adaptive_study_status','tests.test_adaptive_study_observations','tests.test_adaptive_regular',
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
  if sys.argv[1:]==['--isolation-probe']:
   env['FOUR_MARKET_OFFLINE_BENCH']='1'
   args=['tests.test_four_market_shadow_gate.ShadowTransitions.test_bench_blocks_ip_and_does_not_inherit_credentials']
  elif sys.argv[1:]==['--full']:args=['discover','-s','tests']
  elif not sys.argv[1:]:args=list(MODULES)
  else:raise SystemExit('Usage: run_adaptive_router_bench.py [--full|--isolation-probe]')
  return subprocess.run([sys.executable,'-B','-m','unittest',*args],cwd=ROOT,env=env).returncode
if __name__=='__main__':raise SystemExit(main())
