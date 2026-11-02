import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from trading_bot.cross_systemd_controller import SystemdPaperController


class CrossSystemdControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.financial=self.root/'financial.db';self.financial.write_bytes(b'fixture')
        self.units=self.root/'units';self.units.mkdir()
        self.c=SystemdPaperController(self.financial,self.units,None,timeout=1)
        self.props={}
        for n in (self.c.SPOT,self.c.FUTURES):
            self.props[(n,'LoadState')]='loaded';self.props[(n,'NeedDaemonReload')]='no'
            self.props[(n,'DropInPaths')]=str(self.units/(n+'.d')/'90-paper-cross-interlock.conf')
        self.props[(self.c.CROSS,'LoadState')]='not-found'

    def prop(self,unit,key):return self.props.get((unit,key))

    def test_interlock_rejects_stale_units_extra_dropins_and_missing_cross(self):
        with patch.object(self.c,'_property',side_effect=self.prop):
            self.assertTrue(self.c.interlock_loaded(self.root/'marker'))
            self.props[(self.c.SPOT,'NeedDaemonReload')]='yes'
            self.assertFalse(self.c.interlock_loaded(self.root/'marker'))
            self.props[(self.c.SPOT,'NeedDaemonReload')]='no'
            self.props[(self.c.FUTURES,'DropInPaths')]+=' /etc/unexpected.conf'
            self.assertFalse(self.c.interlock_loaded(self.root/'marker'))
            self.props[(self.c.FUTURES,'DropInPaths')]=str(self.units/(self.c.FUTURES+'.d')/'90-paper-cross-interlock.conf')
            (self.units/self.c.CROSS).write_text('new unit')
            self.assertFalse(self.c.interlock_loaded(self.root/'marker'))
            self.props[(self.c.CROSS,'LoadState')]='loaded';self.props[(self.c.CROSS,'NeedDaemonReload')]='no'
            self.props[(self.c.CROSS,'DropInPaths')]=str(self.units/(self.c.CROSS+'.d')/'90-paper-cross-interlock.conf')
            self.assertTrue(self.c.interlock_loaded(self.root/'marker'))

    def test_loaded_units_require_exact_fragment_and_no_reload(self):
        n=self.c.SPOT;self.props[(n,'FragmentPath')]=str(self.units/n)
        with patch.object(self.c,'_property',side_effect=self.prop):
            self.assertTrue(self.c.loaded_units_match(self.units,(n,)))
            self.props[(n,'NeedDaemonReload')]='yes'
            self.assertFalse(self.c.loaded_units_match(self.units,(n,)))
            self.props[(n,'NeedDaemonReload')]='no';self.props[(n,'FragmentPath')]='/unexpected/spot.service'
            self.assertFalse(self.c.loaded_units_match(self.units,(n,)))
