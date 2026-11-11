"""Check one author-saved training choice via ranged public ZIP reads only."""
import hashlib,io,json,pathlib,sys,tempfile,urllib.request,zipfile
import numpy as np
import pyreadr

URL='https://drive.usercontent.google.com/download?id=1y7J3cGFEYYBufVTACzPYr4GXiUSCOKm7&export=download&confirm=t'
SIZE=80263893
PREFIX='unseen_period_results/dvc-exps/TRAIN_14_TEST_10_BTC/'
EXPECTED_02='1a60292454b5b8aeed914a0cda02eb6e9c5807afd247c8afddcc5aede41a9030'
EXPECTED_09='abcf96636539115484437f816ec2997570b2e1f726cfd2e34339c11bcca5d320'

class RangeReader(io.RawIOBase):
 def __init__(self):self.pos=0;self.bytes_read=0;self.requests=0
 def readable(self):return True
 def seekable(self):return True
 def tell(self):return self.pos
 def seek(self,offset,whence=0):
  self.pos=offset if whence==0 else self.pos+offset if whence==1 else SIZE+offset
  if self.pos<0:raise ValueError('negative seek')
  return self.pos
 def read(self,n=-1):
  if n<0:n=SIZE-self.pos
  n=min(n,SIZE-self.pos)
  if n<=0:return b''
  begin=self.pos;end=begin+n-1
  req=urllib.request.Request(URL,headers={'Range':f'bytes={begin}-{end}','User-Agent':'TradingBot research'})
  with urllib.request.urlopen(req,timeout=60) as r:
   if r.status!=206 or not r.headers.get('Content-Range','').startswith(f'bytes {begin}-{end}/'):raise ValueError('range not honored')
   data=r.read()
  if len(data)!=n:raise ValueError('truncated range')
  self.pos+=n;self.bytes_read+=n;self.requests+=1;return data

def main(out):
 reader=RangeReader()
 with zipfile.ZipFile(reader) as z, tempfile.TemporaryDirectory(dir='D:/TradingBot-ML-work/tmp') as temp:
  a=z.read(PREFIX+'02_downsampled.rds');b=z.read(PREFIX+'09_wf_test_evalued.rds')
  if hashlib.sha256(a).hexdigest()!=EXPECTED_02 or hashlib.sha256(b).hexdigest()!=EXPECTED_09:raise ValueError('saved author artifact hash mismatch')
  results={}
  for name in ('05_pnl.rds','08_wf_best_params.rds'):
   raw=z.read(PREFIX+name);p=pathlib.Path(temp)/name;p.write_bytes(raw)
   results[name]={'frame':pyreadr.read_r(str(p))[None],'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
  pnl=results['05_pnl.rds']['frame'];choice=results['08_wf_best_params.rds']['frame'].iloc[0]
  start=int(choice['train_start']);end=int(choice['train_end']);columns=[f'{f}_{s}' for s in (40,50,100,150,200) for f in (5,7,10,15,20,30)]
  matrix=pnl[columns].iloc[start-1:end].to_numpy(float)
  scores=np.sqrt(365*24)*matrix.mean(axis=0)/matrix.std(axis=0,ddof=1)
  selected=columns[int(np.nanargmax(scores))];expected=str(choice['selected_col'])
  if 'train_'+selected!=expected:raise AssertionError((selected,expected))
  sample_path=pathlib.Path(out).with_name('author-sample-train.npz')
  np.savez_compressed(sample_path,pnl=matrix)
  result={'source_archive_size':SIZE,'source_archive_pinned_sha256_from_completed_review':'2bb40a7860733ba073a089c82b3424f4551d101d061fb31f4e9f51da978a81fd','verification':'ZIP CRC plus 02 and 09 SHA-256 match completed review; only byte ranges were fetched, not full archive SHA recheck','02_sha256':EXPECTED_02,'09_sha256':EXPECTED_09,'05_sha256':results['05_pnl.rds']['sha256'],'08_sha256':results['08_wf_best_params.rds']['sha256'],'train_start_1based':start,'train_end_1based':end,'test_start_1based':int(choice['test_start']),'test_end_1based':int(choice['test_end']),'selected':selected,'saved_selected':expected,'recomputed_sharpe':float(scores[np.nanargmax(scores)]),'sample_npz_sha256':hashlib.sha256(sample_path.read_bytes()).hexdigest(),'bytes_transferred':reader.bytes_read,'range_requests':reader.requests}
 pathlib.Path(out).write_text(json.dumps(result,indent=2)+'\n',encoding='utf8');print(json.dumps(result,indent=2))
if __name__=='__main__':main(sys.argv[1])
