"""Actual native Runner await/cancel adapter and SDK; isolated mock HTTP only."""
import asyncio,unittest,json,httpx
from types import SimpleNamespace
import test_native_wire_admission as native
from test_fullchain_foundation import event
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue,current_job
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkRuntime,install_work_runtime

class NativeRunnerParking(unittest.IsolatedAsyncioTestCase):
    provider=native.NativeWireTests.provider
    answer=native.NativeWireTests.answer

    async def asyncSetUp(self):
        self.cfg={'latency_optimization_enabled':True,'model_concurrency':1,'reply_hedge_after_seconds':0,'reply_queue_concurrency':1,'reply_inflight_limit':2}
        self.queue=ReplyQueue(self.cfg.get);self.runtime=WorkRuntime(self.cfg.get,lambda *_:None)
        self.plugin=SimpleNamespace(_work=self.runtime,_reply_queue=self.queue,_cfg=self.cfg.get,_audit=lambda *_:None)
        self.restore=install_work_runtime(self.plugin);self.clients=[]
        self.runner=object.__new__(ToolLoopAgentRunner);self.runner._abort_signal=asyncio.Event()

    async def asyncTearDown(self):
        self.restore();await self.runtime.close()
        for client in self.clients:await client.aclose()

    async def test_parent_parks_before_native_runner_creates_provider_child(self):
        job=self.queue.create(event());self.queue.submit(job);token=current_job.set(job)
        async def http(request):
            self.assertEqual(job.state,'suspended')
            other=self.queue.create(event(origin='p:GroupMessage:2',id_='other'));self.queue.submit(other)
            self.assertEqual(other.state,'running');self.queue.finish(other,'confirmed')
            return self.answer()
        provider=self.provider(http)
        try:
            response=await self.runner._await_or_stop(provider.text_chat(prompt='合成输入',request_max_retries=1))
            self.assertEqual(response.completion_text,'测试答复');self.assertEqual(job.state,'running')
        finally:current_job.reset(token);self.queue.finish(job,'confirmed')

    async def _abort_case(self,cancel_owner):
        entered=asyncio.Event();closed=asyncio.Event();jobs=[]
        async def http(request):
            entered.set()
            try:await asyncio.Future()
            finally:closed.set()
        provider=self.provider(http)
        async def owner():
            job=self.queue.create(event());jobs.append(job);self.queue.submit(job);token=current_job.set(job)
            try:return await self.runner._await_or_stop(provider.text_chat(prompt='合成取消输入',request_max_retries=1))
            finally:current_job.reset(token);self.queue.finish(job,'cancelled')
        task=asyncio.create_task(owner())
        try:
            await asyncio.wait_for(entered.wait(),1)
            self.assertFalse(self.queue.active);self.assertEqual(len(self.queue.inflight),1)
            if cancel_owner:task.cancel()
            else:self.runner._abort_signal.set()
            result=await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),1)
            if cancel_owner:self.assertIsInstance(result[0],asyncio.CancelledError)
            else:self.assertIsNone(result[0])
            self.assertTrue(closed.is_set());self.assertFalse(self.queue.active);self.assertFalse(self.queue.inflight)
            self.assertEqual(self.runtime.scheduler.status()['running'],0)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_native_abort_joins_provider_before_owner_resumes(self):
        await self._abort_case(False)

    async def test_owner_cancellation_leaves_no_job_or_model_slot(self):
        await self._abort_case(True)

    async def test_detached_child_never_releases_its_parents_execution(self):
        job=self.queue.create(event());self.queue.submit(job);token=current_job.set(job)
        async def operation():
            self.assertEqual(job.state,'running');self.assertIn(job.id,self.queue.active)
            return 'child'
        try:
            self.assertEqual(await asyncio.create_task(self.runner._await_or_stop(operation())),'child')
            self.assertEqual(job.state,'running')
        finally:current_job.reset(token);self.queue.finish(job,'confirmed')

    async def test_disabled_runtime_preserves_native_await_behavior(self):
        self.cfg['latency_optimization_enabled']=False
        job=self.queue.create(event());self.queue.submit(job);token=current_job.set(job)
        async def operation():
            self.assertEqual(job.state,'running');return 'unchanged'
        try:self.assertEqual(await self.runner._await_or_stop(operation()),'unchanged')
        finally:current_job.reset(token);self.queue.finish(job,'confirmed')

    async def test_interleaved_unload_leaves_inactive_native_hook_as_passthrough(self):
        second=WorkRuntime(self.cfg.get,lambda *_:None)
        other=SimpleNamespace(_work=second,_reply_queue=self.queue,_cfg=self.cfg.get,_audit=lambda *_:None)
        restore_second=install_work_runtime(other)
        self.restore();self.restore=lambda:None
        job=self.queue.create(event());self.queue.submit(job);token=current_job.set(job)
        async def parked():self.assertEqual(job.state,'suspended');return 'active'
        async def passthrough():self.assertEqual(job.state,'running');return 'inactive'
        try:
            self.assertEqual(await self.runner._await_or_stop(parked()),'active')
            restore_second()
            self.assertEqual(await self.runner._await_or_stop(passthrough()),'inactive')
        finally:
            restore_second();await second.close()
            current_job.reset(token);self.queue.finish(job,'confirmed')

    async def test_native_runner_stream_chunks_and_close_can_cross_child_tasks(self):
        chunk={'id':'c','object':'chat.completion.chunk','created':1,'model':'same-offline-model','choices':[{'index':0,'delta':{'role':'assistant','content':'好'},'finish_reason':None}]}
        final={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}
        body=''.join('data: '+json.dumps(c)+'\n\n' for c in (chunk,final))+'data: [DONE]\n\n'
        provider=self.provider(lambda request:httpx.Response(200,content=body,headers={'content-type':'text/event-stream'}))
        job=self.queue.create(event());self.queue.submit(job);token=current_job.set(job)
        try:
            for early in (False,True):
                stream=provider.text_chat_stream(prompt='合成流式输入');values=[]
                try:
                    while True:
                        try:value=await self.runner._await_or_stop(anext(stream))
                        except StopAsyncIteration:break
                        values.append(value)
                        if early:break
                finally:await stream.aclose()
                self.assertTrue(values)
                if not early:self.assertEqual(values[-1].completion_text,'好')
                self.assertEqual(self.runtime.scheduler.status()['running'],0)
                self.assertEqual(job.state,'running')
        finally:current_job.reset(token);self.queue.finish(job,'confirmed')
