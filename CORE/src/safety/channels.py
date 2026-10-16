"""把即時保護心跳與磁碟紀錄分開；讀取端只能取得共享記憶體唯讀映射。"""
import copy
import ctypes as C
from ctypes import wintypes as W
import json
import mmap
import queue
import struct
import threading
import time
import zlib

NAMES={'guard':r'Global\TEST_功耗即時_v1','miner':r'Global\TEST_礦工即時_v1'}
USER_SID='S-1-5-21-71531830-2240404114-266996542-1005'
SIZE=65536
HEADER=struct.Struct('<8sQII')
MAGIC=b'TESTCH01'
kernel=C.WinDLL('kernel32',use_last_error=True)
kernel.OpenFileMappingW.argtypes=[W.DWORD,W.BOOL,W.LPCWSTR]
kernel.OpenFileMappingW.restype=W.HANDLE
kernel.MapViewOfFile.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t]
kernel.MapViewOfFile.restype=C.c_void_p
kernel.UnmapViewOfFile.argtypes=[C.c_void_p]
kernel.CloseHandle.argtypes=[W.HANDLE]
kernel.CreateFileMappingW.argtypes=[W.HANDLE,C.c_void_p,W.DWORD,W.DWORD,W.DWORD,W.LPCWSTR]
kernel.CreateFileMappingW.restype=W.HANDLE
kernel.LocalFree.argtypes=[C.c_void_p]
advapi=C.WinDLL('advapi32',use_last_error=True)
advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[W.LPCWSTR,W.DWORD,C.POINTER(C.c_void_p),C.c_void_p]


class SecurityAttributes(C.Structure):
    _fields_=[('length',W.DWORD),('descriptor',C.c_void_p),('inherit',W.BOOL)]


def create_global(name,user_write=False):
    """SYSTEM 建立跨工作階段通道，只授權既有本機使用者，不開放其他帳號。"""
    descriptor=C.c_void_p()
    access='GRGW' if user_write else 'GR'
    sddl=f'D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;{access};;;{USER_SID})S:(ML;;NW;;;ME)'
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,C.byref(descriptor),None):
        raise C.WinError(C.get_last_error())
    try:
        attributes=SecurityAttributes(C.sizeof(SecurityAttributes),descriptor,False)
        handle=kernel.CreateFileMappingW(W.HANDLE(-1),C.byref(attributes),4,0,SIZE,NAMES[name])
        if not handle:
            raise C.WinError(C.get_last_error())
        return handle
    finally:
        kernel.LocalFree(descriptor)


class ChannelWriter:
    def __init__(self,name,tagname=None):
        self.reserved=[]
        if name=='guard' and tagname is None:
            self.reserved=[create_global('guard'),create_global('miner',user_write=True)]
        # Python mmap 開啟既有具名物件會要求所有權限；改用明確的讀寫映射，
        # 一般使用者只取得礦工心跳的讀寫權，不能更改物件權限或寫入保護通道。
        self.handle=(kernel.CreateFileMappingW(W.HANDLE(-1),None,4,0,SIZE,tagname) if tagname
                     else kernel.OpenFileMappingW(6,False,NAMES[name]))
        if not self.handle:
            raise C.WinError(C.get_last_error())
        self.view=kernel.MapViewOfFile(self.handle,6,0,0,SIZE)
        if not self.view:
            kernel.CloseHandle(self.handle)
            raise C.WinError(C.get_last_error())
        self.sequence=0
        self.lock=threading.Lock()

    def publish(self,data):
        payload=json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        if len(payload)>SIZE-HEADER.size:
            raise RuntimeError('即時心跳超出允許長度。')
        with self.lock:
            self.sequence+=2
            C.memmove(self.view,HEADER.pack(MAGIC,self.sequence-1,0,0),HEADER.size)
            C.memmove(self.view+HEADER.size,payload,len(payload))
            C.memmove(self.view,HEADER.pack(MAGIC,self.sequence,len(payload),zlib.crc32(payload)),HEADER.size)

    def close(self):
        kernel.UnmapViewOfFile(self.view)
        kernel.CloseHandle(self.handle)
        for handle in self.reserved:
            kernel.CloseHandle(handle)


class ChannelReader:
    def __init__(self,name,tagname=None):
        self.handle=kernel.OpenFileMappingW(4,False,tagname or NAMES[name])
        self.view=None
        if not self.handle:
            raise FileNotFoundError('即時心跳尚未建立。')
        self.view=kernel.MapViewOfFile(self.handle,4,0,0,SIZE)
        if not self.view:
            kernel.CloseHandle(self.handle)
            self.handle=None
            raise OSError('無法取得即時心跳唯讀映射。')

    def read(self):
        for _ in range(3):
            first=C.string_at(self.view,HEADER.size)
            magic,sequence,length,checksum=HEADER.unpack(first)
            if magic!=MAGIC or sequence%2 or not 0<length<=SIZE-HEADER.size:
                continue
            payload=C.string_at(self.view+HEADER.size,length)
            if first!=C.string_at(self.view,HEADER.size) or zlib.crc32(payload)!=checksum:
                continue
            data=json.loads(payload)
            return data if isinstance(data,dict) else None
        return None

    def close(self):
        if self.view:
            kernel.UnmapViewOfFile(self.view)
            self.view=None
        if self.handle:
            kernel.CloseHandle(self.handle)
            self.handle=None


_readers={}
_reader_lock=threading.Lock()


def read_channel(name):
    with _reader_lock:
        if name not in _readers:
            try:
                _readers[name]=ChannelReader(name)
            except OSError:
                return None
        return _readers[name].read()


class BackgroundFileWriter:
    """最多保留一筆最新紀錄；檔案卡住或暫時鎖住不阻塞即時保護。"""
    def __init__(self,write):
        self.queue=queue.Queue(maxsize=1)
        self.stop=threading.Event()
        self.lock=threading.Lock()
        self.state={'錯誤':'','更新時間':time.monotonic(),'最長記錄秒數':0}
        def loop():
            pending=None
            while not self.stop.is_set():
                try:
                    pending=self.queue.get(timeout=.2) if pending is None else self.queue.get_nowait()
                except queue.Empty:
                    if pending is None:
                        continue
                began=time.monotonic()
                try:
                    write(pending)
                    pending=None
                    error=''
                except Exception as exc:
                    error=str(exc)
                with self.lock:
                    self.state['錯誤']=error
                    self.state['最長記錄秒數']=max(self.state['最長記錄秒數'],time.monotonic()-began)
                    if not error:
                        self.state['更新時間']=time.monotonic()
                if error:
                    self.stop.wait(.5)
        self.thread=threading.Thread(target=loop,daemon=True)
        self.thread.start()

    def submit(self,data):
        latest=copy.deepcopy(data)
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(latest)
        except queue.Full:
            pass

    def snapshot(self):
        with self.lock:
            return self.state.copy()


class CachedJson:
    """啟動時讀取設定，之後在背景更新；短暫讀寫衝突沿用已驗證設定。"""
    def __init__(self,read):
        self.read=read
        self.lock=threading.Lock()
        self.stop=threading.Event()
        self.data=read()
        self.error=''
        self.io_error=''
        def loop():
            while not self.stop.wait(.5):
                try:
                    data=read()
                    with self.lock:
                        self.data=data
                        self.error=self.io_error=''
                except OSError as exc:
                    with self.lock:
                        self.io_error=str(exc)
                except Exception as exc:
                    with self.lock:
                        self.error=str(exc)
        self.thread=threading.Thread(target=loop,daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            if self.error:
                raise RuntimeError('設定資料失效：'+self.error)
            return copy.deepcopy(self.data)
