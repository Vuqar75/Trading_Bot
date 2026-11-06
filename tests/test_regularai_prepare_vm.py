import importlib.util
import subprocess
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('pair_prepare_vm',Path(__file__).resolve().parents[1]/'tools/regularai_prepare_vm.py')
prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)

class HealthdiagPrepareTrustTests(unittest.TestCase):
    def test_source_checkout_matches_installed_commit(self):
        self.assertEqual(prepare.OLD.name,'trading-bot-regularai-'+prepare.INSTALLED[:7])

    def test_git_trust_is_exact_idempotent_and_checked_as_service_user(self):
        target='/opt/trading-bot-pair-abcdef1';candidate='a'*40
        for already in (False,True):
            calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if args[1:3]==['config','--system'] and '--get-all' in args:return subprocess.CompletedProcess(args,0,target+'\n' if already else '/opt/other\n','')
                if args[1:3]==['config','--system'] and '--add' in args:return subprocess.CompletedProcess(args,0,'','')
                return subprocess.CompletedProcess(args,0,candidate+'\n','')
            prepare.ensure_service_git_access(target,candidate,run=run)
            adds=[a for a in calls if '--add' in a]
            self.assertEqual(len(adds),0 if already else 1)
            if adds:self.assertEqual(adds[0][-1],target)
            self.assertEqual(calls[-1][:3],['runuser','-u','tradingbot'])

    def test_git_candidate_mismatch_prevents_change(self):
        calls=[]
        def run(args,**kwargs):calls.append(args);return subprocess.CompletedProcess(args,0,'wrong\n','')
        with self.assertRaisesRegex(ValueError,'precheck'):
            prepare.ensure_service_git_access('/opt/trading-bot-pair-abcdef1','a'*40,run=run)
        self.assertEqual(len(calls),1)
