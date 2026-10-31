import unittest
from dataclasses import replace
from trading_bot.fill_ledger import Fill
from trading_bot.recovery_pages import SnapshotSimulator, PageCollector

class RecoveryPagesTest(unittest.TestCase):
    def setUp(self):
        self.fills=[Fill(str(i),'order','client',i,'BUY','0.001','5','BNB','0.01') for i in range(1,6)]
        self.sim=SnapshotSimulator('snapshot-1',self.fills,2)
    def test_reordering_and_identical_duplicate(self):
        c=PageCollector(self.sim.get_manifest())
        for i in (2,0,0,1): c.add(self.sim.get_page(i))
        self.assertEqual(c.finish(),tuple(self.fills))
    def test_missing_page_is_incomplete(self):
        c=PageCollector(self.sim.get_manifest()); c.add(self.sim.get_page(0)); c.add(self.sim.get_page(2))
        self.assertIsNone(c.finish())
    def test_changed_page_and_changed_snapshot_are_rejected(self):
        c=PageCollector(self.sim.get_manifest()); p=self.sim.get_page(0); c.add(p)
        with self.assertRaises(ValueError):c.add(replace(p, fills=(replace(p.fills[0],quote='6'),)+p.fills[1:]))
        with self.assertRaises(ValueError):c.add(replace(p,snapshot_id='other'))
    def test_inconsistent_manifest_and_unordered_history(self):
        m=replace(self.sim.get_manifest(),total=6); c=PageCollector(m)
        for i in range(3):c.add(self.sim.get_page(i))
        with self.assertRaises(ValueError):c.finish()
        with self.assertRaises(ValueError):SnapshotSimulator('x',[self.fills[1],self.fills[0]])
    def test_empty_snapshot(self):
        c=PageCollector(SnapshotSimulator('empty',[]).get_manifest()); self.assertEqual(c.finish(),())
if __name__=='__main__':unittest.main()
