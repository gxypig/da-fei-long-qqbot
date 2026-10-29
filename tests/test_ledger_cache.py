import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,digest
from data.plugins.astrbot_plugin_proactive_agent.ledger_store import LedgerStore

class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.db'
        self.store=LedgerStore(self.path,hot_limit=8);await self.store.start()
    async def asyncTearDown(self):
        await self.store.close();self.tmp.cleanup()
    async def fill(self,count=60):
        for i in range(count):
            self.store.put(str(i),{'id':str(i),'state':'confirmed','origin':'p:GroupMessage:'+str(i%3),
                'cid':'c','at':i,'text':'content-'+str(i),'message_id':str(i)})
        await self.store.flush(str(count-1));await asyncio.sleep(0)
    async def test_eviction_preserves_cold_revision_history_and_restart(self):
        await self.fill();self.assertLessEqual(len(self.store.cache),8);self.assertEqual(len(self.store.rows),60)
        self.assertEqual(self.store.rows['0']['text'],'content-0')
        self.store.put('0',{**self.store.rows['0'],'text':'updated'})
        self.assertEqual(self.store.rows['0']['revision'],2);await self.store.flush('0')
        await self.store.close();self.store=LedgerStore(self.path,hot_limit=8);await self.store.start()
        self.assertEqual(self.store.rows['0']['text'],'updated');self.assertLessEqual(len(self.store.cache),8)
        found=[]
        async for row in self.store.iterate(states={'confirmed'}):found.append(row['id'])
        self.assertEqual(len(set(found)),60);self.assertLessEqual(len(self.store.cache),8)
    async def test_pending_capacity_and_unknown_receipt_survive_eviction_and_restart(self):
        await self.fill()
        self.store.put('uncertain',{'id':'uncertain','state':'unknown','origin':'p:FriendMessage:1','at':-1,'text':'may have sent'})
        await self.store.flush('uncertain');await self.fill(80)
        self.assertEqual(self.store.rows['uncertain']['state'],'unknown')
        await self.store.close();self.store=LedgerStore(self.path,hot_limit=8);await self.store.start()
        self.assertEqual(self.store.rows['uncertain']['state'],'unknown')
    async def test_async_cold_batch_does_not_overwrite_concurrent_new_revision(self):
        await self.fill();gate=asyncio.Event();release=asyncio.Event();original=self.store._read_many
        # Trigger a newer memory-first update while the old cold batch is in flight.
        async def old_read(fn,*args):
            old=fn(*args);gate.set();await release.wait();return old
        with patch('data.plugins.astrbot_plugin_proactive_agent.ledger_store.asyncio.to_thread',old_read):
            task=asyncio.create_task(self.store.fetch(['0']));await gate.wait()
            self.store.put('0',{'id':'0','at':0,'text':'latest','state':'confirmed'})
            release.set();rows=await task
        self.assertEqual(rows[0]['text'],'latest')
    async def test_recent_context_and_old_unsent_filter_ignore_hot_cache_boundary(self):
        await self.store.close();ledger=DeliveryLedger(self.path);ledger.store.hot_limit=8;await ledger.start()
        self.store=ledger.store
        messages=[{'role':'user','content':'question'},{'role':'assistant','content':'unsent'}]
        hashes=[digest(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))) for x in messages]
        ledger.store.put('legacy',{'state':'legacy_undelivered','origin':'old','cid':'c','position':1,
            'row_hash':hashes[1],'previous_hash':hashes[0],'text_hash':digest('unsent')})
        row={'id':'voice','state':'confirmed','origin':'old','cid':'c','at':time.time(),'text':'hello',
            'message_id':'v','attachments':[{'kind':'record','transcript':'hello'}]}
        ledger.store.put('voice',row);await self.fill()
        self.assertEqual(ledger.filter_history('old','c',messages),messages[:1])
        self.assertIn('hello',ledger.voice_context('old','c'))
        self.assertEqual(ledger.recent('old','c')[0]['id'],'voice')
        self.assertLessEqual(len(self.store.cache),8)
