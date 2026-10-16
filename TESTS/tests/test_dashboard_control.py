from context import BUILD
import sys
"""核對恢復按鈕不會重複啟動，且較新的人工停止有優先權。"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import time
import http.client
import json
import threading
from unittest.mock import patch

import actions
import server


class ControlTests(unittest.TestCase):
    def test_running_miner_is_not_restarted(self):
        control=actions.MiningActions(lambda:{'state':'running','miner_pid':123,'server_time':time.time(),'guard_ok':True,'status_age':0,'guard_age':0},lambda pid:True)
        with patch.object(actions.threading,'Thread') as thread:
            self.assertTrue(control.begin()['ok'])
            thread.assert_not_called()
            self.assertFalse(control.snapshot()['busy'])

    def test_busy_request_does_not_start_another_worker(self):
        control=actions.MiningActions(lambda:{'state':'stopped'},lambda pid:False)
        control.state['busy']=True
        with patch.object(actions.threading,'Thread') as thread:
            control.begin()
            thread.assert_not_called()

    def test_stale_running_status_can_be_recovered(self):
        self.assertFalse(actions.healthy_snapshot({'state':'running','guard_ok':True,'server_time':time.time()-20,'status_age':0,'guard_age':0}))

    def test_new_manual_stop_is_preserved(self):
        with TemporaryDirectory() as directory:
            stop=Path(directory)/'停止.flag'
            log=Path(directory)/'操作.log'
            control=actions.MiningActions(lambda:{'state':'stopped'},lambda pid:False)
            with patch.object(actions,'STOP',stop),patch.object(actions,'ACTION_LOG',log),patch.object(actions.ctypes.windll.shell32,'IsUserAnAdmin',return_value=False),patch.object(actions.subprocess,'Popen') as launch:
                fingerprint=actions.stop_fingerprint()
                stop.write_text('使用者剛停止',encoding='utf-8')
                control.run(fingerprint)
                launch.assert_not_called()
                self.assertEqual(stop.read_text(encoding='utf-8'),'使用者剛停止')
                self.assertEqual(control.snapshot()['phase'],'error')

    def test_log_source_does_not_allow_arbitrary_files(self):
        for source in ('../TEST_設定.json','C:/test/TEST_設定.json','wallet'):
            with self.assertRaises(ValueError):
                server.log_tail(source)
        data=server.log_tail('miner')
        self.assertLessEqual(len(data['text'].splitlines()),180)

    def test_launcher_uses_windows_powershell_modules(self):
        with TemporaryDirectory() as directory:
            stop=Path(directory)/'停止.flag'
            control=actions.MiningActions(lambda:{'state':'stopped'},lambda pid:False)
            with patch.object(actions,'STOP',stop),patch.object(actions,'ACTION_LOG',Path(directory)/'操作.log'),patch.object(actions.ctypes.windll.shell32,'IsUserAnAdmin',return_value=False),patch.object(actions.subprocess,'Popen') as launch:
                launch.return_value.wait.return_value=1
                control.run('無')
                self.assertIn('WindowsPowerShell/v1.0/Modules',launch.call_args.kwargs['env']['PSModulePath'])

    def test_manual_stop_cancels_start_and_preserves_marker(self):
        with TemporaryDirectory() as directory:
            stop=Path(directory)/'停止.flag'
            control=actions.MiningActions(lambda:{'state':'stopped'},lambda pid:False)
            control.start_done.clear()
            control.state.update(busy=True,phase='checking')
            old_operation=control.operation
            with patch.object(actions,'STOP',stop),patch.object(actions,'ACTION_LOG',Path(directory)/'操作.log'),patch.object(actions.threading,'Thread') as thread:
                self.assertTrue(control.request_stop()['ok'])
                marker=stop.read_text(encoding='utf-8')
                self.assertTrue(actions.manual_stop_requested())
                self.assertTrue(control.cancel.is_set())
                self.assertTrue(control.request_stop()['ok'])
                self.assertEqual(thread.call_count,1)
                control.update('success','舊啟動完成',False,old_operation)
                self.assertEqual(control.snapshot()['phase'],'stopping')
                control.begin()
                self.assertEqual(thread.call_count,1)
                control.start_done.set()
                with patch.object(actions,'mining_jobs',return_value=[]):
                    control.wait_stopped(marker,control.operation)
                self.assertEqual(control.snapshot()['phase'],'stopped')
                self.assertFalse(control.snapshot()['busy'])
                self.assertEqual(stop.read_text(encoding='utf-8'),marker)

    def test_stop_during_launcher_does_not_report_start_success(self):
        with TemporaryDirectory() as directory:
            stop=Path(directory)/'停止.flag'
            control=actions.MiningActions(lambda:{'state':'running'},lambda pid:True)
            control.operation=1
            control.start_done.clear()
            original_cancel=control.cancel
            with patch.object(actions,'STOP',stop),patch.object(actions,'ACTION_LOG',Path(directory)/'操作.log'), \
                    patch.object(actions.ctypes.windll.shell32,'IsUserAnAdmin',return_value=False), \
                    patch.object(actions.threading,'Thread'),patch.object(actions.subprocess,'Popen') as launch:
                launch.return_value.wait.side_effect=lambda **kwargs:(control.request_stop(),0)[1]
                control.run('無',1,original_cancel)
                self.assertTrue(stop.read_text(encoding='utf-8').startswith('監控畫面人工停止：'))
                self.assertEqual(control.snapshot()['phase'],'stopping')
                self.assertTrue(control.start_done.is_set())

    def test_stop_waiter_restores_marker_removed_by_old_launcher(self):
        with TemporaryDirectory() as directory:
            stop=Path(directory)/'停止.flag'
            control=actions.MiningActions(lambda:{},lambda pid:False)
            with patch.object(actions,'STOP',stop),patch.object(actions,'ACTION_LOG',Path(directory)/'操作.log'),patch.object(actions,'mining_jobs',return_value=[]):
                control.wait_stopped('較新的人工停止',control.operation)
                self.assertEqual(stop.read_text(encoding='utf-8'),'較新的人工停止')
                self.assertEqual(control.snapshot()['phase'],'stopped')

    def test_model_pause_and_manual_stop_are_distinct(self):
        with TemporaryDirectory() as directory:
            stop=Path(directory)/'停止.flag'
            with patch.object(actions,'STOP',stop):
                self.assertFalse(actions.manual_stop_requested())
                stop.write_text('Emerald 本地模型暫停挖礦：測試',encoding='utf-8')
                self.assertFalse(actions.manual_stop_requested())
                stop.write_text('',encoding='utf-8')
                self.assertTrue(actions.manual_stop_requested())

    def test_stop_endpoint_requires_local_origin_and_token(self):
        http_server=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        worker=threading.Thread(target=http_server.serve_forever,daemon=True)
        worker.start()
        try:
            port=http_server.server_port
            headers={'Host':f'127.0.0.1:{port}','Origin':f'http://127.0.0.1:{port}',
                     'Content-Type':'application/json','X-Monitor-Token':server.ACTION_TOKEN}
            with patch.object(server.ACTIONS,'request_stop',return_value={'ok':True,'message':'測試停止'}) as stop:
                # 拒絕來源時不送內文，避免 Windows 關閉尚有未讀內文的連線而重設回應。
                for replacements,body,expected in [({'Origin':'https://example.com'},'',403),({'X-Monitor-Token':'invalid'},'',403),({},'{"command":"任意命令"}',400),({},'{}',202)]:
                    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                    connection.request('POST','/api/stop',body.encode('utf-8'),{**headers,**replacements})
                    response=connection.getresponse()
                    self.assertEqual(response.status,expected)
                    json.loads(response.read())
                    connection.close()
                stop.assert_called_once_with()
        finally:
            http_server.shutdown()
            http_server.server_close()
            worker.join(timeout=2)


if __name__=='__main__':
    unittest.main()
