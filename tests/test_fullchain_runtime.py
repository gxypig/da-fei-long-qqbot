import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,install_delivery_transport
from data.plugins.astrbot_plugin_proactive_agent.fullchain import install_fullchain
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from aiocqhttp import CQHttp

class Runtime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=DeliveryLedger(Path(self.temp.name)/'ledger.db');await self.ledger.start();self.addAsyncCleanup(self.ledger.close)
        self.platform=SimpleNamespace(bot=object());self.cfg={'platform_id':'p','reply_queue_enabled':True,'reply_queue_concurrency':2}
        self.queue=ReplyQueue(self.cfg.get);self.audit=[]
        self.plugin=SimpleNamespace(_cfg=self.cfg.get,_platform=lambda:self.platform,_reply_queue=self.queue,_delivery=self.ledger,
            _audit=lambda kind,value:self.audit.append((kind,value)),context=SimpleNamespace(conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='cid'))))

    async def test_raw_entry_to_receipt_records_owner_and_all_terminal_states(self):
        # Windows Python 3.12 monotonic timers may have a 15.6ms resolution.
        async def convert(adapter,raw):await asyncio.sleep(.04);return SimpleNamespace(message_id=str(raw['message_id']))
        async def process(stage,e):
            if e.is_at_or_wake_command:
                await CQHttp.call_action(self.platform.bot,'send_group_msg',group_id=e.get_group_id(),message=e.get_message_str())
            yield
        async def execute(scheduler,e):
            async for _ in ProcessStage.process(None,e):pass
        transport=AsyncMock(return_value={'message_id':101})
        with patch.object(AiocqhttpAdapter,'convert_message',convert),patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute),patch.object(CQHttp,'call_action',transport):
            restore_delivery=install_delivery_transport(self.plugin);restore=install_fullchain(self.plugin)
            try:
                for wanted in [True,False]:
                    message=await AiocqhttpAdapter.convert_message(self.platform,{'post_type':'message','group_id':'1','user_id':'a','message_id':str(wanted),'raw_message':'你好'})
                    e=event();e.message_obj=message;e.is_at_or_wake_command=wanted;e.stop_event=lambda:None
                    await PipelineScheduler.execute(None,e)
                    job=e.get_extra('qqbot_reply_job')
                    self.assertIs(job.owner,asyncio.current_task())
                    self.assertEqual(job.state,'confirmed' if wanted else 'ignored')
                    self.assertGreater(job.timings['conversion_ms'],5)
                self.assertEqual(transport.await_count,1)
                self.assertEqual(len([k for k,_ in self.audit if k=='reply_job_end']),2)
            finally:restore();restore_delivery()

    async def test_direct_plugin_tasks_are_admitted_before_expensive_work(self):
        release=asyncio.Event();entered=[]
        async def process(stage,e):
            entered.append(e.message_obj.message_id);await release.wait()
            await CQHttp.call_action(self.platform.bot,'send_group_msg',group_id=e.get_group_id(),message='结果')
            yield
        async def execute(scheduler,e):
            async for _ in ProcessStage.process(None,e):pass
        with patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute),patch.object(CQHttp,'call_action',AsyncMock(return_value={'message_id':1})):
            delivery=install_delivery_transport(self.plugin);restore=install_fullchain(self.plugin)
            tasks=[]
            try:
                for group,mid in [('1','1'),('1','2'),('2','3')]:
                    e=event(origin='p:GroupMessage:'+group,id_=mid,text='帮我找视频')
                    e.is_at_or_wake_command=True;e.stop_event=lambda:None
                    tasks.append(asyncio.create_task(PipelineScheduler.execute(None,e)))
                for _ in range(8):await asyncio.sleep(0)
                self.assertEqual(entered,['1','3'])
                release.set();await asyncio.gather(*tasks)
                self.assertEqual(entered,['1','3','2'])
            finally:
                release.set();await asyncio.gather(*tasks,return_exceptions=True);restore();delivery()

    async def test_shutdown_drains_active_without_replaying_pending(self):
        entered=asyncio.Event();gate=asyncio.Event()
        async def process(stage,e):entered.set();await gate.wait();yield
        async def execute(scheduler,e):
            async for _ in ProcessStage.process(None,e):pass
        with patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute):
            restore=install_fullchain(self.plugin);tasks=[]
            try:
                for mid in ['1','2']:
                    e=event(id_=mid);e.is_at_or_wake_command=True;e.stop_event=lambda:None
                    tasks.append(asyncio.create_task(PipelineScheduler.execute(None,e)))
                await entered.wait();await self.queue.drain(.01);await asyncio.gather(*tasks,return_exceptions=True)
                self.assertTrue(self.queue.closed);self.assertFalse(self.queue.pending);self.assertFalse(self.queue.active)
                self.assertTrue(all(j.state=='cancelled' for j in self.queue.jobs.values()))
            finally:restore()
