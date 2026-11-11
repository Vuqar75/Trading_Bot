import csv,hashlib,io,json,tempfile,unittest,zipfile
from pathlib import Path
import numpy as np
from research.pattern_calibration import VERSION,adjust,interpolate
from research.derivatives_feature_study import asof
from research.derivative_metrics_data import FIELDS,SYMBOLS,load

class CalibrationBoundary(unittest.TestCase):
    def fixture(self):
        c={'p_x':[.1,.8],'p_y':[.2,.7],'net_x':[-.01,.01],'net_y':[-.005,.005],'edges':[-.01,.01],
           'cells':[{'examples':100,'days':30,'lower_score':.001}]}
        return {'version':VERSION,'base_model_id':'m','valid_from_ms':1000,'valid_until_ms':10000,'max_label_end_ms':999,'assets':{'BTCUSDT':c}}
    def test_informational_candidate_never_grants_order(self):
        z=adjust(.7,.005,self.fixture(),symbol='BTCUSDT',signal_ms=2000,model_id='m')
        self.assertEqual(z['action'],'CANDIDATE');self.assertIs(z['trade_allowed'],False)
    def test_wrong_model_rejected(self):
        with self.assertRaisesRegex(ValueError,'model_mismatch'):adjust(.7,.005,self.fixture(),symbol='BTCUSDT',signal_ms=2000,model_id='wrong')
    def test_future_labels_rejected(self):
        c=self.fixture();c['max_label_end_ms']=2000
        with self.assertRaisesRegex(ValueError,'stale_or_future'):adjust(.7,.005,c,symbol='BTCUSDT',signal_ms=2000,model_id='m')
    def test_sparse_support_blocks_optimistic_forecast(self):
        c=self.fixture();c['assets']['BTCUSDT']['cells'][0]['days']=3
        z=adjust(.7,.005,c,symbol='BTCUSDT',signal_ms=2000,model_id='m');self.assertEqual(z['action'],'NO_TRADE')
    def test_extrapolation_not_admitted(self):
        z=adjust(.7,.02,self.fixture(),symbol='BTCUSDT',signal_ms=2000,model_id='m');self.assertFalse(z['calibration_supported']);self.assertEqual(z['action'],'NO_TRADE')
    def test_interpolation(self):self.assertAlmostEqual(interpolate(.5,[0,1],[.2,.6]),.4)

class PositioningAsOf(unittest.TestCase):
    def test_never_reads_future_metric(self):
        ts=np.array([0,300000,600000]);v=np.array([[1],[2],[3]])
        x,ok,source=asof(ts,v,np.array([450000]));self.assertEqual(x[0,0],2);self.assertTrue(ok[0]);self.assertEqual(source[0],300000)
    def test_missing_and_stale_history_rejected(self):
        _,ok,_=asof(np.array([300000]),np.array([[1]]),np.array([0,600001]));self.assertFalse(ok.any())
    def test_future_append_does_not_change_past(self):
        a=asof(np.array([0,300000]),np.array([[1],[2]]),np.array([150000]))
        b=asof(np.array([0,300000,600000]),np.array([[1],[2],[900]]),np.array([150000]))
        for x,y in zip(a,b):np.testing.assert_array_equal(x,y)

class ArchiveMetrics(unittest.TestCase):
    def write(self,root,duplicate=False,conflict=False):
        files=[]
        for s in SYMBOLS:
            buf=io.StringIO();w=csv.writer(buf);w.writerow(['create_time','symbol',*FIELDS])
            w.writerow(['2026-10-31 00:05:00',s,100,2,2,2,2]);w.writerow(['2026-10-31 00:00:00',s,100,2,2,2,2])
            if duplicate:w.writerow(['2026-10-31 00:00:00',s,101 if conflict else 100,2,2,2,2])
            b=io.BytesIO()
            with zipfile.ZipFile(b,'w') as z:z.writestr('data.csv',buf.getvalue())
            p=root/(s+'.zip');p.write_bytes(b.getvalue());files.append({'file':p.name,'symbol':s,'day':'2026-10-31','sha256':hashlib.sha256(b.getvalue()).hexdigest()})
        (root/'manifest.json').write_text(json.dumps({'files':files}))
    def test_unordered_records_sorted_before_features(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.write(p);data,q=load(p)
            self.assertTrue((np.diff(data['BTCUSDT'][0])>0).all());self.assertEqual(q['BTCUSDT']['out_of_order_rows'],1)
    def test_conflicting_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.write(p,True,True)
            with self.assertRaisesRegex(ValueError,'conflicting_duplicate'):load(p)
    def test_equal_duplicate_does_not_add_weight(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.write(p,True,False);data,q=load(p);self.assertEqual(len(data['BTCUSDT'][0]),2);self.assertEqual(q['BTCUSDT']['equal_duplicates'],1)

if __name__=='__main__':unittest.main()
