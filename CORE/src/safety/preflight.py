"""在本機無工作礦池核對礦工啟動與結束；全程只讀取顯卡資料。"""
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from TEST_排程規則 import FAN_LIMIT, CORE_STOP, clock_reading_ok, power_reading_ok, period_profile, daytime, mining_allowed
import TEST_執行工具 as helper
from TEST_顯卡讀值 import ReadOnlyGpu
from TEST_即時通道 import read_channel

MINER=helper.MINER
GUARD=ROOT/'TEST_功耗狀態.json'


def read_shared_text(path):
    # 保留刪除共用權限，讓管理員程序可原子更新檔案，避免一般 open() 阻擋 Replace。
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[helper.W.LPCWSTR,helper.W.DWORD,helper.W.DWORD,C.c_void_p,helper.W.DWORD,helper.W.DWORD,helper.W.HANDLE]
    kernel.CreateFileW.restype=helper.W.HANDLE
    kernel.ReadFile.argtypes=[helper.W.HANDLE,C.c_void_p,helper.W.DWORD,C.POINTER(helper.W.DWORD),C.c_void_p]
    kernel.ReadFile.restype=helper.W.BOOL
    kernel.CloseHandle.argtypes=[helper.W.HANDLE]
    for attempt in range(11):
        handle=kernel.CreateFileW(str(path),0x80000000,7,None,3,0,None)
        if handle!=C.c_void_p(-1).value:
            break
        error=C.get_last_error()
        if error not in (2,5,32,33) or attempt==10:
            raise C.WinError(error)
        time.sleep(.002)
    try:
        buffer=C.create_string_buffer(65536)
        count=helper.W.DWORD()
        if not kernel.ReadFile(handle,buffer,65536,C.byref(count),None):
            raise C.WinError(C.get_last_error())
        if count.value==65536:
            raise RuntimeError('保護狀態檔超出預期大小。')
        return buffer.raw[:count.value].decode('utf-8-sig')
    finally:
        kernel.CloseHandle(handle)


def read_guard(identity=None,realtime=False):
    info=read_channel('guard')
    if realtime:
        if info is None:
            raise RuntimeError('功耗保護尚未就緒、已停止或資料過期。')
        return validate_guard(info,identity)
    if info is not None:
        return validate_guard(info,identity)
    for attempt in range(6):
        try:
            content=read_shared_text(GUARD)
            break
        except PermissionError:
            if attempt==5:
                raise
            time.sleep(.002)
    info=json.loads(content)
    return validate_guard(info,identity)


def validate_guard(info,identity=None):
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(info['更新時間UTC'])).total_seconds()
    if age>4 or age< -2 or info['狀態'] not in ('150W保護已就緒','排程保護已就緒'):
        raise RuntimeError('功耗保護尚未就緒、已停止或資料過期。')
    scheduled=info.get('模式')=='平日排程，70%風扇優先'
    target=info.get('目標功耗上限W',150) if scheduled else 150
    clock=info.get('最高核心時脈MHz')
    data=info['顯卡']
    if not isinstance(target,(int,float)) or not 150<=target<=min(450,info.get('原廠功耗上限W',450),info.get('時段功耗上限W',450)) or abs(data['功耗上限W']-target)>.6:
        raise RuntimeError('功耗限制與排程目標不符。')
    maximum=info.get('原廠最高核心時脈MHz',900) if scheduled else 900
    remaining=0
    previous=None
    if info.get('核心時脈切換期限UTC'):
        remaining=(datetime.fromisoformat(info['核心時脈切換期限UTC'])-datetime.now(timezone.utc)).total_seconds()
        previous=info.get('前次最高核心時脈MHz')
        if type(previous) is not int or not 900<=previous<=maximum:
            previous=None
    if not isinstance(clock,int) or not 900<=clock<=maximum or not clock_reading_ok(data.get('核心時脈MHz',9999),clock,previous,remaining):
        raise RuntimeError('核心時脈條件與排程不符。')
    if scheduled and not info.get('負載時段允許',True) and (target!=150 or clock!=900):
        raise RuntimeError('禁止挖礦時段必須維持150W及900MHz。')
    power_remaining = 0
    previous_power = None
    if info.get('功耗切換期限UTC'):
        power_remaining = (datetime.fromisoformat(info['功耗切換期限UTC']) - datetime.now(timezone.utc)).total_seconds()
        previous_power = info.get('前次功耗上限W')
    if not (type(data['風扇百分比']) in (int,float) and 0<=data['風扇百分比']<=FAN_LIMIT) or data['核心C']>=CORE_STOP or not power_reading_ok(data['功耗W'], target, previous_power, power_remaining):
        raise RuntimeError('風扇100%上限、溫度或功耗保護讀值不符合條件。')
    if identity is not None and info['執行識別碼']!=identity:
        raise RuntimeError('功耗保護程序已更換，停止。')
    if (ROOT/'TEST_停止.flag').exists():
        raise RuntimeError('收到停止指令。')
    return info


def policy_hashes():
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('TEST_啟動核對.py','TEST_即時通道.py','TEST_顯卡讀值.py','TEST_執行工具.py','TEST_設定.json','TEST_功耗保護.ps1','TEST_Run.py','TEST_入口.py','TEST_排程規則.py','TEST_功耗排程.py','TEST_顯存節能.py','TEST_同步排程時間.ps1','TEST_風扇排程.py','TEST_風扇監測.py','TEST_風扇/Configurations/TEST_尖峰風扇70.json','TEST_風扇/Configurations/TEST_離峰風扇100.json')}


def check(sample):
    from TEST_排程規則 import fan_check
    fan_check(sample)


def configured_power(guard, config=None):
    """啟動、冷卻與正式負載共用目前保存的時段功耗，不另訂150W起跑值。"""
    if config is None:
        config = json.loads(read_shared_text(ROOT/'TEST_設定.json'))
    schedule = config['功耗排程']
    if not mining_allowed(schedule):
        raise RuntimeError('目前時段不允許開始挖礦。')
    watts = period_profile(schedule, daytime(), guard['原廠最高核心時脈MHz'], guard['原廠功耗上限W'])[0]
    if guard['目標功耗上限W'] != watts or abs(guard['顯卡']['功耗上限W']-watts) > .6:
        raise RuntimeError('功耗保護尚未達到保存的時段設定，不開始挖礦。')
    return watts


def args(pool,log):
    config=json.loads((ROOT/'TEST_設定.json').read_text(encoding='utf-8-sig'))
    if config['收款地址']!='__TRC20_WALLET__':
        raise RuntimeError('收款地址與使用者要求不同。')
    if config.get('顯卡工作強度')!=1:
        raise RuntimeError('目前核准的靜音工作強度應為1。')
    return [str(MINER),'--algorithm','pearlhash','--disable-cpu','--disable-msr-tweaks','--disable-huge-pages','--disable-gpu-amd','--disable-gpu-intel','--gpu-id','0','--gpu-intensity','1','--enable-workers-ramp-up','--gpu-disable-oc','--gpu-sensors-disable','--disable-worker-watchdog','--pool',pool,'--wallet','USDT:'+config['收款地址']+'.quiet4090','--password','x','--log-file',str(log),'--log-file-mode','0']


def suspended_launch(arguments,output,kernel,job):
    process=subprocess.Popen(arguments,cwd=MINER.parent,stdout=output,stderr=subprocess.STDOUT,creationflags=0x08000004)
    if not kernel.AssignProcessToJobObject(job,helper.W.HANDLE(int(process._handle))):
        process.kill()
        raise C.WinError(C.get_last_error())
    ntdll=C.WinDLL('ntdll')
    resume=ntdll.NtResumeProcess
    resume.argtypes=[helper.W.HANDLE]
    resume.restype=C.c_long
    if resume(helper.W.HANDLE(int(process._handle)))<0:
        kernel.TerminateJobObject(job,1)
        raise RuntimeError('無法在程序保護就緒後繼續礦工。')
    return process


def run(seconds=25,quick=False):
    if C.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('礦工核對必須以一般使用者權限執行。')
    if not MINER.is_file() or hashlib.sha256(MINER.read_bytes()).hexdigest()!=helper.EXPECTED:
        raise RuntimeError('礦工與已核對的官方內容不符。')
    reader=ReadOnlyGpu()
    guard=read_guard()
    watts=configured_power(guard)
    import TEST_執行工具 as gpu
    if abs(gpu.read_gpu()['功耗上限W']-watts)>.6:
        raise RuntimeError('保存的時段功耗尚未驗證，不開始核對。')
    initial=reader.read()
    check(initial)
    if initial['核心C']>=50:
        raise RuntimeError('顯卡起始溫度過高。')
    server=socket.socket()
    server.bind(('127.0.0.1',0))
    server.listen(4)
    server.settimeout(.2)
    port=server.getsockname()[1]
    quit_event=threading.Event()
    received=[]

    def hold_jobs():
        while not quit_event.is_set():
            try:
                client,_=server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(.2)
                while not quit_event.is_set():
                    try:
                        packet=client.recv(65536)
                        if not packet:
                            break
                        if sum(map(len,received))<1048576:
                            received.append(packet)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    # 故意不回覆任何工作，也不連線到外部礦池。

    network=threading.Thread(target=hold_jobs,daemon=True)
    network.start()
    kernel,job=helper.make_job()
    process=None
    record={'模式':'只核對啟動與結束，不提供挖礦工作','外部礦池連線':False,'核對通過':False,'開始UTC':helper.utc(),'礦工SHA256':helper.EXPECTED,'功耗上限W':watts,'保護識別碼':guard['執行識別碼'],'保護程式SHA256':policy_hashes(),'最高實際風扇百分比':0,'最高設定風扇百分比':0,'樣本數':0,'樣本間隔秒數':.05}
    output=(ROOT/'TEST_啟動輸出.log').open('wb')
    events=(ROOT/'TEST_啟動風扇紀錄.jsonl').open('w',encoding='utf-8')
    log=ROOT/'TEST_Init.log'

    def sample(phase):
        read_guard(guard['執行識別碼'])
        data=reader.read()
        record['最高實際風扇百分比']=max(record['最高實際風扇百分比'],max(item['實際百分比'] for item in data['風扇']))
        record['最高設定風扇百分比']=max(record['最高設定風扇百分比'],max(item['設定百分比'] for item in data['控制']))
        record['樣本數']+=1
        record['最後讀值']=data
        events.write(json.dumps({'時間UTC':helper.utc(),'階段':phase,**data},ensure_ascii=False)+'\n')
        events.flush()
        check(data)

    try:
        sample('啟動前')
        arguments=args(f'stratum+tcp://127.0.0.1:{port}',log)
        process=suspended_launch(arguments,output,kernel,job)
        begin=time.monotonic()
        while time.monotonic()-begin<seconds:
            if process.poll() is not None:
                raise RuntimeError(f'核對期間礦工提早退出，代碼{process.returncode}。')
            sample('初始化與等待工作')
            if quick and received and log.exists():
                content=log.read_text(encoding='utf-8',errors='replace')
                if 'GPU sensors: disabled' in content and 'Cpu mining: disabled' in content:
                    break
            time.sleep(.05)
        content=log.read_text(encoding='utf-8',errors='replace')
        if 'GPU sensors: disabled' not in content or 'Cpu mining: disabled' not in content:
            raise RuntimeError('礦工未確認關閉GPU感測控制與CPU挖礦。')
        if 'GPU sensors thread started' in content or 'GPU temperature protection monitor thread started' in content:
            raise RuntimeError('礦工仍啟動不應存在的GPU控制監控流程。')
        if not received:
            raise RuntimeError('礦工未完成啟動並連線到本機核對入口。')
        kernel.TerminateJobObject(job,1)
        process.wait(timeout=5)
        after=time.monotonic()
        while time.monotonic()-after<5:
            sample('結束後')
            time.sleep(.05)
        record['核對通過']=True
        record['原因']='啟動、無工作等待及結束後，全部讀值均通過目前風扇與溫度保護。'
    except Exception as exc:
        record['原因']=str(exc)
        raise
    finally:
        kernel.TerminateJobObject(job,1)
        kernel.CloseHandle(job)
        if process and process.poll() is None:
            process.wait(timeout=5)
        quit_event.set()
        server.close()
        output.close()
        events.close()
        record['結束UTC']=helper.utc()
        (ROOT/'TEST_啟動核對結果.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(record,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    run()
