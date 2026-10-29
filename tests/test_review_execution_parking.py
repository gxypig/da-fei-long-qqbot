import asyncio
import unittest
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue,current_job
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer


class ReviewExecutionParking(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waiting_review_does_not_occupy_execution_or_call_model(self):
        queue=ReplyQueue({'reply_queue_concurrency':1,'reply_inflight_limit':2}.get)
        entered=asyncio.Event();calls=[];jobs=[]
        async def generate(*_):calls.append(True);return '{}'
        reviewer=ReplyReviewer(generate,lambda *_:None)
        for _ in range(3):await reviewer._slots.acquire()
        async def owner():
            job=queue.create(event(origin='p:GroupMessage:1'));jobs.append(job)
            queue.submit(job);await queue.wait(job);token=current_job.set(job)
            try:
                entered.set()
                await reviewer.review('听着呢',origin=job.origin,character={'persona_status':'selected'})
            finally:current_job.reset(token)
        task=asyncio.create_task(owner())
        try:
            await entered.wait();await asyncio.sleep(0)
            other=queue.create(event(origin='p:GroupMessage:2',id_='other'));queue.submit(other)
            self.assertEqual(other.state,'running')
            queue.finish(other,'confirmed')
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(jobs[0].state,'cancelled');self.assertFalse(calls)
            self.assertFalse(queue.active);self.assertFalse(queue.inflight)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            for _ in range(3):reviewer._slots.release()

    async def test_review_child_does_not_release_its_parents_execution_slot(self):
        queue=ReplyQueue({'reply_queue_concurrency':1}.get)
        job=queue.create(event());queue.submit(job);token=current_job.set(job)
        async def generate(*_):
            self.assertIn(job.id,queue.active)
            self.assertEqual(job.state,'running')
            return '{"pass":true,"natural":true,"chat_fit":true}'
        reviewer=ReplyReviewer(generate,lambda *_:None)
        try:
            result=await asyncio.create_task(reviewer.review('嗯',origin=job.origin,character={'persona_status':'selected'}))
            self.assertEqual(result,'嗯');self.assertEqual(job.state,'running')
        finally:current_job.reset(token);queue.finish(job,'confirmed')

    async def test_saturated_review_slots_cannot_block_completed_review_resumption(self):
        queue=ReplyQueue({'reply_queue_concurrency':4,'reply_inflight_limit':8}.get)
        release=asyncio.Event();first_three=asyncio.Event();submitted=asyncio.Event()
        calls=0;count=0;seen=[]
        async def generate(*_):
            nonlocal calls
            job=current_job.get()
            # This is the existing model-wait parking contract. Review's own
            # semaphore must not be held while waiting to regain execution.
            async with queue.parked(job,'model_wait'):
                calls+=1
                if calls==3:first_three.set()
                await release.wait()
            return '{"pass":true,"natural":true,"chat_fit":true}'
        reviewer=ReplyReviewer(generate,lambda *_:None,timeout=.3)
        async def answer(index):
            nonlocal count
            job=queue.create(event(origin=f'p:GroupMessage:{index+1}',id_=str(index)))
            queue.submit(job);seen.append(job);count+=1
            if count==8:submitted.set()
            token=current_job.set(job)
            try:
                await queue.wait(job)
                result=await reviewer.review('嗯，听着呢',origin=job.origin,character={'persona_status':'selected'})
                self.assertEqual(job.state,'running')
                queue.finish(job,'confirmed')
                return result
            except BaseException:
                queue.finish(job,'failed');raise
            finally:current_job.reset(token)
        tasks=[asyncio.create_task(answer(i)) for i in range(8)]
        try:
            await asyncio.wait_for(first_three.wait(),.5)
            await asyncio.wait_for(submitted.wait(),.5)
            release.set()
            results=await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),1)
            self.assertEqual([type(r).__name__ for r in results if isinstance(r,BaseException)],[])
            self.assertEqual(results,['嗯，听着呢']*8)
            self.assertEqual(calls,8)
            self.assertFalse(queue.active);self.assertFalse(queue.inflight);self.assertFalse(queue.pending)
        finally:
            release.set()
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
