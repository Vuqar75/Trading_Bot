import subprocess,sys,tempfile,unittest
from pathlib import Path
from trading_bot.app import Application
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.testnet_observer import ObservationError

class PaperActivationTests(unittest.TestCase):
 def test_manual_activation_and_restart_pause(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state.db';s=RuntimeStore(path,{'mode':'paper'})
   app=Application(s,PaperExchange(),None)
   self.assertTrue(s.get('paused'));app.enable_paper_entries();self.assertFalse(s.get('paused'))
   s.close();s=RuntimeStore(path,{'mode':'paper'});Application(s,PaperExchange(),None)
   self.assertTrue(s.get('paused'));s.close()
 def test_gate_prevents_activation(self):
  with tempfile.TemporaryDirectory() as d:
   s=RuntimeStore(Path(d)/'state.db',{'mode':'paper'});app=Application(s,PaperExchange(),None)
   with s.transaction():s.set('blocked','RECOVERY_REQUIRED')
   with self.assertRaises(ObservationError):app.enable_paper_entries()
   self.assertTrue(s.get('paused'));s.close()
 def test_testnet_cannot_activate_and_cli_rejects(self):
  class TestExchange:mode='testnet'
  with tempfile.TemporaryDirectory() as d:
   s=RuntimeStore(Path(d)/'state.db',{'mode':'testnet'});app=Application(s,TestExchange(),None)
   with self.assertRaises(ObservationError):app.enable_paper_entries()
   s.close()
  p=subprocess.run([sys.executable,'-B','-m','trading_bot.app','--mode','testnet','--enable-testnet-orders','--allow-paper-signal'],capture_output=True)
  self.assertNotEqual(p.returncode,0)
