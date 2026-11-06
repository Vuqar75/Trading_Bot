"""One-time saved-credential relay over an already authorized, pinned SSH path.

No login, key prompt, plaintext temporary file, remote overwrite or API call.
The VM user/SSH alias must be the existing trusted operator connection.
"""
import argparse
import re
import shlex
import subprocess

from .adaptive_auto_once import secret_from_credential_manager

REMOTE = '''set -eu
umask 077
dir=/etc/credstore.encrypted
test -d "$dir"
test ! -e "$dir/tradingbot-ai.cred"
tmp=$(mktemp "$dir/.adaptive-ai-XXXXXXXX")
trap 'rm -f "$tmp"' EXIT
systemd-creds encrypt --with-key=host --name=TRADING_BOT_AI - "$tmp"
chmod 600 "$tmp"
ln "$tmp" "$dir/tradingbot-ai.cred"
echo SERVER_CREDENTIAL_PROVISIONED
'''


def provision(target, *, credential=secret_from_credential_manager, runner=subprocess.run):
    if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,199}', target):
        raise ValueError('trusted_ssh_alias_required')
    key = credential()
    if not isinstance(key,str) or not key.strip() or len(key)>8192 or any(c in key for c in ('\n','\r','\x00')):
        raise ValueError('saved_credential_invalid')
    command = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',target,
               'sudo -n sh -c '+shlex.quote(REMOTE)]
    try:
        result = runner(command,input=key.encode(),capture_output=True,timeout=90)
    finally:
        del key
    if result.returncode or b'SERVER_CREDENTIAL_PROVISIONED' not in result.stdout:
        # Never forward command stderr/stdout: encryption programs may include
        # arbitrary details. Inspect safe operator diagnostics separately.
        raise ValueError('server_credential_not_provisioned')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-target',required=True)
    args=parser.parse_args()
    try:provision(args.ssh_target)
    except Exception:raise SystemExit('SERVER_CREDENTIAL_NOT_PROVISIONED') from None
    print('SERVER_CREDENTIAL_PROVISIONED')


if __name__=='__main__':main()
