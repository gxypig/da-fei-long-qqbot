import asyncio,unittest
from types import SimpleNamespace
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import ModelScheduler,WorkRuntime,model_work
from data.plugins.astrbot_plugin_proactive_agent.wire_admission import Frame,one_attempt,RetryBudgetExhausted,scope
from data.plugins.astrbot_plugin_proactive_agent.adaptive_limits import AdaptiveLimits

def provider():return SimpleNamespace(provider_config={'provider_source_id':'test-source'})
async def spin(predicate):
    for _ in range(300):
        if predicate():return
        await asyncio.sleep(.001)
    raise AssertionError('condition did not become true')

class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler=ModelScheduler({'model_concurrency':2});self.addAsyncCleanup(self.scheduler.close)
    async def test_retry_backoff_has_no_slot_and_other_request_can_run(self):
        failed=asyncio.Event();retry=asyncio.Event();order=[];frame=Frame(self.scheduler,provider(),'interactive',2)
        async def error():raise OSError('temporary')
        async def task():
            try:await one_attempt(frame,error)
            except OSError:failed.set();await retry.wait()
            return await one_attempt(frame,lambda:answer('retry'))
        async def answer(name):order.append(name);return name
        pending=asyncio.create_task(task());await failed.wait()
        self.assertEqual(self.scheduler.status()['running'],0)
        self.assertEqual(await self.scheduler.run(provider(),lambda:answer('other')),'other')
        retry.set();await pending;self.assertEqual(order,['other','retry'])
    async def test_shared_budget_across_multiple_native_retry_loops(self):
        frame=Frame(self.scheduler,provider(),'interactive',2);calls=0
        async def error():
            nonlocal calls
            calls+=1;raise OSError('temporary')
        for _ in range(2):
            with self.assertRaises(OSError):await one_attempt(frame,error)
        with self.assertRaises(RetryBudgetExhausted):await one_attempt(frame,error)
        self.assertEqual(calls,2)
    async def test_stream_holds_actual_slot_until_closed_and_cleans_native_stream(self):
        closed=[]
        class Stream:
            def __aiter__(self):return self
            async def __anext__(self):return 'chunk'
            async def close(self):closed.append(True)
        async def request():return Stream()
        async with scope(self.scheduler,provider(),'interactive',streaming=True) as frame:
            stream=await one_attempt(frame,request)
            self.assertEqual(self.scheduler.status()['running'],1)
            self.assertEqual(await anext(stream),'chunk')
        self.assertTrue(closed);self.assertEqual(self.scheduler.status()['running'],0)
    async def test_review_generation_round_robin_borrows_unused_share(self):
        self.scheduler.cfg={'model_concurrency':1};gate=asyncio.Event();order=[]
        blocker=asyncio.create_task(self.scheduler.run(provider(),gate.wait));await spin(lambda:self.scheduler.status()['running']==1)
        async def run(label):order.append(label)
        jobs=[asyncio.create_task(self.scheduler.run(provider(),lambda label=kind+str(i):run(label),kind=kind))
              for kind in ('interactive','review') for i in range(3)]
        await spin(lambda:self.scheduler.status()['waiting']==6);gate.set();await asyncio.gather(blocker,*jobs)
        self.assertEqual([x.rstrip('012') for x in order],['review','interactive']*3)
    async def test_aged_schedules_are_one_per_ten_and_one_inflight(self):
        self.scheduler.cfg={'model_concurrency':1};self.scheduler.promotion_seconds=0
        gate=asyncio.Event();order=[]
        blocker=asyncio.create_task(self.scheduler.run(provider(),gate.wait));await spin(lambda:self.scheduler.status()['running']==1)
        async def run(label):order.append(label)
        jobs=[asyncio.create_task(self.scheduler.run(provider(),lambda kind=kind:run(kind),kind=kind))
              for kind in ['scheduled']*3+['interactive']*25]
        await spin(lambda:self.scheduler.status()['waiting']==28);gate.set();await asyncio.gather(blocker,*jobs)
        indices=[i for i,k in enumerate(order) if k=='scheduled'];self.assertEqual(indices[:2],[8,18])
        self.assertEqual(order[-1],'scheduled')

class AdaptiveTests(unittest.TestCase):
    def setUp(self):self.now=0.;self.a=AdaptiveLimits(lambda:self.now)
    def window(self,execution=10,queue=12,count=20,occupied=2):
        self.a.touch('a',occupied)
        for i in range(count):
            self.now+=59/count;self.a.finish('a',queue,execution)
        self.now+=1.001;self.a.touch('a',occupied)
    def test_only_two_sufficient_windows_can_increase(self):
        self.window();self.assertEqual(self.a.limit('a'),2)
        self.window();self.assertEqual(self.a.limit('a'),3)
    def test_insufficient_or_low_utilization_does_not_increase(self):
        self.window(count=19);self.window();self.assertEqual(self.a.limit('a'),2)
        self.window(occupied=1);self.window(occupied=1);self.assertEqual(self.a.limit('a'),2)
    def test_429_immediately_decreases_and_separates_sources(self):
        self.a.finish('a',0,1,status=429);self.assertEqual(self.a.limit('a'),1);self.assertEqual(self.a.limit('b'),2)
        self.assertEqual(self.a.public('a')['cooldown_until'],300)
    def test_two_slow_windows_decrease_and_cooldown_blocks_rise(self):
        self.window();self.window();self.assertEqual(self.a.limit('a'),3)
        self.window(execution=13,occupied=3);self.window(execution=13,occupied=3)
        self.assertEqual(self.a.limit('a'),2)
        self.window();self.window();self.assertEqual(self.a.limit('a'),2)
    def test_timeout_blocks_increase_without_cancelling_work(self):
        self.window();self.a.finish('a',15,10,timed_out=True);self.window();self.assertEqual(self.a.limit('a'),2)
