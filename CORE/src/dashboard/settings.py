"""只修改核准的功耗與五組風扇設定；硬體交由原有排程及啟動核對。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import time
import uuid

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent
FILES = ('TEST_設定.json', 'TEST_風扇/Configurations/TEST_尖峰風扇70.json',
         'TEST_風扇/Configurations/TEST_離峰風扇100.json', 'TEST_風扇/Configurations/userConfig.json')
FIELDS = {'day_watts': (150, 450, 10), 'night_watts': (150, 450, 10),
          'gpu_fan': (50, 100, 1), 'board_fan': (30, 100, 1)}
LABELS = {'day_watts':'平日日間功耗','night_watts':'離峰與週末功耗','gpu_fan':'顯卡風扇','board_fan':'主機板風扇'}
GPU_IDS = {'NVApiWrapper/0-AD102-A/control/0', 'NVApiWrapper/0-AD102-A/control/1'}
BOARD_IDS = {'/lpc/it8696e/control/0', '/lpc/it8696e/control/1', '/lpc/it8696e/control/5'}


def read_bundle():
    from TEST_啟動核對 import read_shared_text
    return {name: read_shared_text(SOURCE / name) for name in FILES}


def revision(bundle):
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode('utf-8'))
        digest.update(bundle[name].encode('utf-8'))
    return digest.hexdigest()


def values_from(bundle):
    schedule = json.loads(bundle[FILES[0]])['功耗排程']
    return {'day_watts': schedule['平日固定功耗上限W'], 'night_watts': schedule['離峰固定功耗上限W'],
            'gpu_fan': schedule['尖峰風扇百分比'], 'board_fan': schedule['主機板風扇百分比']}


def public_settings():
    bundle = read_bundle()
    return {'values': values_from(bundle), 'revision': revision(bundle),
            'limits': {key: {'min': low, 'max': high, 'step': step} for key, (low, high, step) in FIELDS.items()}}


def prepare(payload):
    if not isinstance(payload, dict) or set(payload) != set(FIELDS) | {'revision'}:
        raise ValueError('設定欄位不符。')
    for key, (low, high, step) in FIELDS.items():
        value = payload[key]
        if type(value) is not int or not low <= value <= high or (value-low) % step:
            raise ValueError(f'{LABELS[key]}必須介於 {low}～{high}，每次 {step}。')
    bundle = read_bundle()
    if payload['revision'] != revision(bundle):
        raise ValueError('設定已被其他操作變更，請按「重新讀取」後再調整。')
    requested = {key: payload[key] for key in FIELDS}
    schedule = json.loads(bundle[FILES[0]])['功耗排程']
    unchanged = requested == values_from(bundle) and schedule['離峰風扇百分比'] == requested['gpu_fan']
    documents = {name: json.loads(content) for name, content in bundle.items()}
    day, night, gpu, board = (requested[key] for key in FIELDS)
    config = documents[FILES[0]]
    target = config['功耗排程']
    description = f'平日09:15至17:45為{day}W；離峰及週末{night}W；顯卡兩組{gpu}%；主機板三組{board}%；溫度保護優先。'
    config['運作模式'] = description
    target.update({'平日固定功耗上限W': day, '離峰固定功耗上限W': night, '其他時段功耗上限W': night,
                   '最高可嘗試功耗W': max(day, night), '尖峰風扇百分比': gpu, '離峰風扇百分比': gpu,
                   '主機板風扇百分比': board, '離峰逐檔測試': False,
                   '功耗控制方式': description + '核心80°C降瓦與82°C停止保留。', '挖礦時段說明': description,
                   '風扇控制': f'RTX 4090兩組固定{gpu}%；主機板Fan #1、#2、#6固定{board}%；保護優先。'})
    for name in FILES[1:]:
        controls = documents[name]['FanControl']['Controls']
        matched = set()
        for control in controls:
            identifier = control.get('Identifier')
            if identifier in GPU_IDS | BOARD_IDS:
                matched.add(identifier)
                percent = gpu if identifier in GPU_IDS else board
                unchanged = (unchanged and control.get('ManualControlValue') == percent and control.get('MinimumPercent') == percent
                             and control.get('ManualControl') is True and control.get('Enable') is True
                             and control.get('SelectedOffset', 0) == 0 and control.get('SelectedFanCurve') is None)
                control.update(Enable=True, ManualControl=True, ManualControlValue=percent,
                               MinimumPercent=percent, SelectedFanCurve=None, SelectedOffset=0, ForceApply=True)
        if matched != GPU_IDS | BOARD_IDS:
            raise ValueError('FanControl 設定無法確認兩組顯卡與三組主機板風扇。')
    return {'before': bundle, 'documents': documents, 'values': requested, 'unchanged': unchanged}


def atomic_write(path, content):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        for attempt in range(30):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(.03)
    finally:
        temporary.unlink(missing_ok=True)


def write_plan(plan):
    """只在負載及功耗背景已結束後呼叫；任何部分失敗均嘗試回復原檔。"""
    if revision(read_bundle()) != revision(plan['before']):
        raise ValueError('停止期間設定另有變更，已取消套用，保留停挖。')
    backup = SOURCE.parent / '備份' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_網頁設定_' + uuid.uuid4().hex[:8])
    for name, content in plan['before'].items():
        path = backup / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((SOURCE / name).read_bytes())
    written = []
    try:
        for name, document in plan['documents'].items():
            written.append(name)
            atomic_write(SOURCE / name, json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        errors = []
        for name in reversed(written):
            try:
                atomic_write(SOURCE / name, (backup / name).read_bytes())
            except OSError:
                errors.append(name)
        if errors:
            raise RuntimeError('部分設定無法回復，已保留停挖；備份：' + str(backup))
        raise
    return backup


def workers(mode):
    from actions import launcher_environment
    arguments = ['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe', '-NoLogo', '-NoProfile',
                 '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / '設定背景.ps1'), '-模式', mode]
    options = dict(cwd=SOURCE, env=launcher_environment(), timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if mode == '狀態':
        result = subprocess.run(arguments, capture_output=True, encoding='utf-8-sig', errors='replace', **options)
    else:
        # 常駐子程序可能繼承管線；背景啟動不可用 communicate 等待它們關閉輸出。
        with (ROOT / '設定背景.log').open('ab') as output:
            result = subprocess.run(arguments, stdout=output, stderr=subprocess.STDOUT, **options)
    if result.returncode:
        raise RuntimeError('背景排程操作失敗，請查看設定背景.log：' + (result.stderr or result.stdout or '')[-1200:])
    return json.loads(result.stdout) if mode == '狀態' else None


def wait_quiet(checkpoint):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        checkpoint()
        state = workers('狀態')
        if not state['mining'] and not state['guard_running']:
            return
        time.sleep(.5)
    raise RuntimeError('前一個負載或功耗背景尚未結束，取消套用並保留停挖。')


def verify_fans(values, checkpoint):
    from TEST_顯卡讀值 import ReadOnlyGpu
    from TEST_排程規則 import fan_check
    from actions import launcher_environment
    reader = ReadOnlyGpu()
    executable = shutil.which('pwsh') or 'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        checkpoint()
        sample = reader.read()
        fan_check(sample)
        gpu_ok = (len(sample['風扇']) == len(sample['控制']) == 2
                  and all(item['實際百分比'] == values['gpu_fan'] and item['RPM'] > 0 for item in sample['風扇'])
                  and all(item['設定百分比'] == values['gpu_fan'] and item['模式'] == '手動' for item in sample['控制']))
        result = subprocess.run([executable, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'board_fans.ps1')],
                                capture_output=True, encoding='utf-8-sig', timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                                env=launcher_environment())
        fans = json.loads(result.stdout).get('fans', []) if result.returncode == 0 else []
        board_ok = len(fans) == 3 and all(fan['percent'] == values['board_fan'] and (fan['rpm'] or 0) > 0 for fan in fans)
        if gpu_ok and board_ok:
            return
        time.sleep(1)
    raise RuntimeError('五組風扇未能達到設定，保留停挖；請查看風扇與操作 LOG。')


def verify_guard(values, checkpoint):
    from TEST_即時通道 import read_channel
    from TEST_排程規則 import daytime
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        checkpoint()
        data = read_channel('guard') or {}
        expected = values['day_watts' if daytime() else 'night_watts']
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(data['更新時間UTC'])).total_seconds()
        except (ValueError, KeyError, TypeError):
            age = 999
        power = data.get('目標功耗上限W')
        if (0 <= age <= 4 and data.get('狀態') == '排程保護已就緒' and data.get('持續負載核對通過') is True
                and data.get('時段功耗上限W') == expected and data.get('風扇目標百分比') == values['gpu_fan']):
            if type(power) in (int, float) and 150 <= power <= expected and abs(data.get('顯卡', {}).get('功耗上限W', 0)-power) <= .6:
                return power
        time.sleep(1)
    raise RuntimeError('新設定尚未通過持續負載核對，請查看保護與啟動 LOG。')
