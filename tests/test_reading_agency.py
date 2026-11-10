import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import test_round20_memory as fixture
from data.plugins.astrbot_plugin_proactive_agent.reading_agency import KEY,public_url
from data.plugins.astrbot_plugin_proactive_agent.fixed_schedule import plans

M='data.plugins.astrbot_plugin_proactive_agent.'
ORIGIN=fixture.ORIGIN
OTHER='test:FriendMessage:666666'


class Agency(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.Runtime.asyncSetUp
    asyncTearDown=fixture.Runtime.asyncTearDown
    enable=fixture.Runtime.enable
    def setup(self):
        self.enable(active_reading_enabled=True,reading_discovery_enabled=True,reading_autonomy_enabled=True,
                    reading_web_enabled=True,reading_allowed_domains='tieba.baidu.com',allow_private=True,
                    experience_sharing_enabled=True,experience_share_auto=True)
        self.p._targets=SimpleNamespace(refresh=AsyncMock(),contacts={'FriendMessage':{'123456':{'name':'甲'},'666666':{'name':'乙'}}},status=lambda origin:{'available':origin in {ORIGIN,OTHER}})
        self.p._search_meme=AsyncMock();self.p._recent_messages={}
        self.life.store.put('life',{'interests':['动画绘画']})
        self.a=self.s.agency
    def decision(self,action='wait',**kw):
        return {'action':action,'reason':'想弄清楚绘画留白的作用','next_check_seconds':180,'resume_on':'time',**kw}
    def source(self):
        return self.a.sources()[0]
    def search_decision(self,**kw):
        src=self.source()
        return self.decision('search',source_id=src['id'],quote=src['text'],query='绘画 留白',public_topic=True,site='',**kw)
    def offers(self,count=4,scope='public'):
        return [{'id':str(i),'url':f'https://example.org/articles/{i}','title':f'文章{i}','at':self.now,'scope':scope,
                 'plan':{'kind':'web','query':'绘画 留白','visibility':scope}} for i in range(count)]
    async def step(self,result):
        with patch.object(self.a,'ask',AsyncMock(return_value=result)) as model:
            await self.s.reading()
        return model
    async def test_model_wait_sets_own_interval_and_does_not_spin(self):
        self.setup();await self.step(self.decision(next_check_seconds=17))
        self.assertEqual(self.a.state()['next_at'],self.now+17)
        with patch.object(self.a,'ask',AsyncMock()) as model:self.assertFalse(await self.a.step())
        model.assert_not_called()
        self.now+=61;await self.step(self.decision(next_check_seconds=7200))
        self.assertEqual(self.a.state()['next_at'],self.now+7200)
    async def test_new_chat_does_not_override_chosen_rest_or_start_front_models(self):
        self.setup();await self.step(self.decision(next_check_seconds=86400))
        self.s.observe(ORIGIN,'new','这种水墨画为什么好看','123456',self.now+1)
        self.p._generate_json.assert_not_called()
        model=await self.step(self.decision());model.assert_not_awaited()
    async def test_search_can_select_other_public_websites(self):
        self.setup();self.p._search_meme.return_value={'results':[{'url':'https://www.museum.example/article/1','title':'留白艺术'},{'url':'http://127.0.0.1/private','title':'错误'}]}
        await self.step(self.search_decision())
        self.p._search_meme.assert_awaited_once_with('绘画 留白','qzone')
        self.assertEqual(len(self.a.state()['offers']),1)
        self.assertIn('museum.example',self.a.state()['offers'][0]['url'])
        self.assertFalse(any(r.get('type')=='material' for r in self.s.store.rows.values()))
    async def test_model_can_choose_more_than_two_articles(self):
        self.setup();self.a.update(offers=self.offers())
        await self.step(self.decision('read',result_ids=['3','0','2']))
        jobs=[r for r in self.s.store.rows.values() if r.get('type')=='read']
        self.assertEqual(len(jobs),3);self.assertEqual({r['value'] for r in jobs},{'https://example.org/articles/'+i for i in ['0','2','3']})
        self.assertTrue(all(r['autonomous'] for r in jobs))
    async def test_explicit_reselection_retries_exhausted_read(self):
        self.setup();row=self.offers(1)[0];self.a.update(offers=[row])
        old=await self.s.queue_read('web',row['url'],'public',request_id='explore:'+self.a.state()['id']+':0')
        self.s.store.put(old['id'],{**old,'state':'failed','attempts':5})
        await self.a.read(self.decision('read',result_ids=['0']),[row],self.s.generation)
        resumed=self.s.store.rows[old['id']]
        self.assertEqual(resumed['state'],'retry');self.assertEqual(resumed['attempts'],0)
        self.assertEqual(resumed['retry_requested_by'],'autonomous')
    async def test_unknown_result_ids_do_not_partially_enqueue(self):
        self.setup();self.a.update(offers=self.offers())
        await self.step(self.decision('read',result_ids=['0','invented']))
        self.assertFalse(any(r.get('type')=='read' for r in self.s.store.rows.values()))
        self.assertEqual(self.a.state()['state'],'retry')
    async def test_private_search_and_read_scope_preserved(self):
        self.setup();self.a.update(offers=self.offers(1,ORIGIN))
        await self.step(self.decision('read',result_ids=['0']))
        self.assertEqual(next(r for r in self.s.store.rows.values() if r.get('type')=='read')['visibility'],ORIGIN)
    async def test_old_daily_cap_does_not_stop_autonomous_read(self):
        self.setup()
        from data.plugins.astrbot_plugin_proactive_agent.long_memory_store import day
        quota='webquota:'+day(self.now);self.s.store.put(quota,{'count':50})
        job=await self.s.queue_read('web','https://example.org/article','public')
        async def fetch(url,allowed,reserve,**kw):
            await reserve();return {'url':url,'content':'真实正文'}
        with patch(M+'safe_reader.read_web',side_effect=fetch):result=await self.s._read({**job,'autonomous':True})
        self.assertEqual(result['content'],'真实正文');self.assertEqual(self.s.store.rows[quota]['count'],51)
    async def test_generic_failure_does_not_fallback_to_excerpt(self):
        self.setup();job=await self.s.queue_read('web','https://example.org/article','public')
        with patch(M+'safe_reader.read_web',AsyncMock(side_effect=ValueError('验证'))):
            with self.assertRaises(ValueError):await self.s._read({**job,'autonomous':True,'search_excerpt':{'url':job['value'],'excerpt':'摘要'*100}})
    async def test_old_generic_excerpt_cache_is_not_a_new_autonomous_read(self):
        self.setup();url='https://example.org/article';job=await self.s.queue_read('web',url,'public')
        self.s.store.put('material:old',{'type':'material','url':url,'at':self.now,'visibility':'public','scope':'搜索摘要','content':'旧摘要'})
        with patch(M+'safe_reader.read_web',AsyncMock(return_value={'content':'实际正文'})) as fetch:
            result=await self.s._read({**job,'autonomous':True})
        fetch.assert_awaited_once();self.assertEqual(result['content'],'实际正文')
    async def test_bili_selected_material_does_not_repeat_discovery(self):
        self.setup();row=self.offers(1)[0];row.update(plan={'kind':'bili','query':'留白','visibility':'public'},material={'title':'动画','content':'实际简介','url':'https://www.bilibili.com/video/BVtest','scope':'简介','bvid':'BVtest'})
        self.a.update(offers=[row]);await self.step(self.decision('read',result_ids=['0']))
        self.p._generate_json.return_value=json.dumps({'relevant':True,'thought':'','worth_sharing':False})
        with patch(M+'persona_authority.bind_origin',AsyncMock(return_value={})),patch.object(self.s,'_read',AsyncMock()) as read:await self.s.reading()
        read.assert_not_called();materials=[r for r in self.s.store.rows.values() if r.get('type')=='material'];self.assertEqual(len(materials),1);self.assertEqual(materials[0]['visibility'],'public');self.assertEqual(materials[0]['state'],'read')
    async def test_model_selects_event_and_target_without_unanswered_stop(self):
        self.setup();self.a.update(source_scope=OTHER);event=self.life.event('tool','实际读了某篇文章',visibility='public',completed=True)
        self.life.store.put('contact:'+OTHER,{'unanswered':50,'last_confirmed_at':self.now,'last_content':'聊过水墨画法'})
        with patch.object(self.life,'opportunity',AsyncMock(return_value={'decision':'confirmed','reason':'有新看法'})) as send:
            await self.step(self.decision('share',event_id=event['id'],target_id=OTHER,contact_reason='继续聊画法',target_quote='水墨画法'))
        self.assertEqual(send.call_args.args[0],OTHER);self.assertEqual(send.call_args.kwargs['event_id'],event['id'])
        self.assertEqual(send.call_args.kwargs['mode'],'autonomous')
    async def test_actual_decision_ignores_unanswered_and_old_cooldowns(self):
        self.setup();event=self.life.event('tool','刚读到一件新东西',visibility='public',completed=True)
        self.life.store.put('contact:'+OTHER,{'unanswered':50,'last_confirmed_at':self.now,'last_inbound':self.now,'next_candidate_after':self.now+3600})
        c=self.life.candidate(OTHER,mode='autonomous',event_id=event['id'])
        self.assertEqual(self.life.decide(c)['decision'],'send')
        self.busy=True;self.assertEqual(self.life.decide(c)['decision'],'defer')
        self.busy=False;self.p._quiet_now=lambda:True;self.assertEqual(self.life.decide(c)['decision'],'defer')
    async def test_private_event_cannot_be_shared_to_other_target(self):
        self.setup();event=self.life.event('chat_confirmed','甲的私人经历',visibility=ORIGIN,completed=True)
        with patch.object(self.life,'opportunity',AsyncMock()) as send:await self.step(self.decision('share',event_id=event['id'],target_id=OTHER))
        send.assert_not_called();self.assertEqual(self.a.state()['state'],'retry')
    async def test_confirmed_and_unknown_events_cannot_be_selected_again(self):
        self.setup();event=self.life.event('tool','已分享的事情',visibility='public',completed=True)
        c=self.life.candidate(OTHER,mode='autonomous',event_id=event['id'])
        for status in ['confirmed','unknown','submitting']:
            self.life.store.put('candidate:'+c['id'],{**c,'status':status})
            self.assertIsNone(self.life.candidate(OTHER,mode='autonomous',event_id=event['id']))
    async def test_normal_timer_delegates_but_required_tasks_remain(self):
        self.setup();self.enable(fixed_messages_enabled=True,fixed_private_ids='123456',fixed_message_interval_minutes=149)
        self.assertEqual(plans(self.p),{});self.assertIn('private:123456',plans(self.p,include_disabled=True))
        self.enable(fixed_message_required=True);self.assertIn('private:123456',plans(self.p))
        self.assertEqual((await self.life.free_opportunity())['decision'],'delegated')
    async def test_disabling_restores_legacy_timer_and_whitelist(self):
        self.setup();self.enable(fixed_messages_enabled=True,fixed_private_ids='123456',reading_autonomy_enabled=False)
        self.assertIn('private:123456',plans(self.p))
        with self.assertRaises(ValueError):await self.s.queue_read('web','https://example.org/a','public')
        self.assertFalse(await self.a.step())
    async def test_switching_mode_cancels_legacy_discretionary_draft(self):
        self.setup();event=self.life.event('tool','可分享的事情',visibility='public',completed=True)
        candidate=self.life.candidate(OTHER,mode='free',event_id=event['id'])
        self.assertEqual(self.life.decide(candidate)['decision'],'defer')
        self.assertIn('旧定时草稿',self.life.decide(candidate)['reason'])
    async def test_model_error_backoff_prevents_failure_loop(self):
        self.setup();await self.step({'action':'wait','next_check_seconds':float('nan'),'reason':'不合格'})
        retry=self.a.state()['retry_at'];self.assertGreater(retry,self.now)
        self.s.observe(ORIGIN,'new','新想法','123456',self.now+1)
        with patch.object(self.a,'ask',AsyncMock()) as model:self.assertFalse(await self.a.step())
        model.assert_not_called()
    async def test_pending_model_cancelled_quickly_for_front_chat(self):
        self.setup();started=asyncio.Event();cancelled=asyncio.Event()
        async def generation(*args):
            started.set()
            try:await asyncio.sleep(20)
            finally:cancelled.set()
        self.p._generate_json= generation
        with patch(M+'persona_authority.bind_origin',AsyncMock(return_value={})):
            task=asyncio.create_task(self.a.step());await started.wait();self.busy=True
            with self.assertRaises(Exception):await asyncio.wait_for(task,.6)
        self.assertTrue(cancelled.is_set());self.assertEqual(self.a.state()['state'],'paused_for_chat')
    async def test_mode_does_not_override_closed_target_channel(self):
        self.setup();self.enable(allow_private=False)
        event=self.life.event('tool','新文章',visibility='public',completed=True)
        with patch.object(self.life,'opportunity',AsyncMock()) as send:await self.step(self.decision('share',event_id=event['id'],target_id=OTHER))
        send.assert_not_called()


class Validation(unittest.TestCase):
    def test_general_article_reader_excludes_long_navigation(self):
        from data.plugins.astrbot_plugin_proactive_agent.safe_reader import main_article
        result=main_article('<nav>'+'导航'*10000+'</nav><main><aside>推荐内容</aside><article>'+'实际文章段落。'*100+'</article></main>')
        self.assertNotIn('导航',result);self.assertNotIn('推荐内容',result);self.assertIn('实际文章',result)
    def test_public_sites_supported_private_addresses_rejected(self):
        self.assertEqual(public_url('https://example.org/a'),'https://example.org/a')
        for url in ['http://127.0.0.1/','http://169.254.169.254/','file:///C:/secret','https://u:p@example.org/a','https://example.org:6197/a']:
            with self.subTest(url=url),self.assertRaises(ValueError):public_url(url)


if __name__=='__main__':unittest.main()
