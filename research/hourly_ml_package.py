"""Copy compact, losslessly verified final artifacts from D: into Git."""
import gzip,hashlib,lzma,pathlib,shutil,sys
src=pathlib.Path(sys.argv[1]);dst=pathlib.Path('research/hourly_ml_results');dst.mkdir(exist_ok=True)
for name in ('predictions','trades'):
 g=src/(name+'.json.gz');x=src/(name+'.json.xz')
 def raw_hash(opener,path):
  h=hashlib.sha256()
  with opener(path,'rb') as f:
   for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
  return h.hexdigest()
 if raw_hash(gzip.open,g)!=raw_hash(lzma.open,x):raise ValueError('compressed artifact mismatch')
 shutil.copy2(x,dst/x.name)
for name in ('summary.json','audit.json'):shutil.copy2(src/name,dst/name)
for p in sorted(dst.iterdir()):print(p.name,p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest())
