import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from research.legacy_universe_data import H,M,load


class LegacyDataTests(unittest.TestCase):
    def fixture(self,root,fund=None,missing=False):
        files=[]
        for kind in ('klines','markPriceKlines','fundingRate'):
            name=kind+'.zip';stream=io.StringIO();writer=csv.writer(stream)
            if kind=='fundingRate':
                writer.writerow(['calc_time','funding_interval_hours','last_funding_rate'])
                writer.writerows(fund or [(0,8,.0001),(8*H+1,4,.0001),(12*H,4,.0001)])
            else:
                writer.writerow(['open_time','open','high','low','close','volume','close_time'])
                for t in (0,) if missing and kind=='markPriceKlines' else (0,M):writer.writerow([t,1.,1.1,.9,1.,100.,t+M-1])
            with zipfile.ZipFile(root/name,'w') as z:z.writestr('data.csv',stream.getvalue())
            raw=(root/name).read_bytes();files.append({'file':name,'symbol':'XRPUSDT','kind':kind,'sha256':hashlib.sha256(raw).hexdigest()})
        (root/'manifest.json').write_text(json.dumps({'files':files}))

    def test_variable_funding_cadence_is_not_forced_to_eight_hours(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);trade,mark,v,fund,_=load(root,'XRPUSDT',0,0,2*M)
            self.assertEqual(trade.shape,(2,4));self.assertEqual(len(fund),3)
            self.assertEqual(fund[1][0],8*H+1)

    def test_missing_minutes_and_missing_funding_not_interpolated(self):
        for funding,missing in (([(0,8,.0001),(16*H,8,.0001)],False),(None,True)):
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);self.fixture(root,funding,missing)
                with self.assertRaises(ValueError):load(root,'XRPUSDT',0,0,2*M)

    def test_source_revision_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);(root/'klines.zip').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):load(root,'XRPUSDT',0,0,2*M)


if __name__=='__main__':unittest.main()
