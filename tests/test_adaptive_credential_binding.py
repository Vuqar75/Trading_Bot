import contextlib,ctypes,importlib.util,io,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
spec=importlib.util.spec_from_file_location('credential_binding',Path(__file__).resolve().parents[1]/'tools/bind_adaptive_windows_credential.py');binding=importlib.util.module_from_spec(spec);spec.loader.exec_module(binding)
class Fn:
 def __init__(self,fn):self.fn=fn
 def __call__(self,*a):return self.fn(*a)
class BindingTests(unittest.TestCase):
 def test_secret_never_printed_or_put_in_arguments(self):
  calls=[]
  class Fake:
   CredReadW=Fn(lambda *a:False)
   CredFree=Fn(lambda *a:None)
   CredWriteW=Fn(lambda *a:calls.append('write') or True)
  output=io.StringIO()
  with patch.object(binding.ctypes,'WinDLL',return_value=Fake(),create=True),patch.object(binding.ctypes,'get_last_error',return_value=1168,create=True),patch.object(binding.getpass,'getpass',return_value='fake-secret'),patch.object(sys.stdin,'isatty',return_value=True),contextlib.redirect_stdout(output):binding.main()
  self.assertEqual(calls,['write']);self.assertEqual(output.getvalue(),'CREDENTIAL_SAVED\n');self.assertNotIn('fake-secret',output.getvalue())
 def test_existing_credential_is_not_overwritten(self):
  calls=[]
  class Fake:
   CredReadW=Fn(lambda *a:True)
   CredFree=Fn(lambda *a:None)
   CredWriteW=Fn(lambda *a:calls.append('write') or True)
  with patch.object(binding.ctypes,'WinDLL',return_value=Fake(),create=True),patch.object(sys.stdin,'isatty',return_value=True):
   with self.assertRaisesRegex(SystemExit,'not_overwritten'):binding.main()
  self.assertEqual(calls,[])
