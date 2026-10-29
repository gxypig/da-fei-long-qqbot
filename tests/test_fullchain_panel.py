import asyncio
import copy
from pathlib import Path
import tempfile
from temp_support import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from quart import Quart
from test_maintenance import Main
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.fullchain_panel import register_fullchain_panel,FEATURES
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
from data.plugins.astrbot_plugin_proactive_agent.ai_profiles import AIProfiles,identity


class Config(dict):
    def save_config(self):self.saved=copy.deepcopy(dict(self))


class PanelContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Quart(__name__);self.config=Config({**FEATURES,'private_prompt':'keep','reply_queue_concurrency':1})
        self.queue=ReplyQueue(self.config.get)
        self.profiles=AIProfiles(Path(self.temp.name)/'profiles.db',self.config.get,busy=lambda:True)
        await self.profiles.start();self.addAsyncCleanup(self.profiles.close)
        def register(route,handler,methods,desc):self.app.add_url_rule('/'+route.rsplit('/',1)[-1],view_func=handler,methods=methods)
        self.plugin=SimpleNamespace(config=self.config,_cfg=self.config.get,_reply_queue=self.queue,_ai_profiles=self.profiles,
            _live_journal=SimpleNamespace(delta=lambda *a:{'rows':[],'cursor':0,'instance':'fixture'}),
            _work=SimpleNamespace(busy=lambda:True),context=SimpleNamespace(register_web_api=register,get_using_provider_async=AsyncMock()))
        register_fullchain_panel(self.plugin);self.client=self.app.test_client()

    async def get(self,path):return (await (await self.client.get('/'+path)).get_json())['data']

    async def test_settings_cas_type_and_failed_disk_restore(self):
        body=await self.get('panel_fullchain_settings');body['values']['image_memory_enabled']=True
        response=await self.client.post('/panel_fullchain_settings',json=body);self.assertEqual(response.status_code,200)
        self.assertEqual(self.config.saved['private_prompt'],'keep')
        self.assertEqual((await self.client.post('/panel_fullchain_settings',json=body)).status_code,409)
        body=await self.get('panel_fullchain_settings');body['values']['reply_queue_concurrency']=True
        self.assertEqual((await self.client.post('/panel_fullchain_settings',json=body)).status_code,400)
        body=await self.get('panel_fullchain_settings');body['values']['image_memory_enabled']=False
        with patch.object(self.config,'save_config',side_effect=OSError('full')):
            self.assertEqual((await self.client.post('/panel_fullchain_settings',json=body)).status_code,500)
        self.assertTrue(self.config['image_memory_enabled'])

    async def test_partial_feature_updates_do_not_overwrite_other_editors(self):
        before=await self.get('panel_fullchain_settings')
        body={'values':{'ai_probe_enabled':True},'field_revisions':before['field_revisions']}
        other={'values':{'image_memory_enabled':True},'field_revisions':before['field_revisions']}
        self.assertEqual((await self.client.post('/panel_fullchain_settings',json=body)).status_code,200)
        self.assertEqual((await self.client.post('/panel_fullchain_settings',json=other)).status_code,200)
        self.assertTrue(self.config['ai_probe_enabled']);self.assertTrue(self.config['image_memory_enabled'])
        self.assertEqual((await self.client.post('/panel_fullchain_settings',json=body)).status_code,409)
        self.assertFalse(self.config['ai_profiles_enabled'])

    async def test_queue_conflicts_missing_body_unchanged_and_real_move(self):
        jobs=[self.queue.create(event(origin='p:GroupMessage:'+str(i),id_=str(i))) for i in range(3)]
        for job in jobs:self.queue.submit(job)
        snap=await self.get('panel_reply_queue')
        response=await self.client.get('/panel_reply_queue',query_string={'version':snap['version'],'instance':snap['instance']})
        self.assertTrue((await response.get_json())['data']['unchanged'])
        body={'instance':snap['instance'],'version':snap['version'],'request_id':'panel-move','action':'move','job_id':jobs[2].id,'before':jobs[1].id}
        self.assertEqual((await self.client.post('/panel_reply_queue_action',json=body)).status_code,200)
        self.assertEqual(self.queue.snapshot()['pending'][0]['id'],jobs[2].id)
        self.assertEqual((await self.client.post('/panel_reply_queue_action',json={**body,'request_id':'stale-request'})).status_code,409)
        self.assertEqual((await self.client.post('/panel_reply_queue_action')).status_code,400)
        self.queue.close()

    async def test_profiles_durable_idempotent_and_restart_instance_invalid(self):
        snap=await self.get('panel_ai_profiles')
        body={'instance':snap['instance'],'version':snap['version'],'request_id':'confirm-panel','action':'confirm','platform':'qq','account':'123'}
        self.assertEqual((await self.client.post('/panel_ai_profile_action',json=body)).status_code,200)
        self.assertTrue((await (await self.client.post('/panel_ai_profile_action',json=body)).get_json())['data']['duplicate'])
        restored=AIProfiles(Path(self.temp.name)/'profiles.db',self.config.get,busy=lambda:True)
        await restored.start();self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.store.rows[identity('qq','123')]['state'],'manual_confirmed')
        self.assertNotEqual(restored.instance,body['instance'])
        self.assertEqual((await self.client.post('/panel_ai_profile_action',json=None)).status_code,400)

    async def test_busy_probe_does_not_call_model_and_live_get_is_pure(self):
        response=await self.client.post('/panel_model_probe',json={})
        self.assertEqual((await response.get_json())['data']['state'],'deferred')
        self.plugin.context.get_using_provider_async.assert_not_awaited()
        before=self.profiles.version
        for _ in range(3):await self.get('panel_ai_profiles');await self.get('panel_live_messages')
        self.assertEqual(before,self.profiles.version)

    async def test_actual_native_extension_rejects_unauthenticated(self):
        # Exercise AstrBot's real route dependency rather than a fake login
        # decorator. An unregistered handler must never be reached without auth.
        from fastapi import FastAPI
        from httpx import ASGITransport,AsyncClient
        from astrbot.dashboard.api.plugins import legacy_router
        from astrbot.dashboard.responses import ApiError
        from starlette.responses import JSONResponse
        app=FastAPI();app.include_router(legacy_router)
        @app.exception_handler(ApiError)
        async def error(request,exc):return JSONResponse({'status':'error','message':exc.message},status_code=exc.status_code)
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            for method,path in [('GET','panel_reply_queue'),('POST','panel_reply_queue_action'),('POST','panel_ai_profile_action')]:
                response=await client.request(method,'/api/plug/astrbot_plugin_proactive_agent/'+path)
                self.assertIn(response.status_code,(401,403))

    async def test_drain_stops_producers_and_flushes_without_replay(self):
        self.plugin._task=asyncio.create_task(asyncio.sleep(3600))
        self.plugin._fixed_task=asyncio.create_task(asyncio.sleep(3600))
        self.plugin._learning=SimpleNamespace(_persistence=SimpleNamespace(flush=AsyncMock()))
        response=await self.client.post('/panel_drain',json={})
        data=(await response.get_json())['data']
        self.assertTrue(data['drained']);self.assertFalse(data['replay'])
        self.assertTrue(self.plugin._task.cancelled());self.assertTrue(self.plugin._fixed_task.cancelled())
        self.plugin._learning._persistence.flush.assert_awaited_once()
