from context import BUILD
import sys
"""使用隔離設定與假硬體核對網頁控制，不影響正在運行的礦工。"""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch, Mock

import settings
import actions

ORIGINAL = settings.read_bundle()
STAGED = BUILD / 'TEST_排程規則.py'


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.root=Path(self.temp.name)/'CORE'
        self.root.mkdir()
        for name,content in ORIGINAL.items():
            path=self.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content.encode('utf-8'))
        self.source=patch.object(settings,'SOURCE',self.root)
        self.source.start()

    def tearDown(self):
        self.source.stop()
        self.temp.cleanup()

    def payload(self,**changes):
        data=settings.public_settings()
        return {**data['values'],'revision':data['revision'],**changes}

    def test_ranges_are_strict_and_no_arbitrary_fields(self):
        for changes in ({'day_watts':460},{'night_watts':500},{'day_watts':181},{'gpu_fan':49},
                        {'board_fan':29},{'gpu_fan':101},{'day_watts':True},{'board_fan':float('nan')},
                        {'gpu_fan':'100'},{'weekday_start':'25:00'},{'weekday_end':'09:15'},
                        {'weekend_peak':'true'},{'weekend_start':'24:00'},{'command':'任意命令'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                settings.prepare(self.payload(**changes))
        self.assertFalse(settings.prepare(self.payload(day_watts=200,gpu_fan=100))['unchanged'])

    def test_public_values_have_no_wallet_or_pool(self):
        result=settings.public_settings()
        self.assertEqual(set(result['values']),set(settings.FIELDS) | set(settings.TIME_FIELDS) | {'weekend_peak'})
        self.assertNotIn('收款地址',json.dumps(result,ensure_ascii=False))

    def test_unchanged_is_a_noop(self):
        control=actions.MiningActions(lambda:{},lambda pid:False)
        with patch.object(actions.threading,'Thread') as worker:
            result=control.apply_settings(self.payload())
        self.assertTrue(result['ok'])
        worker.assert_not_called()

    def test_stale_revision_is_rejected(self):
        payload=self.payload(day_watts=200)
        path=self.root/settings.FILES[0]
        path.write_text(path.read_text(encoding='utf-8')+' ',encoding='utf-8')
        with self.assertRaises(ValueError):settings.prepare(payload)

    def test_only_allowed_settings_and_five_controls_change(self):
        plan=settings.prepare(self.payload(day_watts=200,night_watts=220,gpu_fan=90,board_fan=60))
        original=json.loads(ORIGINAL[settings.FILES[0]])
        result=plan['documents'][settings.FILES[0]]
        for key in original:
            if key not in ('功耗排程','運作模式'):self.assertEqual(result[key],original[key])
        self.assertEqual(result['功耗排程']['核心停止溫度C'],82)
        self.assertEqual(result['功耗排程']['核心降瓦溫度C'],75)
        for name in settings.FILES[1:]:
            old=json.loads(ORIGINAL[name])['FanControl']['Controls']
            new=plan['documents'][name]['FanControl']['Controls']
            for before,after in zip(old,new):
                identifier=before['Identifier']
                if identifier in settings.GPU_IDS:self.assertEqual(after['ManualControlValue'],90)
                elif identifier in settings.BOARD_IDS:self.assertEqual(after['ManualControlValue'],60)
                else:self.assertEqual(before,after)

    def test_guard_verifies_new_window_not_cached_previous_window(self):
        import TEST_排程規則 as rules
        plan = settings.prepare(self.payload(
            day_watts=200, night_watts=150,
            weekday_start='07:00', weekday_end='09:00'))
        values = plan['values']
        saved = plan['documents'][settings.FILES[0]]['功耗排程']
        guard = {'更新時間UTC': datetime.now(timezone.utc).isoformat(),
                 '狀態': '排程保護已就緒',
                 '持續負載核對通過': True,
                 '時段功耗上限W': 200,
                 '風扇目標百分比': values['gpu_fan'],
                 '目標功耗上限W': 180,
                 '顯卡': {'功耗上限W': 180}}
        # 2026/10/16 08:30台灣時間：新設定是平日功耗，舊快取09:15才開始。
        with patch.object(rules, 'datetime') as now, \
             patch('TEST_即時通道.read_channel', return_value=guard), \
             patch.object(settings.time, 'sleep', side_effect=AssertionError('錯用舊時段進入重試')):
            now.now.return_value = datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc)
            self.assertEqual(settings.verify_guard(values, lambda: None, saved), 180)

    def test_custom_weekday_and_weekend_times_persist(self):
        requested = self.payload(weekday_start='07:00', weekday_end='19:00',
                                 weekend_peak=True, weekend_start='10:00',
                                 weekend_end='14:00')
        plan = settings.prepare(requested)
        self.assertFalse(plan['unchanged'])
        settings.write_plan(plan)
        saved = settings.public_settings()['values']
        for key in ('weekday_start', 'weekday_end', 'weekend_peak',
                    'weekend_start', 'weekend_end'):
            self.assertEqual(saved[key], requested[key])

    def test_backup_is_complete_and_failed_write_rolls_back(self):
        plan=settings.prepare(self.payload(day_watts=200))
        original_write=settings.atomic_write
        count=0
        def failing(path,text):
            nonlocal count
            count+=1
            if count==2:raise OSError('模擬第二檔寫入失敗')
            return original_write(path,text)
        with patch.object(settings,'atomic_write',side_effect=failing):
            with self.assertRaises(OSError):settings.write_plan(plan)
        self.assertEqual(settings.read_bundle(),ORIGINAL)
        backup=next((self.root.parent/'備份').iterdir())
        for name in settings.FILES:self.assertEqual((backup/name).read_bytes().decode('utf-8-sig'),ORIGINAL[name])

    def test_successful_save_keeps_settings_on_disk(self):
        plan=settings.prepare(self.payload(day_watts=200,gpu_fan=90))
        settings.write_plan(plan)
        self.assertEqual(settings.public_settings()['values']['day_watts'],200)
        self.assertEqual(settings.public_settings()['values']['gpu_fan'],90)

    def run_control(self,resume=True,stop_during_fan_check=False):
        control=actions.MiningActions(lambda:{},lambda pid:False)
        control.operation=1
        control.state.update(busy=True,phase='settings')
        control.start_done.clear()
        stop=self.root/'停止.flag'
        if not resume:stop.write_text('原本人工停止',encoding='utf-8')
        def launch(fingerprint,operation,cancel,complete):
            self.assertFalse(complete)
            self.assertEqual(actions.stop_fingerprint(),fingerprint)
            stop.unlink()
            control.update('success','假啟動通過',True,operation)
        plan=settings.prepare(self.payload(day_watts=200,gpu_fan=90))
        with patch.object(actions,'STOP',stop),patch.object(actions,'ACTION_LOG',self.root/'操作.log'), \
             patch.object(settings,'wait_quiet') as quiet,patch.object(settings,'workers') as workers, \
             patch.object(settings,'write_plan',return_value=self.root/'備份') as save, \
             patch.object(settings,'verify_fans') as fan,patch.object(settings,'verify_guard',return_value=200) as guard, \
             patch.object(control,'run',side_effect=launch) as start,patch.object(actions.threading,'Thread'):
            if stop_during_fan_check:fan.side_effect=lambda *_:control.request_stop()
            control.run_settings(plan,actions.stop_fingerprint(),resume,1,control.cancel)
            quiet.assert_called_once()
            save.assert_called_once()
            self.assertEqual([call.args[0] for call in workers.call_args_list],['停止風扇','啟動風扇'])
            if stop_during_fan_check:
                start.assert_not_called()
                guard.assert_not_called()
                self.assertTrue(stop.read_text(encoding='utf-8').startswith('監控畫面人工停止：'))
                self.assertEqual(control.snapshot()['phase'],'stopping')
            elif resume:
                start.assert_called_once()
                guard.assert_called_once()
                self.assertEqual(guard.call_args.args[2],
                                 plan['documents'][settings.FILES[0]]['功耗排程'])
                self.assertFalse(stop.exists())
                self.assertEqual(control.snapshot()['phase'],'success')
            else:
                start.assert_not_called()
                self.assertEqual(stop.read_text(encoding='utf-8'),'原本人工停止')
                self.assertEqual(control.snapshot()['phase'],'success')
            self.assertTrue(control.start_done.is_set())

    def test_running_session_is_rechecked_after_save(self):self.run_control()
    def test_manual_stopped_session_is_never_started_by_settings(self):self.run_control(resume=False)
    def test_new_manual_stop_during_settings_prevents_resume(self):self.run_control(stop_during_fan_check=True)

    def test_startup_targets_follow_saved_configuration_and_protection_remains(self):
        (self.root/'TEST_排程規則.py').write_text(STAGED.read_text(encoding='utf-8'),encoding='utf-8')
        data=json.loads((self.root/'TEST_設定.json').read_text(encoding='utf-8'))
        data['功耗排程'].update(尖峰風扇百分比=90,離峰風扇百分比=90,平日固定功耗上限W=200)
        (self.root/'TEST_設定.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        spec=importlib.util.spec_from_file_location('隔離排程規則',self.root/'TEST_排程規則.py')
        rules=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rules)
        self.assertEqual((rules.DAY_FAN,rules.NIGHT_FAN,rules.DAY_POWER),(90,90,200))
        self.assertEqual(rules.period_profile(data['功耗排程'],True)[0:3:2],(200,90))
        with self.assertRaises(RuntimeError):rules.period_profile(data['功耗排程'],True,hardware_limit=180)
        sample={'風扇':[{'實際百分比':90}]*2,'控制':[{'設定百分比':90,'模式':'手動'}]*2,
                '核心C':80,'熱點C':100,'顯存C':54}
        self.assertEqual(rules.decide(True,True,200,200,sample,60,True,100)[0],180)
        sample['核心C']=82
        with self.assertRaises(RuntimeError):rules.fan_check(sample)
        sample['核心C']=None
        with self.assertRaises(RuntimeError):rules.fan_check(sample)

    def test_board_verification_uses_same_modern_powershell_as_monitor(self):
        from TEST_顯卡讀值 import ReadOnlyGpu
        sample={'風扇':[{'實際百分比':80,'RPM':2500}]*2,'控制':[{'設定百分比':80,'模式':'手動'}]*2,
                '核心C':40,'熱點C':45,'顯存C':44}
        with patch('TEST_顯卡讀值.ReadOnlyGpu') as reader,patch.object(settings.shutil,'which',return_value='pwsh.EXE'), \
             patch.object(settings.subprocess,'run') as command:
            reader.return_value.read.return_value=sample
            command.return_value.returncode=0
            command.return_value.stdout=json.dumps({'fans':[{'percent':50,'rpm':1000}]*3})
            settings.verify_fans({'gpu_fan':80,'board_fan':50},lambda:None)
            self.assertEqual(command.call_args.args[0][0],'pwsh.EXE')

    def test_background_launcher_does_not_capture_daemon_pipes(self):
        with patch.object(settings,'ROOT',self.root),patch.object(settings.subprocess,'run') as command:
            command.return_value.returncode=0
            settings.workers('啟動風扇')
            self.assertNotIn('capture_output',command.call_args.kwargs)
            self.assertIn('stdout',command.call_args.kwargs)


if __name__=='__main__':unittest.main()
