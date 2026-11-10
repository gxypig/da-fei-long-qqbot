import asyncio,json,time,unittest
from unittest.mock import AsyncMock,patch
from pathlib import Path
import test_reading_agency as fixture
M,ORIGIN,OTHER=fixture.M,fixture.ORIGIN,fixture.OTHER
from data.plugins.astrbot_plugin_proactive_agent.exploration_loop import ExplorationLoop
from data.plugins.astrbot_plugin_proactive_agent.reading_runtime import ReadingRuntime
from data.plugins.astrbot_plugin_proactive_agent.safe_reader import parse_raw
from data.plugins.astrbot_plugin_proactive_agent.reading_share import text_error,evidence_error
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import BackgroundPaused

BODY='留白能够突出主体。蓝色背景搭配少量金色细节，主角轮廓更清楚。'+('这里讨论不同的绘画风格和颜色关系。'*12)

class Exploration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.Agency.asyncSetUp
    asyncTearDown=fixture.Agency.asyncTearDown
    enable=fixture.Agency.enable
    setup=fixture.Agency.setup
    decision=fixture.Agency.decision
    source=fixture.Agency.source
    offers=fixture.Agency.offers
    search_decision=fixture.Agency.search_decision
    step=fixture.Agency.step
    async def material(self,offer=None):
        row=offer or self.offers(1)[0];self.a.update(offers=[row])
        await self.a.read(self.decision('read',result_ids=[row['id']]),[row],self.s.generation)
        with patch.object(self.s,'_read',AsyncMock(return_value={'title':'配色与留白','url':row['url'],'content':BODY,'body_verified':True,'scope':'网页正文','reading_level':'full_body','links':[{'id':'link:two','kind':'body','title':'配色研究','url':'https://example.org/second','paragraph':'第二个来源给出了配色研究'}]})):
            await self.s.reading()
        return self.s.store.rows[self.a.state()['pages'][-1]]
    def observation(self,m):return {'material_id':m['id'],'quote':'蓝色背景搭配少量金色细节','thought':'少量金色细节比铺满画面耐看','topic':'配色','relevant':True,'worth_sharing':False}
    async def test_legacy_migration_preserves_rest_and_actual_sources(self):
        self.setup();from data.plugins.astrbot_plugin_proactive_agent.reading_agency import KEY
        until=self.now+1800
        self.s.store.put(KEY,{'state':'resting','reason':'暂时不想继续','next_at':until,'plan':{'query':'绘画留白','visibility':ORIGIN},'offers':[{'url':'https://example.org/read','selected':True}], 'recent_actions':[{'action':'read','reason':'看过正文','at':self.now}]})
        self.s.store.put('material:old',{'type':'material','url':'https://example.org/read','content':BODY})
        await self.a.migrate();row=self.a.state()
        self.assertEqual(row['state'],'resting');self.assertEqual(row['next_at'],until)
        self.assertEqual(row['source_scope'],ORIGIN);self.assertEqual(row['pages'],['material:old'])
        self.assertEqual(row['actions'][0]['state'],'legacy_recorded')
        ident=row['id'];await self.a.migrate();self.assertEqual(self.a.state()['id'],ident)
    async def test_one_page_finish_without_second_read_or_share(self):
        self.setup();m=await self.material();await self.step(self.decision('finish',observations=[self.observation(m)]))
        self.assertEqual(self.a.state()['state'],'finished');self.assertEqual(len(self.a.state()['pages']),1)
        self.assertFalse(any(a['action']=='share' for a in self.a.state()['actions']))
        self.assertEqual(self.s.store.rows[m['id']]['thought'],'少量金色细节比铺满画面耐看')
    async def test_follow_actual_body_link_and_no_invented_url(self):
        self.setup();m=await self.material();await self.step(self.decision('follow',link_ids=['link:two'],observations=[self.observation(m)]))
        jobs=[r for r in self.s.store.rows.values() if r.get('type')=='read' and r.get('value')=='https://example.org/second']
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['depth'],1);self.assertEqual(jobs[0]['parent_material'],m['id'])
    async def test_independent_search_then_source_can_be_read_without_link(self):
        self.setup();m=await self.material();self.p._search_meme.return_value={'results':[{'title':'另一来源','url':'https://elsewhere.example/color'}]}
        decision=self.search_decision();decision['observations']=[self.observation(m)]
        await self.step(decision)
        self.assertTrue(any(o['url']=='https://elsewhere.example/color' for o in self.a.state()['offers']))
    async def test_resumed_read_can_search_without_lost_original_interest_id(self):
        self.setup();m=await self.material();self.a.update(source_scope=ORIGIN)
        self.p._search_meme.return_value={'results':[]}
        await self.step(self.decision('search',query='绘画 留白',public_topic=True))
        self.p._search_meme.assert_awaited_once_with('绘画 留白',ORIGIN)
        self.assertEqual(self.a.state()['source']['material_id'],m['id'])
        self.assertEqual(self.a.state()['source_scope'],ORIGIN)
    async def test_continuation_never_promotes_another_private_material(self):
        self.setup();m=await self.material();self.a.update(source_scope='public')
        self.s.store.put(m['id'],{**m,'visibility':OTHER})
        self.assertIsNone(self.a.continuation_source())
        await self.step(self.decision('search',query='绘画 留白',public_topic=True))
        self.p._search_meme.assert_not_awaited()
    async def test_explicit_unknown_source_is_not_replaced_by_fallback(self):
        self.setup();await self.material()
        await self.step(self.decision('search',source_id='invented',quote='不存在',query='绘画 留白',public_topic=True))
        self.p._search_meme.assert_not_awaited()
    async def test_network_completion_survives_chat_and_join_cancellation(self):
        self.setup();gate=asyncio.Event();started=asyncio.Event()
        async def download():started.set();await gate.wait();return {'text':'真实页面返回'}
        factory=AsyncMock(side_effect=download)
        one=asyncio.create_task(self.s.read_runtime.read(['slow'],factory));await started.wait()
        two=asyncio.create_task(self.s.read_runtime.read(['slow'],factory));await asyncio.sleep(.01)
        one.cancel();self.busy=True;gate.set()
        await asyncio.gather(one,return_exceptions=True)
        self.assertEqual((await two)['text'],'真实页面返回');factory.assert_awaited_once()
        self.busy=False;self.s.read_runtime=ReadingRuntime(self.s)
        self.assertEqual((await self.s.read_runtime.read(['slow'],factory))['text'],'真实页面返回');factory.assert_awaited_once()
    async def test_raw_fetch_checkpoint_postpones_parse_and_survives_restart(self):
        self.setup();raw={'url':'https://example.org/a','text':'<article>'+BODY+'</article>','ctype':'text/html','bytes':300}
        checkpoint=self.s.read_runtime.checkpoint(raw['url']);await checkpoint.save(raw);self.busy=True
        with self.assertRaises(BackgroundPaused):await checkpoint.parse(raw,parse_raw)
        self.busy=False;again=ReadingRuntime(self.s).checkpoint(raw['url'])
        result=await again.parse(await again.load(),parse_raw);self.assertEqual(result['content'],BODY)
        self.assertEqual(self.s.store.rows[again.id]['stage'],'parsed')
    async def test_old_fragment_reparsed_from_raw_without_download_or_quota(self):
        self.setup();url='https://example.org/article'
        self.s.store.put('material:legacy',{'type':'material','url':url,'visibility':'public','at':self.now,'title':'旧片段','content':'旧片段','scope':'页面片段'})
        await self.s.read_runtime.checkpoint(url).save({'url':url,'text':'<div class="article-body">'+BODY+'</div>','ctype':'text/html','bytes':1000})
        with patch(M+'safe_reader.aiohttp.ClientSession',side_effect=AssertionError('不应重新下载')):
            result=await self.s._read({'kind':'web','value':url,'visibility':'public','autonomous':True})
        self.assertEqual(result['parser_version'],2)
        self.assertEqual(result['reading_level'],'full_body')
        self.assertEqual(result['content'],BODY)
        self.assertFalse(any(k.startswith('webquota:') for k in self.s.store.rows))
    async def test_read_finishes_during_chat_without_interpretation(self):
        self.setup();row=self.offers(1)[0];self.a.update(offers=[row]);await self.a.read(self.decision('read',result_ids=['0']),[row],self.s.generation)
        async def fetch(job):self.busy=True;return {'title':'配色','url':row['url'],'content':BODY,'scope':'正文','body_verified':True}
        with patch.object(self.s,'_read',side_effect=fetch):await self.s.reading()
        self.assertEqual(len(self.a.state()['pages']),1);self.p._generate_json.assert_not_called()
        self.busy=False;self.s.agency=ExplorationLoop(self.s);self.a=self.s.agency
        self.assertEqual(len(self.a.state()['pages']),1)
    async def test_paused_or_finished_task_cannot_enqueue_next_read(self):
        self.setup();m=await self.material();id_=self.a.state()['id'];self.a.request_resume(id_,'pause')
        self.assertFalse(await self.a.step());self.a.request_resume(id_,'resume');self.assertFalse(self.a.state()['manual_pause'])
        self.a.request_resume(id_,'finish');self.assertEqual(self.a.state()['state'],'finished')
    async def test_cannot_switch_task_until_paused_model_has_exited(self):
        self.setup();self.a.update(state='paused',manual_pause=True)
        original=self.a.state()['id'];self.s.store.put('exploration:second',{'id':'second','type':'exploration','state':'paused'})
        self.a.running=True
        with self.assertRaisesRegex(ValueError,'正在退出'):self.a.request_resume('second','resume')
        self.assertEqual(self.a.state()['id'],original)
        self.a.running=False;self.a.request_resume('second','resume')
        self.assertEqual(self.a.state()['id'],'second')
    async def test_page_depth_and_time_caps_preserve_remaining_intent(self):
        self.setup();self.a.update(cycle_pages=[f'https://example.org/{i}' for i in range(8)])
        with self.assertRaises(BackgroundPaused):await self.a.read(self.decision('read',result_ids=['0']),self.offers(1),self.s.generation)
        self.assertTrue(self.a.state()['budget_paused']);self.assertFalse(any(r.get('type')=='read' for r in self.s.store.rows.values()))
        self.a.request_resume(self.a.state()['id'],'resume');self.assertEqual(self.a.state()['cycle_pages'],[])
        row=self.offers(1)[0]|{'depth':4}
        with self.assertRaises(ValueError):await self.a.read(self.decision('read',result_ids=['0']),[row],self.s.generation)
        self.a.update(active_seconds=601)
        await self.a.step();self.assertTrue(self.a.state()['budget_paused'])
    async def test_loop_and_same_content_do_not_reinterpret(self):
        self.setup();m=await self.material();await self.a.observations([self.observation(m)])
        row=self.offers(1)[0];before=len([r for r in self.s.store.rows.values() if r.get('type')=='read'])
        await self.a.read(self.decision('read',result_ids=['0']),[row],self.s.generation)
        self.assertEqual(len([r for r in self.s.store.rows.values() if r.get('type')=='read']),before)
        other=self.offers(2)[1];duplicate=await self.material(other)
        self.assertEqual(duplicate['duplicate_of'],m['id']);self.assertIn('interpreted_at',duplicate)

    async def test_share_budget_expiry_waits_for_local_work_to_exit(self):
        self.setup();exited=asyncio.Event()
        self.a.update(active_seconds=599.97,manual_wake=True,intent={'id':'budget-test','decision':self.decision('share'),'sources':[],'targets':[],'events':[],'offers':[]})
        async def work(*_):
            try:await asyncio.Event().wait()
            finally:exited.set()
        with patch.object(self.a,'execute',side_effect=work):
            with self.assertRaises(BackgroundPaused):await self.a.step()
        self.assertTrue(exited.is_set());self.assertTrue(self.a.state()['budget_paused'])
        self.assertIsNotNone(self.a.state()['intent']);self.assertFalse(self.a.running)
        self.assertIsNone(self.a._active_started)

    async def test_send_boundary_includes_current_step_elapsed_budget(self):
        self.setup();self.a.update(active_seconds=599)
        self.a._active_started=time.monotonic()-2
        with self.assertRaises(BackgroundPaused):await self.a.share_check(self.s.generation)
        self.assertTrue(self.a.state()['budget_paused'])
    async def test_failure_has_no_read_claim_and_can_retry_once(self):
        self.setup();offer=self.offers(1)[0];self.a.update(offers=[offer]);await self.a.read(self.decision('read',result_ids=['0']),[offer],self.s.generation)
        with patch.object(self.s,'_read',AsyncMock(side_effect=TimeoutError('网页超时'))):await self.s.reading()
        self.assertFalse(self.a.state()['pages']);job=next(r for r in self.s.store.rows.values() if r.get('type')=='read');self.assertEqual(job['state'],'failed')
        self.a.request_resume(self.a.state()['id'],'retry');self.assertEqual(self.s.store.rows[job['id']]['state'],'retry')
    async def test_wait_without_new_facts_never_spins_model(self):
        self.setup();await self.step(self.decision('wait',resume_on='new_information',next_check_seconds=61));self.now+=1000
        model=await self.step(self.decision());model.assert_not_awaited()
    async def test_unseen_other_chat_does_not_wake_private_exploration(self):
        self.setup();self.a.update(source_scope=ORIGIN)
        await self.step(self.decision('wait',resume_on='new_information',next_check_seconds=61));self.now+=1000
        self.s.discovery.observe(OTHER,'other-new','另一会话的新问题',self.now)
        model=await self.step(self.decision());model.assert_not_awaited()
        self.s.discovery.observe(ORIGIN,'own-new','同会话新增线索',self.now)
        model=await self.step(self.decision());model.assert_awaited_once()
    async def test_expiry_or_timestamp_only_update_is_not_new_fact(self):
        self.setup();self.a.update(source_scope=ORIGIN)
        self.s.discovery.observe(ORIGIN,'old','一天前已经知道的内容',self.now)
        event={'id':'event:wake','record_type':'event','visibility':'public','summary':'已知兴趣','source':'virtual','at':self.now,'expires_at':self.now+200000}
        self.life.store.put(event['id'],event)
        await self.step(self.decision('wait',resume_on='new_information',next_check_seconds=61));self.now+=86401
        self.life.store.put(event['id'],{**event,'at':self.now})
        model=await self.step(self.decision());model.assert_not_awaited()
        self.life.store.put(event['id'],{**event,'summary':'新的实际进展'})
        model=await self.step(self.decision());model.assert_awaited_once()
    async def test_visible_material_content_change_wakes_without_new_id(self):
        self.setup();self.a.update(source_scope=ORIGIN)
        material={'id':'material:wake','type':'material','visibility':'public','content':'原来的正文','state':'ready','at':self.now}
        self.s.materials=[material]
        await self.step(self.decision('wait',resume_on='new_information',next_check_seconds=61));self.now+=1000
        self.s.materials=[{**material,'content':'原页面更新后的正文'}]
        model=await self.step(self.decision());model.assert_awaited_once()
    async def test_model_preemption_waits_for_real_cleanup(self):
        self.setup();start=asyncio.Event();cleanup=asyncio.Event()
        async def generate():
            start.set()
            try:await asyncio.sleep(9)
            finally:await asyncio.sleep(.04);cleanup.set()
        task=asyncio.create_task(self.s.read_runtime.model(generate(),self.s.generation));await start.wait();then=time.monotonic();self.busy=True
        with self.assertRaises(BackgroundPaused):await task
        self.assertLess(time.monotonic()-then,.2);self.assertTrue(cleanup.is_set())
    async def test_public_planning_never_gets_other_private_chat_text(self):
        self.setup();self.p._recent_messages={OTHER:[{'text':'我的住址是测试街，喜欢金色细节'}]}
        self.life.store.put('contact:'+OTHER,{'last_content':'我的私人银行卡信息'})
        self.a.update(source_scope='public');m=await self.material();await self.a.observations([self.observation(m)])
        targets=await self.a.targets(self.s.generation)
        self.assertNotIn('银行卡',str(targets));self.assertNotIn('住址',str(targets));self.assertNotIn('测试街',str(targets))
        self.assertTrue(next(r for r in targets if r['id']==OTHER)['matched_public_terms'])
    async def test_idle_grace_and_chat_cooldown_are_not_lost(self):
        self.setup();self.assertFalse(self.s.read_runtime.ready());self.now+=9;self.assertFalse(self.s.read_runtime.ready())
        self.now+=1;self.assertTrue(self.s.read_runtime.ready());self.busy=True;self.assertFalse(self.s.read_runtime.ready())
        self.busy=False;self.assertFalse(self.s.read_runtime.ready());self.now+=10;self.assertTrue(self.s.read_runtime.ready())
    async def test_panel_task_version_and_idempotent_controls(self):
        from quart import Quart
        from data.plugins.astrbot_plugin_proactive_agent.memory_panel import register_memory_panel
        self.setup();task=self.a.update(state='waiting');routes={}
        self.p.context.register_web_api=lambda path,fn,methods,desc:routes.update({path:(fn,methods)})
        register_memory_panel(self.p);app=Quart('round22-panel')
        for i,(path,(fn,methods)) in enumerate(routes.items()):app.add_url_rule(path,str(i),fn,methods=methods)
        client=app.test_client();url='/astrbot_plugin_proactive_agent/panel_memory_action'
        body={'kind':'exploration','id':task['id'],'version':task['revision'],'action':'pause','request_id':'round22-request1'}
        self.assertEqual((await client.post(url,json=body)).status_code,200)
        self.assertEqual((await client.post(url,json=body)).status_code,200)
        self.assertEqual((await client.post(url,json=body|{'request_id':'round22-request2'})).status_code,409)
        self.p._life.store.put('legacy-basis',{'origin':ORIGIN,'basis':'旧记录的依据是文字','status':'confirmed'})
        self.p._life.store.put('empty-basis',{'origin':ORIGIN,'basis':None,'status':'draft'})
        detail=await (await client.get('/astrbot_plugin_proactive_agent/panel_memory_detail',query_string={'kind':'exploration','id':task['id']})).get_json()
        self.assertEqual(detail['data']['state'],'paused');self.assertIn('materials',detail['data'])
        self.assertEqual(detail['data']['shares'],[])

class ShareBoundary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_round2_life
        await test_round2_life.LifeTests.asyncSetUp(self)
        from types import SimpleNamespace
        self.config.update(proactive_life_enabled=True)
        self.material={'id':'material:one','content':BODY,'basis':'蓝色背景搭配少量金色细节','thought':'少量金色细节比铺满画面耐看','body_verified':True}
        self.plugin._memory_life=SimpleNamespace(store=SimpleNamespace(rows={self.material['id']:self.material}),material_enabled=lambda e:True,share_allowed=lambda *a,**k:True)
        self.event=self.life.event('tool','读了绘画文章',visibility='public',completed=True,extra={'operation':'extended_reading','extended_material':self.material['id']})
        self.plugin._post_qzone=AsyncMock(return_value={'tid':'confirmed-test'})
    async def test_three_bad_generations_saved_no_post_and_reviewer_off(self):
        self.plugin._generate_json.return_value=json.dumps({'decision':'send','content':'https://example.org/article'})
        result=await self.life.opportunity('qzone',event_id=self.event['id'])
        self.assertEqual(result['decision'],'drop');self.plugin._post_qzone.assert_not_called()
        self.assertEqual(self.plugin._generate_json.await_count,3)
        candidate=next(r for k,r in self.life.store.rows.items() if k.startswith('candidate:'));self.assertEqual(len(candidate['drafts']),3)
    async def test_excerpt_rejected_before_generation(self):
        self.material['body_verified']=False
        result=await self.life.opportunity('qzone',event_id=self.event['id'])
        self.assertEqual(result['decision'],'drop');self.plugin._generate_json.assert_not_called();self.plugin._post_qzone.assert_not_called()

class ReadingEvidence(unittest.TestCase):
    def test_div_article_body_is_readable_but_navigation_list_is_not(self):
        base={'url':'https://example.org/post','ctype':'text/html','bytes':900}
        result=parse_raw(base|{'text':'<div class="post-content"><p>'+BODY+'</p><p>正文提到<a href="/study">另一篇配色研究</a>，可以继续核对。</p></div>'})
        self.assertTrue(result['body_verified']);self.assertEqual(result['links'][0]['url'],'https://example.org/study')
        links=''.join('<p><a href="/nav">推荐列表里的标题内容</a></p>' for _ in range(30))
        self.assertFalse(parse_raw(base|{'text':'<div class="post-content">'+links+'</div>'})['body_verified'])
        self.assertFalse(parse_raw(base|{'text':'<div class="content">'+BODY+'</div>'})['body_verified'])
    def test_body_links_exclude_navigation_ads_and_private_addresses(self):
        html='<nav><a href="https://x.example/menu">菜单页面</a></nav><article><p>一个有用的正文来源，可以参见<a href="/second">实际研究</a></p><aside><a href="/advert">广告内容</a></aside><p>内网禁止<a href="http://127.0.0.1/secret">内部接口</a></p>'+BODY+'</article>'
        result=parse_raw({'url':'https://example.org/first','text':html,'ctype':'text/html','bytes':600})
        self.assertEqual([r['url'] for r in result['links']],['https://example.org/second']);self.assertIn('正文来源',result['links'][0]['paragraph'])
        self.assertEqual(result['reading_level'],'full_body');self.assertTrue(result['body_verified'])
    def test_summary_and_naked_link_cannot_publish_even_without_reviewer(self):
        m={'content':BODY,'basis':'蓝色背景搭配少量金色细节','thought':'少量金色细节比铺满画面耐看','body_verified':True}
        self.assertTrue(evidence_error(m|{'body_verified':False,'reading_level':'search_excerpt'}))
        self.assertTrue(text_error(m,'有意思 https://example.org/post'))
        self.assertTrue(text_error(m,'今天看了一篇文章，我认为很有意思，也值得大家认真思考'))
        self.assertFalse(text_error(m,'蓝色背景搭配少量金色细节，我觉得比铺满画面耐看，视线会落在主角身上 https://example.org/post'))

if __name__=='__main__':unittest.main()
