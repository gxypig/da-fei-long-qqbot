import asyncio,gzip,hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import test_memewiki_integration as fixtures
from data.plugins.astrbot_plugin_proactive_agent.wiki_update import WikiUpdater,WikiConflict,validate_db

class UpdateTests(fixtures.MemeWikiIntegration):
    async def asyncSetUp(self):
        await super().asyncSetUp();self.cfg={'wiki_update_enabled':False}
        self.p=SimpleNamespace(_memory=self.memory,_cfg=self.cfg.get,_work=SimpleNamespace(busy=lambda:False))
        self.up=WikiUpdater(self.p);self.up.wait_idle=AsyncMock()
    async def asyncTearDown(self):await self.up.close()

    async def complete(self):
        if self.up.task:await self.up.task

    def package(self,meaning='新含义'):
        target=self.root/'new.db'
        if target.exists():target.unlink()
        import shutil;shutil.copyfile(self.db,target)
        with closing(sqlite3.connect(target)) as db,db:db.execute('UPDATE memes SET summary=? WHERE title=?',(meaning,'绷不住了'))
        data=gzip.compress(target.read_bytes(),mtime=0);return data,hashlib.sha256(data).hexdigest()

    async def test_atomic_offline_import_custom_survives_and_rollback(self):
        self.memory.import_entries([{'phrase':'自定义','meaning':'独立保留','usage':'只在这个场景'}])
        data,sha=self.package();await self.up.begin(offline=data,sha256=sha);await self.complete()
        self.assertEqual(self.up.state['job']['state'],'completed');self.assertEqual(self.wiki.exact('绷不住了')['meaning'],'新含义')
        self.assertEqual(self.memory.entries['自定义']['meaning'],'独立保留')
        old=self.up.state['versions'][0]['id'];await self.up.rollback(old,self.up.state['revision'])
        self.assertEqual(self.wiki.exact('绷不住了')['meaning'],'无法控制笑或情绪')
        with self.assertRaises(WikiConflict):await self.up.rollback(old,-1)

    async def test_bad_package_and_page_failure_keep_old_active(self):
        before=self.wiki.signature;data,sha=self.package()
        await self.up.begin(offline=data,sha256='bad');await self.complete()
        self.assertEqual(self.up.state['job']['state'],'failed');self.assertEqual(self.wiki.signature,before)
        self.up.fetch=AsyncMock(return_value={'items':[],'has_more':True,'offset':0})
        await self.up.begin();await self.complete()
        self.assertEqual(self.up.state['job']['state'],'failed');self.assertEqual(self.wiki.signature,before)

    async def test_running_start_is_idempotent_and_cancel_preserves_old(self):
        gate=asyncio.Event()
        async def fetch(*_):await gate.wait();return {'items':[],'has_more':False,'offset':0}
        self.up.fetch=fetch;a=await self.up.begin();b=await self.up.begin()
        self.assertEqual(a['job']['id'],b['job']['id'])
        await asyncio.sleep(.03);await self.up.cancel()
        self.assertEqual(self.up.state['job']['state'],'cancelled');self.assertIsNone(self.up.state['active'])
        self.assertTrue(self.wiki.exact('绷不住了'))

    async def test_source_revision_invalidates_understanding_and_late_result(self):
        await self.memory.lookup('绷不住了','a')
        self.generate.return_value=json.dumps({'intent':'借失控表达情绪','avoid':'认真求助不用','adaptations':['笑得不行'],'triggers':['笑死']})
        await self.memory.understand('绷不住了','a');self.assertIn('intent',self.memory.contextual('绷不住了')[0])
        data,sha=self.package();await self.up.begin(offline=data,sha256=sha);await self.complete()
        self.assertNotIn('intent',self.memory.contextual('绷不住了')[0])
        await self.memory.lookup('绷不住了','a')
        gate=asyncio.Event();started=asyncio.Event();answer=self.generate.return_value
        async def generate(*_):started.set();await gate.wait();return answer
        self.memory.generate=generate;t=asyncio.create_task(self.memory.understand('绷不住了','a'));await started.wait()
        data,sha=self.package('又一次修订');await self.up.begin(offline=data,sha256=sha);await self.complete();gate.set()
        await t;self.assertNotIn('intent',self.memory.contextual('绷不住了')[0])

    async def test_renamed_revision_invalidates_old_understanding_without_deleting_other_entries(self):
        await self.memory.lookup('绷不住了','a')
        old=self.wiki.exact('绷不住了');slug=old['wiki_slug']
        self.up.fetch=AsyncMock(side_effect=[{'items':[{'slug':slug,'updated_at':'v2'}],'has_more':False},
            {'item':{'slug':slug,'title':'绷不住新版','summary':'修订后的含义','usage':'新语境'}}])
        await self.up.begin();await self.complete()
        self.assertEqual(self.up.state['job']['state'],'completed')
        self.assertNotIn('绷不住了',self.wiki.local_entries)
        self.assertEqual(self.wiki.exact('绷不住新版')['meaning'],'修订后的含义')
        self.assertFalse(any(r['phrase']=='绷不住了' for r in self.memory.contextual('绷不住了')))
        self.assertEqual(self.up.state['job']['updated'],1)

    async def test_mismatched_detail_cannot_replace_another_entry(self):
        before=self.wiki.signature
        self.up.fetch=AsyncMock(side_effect=[{'items':[{'slug':'wanted'}],'has_more':False},
            {'item':{'slug':'unrelated','title':'绷不住了','summary':'错误详情'}}])
        await self.up.begin();await self.complete()
        self.assertEqual(self.up.state['job']['state'],'failed');self.assertEqual(self.wiki.signature,before)

    async def test_chat_queries_never_rebuild_dictionary(self):
        with patch.object(self.wiki,'refresh',side_effect=AssertionError('chat rebuilt full index')):
            self.assertTrue(self.memory.contextual('绷不住了'))
            self.assertTrue(self.wiki.exact('绷不住了'));self.assertTrue(self.wiki.match('绷不住了'))

    async def test_only_current_and_two_prior_successful_versions_are_retained(self):
        for n in range(4):
            data,sha=self.package('新义'+str(n));await self.up.begin(offline=data,sha256=sha);await self.complete()
        self.assertEqual(len(self.up.state['versions']),3)
        self.assertEqual(len(list(self.up.root.glob('*.db'))),3)

    async def test_missing_list_entry_is_not_deleted_and_revision_is_fetched(self):
        self.up.fetch=AsyncMock(side_effect=[{'items':[{'title':'新词','slug':'new','updated_at':'today'}],'offset':0,'has_more':False},
            {'item':{'title':'新词','slug':'new','summary':'公开定义','content_html':'使用内容','tags':[]}}])
        await self.up.begin();await self.complete()
        self.assertEqual(self.up.state['job']['state'],'completed');self.assertTrue(self.wiki.exact('新词'));self.assertTrue(self.wiki.exact('绷不住了'))

    async def test_rate_limit_retry_budget_and_idle_recheck(self):
        class Response:
            def __init__(self,status):self.status=status;self.headers={'Retry-After':'7'}
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            def raise_for_status(self):
                if self.status!=200:raise __import__('aiohttp').ClientError('fixture')
            async def json(self):return {'items':[]}
        session=SimpleNamespace(get=Mock(side_effect=[Response(429),Response(503),Response(200)]))
        self.up.state['job']={}
        with patch('data.plugins.astrbot_plugin_proactive_agent.wiki_update.asyncio.sleep',new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.up.fetch(session,'https://example.invalid'),{'items':[]})
            self.assertEqual(session.get.call_count,3);self.assertEqual(self.up.wait_idle.await_count,6)
            self.assertEqual(sum(call.args==(7.,) for call in sleep.await_args_list),2)

    async def test_setting_compare_and_swap_and_failed_save_preserve_other_settings(self):
        from quart import Quart
        from data.plugins.astrbot_plugin_proactive_agent.wiki_update_api import api
        class Config(dict):
            def save_config(self):return not self.get('fail')
        cfg=Config(other_feature=True);self.p.config=cfg;self.p._cfg=cfg.get;self.p._wiki_updates=self.up;self.p._panel_lock=asyncio.Lock()
        app=Quart(__name__)
        async def endpoint():return await api(self.p)
        app.add_url_rule('/update',view_func=endpoint,methods=['GET','POST']);client=app.test_client()
        initial=(await (await client.get('/update')).get_json())['data']
        self.assertTrue(initial['automatic']);self.assertEqual(initial['time'],'04:00')
        body={'action':'settings','enabled':False,'time':'05:00','config_revision':initial['config_revision']}
        self.assertEqual((await client.post('/update',json=body)).status_code,200);self.assertTrue(cfg['other_feature'])
        self.assertEqual((await client.post('/update',json=body)).status_code,409)
        current=(await (await client.get('/update')).get_json())['data'];cfg['fail']=True
        body.update(enabled=True,time='06:00',config_revision=current['config_revision'])
        self.assertEqual((await client.post('/update',json=body)).status_code,500)
        self.assertFalse(cfg['wiki_update_enabled']);self.assertEqual(cfg['wiki_update_time'],'05:00')
