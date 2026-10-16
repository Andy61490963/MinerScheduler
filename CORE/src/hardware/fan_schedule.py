"""依啟動時核准的台灣排程設定兩組顯卡風扇，保留切換及溫度保護。"""
from datetime import datetime, timezone
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
from TEST_顯卡讀值 import ReadOnlyGpu
from TEST_功耗排程 import Nvidia, halt_miner
from TEST_排程規則 import TAIPEI, FAN_LIMIT, DAY_FAN, NIGHT_FAN, DAY_POWER, daytime, fan_check, temporary_trial
from TEST_啟動核對 import read_shared_text

STATUS=ROOT/'TEST_風扇排程狀態.json'
CONFIGS=ROOT/'TEST_風扇/Configurations'
DAY=CONFIGS/'TEST_尖峰風扇70.json'
QUIET=CONFIGS/'TEST_離峰風扇100.json'


def read_json(path):
    """允許Windows原子替換狀態檔，短暫共用衝突先重試。"""
    for attempt in range(10):
        try:
            return json.loads(read_shared_text(path))
        except (OSError,ValueError):
            if attempt==9:
                raise
            time.sleep(.02)


def baseline_ready(gpu,guard):
    """必須已降回150W與900MHz，才能切換離峰低風扇。"""
    return (type(gpu.get('功耗上限W')) in (int,float)
            and abs(gpu['功耗上限W']-150)<=.6
            and type(gpu.get('核心時脈MHz')) in (int,float)
            and 0<=gpu['核心時脈MHz']<=915
            and guard.get('最高核心時脈MHz')==900
            and guard.get('狀態') in ('排程保護已就緒','已停止'))


def profile_for(active,gpu,guard):
    if not active:
        return QUIET
    # 短測仍要求既有核准風扇，僅接受有效且有時限的保護心跳。
    trial = temporary_trial({'臨時功耗短測':guard.get('臨時功耗短測')})
    if trial is not None and guard.get('風扇目標百分比') == DAY_FAN:
        return DAY if (gpu.get('功耗上限W',999) <= trial['功耗W'] + .6
                       and guard.get('目標功耗上限W',999) <= trial['功耗W']) else None
    # 夜轉日先確認硬體功耗已回到一般日間範圍，才允許切換風扇。
    return DAY if (gpu.get('功耗上限W',999) <= DAY_POWER + .6
                   and guard.get('目標功耗上限W',150) <= DAY_POWER) else None


def confirmed(active,sample):
    fan_check(sample)
    expected=DAY_FAN if active else NIGHT_FAN
    return all(item['實際百分比']==expected for item in sample['風扇']) and all(
        item['設定百分比']==expected and item['模式']=='手動' for item in sample['控制'])


def load_profile(path):
    """只使用FanControl官方設定檔載入介面，不切回顯卡原廠無上限自動模式。"""
    config=read_json(path)
    controls=[item for item in config['FanControl']['Controls'] if item['Identifier'].startswith('NVApiWrapper/0-AD102-A/control/')]
    if len(controls)!=2:
        raise RuntimeError('設定檔無法確認兩組顯卡風扇。')
    if path in (DAY,QUIET):
        expected=DAY_FAN if path==DAY else NIGHT_FAN
        if any(not item['Enable'] or not item['ManualControl'] or item['ManualControlValue']!=expected or item.get('SelectedOffset',0)!=0 for item in controls):
            raise RuntimeError('正式設定檔不是預期的兩組固定風扇百分比。')
    if any(not 0<=item.get('MinimumPercent',0)<=FAN_LIMIT or (item.get('ManualControl') and not 0<=item.get('ManualControlValue',999)<=FAN_LIMIT) for item in controls):
        raise RuntimeError('設定檔超過目前核准的100%風扇上限。')
    if any(not item.get('ManualControl') for item in controls):
        curves=config['FanControl']['FanCurves']
        for item in controls:
            if item.get('ManualControl'):
                continue
            name=item['SelectedFanCurve']['Name']
            curve=next(curve for curve in curves if curve['Name']==name)
            points=[float(point.split(',')[1]) for point in curve['Points']]
            if not item['Enable'] or item['ManualControl'] or item.get('SelectedOffset',0)!=0 or curve['MaximumCommand']>FAN_LIMIT or any(not 0<=value<=FAN_LIMIT for value in points):
                raise RuntimeError('白天曲線超出已核准的70%範圍。')
    log=ROOT/'TEST_風扇切換輸出.log'
    with log.open('ab') as output:
        result=subprocess.run([str(ROOT/'TEST_風扇/FanControl.exe'),'-c',str(path),'-m'],
                              cwd=ROOT/'TEST_風扇',stdout=output,stderr=subprocess.STDOUT,
                              timeout=8,creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError('FanControl設定切換失敗。')


def save(state):
    state['更新時間UTC']=datetime.now(timezone.utc).isoformat()
    state['台灣時間']=datetime.now(TAIPEI).isoformat()
    temporary=STATUS.with_suffix('.風扇.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    for attempt in range(40):
        try:
            temporary.replace(STATUS)
            break
        except PermissionError:
            if attempt==39:
                raise
            time.sleep(.005)


def main():
    with (ROOT/'TEST_風扇排程鎖.dat').open('a+b') as lock:
        if lock.tell()==0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            return
        state={'狀態':'啟動中','程序編號':os.getpid(),'模式':'','原因':''}
        reader=device=None
        previous=None
        applied=None
        pending=None
        next_attempt=next_save=0
        while True:
            now=time.monotonic()
            active=daytime()
            try:
                if reader is None:
                    reader=ReadOnlyGpu()
                if device is None:
                    device=Nvidia()
                sample=reader.read()
                fan_check(sample)
                gpu=device.read(sample)
                guard=read_json(ROOT/'TEST_功耗狀態.json')
                if previous!=active:
                    previous=active
                    applied=None
                    pending=now
                    next_attempt=0
                desired=profile_for(active,gpu,guard)
                matches=confirmed(active,sample)
                state.update({'模式':f'兩組全天固定{DAY_FAN}%；主機板既有控制保留',
                              '目標風扇百分比':DAY_FAN if active else NIGHT_FAN,'功耗切換完成':desired is not None,
                              '切換等待秒數':round(now-pending,3) if pending is not None else 0,
                              '兩組風扇':sample['風扇'],'兩組風扇控制':sample['控制'],'顯卡':gpu,'原因':''})
                if desired is None:
                    state['狀態']=f'等待功耗先降至平日{DAY_POWER}W範圍'
                    if pending is not None and now-pending>10:
                        raise RuntimeError(f'夜轉日功耗未及時降至{DAY_POWER}W，停止負載並保留現有風扇。')
                elif applied!=desired or not matches:
                    if pending is None:
                        pending=now
                    state['狀態']='正在切換並核對兩組風扇'
                    if now>=next_attempt:
                        load_profile(desired)
                        applied=desired
                        next_attempt=time.monotonic()+3
                    # 設定載入本身需要時間，逾時前重新讀取，避免拿載入前的舊樣本誤停。
                    if time.monotonic()-pending>10 and not confirmed(active,reader.read()):
                        period='尖峰' if active else '離峰'
                        expected=DAY_FAN if active else NIGHT_FAN
                        actual=[item['實際百分比'] for item in sample['風扇']]
                        controls=[item['設定百分比'] for item in sample['控制']]
                        raise RuntimeError(f'兩組{period}風扇未能在十秒內固定{expected}%，回報{actual}、設定{controls}，停止負載。')
                else:
                    pending=None
                    state['狀態']='風扇排程運作中'
                if now>=next_save:
                    save(state)
                    next_save=now+.5
            except Exception as exc:
                reason='風扇排程保護：'+str(exc)
                if not (ROOT/'TEST_停止.flag').exists():
                    halt_miner(reason)
                state.update({'狀態':'停止負載並等待風扇設定恢復','原因':reason})
                save(state)
                time.sleep(1)
            time.sleep(.1)


if __name__=='__main__':
    main()
