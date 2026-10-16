from context import BUILD
import sys
"""核對原廠450W上限與降瓦保護邊界，不存取硬體。"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from TEST_排程規則 import power_reading_ok
import TEST_功耗排程 as power


class PowerLimitTests(unittest.TestCase):
    def test_450w_readback_and_draw_tolerance(self):
        self.assertTrue(power_reading_ok(450, 450))
        self.assertTrue(power_reading_ok(475, 450))
        self.assertFalse(power_reading_ok(476, 450))

    def test_above_approved_power_setting_is_rejected(self):
        for target in (451, 600):
            with self.subTest(功耗上限W=target):
                self.assertFalse(power_reading_ok(450, target))

    def test_backoff_from_450w_retains_two_second_limit(self):
        self.assertTrue(power_reading_ok(450, 150, 450, 2))
        self.assertFalse(power_reading_ok(450, 150, 450, 0))
        self.assertFalse(power_reading_ok(450, 150, 450, 2.01))
        self.assertFalse(power_reading_ok(476, 150, 450, 1))
        self.assertFalse(power_reading_ok(450, 150, 451, 1))

    def test_profile_accepts_450w_and_rejects_451w(self):
        for watts in (450, 451):
            config = json.dumps({'功耗排程': {
                '平日固定功耗上限W': watts,
                '平日固定核心時脈上限MHz': 3105}})
            with patch.object(power, 'read_shared_text', return_value=config):
                if watts == 450:
                    self.assertEqual(power.read_profile(3105), (450, 3105))
                    self.assertIsNone(power.lower_profile((450, 3105)))
                else:
                    with self.assertRaisesRegex(RuntimeError, '150W與450W'):
                        power.read_profile(3105)
                    with self.assertRaisesRegex(RuntimeError, '設定失效'):
                        power.lower_profile((450, 3105))

    def test_running_profile_can_still_lower_from_450w(self):
        config = json.dumps({'功耗排程': {
            '平日固定功耗上限W': 200,
            '平日固定核心時脈上限MHz': 3105}})
        with patch.object(power, 'read_shared_text', return_value=config):
            self.assertEqual(power.lower_profile((450, 3105)), (200, 3105))


if __name__ == '__main__':
    unittest.main(verbosity=2)
