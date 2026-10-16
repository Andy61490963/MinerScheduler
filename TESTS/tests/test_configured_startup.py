"""保存的日間、離峰及週末功耗直接啟動，故障與溫控保護仍優先。"""
from context import BUILD
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import TEST_排程規則 as rules
import TEST_啟動核對 as startup
import TEST_功耗排程 as guard


class ConfiguredStartupTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((BUILD/'TEST_設定.json').read_text(encoding='utf-8-sig'))
        self.config['功耗排程'].update(平日固定功耗上限W=350,離峰固定功耗上限W=230)
        self.sample={'風扇':[{'實際百分比':rules.DAY_FAN,'RPM':2500}]*2,
                     '控制':[{'設定百分比':rules.DAY_FAN,'模式':'手動'}]*2,
                     '核心C':60,'熱點C':75,'顯存C':60}

    def target(self,watts=350,current=150,ceiling=None,cooling=0,now=1,allowed=True,fans=True,available=True):
        return rules.configured_target((watts,3105,rules.DAY_FAN),current,ceiling or watts,
                                       cooling,now,self.sample,allowed,fans,available)

    def test_saved_power_is_applied_before_first_minute_or_miner_creation(self):
        for watts in (150,230,350,450):
            with self.subTest(功耗W=watts):
                target,clock,*_=self.target(watts=watts)
                self.assertEqual(target,watts)
                self.assertEqual(clock,900 if watts==150 else 3105)

    def test_offpeak_and_weekend_use_saved_night_power(self):
        for moment,expected in (('2026-10-16T09:14:59+08:00',230),
                                ('2026-10-16T09:15:00+08:00',350),
                                ('2026-10-16T17:44:59+08:00',350),
                                ('2026-10-16T17:45:00+08:00',230),
                                ('2026-10-17T12:00:00+08:00',230)):
            with self.subTest(時刻=moment):
                peak=rules.daytime(datetime.fromisoformat(moment))
                watts=rules.period_profile(self.config['功耗排程'],peak)[0]
                self.assertEqual(self.target(watts=watts)[0],expected)
                info={'原廠最高核心時脈MHz':3105,'原廠功耗上限W':450,
                      '目標功耗上限W':watts,'顯卡':{'功耗上限W':watts}}
                with patch.object(startup,'daytime',return_value=peak):
                    self.assertEqual(startup.configured_power(info,self.config),expected)

    def test_safety_wait_overrides_requested_power(self):
        for kwargs in ({'allowed':False},{'fans':False},{'available':False}):
            with self.subTest(條件=kwargs):
                self.assertEqual(self.target(**kwargs)[:2],(150,900))

    def test_thermal_backoff_stays_lower_after_cooling(self):
        for name,value in (('核心C',80),('熱點C',105),('顯存C',90)):
            with self.subTest(感測器=name):
                original=self.sample[name]
                self.sample[name]=value
                target,clock,ceiling,until,_=self.target(current=350)
                self.assertEqual((target,ceiling,until),(330,330,61))
                self.sample[name]=original
                self.assertEqual(self.target(current=330,ceiling=ceiling,cooling=until,now=100)[0],330)

    def test_sensor_failure_and_core_stop_are_not_bypassed(self):
        for value in (None,float('nan'),82):
            self.sample['核心C']=value
            with self.assertRaises(RuntimeError):self.target()

    def test_unexpected_150w_is_rejected_when_saved_value_is_350w(self):
        info={'原廠最高核心時脈MHz':3105,'原廠功耗上限W':450,
              '目標功耗上限W':150,'顯卡':{'功耗上限W':150}}
        with patch.object(startup,'daytime',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'保存的時段設定'):
                startup.configured_power(info,self.config)

    def test_new_miner_gets_saved_power_without_elapsed_gate(self):
        data={'保護識別碼':'本輪','更新時間UTC':datetime.now(timezone.utc).isoformat(),
              'CPU挖礦':False,'礦工SHA256':guard.EXPECTED_MINER,
              '狀態':'挖礦中，核對持續負載','經過秒數':1,'礦工控制已關閉':False}
        self.assertTrue(guard.load_available('本輪',None,False))
        self.assertTrue(guard.load_available('本輪',data,False))
        self.assertFalse(guard.load_ready('本輪',data))
        self.assertEqual(self.target(available=guard.load_available('本輪',data,True))[0],350)

    def test_lost_stopped_wrong_or_stale_miner_falls_back(self):
        good={'保護識別碼':'本輪','更新時間UTC':datetime.now(timezone.utc).isoformat(),
              'CPU挖礦':False,'礦工SHA256':guard.EXPECTED_MINER,'狀態':'挖礦中'}
        bad=[None,{**good,'狀態':'已停止'},{**good,'CPU挖礦':True},
             {**good,'礦工SHA256':'未知'},{**good,'保護識別碼':'舊輪'},
             {**good,'更新時間UTC':(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()}]
        for data in bad:
            with self.subTest(負載=data):
                self.assertFalse(guard.load_available('本輪',data,True))
                self.assertEqual(self.target(available=guard.load_available('本輪',data,True))[0],150)


if __name__=='__main__':unittest.main()
