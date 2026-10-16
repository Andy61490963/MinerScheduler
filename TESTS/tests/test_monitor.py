from context import BUILD
import sys
"""只測試新增監控程式的資料判讀，不接觸礦工或硬體。"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import server


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = server.stamp("2026-10-15T15:30:00+00:00")
        self.status = {"狀態": "挖礦中，持續負載核對通過", "開始UTC": "2026-10-15T15:00:00+00:00",
                       "更新時間UTC": "2026-10-15T15:30:00+00:00", "礦工PID": 512,
                       "保護識別碼": "測試", "經過秒數": 1800, "收款地址": "不得出現在輸出",
                       "目前讀值": {"功耗W": 230, "功耗上限W": 230, "核心C": 70,
                                   "熱點C": 108, "顯存C": 54, "風扇": [], "控制": []}}
        self.guard = {"更新時間UTC": self.status["更新時間UTC"], "執行識別碼": "測試", "狀態": "排程保護已就緒"}
        self.log = "[2026-10-15 23:29:50] GPU0 RTX 4090: 220.00 TH/s [T:- FAN:- P:- EFF:- CC:- MC:- A:14 R:1 HW:0]\n[2026-10-15 23:29:50] Average hashrate: 1m 219.00 TH/s | 15m 218.00 TH/s | 1h 211.00 TH/s\n"

    def collect(self, status=None, alive=True, now=None, fan=None, manual_stop=False):
        data = status or self.status
        monitor = server.Monitor()
        def read_json(name):
            sources={"TEST_Status.json":data,"TEST_功耗狀態.json":self.guard,"TEST_風扇排程狀態.json":fan or {}}
            return copy.deepcopy(sources[name]), None
        def seed(start, timestamp):
            monitor.samples.extend({"t": self.now - 60 + i, "power": 230, "core": 70, "hotspot": 108, "memory": 54} for i in range(60))
        with patch.object(monitor, "read_json", side_effect=read_json), patch.object(monitor, "seed", side_effect=seed), \
                patch.object(server, "pid_alive", return_value=alive), patch.object(server, "shared_read", return_value=self.log), \
                patch.object(server.time, "time", return_value=now or self.now), \
                patch.object(server, "manual_stop_requested", return_value=manual_stop):
            monitor.collect()
        return monitor.snapshot

    def test_timezone_and_hash_unit(self):
        self.assertEqual(server.stamp("2026-10-15T23:30:00+08:00"), self.now)
        self.assertEqual(server.ths("220000", "GH/s"), 220)
        reports = server.parse_log(self.log)
        self.assertEqual(reports[-1]["one_minute"], 219)
        self.assertEqual(reports[-1]["rejected"], 1)

    def test_efficiency_and_private_fields(self):
        data = self.collect()
        self.assertEqual(data["state"], "running")
        self.assertAlmostEqual(data["efficiency"], 220 / 230)
        self.assertTrue(data["guard_ok"])
        self.assertNotIn("不得出現在輸出", json.dumps(data, ensure_ascii=False))

    def test_stale_sensors_hide_live_hash(self):
        data = self.collect(now=self.now + 20)
        self.assertEqual(data["state"], "unknown")
        self.assertFalse(data["sensor_fresh"])
        self.assertIsNone(data["hashrate"])
        self.assertIsNone(data["efficiency"])

    def test_stopped_process_and_stale_hash(self):
        data = self.collect(alive=False)
        self.assertEqual(data["state"], "stopped")
        self.assertIsNone(data["efficiency"])
        current = copy.deepcopy(self.status)
        current["更新時間UTC"] = "2026-10-15T15:34:00+00:00"
        data = self.collect(status=current, now=self.now + 240)
        self.assertEqual(data["state"], "running")
        self.assertIsNone(data["hashrate"])

    def test_session_filter_and_missing_power(self):
        self.assertEqual(server.parse_log(self.log, self.now), [])
        report = {"t": 100, "hashrate": 220}
        self.assertEqual(server.aligned_efficiency(report, []), (None, None))
        points = [{"t": i, "power": 0} for i in range(70, 101)]
        self.assertEqual(server.aligned_efficiency(report, points), (None, None))
        gaps = [{"t": i, "power": 230} for i in list(range(70, 81)) + list(range(90, 101))]
        self.assertEqual(server.aligned_efficiency(report, gaps), (None, None))

    def test_stopped_miner_uses_fresh_idle_sensors_without_hashrate(self):
        fan={"更新時間UTC":"2026-10-15T15:31:00+00:00", "顯卡":{"功耗W":20,"核心C":40,"熱點C":45,"顯存C":42},
             "兩組風扇":[{"編號":1,"RPM":2500,"實際百分比":80}],"兩組風扇控制":[{"編號":1,"設定百分比":80}]}
        data=self.collect(alive=False,now=self.now+60,fan=fan,manual_stop=True)
        self.assertEqual(data['state'],'stopped')
        self.assertTrue(data['manual_stop'])
        self.assertTrue(data['sensor_fresh'])
        self.assertEqual(data['sensor_age'],0)
        self.assertEqual(data['status_age'],60)
        self.assertEqual(data['power'],20)
        self.assertEqual(data['temperatures']['hotspot'],45)
        self.assertEqual(data['gpu_fans'][0]['rpm'],2500)
        self.assertIsNone(data['hashrate'])
        self.assertIsNone(data['efficiency'])

    def test_stale_idle_sensors_are_not_marked_live(self):
        fan={"更新時間UTC":self.status['更新時間UTC'],"顯卡":{"功耗W":20,"熱點C":45}}
        data=self.collect(alive=False,now=self.now+60,fan=fan,manual_stop=True)
        self.assertFalse(data['sensor_fresh'])
        self.assertIsNone(data['hashrate'])

    def test_manual_stop_is_not_complete_while_miner_still_alive(self):
        data=self.collect(manual_stop=True)
        self.assertEqual(data['state'],'running')

    def test_shared_read_allows_atomic_replace(self):
        path = Path(__file__).with_name("共用讀取測試.txt")
        temporary = path.with_suffix(".tmp")
        try:
            path.write_text("第一筆", encoding="utf-8")
            self.assertEqual(server.shared_read(path), "第一筆")
            temporary.write_text("第二筆", encoding="utf-8")
            temporary.replace(path)
            self.assertEqual(server.shared_read(path), "第二筆")
        finally:
            path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
