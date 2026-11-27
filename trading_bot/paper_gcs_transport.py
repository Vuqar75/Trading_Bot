"""Create-only GCS upload and bounded read-back via the VM service account."""
import hashlib
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

TOKEN_URL = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
STORAGE = 'https://storage.googleapis.com'


def upload_verified(path, digest, *, bucket, kind, captured_at, downloaded, opener=urlopen):
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]',bucket or ''):
        raise ValueError('Invalid bucket')
    if kind not in ('hourly','daily') or not re.fullmatch('[0-9a-f]{64}',digest or ''):
        raise ValueError('Invalid backup identity')
    if type(captured_at) not in (int,float) or captured_at<=0:
        raise ValueError('Invalid backup time')
    path, downloaded = Path(path), Path(downloaded)
    size = path.stat().st_size
    name = f'paper-backups/{kind}/{int(captured_at)}-{digest}.tar.gz'
    try:
        with opener(Request(TOKEN_URL,headers={'Metadata-Flavor':'Google'}),timeout=5) as reply:
            token = json.load(reply)['access_token']
    except (OSError, ValueError, KeyError):
        raise ValueError('External backup VM credential unavailable') from None
    headers = {'Authorization':'Bearer '+token}
    query = urlencode({'uploadType':'media','name':name,'ifGenerationMatch':0})
    upload_url = f'{STORAGE}/upload/storage/v1/b/{bucket}/o?{query}'
    upload_outcome = 'ok'
    with path.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:
            raise ValueError('Local archive mismatch')
        stream.seek(0)
        request = Request(upload_url, data=stream, method='POST',
                          headers=headers | {'Content-Type':'application/gzip','Content-Length':str(size)})
        try:
            with opener(request,timeout=90) as reply:
                if json.load(reply).get('name')!=name:
                    raise ValueError('External backup upload object mismatch')
        except HTTPError as exc:
            upload_outcome = f'HTTP {exc.code}'
            if exc.code not in (408,409,412,429) and exc.code<500:
                raise ValueError('External backup upload '+upload_outcome) from None
        except (URLError, TimeoutError, ConnectionError) as exc:
            upload_outcome = type(exc).__name__
    read_url = f'{STORAGE}/storage/v1/b/{bucket}/o/{quote(name,safe="")}?alt=media'
    remaining = size
    checksum = hashlib.sha256()
    try:
        with opener(Request(read_url,headers=headers),timeout=90) as reply, downloaded.open('xb') as dest:
            while remaining:
                chunk = reply.read(min(1024*1024,remaining))
                if not chunk:
                    break
                dest.write(chunk)
                checksum.update(chunk)
                remaining -= len(chunk)
            if remaining or reply.read(1):
                raise ValueError('External backup read-back size mismatch')
    except HTTPError as exc:
        raise ValueError(f'External backup read-back HTTP {exc.code}, upload {upload_outcome}') from None
    except OSError as exc:
        raise ValueError(f'External backup read-back {type(exc).__name__}, upload {upload_outcome}') from None
    if checksum.hexdigest()!=digest:
        raise ValueError('External backup checksum mismatch')
    return {'object':f'gs://{bucket}/{name}','sha256':digest}
