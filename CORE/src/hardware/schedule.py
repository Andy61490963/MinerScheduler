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
# 設定套用時會先停止負載並重新啟動背景程序，同一執行期使用同一份時段快照。
TIME_SCHEDULE = json.loads((Path(__file__).resolve().parent / 'TEST_設定.json').read_text(encoding='utf-8-sig'))['功耗排程']
BASE = 150
STEP = 10
SETTLE = 30
CORE_RAISE = 70
CORE_BACKOFF = 75
CORE_STOP = 82  # 最後一道硬體保護，不可因降瓦而忽略。
HOTSPOT_BACKOFF = 95
MEMORY_BACKOFF = 84
HOTSPOT_STOP = 110
MEMORY_STOP = 100
THERMAL_STEP = 40
THERMAL_INTERVAL = 1
RECOVERY_INTERVAL = 30
RECOVERY_HOLD = 180
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


def time_minutes(value):
    """只接受 00:00～23:59；跨日區間須拆成兩段，目前不允許。"""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ':':
        raise ValueError('排程時間必須為 HH:MM。')
    hour, minute = value[:2], value[3:]
    if not hour.isascii() or not minute.isascii() or not hour.isdigit() or not minute.isdigit():
        raise ValueError('排程時間必須為 HH:MM。')
    if int(hour) > 23 or int(minute) > 59:
        raise ValueError('排程時間超出允許範圍。')
    return int(hour) * 60 + int(minute)


def validated_schedule_windows(schedule):
    """設定 API 與硬體排程共用的時段邊界驗證。"""
    if not isinstance(schedule, dict):
        raise ValueError('功耗排程格式不符。')
    weekend_enabled = schedule.get('週末使用平日功耗', False)
    if type(weekend_enabled) is not bool:
        raise ValueError('週末時段開關必須是布林值。')
    windows = {}
    for label, start_key, end_key in (
        ('weekday', '平日時段開始', '平日時段結束'),
        ('weekend', '週末時段開始', '週末時段結束'),
    ):
        start = time_minutes(schedule.get(start_key, '09:15'))
        end = time_minutes(schedule.get(end_key, '17:45'))
        if start >= end:
            raise ValueError('同一天的排程結束時間必須晚於開始時間。')
        windows[label] = (start, end)
    return windows, weekend_enabled


def daytime(moment=None, schedule=None):
    """統一判斷本地時段；週末未啟用時始終使用離峰功耗。"""
    moment = (moment or datetime.now(timezone.utc)).astimezone(TAIPEI)
    schedule = TIME_SCHEDULE if schedule is None else schedule
    windows, weekend_enabled = validated_schedule_windows(schedule)
    if moment.weekday() >= 5 and not weekend_enabled:
        return False
    begin, end = windows['weekday' if moment.weekday() < 5 else 'weekend']
    minutes = moment.hour * 60 + moment.minute
    return begin <= minutes < end


def transition_profile(previous_profile, new_profile, ceiling, cooling_until, now):
    """跨時段先回150W。若正處於熱保護，保留冷卻期限並由150W漸進恢復。"""
    if previous_profile is None:
        return new_profile[0], now
    thermal_restricted = cooling_until > now or ceiling < previous_profile[0]
    if thermal_restricted:
        return BASE, max(cooling_until, now)
    return new_profile[0], now


def mining_allowed(schedule, moment=None):
    """停用離峰挖礦時只允許已設定的日間功耗時段提供工作。"""
    offpeak = schedule.get('離峰挖礦', True)
    if type(offpeak) is not bool:
        raise ValueError('離峰挖礦設定必須為布林值。')
    return offpeak or daytime(moment, schedule)


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
    if sample['熱點C'] >= HOTSPOT_STOP or sample['顯存C'] >= MEMORY_STOP:
        raise RuntimeError('熱點或顯存已超過緊急溫度門檻，停止負載並保留散熱。')


def decide(active, ready, current, ceiling, sample, seconds, stable, utilization):
    fan_check(sample)
    if not active or not ready:
        return BASE, '其他時段或尚未通過持續負載核對，維持150W。'
    if sample['核心C'] >= CORE_BACKOFF:
        return max(BASE, current - 20), 'GPU核心達75°C，降低功耗；風扇維持核准時段目標。'
    cool = sample['核心C'] <= CORE_RAISE
    if current < ceiling and seconds >= SETTLE and stable and cool and utilization >= 80:
        return min(ceiling, current + STEP), '溫度穩定且有餘裕，增加10W。'
    return current, '等待溫度穩定或維持已找到的功耗。'


def configured_target(profile, current, ceiling, cooling_until, now, sample,
                      allowed, fans_ready, load_available=True, last_change_at=None):
    """優先降瓦並保持挖礦；超過 CORE_STOP 或感測失效仍由 fan_check 強制停止。"""
    fan_check(sample)
    if not allowed or not fans_ready or not load_available:
        return BASE, 900, ceiling, cooling_until, '時段禁止、風扇未就緒或負載已停止，維持保護待機。'

    too_hot = (sample['核心C'] >= CORE_BACKOFF
               or sample['熱點C'] >= HOTSPOT_BACKOFF
               or sample['顯存C'] >= MEMORY_BACKOFF)
    changed_ago = float('inf') if last_change_at is None else now - last_change_at

    # 設定值可在運行期間降瓦；任何分支都不得回傳高於目前核准時段的目標。
    ceiling = min(ceiling, profile[0])
    if too_hot:
        cooling_until = max(cooling_until, now + RECOVERY_HOLD)
        if current > BASE and changed_ago >= THERMAL_INTERVAL:
            # 一次最多降40W；若時段設定更低，須優先遵守新上限。
            ceiling = min(ceiling, max(BASE, current - THERMAL_STEP))
            reason = '溫度接近限制，分級降低40W或直接降至新上限並繼續挖礦；緊急停機保護仍保留。'
        else:
            reason = '溫度偏高，維持目前降瓦設定並監控下一筆感測資料。'
        target = min(current, ceiling)
    else:
        recovered = (now >= cooling_until and changed_ago >= RECOVERY_INTERVAL
                     and sample['核心C'] <= CORE_RAISE and sample['熱點C'] <= 90
                     and sample['顯存C'] <= 78)
        if recovered and ceiling < profile[0]:
            ceiling = min(profile[0], ceiling + STEP)
            reason = '溫度持續回穩，緩慢恢復10W。'
        elif now < cooling_until:
            reason = '降瓦冷卻觀察中，暫不提高功耗。'
        else:
            reason = '依保存時段功耗維持挖礦與溫度監控。'
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
