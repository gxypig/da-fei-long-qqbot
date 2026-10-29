import asyncio,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_maintenance import Main
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.session_mute import SessionMutes
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,install_delivery_transport
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
from data.plugins.astrbot_plugin_proactive_agent.fullchain import install_fullchain
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from aiocqhttp import CQHttp

class MutedObservers(unittest.IsolatedAsyncioTestCase):
    async def test_group_friend_commands_and_mentions_observed_without_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'platform_id':'p','reply_queue_enabled':True};q=ReplyQueue(cfg.get)
            ledger=DeliveryLedger(Path(folder)/'ledger.db');await ledger.start()
            mute=SessionMutes(Path(folder)/'mutes.json',q,ledger);await mute.start()
            for origin in ('p:GroupMessage:1','p:FriendMessage:2'):
                await mute.change(origin,True,None,0,origin)
            observed=AsyncMock();learning=SimpleNamespace(enqueue_meme=Mock(),confirmed_job=Mock())
            profiles=SimpleNamespace(observe=Mock(),end_turn=Mock())
            plugin=SimpleNamespace(_cfg=cfg.get,_reply_queue=q,_delivery=ledger,_mutes=mute,
                _platform=lambda:None,_audit=Mock(),_ai_profiles=profiles,_learning=learning,track_session=observed,
                context=SimpleNamespace(conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='cid'))))
            generated=[]
            async def process(stage,e):generated.append(e.get_message_str());yield
            async def execute(stage,e):
                async for _ in ProcessStage.process(None,e):pass
            with patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute):
                restore=install_fullchain(plugin)
                try:
                    for origin in ('p:GroupMessage:1','p:GroupMessage:a_1','p:FriendMessage:2'):
                        for text in ('@大肥龙 你好','/主动状态','这图真好笑'):
                            e=event(origin=origin,text=text,id_=origin+text);e.stop_event=Mock();e.is_at_or_wake_command=True
                            if ':FriendMessage:' in origin:e.get_group_id=lambda:''
                            await PipelineScheduler.execute(None,e)
                            self.assertEqual(e.get_extra('qqbot_reply_job').state,'cancelled')
                    self.assertFalse(generated);self.assertEqual(observed.await_count,9)
                    self.assertEqual(profiles.observe.call_count,9);self.assertFalse(q.active);self.assertFalse(q.pending)
                    other=event(origin='p:GroupMessage:3');other.is_at_or_wake_command=True;other.stop_event=Mock()
                    await PipelineScheduler.execute(None,other);self.assertEqual(generated,['你好'])
                finally:restore();q.close();await mute.close();await ledger.close()
            self.assertFalse(ledger.store.pending);self.assertTrue(ledger.store.closed)
