import unittest
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.adaptive_limits import AdaptiveLimits

class RecoveryTests(unittest.TestCase):
    def setUp(self):self.now=0.;self.a=AdaptiveLimits(lambda:self.now)
    def healthy(self,n=10,seconds=4):
        for _ in range(n):
            self.now+=seconds;self.a.finish('a',20,seconds)
    def degraded(self):
        self.a.finish('a',0,1,status=429);self.now=301
    def test_recovery_without_baseline_and_without_twenty_per_minute(self):
        self.degraded();self.healthy();self.a.touch('a',1,queued=True)
        self.assertEqual(self.a.limit('a'),2);self.assertTrue(self.a.public('a')['probation'])
        self.assertIsNone(self.a.public('a')['baseline2'])
        self.healthy();self.assertFalse(self.a.public('a')['probation'])
        self.assertEqual(self.a.public('a')['baseline2'],4);self.assertEqual(self.a.limit('a'),2)
    def test_needs_queue_cooldown_and_recent_ten(self):
        self.degraded();self.healthy(9);self.a.touch('a',1,queued=True);self.assertEqual(self.a.limit('a'),1)
        self.healthy(1);self.a.touch('a',1,queued=False);self.assertEqual(self.a.limit('a'),1)
        self.now+=601;self.a.touch('a',1,queued=True);self.assertEqual(self.a.limit('a'),1)
        self.assertEqual(self.a.public('a')['recovery_successes'],0)
    def test_failed_trial_reverts_and_requires_new_evidence(self):
        for failure in ({'status':503},{'status':504},{'timed_out':True},{'outcome':'failed'}):
            with self.subTest(failure=failure):
                self.setUp();self.degraded();self.healthy();self.a.touch('a',1,queued=True)
                self.a.finish('a',0,1,**failure)
                self.assertEqual(self.a.limit('a'),1);self.assertEqual(self.a.public('a')['recovery_successes'],0)
                self.assertGreater(self.a.public('a')['cooldown_until'],self.now)
    def test_failure_never_establishes_healthy_baseline(self):
        self.a.touch('a',2,queued=True)
        for _ in range(40):
            self.now+=3;self.a.finish('a',20,2,status=503,outcome='failed');self.a.touch('a',2,queued=True)
        self.assertIsNone(self.a.public('a')['baseline2']);self.assertEqual(self.a.limit('a'),2)
    def test_cancel_does_not_count_as_success_or_make_trial_fail(self):
        self.degraded();self.healthy();self.a.touch('a',1,queued=True)
        self.a.finish('a',0,0,outcome='cancelled')
        self.assertTrue(self.a.public('a')['probation']);self.assertEqual(self.a.public('a')['recovery_successes'],10)
    def test_midrange_hold_reason_is_current(self):
        s=self.a.state('a');s['baseline2']=10;s['reason']='样本不足，保持并发'
        s['windows'].extend([dict(samples=25,queue_p95=20,execution_p95=11.5,errors=False,utilization=1)]*2)
        self.a._evaluate(s,500);self.assertNotIn('样本不足',s['reason'])

if __name__=='__main__':unittest.main()
