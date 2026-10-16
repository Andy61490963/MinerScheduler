"""直接讀取 NVIDIA 感測器；不載入風扇控制套件，也不取得任何寫入介面。"""
import ctypes as C
import json
from pathlib import Path
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')


class Thermal(C.Structure):
    _fields_ = [('version',C.c_uint),('mask',C.c_uint),('reserved',C.c_int*8),('temperatures',C.c_int*32)]


class FanItem(C.Structure):
    _fields_ = [('index',C.c_uint),('rpm',C.c_uint),('minimum',C.c_uint),('maximum',C.c_uint),('level',C.c_uint),('reserved',C.c_uint*8)]


class FanStatus(C.Structure):
    _fields_ = [('version',C.c_uint),('count',C.c_uint),('reserved',C.c_ulonglong*4),('items',FanItem*32)]


class ControlItem(C.Structure):
    _fields_ = [('index',C.c_uint),('level',C.c_uint),('mode',C.c_uint),('reserved',C.c_uint*8)]


class ControlStatus(C.Structure):
    _fields_ = [('version',C.c_uint),('reserved0',C.c_uint),('count',C.c_uint),('reserved1',C.c_uint*8),('items',ControlItem*32)]


class ReadOnlyGpu:
    # 白名單只有初始化、列舉及讀取；沒有設定或還原風扇、時脈、電壓與功耗的功能。
    ALLOWED = {0x0150E828,0xE5AC921F,0x35AED5E8,0x814B209F,0x65FE3AAD}

    def __init__(self):
        self.dll = C.CDLL('C:/Windows/System32/nvapi64.dll')
        self.query = self.dll.nvapi_QueryInterface
        self.query.argtypes = [C.c_uint]
        self.query.restype = C.c_void_p
        init = self.api(0x0150E828)
        if init()!=0:
            raise RuntimeError('NVIDIA 純讀介面初始化失敗。')
        enum = self.api(0xE5AC921F,C.POINTER(C.c_void_p),C.POINTER(C.c_uint))
        handles,count=(C.c_void_p*64)(),C.c_uint()
        if enum(handles,C.byref(count))!=0 or count.value!=1:
            raise RuntimeError('純讀監控要求恰好一張 NVIDIA 顯卡。')
        self.handle=handles[0]
        self.get_thermal=self.api(0x65FE3AAD,C.c_void_p,C.POINTER(Thermal))
        self.get_fans=self.api(0x35AED5E8,C.c_void_p,C.POINTER(FanStatus))
        self.get_controls=self.api(0x814B209F,C.c_void_p,C.POINTER(ControlStatus))
        self.mask=0
        for bit in range(32):
            probe=Thermal()
            probe.version=C.sizeof(Thermal)|(2<<16)
            probe.mask=1<<bit
            if self.get_thermal(self.handle,C.byref(probe))!=0:
                break
            self.mask=(1<<(bit+1))-1
        if self.mask<128:
            raise RuntimeError('必要的核心、熱點與顯存感測器不可讀。')

    def api(self,code,*types):
        if code not in self.ALLOWED:
            raise RuntimeError('拒絕取得不在純讀白名單的介面。')
        address=self.query(code)
        if not address:
            raise RuntimeError(f'所需純讀介面不存在：{code:08x}。')
        return C.CFUNCTYPE(C.c_int,*types)(address)

    def read(self):
        temperatures=Thermal()
        temperatures.version=C.sizeof(Thermal)|(2<<16)
        temperatures.mask=self.mask
        fans=FanStatus()
        fans.version=C.sizeof(FanStatus)|(1<<16)
        controls=ControlStatus()
        controls.version=C.sizeof(ControlStatus)|(1<<16)
        codes=[self.get_thermal(self.handle,C.byref(temperatures)),self.get_fans(self.handle,C.byref(fans)),self.get_controls(self.handle,C.byref(controls))]
        if any(code!=0 for code in codes) or fans.count!=2 or controls.count!=2:
            raise RuntimeError(f'純讀監控失敗：{codes}，風扇數量 {fans.count}/{controls.count}。')
        result={'核心C':temperatures.temperatures[0]/256,'熱點C':temperatures.temperatures[1]/256,'顯存C':temperatures.temperatures[7]/256}
        if not all(0<value<125 for value in result.values()):
            raise RuntimeError(f'溫度讀值不合理：{result}。')
        result['風扇']=[{'編號':fans.items[i].index,'RPM':fans.items[i].rpm,'實際百分比':fans.items[i].level} for i in range(fans.count)]
        result['控制']=[{'編號':controls.items[i].index,'設定百分比':controls.items[i].level,'模式':'手動' if controls.items[i].mode==1 else '自動'} for i in range(controls.count)]
        return result


if __name__=='__main__':
    reader=ReadOnlyGpu()
    print(json.dumps({'純讀白名單':[f'{code:08x}' for code in sorted(reader.ALLOWED)],'讀值':reader.read()},ensure_ascii=False,indent=2))
