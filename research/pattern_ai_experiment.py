"""Purged chronological pattern-aware ML study. No live trading or paid API."""
import datetime as dt,gzip,hashlib,json,math
from pathlib import Path
import sys
import numpy as np
from sklearn.ensemble import RandomForestClassifier,RandomForestRegressor
from research import rsi_crossasset as data
from research import technical_patterns as tech
from research.usdm_intraday_data import zipped
r=data.r;M=r.M;H=60*M


def month(t):return dt.datetime.fromtimestamp(t/1000,dt.timezone.utc).strftime('%Y-%m')
def ms(s):return r.core.stamp(s)
def nextmonth(s):
    y,m=map(int,s.split('-'));return f'{y+1}-01' if m==12 else f'{y}-{m+1:02d}'
def shifted(s,n):
    y,m=map(int,s.split('-'));i=y*12+m-1+n;return f'{i//12}-{i%12+1:02d}'


def outcome(t,side,atr_pct,symbol,trade,mark,funding,stress):
    impact=.001 if stress else .0005;fee=.0005
    entry_t=t+(6 if stress else 1)*M;ei=(entry_t-r.START)//M;deadline=ei+240
    if deadline>=len(trade):return {'rejection':'incomplete_label_window'}
    raw=float(trade[ei,0]);fill=raw*(1+side*impact);mp=float(mark[ei,0])
    qty,error=data.sizing(fill,mp,symbol,impact)
    if error:return {'rejection':error}
    if not .97<=fill/mp<=1.03:return {'rejection':'price_mark_bound'}
    stop_pct=float(np.clip(1.5*atr_pct,.005,.02));risk=qty*fill*(stop_pct+2*(fee+impact)+.0021)
    if risk>5:return {'rejection':'risk_cap'}
    stop=fill*(1-side*stop_pct);target=fill*(1+side*2*stop_pct)
    path=trade[ei:deadline];mpath=mark[ei:deadline]
    stops=np.flatnonzero(path[:,2]<=stop if side==1 else path[:,1]>=stop)
    targets=np.flatnonzero(path[:,1]>=target if side==1 else path[:,2]<=target)
    liqs=np.flatnonzero(mpath[:,2]<=fill*fee/.995 if side==1 else mpath[:,1]>=(2*fill-fill*fee)/1.005)
    si=int(stops[0]) if len(stops) else 10**9;ti=int(targets[0]) if len(targets) else 10**9;li=int(liqs[0]) if len(liqs) else 10**9
    liqfee=0.
    if min(si,ti,li)==10**9:
        exit_i=deadline;exit_t=r.START+exit_i*M;ref=float(trade[exit_i,0]);reason='time'
    else:
        k=min(si,ti,li);exit_i=ei+k;exit_t=r.START+(exit_i+1)*M
        if li==k:
            reason='liquidation';ref=float(path[k,2] if side==1 else path[k,1]);liqfee=.01
        elif si==k:
            reason='stop';ref=float(min(path[k,0],stop) if side==1 else max(path[k,0],stop))
        else:reason='target';ref=float(target)
    fts=np.asarray([x[0] for x in funding]);lo=int(np.searchsorted(fts,entry_t));hi=int(np.searchsorted(fts,exit_t,side='right'))
    events=[]
    for ft,rate in funding[lo:hi]:
        mi=(ft-r.START)//M;amount=-side*qty*float(mark[mi,0])*rate
        ambiguous=(reason!='time' and ft//M==(exit_t-M)//M) or ft==exit_t
        if ambiguous:amount=min(amount,0.)
        events.append({'event_ms':ft,'rate':rate,'mark':float(mark[mi,0]),'applied':float(amount),'ambiguous':ambiguous})
    exitfill=ref*(1-side*impact);fees=qty*fee*(fill+exitfill)
    gross=qty*side*(ref-raw);friction=qty*(abs(fill-raw)+abs(exitfill-ref));fund=sum(x['applied'] for x in events);lf=qty*exitfill*liqfee
    net=gross-fees-friction+fund-lf
    return {'signal_ms':t,'entry_ms':entry_t,'exit_ms':exit_t,'side':side,'symbol':symbol,'qty':qty,'entry':fill,'exit':exitfill,
            'entry_reference':raw,'exit_reference':ref,'notional':qty*raw,'estimated_risk':risk,'stop_pct':stop_pct,'stop':stop,'target':target,
            'gross':gross,'fees':fees,'impact':friction,'funding':fund,'funding_events':events,'liquidation_fee':lf,
            'net':net,'net_return':net/(qty*raw),'reason':reason,'entry_fee':qty*fill*fee,'hold_min':(exit_t-entry_t)//M}


def dataset(root,out):
    rows=[];markets={};proof={}
    for symbol in data.SYMBOLS:
        trade,mark,fund,inputs,conflicts=r.core.load_data(root,symbol)
        assert not conflicts and np.isfinite(trade).all() and np.isfinite(mark).all()
        volume=np.full(len(trade),np.nan)
        for p in sorted((root/'minute/klines').glob(symbol+'-1m-*.zip')):
            for row in zipped(p):
                if row[0].isdigit():
                    i=(int(row[0])-r.START)//M
                    if 0<=i<len(volume):volume[i]=float(row[5])
        assert np.isfinite(volume).all()
        X,flags,atr=tech.frame(trade,volume,r.START)
        small,_,_=tech.frame(trade[:4800],volume[:4800],r.START)
        np.testing.assert_allclose(X[:320],small,equal_nan=True)
        for i in range(len(X)):
            t=r.START+(i+1)*15*M
            if not r.BEGIN<=t<r.END-246*M or not np.isfinite(X[i]).all():continue
            long=bool(flags[i,::2].any());short=bool(flags[i,1::2].any())
            if long==short:continue
            side=1 if long else -1;x=tech.vector(X[i],side,symbol)
            a=outcome(t,side,float(atr[i]),symbol,trade,mark,fund,False)
            b=outcome(t,side,float(atr[i]),symbol,trade,mark,fund,True)
            rows.append({'T':t,'symbol':symbol,'side':side,'x':x.tolist(),'patterns':[name for name,yes in zip(tech.PATTERNS,flags[i]) if yes],
                         'label_end_ms':t+246*M,'base':a,'stress':b})
        markets[symbol]=(trade,mark,fund);proof[symbol]=inputs
        print('pattern dataset',symbol,sum(x['symbol']==symbol for x in rows),flush=True)
    rows.sort(key=lambda x:(x['T'],x['symbol']))
    with gzip.open(out/'examples.json.gz','wt') as f:json.dump(rows,f,separators=(',',':'))
    (out/'sources.json').write_text(json.dumps(proof,indent=2)+'\n')
    return rows,markets


def fit(train):
    X=np.asarray([x['x'] for x in train]);y=np.asarray([x['stress']['net_return'] for x in train])
    if len(train)<200 or len(set(y>0))<2:return None
    params={'n_estimators':64,'max_depth':5,'min_samples_leaf':40,'random_state':42,'n_jobs':2}
    clf=RandomForestClassifier(**params).fit(X,(y>0).astype(int))
    reg=RandomForestRegressor(**params).fit(X,y)
    return clf,reg


def replay(rows,predictions,markets,symbol,mode,stress):
    cash=1000.;peak=1000.;dd=0.;busy=0;trades=[];rejections={}
    scenario='stress' if stress else 'base';trade,mark,_=markets[symbol]
    def equity(v):
        nonlocal peak,dd
        peak=max(peak,v);dd=max(dd,(peak-v)/peak)
    for row in rows:
        if row['symbol']!=symbol or row['T']<ms('2025-07-01') or row['T']>=r.END:continue
        if mode=='AI':
            p=predictions.get((row['T'],symbol))
            if not p or p['p_win']<.60 or p['pred_net']<.0005:continue
        if row['T']<busy:continue
        z=row[scenario]
        if 'rejection' in z:
            key=z['rejection'];rejections[key]=rejections.get(key,0)+1;continue
        if z['entry']*z['qty']+z['entry_fee']>cash:
            rejections['insufficient_cash']=rejections.get('insufficient_cash',0)+1;continue
        entry_i=(z['entry_ms']-r.START)//M;exit_i=(z['exit_ms']-r.START)//M
        funds={}
        for f in z['funding_events']:funds.setdefault((f['event_ms']-r.START)//M,[]).append(f['applied'])
        accrued=0.;cost=.0005+(.001 if stress else .0005)
        for i in range(entry_i,exit_i):
            accrued+=sum(funds.get(i,[]));favorable=mark[i,1] if z['side']==1 else mark[i,2];adverse=mark[i,2] if z['side']==1 else mark[i,1]
            for mp in (favorable,adverse):equity(cash-z['entry_fee']+accrued+z['qty']*(mp-z['entry'])*z['side']-z['qty']*mp*cost)
        cash+=z['net'];equity(cash);busy=z['exit_ms'];trades.append(z)
    net=sum(z['net'] for z in trades)
    assert abs(cash-1000-net)<1e-7
    quarters={}
    for z in trades:
        d=dt.datetime.fromtimestamp(z['exit_ms']/1000,dt.timezone.utc);key=f'{d.year}Q{(d.month-1)//3+1}';quarters[key]=quarters.get(key,0)+z['net']
    summary={'closed':len(trades),'long':sum(z['side']==1 for z in trades),'short':sum(z['side']==-1 for z in trades),'net':net,
             'gross':sum(z['gross'] for z in trades),'fees':sum(z['fees'] for z in trades),'impact':sum(z['impact'] for z in trades),
             'funding':sum(z['funding'] for z in trades),'liquidation_fee':sum(z['liquidation_fee'] for z in trades),'max_dd':dd,
             'without_best':net-max([z['net'] for z in trades] or [0]),'quarters':quarters,'rejections':rejections,
             'mean_hold_min':float(np.mean([z['hold_min'] for z in trades])) if trades else None}
    return trades,summary


def main(root,out):
    r.START=ms('2024-12-01');r.BEGIN=ms('2025-01-01');r.END=ms('2026-11-01');data.configure();out.mkdir(parents=True,exist_ok=True)
    rows,markets=dataset(root,out);predictions={};folds=[]
    for i in range(16):
        name=shifted('2025-07',i);a=ms(name+'-01');b=ms(nextmonth(name)+'-01');start=ms(shifted(name,-6)+'-01')
        train=[x for x in rows if start<=x['T']<a and x['label_end_ms']<a and 'net_return' in x['stress']]
        test=[x for x in rows if a<=x['T']<b];models=fit(train)
        folds.append({'test_month':name,'train_start_ms':start,'test_start_ms':a,'test_end_ms':b,'max_train_label_end_ms':max(x['label_end_ms'] for x in train),
                      'train_rows':len(train),'test_rows':len(test),'model_fitted':bool(models)})
        assert folds[-1]['max_train_label_end_ms']<a
        if models and test:
            clf,reg=models;X=np.asarray([x['x'] for x in test]);p=clf.predict_proba(X)[:,1];net=reg.predict(X)
            ct,rt=tech.tree_json(clf,True),tech.tree_json(reg)
            for j in range(min(10,len(test))):
                xx=X[j].astype(np.float32)
                assert abs(tech.predict_trees(ct,xx)-p[j])<1e-9
                assert abs(tech.predict_trees(rt,xx)-net[j])<1e-9
            for row,pp,nn in zip(test,p,net):predictions[(row['T'],row['symbol'])]={'T':row['T'],'symbol':row['symbol'],'p_win':float(pp),'pred_net':float(nn),'fold':name}
        print('trained pattern AI',name,len(train),len(test),flush=True)
    journals={};summaries={}
    for symbol in data.SYMBOLS:
        for mode in ('baseline','AI'):
            for stress in (False,True):
                key=symbol+'/'+mode+('/stress' if stress else '/base')
                journals[key],summaries[key]=replay(rows,predictions,markets,symbol,mode,stress)
                print(key,'closed',summaries[key]['closed'],'net',summaries[key]['net'],flush=True)
    # Final inference artifact: May-Oct labels, purged before November, no evaluation retuning.
    cut=ms('2026-11-01');train=[x for x in rows if ms('2026-05-01')<=x['T']<cut and x['label_end_ms']<cut and 'net_return' in x['stress']]
    clf,reg=fit(train)
    bundle={'feature_version':tech.VERSION,'features':tech.FEATURES,'venue':'binance-usdm','valid_from_ms':cut,'valid_until_ms':ms('2026-12-01'),
            'train_label_end_ms':max(x['label_end_ms'] for x in train),'classifier':tech.tree_json(clf,True),'regressor':tech.tree_json(reg),
            'admitted':False,'feature_importance':dict(zip(tech.FEATURES,map(float,reg.feature_importances_)))}
    bundle['model_id']=hashlib.sha256(json.dumps(bundle,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    (out/'model.json').write_text(json.dumps(bundle,separators=(',',':'))+'\n')
    (out/'summary.json').write_text(json.dumps({'protocol':'PATTERN_AI_PROTOCOL.md','folds':folds,'results':summaries,'feature_version':tech.VERSION},indent=2)+'\n')
    for name,value in [('trades',journals),('predictions',list(predictions.values()))]:
        with gzip.open(out/(name+'.json.gz'),'wt') as f:json.dump(value,f,separators=(',',':'))

if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))
