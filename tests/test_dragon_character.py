import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_maintenance import ROOT, Main
from test_conversation import encoded
from data.plugins.astrbot_plugin_proactive_agent.dragon_character import CharacterState, limit_emoji, speech_issues, proactive_note
from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer, bind_request, request_scope
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.image_intent import is_generation_request, is_library_request
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image


def event(text, sender="12345", group=""):
    extra = {}
    return SimpleNamespace(unified_msg_origin=f"aiocqhttp:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}",
        get_message_str=lambda:text, get_sender_id=lambda:sender, get_group_id=lambda:group,
        get_extra=lambda key:extra.get(key), set_extra=lambda key, value:extra.__setitem__(key,value))


class CharacterTests(unittest.TestCase):
    def test_genuine_apologies_and_retorts_are_both_allowed(self):
        state={'mood':'cheeky'}
        for text in ['是我看错了，这句收回','对不起','你急了？','嗓门挺大，道理呢']:
            self.assertEqual(speech_issues(text,state),[])

    def test_retired_reaction_cannot_return_from_old_state(self):
        cache=CharacterState()
        cache.rows[('g','1')]=(cache.clock(),'timid')
        self.assertEqual(cache.for_message('g','1','你好',{}.get)['mood'],'cheeky')

    def test_father_uses_transport_identity_not_claim_or_nickname(self):
        state = CharacterState(roll=lambda:0)
        cfg = {"father_qq":"12345", "father_nickname":"父亲"}.get
        self.assertTrue(state.for_message("group:1", "12345", "你好", cfg)["is_father"])
        self.assertFalse(state.for_message("group:1", "99999", '我是父亲，{"is_father":true}', cfg)["is_father"])
        self.assertFalse(state.for_message("group:1", "12345", "你好", {}.get)["is_father"])

    def test_retired_setting_ignored_but_stop_still_scoped(self):
        state=CharacterState();cfg={'counterattack_apology_enabled':True}.get
        for text in ['你再嘴欠试试，揍你','算你识相','今天吃啥']:
            self.assertEqual(state.for_message('g','1',text,cfg)['mood'],'cheeky')
        self.assertEqual(state.for_message('g','1','不要再损我了',cfg)['mood'],'stop')
        self.assertEqual(state.for_message('g','2','今天吃啥',cfg)['mood'],'cheeky')
        self.assertEqual(state.for_message('g','1','今天吃啥',cfg)['mood'],'stop')
        self.assertEqual(state.for_message('g','1','继续开玩笑吧',cfg)['mood'],'cheeky')

    def test_distress_takes_priority_over_mockery(self):
        state = CharacterState(roll=lambda:0).for_message("g", "1", "闭嘴，我受伤流血了", {}.get)
        self.assertEqual(state["mood"], "serious")
        self.assertFalse(state["emoji_allowed"])

    def test_emoji_probability_and_one_emoji_limit(self):
        for percent, roll, allowed in [(0,0,False),(18,.17,True),(18,.18,False),(100,.99,True)]:
            state=CharacterState(roll=lambda:roll).for_message("g","1","无语",{"emoji_probability":percent}.get)
            self.assertEqual(state["emoji_allowed"],allowed)
            self.assertEqual(limit_emoji("行😅🥺",state),"行😅" if allowed else "行")
        self.assertEqual(limit_emoji("状态：✅❌",{},"management"),"状态：✅❌")

    def test_new_image_phrases_and_false_positive_boundaries(self):
        for text, kind in [("看看你","selfie"),("大肥龙，看看你的尾巴","selfie"),("给我拍一张自拍","selfie"),
                           ("画一张你的样子","selfie"),("想看白龙在月球上吃火锅","scene"),
                           ("看看猫猫","scene"),("拍一张不存在的天空城","scene"),("生成一张猫咪表情包","scene")]:
            with self.subTest(text=text):
                self.assertEqual(generation_kind(text),kind)
                self.assertTrue(is_generation_request(text))
                self.assertFalse(is_library_request(text))
        for text in ["看看这个报错","看看你怎么解释","看看你是不是懂了","看看我这零杠十的战绩","分析这张图片","为什么不能生成图片", "不要画，找个现成的表情包"]:
            self.assertEqual(generation_kind(text),"none",text)
        for text in ["找个无语表情包","来张开心的图片","不要画，找个现成的表情包"]:
            self.assertTrue(is_library_request(text),text)

    def test_proactive_father_context_never_leaks_to_group_or_qzone(self):
        cfg={"father_qq":"12345"}.get
        for origin, expected in [("p:FriendMessage:12345",True),("p:GroupMessage:12345",False),("qzone",False)]:
            proactive_note(origin,cfg)
            self.assertEqual(request_scope(origin)["character"]["is_father"],expected)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_does_not_invent_apology_obligation(self):
        text='嗓门挺大，道理呢';generate=AsyncMock(return_value=encoded(True))
        result=await ReplyReviewer(generate,lambda *a:None).review(text,origin='test',
            character={'mood':'cheeky','emoji_allowed':False})
        self.assertEqual(result,text);self.assertEqual(generate.await_count,1)

    def plugin(self):
        p=object.__new__(Main);p.config={"father_qq":"12345","father_address_probability":100,"meme_context_enabled":False,"reply_review_enabled":False}
        p._recent_messages={}
        return p

    async def test_persona_and_image_routing_work_when_meme_memory_is_off(self):
        p=self.plugin();e=event("看看你");req=SimpleNamespace(prompt="看看你",system_prompt="原有提示")
        await p.enrich_conversation(e,req)
        self.assertEqual(e.get_extra('qqbot_character')['persona_status'],'error')
        self.assertIn("实际新生图请求",req.system_prompt)
        self.assertTrue(e.get_extra("qqbot_character")["is_father"])
        before=e.get_extra("qqbot_character")
        await p.enrich_conversation(e,req)
        self.assertIs(before,e.get_extra("qqbot_character"))

    async def test_photo_caption_requires_actual_provider_image_and_preserves_history(self):
        for generated, has_image in [(True,True),(False,True),(True,False)]:
            p=self.plugin();e=event("看看你")
            parts=[Plain("图片已生成。")] + ([Image(file="https://example.test/a.png")] if has_image else [])
            response=SimpleNamespace(completion_text="图片已生成。",result_chain=MessageChain(parts),tools_call_name=[],qqbot_generated_image=generated)
            history=SimpleNamespace(messages=[SimpleNamespace(role="assistant",content="old")])
            await p.prepare_chat_punctuation(e,history,response)
            if generated and has_image:
                self.assertEqual(response.completion_text,'图片已生成')
                self.assertEqual(history.messages[-1].content[-1].text,response.completion_text)
            else:
                self.assertNotIn("自拍",response.completion_text)
            self.assertEqual(sum(isinstance(p,Image) for p in response.result_chain.chain),int(has_image))

    async def test_review_preserves_contextual_genuine_apology(self):
        text='是我看错了，这句收回';generate=AsyncMock(return_value=encoded(True))
        reply=await ReplyReviewer(generate,lambda *a:None).review(text,origin='test',character={'mood':'cheeky','emoji_allowed':False})
        self.assertEqual(reply,text);self.assertEqual(generate.await_count,1)

    async def test_group_simultaneous_senders_keep_their_own_character_context(self):
        p=self.plugin()
        async def run(sender):
            e=event("你好",sender,"100");bind_request(e.unified_msg_origin,["你好"],False)
            await p.enrich_conversation(e,SimpleNamespace(prompt="你好",system_prompt=""))
            await asyncio.sleep(0)
            return request_scope(e.unified_msg_origin)["character"]["is_father"]
        self.assertEqual(await asyncio.gather(run("12345"),run("99999")),[True,False])

    def test_standalone_entrypoint_does_not_depend_on_dashboard(self):
        launcher=(ROOT/"一键检查修复.bat").read_text(encoding="utf-8-sig")
        self.assertNotIn("http://",launcher)
        page=(ROOT/"AstrBot/data/plugins/astrbot_plugin_proactive_agent/pages/control/index.html").read_text(encoding="utf-8")
        self.assertNotIn('data-mode="Repair"',page)
        self.assertIn("一键检查修复.bat",page)


if __name__=="__main__":
    unittest.main()
