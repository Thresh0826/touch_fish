"""Local Windows Credential Manager storage. No plaintext fallback."""
import ctypes
from ctypes import wintypes as w
import sys

class Credential(ctypes.Structure):
    _fields_=[('Flags',w.DWORD),('Type',w.DWORD),('TargetName',w.LPWSTR),
              ('Comment',w.LPWSTR),('LastWritten',w.FILETIME),
              ('CredentialBlobSize',w.DWORD),('CredentialBlob',ctypes.POINTER(w.BYTE)),
              ('Persist',w.DWORD),('AttributeCount',w.DWORD),('Attributes',ctypes.c_void_p),
              ('TargetAlias',w.LPWSTR),('UserName',w.LPWSTR)]

class WindowsCredentials:
    def __init__(self):
        if sys.platform!='win32':
            raise ValueError('账号密码设置需要在 Windows 本机完成。')
        self.api=ctypes.WinDLL('advapi32',use_last_error=True)
        self.api.CredWriteW.argtypes=[ctypes.POINTER(Credential),w.DWORD]
        self.api.CredWriteW.restype=w.BOOL
        self.api.CredReadW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.POINTER(Credential))]
        self.api.CredReadW.restype=w.BOOL
        self.api.CredFree.argtypes=[ctypes.c_void_p]
        self.api.CredFree.restype=None

    def write(self,target,username,password):
        encoded=password.encode('utf-16-le')
        if len(encoded)>2560:
            raise ValueError('密码长度超过 Windows 凭据存储上限。')
        blob=(w.BYTE*len(encoded)).from_buffer_copy(encoded)
        data=Credential()
        data.Type=1  # CRED_TYPE_GENERIC
        data.TargetName=target
        data.UserName=username
        data.CredentialBlobSize=len(encoded)
        data.CredentialBlob=ctypes.cast(blob,ctypes.POINTER(w.BYTE))
        data.Persist=2  # Local computer, current Windows user.
        try:
            if not self.api.CredWriteW(ctypes.byref(data),0):
                raise ValueError('Windows 凭据保存失败，请确认当前用户可使用凭据管理器。')
        finally:
            ctypes.memset(blob,0,len(encoded))

    def read(self,target):
        pointer=ctypes.POINTER(Credential)()
        if not self.api.CredReadW(target,1,0,ctypes.byref(pointer)):
            raise ValueError('未找到本机保存的凭据，请在设置中重新填写账号密码。')
        try:
            data=pointer.contents
            if not data.CredentialBlobSize or data.CredentialBlobSize>2560:
                raise ValueError('本机凭据内容无效，请重新保存密码。')
            raw=ctypes.string_at(data.CredentialBlob,data.CredentialBlobSize)
            return data.UserName, raw.decode('utf-16-le').rstrip('\x00')
        finally:
            self.api.CredFree(pointer)

    def exists(self,target):
        pointer=ctypes.POINTER(Credential)()
        if not self.api.CredReadW(target,1,0,ctypes.byref(pointer)):
            return False
        self.api.CredFree(pointer)
        return True
