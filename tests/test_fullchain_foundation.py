import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue,QueueConflict,ReplyCancelled,current_job,error_fingerprint
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,receipt_status,install_delivery_transport,DeliveryUnconfirmed
from data.plugins.astrbot_plugin_proactive_agent.group_wake import group_wake_reason
from data.plugins.astrbot_plugin_proactive_agent.route_guard import bind_route,install_route_guard
from aiocqhttp import CQHttp
from astrbot.api.message_components import Plain

def event(origin='p:GroupMessage:1',sender='a',id_='1',text='你好'):
    extra={}
    return SimpleNamespace(unified_msg_origin=origin,message_obj=SimpleNamespace(message_id=id_),
        get_sender_id=lambda:sender,get_message_str=lambda:text,get_extra=lambda key:extra.get(key),
        set_extra=lambda k,v:extra.__setitem__(k,v),get_group_id=lambda:origin.rsplit(':',1)[-1],
        get_self_id=lambda:'self',get_platform_id=lambda:'p',get_messages=lambda:[Plain(text)])

class QueueTests(unittest.IsolatedAsyncioTestCase):
    def job(self,q,origin='p:GroupMessage:1',sender='a',id_='1',text='你好'):
        return q.create(event(origin,sender,id_,text))

    async def test_same_session_serial_does_not_block_other_session(self):
        q=ReplyQueue({}.get)
        a=self.job(q);b=self.job(q,id_='2');c=self.job(q,origin='p:FriendMessage:2',id_='3')
        for j in [a,b,c]:q.submit(j)
        self.assertEqual([x.state for x in [a,b,c]],['running','pending','running'])
        self.assertEqual(b.reason,'同一会话上一条仍在执行')
        q.finish(a,'confirmed');await q.wait(b)
        self.assertEqual(b.state,'running')

    async def test_real_reorder_forces_quotes_on_every_crossed_live_job(self):
        q=ReplyQueue({'reply_queue_concurrency':1}.get)
        jobs=[self.job(q,origin='p:GroupMessage:'+str(i),id_=str(i)) for i in range(4)]
        for j in jobs:q.submit(j)
        body={'instance':q.instance,'version':q.version,'request_id':'request-1','job_id':jobs[3].id,'action':'move','before':jobs[1].id}
        r=q.action(body);self.assertEqual([x['id'] for x in r['pending']],[jobs[i].id for i in [3,1,2]])
        self.assertTrue(all(j.force_quote for j in jobs[1:]))
        self.assertTrue(q.action(body)['duplicate'])
        with self.assertRaises(QueueConflict):q.action({**body,'action':'delete'})
        q.finish(jobs[0],'confirmed');self.assertEqual(jobs[3].state,'running')

    async def test_delete_only_current_request_forces_following_same_session(self):
        q=ReplyQueue({'reply_queue_concurrency':1}.get)
        a,b,c,d=[self.job(q,origin='p:GroupMessage:'+g,id_=str(i)) for i,g in enumerate(['0','1','1','2'])]
        for j in [a,b,c,d]:q.submit(j)
        q.action({'instance':q.instance,'version':q.version,'request_id':'delete-1','job_id':b.id,'action':'delete'})
        self.assertEqual(b.state,'cancelled');self.assertTrue(c.force_quote);self.assertFalse(d.force_quote)
        self.assertEqual(b.sources[0]['text'],'你好')
        with self.assertRaises(ReplyCancelled):await q.wait(b)

    async def test_conflicts_starts_restart_and_snapshot_is_read_only(self):
        q=ReplyQueue({'reply_queue_concurrency':1}.get);a,b=[self.job(q,id_=str(i)) for i in range(2)]
        q.submit(a);q.submit(b)
        version=q.version
        self.assertTrue(q.snapshot(version,q.instance)['unchanged']);self.assertEqual(q.version,version)
        body={'instance':q.instance,'version':version,'request_id':'delete-2','job_id':b.id,'action':'delete'}
        q.finish(a,'confirmed')
        with self.assertRaises(QueueConflict):q.action(body)
        with self.assertRaises(QueueConflict):ReplyQueue({}.get).action(body)

    async def test_merge_keeps_sources_and_manual_jobs_are_protected(self):
        q=ReplyQueue({'reply_queue_concurrency':1}.get)
        a=self.job(q,id_='a');q.submit(a)
        b=self.job(q,id_='b');q.submit(b,mergeable=True)
        c=self.job(q,id_='c');q.submit(c,mergeable=True)
        self.assertEqual(c.state,'merged');self.assertEqual([x['id'] for x in b.sources],['b','c'])
        b.manually_adjusted=True
        d=self.job(q,id_='d');q.submit(d,mergeable=True);self.assertEqual(d.state,'pending')
        q.close();self.assertEqual(d.state,'cancelled')

    async def test_error_echo_is_account_neutral_and_keeps_new_questions(self):
        a='APIError: insufficient_quota request_id=ABC123'
        b='APIError: insufficient_quota request_id=DEF456'
        self.assertEqual(error_fingerprint(a),error_fingerprint(b))
        self.assertIsNone(error_fingerprint(b+'，怎么办？'))
        q=ReplyQueue({}.get)
        first=self.job(q,text=a);q.submit(first)
        repeated=self.job(q,text=b);q.submit(repeated);self.assertEqual(repeated.state,'merged')
        other=self.job(q,sender='b',text=b);q.submit(other);self.assertEqual(other.state,'pending')

class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=DeliveryLedger(Path(self.temp.name)/'ledger.db');await self.ledger.start()
        self.addAsyncCleanup(self.ledger.close)

    def test_receipt_requires_real_id(self):
        for value in [None,{}, {'status':'async','retcode':1},{'message_id':None},{'message_id':0},{'message_id':True}]:
            self.assertEqual(receipt_status(value)[0],'unknown')
        self.assertEqual(receipt_status({'status':'failed','retcode':1404})[0],'failed')
        self.assertEqual(receipt_status({'status':'ok','data':{'message_id':-123}}),('confirmed','-123'))

    async def test_only_confirmed_memory_and_history_dedup(self):
        q=ReplyQueue({}.get);j=q.create(event());j.cid='cid'
        x=self.ledger.begin(j.origin,j.cid,'发送内容',[],j)
        self.assertEqual(self.ledger.note(j.origin,'cid'),'')
        self.ledger.result(x,'confirmed','100')
        self.assertIn('发送内容',self.ledger.note(j.origin,'cid'))
        self.assertEqual(self.ledger.note(j.origin,'different'),'')
        self.ledger.history_saved(j);self.assertEqual(self.ledger.note(j.origin,'cid'),'')
        y=self.ledger.begin(j.origin,j.cid,'', [{'kind':'image'}],j);self.ledger.result(y,'failed')
        self.assertEqual(self.ledger.state(j),'partial')
        self.assertNotIn('image',self.ledger.note(j.origin,'cid'))

    async def test_restart_turns_inflight_into_unknown_without_transport(self):
        x=self.ledger.begin('p:FriendMessage:1','cid','待发送',[])
        await self.ledger.store.flush(x);await self.ledger.close()
        self.ledger=DeliveryLedger(Path(self.temp.name)/'ledger.db');await self.ledger.start()
        self.assertEqual(self.ledger.store.rows[x]['state'],'unknown')
        self.assertEqual(self.ledger.note('p:FriendMessage:1','cid'),'')
        await self.ledger.close()

    async def test_transport_failure_unknown_and_success_are_distinct(self):
        bot=object();p=SimpleNamespace(_delivery=self.ledger,_platform=lambda:SimpleNamespace(bot=bot),_cfg={'platform_id':'p'}.get,
            context=SimpleNamespace(conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='cid'))))
        original=AsyncMock(side_effect=[{'retcode':1404,'status':'failed'},None,{'message_id':9}])
        with patch.object(CQHttp,'call_action',original):
            restore=install_delivery_transport(p)
            try:
                for _ in range(2):
                    with self.assertRaises(DeliveryUnconfirmed):await CQHttp.call_action(bot,'send_private_msg',user_id='1',message=[{'type':'text','data':{'text':'本龙来了'}}])
                await CQHttp.call_action(bot,'send_private_msg',user_id='1',message=[{'type':'text','data':{'text':'这次成功'}}])
            finally:restore()
        self.assertEqual(original.await_count,3)
        rows=[r for r in self.ledger.store.rows.values() if 'state' in r]
        self.assertEqual([r['state'] for r in rows],['failed','unknown','confirmed'])
        self.assertNotIn('本龙来了',self.ledger.note('p:FriendMessage:1','cid'))

    async def test_storage_retries_do_not_repeat_delivery(self):
        x=self.ledger.begin('p:FriendMessage:1','cid','只发一次',[]);await self.ledger.store.flush(x)
        original=self.ledger.store._write;attempts=[]
        def flaky(batch):
            attempts.append(1)
            if len(attempts)==1:raise OSError('busy')
            return original(batch)
        with patch.object(self.ledger.store,'_write',flaky):
            self.ledger.result(x,'confirmed','101')
            self.assertIn('只发一次',self.ledger.note('p:FriendMessage:1','cid'))
            await self.ledger.store.flush(x)
        self.assertGreaterEqual(len(attempts),2);self.assertEqual(self.ledger.stats['confirmed'],1)
        self.assertGreater(self.ledger.store.stats['failures'],0)

    async def test_forced_quote_all_components_even_when_auto_quote_off(self):
        q=ReplyQueue({}.get);j=q.create(event());j.force_quote=True
        bot=object();p=SimpleNamespace(_platform=lambda:SimpleNamespace(bot=bot),_cfg={'platform_id':'p','auto_quote_enabled':False}.get)
        send=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',send):
            restore=install_route_guard(p);token=current_job.set(j);bind_route(j.origin,j.sender,j.source_id,True)
            try:
                for message in [[{'type':'text','data':{'text':'回复'}}],[{'type':'image','data':{'file':'base64://abc'}}]]:
                    await CQHttp.call_action(bot,'send_group_msg',group_id='1',message=message)
            finally:current_job.reset(token);restore()
        for call in send.call_args_list:self.assertEqual(call.kwargs['message'][0],{'type':'reply','data':{'id':'1'}})

class WakeBoundaryTests(unittest.TestCase):
    def test_latin_tokens_not_substrings(self):
        cfg={'platform_id':'p','group_wake_words':'ai,gpt,大肥龙'}.get
        for text in ['train','painting','gptish']:
            self.assertIsNone(group_wake_reason(event(text=text),{},cfg))
        for text in ['这个ai真会说','GPT，过来','你好，大肥龙']:
            self.assertEqual(group_wake_reason(event(text=text),{},cfg),'keyword')
