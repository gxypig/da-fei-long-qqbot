import asyncio,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.meme_usage import MemeUsage,key

class Event:
    def __init__(self,origin='bot:GroupMessage:1'):
        self.unified_msg_origin=origin;self.extra={}
    def get_extra(self,k):return self.extra.get(k)
    def set_extra(self,k,v):self.extra[k]=v

class UsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'use.db'
        self.usage=MemeUsage(self.path,rng=lambda:0);await self.usage.start()
        self.rows=[{'phrase':'下次一定','meaning':'拖延承诺','usage':'又不想做','retrieval_kind':'context'}]
    async def asyncTearDown(self):
        await self.usage.close();self.temp.cleanup()
    def sent(self,event,rid='r1',job='j1',**kw):
        row={'id':rid,'job_id':job,'state':'confirmed','origin':event.unified_msg_origin,'text':'下次一定','meme_plan':event.get_extra('qqbot_meme_plan'),**kw}
        self.usage.delivery(row);return row
    async def test_direct_bypasses_sampling_but_serious_does_not(self):
        self.usage.rng=lambda:1;rows=[{**self.rows[0],'retrieval_kind':'direct'}]
        self.assertTrue(self.usage.choose(Event(),rows,'下次一定'))
        self.assertFalse(self.usage.choose(Event(),rows,'不要玩梗，下次一定'))

    async def test_persistent_stop_blocks_later_direct_and_active_opportunities(self):
        for kind in ('direct','context'):
            event=Event();event.set_extra('qqbot_character',{'mood':'stop'})
            self.assertFalse(self.usage.choose(event,[{**self.rows[0],'retrieval_kind':kind}],'下次一定'))
            self.assertEqual(event.get_extra('qqbot_meme_plan')['reason'],'serious_or_stop')
        other=Event('bot:GroupMessage:2')
        self.assertTrue(self.usage.choose(other,self.rows,'这个工作明天再做'))
    async def test_seeded_twenty_percent_and_once_per_request(self):
        count=0
        for i in range(100):
            self.usage.rng=lambda i=i:i/100;e=Event();result=self.usage.choose(e,self.rows,'又拖延了');count+=bool(result)
            self.usage.rng=lambda:1;self.assertEqual(self.usage.choose(e,self.rows,'又拖延了'),result)
        self.assertEqual(count,20)
    async def test_failed_unknown_and_optional_are_not_reply_counts(self):
        e=Event();self.usage.choose(e,self.rows,'又拖延了')
        for i,kw in enumerate([{'state':'failed'},{'state':'unknown'},{'optional':True},{'purpose':'error_notification'}]):self.sent(e,str(i),**kw)
        self.assertNotIn(key(e.unified_msg_origin),self.usage.store.rows)
    async def test_segments_are_one_reply_literal_is_not_claimed_semantic_use(self):
        e=Event();self.usage.choose(e,self.rows,'又拖延了');self.sent(e);self.sent(e,'r2');self.sent(e,'r2')
        row=self.usage.store.rows['reply:j1'];self.assertEqual(row['literal_matches'],['下次一定']);self.assertEqual(row['uses'],[])
        self.assertEqual(self.usage.store.rows[key(e.unified_msg_origin)]['sequence'],1)
    async def test_three_confirmed_gap_and_twenty_repeat(self):
        e=Event();self.usage.choose(e,self.rows,'又拖延了');self.sent(e)
        async def generate(*args):return {'uses':[{'phrase':'下次一定','evidence':'下次一定','fit':True,'reason':'再次拖延'}]}
        self.usage.generate=generate;await self.usage.assess(self.usage.store.rows['reply:j1'])
        other=[{**self.rows[0],'phrase':'明天再说'}]
        for i in range(3):
            self.assertFalse(self.usage.choose(Event(),other,'又拖延了'));self.sent(Event(),f'a{i}',f'a{i}',text='好')
        self.assertTrue(self.usage.choose(Event(),other,'又拖延了'))
        self.assertFalse(self.usage.choose(Event(),self.rows,'又拖延了'))
        for i in range(17):self.sent(Event(),f'b{i}',f'b{i}',text='好')
        self.assertTrue(self.usage.choose(Event(),self.rows,'又拖延了'))
    async def test_restart_reconciles_only_confirmed_outbox(self):
        e=Event();self.usage.choose(e,self.rows,'又拖延了');row=self.sent(e)
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
        ledger=DeliveryLedger(self.path.with_name('delivery.db'));await ledger.start()
        self.addAsyncCleanup(ledger.close)
        ledger.store.put(row['id'],row);await ledger.store.flush(row['id'])
        await self.usage.close();self.usage=MemeUsage(self.path);await self.usage.start(ledger)
        self.assertEqual(self.usage.store.rows[key(e.unified_msg_origin)]['sequence'],1)
        self.assertEqual(len(self.usage.page()['rows']),1)
        await ledger.close()
    async def test_inference_requires_exact_sent_evidence(self):
        e=Event();self.usage.choose(e,self.rows,'又拖延了');self.sent(e,text='承诺先存档')
        async def generate(*args):return {'uses':[{'phrase':'下次一定','evidence':'我会做到','fit':True}]}
        self.usage.generate=generate
        with self.assertRaises(ValueError):await self.usage.assess(self.usage.store.rows['reply:j1'])
        async def valid(*args):return {'uses':[{'phrase':'下次一定','evidence':'承诺先存档','fit':True,'reason':'改编拖延承诺'}]}
        self.usage.generate=valid;await self.usage.assess(self.usage.store.rows['reply:j1'])
        self.assertEqual(self.usage.store.rows['reply:j1']['evidence'][0]['mode'],'inferred')
    async def test_other_session_and_abandoned_draft_do_not_spend_gap(self):
        e=Event();self.usage.choose(e,self.rows,'又拖延了');self.usage.finished(e,'cancelled')
        self.assertTrue(self.usage.choose(Event(),self.rows,'又拖延了'))
        self.sent(e);self.assertTrue(self.usage.choose(Event('bot:FriendMessage:1'),self.rows,'又拖延了'))
