from context import BUILD
"""AI 與挖礦互斥的離線回歸；不執行模型、礦工或硬體操作。"""

import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.error import URLError

from TEST_AI協調 import Coordinator, model_busy


class 協調測試(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stop = self.root / "TEST_停止.flag"
        self.resumed = 0

    def resume(self, marker):
        self.assertEqual(self.stop.read_text(encoding="utf-8"), marker)
        self.resumed += 1
        self.stop.unlink()

    def test_忙碌停止且閒置滿緩衝才恢復(self):
        controller = Coordinator(self.root, lambda: True, self.resume)
        controller.tick(True, 0)
        self.assertTrue(controller.owns_stop())
        controller.tick(False, 1)
        controller.tick(False, 60)
        self.assertEqual(self.resumed, 0)
        controller.tick(False, 61)
        self.assertEqual(self.resumed, 1)
        self.assertFalse(self.stop.exists())
        controller.tick(False, 90)
        self.assertEqual(self.resumed, 1)

    def test_再次忙碌重新計算緩衝(self):
        controller = Coordinator(self.root, lambda: True, self.resume)
        controller.tick(True, 0)
        controller.tick(False, 1)
        controller.tick(True, 50)
        controller.tick(False, 51)
        controller.tick(False, 100)
        self.assertEqual(self.resumed, 0)
        controller.tick(False, 111)
        self.assertEqual(self.resumed, 1)

    def test_不覆寫先前人工停止(self):
        self.stop.write_text("", encoding="utf-8")
        controller = Coordinator(self.root, lambda: True, self.resume)
        controller.tick(True, 0)
        controller.tick(False, 1)
        controller.tick(False, 100)
        self.assertEqual(self.stop.read_text(), "")
        self.assertEqual(self.resumed, 0)

    def test_暫停期間人工停止優先(self):
        controller = Coordinator(self.root, lambda: True, self.resume)
        controller.tick(True, 0)
        self.stop.write_text("", encoding="utf-8")
        controller.tick(False, 1)
        controller.tick(False, 100)
        self.assertEqual(self.resumed, 0)

    def test_沒有挖礦程序不擅自重啟故障負載(self):
        controller = Coordinator(self.root, lambda: False, self.resume)
        controller.tick(True, 0)
        controller.tick(False, 1)
        controller.tick(False, 100)
        self.assertEqual(self.resumed, 0)
        self.assertFalse(self.stop.exists())

    def test_只在明確授權時接管本次暫停(self):
        self.stop.write_text("", encoding="utf-8")
        controller = Coordinator(self.root, lambda: False, self.resume, claim=True)
        controller.tick(False, 1)
        controller.tick(False, 61)
        self.assertEqual(self.resumed, 1)

    def test_重啟只承接原本自己的暫停(self):
        controller = Coordinator(self.root, lambda: True, self.resume)
        controller.tick(True, 0)
        restored = Coordinator(self.root, lambda: False, self.resume)
        self.assertTrue(restored.owns_stop())
        restored.tick(False, 1)
        restored.tick(False, 61)
        self.assertEqual(self.resumed, 1)

    def test_監測確認閒置忙碌與壞格式(self):
        for payload, expected in [
            ([{"is_processing": False}], False),
            ([{"is_processing": True}], True),
            ([{"is_processing": "false"}], True),
            ([], True),
            ({"status": "loading"}, True),
        ]:
            with self.subTest(payload=payload):
                opener = lambda *_args, **_kwargs: io.StringIO(json.dumps(payload))
                self.assertEqual(model_busy(opener), expected)

    def test_連線拒絕與逾時分開處理(self):
        def refused(*_args, **_kwargs):
            raise URLError(ConnectionRefusedError())

        def timed_out(*_args, **_kwargs):
            raise URLError(TimeoutError())

        self.assertFalse(model_busy(refused))
        self.assertTrue(model_busy(timed_out))

    def test_Windows關閉埠的延遲不誤判為模型忙碌(self):
        def closed_windows_port(*_args, **kwargs):
            # 實測拒絕連線約需兩秒；較短逾時會先丟出TimeoutError。
            if kwargs['timeout'] < 2:
                raise URLError(TimeoutError())
            raise URLError(ConnectionRefusedError())
        self.assertFalse(model_busy(closed_windows_port))


if __name__ == "__main__":
    unittest.main()
