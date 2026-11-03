"""Explicit object upload and read-back verification; no bucket provisioning."""
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path


def upload_verified(path, digest, *, bucket, kind, captured_at, run=subprocess.run):
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]',bucket or ''):raise ValueError('Invalid bucket')
    if kind not in ('hourly','daily') or not re.fullmatch('[0-9a-f]{64}',digest or ''):raise ValueError('Invalid backup identity')
    if type(captured_at) not in (int,float) or captured_at<=0:raise ValueError('Invalid backup time')
    name=f'paper-backups/{kind}/{int(captured_at)}-{digest}.tar.gz';uri=f'gs://{bucket}/{name}'
    with Path(path).open('rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=digest:raise ValueError('Local archive mismatch')
    # Create-only; on uncertain result a read-back of the same immutable name
    # decides success. No exception output is propagated (may contain URLs).
    try:run(['gcloud','storage','cp','--if-generation-match=0',str(path),uri],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
    except (OSError,subprocess.SubprocessError):pass
    with tempfile.TemporaryDirectory(prefix='paper-object-check-') as tmp:
        dest=Path(tmp)/'object';os.chmod(tmp,0o700)
        try:run(['gcloud','storage','cp',uri,str(dest)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        except (OSError,subprocess.SubprocessError):raise ValueError('External backup read-back unavailable') from None
        if not dest.is_file() or dest.is_symlink():raise ValueError('External backup read-back missing')
        with dest.open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest()!=digest:raise ValueError('External backup checksum mismatch')
    return {'object':uri,'sha256':digest}
