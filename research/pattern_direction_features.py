"""One fixed side-aligned representation. No labels, fitting, IO or execution."""
import numpy as np
from .technical_patterns import FEATURES as RAW

VERSION='pattern-direction-aligned-v1'
SIGNED=('ret1','ret4','ret16','ret96','ema9_gap','ema20_gap','ema50_gap','ema200_gap','hour_trend','hour_gap','fourhour_gap')
RENAMED={'rsi':'rsi_centered_aligned','upper_wick':'opposing_wick','lower_wick':'supporting_wick','close_location':'close_location_aligned'}
FEATURES=[RENAMED.get(k,k+'_aligned' if k in SIGNED else k) for k in RAW[:22]]+['engulf_aligned','pin_aligned','inside_aligned','break_aligned']+RAW[30:]


def transform(values):
    x=np.asarray(values,dtype=np.float32)
    if x.ndim!=2 or x.shape[1]!=len(RAW) or not np.isfinite(x).all():raise ValueError('direction_input_shape_or_value')
    side=x[:,30]
    if not np.isin(side,[-1.,1.]).all():raise ValueError('direction_side')
    if not np.isin(x[:,31:], [0.,1.]).all() or not np.all(x[:,31:].sum(axis=1)==1):raise ValueError('direction_asset')
    flags=x[:,22:30]
    if not np.isin(flags,[0.,1.]).all():raise ValueError('direction_pattern_flag')
    long=flags[:,::2].any(axis=1);short=flags[:,1::2].any(axis=1)
    if not np.all((long & ~short & (side==1)) | (short & ~long & (side==-1))):raise ValueError('direction_pattern_identity')
    base=x[:,:22].copy()
    for name in SIGNED:base[:,RAW.index(name)]*=side
    base[:,4]=(x[:,4]-.5)*side
    short=side==-1
    base[:,8]=np.where(short,x[:,9],x[:,8])
    base[:,9]=np.where(short,x[:,8],x[:,9])
    base[:,10]=np.where(short,1-x[:,10],x[:,10])
    aligned=np.maximum(flags[:,::2],flags[:,1::2])
    return np.ascontiguousarray(np.column_stack((base,aligned,x[:,30:])),dtype=np.float32)
