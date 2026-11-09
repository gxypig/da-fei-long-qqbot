"""Synthetic reproductions of quote-boundary loss and image-turn misattribution."""
import asyncio,json,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_maintenance import Main
import test_round8_visual as visual_tests
import test_native_wire_admission as native_tests
Event=visual_tests.Event
from astrbot.api.message_components import Image
from data.plugins.astrbot_plugin_proactive_agent.send_checks import normalize,own_style
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import (
    ReplyReviewer,OutboundGuard,bind_request,request_scope,approve_once,consume_approval)
from data.plugins.astrbot_plugin_proactive_agent.review_input import bind_current,attach_visual,evidence

PASS={'pass':True,'natural':True,'chat_fit':True}

class Formatting(unittest.TestCase):
    def test_quoted_sentence_boundaries_survive_repeated_passes(self):
        for text in ['视频的标题是符号\n“白屏”是评论的说法',
                     '前一句\n「原话」\n后一句', '前一句 “原话” 后一句',
                     '前一句\r\n“原话”\r\n后一句', '“保留。\n原文。”\n再聊',
                     '前一句\n"原话"\n后一句', '前一句\n『原话』\n后一句',
                     '前一句\n```python\nprint(1)\n```\n后一句']:
            with self.subTest(text=text):
                self.assertEqual(normalize(text),text)
                self.assertEqual(normalize(normalize(text)),text)
                self.assertEqual(own_style(text),text)
    def test_sentence_period_removed_without_gluing_quote(self):
        self.assertEqual(normalize('前一句。\n“原文。”\n后一句。'),'前一句\n“原文。”\n后一句')
    def test_management_and_verbatim_remain_byte_exact(self):
        text='  第一行。\r\n“第二行。”  '
        self.assertEqual(normalize(text,mode='management'),text)
        self.assertEqual(normalize('draft',{'verbatim_text':text}),text)
    def test_urls_decimals_filenames_and_final_newline(self):
        text='数值 3.14\n“原话”\nhttps://example.org/a.png'
        self.assertEqual(normalize(text),text)

class CurrentTurn(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        p=object.__new__(Main);p._recent_messages={};p.config={'reply_review_enabled':True,'review_policy':'all'}
        return p
    def event(self,text='',sender='one',origin='bot:FriendMessage:one',mid='img',images=True):
        e=Event(text,sender=sender,origin=origin,mid=mid,images=[Image.fromFileSystem('/synthetic/image.png')] if images else [])
        e.get_group_id=lambda:'' if ':FriendMessage:' in origin else 'g'
        return e
    async def test_image_only_is_bound_to_current_turn_and_not_old_question(self):
        p=self.plugin();old=self.event('你故意的啊',mid='old',images=False)
        await p.track_session(old);e=self.event();await p.track_session(e)
        scope=request_scope(e.unified_msg_origin)
        self.assertEqual(scope['incoming']['message_id'],'img')
        self.assertEqual(scope['incoming']['text'],'')
        self.assertEqual(scope['incoming']['attachments'],['Image'])
        self.assertIn('仅含附件',scope['recent'][-1]);self.assertFalse(scope['management'])
    async def test_current_image_reused_in_same_review_call(self):
        p=self.plugin();e=self.event();await p.track_session(e)
        attach_visual(e,['/synthetic/current.png'],[])
        generate=AsyncMock(return_value=json.dumps(PASS))
        r=ReplyReviewer(generate,lambda *a:None)
        await r.review('你这表情可不像服气',origin=e.unified_msg_origin,recent=['旧问题'],prior_replies=['这局我赢了'])
        args=generate.await_args
        self.assertEqual(args.kwargs['image_urls'],['/synthetic/current.png'])
        self.assertEqual(args.args[1]['当前收到的消息']['text'],'')
        self.assertEqual(args.args[1]['附件数'],0)
        self.assertIn('历史末条文字',args.args[0]);generate.assert_awaited_once()
    async def test_new_text_turn_does_not_inherit_image_or_old_approval(self):
        p=self.plugin();e=self.event();await p.track_session(e);attach_visual(e,['/synthetic/current.png'],[])
        approve_once(e.unified_msg_origin,'回应')
        new=self.event('换个话题',mid='next',images=False);await p.track_session(new)
        self.assertFalse(consume_approval(e.unified_msg_origin,'回应'))
        current,images=evidence(e.unified_msg_origin,'chat');self.assertEqual(images,[])
        self.assertEqual(current['text'],'换个话题')
    async def test_same_scope_receipt_skips_second_review_even_with_image(self):
        p=self.plugin();e=self.event();await p.track_session(e)
        state={'persona_status':'selected'};request_scope(e.unified_msg_origin)['character']=state
        attach_visual(e,['/synthetic/current.png'],[])
        p.review_policy_version=lambda:'v';p._personas=object()
        generate=AsyncMock(return_value=json.dumps(PASS));reviewer=ReplyReviewer(generate,lambda *a:None)
        text=await reviewer.review('你这表情可不像服气',origin=e.unified_msg_origin,character=state)
        approve_once(e.unified_msg_origin,text,policy_version='v')
        p._cfg=lambda k,d:'bot' if k=='platform_id' else d
        payload=await OutboundGuard(p,reviewer).prepare('send_private_msg',{'user_id':'one','message':text})
        generate.assert_awaited_once();self.assertEqual(payload['message'][0]['data']['text'],text)
    async def test_edit_after_approval_still_requires_review(self):
        p=self.plugin();e=self.event();await p.track_session(e);approve_once(e.unified_msg_origin,'原回复')
        self.assertFalse(consume_approval(e.unified_msg_origin,'被插件改写'))
        approve_once(e.unified_msg_origin,'原回复');attach_visual(e,['changed.png'],[])
        self.assertFalse(consume_approval(e.unified_msg_origin,'原回复'))
    async def test_same_origin_concurrent_senders_keep_distinct_images(self):
        async def run(sender):
            p=self.plugin();e=self.event(sender=sender,origin='bot:GroupMessage:g',mid=sender)
            await p.track_session(e);attach_visual(e,[sender+'.png'],[]);await asyncio.sleep(0)
            data,images=evidence(e.unified_msg_origin,'chat')
            return data['sender'],images
        self.assertEqual(await asyncio.gather(run('a'),run('b')),[('a',['a.png']),('b',['b.png'])])
    async def test_missing_input_image_is_explicit_not_substituted_by_history(self):
        p=self.plugin();e=self.event();await p.track_session(e);attach_visual(e,[],[])
        g=AsyncMock(return_value=json.dumps(PASS));r=ReplyReviewer(g,lambda *a:None)
        await r.review('没看清这张',origin=e.unified_msg_origin,recent=['旧问题'])
        self.assertEqual(g.await_args.kwargs,{})
        self.assertEqual(g.await_args.args[1]['当前收到的消息']['available_images'],0)
    async def test_qzone_never_receives_private_image_context(self):
        p=self.plugin();e=self.event();await p.track_session(e);attach_visual(e,['private.png'],[])
        self.assertEqual(evidence(e.unified_msg_origin,'qzone'),({},[]))
    async def test_normal_text_review_has_no_images(self):
        p=self.plugin();e=self.event('今晚好冷',images=False);await p.track_session(e)
        g=AsyncMock(return_value=json.dumps(PASS));await ReplyReviewer(g,lambda *a:None).review('穿件外套',origin=e.unified_msg_origin)
        self.assertEqual(g.await_args.kwargs,{})
    async def test_certified_multiline_quote_rewrite_returns_in_one_call(self):
        bind_request('incident',[],False)
        fixed='标题是一串符号\n“白屏”是评论里的说法'
        verdict={**PASS,'pass':False,'natural':False,'reason':'缺少断句','replacement':fixed,
                 'replacement_check':{**PASS,'facts_preserved':True}}
        g=AsyncMock(return_value=json.dumps(verdict,ensure_ascii=False))
        self.assertEqual(await ReplyReviewer(g,lambda *a:None).review('标题是一串符号“白屏”是评论里的说法',origin='incident'),fixed)
        g.assert_awaited_once()

class VisualReview(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=visual_tests.Visual.asyncSetUp
    image=visual_tests.Visual.image
    async def test_prepared_originals_are_shared_without_new_download(self):
        event=Event('看这张',images=[self.image()]);bind_request(event.unified_msg_origin,[],False);bind_current(event)
        await self.service.enrich(event,SimpleNamespace(image_urls=[],extra_user_content_parts=[],system_prompt=''))
        data,images=evidence(event.unified_msg_origin,'chat')
        self.assertEqual([Path(p).resolve() for p in images],[self.path.resolve()]);self.assertEqual(data['available_images'],1)

class NativeReviewImage(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=native_tests.NativeWireTests.asyncSetUp
    asyncTearDown=native_tests.NativeWireTests.asyncTearDown
    provider=native_tests.NativeWireTests.provider
    answer=native_tests.NativeWireTests.answer
    async def test_image_reaches_same_native_review_request_and_model(self):
        from test_reply_pipeline import plugin
        payloads=[]
        async def handler(request):
            payloads.append(json.loads(request.content));return self.answer(json.dumps(PASS))
        p=plugin();p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=self.provider(handler)))
        p.config['review_low_latency']=False
        image='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII='
        await p._generate_json('当前消息审阅',{'当前收到的消息':{'attachments':['Image']}},'offline',image_urls=[image])
        self.assertEqual(len(payloads),1);self.assertEqual(payloads[0]['model'],'same-offline-model')
        parts=[p for message in payloads[0]['messages'] for p in message.get('content',[]) if isinstance(p,dict)]
        self.assertEqual(len([p for p in parts if p.get('type')=='image_url']),1)
