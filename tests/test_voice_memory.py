import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import AsyncMock,Mock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.voice_memory import install_voice_memory,record_transcript
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,install_delivery_transport,DeliveryUnconfirmed
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyJob,current_job
from astrbot.api.message_components import Record,Plain
from astrbot.api.event import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent as Event
from aiocqhttp import CQHttp
from test_fullchain_foundation import event


class Bot:
    async def send_private_msg(self,**kwargs):return await CQHttp.call_action(self,'send_private_msg',**kwargs)
    async def send_group_msg(self,**kwargs):return await CQHttp.call_action(self,'send_group_msg',**kwargs)


class VoiceMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'delivery.db'
        for name,value in [('a',0),('b',100)]:
            with wave.open(str(Path(self.temp.name)/(name+'.wav')),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(8000)
                output.writeframes(int(value).to_bytes(2,'little')*80)
        self.ledger=DeliveryLedger(self.path);await self.ledger.start();self.addAsyncCleanup(self.ledger.close)
        self.bot=Bot();self.transport=AsyncMock(return_value={'message_id':7})
        self.plugin=SimpleNamespace(_delivery=self.ledger,_platform=lambda:SimpleNamespace(bot=self.bot),
            _cfg={'platform_id':'p'}.get,context=SimpleNamespace(conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='c'))))
        self.patch=patch.object(CQHttp,'call_action',self.transport);self.patch.start();self.addCleanup(self.patch.stop)
        self.restore_delivery=install_delivery_transport(self.plugin);self.addCleanup(self.restore_delivery)
        self.restore_voice=install_voice_memory(self.plugin);self.addCleanup(self.restore_voice)

    async def send(self,parts,target='1',group=False):
        await Event.send_message(self.bot,MessageChain(parts),is_group=group,session_id=target)

    def audio(self,text=None,name='a'):
        return Record.fromFileSystem(Path(self.temp.name)/(name+'.wav'),text=text)

    def rows(self):return [r for r in self.ledger.store.rows.values() if r.get('attachments')]

    async def test_confirmed_tts_original_survives_restart_and_no_metadata_on_wire(self):
        job=ReplyJob('p:FriendMessage:1','a','m','question');job.cid='c'
        token=current_job.set(job)
        try:await self.send([self.audio('本龙说的是这句话')])
        finally:current_job.reset(token)
        row=self.rows()[0];self.assertEqual(row['state'],'confirmed')
        self.assertEqual(self.ledger.confirmed_text(job),'本龙说的是这句话')
        self.assertEqual(row['attachments'][0]['transcript_source'],'tts_input')
        wire=self.transport.call_args.kwargs['message'][0]
        self.assertEqual(set(wire['data']),{'file'})
        self.assertIsNone(record_transcript(wire,self.bot,'p:FriendMessage:1'))
        await self.ledger.close()
        reopened=DeliveryLedger(self.path);await reopened.start()
        try:
            note=reopened.note('p:FriendMessage:1','c')
            self.assertIn('本龙说的是这句话',note)
            self.assertIn('发送形式',note);self.assertIn('语音',note)
            self.assertNotIn('本龙说的是这句话',reopened.note('p:FriendMessage:2','c'))
        finally:await reopened.close()

    async def test_concurrent_sessions_do_not_mix_voice_text_even_with_identical_audio(self):
        async def slow(*args,**kwargs):await asyncio.sleep(.01);return {'message_id':7}
        self.transport.side_effect=slow
        await asyncio.gather(self.send([self.audio('甲的语音')],target='1'),
                             self.send([self.audio('乙的语音')],target='2',group=True))
        self.assertEqual({r['origin']:r['text'] for r in self.rows()},
                         {'p:FriendMessage:1':'甲的语音','p:GroupMessage:2':'乙的语音'})

    async def test_failure_unknown_and_audio_without_text_never_become_spoken_facts(self):
        for response in ({'retcode':100,'status':'failed'},{}):
            self.transport.return_value=response
            with self.assertRaises(DeliveryUnconfirmed):await self.send([self.audio('尚未确认')])
        self.assertNotIn('尚未确认',self.ledger.note('p:FriendMessage:1','c'))
        self.transport.return_value={'message_id':8}
        await self.send([self.audio()])
        row=self.rows()[-1];self.assertEqual(row['text'],'')
        self.assertNotIn('transcript',row['attachments'][0])

    async def test_segmented_dual_output_stores_once_in_send_order(self):
        await self.send([self.audio('第一句'),Plain('第一句'),
                         self.audio('第二句','b'),Plain('第二句')])
        self.assertEqual(self.rows()[0]['text'],'第一句\n第二句')

    async def test_conflicting_text_for_same_audio_is_unknown(self):
        await self.send([self.audio('甲'),self.audio('乙')])
        self.assertEqual(self.rows()[0]['text'],'')

    async def test_actual_voice_is_visible_in_journal_and_delivery_timing(self):
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkRuntime,install_work_runtime
        from data.plugins.astrbot_plugin_proactive_agent.live_journal import install_live_journal
        cfg={'platform_id':'p','latency_optimization_enabled':True}
        self.plugin._cfg=cfg.get;audits=[]
        self.plugin._audit=lambda kind,row:audits.append((kind,row))
        self.plugin._live_journal=SimpleNamespace(add=Mock(),error=None)
        runtime=self.plugin._work=WorkRuntime(cfg.get,self.plugin._audit)
        self.addAsyncCleanup(runtime.close)
        live=install_live_journal(self.plugin);self.addCleanup(live)
        work=install_work_runtime(self.plugin);self.addCleanup(work)
        e=event(origin='p:FriendMessage:1')
        e.set_extra('qqbot_received_at',runtime.clock())
        e.set_extra('qqbot_final_text','已审阅的语音')
        with runtime.pipeline_work(e),runtime.reply_work(e):
            runtime.mark(e,'final_ready')
            await self.send([self.audio('已审阅的语音')])
        self.assertEqual(self.plugin._live_journal.add.call_args.args[1],'[语音] 已审阅的语音')
        deliveries=[row for kind,row in audits if kind=='reply_text_delivery']
        self.assertEqual(len(deliveries),1);self.assertEqual(deliveries[0]['via'],'voice_final_ready')

    async def test_interleaved_unload_does_not_duplicate_or_break_transport(self):
        second=install_voice_memory(self.plugin)
        self.addCleanup(second)
        self.restore_voice()
        await self.send([self.audio('仍然正常')])
        self.assertEqual(self.rows()[0]['text'],'仍然正常')
        second();await self.send([self.audio('卸载后透传','b')])
        self.assertEqual(self.rows()[-1]['text'],'')
        self.assertEqual(self.transport.await_count,2)
