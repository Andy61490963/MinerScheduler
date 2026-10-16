"""核對三十瓦短測、期限回復、原廠上限及過熱後禁止升瓦。"""
from context import BUILD
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import unittest
import TEST_排程規則 as rules

class TrialTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.schedule=json.loads((BUILD/'TEST_設定.json').read_text(encoding='utf-8'))['功耗排程']
        self.schedule['臨時功耗短測']={'識別碼':'a'*32,'開始UTC':(self.now-timedelta(seconds=1)).isoformat(),
            '結束UTC':(self.now+timedelta(minutes=19)).isoformat(),'步幅W':30,'功耗W':200}

    def test_authorized_daytime_trial_can_reach_450(self):
        self.schedule['臨時功耗短測']['功耗W']=450
        self.assertEqual(rules.period_profile(self.schedule,True)[0],450)
        with self.assertRaises(RuntimeError):rules.period_profile(self.schedule,True,hardware_limit=400)

    def test_only_authorized_increment_and_last_remainder(self):
        fresh=deepcopy(self.schedule)
        fresh['臨時功耗短測']['功耗W']=230
        self.assertTrue(rules.trial_raise_allowed(self.schedule,fresh,True,200,230,200,self.now))
        self.assertFalse(rules.trial_raise_allowed(self.schedule,fresh,True,200,260,200,self.now))
        self.assertFalse(rules.trial_raise_allowed(self.schedule,fresh,True,200,230,180,self.now))
        self.assertTrue(rules.trial_raise_allowed(self.schedule,fresh,True,440,450,440,self.now))
        fresh['臨時功耗短測']['識別碼']='b'*32
        self.assertFalse(rules.trial_raise_allowed(self.schedule,fresh,True,200,230,200,self.now))

    def test_expired_trial_restores_regular_day_and_night(self):
        self.schedule['臨時功耗短測'].update(開始UTC=(self.now-timedelta(minutes=19)).isoformat(),結束UTC=(self.now-timedelta(seconds=1)).isoformat(),功耗W=440)
        self.assertIsNone(rules.temporary_trial(self.schedule,self.now))
        self.assertEqual(rules.period_profile(self.schedule,True)[0],self.schedule['平日固定功耗上限W'])
        self.assertEqual(rules.period_profile(self.schedule,False)[0],self.schedule['離峰固定功耗上限W'])

    def test_invalid_permissions_and_unbounded_duration_are_rejected(self):
        for change in ({'功耗W':451},{'功耗W':True},{'步幅W':100},{'結束UTC':(self.now+timedelta(hours=1)).isoformat()}):
            schedule=deepcopy(self.schedule)
            schedule['臨時功耗短測'].update(change)
            with self.subTest(change=change),self.assertRaises(RuntimeError):rules.temporary_trial(schedule,self.now)

    def test_runtime_cannot_enable_trial_after_start(self):
        initial=deepcopy(self.schedule)
        initial.pop('臨時功耗短測')
        self.assertFalse(rules.trial_raise_allowed(initial,self.schedule,True,200,230,200,self.now))

    def test_fan_schedule_accepts_only_live_trial_power(self):
        import TEST_風扇排程 as fans
        trial=self.schedule['臨時功耗短測']
        trial['功耗W']=440
        guard={'臨時功耗短測':trial,'風扇目標百分比':80,'目標功耗上限W':440}
        self.assertEqual(fans.profile_for(True,{'功耗上限W':440},guard),fans.DAY)
        self.assertIsNone(fans.profile_for(True,{'功耗上限W':450},guard))
        trial['開始UTC']=(self.now-timedelta(minutes=19)).isoformat()
        trial['結束UTC']=(self.now-timedelta(seconds=1)).isoformat()
        self.assertEqual(fans.profile_for(True,{'功耗上限W':440},guard),fans.DAY if rules.DAY_POWER>=440 else None)

if __name__=='__main__':unittest.main()
