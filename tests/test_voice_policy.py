import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from test_async_emoji import Event
from data.plugins.astrbot_plugin_proactive_agent.voice_memory import explicit_voice_request,install_voice_memory,prepare_voice_state,voice_note,voice_issues
from astrbot.api.message_components import Record,Plain
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.star_handler import star_handlers_registry


class VoicePolicy(unittest.IsolatedAsyncioTestCase):
    def test_request_not_history_negation_quotation_or_configuration(self):
        for text in ['发条语音','能用语音回复我吗','用声音说你好','我想听你的声音','刚才没听清，再发一条语音','用语音说“你好”','用语音','语音回我','换成语音吧','用语音给我道歉','用语音卖个萌','你能生成一段语音吗']:
            self.assertTrue(explicit_voice_request(text),text)
        for text in ['别发语音','发语音吧，不用发语音了','你刚才发的语音说什么','你发过语音吗','我发语音你能听懂吗','我用语音说给你听','不是让你发语音','语音概率设置10%','他说“发语音”是什么意思','今天好开心']:
            self.assertFalse(explicit_voice_request(text),text)

    async def test_forced_voice_and_one_character_preserve_normal_ten_percent_setting(self):
        config=copy.deepcopy(DEFAULT_CONFIG)
        config['provider_tts_settings'].update(enable=True,trigger_probability=.1,dual_output=False,use_file_service=False)
        config['platform_settings']['segmented_reply']['enable']=False
        config['content_safety']['also_use_in_response']=False
        config['t2i']=False
        provider=SimpleNamespace(get_audio=AsyncMock(return_value='generated.wav'))
        ctx=SimpleNamespace(astrbot_config=config,plugin_manager=SimpleNamespace(context=SimpleNamespace(
            get_using_tts_provider_async=AsyncMock(return_value=provider))))
        stage=ResultDecorateStage();await stage.initialize(ctx)
        plugin=SimpleNamespace(_platform=lambda:None,_cfg={}.get,_audit=lambda *_:None)
        restore=install_voice_memory(plugin)
        try:
            with patch.object(star_handlers_registry,'get_handlers_by_event_type',return_value=[]),patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=True)),patch('astrbot.core.pipeline.result_decorate.stage.random.random',return_value=.9):
                for request,reply,wanted in [('发条语音','好呀',True),('用语音回复我','嗯',True),('用语音给我道歉','本龙错了',True),('用语音卖个萌','摸摸龙角嘛',True),('今天真开心','好呀',False)]:
                    e=Event(text=request);e.result.chain=[Plain(reply)]
                    async for _ in stage.process(e):pass
                    self.assertEqual(any(isinstance(p,Record) for p in e.result.chain),wanted)
                    if wanted:self.assertEqual(e.get_extra('qqbot_voice_result_text'),reply)
                self.assertEqual(stage.tts_trigger_probability,.1)
                self.assertEqual(config['provider_tts_settings']['trigger_probability'],.1)
        finally:restore()

    async def test_capability_uses_local_session_and_provider_without_ordinary_turn_lookup(self):
        provider=AsyncMock(return_value=object())
        plugin=SimpleNamespace(context=SimpleNamespace(get_config=lambda **_: {'provider_tts_settings':{'enable':True}},
                               get_using_tts_provider_async=provider),_audit=lambda *_:None)
        with patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=True)):
            ordinary={};await prepare_voice_state(plugin,Event('今天好开心'),ordinary)
            self.assertEqual(ordinary,{});provider.assert_not_awaited()
            state={};await prepare_voice_state(plugin,Event('用语音给我道歉'),state)
            self.assertTrue(state['voice_capability']['available'])
            self.assertTrue(state['voice_capability']['explicit'])
            self.assertIn('旧聊天',voice_note(state))
            self.assertTrue(voice_issues('现在发不了新语音，先文字版：摸摸龙角嘛',state))
            self.assertTrue(voice_issues('这句先打字，新的语音现在生成不了',state))
            self.assertEqual(voice_issues('对不起，本龙说重了',state),[])
            self.assertEqual(voice_issues('刚才说发不了语音是本龙说错了',state),[])
            self.assertTrue(voice_issues('刚才说错了，现在还是没法发语音',state))
        with patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=False)):
            disabled={};await prepare_voice_state(plugin,Event('发语音'),disabled)
            self.assertFalse(disabled['voice_capability']['available'])

    async def test_false_denial_cannot_pass_local_or_model_review(self):
        from data.plugins.astrbot_plugin_proactive_agent.reply_pipeline import fast_path_issues
        from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer
        import json
        state={'voice_capability':{'configured':True,'available':True,'explicit':True},
               'voice_receipts':'\n平台成功回执：先前那句确实以语音发送'}
        draft='现在没法生成新的语音，只能先打字认错'
        self.assertTrue(fast_path_issues(draft,'用语音给我道歉',state=state))
        generate=AsyncMock(side_effect=[json.dumps({'pass':False,'natural':False,'chat_fit':False,'replacement':'对不起，本龙说重了'}),
                                       json.dumps({'pass':True,'natural':True,'chat_fit':True})])
        reviewer=ReplyReviewer(generate,lambda *_:None)
        actual=await reviewer.review(draft,origin='test:FriendMessage:1',recent=['用语音给我道歉'],character=state)
        self.assertEqual(actual,'对不起，本龙说重了')
        self.assertIn('已启用语音输出',generate.await_args_list[0].args[0])
        self.assertIn('平台成功回执：先前那句确实以语音发送',generate.await_args_list[0].args[0])
        self.assertTrue(generate.await_args_list[0].args[1]['必须修正的问题'])

    async def test_generation_and_review_share_receipts_even_without_meme_context(self):
        plugin=object.__new__(Main);plugin.config={'meme_context_enabled':False}
        plugin.context=SimpleNamespace(get_config=lambda **_: {'provider_tts_settings':{'enable':True}},
                                       get_using_tts_provider_async=AsyncMock(return_value=object()))
        proof='\n成功回执确认的语音原文：摸摸龙角嘛'
        plugin._delivery=SimpleNamespace(note=lambda *_:'',voice_context=lambda *_:proof)
        event=Event('你刚才是用语音还是文字回复的')
        req=SimpleNamespace(prompt=event.get_message_str(),system_prompt='角色设定',conversation=SimpleNamespace(cid='same-chat'))
        with patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=True)):
            await plugin.enrich_conversation(event,req)
        self.assertIn(proof,req.system_prompt)
        self.assertIn(proof,voice_note(plugin.character_context(event)))
        self.assertIn('已启用语音输出',req.system_prompt)
        self.assertEqual(req.system_prompt.count(proof),1)
        state=plugin.character_context(event)
        state['voice_capability']['available']=False
        self.assertIn(proof,voice_note(state))
