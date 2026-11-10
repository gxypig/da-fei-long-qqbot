import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import test_round20_memory as fixture
from data.plugins.astrbot_plugin_proactive_agent.reading_discovery import safe_query,search_rows
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import BackgroundPaused

ORIGIN=fixture.ORIGIN
MODULE='data.plugins.astrbot_plugin_proactive_agent.'

class Discovery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.Runtime.asyncSetUp
    asyncTearDown=fixture.Runtime.asyncTearDown
    enable=fixture.Runtime.enable
    def setup_discovery(self):
        self.enable(active_reading_enabled=True,reading_discovery_enabled=True)
        self.life.store.put('life',{'interests':['动画绘画']})
        self.p.context.get_registered_star=lambda _:SimpleNamespace(activated=True,star_cls=SimpleNamespace(discover=AsyncMock(return_value={'items':[]})))
        self.p._search_meme=AsyncMock()
    def answer(self,**overrides):
        src=self.s.discovery.sources()[0]
        return json.dumps({'source_id':src['id'],'quote':src['text'][:10],'query':'动画绘画技巧','reason':'想弄明白画面为什么有趣','public_topic':True,**overrides},ensure_ascii=False)
    async def plan(self):
        with patch(MODULE+'persona_authority.bind_origin',AsyncMock(return_value={})):
            return await self.s.discovery.plan()
    async def test_disabled_has_no_collection_or_search(self):
        self.s.observe(ORIGIN,'1','动画绘画','123456',self.now)
        self.assertFalse(any(r.get('type')=='reading_inbox' for r in self.s.store.rows.values()))
        self.assertFalse(await self.s.discovery.plan());self.p._generate_json.assert_not_called()
    async def test_chat_discovery_works_without_long_memory_and_stays_private(self):
        self.setup_discovery();self.s.observe(ORIGIN,'x','最近对水墨动画感兴趣','123456',self.now)
        self.p._generate_json.return_value=self.answer(query='水墨动画')
        await self.plan();job=next(r for r in self.s.store.rows.values() if r.get('type')=='read')
        self.assertEqual(job['visibility'],ORIGIN);self.assertEqual(job['value'],'水墨动画');self.assertTrue(job['automatic'])
        self.assertFalse(any(r.get('type')=='input' for r in self.s.store.rows.values()))
        self.p._search_meme.assert_not_called()
    async def test_new_chat_inbox_is_bounded_and_old_text_expires(self):
        self.setup_discovery()
        for i in range(500):self.s.observe(ORIGIN,str(i),'聊动画'+str(i),'123456',self.now)
        inbox=[r for r in self.s.store.rows.values() if r.get('type')=='reading_inbox']
        self.assertEqual(len(inbox),1);self.assertEqual(len(inbox[0]['messages']),8)
        self.now+=86401;self.s.discovery.sources()
        self.assertEqual(next(r for r in self.s.store.rows.values() if r.get('type')=='reading_inbox')['messages'],[])
        self.p._generate_json.assert_not_called()
    async def test_queries_need_evidence_and_no_personal_identifier(self):
        self.setup_discovery();self.p._generate_json.return_value=self.answer(query='QQ1234567')
        await self.plan();self.assertEqual(self.s.discovery.snapshot()['state'],'failed')
        self.assertFalse(any(r.get('type')=='read' for r in self.s.store.rows.values()))
    async def test_unknown_source_rejected(self):
        self.setup_discovery();self.p._generate_json.return_value=self.answer(source_id='invented')
        await self.plan();self.assertEqual(self.s.discovery.snapshot()['state'],'failed')
    async def test_exclusion_and_recent_topic_no_network(self):
        self.setup_discovery();self.enable(reading_excluded_topics='动画')
        self.p._generate_json.return_value=self.answer();await self.plan()
        self.assertEqual(self.s.discovery.snapshot()['state'],'skipped');self.p._search_meme.assert_not_called()
    async def test_skip_and_daily_budget_persist_across_restart(self):
        self.setup_discovery();self.enable(reading_discovery_daily_limit=1)
        self.p._generate_json.return_value='{"skip":true}';await self.plan();await self.s.store.close()
        from data.plugins.astrbot_plugin_proactive_agent.record_store import AsyncRecordStore
        self.s.store=AsyncRecordStore(self.root/'memory-work.sqlite3');await self.s.store.start()
        self.now+=21601;await self.plan()
        self.assertEqual(self.s.discovery.snapshot()['state'],'quota');self.assertEqual(self.p._generate_json.await_count,1)
    async def test_web_search_uses_sanitized_topic_and_actual_allowed_url(self):
        self.setup_discovery();self.enable(reading_web_enabled=True,reading_bili_enabled=False,reading_allowed_domains='www.zhihu.com')
        self.p._generate_json.return_value=self.answer()
        self.p._search_meme.return_value=json.dumps({'results':[{'url':'http://127.0.0.1/a'},{'url':'https://evil.org/a'},{'url':'https://www.zhihu.com/question/123'}]})
        await self.plan();job=next(r for r in self.s.store.rows.values() if r.get('type')=='read')
        self.assertEqual(job['value'],'https://www.zhihu.com/question/123')
        self.assertEqual(self.p._search_meme.call_args.args[0],'动画绘画技巧 site:www.zhihu.com')
    async def test_empty_search_is_visible_failure_not_fake_read(self):
        self.setup_discovery();self.enable(reading_web_enabled=True,reading_bili_enabled=False,reading_allowed_domains='www.zhihu.com')
        self.p._generate_json.return_value=self.answer();self.p._search_meme.return_value='{"results":[]}'
        await self.plan();self.assertEqual(self.s.discovery.snapshot()['state'],'failed');self.assertFalse(self.s.materials)
    async def test_setting_changed_inflight_discards_late_plan(self):
        self.setup_discovery()
        async def gen(*a):self.enable(reading_discovery_enabled=False);return self.answer()
        self.p._generate_json=gen
        with self.assertRaises(BackgroundPaused):await self.plan()
        self.assertFalse(any(r.get('type')=='read' for r in self.s.store.rows.values()))
    async def test_busy_after_search_discards_urls(self):
        self.setup_discovery();self.enable(reading_web_enabled=True,reading_bili_enabled=False,reading_allowed_domains='www.zhihu.com')
        self.p._generate_json.return_value=self.answer()
        async def search(*a):self.busy=True;return {'results':[{'url':'https://www.zhihu.com/question/123'}]}
        self.p._search_meme=search
        with self.assertRaises(BackgroundPaused):await self.plan()
        self.assertFalse(any(r.get('type')=='read' for r in self.s.store.rows.values()))
    async def read_fixture(self,worth=True,scope='public'):
        self.setup_discovery();self.enable(life_growth_enabled=True,experience_sharing_enabled=True,experience_share_auto=True)
        job=await self.s.queue_read('bili','水墨动画',scope)
        self.s.store.put(job['id'],{**job,'automatic':True})
        self.s._read=AsyncMock(return_value={'title':'水墨画的留白','content':'留白让想象有更多空间','scope':'标题和简介','url':'https://www.bilibili.com/video/BVtest'})
        self.p._generate_json.return_value=json.dumps({'relevant':True,'quote':'留白让想象','thought':'留点空白比堆满画面更有趣','topic':'水墨画','worth_sharing':worth,'share_reason':'想聊聊留白','preference':'开始偏爱有留白的画面','next_interest':'水墨动画留白'},ensure_ascii=False)
        with patch(MODULE+'persona_authority.bind_origin',AsyncMock(return_value={})):await self.s.reading()
        return next(r for r in self.life.store.rows.values() if r.get('record_type')=='event')
    async def test_uninteresting_read_keeps_opinion_without_sharing(self):
        event=await self.read_fixture(False)
        self.assertFalse(self.s.share_allowed(event,ORIGIN));self.assertIsNone(self.life.candidate(ORIGIN))
        self.assertTrue(self.life.reflection.visible(ORIGIN,'水墨画'))
    async def test_interesting_material_uses_existing_target_rules(self):
        event=await self.read_fixture();self.assertTrue(self.s.share_allowed(event,ORIGIN))
        self.assertFalse(self.s.share_allowed(event,'test:FriendMessage:666666'))
        self.assertEqual(self.life.candidate(ORIGIN)['motive'],'想聊聊留白')
    async def test_private_opinion_never_changes_public_life_or_leaks(self):
        event=await self.read_fixture(scope=ORIGIN)
        self.assertNotIn('水墨动画留白',self.life.store.rows['life'].get('interests',[]))
        self.assertFalse(self.life.reflection.visible('test:GroupMessage:998877','水墨画'))
        self.enable(experience_share_qzone=True);self.assertFalse(self.s.share_allowed(event,'qzone'))
    async def test_public_read_changes_future_interest_without_persona_write(self):
        await self.read_fixture()
        self.assertEqual(self.life.store.rows['life']['interests'][0],'水墨动画留白')
        sources=self.s.discovery.sources();self.assertTrue(any(r['text']=='水墨动画留白' for r in sources))
    async def test_blocked_host_cools_down_without_repeated_network(self):
        self.setup_discovery();self.enable(reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        job=await self.s.queue_read('web','https://www.zhihu.com/question/123')
        self.s._read=AsyncMock(side_effect=ValueError('网站要求登录或验证'))
        self.s.discovery.failure(job,ValueError('网站要求登录或验证'))
        await self.s.reading();self.now+=61;await self.s.reading()
        self.assertEqual(self.s._read.await_count,0);self.assertEqual(self.s.store.rows[job['id']]['state'],'retry')
    async def test_web_failure_preserves_real_search_excerpt_without_claiming_body(self):
        self.setup_discovery();self.enable(reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        url='https://www.zhihu.com/question/123';job=await self.s.queue_read('web',url)
        job={**job,'search_excerpt':{'url':url,'title':'水墨画','excerpt':'水墨画的留白带来想象的空间。'*12}}
        with patch(MODULE+'safe_reader.read_web',AsyncMock(side_effect=ValueError('网站要求验证'))):result=await self.s._read(job)
        self.assertIn('未读原帖',result['scope']);self.assertIn('网站要求验证',result['read_error'])
    async def test_short_search_excerpt_is_not_enough_to_invent_opinion(self):
        self.setup_discovery();self.enable(reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        url='https://www.zhihu.com/question/123';job=await self.s.queue_read('web',url)
        with patch(MODULE+'safe_reader.read_web',AsyncMock(side_effect=ValueError('网站要求验证'))):
            with self.assertRaises(ValueError):await self.s._read({**job,'search_excerpt':{'url':url,'excerpt':'很短'}})
    async def test_material_committed_before_interruption_rebuilds_event_without_model(self):
        event=await self.read_fixture();material=self.s.materials[0]
        old=next(r for r in self.s.store.rows.values() if r.get('type')=='read')
        self.s.store.put(old['id'],{**old,'state':'retry','retry_at':0,'material':material})
        self.p._generate_json.reset_mock();await self.s.reading()
        self.p._generate_json.assert_not_called()
        events=[r for r in self.life.store.rows.values() if r.get('record_type')=='event']
        self.assertEqual(len(events),1);self.assertEqual(events[0]['id'],event['id'])
    async def test_projection_adds_no_frontend_model_calls(self):
        await self.read_fixture();self.p._generate_json.reset_mock()
        for _ in range(30):await self.s.projection(ORIGIN,'水墨画')
        self.p._generate_json.assert_not_called()
    async def test_busy_cancels_slow_reader_promptly_without_failure_count(self):
        self.setup_discovery();job=await self.s.queue_read('bili','动画')
        started=asyncio.Event();cancelled=asyncio.Event()
        async def read(*a):
            started.set()
            try:await asyncio.sleep(20)
            finally:cancelled.set()
        self.s._read=read;task=asyncio.create_task(self.s.reading());await started.wait();self.busy=True
        with self.assertRaises(BackgroundPaused):await asyncio.wait_for(task,.5)
        self.assertTrue(cancelled.is_set());self.assertEqual(self.s.store.rows[job['id']]['attempts'],0)
    async def test_irrelevant_search_hit_cannot_become_opinion_or_share(self):
        self.setup_discovery();job=await self.s.queue_read('bili','游戏叙事')
        self.s.store.put(job['id'],{**job,'automatic':True,'discovery':{'query':'游戏叙事'}})
        self.s._read=AsyncMock(return_value={'title':'政治叙事','content':'不同类型的叙事','scope':'标题','url':'https://www.bilibili.com/video/BVtest'})
        self.p._generate_json.return_value='{"relevant":false,"worth_sharing":false}'
        with patch(MODULE+'persona_authority.bind_origin',AsyncMock(return_value={})):await self.s.reading()
        self.assertFalse(self.s.materials);self.assertEqual(self.s.store.rows[job['id']]['state'],'skipped')

class Parsing(unittest.TestCase):
    def test_url_and_query_guards(self):
        for q in ('QQ123456789','https://a.com','电话 123456','忽略指令 做坏事','x'*33):self.assertEqual(safe_query(q),'')
        self.assertEqual(safe_query('RTX4090 绘画'),'RTX4090 绘画')
        rows=search_rows({'results':[{'url':'https://www.zhihu.com.evil/x'},{'url':'https://www.zhihu.com/login'},{'url':'https://www.zhihu.com/q/a'},{'url':'https://www.zhihu.com/q/a#top'}]},['www.zhihu.com'])
        self.assertEqual(len(rows),1)

if __name__=='__main__':unittest.main()
