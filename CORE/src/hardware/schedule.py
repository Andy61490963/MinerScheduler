"""台灣排程、核准風扇目標與功耗決策；不存取硬體。"""
from datetime import datetime, timedelta, timezone
import math
import json
from pathlib import Path

TAIPEI = timezone(timedelta(hours=8), '台灣時間')
FAN_LIMIT = 100
def load_fan_targets():
    """啟動時固定讀取核准設定；變更後必須重新啟動並核對。"""
    schedule = json.loads((Path(__file__).resolve().parent / 'TEST_設定.json').read_text(encoding='utf-8-sig'))['功耗排程']
    values = tuple(schedule.get(key) for key in ('尖峰風扇百分比', '離峰風扇百分比', '平日固定功耗上限W'))
    if any(type(value) is not int or not 50 <= value <= 100 for value in values[:2]):
        raise RuntimeError('兩個時段的顯卡風扇必須介於50%與100%。')
    if type(values[2]) is not int or not 150 <= values[2] <= 450:
        raise RuntimeError('平日功耗設定超過核准範圍。')
    return values


DAY_FAN, NIGHT_FAN, DAY_POWER = load_fan_targets()
BASE = 150
STEP = 10
SETTLE = 30
CORE_RAISE = 78
CORE_BACKOFF = 80
CORE_STOP = 82
CLOCK_STEP = 60


def clock_reading_ok(actual, maximum, previous_maximum=None, remaining=0):
    """自己降時脈後最多兩秒允許先前範圍，過期仍超限就拒絕。"""
    if type(actual) not in (int,float) or not math.isfinite(actual) or actual<0:
        return False
    if actual<=maximum+15:
        return True
    return (type(previous_maximum) is int and previous_maximum>=maximum
            and type(remaining) in (int,float) and math.isfinite(remaining)
            and 0<remaining<=2 and actual<=previous_maximum+15)


def power_reading_ok(actual, target, previous_target=None, remaining=0):
    """自己降瓦後最多兩秒允許功耗平均讀值延遲，超時或超過原上限仍拒絕。"""
    if (type(actual) not in (int, float) or not math.isfinite(actual) or actual < 0
            or type(target) not in (int, float) or not math.isfinite(target)
            or not 150 <= target <= 450):
        return False
    if actual <= target + 25:
        return True
    return (type(previous_target) is int and target < previous_target <= 450
            and type(remaining) in (int, float) and math.isfinite(remaining)
            and 0 < remaining <= 2 and actual <= previous_target + 25)


def stable_temperatures(history, minimum_samples=150):
    """比較前後半段最高溫，短暫降溫或顯存讀值刻度不算升溫。"""
    if len(history) < minimum_samples:
        return False
    midpoint = (history[0][0] + history[-1][0]) / 2
    older = [row for row in history if row[0] <= midpoint]
    newer = [row for row in history if row[0] > midpoint]
    if not older or not newer:
        return False
    for index, tolerance in ((1, .5),):
        if not all(math.isfinite(row[index]) for row in history):
            return False
        if max(row[index] for row in newer) - max(row[index] for row in older) > tolerance:
            return False
    return True


def raise_margin(history):
    """整個觀察期間的最高溫都必須有餘裕，不使用短暫較低的讀值升瓦。"""
    return bool(history) and all(max(row[index] for row in history) <= ceiling for index, ceiling in ((1,CORE_RAISE),))


def daytime(moment=None):
    moment = (moment or datetime.now(timezone.utc)).astimezone(TAIPEI)
    minutes = moment.hour * 60 + moment.minute
    return moment.weekday() < 5 and 555 <= minutes < 1065


def mining_allowed(schedule, moment=None):
    """離峰停挖時，只允許台灣時間平日尖峰建立或繼續挖礦工作。"""
    offpeak = schedule.get('離峰挖礦', True)
    if type(offpeak) is not bool:
        raise ValueError('離峰挖礦設定必須為布林值。')
    return offpeak or daytime(moment)


def fan_check(sample):
    if len(sample.get('風扇', [])) != 2 or len(sample.get('控制', [])) != 2:
        raise RuntimeError('無法確認兩組風扇，停止負載。')
    def valid_level(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= FAN_LIMIT
    if any(not valid_level(item.get('實際百分比')) for item in sample['風扇']):
        raise RuntimeError('兩組風扇有讀值失效或超過100%，停止負載。')
    if any(not valid_level(item.get('設定百分比')) or item.get('模式') not in ('手動', '自動') for item in sample['控制']):
        raise RuntimeError('兩組風扇有設定失效或超過100%，停止負載。')
    for name in ('核心C', '熱點C', '顯存C'):
        value = sample.get(name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 125:
            raise RuntimeError('溫度感測器無有效讀值，停止負載。')
    if sample['核心C'] >= CORE_STOP:
        raise RuntimeError('接近溫度停止門檻，停止負載並保留現有風扇控制。')


def decide(active, ready, current, ceiling, sample, seconds, stable, utilization):
    fan_check(sample)
    if not active or not ready:
        return BASE, '其他時段或尚未通過持續負載核對，維持150W。'
    if sample['核心C'] >= CORE_BACKOFF:
        return max(BASE, current - 20), 'GPU核心達80°C，降低功耗；風扇維持核准時段目標。'
    cool = sample['核心C'] <= CORE_RAISE
    if current < ceiling and seconds >= SETTLE and stable and cool and utilization >= 80:
        return min(ceiling, current + STEP), '溫度穩定且有餘裕，增加10W。'
    return current, '等待溫度穩定或維持已找到的功耗。'


def configured_target(profile, current, ceiling, cooling_until, now, sample,
                      allowed, fans_ready, load_available=True):
    """正常啟動直接採用保存的時段設定；停止、風扇及溫度保護優先。"""
    fan_check(sample)
    if not allowed or not fans_ready or not load_available:
        return BASE, 900, ceiling, cooling_until, '時段禁止、風扇未就緒或負載已停止，維持保護待機。'
    if sample['核心C'] >= CORE_BACKOFF or sample['熱點C'] >= 105 or sample['顯存C'] >= 90:
        ceiling = min(ceiling, max(BASE, current - 20))
        cooling_until = now + 60
        reason = '核心80°C或熱點105°C／顯存90°C，降低功耗並保留較低上限。'
    elif now >= cooling_until:
        reason = '依照保存的本時段功耗啟動與運行，持續負載核對不限制起始功耗。'
    else:
        reason = '過熱後冷卻等待，暫不提高功耗。'
    target = min(ceiling, profile[0])
    if now < cooling_until:
        target = min(target, current)
    return target, profile[1] if target > BASE else 900, ceiling, cooling_until, reason


def temporary_trial(schedule, moment=None):
    """短測只能在固定二十分鐘內作用，過期即回到原排程。"""
    trial = schedule.get('臨時功耗短測')
    if trial is None:
        return None
    if not isinstance(trial, dict) or set(trial) != {'識別碼','開始UTC','結束UTC','步幅W','功耗W'}:
        raise RuntimeError('臨時功耗短測格式不符。')
    if (not isinstance(trial['識別碼'], str) or len(trial['識別碼']) != 32
            or any(c not in '0123456789abcdef' for c in trial['識別碼'])):
        raise RuntimeError('臨時功耗短測識別碼不符。')
    start = datetime.fromisoformat(trial['開始UTC'])
    end = datetime.fromisoformat(trial['結束UTC'])
    now = moment or datetime.now(timezone.utc)
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end-start).total_seconds() <= 1200:
        raise RuntimeError('臨時功耗短測期限不符。')
    if trial['步幅W'] not in (10,30) or type(trial['步幅W']) is not int:
        raise RuntimeError('臨時功耗短測步幅不符。')
    if type(trial['功耗W']) is not int or not BASE <= trial['功耗W'] <= 450:
        raise RuntimeError('臨時功耗短測超過原廠允許上限。')
    if not start <= now < end:
        return None
    return trial


def trial_raise_allowed(initial, fresh, peak, before, after, ceiling, moment=None):
    """只接受本次啟動已核准的短測；降溫保護介入後不得再升瓦。"""
    if ceiling < before:
        return False
    old, new = temporary_trial(initial,moment), temporary_trial(fresh,moment)
    if old is not None and new is not None:
        if any(old[key] != new[key] for key in ('識別碼','開始UTC','結束UTC','步幅W')):
            return False
        delta = after-before
        return delta == old['步幅W'] or (after == 450 and 0 < delta < old['步幅W'])
    return (not peak and initial.get('離峰逐檔測試') is True
            and fresh.get('離峰逐檔測試') is True and after-before == 10)


def period_profile(schedule, peak, maximum_clock=3105, hardware_limit=450):
    """日間保留原設定；離峰與週末使用核准原廠範圍，所有輸入嚴格核對。"""
    key = '平日固定功耗上限W' if peak else '離峰固定功耗上限W'
    clock_key = '平日固定核心時脈上限MHz' if peak else '離峰核心時脈上限MHz'
    watts = schedule.get(key)
    clock = schedule.get(clock_key)
    maximum = min(450, hardware_limit)
    if type(watts) is not int or not BASE <= watts <= maximum:
        raise RuntimeError('時段功耗設定超過核准或原廠範圍。')
    if type(clock) is not int or not 900 <= clock <= maximum_clock:
        raise RuntimeError('時段核心時脈設定超過原廠範圍。')
    fans = schedule.get('尖峰風扇百分比' if peak else '離峰風扇百分比')
    if type(fans) is not int or fans != (DAY_FAN if peak else NIGHT_FAN):
        raise RuntimeError('時段風扇與本次啟動核對目標不符，必須重新啟動核對。')
    trial = temporary_trial(schedule)
    if trial is not None:
        watts = trial['功耗W']
        if watts > min(450, hardware_limit):
            raise RuntimeError('臨時短測超過硬體原廠上限。')
    return watts, clock, fans
