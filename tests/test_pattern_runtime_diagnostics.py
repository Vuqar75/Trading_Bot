import copy,hashlib,json,unittest
import numpy as np
from research import technical_patterns as tech
from tools import audit_pattern_runtime as audit


class RuntimeDiagnosticsTest(unittest.TestCase):
    def identity(self,bundle):
        bundle.pop('model_id',None)
        bundle['model_id']=hashlib.sha256(json.dumps(bundle,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return bundle

    def bundle(self):
        split={'feature':[31,-2,-2],'threshold':[.5,-2.,-2.],'left':[1,-1,-1],'right':[2,-1,-1],'value':[.25,.2,.3]}
        leaf={'feature':[-2],'threshold':[-2.],'left':[-1],'right':[-1],'value':[-.003]}
        return self.identity({'features':tech.FEATURES,'feature_version':tech.VERSION,'venue':'binance-usdm','admitted':False,
                              'valid_from_ms':1,'valid_until_ms':10000000,'train_label_end_ms':0,'classifier':[split],'regressor':[leaf]})

    def packet(self,bundle):
        f=dict.fromkeys(tech.FEATURES[:30],0.);f.update(atr_pct=.001,pin_long=1.)
        return {'feature_version':tech.VERSION,'venue':'binance-usdm','symbol':'BTCUSDT','signal_ms':900000,
                'side':'LONG','detected_patterns':['pin_long'],'features':f,
                'ml':{'model_id':bundle['model_id'],'trade_allowed':False,'action':'NO_TRADE','p_win_estimate':.3,'expected_stress_net_return':-.003}}

    def test_model_content_and_graph_corruption_are_detected(self):
        b=self.bundle();self.assertEqual(audit.validate_model(b),b['model_id'])
        bad=copy.deepcopy(b);bad['regressor'][0]['value'][0]=0
        with self.assertRaisesRegex(ValueError,'content_identity'):audit.validate_model(bad)
        bad=copy.deepcopy(b);bad['classifier'][0]['left'][0]=0;self.identity(bad)
        with self.assertRaisesRegex(ValueError,'tree_edge'):audit.validate_model(bad)

    def test_independent_native_walker_matches_runtime(self):
        b=self.bundle();rng=np.random.default_rng(42);X=rng.normal(size=(100,len(tech.FEATURES))).astype(np.float32)
        X[:3,31]=[np.nextafter(np.float32(.5),np.float32(0)),.5,np.nextafter(np.float32(.5),np.float32(1))]
        for kind in ('classifier','regressor'):
            tree=audit.native_tree(b[kind][0],kind);value=tree.predict(X);native=value[:,1] if kind=='classifier' else value[:,0]
            portable=[tech.predict_trees(b[kind],row) for row in X]
            np.testing.assert_array_equal(native,portable)

    def test_packet_replay_detects_wrong_asset_prediction_and_gate(self):
        b=self.bundle();p=self.packet(b)
        self.assertEqual(audit.packet_audit(b,p)['status'],'match')
        bad=copy.deepcopy(p);bad['symbol']='ETHUSDT'
        self.assertEqual(audit.packet_audit(b,bad)['status'],'prediction_mismatch')
        bad=copy.deepcopy(p);bad['ml']['action']='CANDIDATE'
        self.assertEqual(audit.packet_audit(b,bad)['status'],'prediction_mismatch')

    def test_partial_history_cannot_be_reconstructed_or_passed(self):
        b=self.bundle();p=self.packet(b);p['features']={'atr_pct':.001}
        result=audit.packet_audit(b,p)
        self.assertEqual(result['status'],'insufficient_feature_evidence');self.assertEqual(result['retained_features'],1)

    def test_pattern_flags_time_and_model_identity_are_bound(self):
        b=self.bundle();p=self.packet(b)
        bad=copy.deepcopy(p);bad['features']['pin_long']=0.
        self.assertEqual(audit.packet_audit(b,bad)['status'],'pattern_feature_mismatch')
        bad=copy.deepcopy(p);bad['signal_ms']=10000000
        self.assertEqual(audit.packet_audit(b,bad)['status'],'outside_model_window')
        bad=copy.deepcopy(p);bad['ml']['model_id']='different'
        self.assertEqual(audit.packet_audit(b,bad)['status'],'model_id_mismatch')
        bad=copy.deepcopy(p);bad['ml']['signal_ms']=1
        self.assertEqual(audit.packet_audit(b,bad)['status'],'model_identity_mismatch')

    def test_wait_is_not_a_missing_prediction_error(self):
        b=self.bundle();p=self.packet(b);p.update(side='WAIT',detected_patterns=[]);p['features']['pin_long']=0.
        p['ml'].pop('p_win_estimate');p['ml'].pop('expected_stress_net_return')
        self.assertEqual(audit.packet_audit(b,p)['status'],'no_direction')

    def test_bootstrap_leaf_target_check_without_training(self):
        n=200;y=np.tile([-.001,.002],100);X=np.zeros((n,len(tech.FEATURES)),dtype=np.float32)
        b={'classifier':[],'regressor':[]};seeds=np.random.RandomState(42).randint(np.iinfo(np.int32).max,size=64)
        for seed in seeds:
            selected=np.random.RandomState(int(seed)).randint(0,n,size=n)
            for kind,target in (('classifier',(y>0).astype(float)),('regressor',y)):
                b[kind].append({'feature':[-2],'threshold':[-2.],'left':[-1],'right':[-1],'value':[float(target[selected].mean())]})
        result=audit.forest_audit(b,X,y)
        self.assertEqual(result['classifier']['leaves_verified'],64)
        self.assertLess(result['regressor']['bootstrap_leaf_target_max_abs_error'],1e-12)
        b['regressor'][0]['value'][0]+=.1
        with self.assertRaisesRegex(ValueError,'mismatch'):audit.forest_audit(b,X,y)

    def test_past_only_constant_benchmark_has_no_test_label_leakage(self):
        bar=audit.BAR;T=np.array([bar,2*bar,3*bar,10*bar,11*bar,12*bar])
        inputs={'T':T,'label_end_ms':T+bar,'symbol':np.array([0,1,2,0,1,2]),'stress_target':np.array([-.01,0.,.01,-.02,.02,.001])}
        predictions=[{'T':int(T[i]),'symbol':audit.ASSETS[i-3],'fold':'toy','p_win':1/3,'pred_net':0.} for i in (3,4,5)]
        folds=[{'test_month':'toy','train_start_ms':0,'test_start_ms':10*bar,'test_end_ms':14*bar,'max_train_label_end_ms':4*bar,'train_rows':3,'test_rows':3}]
        result=audit.constant_benchmarks(inputs,predictions,folds)
        self.assertEqual(result['total']['rf'],result['total']['global'])
        self.assertEqual(result['total']['n'],3)
        bad=copy.deepcopy(folds);bad[0]['max_train_label_end_ms']=10*bar
        with self.assertRaisesRegex(ValueError,'training_receipt'):audit.constant_benchmarks(inputs,predictions,bad)
