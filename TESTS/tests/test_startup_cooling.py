from context import BUILD
import sys
"""重現初始化後尚未冷卻的啟動誤停，保留原有保護與等待期限。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import TEST_入口 as entry


class StartupCoolingTests(unittest.TestCase):
    def run_wait(self, levels, watts=350, stopped=False, allowed=True):
        clock=type('Clock', (), {'now':0.0})()
        clock.monotonic=lambda:clock.now
        clock.sleep=lambda seconds:setattr(clock,'now',clock.now+seconds)
        reader=Mock()
        samples=[{'核心C':level} for level in levels]
        reader.read.side_effect=samples if len(samples)>1 else None
        reader.read.return_value=samples[-1]
        stop=Mock()
        stop.exists.return_value=stopped
        data={'功耗上限W':watts,'核心溫度C':49,'顯卡功耗W':14}
        with patch.object(entry,'time',clock), \
                patch.object(entry.startup,'read_guard'), \
                patch.object(entry.startup,'configured_power',return_value=350), \
                patch.object(entry.startup,'read_shared_text',return_value='{"功耗排程":{}}'), \
                patch.object(entry.startup,'check'), \
                patch.object(entry.startup.helper,'read_gpu',return_value=data), \
                patch.object(entry,'mining_allowed',return_value=allowed):
            result=entry.wait_idle_after_check(reader,stop)
        return result,clock.now

    def test_warm_initialization_waits_until_original_threshold(self):
        result,elapsed=self.run_wait([54,50,49])
        self.assertTrue(result)
        self.assertEqual(elapsed,.5)

    def test_persistent_heat_stops_at_two_minutes(self):
        with self.assertRaisesRegex(RuntimeError,'兩分鐘內'):
            self.run_wait([54])

    def test_stop_request_is_preserved(self):
        with self.assertRaisesRegex(RuntimeError,'停止指令'):
            self.run_wait([49],stopped=True)

    def test_wrong_power_limit_is_rejected_immediately(self):
        with self.assertRaisesRegex(RuntimeError,'功耗上限與保存的時段設定不符'):
            self.run_wait([49],watts=300)

    def test_offpeak_does_not_resume_mining(self):
        self.assertEqual(self.run_wait([49],allowed=False),(False,0))


if __name__=='__main__':
    unittest.main(verbosity=2)
