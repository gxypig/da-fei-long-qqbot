import asyncio,json,re,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer,ReviewUnavailable,bind_request
from data.plugins.astrbot_plugin_proactive_agent.voice_expression import plan
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import current_purpose
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
from data.plugins.astrbot_plugin_proactive_agent.fish_voice import transform

PASS={'pass':True,'natural':True,'chat_fit':True}
class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):bind_request('test:FriendMessage:1',[],False,{}, {'enabled':True,'mode':'full_review'})
    def reviewer(self,rows):return ReplyReviewer(AsyncMock(side_effect=[json.dumps(r,ensure_ascii=False) for r in rows]),lambda *_:None)
    async def test_certified_correction_finishes_in_same_call(self):
        r=self.reviewer([{**PASS,'pass':False,'reason':'指代误解','replacement':'你说哪个？','replacement_check':{**PASS,'facts_preserved':True}}])
        self.assertEqual(await r.review('对，群里叫我测试角色',origin='test:FriendMessage:1'),'你说哪个？')
        self.assertEqual(r.generate.await_count,1)
    async def test_uncertified_legacy_replacement_still_gets_reviewed(self):
        for cert in (None,{**PASS,'facts_preserved':False},{**PASS,'facts_preserved':'true'}):
            r=self.reviewer([{**PASS,'pass':False,'reason':'强行比喻','replacement':'好困就歇会儿','replacement_check':cert},PASS])
            self.assertEqual(await r.review('奇怪比喻',origin='test:FriendMessage:1'),'好困就歇会儿')
            self.assertEqual(r.generate.await_count,2)
            self.assertNotIn('强行比喻',r.generate.await_args.args[1]['必须修正的问题'])
    async def test_certificate_cannot_override_hard_checks(self):
        row={**PASS,'pass':False,'replacement':'[待生成]','replacement_check':{**PASS,'facts_preserved':True}}
        r=self.reviewer([row]*3)
        with self.assertRaises(ReviewUnavailable):await r.review('原稿',origin='test:FriendMessage:1')
    async def test_error_receipt_is_not_an_answer_or_tts_request(self):
        token=current_job.set(SimpleNamespace(reply_purpose='error_notification'))
        try:self.assertEqual(current_purpose(),'error_notification')
        finally:current_job.reset(token)
        event=SimpleNamespace(get_extra=lambda k:'template_receipt',get_result=lambda:object())
        await transform(None,None,event,force=True)

class VoiceTests(unittest.TestCase):
    def test_lively_is_more_expressive_but_bounded_and_preserves_text(self):
        text='哈哈，这次总算过了！哼哼，交给本龙。可惜了……下次再来。你没事吧？别怕，慢慢来。记住，明天再说'
        a,b=plan(text,intensity='natural'),plan(text,intensity='lively')
        self.assertGreater(len(b['cues']),len(a['cues']));self.assertLessEqual(len(b['cues']),8)
        self.assertEqual(re.sub(r'\[[^]]+\]','',b['text']),text)
        self.assertIn('[chuckling]',b['text'])
    def test_nonverbal_needs_literal_basis_and_quotes_do_not_trigger_it(self):
        for text in ('你今天很难过，我陪着你','他说“哈哈，笑死了”，我没笑','别笑，今天真倒霉'):
            self.assertNotIn('[chuckling]',plan(text)['text'])
        self.assertIn('[sighing]',plan('唉，又没赶上')['text'])
        self.assertIn('[break]',plan('等一下……找到了')['text'])
    def test_requested_tone_controls_without_extra_sounds(self):
        p=plan('哈哈，听得到吗',instruction='小声说')
        self.assertIn('[whispering]',p['text']);self.assertNotIn('[chuckling]',p['text'])
