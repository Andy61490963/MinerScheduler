from context import BUILD
"""模擬磁碟延遲與暫時鎖檔，確認紀錄不再阻塞礦工的即時保護。"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import uuid
from datetime import datetime,timezone,timedelta
from unittest.mock import patch

import TEST_Run as runtime
import TEST_啟動核對 as startup
from TEST_即時通道 import ChannelWriter, ChannelReader, BackgroundFileWriter


class BackgroundDelayTests(unittest.TestCase):
    def test_guard_heartbeat_does_not_read_disk(self):
        info={'更新時間UTC':datetime.now(timezone.utc).isoformat(),'狀態':'排程保護已就緒',
              '模式':'平日排程，70%風扇優先','目標功耗上限W':150,'最高核心時脈MHz':900,
              '原廠最高核心時脈MHz':3105,'時段功耗上限W':180,'執行識別碼':'測試',
              '顯卡':{'功耗上限W':150,'功耗W':140,'核心時脈MHz':900,'核心C':55,'風扇百分比':80}}
        with patch.object(startup,'read_channel',return_value=info),patch.object(startup,'read_shared_text',side_effect=AssertionError('即時保護不可依賴磁碟')),patch.object(startup,'ROOT',Path('不存在的測試路徑')):
            self.assertEqual(startup.read_guard('測試',realtime=True)['目標功耗上限W'],150)
            with self.assertRaisesRegex(RuntimeError,'程序已更換'):
                startup.read_guard('其他程序',realtime=True)
            info['更新時間UTC']=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()
            with self.assertRaisesRegex(RuntimeError,'資料過期'):
                startup.read_guard('測試',realtime=True)

    def test_active_fan_profiles_match_user_request(self):
        import json
        for name in ('TEST_尖峰風扇70.json','TEST_離峰風扇100.json'):
            data=json.loads((BUILD/'TEST_風扇/Configurations'/name).read_text(encoding='utf-8-sig'))
            controls=[c for c in data['FanControl']['Controls'] if c['Enable']]
            board=[c for c in controls if c['Identifier'].startswith('/lpc/')]
            gpu=[c for c in controls if c['Identifier'].startswith('NVApiWrapper/')]
            self.assertEqual(len(board),3)
            self.assertEqual(len(gpu),2)
            self.assertTrue(all(c['ManualControl'] and c['ManualControlValue']==50 for c in board))
            self.assertTrue(all(c['ManualControl'] and c['ManualControlValue']==80 for c in gpu))

    def test_file_delay_keeps_shared_heartbeat_fresh(self):
        blocked=threading.Event()
        release=threading.Event()
        def disk(data):
            blocked.set()
            release.wait(2)
        writer=BackgroundFileWriter(disk)
        test_name='Local\\TEST_延遲測試_'+str(uuid.uuid4())
        channel=ChannelWriter('miner',tagname=test_name)
        reader=ChannelReader('miner',tagname=test_name)
        try:
            writer.submit({'序號':1})
            self.assertTrue(blocked.wait(1))
            for i in range(30):
                channel.publish({'序號':i,'狀態':'挖礦中'})
                self.assertEqual(reader.read()['序號'],i)
            self.assertEqual(reader.read()['狀態'],'挖礦中')
        finally:
            release.set()
            writer.stop.set()
            writer.thread.join(2)
            reader.close()
            channel.close()

    def test_temporary_file_error_retries_without_fatal_error(self):
        attempts=[]
        done=threading.Event()
        def disk(data):
            attempts.append(data)
            if len(attempts)==1:
                raise PermissionError('模擬短暫鎖檔')
            done.set()
        writer=BackgroundFileWriter(disk)
        try:
            writer.submit({'測試':True})
            self.assertTrue(done.wait(2))
            self.assertGreaterEqual(len(attempts),2)
        finally:
            writer.stop.set()
            writer.thread.join(1)

    def test_safety_errors_still_stop_immediately(self):
        from TEST_排程規則 import fan_check,decide
        base={'核心C':70,'熱點C':90,'顯存C':54,
              '風扇':[{'編號':i,'實際百分比':80} for i in (1,2)],
              '控制':[{'編號':i,'設定百分比':80,'模式':'手動'} for i in (1,2)]}
        self.assertLess(decide(True,True,230,230,{**base,'核心C':80},30,True,99)[0],230)
        for sample in ({**base,'核心C':82},{**base,'熱點C':float('nan')}):
            with self.assertRaises(RuntimeError):
                fan_check(sample)
        called=[]
        with patch.object(runtime,'STOP') as stop:
            stop.exists.return_value=True
            worker=runtime.BackgroundReading(lambda:base,fan_check,.05,called.append,1)
            worker.thread.join(1)
            self.assertTrue(called)
            self.assertIn('停止',called[0])

    def test_miner_forbidden_controls_remain_fatal(self):
        with TemporaryDirectory() as directory,patch.object(runtime,'ROOT',Path(directory)), \
                patch.object(runtime,'monitor_log',side_effect=RuntimeError('礦工啟用了禁止的GPU控制流程，停止。')):
            logger=runtime.BackgroundLog()
            try:
                logger.submit({'經過秒數':1,'目前讀值':{}})
                logger.thread.join(1)
                self.assertIn('禁止',logger.snapshot()['錯誤'])
            finally:
                logger.stop.set()
                logger.recording.stop.set()
                logger.recording.thread.join(1)

    def test_blocked_recording_does_not_block_log_validation(self):
        blocked=threading.Event()
        release=threading.Event()
        calls=[]
        def delayed_save(data):
            blocked.set()
            release.wait(3)
        def read_log(offset,tail,accepted):
            calls.append(1)
            return offset+1,'GPU sensors: disabled\nCpu mining: disabled\n',accepted+1
        with TemporaryDirectory() as directory, patch.object(runtime,'ROOT',Path(directory)), \
                patch.object(runtime,'save',side_effect=delayed_save), patch.object(runtime,'monitor_log',side_effect=read_log):
            logger=runtime.BackgroundLog()
            sample={'經過秒數':1,'目前讀值':{}}
            try:
                logger.submit(sample)
                self.assertTrue(blocked.wait(1),'模擬寫入未開始')
                logger.submit(sample)
                deadline=time.monotonic()+.6
                while len(calls)<2 and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertGreaterEqual(len(calls),2,'紀錄寫入阻塞了礦工紀錄驗證')
            finally:
                release.set()
                logger.stop.set()
                logger.thread.join(2)
                if hasattr(logger,'recording'):
                    logger.recording.stop.set()
                    logger.recording.thread.join(2)


if __name__=='__main__':
    unittest.main()
