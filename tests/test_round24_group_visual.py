import asyncio,time,unittest
from unittest.mock import patch
from test_round8_visual import Event
import test_round23_visual as fixture
from astrbot.api.message_components import Reply

class GroupVisual(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.Continuity.asyncSetUp
    image=fixture.Continuity.image
    enrich=fixture.Continuity.enrich
    prepare_cache=fixture.Continuity.prepare_cache
    remember=fixture.Continuity.remember
    async def test_two_speakers_37_seconds_apart_share_image_not_identity(self):
        self.service.observe(Event(images=[self.image()],sender='alice',mid='photo'))
        self.now+=20;self.service.observe(Event('这个东西是我刚做的',sender='alice',mid='explain'))
        self.now+=17
        req=await self.enrich(Event('给你吃',sender='bob',mid='offer'))
        self.assertEqual(len(req.image_urls),1)
        notes=str(req.extra_user_content_parts)
        self.assertIn('alice',notes);self.assertIn('photo',notes);self.assertIn('刚做',notes)
        self.assertIn('同群讨论',notes);self.assertIn('换了说话人',req.system_prompt)
        self.assertEqual(req._qqbot_visual_owner[1],'bob')
    async def test_restart_recovers_group_original_without_reassigning_author(self):
        await self.prepare_cache();_,path=await self.remember(sender='alice');self.service.rows.clear();self.wall+=37
        with patch('data.plugins.astrbot_plugin_proactive_agent.incoming_visual.time.time',return_value=self.wall):
            req=await self.enrich(Event('给你吃',sender='bob',mid='new'))
        self.assertEqual(req.image_urls,[path]);self.assertIn('alice',str(req.extra_user_content_parts))
    async def test_private_images_cannot_be_shared_even_with_none_sender(self):
        await self.prepare_cache();await self.remember(origin='bot:FriendMessage:alice')
        self.assertEqual(await self.cache.recent('bot:FriendMessage:alice',None),[])
        req=await self.enrich(Event('给你吃',sender='bob',origin='bot:GroupMessage:g'))
        self.assertEqual(req.image_urls,[])
    async def test_other_speaker_topic_break_survives_cache_and_restart(self):
        await self.prepare_cache();await self.remember(sender='alice');self.wall+=1
        with patch('data.plugins.astrbot_plugin_proactive_agent.incoming_visual.time.time',return_value=self.wall):self.service.observe(Event('换个话题',sender='bob',mid='break'))
        await asyncio.gather(*self.service.cleanup_tasks);self.service.rows.clear()
        self.assertEqual(await self.cache.recent('bot:GroupMessage:g',None),[])
    async def test_unrelated_statement_does_not_load_recent_group_photos(self):
        await self.prepare_cache();await self.remember(sender='alice');self.service.rows.clear()
        with patch.object(self.cache,'recent',side_effect=AssertionError('unrelated text must not scan')):
            self.assertEqual((await self.enrich(Event('明天上午九点开会',sender='bob'))).image_urls,[])
    async def test_group_topic_break_blocks_same_author_fallback_in_memory(self):
        self.service.observe(Event(images=[self.image()],sender='alice',mid='photo'))
        self.now+=2;self.service.observe(Event('换个话题',sender='bob',mid='break'))
        self.now+=2
        self.assertEqual((await self.enrich(Event('这张图呢',sender='alice',mid='later'))).image_urls,[])
    async def test_explicit_quote_beats_shared_group_image(self):
        self.service.observe(Event(images=[self.image()],sender='alice',mid='photo'))
        req=await self.enrich(Event('这个怎么样',sender='bob',extra=[Reply(id='other',sender_id='carol',chain=[])]))
        self.assertEqual(req.image_urls,[])
    async def test_implicit_reference_expires_without_extending_one_hour(self):
        await self.prepare_cache();await self.remember(sender='alice');self.service.rows.clear();self.wall+=181
        with patch('data.plugins.astrbot_plugin_proactive_agent.incoming_visual.time.time',return_value=self.wall):
            self.assertEqual((await self.enrich(Event('给你吃',sender='bob'))).image_urls,[])
            self.assertEqual(len((await self.enrich(Event('刚才那张图片是什么',sender='bob',mid='explicit'))).image_urls),1)
    async def test_two_images_keep_attribution_and_model_disambiguates(self):
        self.service.observe(Event(images=[self.image()],sender='alice',mid='first'))
        self.now+=10;self.service.observe(Event(images=[self.image()],sender='carol',mid='second'))
        req=await self.enrich(Event('这张图如何',sender='bob',mid='question'))
        notes=str(req.extra_user_content_parts)
        self.assertIn('alice',notes);self.assertIn('carol',notes);self.assertIn('不能确定指哪张',req.system_prompt)
