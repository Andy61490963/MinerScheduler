"""使用官方功耗介面執行平日排程；不取得任何風扇寫入介面。"""
import argparse
from collections import deque
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timedelta, timezone
import json
import msvcrt
import os
from pathlib import Path
import sys
import threading
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
from TEST_顯卡讀值 import ReadOnlyGpu
from TEST_排程規則 import BASE, FAN_LIMIT, SETTLE, CORE_RAISE, CORE_BACKOFF, CLOCK_STEP, TAIPEI, daytime, decide, fan_check, stable_temperatures, raise_margin, clock_reading_ok, period_profile, mining_allowed, temporary_trial, trial_raise_allowed, configured_target, transition_profile
from TEST_啟動核對 import read_shared_text
from TEST_執行工具 import EXPECTED as EXPECTED_MINER
from TEST_顯存節能 import MemoryPolicy
from TEST_即時通道 import ChannelWriter, BackgroundFileWriter, CachedJson, read_channel

STATUS = ROOT / 'TEST_功耗狀態.json'
STOP = ROOT / 'TEST_停止.flag'
EXPECTED = 'GPU-__LOCAL_UUID__'


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, data):
    temporary = path.with_suffix('.排程.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(100):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(.005)


class Utilization(C.Structure):
    _fields_ = [('gpu', C.c_uint), ('memory', C.c_uint)]


class Nvidia:
    # 只允許讀取、功耗與核心限制及810MHz顯存節能；不提供風扇或電壓寫入功能。
    ALLOWED = {
        'nvmlInit_v2', 'nvmlShutdown', 'nvmlDeviceGetHandleByIndex_v2',
        'nvmlDeviceGetUUID', 'nvmlDeviceGetPowerManagementLimit',
        'nvmlDeviceGetPowerManagementDefaultLimit', 'nvmlDeviceGetPowerManagementLimitConstraints',
        'nvmlDeviceGetPowerUsage', 'nvmlDeviceGetClockInfo', 'nvmlDeviceGetMaxClockInfo', 'nvmlDeviceGetUtilizationRates',
        'nvmlDeviceSetPowerManagementLimit', 'nvmlDeviceSetGpuLockedClocks',
        'nvmlDeviceSetMemoryLockedClocks', 'nvmlDeviceResetMemoryLockedClocks',
    }

    def __init__(self):
        self.dll = C.CDLL(str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/nvml.dll'))
        self.call('nvmlInit_v2', [], [])
        self.handle = C.c_void_p()
        self.call('nvmlDeviceGetHandleByIndex_v2', [C.c_uint, C.POINTER(C.c_void_p)], [0, C.byref(self.handle)])
        value = C.create_string_buffer(96)
        self.call('nvmlDeviceGetUUID', [C.c_void_p, C.c_char_p, C.c_uint], [self.handle, value, 96])
        if value.value.decode() != EXPECTED:
            raise RuntimeError('顯卡識別碼不符，不調整硬體。')
        low, high = C.c_uint(), C.c_uint()
        self.call('nvmlDeviceGetPowerManagementLimitConstraints', [C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_uint)], [self.handle, C.byref(low), C.byref(high)])
        if low.value > BASE * 1000 or high.value < BASE * 1000:
            raise RuntimeError('顯卡不支援150W基準。')
        self.ceiling = min(450, high.value // 1000, self.number('nvmlDeviceGetPowerManagementDefaultLimit') // 1000)
        maximum = C.c_uint()
        self.call('nvmlDeviceGetMaxClockInfo', [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)], [self.handle, 0, C.byref(maximum)])
        self.maximum_clock = maximum.value
        self._cached = None
        self._cache_until = 0

    def call(self, name, types, args):
        if name not in self.ALLOWED:
            raise RuntimeError('拒絕取得未核准的顯卡寫入介面。')
        function = getattr(self.dll, name)
        function.argtypes, function.restype = types, C.c_int
        begin = time.monotonic()
        result = function(*args)
        elapsed = time.monotonic() - begin
        if elapsed > .5:
            try:
                with (ROOT/'TEST_顯卡介面延遲.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps({'時間UTC':utc(),'介面':name,'等待秒數':round(elapsed,3)},ensure_ascii=False)+'\n')
            except OSError:
                pass
        if result:
            raise RuntimeError(f'顯卡介面{name}失敗，代碼{result}。')

    def number(self, name):
        value = C.c_uint()
        self.call(name, [C.c_void_p, C.POINTER(C.c_uint)], [self.handle, C.byref(value)])
        return value.value

    def read(self, sample):
        # 功耗及時脈最多每半秒查詢一次；風扇與溫度仍使用每次新的NVAPI讀值。
        if self._cached is None or time.monotonic() >= self._cache_until:
            clock, memory, utilization = C.c_uint(), C.c_uint(), Utilization()
            self.call('nvmlDeviceGetClockInfo', [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)], [self.handle, 0, C.byref(clock)])
            self.call('nvmlDeviceGetClockInfo', [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)], [self.handle, 2, C.byref(memory)])
            self.call('nvmlDeviceGetUtilizationRates', [C.c_void_p, C.POINTER(Utilization)], [self.handle, C.byref(utilization)])
            self._cached = {'功耗上限W':self.number('nvmlDeviceGetPowerManagementLimit')/1000,
                            '功耗W':self.number('nvmlDeviceGetPowerUsage')/1000,
                            '使用率':utilization.gpu,'核心時脈MHz':clock.value,'顯存時脈MHz':memory.value}
            self._cache_until = time.monotonic() + .5
        return {
            **self._cached,
            '核心C': sample['核心C'], '熱點C': sample['熱點C'], '顯存C': sample['顯存C'],
            '風扇百分比': max(item['實際百分比'] for item in sample['風扇']),
        }

    def power(self, watts):
        if not BASE <= watts <= self.ceiling:
            raise RuntimeError('功耗限制超出150W至原廠額定範圍。')
        self.call('nvmlDeviceSetPowerManagementLimit', [C.c_void_p, C.c_uint], [self.handle, int(watts * 1000)])
        if abs(self.number('nvmlDeviceGetPowerManagementLimit') / 1000 - watts) > .6:
            raise RuntimeError('功耗限制未通過讀回驗證。')
        self._cached = None

    def clocks(self, maximum):
        if not isinstance(maximum, int) or not 900 <= maximum <= self.maximum_clock:
            raise RuntimeError('核心時脈限制超出900MHz至原廠上限。')
        self.call('nvmlDeviceSetGpuLockedClocks', [C.c_void_p, C.c_uint, C.c_uint], [self.handle, 210, maximum])
        self._cached = None


    def memory_clocks(self, maximum):
        """僅允許810MHz降頻，或還原驅動預設顯存時脈。"""
        if maximum is None:
            self.call('nvmlDeviceResetMemoryLockedClocks', [C.c_void_p], [self.handle])
        elif type(maximum) is int and maximum == 810:
            self.call('nvmlDeviceSetMemoryLockedClocks', [C.c_void_p, C.c_uint, C.c_uint], [self.handle, 810, 810])
        else:
            raise ValueError('顯存節能僅允許810MHz或還原。')
        self._cached = None


def halt_miner(reason):
    STOP.write_text(reason, encoding='utf-8')
    # 停止標記由一般權限監控處理；讀值失效時另直接停止指定的既有負載。
    try:
        state = json.loads((ROOT / 'TEST_Status.json').read_text(encoding='utf-8-sig'))
        pid = state.get('礦工PID')
        if not isinstance(pid, int) or pid <= 0:
            return
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        kernel.OpenProcess.restype = W.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        kernel.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
        kernel.CloseHandle.argtypes = [W.HANDLE]
        handle = kernel.OpenProcess(0x1001, False, pid)
        if handle:
            try:
                buffer, size = C.create_unicode_buffer(32768), W.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size)) and Path(buffer.value).resolve() == (ROOT / 'TEST_Core/TEST.exe').resolve():
                    kernel.TerminateProcess(handle, 1)
            finally:
                kernel.CloseHandle(handle)
    except Exception:
        pass


class Watcher:
    def __init__(self):
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.data, self.updated, self.error = None, time.monotonic(), ''
        self.maximum_gap = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            reader, previous = ReadOnlyGpu(), time.monotonic()
            while not self.stop.is_set():
                begin = time.monotonic()
                sample = reader.read()
                fan_check(sample)
                now = time.monotonic()
                if now - previous > 1:
                    raise RuntimeError('風扇監控中斷超過一秒，停止負載。')
                with self.lock:
                    self.data, self.updated = sample, now
                    self.maximum_gap = max(self.maximum_gap, now - previous)
                previous = now
                self.stop.wait(max(0, .05 - (time.monotonic() - begin)))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            halt_miner(str(exc))

    def snapshot(self):
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if time.monotonic() - self.updated > 1:
                raise RuntimeError('兩組風扇讀值超過一秒未更新，停止負載。')
            return self.data


def load_ready(identity,data=None):
    """一分鐘核對只用於負載狀態及顯存節能，不覆寫保存的起始功耗。"""
    try:
        data=data if data is not None else json.loads(read_shared_text(ROOT/'TEST_Status.json'))
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(data['更新時間UTC'])).total_seconds()
        verified=(data.get('經過秒數',0)>=60 and data.get('礦工控制已關閉') is True
                  and data.get('CPU挖礦') is False and data.get('礦工SHA256')==EXPECTED_MINER)
        return bool(0<=age<4 and data.get('保護識別碼')==identity and verified
                    and data.get('狀態','').startswith('挖礦中'))
    except (OSError,ValueError,KeyError,TypeError):
        return False


def load_available(identity, data, seen):
    """尚未建立負載時允許設定功耗上限；已有負載失聯或停止時回到保護待機。"""
    if data is None or data.get('保護識別碼') != identity:
        return not seen
    try:
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(data['更新時間UTC'])).total_seconds()
        return bool(0 <= age < 4 and data.get('CPU挖礦') is False
                    and data.get('礦工SHA256') == EXPECTED_MINER
                    and data.get('狀態','').startswith('挖礦中'))
    except (ValueError, KeyError, TypeError):
        return False


def read_profile(maximum_clock):
    """正式排程最多原廠450W，沒有全速試驗或暫停溫度門檻的模式。"""
    config=json.loads(read_shared_text(ROOT/'TEST_設定.json'))['功耗排程']
    watts=config.get('平日固定功耗上限W')
    clock=config.get('平日固定核心時脈上限MHz')
    if type(watts) is not int or not BASE<=watts<=450:
        raise RuntimeError('正式平日功耗上限必須介於150W與450W。')
    if type(clock) is not int or not 900<=clock<=maximum_clock:
        raise RuntimeError('正式核心時脈範圍失效。')
    return watts,clock


def lower_profile(current_profile):
    """執行中只接受降低功耗的設定；不改時脈、不接受升瓦或測試模式切換。"""
    if current_profile is None:
        return None
    config=json.loads(read_shared_text(ROOT/'TEST_設定.json'))['功耗排程']
    watts=config.get('平日固定功耗上限W')
    clock=config.get('平日固定核心時脈上限MHz')
    if config.get('快速降瓦測試') is True:
        raise RuntimeError('執行中不可切換快速測試模式。')
    if type(watts) is not int or not BASE<=watts<=450 or type(clock) is not int or clock!=current_profile[1]:
        raise RuntimeError('執行中功耗設定失效或核心時脈設定改變，停止負載。')
    return (watts,clock) if watts<current_profile[0] else None


def main(read_only=False, seconds=10):
    if read_only:
        device, reader = Nvidia(), ReadOnlyGpu()
        end = time.monotonic() + seconds
        highest = 0
        while time.monotonic() < end:
            sample = reader.read()
            fan_check(sample)
            highest = max(highest, *(item['實際百分比'] for item in sample['風扇']))
            data = device.read(sample)
            time.sleep(.05)
        print(json.dumps({'純讀核對通過': True, '最高風扇百分比': highest, '台灣時間': datetime.now(TAIPEI).isoformat(), '目前為平日提高功耗時段': daytime(), '原廠功耗上限W': device.ceiling, '顯卡': data}, ensure_ascii=False))
        return
    if not C.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('設定功耗需要Windows管理員權限，未改變顯卡設定。')
    with (ROOT / 'TEST_功耗鎖.dat').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        device = watcher = memory_policy = None
        state = {'狀態': '檢查中', '執行識別碼': str(uuid.uuid4()), '程序編號': os.getpid(), '原因': '', '背景執行': True, '模式': '平日排程，70%風扇優先', '最高核心時脈MHz': 900, '原始功耗上限W': 450, '目標功耗上限W': BASE, '風扇目標百分比': FAN_LIMIT}
        history, current, clock_limit = deque(), BASE, 900
        last_change, next_save, cooling_until = time.monotonic(), 0, 0
        ceiling, previous_active = BASE, False
        previous_clock_limit, clock_until = 900, 0
        live_channel=ChannelWriter('guard')
        state_writer=BackgroundFileWriter(lambda data:atomic(STATUS,data))
        config_cache=None
        event_writer=None

        def apply_power(watts):
            nonlocal last_change
            previous_power = current
            device.power(watts)
            last_change = time.monotonic()
            if watts < previous_power:
                state['前次功耗上限W'] = previous_power
                state['功耗切換期限UTC'] = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()

        def apply_clocks(maximum):
            nonlocal clock_limit, previous_clock_limit, clock_until
            previous_clock_limit = clock_limit
            device.clocks(maximum)
            clock_limit = maximum
            clock_until = time.monotonic() + 2
            state['前次最高核心時脈MHz'] = previous_clock_limit
            state['核心時脈切換期限UTC'] = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()

        def save():
            state['更新時間UTC'] = utc()
            state['台灣時間'] = datetime.now(TAIPEI).isoformat()
            disk=state_writer.snapshot()
            state['紀錄寫入提示']=disk['錯誤']
            state['紀錄寫入延遲秒數']=round(max(0,time.monotonic()-disk['更新時間']),2)
            live_channel.publish(state)
            state_writer.submit(state)

        def write_event(data):
            path = ROOT / 'TEST_排程紀錄.jsonl'
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.replace(ROOT / 'TEST_排程紀錄.前段.jsonl')
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(data,ensure_ascii=False)+'\n')

        event_writer=BackgroundFileWriter(write_event)
        def event(reason):
            event_writer.submit({'台灣時間':datetime.now(TAIPEI).isoformat(),'原因':reason,'目標功耗上限W':current,'最高核心時脈MHz':clock_limit,'當日可嘗試上限W':ceiling})

        try:
            device = Nvidia()
            device.power(BASE)
            device.clocks(900)
            if STOP.exists():
                raise RuntimeError('已有停止標記，維持150W及900MHz，不自行恢復負載。')
            config_cache=CachedJson(lambda:json.loads(read_shared_text(ROOT/'TEST_設定.json')))
            schedule = config_cache.snapshot()['功耗排程']
            # 啟動前同時驗證兩種時段，不讓錯誤設定到切換時才生效。
            for peak in (True, False):
                period_profile(schedule, peak, device.maximum_clock, device.ceiling)
            memory_target = schedule.get('尖峰顯存時脈MHz')
            memory_policy = MemoryPolicy(device, memory_target)
            device.memory_clocks(None)
            state['尖峰顯存時脈MHz'] = memory_target
            powershell = str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe')
            environment = os.environ.copy()
            environment['PSModulePath'] = str(Path(powershell).parent/'Modules')+';C:/Program Files/WindowsPowerShell/Modules'
            with (ROOT/'TEST_排程同步輸出.log').open('wb') as output:
                synchronized = subprocess.run([powershell,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'TEST_同步排程時間.ps1')],cwd=ROOT,env=environment,stdout=output,stderr=subprocess.STDOUT,creationflags=0x08000000,timeout=15)
            if synchronized.returncode:
                raise RuntimeError('排程同步失敗，保留150W及900MHz。')
            watcher = Watcher()
            sample = None
            while sample is None:
                sample = watcher.snapshot()
                time.sleep(.01)
            state.update({'狀態':'排程保護已就緒','原廠功耗上限W':device.ceiling,
                          '原廠最高核心時脈MHz':device.maximum_clock,
                          '功耗控制方式':'依正式設定控制各時段功耗，風扇確認及溫度保護優先'})
            previous_peak = None
            profile = None
            seen_miner = False
            while not STOP.exists():
                now = time.monotonic()
                sample = watcher.snapshot()
                fan_check(sample)
                data = device.read(sample)
                if abs(data['功耗上限W']-current) > .6:
                    raise RuntimeError('其他程式改變功耗限制，停止負載。')
                if not clock_reading_ok(data['核心時脈MHz'],clock_limit,previous_clock_limit,max(0,clock_until-now)):
                    raise RuntimeError('核心時脈在切換期限後仍超過設定，停止負載。')
                peak = daytime()
                wanted = period_profile(schedule,peak,device.maximum_clock,device.ceiling)
                if previous_peak != peak:
                    # 先回到150W及900MHz，再讓風扇與新時段同步；不得高瓦降低風扇。
                    if current != BASE:
                        apply_power(BASE)
                        current = BASE
                    if clock_limit != 900:
                        apply_clocks(900)
                    # 跨時段不可直接解除上一時段的熱保護；必要時從150W逐步回升。
                    ceiling, cooling_until = transition_profile(
                        profile, wanted, ceiling, cooling_until, now)
                    profile = wanted
                    previous_peak = peak
                    event(f"切換{'平日' if peak else '離峰'}{profile[0]}W及{profile[2]}%風扇。")
                # 執行中接受降瓦，升瓦或核心／風扇變更需重新啟動核對。
                fresh = config_cache.snapshot()['功耗排程']
                updated = period_profile(fresh,peak,device.maximum_clock,device.ceiling)
                if updated[1:] != profile[1:]:
                    raise RuntimeError('執行中不可改變核心與風扇設定。')
                if updated[0] > profile[0]:
                    if not trial_raise_allowed(schedule,fresh,peak,profile[0],updated[0],ceiling):
                        raise RuntimeError('僅接受本次授權逐檔步幅，過熱降瓦後不得升瓦。')
                    profile = updated
                    ceiling = profile[0]
                if updated[0] < profile[0]:
                    profile = updated
                    ceiling = min(ceiling,profile[0])
                allowed = mining_allowed(fresh)
                miner_live=read_channel('miner')
                available = load_available(state['執行識別碼'], miner_live, seen_miner)
                if not allowed:
                    seen_miner = False
                elif miner_live is not None and miner_live.get('保護識別碼') == state['執行識別碼']:
                    seen_miner = True
                ready = load_ready(state['執行識別碼'],miner_live) if miner_live is not None else False
                fans_ready = all(item['實際百分比']==profile[2] and item.get('RPM',0)>0 for item in sample['風扇']) and all(
                    item['設定百分比']==profile[2] and item['模式']=='手動' for item in sample['控制'])
                memory_policy.update(allowed,ready,data,now)
                target, next_clock, ceiling, cooling_until, reason = configured_target(
                    profile,current,ceiling,cooling_until,now,sample,allowed,fans_ready,available,
                    last_change_at=last_change)
                if target != current:
                    apply_power(target)
                    current = target
                if next_clock != clock_limit:
                    apply_clocks(next_clock)
                state.update({'目標功耗上限W':current,'最高核心時脈MHz':clock_limit,
                    '平日提高功耗時段':peak,'臨時功耗短測':temporary_trial(fresh),'負載時段允許':allowed,'時段功耗上限W':profile[0],
                    '風扇目標百分比':profile[2],'持續負載核對通過':ready,'當日可嘗試上限W':ceiling,
                    '原因':reason,'兩組風扇':sample['風扇'],'兩組風扇控制':sample['控制'],
                    '顯存節能已套用':memory_policy.applied,'風扇監控最長間隔秒數':round(watcher.maximum_gap,4)})
                state['顯卡'] = device.read(sample)
                if now >= next_save:
                    save()
                    next_save = now+.5
                time.sleep(.1)
            state['原因'] = '收到停止指令，保留150W及900MHz。'
        except Exception as exc:
            state['原因'] = str(exc)
            halt_miner(str(exc))
        finally:
            if watcher:
                watcher.stop.set()
            # 無論停止原因，都先還原顯存，再保留低功耗與既有風扇。
            if memory_policy:
                try:
                    memory_policy.restore()
                    state['顯存節能已套用'] = False
                    state['顯存降頻已還原'] = True
                except Exception as exc:
                    state['顯存還原錯誤'] = str(exc)
            # 不提高風扇，也不還原高功耗。
            if device:
                try:
                    device.power(BASE)
                    device.clocks(900)
                except Exception as exc:
                    state['回復基準錯誤'] = str(exc)
            state.update({'狀態': '已停止', '目標功耗上限W': BASE, '最高核心時脈MHz': 900})
            save()
            event(state['原因'])
            if config_cache:
                config_cache.stop.set()
            # 停止心跳已先發布；等候寫入不會延後硬體回到安全基準。
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and not state_writer.queue.empty():
                time.sleep(.02)
            state_writer.stop.set()
            event_writer.stop.set()
            live_channel.close()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='台灣功耗排程，依核准風扇設定與溫度保護運行。')
    parser.add_argument('--read-only', action='store_true', help='僅讀取核對，不調整任何硬體。')
    parser.add_argument('--seconds', type=int, default=10, help='純讀核對秒數。')
    options = parser.parse_args()
    main(options.read_only, options.seconds)
