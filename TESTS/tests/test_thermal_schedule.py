"""時段與溫控決策離線回歸：不存取 GPU 或修改正式執行檔。"""
from context import BUILD
from datetime import datetime
import unittest
import TEST_排程規則 as rules


def sample(core=65, hotspot=80, memory=70):
    return {
        '核心C': core, '熱點C': hotspot, '顯存C': memory,
        '風扇': [{'實際百分比': 80, 'RPM': 2300} for _ in range(2)],
        '控制': [{'設定百分比': 80, '模式': '手動'} for _ in range(2)],
    }


class ThermalScheduleTests(unittest.TestCase):
    def test_preemptive_throttle_without_stop(self):
        watts, clock, ceiling, cooling, reason = rules.configured_target(
            (350, 3105, 80), 350, 350, 0, 100, sample(core=76), True, True, True, 90)
        self.assertEqual((watts, clock, ceiling), (310, 3105, 310))
        self.assertGreater(cooling, 100)
        self.assertIn('降低40W', reason)

    def test_power_write_rate_limit(self):
        watts, clock, ceiling, *_ = rules.configured_target(
            (350, 3105, 80), 350, 350, 0, 100, sample(hotspot=98), True, True, True, 99.5)
        self.assertEqual((watts, ceiling), (350, 350))

    def test_cooling_only_recovers_small_steps(self):
        watts, clock, ceiling, *_ = rules.configured_target(
            (350, 3105, 80), 310, 310, 30, 100, sample(), True, True, True, 40)
        self.assertEqual((watts, ceiling), (320, 320))
        watts, *_ = rules.configured_target(
            (350, 3105, 80), 310, 310, 190, 100, sample(), True, True, True, 40)
        self.assertEqual(watts, 310)

    def test_critical_temperature_still_stops(self):
        with self.assertRaises(RuntimeError):
            rules.configured_target((350, 3105, 80), 150, 150, 0, 100, sample(core=82), True, True)
        with self.assertRaises(RuntimeError):
            rules.fan_check(sample(core=None))

    def test_hotspot_memory_emergency_cutoff(self):
        with self.assertRaises(RuntimeError):
            rules.fan_check(sample(hotspot=110))
        with self.assertRaises(RuntimeError):
            rules.fan_check(sample(memory=100))
        rules.fan_check(sample(core=75, hotspot=105, memory=90))

    def test_weekday_and_weekend_windows(self):
        schedule = {'平日時段開始': '07:00', '平日時段結束': '08:00',
                    '週末使用平日功耗': True, '週末時段開始': '10:00', '週末時段結束': '12:00'}
        weekday = datetime(2026, 10, 16, 7, 30, tzinfo=rules.TAIPEI)
        weekend = datetime(2026, 10, 17, 10, 30, tzinfo=rules.TAIPEI)
        self.assertTrue(rules.daytime(weekday, schedule))
        self.assertTrue(rules.daytime(weekend, schedule))
        schedule['週末使用平日功耗'] = False
        self.assertFalse(rules.daytime(weekend, schedule))
        self.assertFalse(rules.daytime(datetime(2026, 10, 16, 9, 15, tzinfo=rules.TAIPEI), schedule))

    def test_invalid_time_window_fails_closed(self):
        with self.assertRaises(ValueError):
            rules.daytime(datetime(2026, 10, 16, 10, tzinfo=rules.TAIPEI),
                          {'平日時段開始': '18:00', '平日時段結束': '09:00'})


if __name__ == '__main__':
    unittest.main()
