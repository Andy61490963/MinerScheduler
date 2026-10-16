"""只讀顯卡功耗與建立礦工程序保護；不控制風扇。"""
import csv
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parent
MINER=ROOT/'TEST_Core/TEST.exe'
EXPECTED='2cb529f44a06f172a18753267e363c1df1106b31c09c64e6b29627f99c816aa4'
SMI=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/nvidia-smi.exe'
def utc():
    return datetime.now(timezone.utc).isoformat()

class IoCounters(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]

class BasicLimit(C.Structure):
    _fields_ = [('PerProcessUserTimeLimit', C.c_longlong), ('PerJobUserTimeLimit', C.c_longlong), ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', C.c_size_t), ('MaximumWorkingSetSize', C.c_size_t), ('ActiveProcessLimit', W.DWORD), ('Affinity', C.c_size_t), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]

class ExtendedLimit(C.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IoCounters), ('ProcessMemoryLimit', C.c_size_t), ('JobMemoryLimit', C.c_size_t), ('PeakProcessMemoryUsed', C.c_size_t), ('PeakJobMemoryUsed', C.c_size_t)]

def make_job():
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [C.c_void_p, W.LPCWSTR]
    kernel.CreateJobObjectW.restype = W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    kernel.SetInformationJobObject.restype = W.BOOL
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.AssignProcessToJobObject.restype = W.BOOL
    kernel.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise C.WinError(C.get_last_error())
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel.SetInformationJobObject(handle, 9, C.byref(limits), C.sizeof(limits)):
        kernel.CloseHandle(handle)
        raise C.WinError(C.get_last_error())
    return kernel, handle

def read_gpu():
    fields = "name,uuid,pci.bus_id,power.limit,power.draw,temperature.gpu,fan.speed,utilization.gpu,clocks.current.graphics,clocks.current.memory"
    p = subprocess.run([str(SMI), "-i", "0", f"--query-gpu={fields}", "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True, creationflags=0x08000000)
    row = next(csv.reader([p.stdout.strip()]))
    keys = ["顯卡", "識別碼", "匯流排", "功耗上限W", "顯卡功耗W", "核心溫度C", "風扇百分比", "GPU使用率", "核心時脈MHz", "顯存時脈MHz"]
    data = dict(zip(keys, [x.strip() for x in row]))
    for key in keys[3:]:
        data[key] = float(data[key])
    return data
