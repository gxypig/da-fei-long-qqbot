import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_chat_flow import event
from astrbot.api.message_components import Plain,Image
from data.plugins.astrbot_plugin_proactive_agent.fish_voice import review_before_synthesis
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import bind_request,approve_once,consume_approval
from data.plugins.astrbot_plugin_proactive_agent import review_modes

class VoicePreparation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token=review_modes._settings.set(None)
        self.cfg={'reply_review_enabled':True,'review_policy':'all'}
        self.plugin=SimpleNamespace(_cfg=self.cfg.get,_reviewer=SimpleNamespace(review=AsyncMock(return_value='先歇会')),
            review_context=lambda o:[],review_mode=lambda o:'chat',review_policy_version=lambda:'v1')
        self.plugin.review_settings=lambda:review_modes.current(self.cfg.get)
        self.event=event('用语音说说',group='100');self.origin='aiocqhttp:GroupMessage:100'
        bind_request(self.origin,[],False,{})
    async def asyncTearDown(self):review_modes._settings.reset(self.token)
    async def test_rewrite_precedes_synthesis_and_exact_text_has_send_receipt(self):
        result=SimpleNamespace(chain=[Plain('今天先歇会')])
        await review_before_synthesis(self.plugin,self.event,result)
        self.assertEqual(result.chain[0].text,'先歇会');self.plugin._reviewer.review.assert_awaited_once()
        self.assertTrue(consume_approval(self.origin,'先歇会',policy_version='v1'))
    async def test_already_reviewed_text_does_not_add_another_model_call(self):
        approve_once(self.origin,'今天先歇会',policy_version='v1')
        result=SimpleNamespace(chain=[Plain('今天先歇会')])
        await review_before_synthesis(self.plugin,self.event,result)
        self.plugin._reviewer.review.assert_not_awaited()
        self.assertTrue(consume_approval(self.origin,'今天先歇会',policy_version='v1'))
    async def test_off_and_mixed_media_keep_existing_boundary(self):
        for off,mixed in [(True,False),(False,True)]:
            self.cfg['reply_review_enabled']=not off;review_modes._settings.set(None)
            result=SimpleNamespace(chain=[Plain('今天先歇会')]+([Image.fromURL('https://example.com/image.png')] if mixed else []))
            await review_before_synthesis(self.plugin,self.event,result)
            self.plugin._reviewer.review.assert_not_awaited()
    async def test_later_edit_cannot_reuse_pre_voice_approval(self):
        result=SimpleNamespace(chain=[Plain('今天先歇会')]);await review_before_synthesis(self.plugin,self.event,result)
        self.assertFalse(consume_approval(self.origin,'内容被另一个插件改了',policy_version='v1'))
    async def test_long_split_speech_does_not_gain_whole_body_review(self):
        await review_before_synthesis(self.plugin,self.event,SimpleNamespace(chain=[Plain('很长的话'*60)]))
        self.plugin._reviewer.review.assert_not_awaited()
