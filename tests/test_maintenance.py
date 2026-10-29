import asyncio
import base64
import importlib
import json
from pathlib import Path
import sys
import time
import unittest
import os
import tempfile
import atexit
from temp_cleanup import install as install_test_temp_cleanup
install_test_temp_cleanup()
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "AstrBot"))
# AstrBot initializes data paths on import. Keep unit-test artifacts off live data.
_working_directory = Path.cwd()
_sandbox = tempfile.TemporaryDirectory(prefix="qqbot-tests-")
os.chdir(_sandbox.name)
def _cleanup():
    os.chdir(_working_directory)
    _sandbox.cleanup()
atexit.register(_cleanup)
from data.plugins.astrbot_plugin_proactive_agent.image_compat import install_image_compat
from data.plugins.astrbot_plugin_proactive_agent.proactive_policy import decide
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.image_intent import is_library_request
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.tagging import TaggingMixin
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.api.message_components import Image
from openai.types.chat import ChatCompletion
from unittest.mock import AsyncMock
from data.plugins.astrbot_plugin_proactive_agent.main import Main
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.caption_library import CaptionLibraryMixin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval import RetrievalMixin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.common import CaptionGenerationError, WakeImageRequestFilter


def completion(id, text=None, image=None, refusal=None, reason="stop"):
    msg = {"role": "assistant", "content": text}
    if image:
        msg["images"] = [{"image_url": {"url": image}}]
    if refusal:
        msg["refusal"] = refusal
    return ChatCompletion.model_validate({"id": id, "model": "test", "created": 1,
        "object": "chat.completion", "choices": [{"index": 0, "finish_reason": reason, "message": msg}]})


class Images(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.restore = install_image_compat()
        self.provider = object.__new__(ProviderOpenAIOfficial)
        self.provider.reasoning_key = "reasoning_content"

    async def asyncTearDown(self):
        self.restore()

    async def test_concurrent_images_stay_on_their_response(self):
        a = "data:image/png;base64," + base64.b64encode(b"image-A").decode()
        b = "data:image/png;base64," + base64.b64encode(b"image-B").decode()
        ra, rb = await asyncio.gather(*[self.provider._parse_openai_completion(completion(id, image=ref), None) for id, ref in [("A", a), ("B", b)]])
        self.assertEqual([c.file for c in ra.result_chain.chain if isinstance(c, Image)], [a])
        self.assertEqual([c.file for c in rb.result_chain.chain if isinstance(c, Image)], [b])
        self.assertEqual(ra.id, "A")

    async def test_text_and_image_both_preserved(self):
        ref = "data:image/png;base64," + base64.b64encode(b"image").decode()
        result = await self.provider._parse_openai_completion(completion("C", "这是你的图", ref), None)
        self.assertEqual(result.completion_text, "这是你的图")
        self.assertTrue(any(isinstance(c, Image) for c in result.result_chain.chain))

    async def test_real_empty_error_is_not_swallowed(self):
        with self.assertRaises(EmptyModelOutputError):
            await self.provider._parse_openai_completion(completion("empty"), None)

    async def test_content_filter_and_refusal_remain_effective(self):
        ref = "https://example.invalid/image.png"
        with self.assertRaises(Exception):
            await self.provider._parse_openai_completion(completion("blocked", image=ref, reason="content_filter"), None)
        result = await self.provider._parse_openai_completion(completion("refused", image=ref, refusal="不能提供"), None)
        self.assertFalse(any(isinstance(c, Image) for c in result.result_chain.chain))


class Intent(unittest.TestCase):
    def test_actual_wake_filter_does_not_steal_generation(self):
        # This is the filter the framework invokes before activating the handler.
        event=SimpleNamespace(get_message_str=lambda:'帮我生成一张表情包')
        self.assertFalse(WakeImageRequestFilter().filter(event, {}))

    def test_generation_and_questions_bypass_library(self):
        for text in ["帮我生成一张猫咪图片", "画只猫", "/生成一张表情包", "给我画一只龙娘", "这张图片是什么意思", "为什么不能生成图片", "不要给我旧表情，生成一张新的", "draw a cat picture", "给这张图片换个背景", "帮我做一张猫猫图片", "把这张照片改成水彩风格", "帮我修图"]:
            with self.subTest(text=text): self.assertFalse(is_library_request(text))

    def test_existing_image_requests_still_work(self):
        for text in ["找个无语表情包", "来张开心的图片", "给我一个猫猫表情包", "不要画，找个现成的表情包", "表情包"]:
            with self.subTest(text=text): self.assertTrue(is_library_request(text))

    def test_chat_does_not_trigger_search(self):
        for text in ["这个表情包笑死我了", "绷不住了", "图片质量真好", "这张照片很好看"]:
            with self.subTest(text=text): self.assertFalse(is_library_request(text))


class Tags(unittest.TestCase):
    def test_provider_failure_backoff_does_not_restart_job_immediately(self):
        progress=[]
        plugin=SimpleNamespace(_caption_retry_after=time.monotonic()+60,
            _set_caption_progress=lambda **kwargs:progress.append(kwargs),_pending_caption_rel_paths=lambda:['old.png'])
        CaptionLibraryMixin._start_caption_background_task(plugin)
        self.assertEqual(progress[0]['status'],'failed')
        self.assertEqual(progress[0]['remaining'],1)

    def test_recaption_failure_keeps_old_image_searchable(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old.png';path.write_bytes(b'placeholder')
            item={'rel_path':'old.png','caption_status':'failed','captioned_at':123,'auto_tags':['旧标签']}
            plugin=SimpleNamespace(_index={'images':{'old':item}},_hidden_rel_paths=lambda:set(),_norm_rel_path=lambda x:x,
                _library_source_for_rel_path=lambda *a:'manual',_abs_plugin_data_path=lambda x:path,
                _image_id=lambda x:x,_tags_from_item=lambda x:x['auto_tags'],_normalize_tags=lambda x:x,_valid_global_tags=lambda x:x)
            self.assertEqual(len(RetrievalMixin._library_candidates(plugin)),1)
            item['captioned_at']=0
            self.assertEqual(RetrievalMixin._library_candidates(plugin),[])

    def test_recaption_keeps_manual_edits_and_previous_tags(self):
        item={'auto_tags':['旧AI标签'],'manual_tags':['我的标签'],'tags':['我的标签'],
              'manual_tags_override':True,'captioned_at':123,'caption_status':'done'}
        plugin=SimpleNamespace(_index={'images':{'one':item}},_valid_global_tags=lambda x:x)
        CaptionLibraryMixin._reset_all_caption_tags_for_new_categories(plugin)
        self.assertEqual(item['manual_tags'],['我的标签'])
        self.assertTrue(item['manual_tags_override'])
        self.assertEqual(item['auto_tags'],['旧AI标签'])
        self.assertEqual(item['captioned_at'],123)
        self.assertEqual(item['caption_status'],'pending')

    def test_rich_tags_survive_normalization(self):
        class Tagger(TaggingMixin):
            def _caption_category_enabled(self, key): return True
        tags = ["表情包", "二次元"] + [f"使用场景{i}" for i in range(22)]
        result = Tagger()._normalize_caption_tags(tags, "1234-5678-1.jpg")
        self.assertEqual(len(result), 24)
        self.assertIn("使用场景21", result)
        self.assertLessEqual(len(Tagger()._normalize_caption_tags(tags * 3, "x.jpg")), 32)


class CaptionFailure(unittest.IsolatedAsyncioTestCase):
    async def test_provider_prose_is_not_saved_as_tags(self):
        class Captioner(RetrievalMixin):
            def _default_image_caption_provider_id(self): return 'test'
            def _caption_category_labels(self): return []
            def _caption_category_prompt_rules(self): return ''
            def _loads_json_object(self,text): return None
            async def _prepare_caption_image_input(self,path): return str(path),[]
            async def _run_image_caption_provider_request(self,call): return await call()
            async def _llm_generate_with_provider_fallback(self,**kwargs): return SimpleNamespace(completion_text='无法加载这张图片')
        with self.assertRaises(CaptionGenerationError):
            await Captioner()._caption_image(Path('unused.png'))

    async def test_missing_provider_is_not_successful_filename_tags(self):
        class Captioner(RetrievalMixin):
            def _default_image_caption_provider_id(self): return ''
            def _caption_category_labels(self): return []
            def _caption_category_prompt_rules(self): return ''
            async def _prepare_caption_image_input(self,path): return str(path),[]
            async def _run_image_caption_provider_request(self,call): return await call()
            async def _llm_generate_with_provider_fallback(self,**kwargs): return None
        with self.assertRaises(CaptionGenerationError):
            await Captioner()._caption_image(Path('unused.png'))


class Permissions(unittest.TestCase):
    def test_all_management_commands_require_real_admin(self):
        from astrbot.core.star.star_handler import star_handlers_registry
        from astrbot.core.star.filter.permission import PermissionTypeFilter, PermissionType
        handlers=[h for h in star_handlers_registry._handlers if 'astrbot_plugin_proactive_agent.main_' in h.handler_full_name and h.handler_name.startswith('cmd_')]
        self.assertGreaterEqual(len(handlers),10)
        for handler in handlers:
            filters=[f for f in handler.event_filters if isinstance(f,PermissionTypeFilter)]
            with self.subTest(command=handler.handler_name):
                self.assertTrue(filters)
                self.assertEqual(filters[0].permission_type,PermissionType.ADMIN)
                self.assertFalse(filters[0].filter(SimpleNamespace(is_admin=lambda:False),{}))
                self.assertTrue(filters[0].filter(SimpleNamespace(is_admin=lambda:True),{}))


class ConversationContext(unittest.IsolatedAsyncioTestCase):
    def event(self, text="绷不住了"):
        extra={}
        return SimpleNamespace(unified_msg_origin='group:1',get_sender_id=lambda:'2',get_message_str=lambda:text,
            get_extra=lambda k:extra.get(k),set_extra=lambda k,v:extra.__setitem__(k,v))
    async def test_context_stays_in_its_chat_and_expires(self):
        plugin=object.__new__(Main)
        plugin.config={}
        plugin._meme_hints=SimpleNamespace(match=lambda text:[])
        plugin._recent_messages={'group:1':[{'time':time.time(),'text':'群里正在聊游戏'}, {'time':time.time()-9000,'text':'过期消息'}],
                                 'private:2':[{'time':time.time(),'text':'私聊秘密'}]}
        req=SimpleNamespace(prompt='绷不住了',system_prompt='原有角色设定')
        await plugin.enrich_conversation(self.event(),req)
        self.assertTrue(req.system_prompt.startswith('原有角色设定'))
        self.assertIn('群里正在聊游戏',req.system_prompt)
        self.assertNotIn('私聊秘密',req.system_prompt)
        self.assertNotIn('过期消息',req.system_prompt)

    async def test_disabling_meme_context_keeps_persona_and_character(self):
        plugin=object.__new__(Main);plugin.config={'meme_context_enabled':False}
        req=SimpleNamespace(prompt='你好',system_prompt='原有角色设定')
        event=self.event('你好')
        await plugin.enrich_conversation(event,req)
        self.assertTrue(req.system_prompt.startswith('原有角色设定'))
        self.assertIn('原有角色设定',req.system_prompt)
        self.assertEqual(event.get_extra('qqbot_character')['persona_status'],'error')
        self.assertNotIn('图库中相关梗的候选标签',req.system_prompt)


class FakePlugin:
    def __init__(self, action="group", bad_target=False):
        self._busy = False
        self._recent_messages = {"aiocqhttp:GroupMessage:10": [{"time": time.time(), "text": "群话题"}], "aiocqhttp:FriendMessage:20": [{"time": time.time(), "text": "私聊秘密"}]}
        self.state = {"proactive_state": {"last_lane": {"group": "qzone", "private": "group", "qzone": "private"}[action]}}
        self.sent = []
        self.payloads = []
        self.settings = {"enable": True, "allow_group": True, "allow_private": True, "allow_qzone": True}
        self.context = SimpleNamespace(get_config=lambda **kw: {}, persona_manager=SimpleNamespace(get_persona_v3_by_id=lambda _: None), get_using_provider_async=AsyncMock(return_value=self), send_message=self.send)
        self.bad_target = bad_target
        self.logger = SimpleNamespace(info=lambda *args: None)
    def _cfg(self,k,default=None): return self.settings.get(k,default)
    def _quiet_now(self): return False
    def _id_set(self,s): return set(str(s).split(',')) if s else set()
    async def get_kv_data(self,k,default=None): return self.state.get(k,default)
    async def put_kv_data(self,k,v): self.state[k]=v
    async def _collect_targets(self): return [('10','test-group')],[('20','test-friend')]
    async def _qzone_allowed(self): return True
    async def _history(self): return []
    def _clean(self,s): return s[:500]
    def _parse_decision(self,s): return json.loads(s)
    def _build_chain(self,text,images): return text
    async def _record(self,text): pass
    async def send(self,umo,chain): self.sent.append((umo,chain));return True
    async def _post_qzone(self,text,images): self.sent.append(('qzone',text))
    async def text_chat(self, prompt, system_prompt):
        data=json.loads(prompt);self.payloads.append(data)
        return SimpleNamespace(completion_text=json.dumps({'action':data['允许渠道'],'target_id':'999' if self.bad_target else data['目标ID'],'content':'今晚聊个游戏话题','reason':'自然互动'}))


class Proactive(unittest.IsolatedAsyncioTestCase):
    async def test_public_topic_direction_rotates_without_sharing_chat_text(self):
        p=FakePlugin();await decide(p,'test')
        first=p.payloads[-1]['本轮话题方向']
        p.state['proactive_state']['last_send']=0
        await decide(p,'test',dry_run=True)
        self.assertNotEqual(first,p.payloads[-1]['本轮话题方向'])

    async def test_dry_run_never_sends_or_changes_state(self):
        p=FakePlugin();before=json.dumps(p.state)
        out=await decide(p,'test',dry_run=True)
        self.assertTrue(json.loads(out)['dry_run']);self.assertEqual(p.sent,[])
        self.assertEqual(json.dumps(p.state),before)

    async def test_invalid_target_rejected(self):
        p=FakePlugin(bad_target=True);await decide(p,'test');self.assertEqual(p.sent,[])

    async def test_qzone_never_receives_private_context(self):
        p=FakePlugin('qzone');await decide(p,'test',dry_run=True)
        self.assertNotIn('私聊秘密',json.dumps(p.payloads,ensure_ascii=False))
        self.assertEqual(p.payloads[0]['同一会话近期消息'],[])

    async def test_cooldown_prevents_repeat(self):
        p=FakePlugin();await decide(p,'test');await decide(p,'test')
        self.assertEqual(len(p.sent),1)

    async def test_disabled_channel_never_sends(self):
        p=FakePlugin();p.settings.update(allow_group=False,allow_private=False,allow_qzone=False)
        await decide(p,'test');self.assertEqual(p.sent,[])

    async def test_group_context_excludes_private_content(self):
        p=FakePlugin();await decide(p,'test',dry_run=True)
        self.assertEqual([x['文字'] for x in p.payloads[0]['同一会话近期消息']], ['群话题'])
        p.context.get_using_provider_async.assert_awaited_once_with(umo='aiocqhttp:GroupMessage:10')
        self.assertNotIn('私聊秘密',json.dumps(p.payloads,ensure_ascii=False))


if __name__ == '__main__': unittest.main()
