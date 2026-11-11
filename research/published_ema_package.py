"""Preserve compact study artifacts and verify lossless packaging."""
import gzip,hashlib,pathlib,shutil,sys
src=pathlib.Path(sys.argv[1]);dst=pathlib.Path('research/published_ema_transfer_results');dst.mkdir(exist_ok=True)
for name in ('choices.json.gz','signals.json.gz','events.json.gz','ledger.json.gz','audit.json'):
 shutil.copy2(src/name,dst/name)
 if hashlib.sha256((src/name).read_bytes()).digest()!=hashlib.sha256((dst/name).read_bytes()).digest():raise ValueError('copy mismatch')
raw=(src/'summary.json').read_bytes();packed=gzip.compress(raw,mtime=0)
if gzip.decompress(packed)!=raw:raise ValueError('summary compression mismatch')
(dst/'summary.json.gz').write_bytes(packed)
for p in sorted(dst.iterdir()):print(p.name,p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest())
