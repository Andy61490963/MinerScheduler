"""每秒唯讀監測兩組風扇，保留轉速突升事件供定期通知。"""
from collections import deque
from datetime import datetime, timezone
import json
import math
import msvcrt
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from TEST_風扇排程 import read_json
from TEST_排程規則 import TAIPEI, FAN_LIMIT, DAY_FAN, NIGHT_FAN, daytime

STATE = ROOT / 'TEST_風扇監測狀態.json'
CURSOR = ROOT / 'TEST_風扇監測已讀.json'
PEAK_RPM = 2800
QUIET_RPM = 2800


def now():
    return datetime.now(timezone.utc)


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.01)


def load_previous(path, fallback):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return fallback


def run():
    with (ROOT / 'TEST_風扇監測鎖.dat').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        previous = load_previous(STATE, {})
        events = deque(previous.get('事件', []), maxlen=50)
        sequence = previous.get('事件序號', 0)
        active = {}
        pending = {}
        last_mode = None
        grace_end = time.monotonic() + 5
        latest = {}

        def event(kind, details):
            nonlocal sequence
            sequence += 1
            moment = now()
            events.append({'序號': sequence, '類型': kind,
                           '時間UTC': moment.isoformat(),
                           '台灣時間': moment.astimezone(TAIPEI).isoformat(), **details})

        while True:
            began = time.monotonic()
            moment = now()
            peak = daytime(moment)
            target = DAY_FAN if peak else NIGHT_FAN
            threshold = max(PEAK_RPM if peak else QUIET_RPM, target * 32 + 150)
            if last_mode is not None and peak != last_mode:
                grace_end = began + 10
            last_mode = peak
            issues = {}
            sample_valid = False
            try:
                sample = read_json(ROOT / 'TEST_風扇排程狀態.json')
                age = (moment - datetime.fromisoformat(sample['更新時間UTC'])).total_seconds()
                if not -2 <= age <= 5:
                    raise RuntimeError('風扇排程的感測資料超過五秒未更新。')
                fans, controls = sample['兩組風扇'], sample['兩組風扇控制']
                if len(fans) != 2 or len(controls) != 2:
                    raise RuntimeError('無法確認兩組風扇讀值。')
                for fan in fans:
                    rpm = fan['RPM']
                    if type(rpm) not in (int, float) or not math.isfinite(rpm) or rpm < 0:
                        raise RuntimeError('風扇實際轉速讀值無效。')
                    control = next(item for item in controls if item['編號'] == fan['編號'])
                    details = {'風扇編號': fan['編號'], 'RPM': rpm,
                               '設定百分比': control.get('設定百分比'),
                               '回報百分比': fan.get('實際百分比'),
                               '目標百分比': target,
                               '功耗W': sample.get('顯卡', {}).get('功耗W'),
                               'GPU核心C': sample.get('顯卡', {}).get('核心C')}
                    if began >= grace_end:
                        if rpm >= threshold:
                            issues[f"轉速:{fan['編號']}"] = {
                                '問題': '實際轉速突升', '判定門檻RPM': threshold, **details}
                        if (control.get('設定百分比') != target
                                or fan.get('實際百分比') != target
                                or control.get('模式') != '手動'):
                            issues[f"控制:{fan['編號']}"] = {'問題': '固定風扇控制不符', **details}
                latest = {'台灣時間': moment.astimezone(TAIPEI).isoformat(),
                          '兩組風扇': fans, '兩組風扇控制': controls,
                          '顯卡': sample.get('顯卡', {}), '目標百分比': target}
                sample_valid = True
            except Exception as exc:
                issues['資料'] = {'問題': '風扇監測資料中斷', '原因': str(exc)}

            for key in list(pending):
                if key not in issues:
                    del pending[key]
            for key, details in issues.items():
                if key in active:
                    active[key]['持續秒數'] = round(began - active[key]['起點'], 1)
                    active[key]['最高RPM'] = max(active[key].get('最高RPM', 0), details.get('RPM', 0))
                    active[key]['目前'] = details
                else:
                    item = pending.setdefault(key, {'起點': began, '開始台灣時間': moment.astimezone(TAIPEI).isoformat(),
                                                    '最高RPM': details.get('RPM', 0)})
                    item['最高RPM'] = max(item['最高RPM'], details.get('RPM', 0))
                    if began - item['起點'] >= 2:
                        active[key] = {**item, '持續秒數': round(began - item['起點'], 1), '目前': details}
                        event('異常開始', {**details, '開始台灣時間': item['開始台灣時間'],
                                           '持續秒數': active[key]['持續秒數'], '最高RPM': item['最高RPM']})
                        del pending[key]
            for key in list(active):
                if key not in issues:
                    if key != '資料' and not sample_valid:
                        continue
                    item = active.pop(key)
                    event('異常恢復', {**item['目前'], '開始台灣時間': item['開始台灣時間'],
                                      '持續秒數': round(began - item['起點'], 1), '最高RPM': item['最高RPM']})
            report = {'狀態': '監測運作中', '更新時間UTC': now().isoformat(),
                      '取樣間隔秒數': 1, '持續判定秒數': 2,
                      '尖峰RPM門檻': PEAK_RPM, '離峰RPM門檻': QUIET_RPM,
                      '事件序號': sequence, '目前讀值': latest,
                      '持續異常': [{k: v for k, v in item.items() if k != '起點'} for item in active.values()],
                      '事件': list(events)}
            save(STATE, report)
            time.sleep(max(.05, 1 - (time.monotonic() - began)))


def check():
    cursor = load_previous(CURSOR, {'最後序號': 0, '監測中斷': False})
    try:
        state = read_json(STATE)
        age = (now() - datetime.fromisoformat(state['更新時間UTC'])).total_seconds()
        if not -2 <= age <= 10:
            raise RuntimeError('常駐風扇監測超過十秒未更新。')
        fresh = [item for item in state['事件'] if item['序號'] > cursor.get('最後序號', 0)]
        if cursor.get('監測中斷'):
            fresh.append({'類型': '監測程序恢復', '台灣時間': now().astimezone(TAIPEI).isoformat()})
        result = {'監測正常': True, '新事件': fresh, '持續異常': state['持續異常'],
                  '目前讀值': state['目前讀值']}
        cursor.update({'最後序號': state['事件序號'], '監測中斷': False})
    except Exception as exc:
        result = {'監測正常': False, '新事件': [] if cursor.get('監測中斷') else [
            {'類型': '監測程序中斷', '原因': str(exc), '台灣時間': now().astimezone(TAIPEI).isoformat()}]}
        cursor['監測中斷'] = True
    save(CURSOR, cursor)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    if sys.argv[1:] == ['--check']:
        check()
    elif not sys.argv[1:]:
        run()
    else:
        raise SystemExit('用法：不帶參數啟動常駐監測；--check 取得尚未讀取的事件。')
