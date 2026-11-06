"""Windows-only helper: secret goes solely to the caller's captured pipe."""
import ctypes,sys
from ctypes import wintypes
class CREDENTIAL(ctypes.Structure):
 _fields_=[('Flags',wintypes.DWORD),('Type',wintypes.DWORD),('TargetName',wintypes.LPWSTR),('Comment',wintypes.LPWSTR),('LastWritten',wintypes.FILETIME),('CredentialBlobSize',wintypes.DWORD),('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),('Persist',wintypes.DWORD),('AttributeCount',wintypes.DWORD),('Attributes',ctypes.c_void_p),('TargetAlias',wintypes.LPWSTR),('UserName',wintypes.LPWSTR)]
def main():
 ptr=ctypes.POINTER(CREDENTIAL)();adv=ctypes.WinDLL('advapi32',use_last_error=True)
 adv.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(CREDENTIAL))];adv.CredReadW.restype=wintypes.BOOL
 adv.CredFree.argtypes=[ctypes.c_void_p];adv.CredFree.restype=None
 if not adv.CredReadW('Trading_Bot/TRADING_BOT_AI',1,0,ctypes.byref(ptr)):raise SystemExit(2)
 try:
  n=ptr.contents.CredentialBlobSize
  if n<2 or n>8192 or n%2:raise SystemExit(3)
  secret=ctypes.string_at(ptr.contents.CredentialBlob,n).decode('utf-16-le').rstrip('\x00')
  sys.stdout.buffer.write(secret.encode('utf-8'));sys.stdout.buffer.flush()
 finally:adv.CredFree(ptr)
if __name__=='__main__':main()
