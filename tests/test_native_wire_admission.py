"""Actual AstrBot/OpenAI SDK, fake HTTP transport only; no account or model network."""
import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_maintenance import ROOT
import httpx
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.provider.sources import request_retry
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkRuntime,install_work_runtime

class NativeWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg={'latency_optimization_enabled':True,'model_concurrency':2,'reply_hedge_after_seconds':0}
        self.runtime=WorkRuntime(self.cfg.get,lambda *_:None)
        self.plugin=SimpleNamespace(_work=self.runtime,_cfg=self.cfg.get,_audit=lambda *_:None)
        self.restore=install_work_runtime(self.plugin);self.clients=[]
    async def asyncTearDown(self):
        self.restore();await self.runtime.close()
        for client in self.clients:await client.aclose()
    def provider(self,handler):
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler));self.clients.append(client)
        with patch.object(ProviderOpenAIOfficial,'_create_http_client',return_value=client):
            return ProviderOpenAIOfficial({'id':'offline','type':'openai_chat_completion','provider_source_id':'offline',
                'key':['synthetic-key'],'model':'same-offline-model','api_base':'https://offline.invalid/v1'}, {})
    def answer(self,text='测试答复'):
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':1,'model':'same-offline-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14}})
    async def test_native_review_backup_keeps_payload_and_reports_verdict_before_return(self):
        from test_reply_pipeline import plugin
        from test_work_scheduler import Event
        from unittest.mock import AsyncMock
        from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer
        self.cfg['reply_hedge_after_seconds']=15
        entered=asyncio.Event();cancelled=asyncio.Event();payloads=[];notices=[]
        async def handler(request):
            payloads.append(json.loads(request.content))
            if len(payloads)==1:
                entered.set()
                try:await asyncio.Future()
                finally:cancelled.set()
            return self.answer('{"pass":true,"natural":true,"chat_fit":true}')
        async def timer(_):await entered.wait()
        self.runtime._hedge_sleep=timer
        p=plugin();p.config['review_low_latency']=False;p._work=self.runtime
        provider=self.provider(handler)
        p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        reviewer=ReplyReviewer(p._generate_json,lambda *_:None)
        with self.runtime.reply_work(Event()):
            result=await asyncio.wait_for(reviewer.review('奶茶店先笑了',origin='offline',character={'persona_status':'selected'},
                on_verdict=lambda:notices.append('arrived')),5)
        self.assertEqual(result,'奶茶店先笑了');self.assertEqual(notices,['arrived'])
        self.assertEqual(len(payloads),2);self.assertEqual(payloads[0],payloads[1])
        self.assertNotIn('QQBOT_CASUAL_TAIL_GUARD',json.dumps(payloads))
        self.assertTrue(cancelled.is_set());self.assertFalse(self.runtime._hedge_tasks)
        self.assertEqual(self.runtime.scheduler.status()['running'],0)
        self.assertEqual(self.runtime.status()['hedge']['won'],1)
    async def test_admin_sdk_deadline_does_not_change_chat_timeout_or_model(self):
        from data.plugins.astrbot_plugin_proactive_agent.provider_deadline import with_deadline
        seen=[]
        def request(r):
            seen.append((json.loads(r.content)['model'],r.extensions['timeout']['read']))
            return self.answer()
        ordinary=self.provider(request);admin=with_deadline(ordinary,600)
        await admin.text_chat(prompt='compile',request_max_retries=1)
        await ordinary.text_chat(prompt='chat',request_max_retries=1)
        self.assertEqual(seen,[('same-offline-model',600),('same-offline-model',120)])
        self.assertIs(admin.client._client,ordinary.client._client)
    async def test_real_sdk_retry_releases_slot_until_next_http_attempt(self):
        sleeping=asyncio.Event();release=asyncio.Event();calls=[];build=request_retry._build_retrying
        async def handler(request):
            payload=json.loads(request.content);text=payload['messages'][-1]['content'];calls.append(text)
            self.assertGreater(self.runtime.scheduler.status()['running'],0)
            if text=='retry-me' and calls.count(text)==1:return httpx.Response(503,json={'error':{'message':'offline temporary','type':'server_error'}})
            return self.answer()
        async def sleep(_):sleeping.set();await release.wait()
        def retrying(*args,**kwargs):
            result=build(*args,**kwargs);result.sleep=sleep;return result
        provider=self.provider(handler)
        with patch.object(request_retry,'_build_retrying',side_effect=retrying):
            task=asyncio.create_task(provider.text_chat(prompt='retry-me',request_max_retries=2))
            try:
                await asyncio.wait_for(sleeping.wait(),3)
                self.assertEqual(self.runtime.scheduler.status()['running'],0)
                other=await provider.text_chat(prompt='other',request_max_retries=1)
                self.assertEqual(other.completion_text,'测试答复');release.set();await task
                self.assertEqual(calls,['retry-me','other','retry-me'])
            finally:release.set();await asyncio.gather(task,return_exceptions=True)
    async def test_payload_preparation_is_before_model_admission(self):
        entered=asyncio.Event();release=asyncio.Event();original=ProviderOpenAIOfficial._prepare_chat_payload
        async def prepare(provider,*args,**kwargs):
            self.assertEqual(self.runtime.scheduler.status()['running'],0);entered.set();await release.wait()
            return await original(provider,*args,**kwargs)
        provider=self.provider(lambda request:self.answer())
        with patch.object(ProviderOpenAIOfficial,'_prepare_chat_payload',prepare):
            task=asyncio.create_task(provider.text_chat(prompt='hello'))
            try:await asyncio.wait_for(entered.wait(),3);release.set();result=await task
            finally:release.set();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(result.completion_text,'测试答复');self.assertEqual(self.runtime.scheduler.status()['running'],0)
    async def test_native_stream_complete_and_early_close_release_slots(self):
        chunk={'id':'c','object':'chat.completion.chunk','created':1,'model':'same-offline-model','choices':[{'index':0,'delta':{'role':'assistant','content':'好'},'finish_reason':None}]}
        final={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}
        body=''.join('data: '+json.dumps(c)+'\n\n' for c in (chunk,final))+'data: [DONE]\n\n'
        provider=self.provider(lambda request:httpx.Response(200,content=body,headers={'content-type':'text/event-stream'}))
        values=[v async for v in provider.text_chat_stream(prompt='hello')]
        self.assertEqual(values[-1].completion_text,'好');self.assertEqual(self.runtime.scheduler.status()['running'],0)
        iterator=provider.text_chat_stream(prompt='hello');await anext(iterator);await iterator.aclose()
        self.assertEqual(self.runtime.scheduler.status()['running'],0)
