"""Round-two acceptance: real local stores, mocked network/platform only."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.idle_learning import IdleLearning
from data.plugins.astrbot_plugin_proactive_agent.image_memory import ImageMemory
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger


class LearningAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.cfg={'latency_optimization_enabled':True,'idle_learning_enabled':True,'style_learning_enabled':True}
        self.memory=SimpleNamespace(_detect=AsyncMock(return_value=['测试梗']),lookup=AsyncMock(return_value={'status':'learned','entry':{'meaning':'测试'}}),_retry_after={})
        self.s=IdleLearning(Path(self.temp.name)/'learning.db',self.cfg.get,self.memory,AsyncMock(),SimpleNamespace(),lambda *a:None,lambda:False)
        self.s._idle_since=time.monotonic()-3600
        self.addAsyncCleanup(self.s.close)

    async def test_retention_is_not_capacity_and_cursor_traverses_every_record(self):
        for i in range(2017):self.s.record_detail('meme',{'phrase':str(i),'status':'learned'},time.time()+86400)
        self.assertEqual(self.s.snapshot()['records'],2000)
        self.assertEqual(self.s.snapshot()['stats']['knowledge_added'],2017)
        found=[];cursor=None
        while True:
            page=self.s.details(cursor=cursor,limit=100);found.extend(r['id'] for r in page['events'])
            cursor=page['next_events_cursor']
            if cursor is None:break
        self.assertEqual(len(found),2000);self.assertEqual(len(set(found)),2000);self.assertEqual(found,sorted(found,reverse=True))
        self.assertTrue(self.s.enqueue_meme('测试梗',[],'p:FriendMessage:1'))
        job=dict(self.s._db.execute('SELECT * FROM jobs').fetchone());await self.s._run_job(job)
        self.assertEqual(self.s.snapshot()['stats']['knowledge_added'],2018)
        self.assertEqual(self.s.snapshot()['records'],2000)
        self.assertFalse(self.s.snapshot()['historical_metrics_incomplete'])

    async def test_transient_retry_exhaustion_and_manual_retry_keep_one_processed_task(self):
        self.memory.lookup.return_value={'status':'unverified','retryable':True,'error':'network'}
        self.s.enqueue_meme('测试梗',[],'p:FriendMessage:1')
        for attempt in range(3):
            job=dict(self.s._db.execute('SELECT * FROM jobs').fetchone());await self.s._run_job(job)
            self.assertEqual(self.s._db.execute('SELECT status FROM jobs').fetchone()[0],'failed' if attempt==2 else 'pending')
        self.assertEqual(self.s.snapshot()['stats']['processed'],1)
        await self.s.retry(job['id']);self.memory.lookup.return_value={'status':'learned','entry':{'meaning':'恢复后核实'}}
        job=dict(self.s._db.execute('SELECT * FROM jobs').fetchone());await self.s._run_job(job)
        self.assertEqual(self.s.snapshot()['pending'],0);self.assertEqual(self.s.snapshot()['failed_jobs'],0)
        self.assertEqual(self.s.snapshot()['stats']['processed'],1);self.assertEqual(self.s.snapshot()['stats']['knowledge_added'],1)

    async def test_review_and_release_reports_remain_independent(self):
        self.s._report('published',report_type='release_validation',version=1)
        self.s._report('reviewed',report_type='review',category='none')
        snap=self.s.snapshot()
        self.assertEqual(snap['last_release_validation']['status'],'published')
        self.assertEqual(snap['last_review']['status'],'reviewed')


class ImageRecoveryAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_restart_recovers_local_and_platform_original_and_persistent_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=DeliveryLedger(root/'ledger.db');await ledger.start()
            self.addAsyncCleanup(ledger.close)
            p=SimpleNamespace(_cfg={'image_memory_enabled':True}.get,_delivery=ledger,_work=SimpleNamespace(busy=lambda:True),_data_dir=root)
            service=ImageMemory(p,root/'images');await service.start()
            raw=b'\x89PNG\r\n\x1a\nround2-original';ref='base64://'+base64.b64encode(raw).decode()
            attachment=await service.describe_transport({'type':'image','data':{'file':ref}})
            local=ledger.begin('p:FriendMessage:1','cid','',[attachment]);ledger.result(local,'confirmed','local')
            remote=ledger.begin('p:FriendMessage:1','cid','',[{'kind':'image','semantic_status':'awaiting_platform_original'}]);ledger.result(remote,'confirmed','remote')
            service.store.put('task:'+attachment['content_hash'],{'status':'pending','origin':'p:FriendMessage:1','attempts':2,'retry_at':time.time()+60})
            await service.close()
            service=ImageMemory(p,root/'images');await service.start();self.addAsyncCleanup(service.close)
            self.assertIn(attachment['content_hash'],service.pending);self.assertIn('receipt:'+remote,service.pending)
            self.assertEqual(service.retries[attachment['content_hash']][0],2)
            Path(service.store.rows[attachment['content_hash']]['path']).unlink()
            service.pending.clear();service.recover_confirmed();self.assertIn('receipt:'+local,service.pending)
            p._call_onebot=AsyncMock(return_value={'message':[{'type':'image','data':{'file':ref}}]})
            await service._platform_original(local);await service._platform_original(remote)
            self.assertTrue(Path(service.store.rows[attachment['content_hash']]['path']).is_file())
            self.assertEqual(ledger.store.rows[remote]['attachments'][0]['content_hash'],attachment['content_hash'])
            self.assertNotIn('semantic',service.store.rows[attachment['content_hash']])
            await service.close();await ledger.close()
