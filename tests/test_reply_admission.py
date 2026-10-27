import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest
from test_fullchain_runtime import Runtime
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.reply_admission import initial_admission, has_agent_content
from data.plugins.astrbot_plugin_proactive_agent.fullchain import install_fullchain
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
from astrbot.api.message_components import At, Plain, Image, Reply, Face
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.process_stage.method.agent_request import AgentRequestSubStage


def handler(name, module='astrbot.builtin_stars.astrbot.main', filters=()):
    return SimpleNamespace(handler_name=name,handler_module_path=module,event_filters=list(filters))


def empty_event(mid='empty'):
    e=event(id_=mid,text='');e.is_at_or_wake_command=True;e.stop_event=lambda:None
    e.get_messages=lambda:[Face(id=1)]
    e.set_extra('activated_handlers',[handler(n) for n in
        ('handle_session_control_agent','handle_empty_mention','persist_group_message','on_message')]
        +[handler('track_session','data.plugins.astrbot_plugin_proactive_agent.main')])
    return e


class AdmissionPolicy(unittest.TestCase):
    def test_empty_native_face_skips_but_commands_plugins_media_and_at_survive(self):
        e=empty_event();self.assertIsNone(initial_admission(None,e))
        self.assertFalse(has_agent_content(e))
        for component in (Image(file='file:///test.png'),Reply(id=1),At(qq='self')):
            e.get_messages=lambda c=component:[c]
            self.assertIsNotNone(initial_admission(None,e))
        e=empty_event();e.set_extra('activated_handlers',[handler('custom','external.plugin')])
        self.assertEqual(initial_admission(None,e),'plugin_handler')
        e.set_extra('activated_handlers',[handler('cmd',filters=[CommandFilter('status')])])
        self.assertEqual(initial_admission(None,e),'command')
        e=empty_event();e.set_extra('provider_request',object())
        self.assertEqual(initial_admission(None,e),'provider_request')

    def test_probability_is_never_rerolled_or_overridden(self):
        e=event();e.is_at_or_wake_command=False;e.set_extra('qqbot_group_wake',None)
        self.assertIsNone(initial_admission(None,e))
        e.is_at_or_wake_command=True;e.set_extra('qqbot_group_wake','probability')
        self.assertEqual(initial_admission(None,e),'wake_content')

    def test_empty_onebot_notice_and_request_are_observed_without_reply_admission(self):
        for post_type in ('notice','request'):
            e=empty_event();e.get_messages=lambda:[]
            e.message_obj.raw_message={'post_type':post_type}
            self.assertIsNone(initial_admission(None,e))


class AdmissionRuntime(Runtime):
    async def test_passive_flood_runs_observers_without_queue_or_version_churn(self):
        self.cfg['reply_queue_concurrency']=1
        held=self.queue.create(event(id_='held'));self.queue.submit(held)
        pending=self.queue.create(event(id_='pending'));self.queue.submit(pending)
        version=self.queue.version;seen=[]
        async def process(stage,e):seen.append(e.message_obj.message_id);yield
        async def execute(scheduler,e):
            async for _ in ProcessStage.process(None,e):pass
        self.plugin._ai_profiles=SimpleNamespace(observe=Mock(),end_turn=Mock())
        with patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute):
            restore=install_fullchain(self.plugin)
            try:
                with patch.object(self.queue,'pump',wraps=self.queue.pump) as pump:
                    await asyncio.wait_for(asyncio.gather(*[
                        PipelineScheduler.execute(None,empty_event(str(i))) for i in range(500)]),3)
                    pump.assert_not_called()
                self.assertEqual(len(seen),500)
                self.assertEqual(self.plugin._ai_profiles.observe.call_count,500)
                self.assertEqual(self.queue.version,version)
                self.assertEqual(self.queue.pending,[pending.id])
                self.assertEqual(len(self.queue.jobs),2)
                self.assertEqual(self.queue.snapshot()['recent'],[])
                for kind,row in self.audit:
                    if kind=='reply_job_end':self.assertNotIn('reply_queue_ms',row['stages_ms'])
                # Passive traffic must not invalidate a pending manual edit.
                self.queue.action({'instance':self.queue.instance,'version':version,
                    'request_id':'manual-after-passive','job_id':pending.id,'action':'delete'})
            finally:restore()

    async def test_observer_created_provider_request_joins_queue_exactly_once(self):
        self.cfg['reply_queue_concurrency']=1
        held=self.queue.create(event(id_='held'));self.queue.submit(held)
        produced=asyncio.Event();executed=[]
        async def agent(stage,e):executed.append(e.message_obj.message_id);yield
        async def process(stage,e):
            e.set_extra('provider_request',object());produced.set()
            async for x in AgentRequestSubStage.process(None,e):yield x
        async def execute(scheduler,e):
            async for _ in ProcessStage.process(None,e):pass
        with patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute),patch.object(AgentRequestSubStage,'process',agent):
            restore=install_fullchain(self.plugin);task=None
            try:
                e=empty_event();task=asyncio.create_task(PipelineScheduler.execute(None,e))
                await produced.wait();await asyncio.sleep(0)
                self.assertEqual(executed,[]);self.assertEqual(len(self.queue.pending),1)
                self.queue.finish(held,'confirmed');await asyncio.wait_for(task,1)
                self.assertEqual(executed,['empty'])
                self.assertEqual(e.get_extra('qqbot_reply_job').admission,'handler_provider_request')
            finally:
                if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                restore()
