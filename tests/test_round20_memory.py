import asyncio,copy,json,socket,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.long_memory_store import MemoryStore,terms
from data.plugins.astrbot_plugin_proactive_agent.memory_life import MemoryLife
from data.plugins.astrbot_plugin_proactive_agent.life_state import ProactiveLife
from data.plugins.astrbot_plugin_proactive_agent.safe_reader import validate_url,PublicResolver,read_web
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import BackgroundPaused

ORIGIN='test:FriendMessage:123456'
def memory(subject='123456',scope=ORIGIN,key='饮品偏好',content='喜欢茉莉花茶',at=100):
    return dict(scope=scope,subject=subject,fact_key=key,content=content,source_at=at,kind='preference',provenance='user_statement',sources=[{'id':str(at),'quote':content,'sender':subject,'at':at}])

class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=1000
        self.store=MemoryStore(Path(self.tmp.name)/'memory.db',lambda:self.now);self.store.start();self.addCleanup(self.store.close)
    def test_isolation_subjects_unicode_numbers_and_restart(self):
        a=self.store.remember(memory());self.store.remember(memory('2','test:GroupMessage:g',content='喜欢普洱茶'))
        self.assertEqual(self.store.recall(ORIGIN,'茉莉花茶')[0]['subject'],'123456')
        self.assertEqual(self.store.recall('other:FriendMessage:123456','茉莉花茶'),[])
        self.assertEqual(self.store.recall('test:GroupMessage:g','茉莉花茶'),[])
        self.assertIn('rtx4090',terms('我的RTX4090编号123'));self.assertIn('猫',terms('猫'))
        self.store.close();self.store.start();self.assertEqual(self.store.recall(ORIGIN,'茉莉')[0]['id'],a['id'])
    def test_quota_allows_correction_but_no_stale_overwrite(self):
        first=self.store.remember(memory(),1)
        self.assertEqual(self.store.remember(memory(key='颜色',content='喜欢蓝色'),1)['status'],'quota')
        self.assertEqual(self.store.remember(memory(content='现在喜欢乌龙茶',at=200),1)['status'],'corrected')
        self.assertEqual(self.store.remember(memory(),1)['status'],'stale')
        self.assertEqual(self.store.recall(ORIGIN,'茉莉花茶'),[])
        self.assertEqual(self.store.detail(first['id'])['status'],'superseded')
    def test_forget_invalidates_index_survives_restart_and_source_alias(self):
        result=self.store.remember(memory());body={'id':result['id'],'action':'forget','version':1,'request_id':'forget-once'}
        self.assertEqual(self.store.action(body),self.store.action(body));self.assertEqual(self.store.recall(ORIGIN,'茉莉'),[])
        self.store.close();self.store.start()
        self.assertEqual(self.store.remember(memory(at=200))['status'],'forgotten')
        self.assertEqual(self.store.remember(memory(key='喜欢的茶'))['status'],'forgotten')
        self.assertEqual(self.store.detail(result['id'])['sources'],[])
    def test_edit_revision_idempotency_and_older_extraction(self):
        result=self.store.remember(memory());body={'id':result['id'],'action':'correct','version':1,'request_id':'correct-once','content':'只喜欢红茶'}
        a=self.store.action(body);self.assertEqual(a,self.store.action(body))
        with self.assertRaises(RuntimeError):self.store.action({**body,'request_id':'different-request'})
        self.assertEqual(self.store.remember(memory(at=200))['status'],'stale')
        self.assertEqual(self.store.recall(ORIGIN,'红茶')[0]['content'],'只喜欢红茶')
    def test_pagination_traverses_all_without_duplicates(self):
        for i in range(69):self.store.remember(memory(key=f'条目{i}',content=f'项目编号item{i}',at=100+i),100)
        ids=[];cursor=0
        while True:
            page=self.store.listing(cursor=cursor);ids.extend(r['id'] for r in page['rows']);cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(ids),69);self.assertEqual(len(set(ids)),69)

class Runtime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=100000.;self.busy=False;self.config={'platform_id':'test'}
        self.p=SimpleNamespace(_cfg=self.config.get,_data_dir=self.root,_work=SimpleNamespace(busy=lambda:self.busy),_audit=lambda *_:None,
            _generate_json=AsyncMock(),context=SimpleNamespace(get_config=lambda:{'admins_id':['123456','astrbot']},get_registered_star=lambda _:None),
            _memory=SimpleNamespace(wiki=None),_quiet_now=lambda:False,_id_set=lambda x:set(x.split(',')) if x else set())
        self.life=ProactiveLife(self.p,self.root/'life.db',clock=lambda:self.now);await self.life.store.start();self.p._life=self.life
        self.s=MemoryLife(self.p,self.root,clock=lambda:self.now);self.p._memory_life=self.s;await self.s.start()
    async def asyncTearDown(self):await self.s.close();await self.life.store.close();self.tmp.cleanup()
    def enable(self,**kw):self.config.update(kw);self.s.config_changed()
    def observe(self,i=0):self.s.observe(ORIGIN,str(i),'我喜欢茉莉花茶','123456',self.now)
    def answer(self):return json.dumps({'memories':[{'source_id':'0','quote':'喜欢茉莉花茶','subject':'123456','fact_key':'茶偏好','kind':'preference','content':'喜欢茉莉花茶'}]})
    async def test_defaults_off_no_collection_injection_model(self):
        self.observe();await self.s.step();result=await self.s.projection(ORIGIN,'茉莉花茶')
        self.assertFalse(any(r.get('type')=='input' for r in self.s.store.rows.values()));self.assertEqual(result['memory_context'],'');self.p._generate_json.assert_not_called()
    async def test_eight_or_five_minutes_batched_no_foreground_calls(self):
        self.enable(long_memory_enabled=True);self.observe();self.assertFalse(await self.s.extract());self.p._generate_json.assert_not_called()
        self.now+=301;self.p._generate_json.return_value=self.answer();self.assertTrue(await self.s.extract())
        result=await self.s.projection(ORIGIN,'茉莉花茶');self.assertIn('123456',result['memory_context']);self.assertEqual(self.p._generate_json.await_count,1)
        self.assertEqual((await self.s.projection('test:FriendMessage:99999','茉莉花茶'))['memory_context'],'')
    async def test_eight_immediate_and_bad_subject_is_ignored(self):
        self.enable(long_memory_enabled=True)
        for i in range(8):self.observe(i)
        self.p._generate_json.return_value=self.answer().replace('123456','大肥龙')
        await self.s.extract();self.assertEqual(self.s.db.recall(ORIGIN,'茉莉花茶'),[])
    async def test_transient_failure_retries_not_completed(self):
        self.enable(long_memory_enabled=True);self.observe();self.now+=301;self.p._generate_json.side_effect=TimeoutError()
        await self.s.extract();rows=[r for r in self.s.store.rows.values() if r.get('type')=='input'];self.assertEqual(rows[0]['state'],'retry')
        self.now+=61;self.p._generate_json.side_effect=None;self.p._generate_json.return_value=self.answer();await self.s.extract()
        self.assertEqual(self.s.db.recall(ORIGIN,'茉莉')[0]['content'],'喜欢茉莉花茶')
    async def test_late_result_after_disable_discarded(self):
        self.enable(long_memory_enabled=True);self.observe();self.now+=301
        gate=asyncio.Event()
        async def generate(*_):await gate.wait();return self.answer()
        self.p._generate_json=generate;task=asyncio.create_task(self.s.extract());await asyncio.sleep(.01);self.enable(long_memory_enabled=False);gate.set()
        with self.assertRaises(BackgroundPaused):await task
        self.assertEqual(self.s.db.recall(ORIGIN,'茉莉'),[])
    async def test_busy_defers_all_new_background_work(self):
        self.enable(long_memory_enabled=True,active_reading_enabled=True);self.observe();self.now+=301;self.busy=True;await self.s.step();self.p._generate_json.assert_not_called()
    async def test_projection_budget_and_forget_cache(self):
        self.enable(long_memory_enabled=True)
        for i in range(7):self.s.db.remember(memory(key='茉莉'+str(i),content='茉莉'+('内容'*50)+str(i)))
        result=await self.s.projection(ORIGIN,'茉莉');self.assertLessEqual(len(result['memory_context']),400)
        row=self.s.db.recall(ORIGIN,'茉莉')[0];self.s.db.action({'id':row['id'],'version':row['revision'],'action':'forget','request_id':'forget-cache'})
        result=await self.s.projection(ORIGIN,'茉莉');self.assertNotIn(row['content'],result['memory_context'])
    async def test_sharing_defaults_preview_owner_only_and_private_source_stays_private(self):
        event={'visibility':'public','source_scope':'public','extended_material':'material:a'}
        self.assertFalse(self.s.share_allowed(event,ORIGIN,True));self.enable(experience_sharing_enabled=True,active_reading_enabled=True)
        self.assertTrue(self.s.share_allowed(event,ORIGIN,True));self.assertFalse(self.s.share_allowed(event,ORIGIN))
        self.enable(experience_share_auto=True,experience_share_qzone=True,experience_share_groups='998877')
        self.assertTrue(self.s.share_allowed(event,ORIGIN));self.assertFalse(self.s.share_allowed(event,'test:FriendMessage:777777'))
        private={**event,'source_scope':ORIGIN};self.assertFalse(self.s.share_allowed(private,'qzone'));self.assertFalse(self.s.share_allowed(private,'test:GroupMessage:998877'))
    async def test_new_material_cannot_enter_old_sender_or_public_redaction(self):
        self.enable(enable=True,allow_private=True,proactive_life_enabled=True,life_public_auto=True)
        row=self.life.event('chat_confirmed','双方共同聊了很有趣的事情',visibility=ORIGIN,completed=True,extra={'extended_material':'memory:1','source_scope':ORIGIN})
        self.assertIsNone(self.life.candidate(ORIGIN));await self.life.public.step();self.p._generate_json.assert_not_called()
        self.enable(experience_sharing_enabled=True,long_memory_enabled=True);before=copy.deepcopy(self.life.store.rows)
        result=await self.life.opportunity(ORIGIN,dry_run=True);self.assertTrue(result['preview']);self.assertEqual(before,self.life.store.rows)
    async def test_failed_read_retains_error_and_retry(self):
        self.enable(active_reading_enabled=True);job=await self.s.queue_read('bili','测试视频');await self.s.reading()
        self.assertEqual(self.s.store.rows[job['id']]['state'],'retry');self.assertIn('未启用',self.s.store.rows[job['id']]['error'])
    async def test_reading_does_not_send_and_uses_exact_evidence(self):
        self.enable(active_reading_enabled=True)
        wiki=SimpleNamespace(exact=lambda _:dict(phrase='测试梗',meaning='某个测试场景中的调侃',provenance='memewiki',sources=['https://example.org/entry']),titles={})
        self.p._memory.wiki=wiki;await self.s.queue_read('wiki','测试梗');self.p._generate_json.return_value=json.dumps({'quote':'测试场景','thought':'这个接法挺好玩','topic':'测试梗'})
        with patch('data.plugins.astrbot_plugin_proactive_agent.persona_authority.bind_origin',AsyncMock(return_value={})):
            await self.s.reading()
        material=next(r for r in self.s.store.rows.values() if r.get('type')=='material');self.assertEqual(material['state'],'ready');self.assertIn('本地梗百科',material['scope'])
        self.assertIsNone(self.life.candidate(ORIGIN));self.assertEqual(self.p._generate_json.await_count,1)

class ReaderSafety(unittest.IsolatedAsyncioTestCase):
    async def test_allowlist_private_metadata_auth_and_ports(self):
        for url in ['http://127.0.0.1/a','http://169.254.169.254/latest','http://10.0.0.1','https://allowed.com.evil/x','https://user:pass@allowed.com','file:///a','https://allowed.com:123/x']:
            with self.subTest(url=url),self.assertRaises(ValueError):validate_url(url,['allowed.com','127.0.0.1','169.254.169.254','10.0.0.1'])
        self.assertEqual(validate_url('https://allowed.com/a#b',['allowed.com']),'https://allowed.com/a')
    async def test_dns_mixed_public_private_rejected_and_pinned_result(self):
        loop=asyncio.get_running_loop();resolver=PublicResolver()
        def row(ip):return (socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))
        with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[row('1.1.1.1'),row('127.0.0.1')])):
            with self.assertRaises(ValueError):await resolver.resolve('allowed.com',443)
        with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[row('1.1.1.1')])):
            self.assertEqual((await resolver.resolve('allowed.com',443))[0]['host'],'1.1.1.1')
    async def test_redirect_to_internal_rejected_before_second_request(self):
        class Response:
            status=302;headers={'Location':'http://127.0.0.1/internal'}
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
        class Session:
            def __init__(self,**kw):self.connector=kw['connector']
            async def __aenter__(self):return self
            async def __aexit__(self,*_):await self.connector.close()
            def get(self,*a,**kw):return Response()
        reserve=AsyncMock()
        with self.assertRaises(ValueError):await read_web('https://allowed.com/a',['allowed.com','127.0.0.1'],reserve,session_factory=Session)
        reserve.assert_awaited_once()
    async def test_decompressed_response_bound_and_whole_request_deadline(self):
        class Response:
            status=200;headers={'Content-Type':'text/plain'};content_length=None;charset='utf-8'
            def __init__(self,slow=False):self.content=self;self.slow=slow
            def raise_for_status(self):pass
            async def iter_chunked(self,size):
                if self.slow:await asyncio.sleep(1)
                for _ in range(65):yield b'x'*32768
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
        class Session:
            slow=False
            def __init__(self,**kw):self.connector=kw['connector']
            async def __aenter__(self):return self
            async def __aexit__(self,*_):await self.connector.close()
            def get(self,*a,**kw):return Response(self.slow)
        with self.assertRaisesRegex(ValueError,'2MB'):await read_web('https://allowed.com/a',['allowed.com'],AsyncMock(),session_factory=Session)
        Session.slow=True;real_timeout=asyncio.timeout;seen=[]
        def deadline(seconds):seen.append(seconds);return real_timeout(.02)
        with patch('data.plugins.astrbot_plugin_proactive_agent.safe_reader.asyncio.timeout',deadline):
            with self.assertRaises(TimeoutError):await read_web('https://allowed.com/a',['allowed.com'],AsyncMock(),session_factory=Session)
        self.assertEqual(seen,[10])

if __name__=='__main__':unittest.main()
