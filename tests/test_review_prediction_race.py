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

    async def test_late_prediction_does_not_start_a_parallel_reviewer(self):
        p=plugin();e=event('今天喝奶茶');e.set_extra('qqbot_character',{})
        gate=asyncio.Event()
        async def predict(*_):await gate.wait();return '{"risk":"low"}'
        p._generate_json=predict;p.prepare_reply_prediction(e)
        task=asyncio.create_task(p.review_with_pending_prediction(e,'好喝',reasons=['prediction_unavailable'],recent=[],mode='chat',media=0,prior_replies=[],state={},used_tools=False))
        await asyncio.sleep(.02);p._reviewer.review.assert_not_awaited();gate.set()
        self.assertEqual(await task,('好喝',True));p._reviewer.review.assert_not_awaited()
        await p._close_reply_pipeline()

    async def test_mandatory_tool_reply_never_waits_for_prediction(self):
        p=plugin();e=event('今天喝奶茶');e.set_extra('qqbot_character',{})
        gate=asyncio.Event();p._generate_json=AsyncMock(side_effect=gate.wait)
        prediction=p.prepare_reply_prediction(e)
        result=await asyncio.wait_for(p.review_with_pending_prediction(e,'查到了',reasons=['tool_or_media'],recent=[],mode='chat',media=0,prior_replies=[],state={},used_tools=True),.1)
        self.assertFalse(result[1]);p._reviewer.review.assert_awaited_once();await p._close_reply_pipeline()

    async def test_parent_cancel_joins_full_review_without_late_prediction_rescue(self):
        p=plugin();e=event('今天喝奶茶');e.set_extra('qqbot_character',{})
        entered=asyncio.Event();cancelled=[]
        async def full(*a,**k):
            entered.set()
            try:await asyncio.Future()
            except asyncio.CancelledError:cancelled.append(True);raise
        p._reviewer.review=full
        task=asyncio.create_task(p.review_with_pending_prediction(e,'好喝',reasons=['full_policy'],recent=[],mode='chat',media=0,prior_replies=[],state={},used_tools=False))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(cancelled)

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
