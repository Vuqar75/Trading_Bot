"""One-time interactive Windows Credential Manager binding; never prints key."""
import ctypes,getpass,sys,warnings
from ctypes import wintypes
from read_adaptive_windows_credential import CREDENTIAL
def main():
 if not sys.stdin.isatty():raise SystemExit('interactive_terminal_required')
 adv=ctypes.WinDLL('advapi32',use_last_error=True);ptr=ctypes.POINTER(CREDENTIAL)()
 adv.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(CREDENTIAL))];adv.CredReadW.restype=wintypes.BOOL
 adv.CredFree.argtypes=[ctypes.c_void_p];adv.CredWriteW.argtypes=[ctypes.POINTER(CREDENTIAL),wintypes.DWORD];adv.CredWriteW.restype=wintypes.BOOL
 target='Trading_Bot/TRADING_BOT_AI'
 if adv.CredReadW(target,1,0,ctypes.byref(ptr)):
  adv.CredFree(ptr);raise SystemExit('credential_already_exists_not_overwritten')
 if ctypes.get_last_error()!=1168:raise SystemExit('credential_read_check_failed')
 with warnings.catch_warnings():
  warnings.simplefilter('error',getpass.GetPassWarning);secret=getpass.getpass('TRADING_BOT_AI (ввод скрыт, один раз): ')
 if not secret.strip() or len(secret)>4096:raise SystemExit('credential_invalid')
 encoded=bytearray(secret.encode('utf-16-le'));del secret
 try:
  blob=(ctypes.c_ubyte*len(encoded)).from_buffer(encoded)
  item=CREDENTIAL();item.Type=1;item.TargetName=target;item.UserName='Trading_Bot';item.Persist=2;item.CredentialBlobSize=len(encoded);item.CredentialBlob=ctypes.cast(blob,ctypes.POINTER(ctypes.c_ubyte))
  if not adv.CredWriteW(ctypes.byref(item),0):raise SystemExit('credential_write_failed')
  print('CREDENTIAL_SAVED')
 finally:
  for i in range(len(encoded)):encoded[i]=0
if __name__=='__main__':main()
