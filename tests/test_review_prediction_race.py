import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_reply_pipeline import plugin
from test_chat_flow import event
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import bind_request, ReplyReviewer


class LatePrediction(unittest.IsolatedAsyncioTestCase):
    async def test_json_review_parks_parent_before_wait_for_child(self):
        from test_fullchain_foundation import event as queue_event
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue,current_job
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkRuntime
        p=plugin();p.config['review_low_latency']=False
        queue=ReplyQueue({'reply_queue_concurrency':1,'reply_inflight_limit':2}.get)
        job=queue.create(queue_event());queue.submit(job)
        job.owner=asyncio.current_task()
        token=current_job.set(job)
        work=object.__new__(WorkRuntime);work.reply_queue=queue;p._work=work
        async def generate(**kwargs):
            self.assertEqual(job.state,'suspended')
            self.assertNotIn(job.id,queue.active)
            self.assertIn(job.id,queue.inflight)
            other=queue.create(queue_event(origin='p:GroupMessage:2',id_='other'));queue.submit(other)
            self.assertEqual(other.state,'running');queue.finish(other,'confirmed')
            return SimpleNamespace(completion_text='{}')
        p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=SimpleNamespace(text_chat=generate)))
        try:
            self.assertEqual(await p._generate_json('test',{},'p:GroupMessage:1'),'{}')
            self.assertEqual(job.state,'running')
        finally:current_job.reset(token);queue.finish(job,'confirmed')

    async def setup_race(self, *, risk='low', reasons=None, review=None):
        self.p=plugin();self.e=event('下班买杯奶茶')
        self.e.set_extra('qqbot_character',{})
        self.started=asyncio.Event();self.finish=asyncio.Event();self.predict=asyncio.Event()
        self.cancelled=False;self.parking=[]
        @asynccontextmanager
        async def parked():
            self.parking.append('enter')
            try:yield
            finally:self.parking.append('exit')
        self.p._work=SimpleNamespace(parked_model=parked)
        async def default_review(*args,**kwargs):
            self.started.set()
            try:await self.finish.wait()
            except asyncio.CancelledError:self.cancelled=True;raise
            if kwargs.get('on_verdict'):kwargs['on_verdict']()
            return '完整审阅'
        self.p._reviewer.review=AsyncMock(side_effect=review or default_review)
        async def prediction():
            await self.predict.wait()
            if risk=='failed':raise RuntimeError('prediction unavailable')
            return {'risk':risk,'image_ids':[]}
        self.prediction=asyncio.create_task(prediction())
        self.e.set_extra('qqbot_reply_prediction',self.prediction)
        self.e.set_extra('qqbot_prediction_version',self.p.review_policy_version())
        bind_request(self.e.unified_msg_origin,['下班买杯奶茶'],False,{})
        self.task=asyncio.create_task(self.p.review_with_pending_prediction(self.e,'奶茶店先笑了',
            reasons=reasons or ['prediction_unavailable'],recent=['下班买杯奶茶'],mode='chat',media=0,
            prior_replies=[],state={},used_tools=False))
        await asyncio.wait_for(self.started.wait(),.5)

    async def asyncTearDown(self):
        tasks=[getattr(self,name,None) for name in ('task','prediction')]
        for task in tasks:
            if task and not task.done():task.cancel()
        await asyncio.gather(*(t for t in tasks if t),return_exceptions=True)

    async def test_low_prediction_reuses_checks_and_joins_cancelled_review(self):
        await self.setup_race();self.predict.set()
        self.assertEqual(await self.task,('奶茶店先笑了',True))
        self.assertTrue(self.cancelled);self.assertEqual(self.parking,['enter','exit'])

    async def test_unknown_high_cancelled_or_failed_prediction_waits_full_review(self):
        for risk in ('unknown','high','cancelled','failed'):
            await self.setup_race(risk=risk)
            if risk=='cancelled':self.prediction.cancel()
            else:self.predict.set()
            await asyncio.sleep(.01);self.assertFalse(self.task.done())
            self.finish.set();self.assertEqual(await self.task,('完整审阅',False))

    async def test_completed_review_wins_simultaneous_prediction(self):
        await self.setup_race();self.finish.set();self.predict.set()
        self.assertEqual(await self.task,('完整审阅',False))

    async def test_prior_rejection_cannot_be_overridden_while_rewrite_waits(self):
        async def reviewing(*args,**kwargs):
            kwargs['on_verdict']();self.started.set()
            await self.finish.wait();return '修正并复核后'
        await self.setup_race(review=reviewing);self.predict.set()
        await asyncio.sleep(.01);self.assertFalse(self.task.done())
        self.finish.set();self.assertEqual(await self.task,('修正并复核后',False))

    async def test_state_and_policy_changes_require_full_review(self):
        for field in ('state','policy'):
            await self.setup_race()
            if field=='state':self.e.set_extra('qqbot_character',{'mood':'stop'})
            else:self.p.config['emoji_probability']=.55
            self.predict.set();await asyncio.sleep(.01);self.assertFalse(self.task.done())
            self.finish.set();self.assertEqual(await self.task,('完整审阅',False))

    async def test_other_mandatory_reason_never_races(self):
        await self.setup_race(reasons=['prediction_unavailable','tool_or_media'])
        self.predict.set();await asyncio.sleep(.01);self.assertFalse(self.task.done())
        self.finish.set();self.assertEqual(await self.task,('完整审阅',False))
        self.assertEqual(self.parking,[])

    async def test_parent_cancel_joins_child_without_cancelling_shared_prediction(self):
        await self.setup_race();self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):await self.task
        self.assertTrue(self.cancelled);self.assertFalse(self.prediction.done())
        self.assertEqual(self.parking,['enter','exit'])

    async def test_cancellation_resistant_review_verdict_wins(self):
        async def resistant(*args,**kwargs):
            self.started.set()
            try:await self.finish.wait()
            except asyncio.CancelledError:kwargs['on_verdict']();return '仍然要用完整审阅'
        await self.setup_race(review=resistant);self.predict.set()
        self.assertEqual(await self.task,('仍然要用完整审阅',False))

    async def test_real_reviewer_marks_first_verdict_before_rewrite(self):
        sequence=[]
        answers=iter(['{"pass":false,"natural":false,"chat_fit":true,"replacement":"奶茶店先笑了"}',
                      '{"pass":true,"natural":true,"chat_fit":true}'])
        async def generate(*_):sequence.append('generate');return next(answers)
        reviewer=ReplyReviewer(generate,lambda *_:None)
        result=await reviewer.review('钱包又瘪了',origin='a',character={'persona_status':'selected'},
            on_verdict=lambda:sequence.append('verdict'))
        self.assertEqual(result,'奶茶店先笑了')
        self.assertEqual(sequence,['generate','verdict','generate','verdict'])

    async def test_raw_verdict_before_provider_cleanup_blocks_late_prediction(self):
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import _review_verdict_observer
        calls=[]
        async def generate(*_):
            calls.append(True)
            if len(calls)==1:
                # Mirrors a hedged request receiving a verdict while its losing
                # request/admission is still being cleaned up.
                _review_verdict_observer.get()()
                self.started.set();await self.finish.wait()
                return '{"pass":false,"natural":false,"chat_fit":true,"replacement":"钱包先替你心疼了"}'
            return '{"pass":true,"natural":true,"chat_fit":true}'
        reviewer=ReplyReviewer(generate,lambda *_:None)
        await self.setup_race(review=reviewer.review)
        self.predict.set();await asyncio.sleep(.01);self.assertFalse(self.task.done())
        self.finish.set();checked,fast=await self.task
        self.assertEqual(checked,'钱包先替你心疼了');self.assertFalse(fast)
        self.assertEqual(len(calls),2);self.assertIsNone(_review_verdict_observer.get())
