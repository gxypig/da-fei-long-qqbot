import asyncio,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from astrbot.api.message_components import Reply
from test_round8_visual import Event
import test_round13_images as base
from data.plugins.astrbot_plugin_proactive_agent.image_followup import continuation,content_text,completed_execution
from data.plugins.astrbot_plugin_proactive_agent.image_spec import context_material,normalize_plan
from data.plugins.astrbot_plugin_proactive_agent.image_generation import install,generate
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyJob,current_job

def user(text):return {'role':'user','content':text}
def bot(text):return {'role':'assistant','content':text}
def request(history,images=()):return SimpleNamespace(contexts=history,image_urls=list(images),extra_user_content_parts=[],conversation=SimpleNamespace(cid='test-cid'))
def event(text,**kwargs):return Event(text,origin='p:FriendMessage:test',**kwargs)
def original():return [user('看看你这样的照片'),bot('蓝白裙装、荷叶边和蝴蝶结，拍好了'),user('我要看你拍的视频'),bot('给你看看')]
def pending():return original()+[user('这个视频里你要穿刚才这件衣服'),bot('就穿刚才这套蓝白裙装，荷叶边和蝴蝶结都留着')]

class Dialogue(unittest.TestCase):
    def test_completed_impatient_uses_request_even_with_separate_voice_caption(self):
        row={'state':'confirmed','sender':'one','source_text':'这个视频里你要穿刚才这件衣服','text':'','at':time.time(),'visual':{'intent':'animation'}}
        p=SimpleNamespace(_delivery=SimpleNamespace(recent=lambda *a,**k:[row]))
        self.assertIs(completed_execution(p,event('你倒是拍啊'),request(pending())),row)
        quoted=pending()
        quoted[-2]={'role':'user','content':[{'type':'text','text':row['source_text']},
            {'type':'text','text':'[Image unavailable]'},
            {'type':'text','text':'<Quoted Message>\n(bot): [Image]\n</Quoted Message>'},
            {'type':'text','text':'<system_reminder>time</system_reminder>'},
            {'type':'text','text':'[补充图片不可用；不要猜测其中细节]'}]}
        self.assertIs(completed_execution(p,event('你倒是拍啊'),request(quoted)),row)
        for text in ('好','换成红衣服','再拍一个','拍完了吗'):
            self.assertIsNone(completed_execution(p,event(text),request(pending())))
        self.assertIsNone(completed_execution(p,event('你倒是拍啊'),request(pending()+[user('晚饭吃什么'),bot('面条')])) )
        self.assertIsNone(completed_execution(p,event('你倒是拍啊'),request(pending()+[user('这次穿白的吧'),bot('可以')])) )
        for change in ({'at':time.time()-601},{'state':'unknown'},{'sender':'other'}):
            original=dict(row);row.update(change)
            self.assertIsNone(completed_execution(p,event('你倒是拍啊'),request(pending())))
            row.clear();row.update(original)
    def test_actual_amendment_with_quote_enters_planner(self):
        for text in ('这个视频里你要穿刚才这件衣服','这段视频换成刚才那条蓝裙','还是穿那套蓝白裙装，挥挥手','照片里你要拿橘子','把衣服换成蓝白配色','这张不要苹果，换成橘子','这次穿白的吧','衣服改白色','小鸭换到左手','把镜头拉近一些'):
            self.assertTrue(continuation(event(text,extra=[Reply(id='reference')]),request(original())),text)
    def test_confirmations_and_execution_after_unfulfilled_amendment(self):
        for text in ('好','好就这么着','按你说的来','你倒是拍啊','那就拍吧','快拍','赶紧录','现在开始吧','录一下','你赶快拍出来看看'):
            self.assertTrue(continuation(event(text),request(pending())),text)
            self.assertTrue(continuation(event(text),request(pending()+[user('好'),bot('嗯，就这么定了')])),text)
    def test_ordinary_questions_cancellation_and_other_topics_stay_out(self):
        for text in ('好吃吗','拍一拍','拍拍我','拍桌子','视频怎么制作','为什么还没拍','拍好了没','拍完了吗','取消','不用了','别拍了','先不拍','明天吧','看看你会不会写代码'):
            self.assertFalse(continuation(event(text),request(pending())),text)
        for text in ('好','你倒是拍啊','换成蓝色'):
            self.assertFalse(continuation(event(text),request([user('晚饭吃面吗'),bot('可以，吃面吧')])),text)
            self.assertFalse(continuation(event(text),request(pending()+[user('晚饭吃什么'),bot('面条')])),text)
    def test_completion_refusal_and_unknown_are_not_retried_by_ack(self):
        for reply in ('拍好了，收好','给你看看','这次生成的结果未通过内容审核','这次无法确认服务端是否完成'):
            for text in ('好','那就拍吧','你倒是拍啊'):
                self.assertFalse(continuation(event(text),request(pending()+[user('好'),bot(reply)])),(text,reply))
                self.assertFalse(continuation(event(text),request(pending()+[user('好'),bot(reply),user('好'),bot('嗯')])),(text,reply))
        self.assertFalse(continuation(event('好',extra=[Reply(id='foreign')]),request(pending())))
    def test_actual_receipt_overrides_caption_that_sounds_like_offer(self):
        p=SimpleNamespace(_delivery=SimpleNamespace(recent=lambda *a,**k:[{'state':'confirmed','sender':'one','text':'这套可以','visual':{'intent':'animation'}}]))
        self.assertFalse(continuation(event('好'),request(pending()+[user('好'),bot('这套可以')]),p))
    def test_video_continuation_keeps_outfit_but_fresh_requests_reset(self):
        for text in ('我要看你拍的视频','拍个视频看看','我想看你录的短片'):
            self.assertIn('蓝白裙装',json.dumps(context_material(event(text),request(original()[:2])),ensure_ascii=False),text)
        self.assertEqual(context_material(event('画一只狗'),request(original()))['recent'],[])
        self.assertEqual(context_material(event('不用参考旧图，拍个视频看看'),request(original()))['recent'],[])
    def test_camera_from_continuous_artwork_is_preserved(self):
        plan={**base.PLAN,'camera_mode':'third_person','intent':'animation'}
        for text in ('我要看你拍的视频','你倒是拍啊','这个视频里要穿刚才的衣服'):
            material=context_material(event(text),request(pending()))
            self.assertEqual(normalize_plan(plan,material,None)['camera_mode'],'third_person',text)
        material=context_material(event('按刚才那套穿搭拍近景自拍'),request(pending()))
        self.assertEqual(normalize_plan(plan,material,None)['camera_mode'],'selfie')
    def test_group_owner_and_quote_prose_are_not_adopted(self):
        tagged=lambda text,sender:{'role':'user','content':'[群聊发言身份] '+json.dumps({'QQ':sender})+'\n'+text}
        own=[tagged('拍一张你穿蓝裙的自拍','one'),bot('可以换成白裙，按这个方案拍吗')]
        self.assertTrue(continuation(Event('你倒是拍啊'),request(own)))
        self.assertFalse(continuation(Event('你倒是拍啊',sender='other'),request(own)))
        self.assertFalse(continuation(Event('好'),request(own+[tagged('我的衣服呢','other'),bot('可以换成蓝色')])) )
        self.assertNotIn('拍',content_text(user('<Quoted Message>别人：拍个视频</Quoted Message>好')))

class Routing(unittest.IsolatedAsyncioTestCase):
    setup_flow=base.Flow.setup_flow
    async def test_amendment_routes_once_and_keeps_animation_attachment(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        self.setup_flow([base.answer(base.picture(True))],[base.PASS],{'image_generation_api_enabled':True})
        self.plugin._image_gif_unavailable={(id(self.provider),'gpt-image-2.6-sunburst'):__import__('time').monotonic()+1000}
        self.provider.text_chat.side_effect=[base.llm({**base.PLAN,'intent':'animation','scene':'成年龙娘穿蓝白裙装挥手','camera_mode':'third_person'}),base.llm(base.PASS)]
        e=event('这个视频里你要穿刚才这件衣服',extra=[Reply(id='reference')]);e.set_extra('qqbot_generation_request',request(original()))
        with patch.object(Provider,'_query',AsyncMock()) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=e))
            try:
                a,b=await asyncio.gather(Provider._query(self.provider,{}),Provider._query(self.provider,{}))
                self.assertIs(a,b);ordinary.assert_not_awaited();self.create.assert_awaited_once()
                self.assertEqual(e.get_extra('qqbot_generated_format'),'gif');self.assertIn('蓝白裙装',e.get_extra('qqbot_image_provenance')['scene'])
            finally:current_job.reset(token);restore()
    async def test_unrelated_chat_has_no_added_planning_call(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        self.setup_flow([],[],{'image_generation_api_enabled':True});e=event('晚饭吃什么');e.set_extra('qqbot_generation_request',request(pending()))
        with patch.object(Provider,'_query',AsyncMock(return_value='ordinary')) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=e))
            try:
                self.assertEqual(await Provider._query(self.provider,{}),'ordinary');self.provider.text_chat.assert_not_awaited();self.create.assert_not_awaited()
            finally:current_job.reset(token);restore()
    async def test_impatient_duplicate_does_not_submit_another_generation(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        self.setup_flow([],[],{'image_generation_api_enabled':True});started=asyncio.Event();release=asyncio.Event()
        first=event('拍一段挥手视频');first.set_extra('qqbot_generation_request',request(pending()))
        second=event('你倒是拍啊');second.set_extra('qqbot_generation_request',request(pending()))
        async def delayed(*a):started.set();await release.wait();return base.llm({'done':True})
        async def invoke(e):
            token=current_job.set(SimpleNamespace(event=e))
            try:return await Provider._query(self.provider,{})
            finally:current_job.reset(token)
        with patch('data.plugins.astrbot_plugin_proactive_agent.image_generation.generate',side_effect=delayed) as generation:
            restore=install(self.plugin)
            try:
                task=asyncio.create_task(invoke(first));await started.wait()
                answer=await invoke(second);self.assertIn('处理中',answer.completion_text);self.assertEqual(generation.call_count,1)
                release.set();await task
                # Once the old request has finished, new explicit edits still run.
                edited=event('这段视频换成蓝裙');edited.set_extra('qqbot_generation_request',request(pending()))
                await invoke(edited);self.assertEqual(generation.call_count,2)
            finally:release.set();restore()
    async def test_review_and_send_gap_remains_owned_until_receipt(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        self.setup_flow([],[],{'image_generation_api_enabled':True})
        first=event('拍视频');first.set_extra('qqbot_image_progress',{'stage':'ready'})
        job=ReplyJob(first.unified_msg_origin,first.get_sender_id(),'first','拍视频',event=first)
        job.cid='test-cid';job.session_epoch=0;job.state='suspended'
        self.plugin._reply_queue=SimpleNamespace(jobs={job.id:job})
        e=event('你倒是拍啊');e.set_extra('qqbot_generation_request',request(pending()))
        restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=e))
        try:
            result=await Provider._query(self.provider,{})
            self.assertIn('处理中',result.completion_text);self.provider.text_chat.assert_not_awaited();self.create.assert_not_awaited()
            # Re-entering a completed event must retain its own artwork even
            # when a newer edit is still awaiting review or delivery.
            cached=base.llm({'completed':'original artwork'})
            e.set_extra('qqbot_image_api_result',cached)
            self.assertIs(await Provider._query(self.provider,{}),cached)
        finally:current_job.reset(token);restore()
    async def test_completed_impatient_does_not_invent_outstanding_video_or_call_model(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        self.setup_flow([],[],{'image_generation_api_enabled':True})
        row={'state':'confirmed','sender':'one','source_text':'这个视频里你要穿刚才这件衣服','text':'','at':time.time(),'visual':{'intent':'animation'}}
        self.plugin._delivery=SimpleNamespace(recent=lambda *a,**k:[row])
        e=event('你倒是拍啊');e.set_extra('qqbot_generation_request',request(pending()))
        with patch.object(Provider,'_query',AsyncMock()) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=e))
            try:
                result=await Provider._query(self.provider,{})
                self.assertIn('已经发出',result.completion_text);self.assertTrue(result.qqbot_status_receipt)
                ordinary.assert_not_awaited();self.provider.text_chat.assert_not_awaited();self.create.assert_not_awaited()
            finally:current_job.reset(token);restore()

class Facts(unittest.IsolatedAsyncioTestCase):
    async def test_fact_is_bound_to_actual_attachment_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.sqlite3';ledger=DeliveryLedger(path);await ledger.start()
            e=event('拍视频');e.set_extra('qqbot_image_provenance',{'intent':'animation','method':'generated_action_frames','scene':'蓝白裙装挥手','content_hash':'a'*64})
            job=ReplyJob(e.unified_msg_origin,e.get_sender_id(),'test','拍视频',event=e);job.cid='test-cid'
            image=ledger.begin(job.origin,job.cid,'刚拍的',[{'kind':'image','content_hash':'a'*64}],job)
            emoji=ledger.begin(job.origin,job.cid,'',[{'kind':'image','content_hash':'b'*64}],job)
            self.assertNotIn('visual',ledger.store.rows[emoji]);self.assertNotIn('已发送作品',ledger.note(job.origin,job.cid))
            ledger.result(image,'confirmed','receipt');ledger.result(emoji,'failed');await ledger.store.close()
            ledger=DeliveryLedger(path);await ledger.start()
            try:
                self.assertIn('蓝白裙装',ledger.note(job.origin,job.cid))
                p=SimpleNamespace(_delivery=ledger);m=context_material(event('我要看你拍的视频'),request(pending()),p)
                self.assertEqual(m['confirmed_visual'][0]['visual']['intent'],'animation')
                other=Event('我要看你拍的视频',origin=job.origin,sender='other')
                self.assertEqual(context_material(other,request(pending()),p)['confirmed_visual'],[])
                reset=request(pending());reset.conversation.cid='reset';self.assertEqual(context_material(event('我要看你拍的视频'),reset,p)['confirmed_visual'],[])
            finally:await ledger.store.close()

if __name__=='__main__':unittest.main()
