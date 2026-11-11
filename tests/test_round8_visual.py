import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_maintenance import ROOT
from PIL import Image as PILImage
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.provider.entities import ProviderRequest
from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual, detail_views


class Event:
    def __init__(self, text='', images=(), sender='one', origin='bot:GroupMessage:g', mid='1', extra=()):
        self.unified_msg_origin=origin;self.sender=sender;self.text=text;self.extras={}
        self.message_obj=SimpleNamespace(message_id=mid, message=[Plain(text),*images,*extra])
    def get_sender_id(self):return self.sender
    def get_self_id(self):return 'bot'
    def get_message_str(self):return self.text
    def get_messages(self):return self.message_obj.message
    def get_extra(self,key):return self.extras.get(key)
    def set_extra(self,key,value):self.extras[key]=value


class Visual(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'image.png'
        with PILImage.new('RGB',(400,300),'white') as image:image.save(self.path)
        self.now=1.
        self.cfg={'incoming_visual_burst_ms':0}
        self.plugin=SimpleNamespace(_cfg=lambda k,d=None:self.cfg.get(k,d),context=SimpleNamespace(),_audit=Mock())
        self.service=IncomingVisual(self.plugin,clock=lambda:self.now)
    def image(self):return Image.fromFileSystem(str(self.path))
    async def enrich(self,event):
        req=ProviderRequest(prompt=event.text,system_prompt='',image_urls=[])
        await self.service.enrich(event,req)
        return req

    async def test_image_then_text_includes_original_and_provenance(self):
        image=self.image();first=Event(images=[image]);self.service.observe(first)
        self.now+=2
        req=await self.enrich(Event('这张图什么意思',mid='2'))
        self.assertEqual(len(req.image_urls),1)
        self.assertEqual(Path(req.image_urls[0]).read_bytes(),self.path.read_bytes())
        notes='\n'.join(getattr(p,'text','') for p in req.extra_user_content_parts)
        self.assertIn('同一发送者相邻图文',notes);self.assertIn('"发送者": "one"',notes)

    async def test_text_then_image_uses_question_not_image_in_isolation(self):
        self.service.observe(Event('帮我看看这图怎么了'))
        self.now+=2
        req=await self.enrich(Event(images=[self.image()],mid='2'))
        self.assertTrue(any('帮我看看这图怎么了' in getattr(p,'text','') for p in req.extra_user_content_parts))

    async def test_senders_sessions_expiry_and_topic_boundaries(self):
        self.service.observe(Event(images=[self.image()]))
        for sender,origin in [('two','bot:FriendMessage:two'),('one','bot:GroupMessage:other')]:
            req=await self.enrich(Event('这图什么意思',sender=sender,origin=origin,mid='2'))
            self.assertEqual(req.image_urls,[])
        self.now+=3601
        self.assertEqual((await self.enrich(Event('这图什么意思',mid='3'))).image_urls,[])
        self.service.observe(Event(images=[self.image()],mid='4'))
        self.service.observe(Event('换个话题',mid='5'))
        self.assertEqual((await self.enrich(Event('这图什么意思',mid='6'))).image_urls,[])

    async def test_explicit_quote_does_not_pull_unrelated_own_image(self):
        self.service.observe(Event(images=[self.image()]))
        req=await self.enrich(Event('这图什么意思',mid='2',extra=[Reply(id='another',sender_id='two',chain=[self.image()])]))
        self.assertEqual(req.image_urls,[])
        notes='\n'.join(getattr(p,'text','') for p in req.extra_user_content_parts)
        self.assertIn('"发送者": "two"',notes)

    async def test_plain_text_never_waits_downloads_or_calls_model(self):
        self.cfg['incoming_visual_burst_ms']=1500
        with patch('data.plugins.astrbot_plugin_proactive_agent.incoming_visual.asyncio.sleep',side_effect=AssertionError('ordinary text must not wait')):
            req=await self.enrich(Event('今天过得不错'))
        self.assertEqual(req.extra_user_content_parts,[])

    async def test_existing_native_original_is_reused_without_download(self):
        image=self.image();event=Event(images=[image]);self.service.observe(event)
        self.service.captured(event,image,str(self.path))
        with patch.object(Image,'convert_to_file_path',side_effect=AssertionError('must reuse original')):
            await self.enrich(event)
        self.assertEqual(self.service.stats['unavailable'],0)

    async def test_long_screenshot_tiles_are_bounded_temporary_and_do_not_change_original(self):
        with PILImage.new('RGB',(900,4600),'white') as image:image.save(self.path)
        original=self.path.read_bytes()
        req=await self.enrich(Event('读一下这张截图',images=[self.image()]))
        parts=[p for p in req.extra_user_content_parts if p.type=='image_url']
        self.assertEqual(len(parts),4)
        self.assertTrue(all(p._no_save for p in parts))
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(self.service.stats['unavailable'],0)

    async def test_reset_epoch_drops_old_image_context(self):
        self.service.epoch=lambda event:0
        self.service.observe(Event(images=[self.image()]))
        self.service.epoch=lambda event:1
        self.assertEqual((await self.enrich(Event('这图什么意思',mid='2'))).image_urls,[])

    async def test_partial_long_image_reports_incomplete_coverage(self):
        with PILImage.new('RGB',(200,12000),'white') as image:image.save(self.path)
        crops,info=await asyncio.to_thread(detail_views,self.path,'截图',3)
        self.assertEqual(len(crops),3);self.assertFalse(info['complete'])
        self.assertEqual(crops[0]['box'][1],0)
        self.assertEqual(crops[-1]['box'][3],12000)

    async def test_explicit_bottom_question_only_adds_bottom_detail(self):
        with PILImage.new('RGB',(900,4600),'white') as image:image.save(self.path)
        crops,info=await asyncio.to_thread(detail_views,self.path,'最底部的核对码写了什么',4)
        self.assertEqual(len(crops),1)
        self.assertEqual(crops[0]['box'][3],4600)
        self.assertTrue(info['按明确方位选取'])

    async def test_one_image_continuation_requires_previous_admitted_wake(self):
        first=Event('帮我看看这张图');first.is_at_or_wake_command=True
        self.service.observe(first)
        self.service.admitted(first)
        self.now+=3
        image_event=Event(images=[self.image()],mid='2')
        self.assertEqual(self.service.followup_wake(image_event),'image_continuation')
        self.assertIsNone(self.service.followup_wake(Event(images=[self.image()],mid='3')))
        # Neither another sender nor an unadmitted/pure-probability request can
        # open an image continuation in an otherwise wake-only group.
        self.assertIsNone(self.service.followup_wake(Event(images=[self.image()],sender='two',mid='4')))
        self.service.rows.clear()
        first=Event('帮我看看图片');first.is_at_or_wake_command=True
        first.set_extra('qqbot_group_wake','probability')
        self.service.admitted(first)
        self.assertIsNone(self.service.followup_wake(Event(images=[self.image()],mid='5')))
