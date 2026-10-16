"""依核准時段完成核對後挖礦，人工停止與本地模型協調優先。"""
import importlib
import json
import msvcrt
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import TEST_啟動核對 as startup
from TEST_排程規則 import mining_allowed, daytime
from TEST_風扇排程 import confirmed


def wait_peak_fans(reader,stop_path):
    """等待尖峰風扇切換完成，避免礦工核對與風扇升速同時進行。"""
    deadline=time.monotonic()+15
    while True:
        if stop_path.exists():
            raise RuntimeError('風扇就緒核對期間收到停止指令。')
        guard=startup.read_guard()
        config=json.loads(startup.read_shared_text(ROOT/'TEST_設定.json'))
        if not mining_allowed(config['功耗排程']):
            return False
        if confirmed(daytime(),reader.read()):
            return True
        if time.monotonic()>=deadline:
            raise RuntimeError('兩組風扇未達目前時段固定目標，未啟動礦工核對。')
        time.sleep(.1)


def wait_idle_after_check(reader,stop_path):
    """初始化可能短暫加熱顯卡；最多等待兩分鐘恢復原有閒置門檻。"""
    deadline=time.monotonic()+120
    while True:
        if stop_path.exists():
            raise RuntimeError('初始化後冷卻期間收到停止指令。')
        guard=startup.read_guard()
        config=json.loads(startup.read_shared_text(ROOT/'TEST_設定.json'))
        if not mining_allowed(config['功耗排程']):
            return False
        watts=startup.configured_power(guard,config)
        sample=reader.read()
        startup.check(sample)
        data=startup.helper.read_gpu()
        if abs(data['功耗上限W']-watts)>.6:
            raise RuntimeError('初始化後功耗上限與保存的時段設定不符，不開始挖礦。')
        if sample['核心C']<50 and data['核心溫度C']<50 and data['顯卡功耗W']<=50:
            return True
        if time.monotonic()>=deadline:
            raise RuntimeError('初始化後兩分鐘內未恢復低於50°C及50W的閒置條件。')
        time.sleep(.25)


def run_session(stop_path):
    """每次尖峰重新核對保護及官方礦工，硬體或程式錯誤不自動重試。"""
    print('等待功耗保護依保存的時段設定就緒，期間不啟動礦工。',flush=True)
    deadline=time.monotonic()+900
    while True:
        if stop_path.exists():
            raise RuntimeError('等待期間收到停止指令，取消啟動。')
        config=json.loads(startup.read_shared_text(ROOT/'TEST_設定.json'))
        if not mining_allowed(config['功耗排程']):
            return {'排程停止':True,'原因':'等待期間進入離峰。'}
        try:
            startup.configured_power(startup.read_guard(),config)
            break
        except (OSError,ValueError,KeyError,RuntimeError):
            if time.monotonic()>=deadline:
                raise RuntimeError('十五分鐘內功耗保護未達保存設定，未開始挖礦。')
            time.sleep(1)
    reader=startup.ReadOnlyGpu()
    cooling_deadline=time.monotonic()+120
    while reader.read()['核心C']>=50:
        startup.read_guard()
        if time.monotonic()>=cooling_deadline:
            raise RuntimeError('顯卡未在兩分鐘內冷卻到起始門檻，未開始挖礦。')
        time.sleep(1)
    if not mining_allowed(config['功耗排程']):
        return {'排程停止':True,'原因':'冷卻期間進入離峰。'}
    print('等待兩組風扇達目前時段的固定設定，期間不啟動礦工。',flush=True)
    if not wait_peak_fans(reader,stop_path):
        return {'排程停止':True,'原因':'風扇就緒核對期間進入離峰。'}
    print('核對官方礦工啟動，期間不提供挖礦工作。',flush=True)
    startup.run(25,quick=False)
    if not mining_allowed(config['功耗排程']):
        return {'排程停止':True,'原因':'啟動核對期間進入離峰。'}
    print('初始化核對通過，等待恢復低於50°C及50W的閒置條件。',flush=True)
    if not wait_idle_after_check(reader,stop_path):
        return {'排程停止':True,'原因':'初始化後冷卻期間進入離峰。'}
    runtime=importlib.import_module('TEST_Run')
    try:
        return runtime.main()
    except RuntimeError as exc:
        if str(exc)==runtime.SCHEDULE_END:
            return {'排程停止':True,'原因':str(exc)}
        raise


def run_schedule(stop_path):
    runtime=importlib.import_module('TEST_Run')
    waiting=False
    while not stop_path.exists():
        config=json.loads(startup.read_shared_text(ROOT/'TEST_設定.json'))
        if not mining_allowed(config['功耗排程']):
            if not waiting:
                print('離峰停止挖礦，等待台灣時間平日09:15。',flush=True)
            waiting=True
            runtime.save({'狀態':'離峰停止挖礦，等待平日09:15','原因':'僅於台灣時間平日09:15至17:45挖礦。',
                          '礦工PID':None,'監控PID':os.getpid(),'持續負載核對通過':False,'CPU挖礦':False})
            time.sleep(1)
            continue
        waiting=False
        try:
            result=run_session(stop_path)
        except Exception as exc:
            runtime.save({'狀態':'已停止','原因':str(exc),'礦工PID':None,
                          '監控PID':os.getpid(),'持續負載核對通過':False,'CPU挖礦':False})
            raise
        if not result or not result.get('排程停止'):
            raise RuntimeError((result or {}).get('原因') or '挖礦非排程停止，不自動重新啟動。')
    runtime.save({'狀態':'已停止','原因':'收到人工停止指令，不自行恢復挖礦。',
                  '礦工PID':None,'監控PID':os.getpid(),'持續負載核對通過':False,'CPU挖礦':False})


if __name__=='__main__':
    try:
        with (ROOT/'TEST_執行鎖.dat').open('a+b') as lock:
            if lock.tell()==0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            try:
                run_schedule(ROOT/'TEST_停止.flag')
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    except Exception as exc:
        print('未開始或已停止：'+str(exc),flush=True)
        raise SystemExit(1)
