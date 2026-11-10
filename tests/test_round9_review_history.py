import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.review_history import ReviewHistory,linked,delivery_state,observe_format
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer,bind_request,request_scope,ReviewUnavailable
from data.plugins.astrbot_plugin_proactive_agent.review_modes import bind
from types import SimpleNamespace

class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.history=ReviewHistory(Path(self.temp.name)/'history.db')
        await self.history.start();self.origin='test:FriendMessage:one'
        self.settings={'enabled':True,'mode':'full_review'};bind(self.settings)
        bind_request(self.origin,['触发上下文'],False,{'persona_status':'selected','persona_version':'v1'},self.settings)
    async def asyncTearDown(self):
        await self.history.close();self.temp.cleanup()
    def reviewer(self,outputs):
        generate=AsyncMock(side_effect=[json.dumps(r,ensure_ascii=False) for r in outputs]);r=ReplyReviewer(generate,lambda *_:None);r.history=self.history;return r
    async def flush(self):
        while self.history.store.pending:await self.history.store.flush(next(iter(self.history.store.pending)))
    async def test_original_rounds_and_exact_split_delivery_are_distinct(self):
        scope=request_scope(self.origin);scope['review_draft']={'original':'原稿。\n保持换行','formatted':'原稿\n保持换行'}
        r=self.reviewer([{'pass':False,'natural':False,'chat_fit':True,'reason':'多说','replacement':'好呀\n知道啦'}, {'reply':'好呀\n知道啦'},
                         {'pass':True,'natural':True,'chat_fit':True}])
        result=await r.review('原稿\n保持换行',origin=self.origin)
        id_=linked(self.origin);row=await self.history.detail(id_)
        self.assertEqual(row['original'],'原稿。\n保持换行');self.assertEqual(len(row['rounds']),2)
        self.assertEqual(row['approved'],result);self.assertTrue(row['ai_changed']);self.assertTrue(row['format_changed'])
        self.assertEqual(row['delivery_state'],'not_sent')
        for i,(text,state) in enumerate([('好呀','confirmed'),('知道啦','unknown')]):
            self.history.delivered({'review_id':id_,'id':str(i),'state':state,'text':text,'purpose':'answer'})
        row=await self.history.detail(id_);self.assertEqual(row['delivery_state'],'partial')
        self.history.delivered({'review_id':id_,'id':'1','state':'confirmed','text':'知道啦','purpose':'answer'})
        self.assertEqual((await self.history.detail(id_))['delivery_state'],'confirmed')
        await self.flush();listing=await self.history.list();self.assertEqual(len(listing['rows']),1)
        self.assertNotIn('original',listing['rows'][0]);self.assertNotIn('rounds',listing['rows'][0])
    async def test_reuse_and_cancellation_do_not_fabricate_approval(self):
        gate=asyncio.Event()
        async def generate(*_):await gate.wait();return '{"pass":true,"natural":true,"chat_fit":true}'
        r=ReplyReviewer(generate,lambda *_:None);r.history=self.history
        tasks=[asyncio.create_task(r.review('原文',origin=self.origin)) for _ in range(2)]
        await asyncio.sleep(.02);gate.set();await asyncio.gather(*tasks)
        self.assertEqual(len([k for k in self.history.store.rows if not k.startswith('__')]),1)
        r=self.reviewer([{'pass':False,'natural':False,'chat_fit':True,'reason':'不合适','replacement':'另一稿'}, {'reply':'另一稿'}, {'pass':False,'natural':False,'chat_fit':True,'reason':'不合适'}, {'reply':'第三稿'}, {'pass':False,'natural':False,'chat_fit':True,'reason':'不合适'}])
        with self.assertRaises(ReviewUnavailable):await r.review('坏例',origin=self.origin)
        row=await self.history.detail(linked(self.origin));self.assertEqual(row['result'],'failed');self.assertIsNone(row['approved']);self.assertEqual(len(row['rounds']),3)
    async def test_off_has_no_model_and_format_only_preserves_original(self):
        settings={'enabled':False,'mode':'off'};bind_request(self.origin,[],False,{},settings)
        r=self.reviewer([]);r.enabled=lambda:False
        self.assertEqual(await r.review('原文。',origin=self.origin),'原文。');r.generate.assert_not_called()
        plugin=SimpleNamespace(_review_history=self.history,review_settings=lambda:settings)
        observe_format(plugin,self.origin,'原文。','原文',{},'chat')
        row=await self.history.detail(linked(self.origin));self.assertEqual(row['result'],'format_only');self.assertFalse(row['ai_changed'])
        self.assertEqual(row['rounds'],[])
    async def test_disk_failure_is_nonblocking_visible_and_recovers(self):
        scope=request_scope(self.origin)
        with patch.object(self.history.store,'_write',side_effect=OSError('disk unavailable')):
            begin=time.monotonic();id_=self.history.begin('原文','原文',origin=self.origin,state={},mode='chat',settings=self.settings,scope=scope)
            self.assertLess(time.monotonic()-begin,.05)
            await asyncio.sleep(.08);self.assertTrue(self.history.store.pending)
        await self.flush();self.assertIsNotNone(await self.history.detail(id_))
    async def test_prediction_direct_records_only_format_without_natural_approval(self):
        settings={'enabled':True,'mode':'speed_first'};bind_request(self.origin,[],False,{},settings)
        plugin=SimpleNamespace(_review_history=self.history,review_settings=lambda:settings)
        observe_format(plugin,self.origin,'原文。','原文',{},'chat')
        self.assertIsNone(linked(self.origin))
        observe_format(plugin,self.origin,'原文','原文',{},'chat',final_unreviewed=True)
        row=await self.history.detail(linked(self.origin))
        self.assertEqual(row['original'],'原文。');self.assertEqual(row['result'],'format_only')
        self.assertEqual(row['rounds'],[]);self.assertFalse(row['ai_changed'])
    async def test_review_gets_actual_gallery_state(self):
        reviewer=self.reviewer([{'pass':True,'natural':True,'chat_fit':True}])
        await reviewer.review('这张还真没有',origin=self.origin,character={'gallery_result':'unmatched'})
        payload=reviewer.generate.await_args.args[1]
        self.assertEqual(payload['不可改变的事实']['图库结果'],'unmatched')
    async def test_stable_pagination_keyword_and_retention(self):
        self.history.store.cfg=lambda k,d:100 if k=='review_history_limit' else d
        for i in range(105):
            id_=self.history.begin('<img onerror=evil> '+str(i),'改稿',origin=self.origin,state={},mode='chat',settings=self.settings,scope={})
            self.history.update(id_,result='passed',approved='改稿',ai_changed=True)
        await self.flush();rows=[];cursor=None
        while True:
            data=await self.history.list(cursor=cursor,keyword='<img');rows+=data['rows'];cursor=data['next_cursor']
            if not cursor:break
        self.assertEqual(len(rows),100);self.assertEqual(len({r['review_id'] for r in rows}),100)
        self.assertIn('<img',rows[0]['original_summary'])
    async def test_restart_keeps_exact_evidence_not_guessed_originals(self):
        r=self.reviewer([{'pass':True,'natural':True,'chat_fit':True}]);await r.review('保留\n原文',origin=self.origin);id_=linked(self.origin)
        await self.history.close();self.history=ReviewHistory(Path(self.temp.name)/'history.db');await self.history.start()
        self.assertEqual((await self.history.detail(id_))['original'],'保留\n原文')
        self.assertIsNone(await self.history.detail('legacy-journal-id'))

    async def test_late_receipt_for_cold_record_reloads_exact_id(self):
        id_=self.history.begin('原文','原文',origin=self.origin,state={},mode='chat',settings=self.settings,scope={})
        self.history.update(id_,result='passed',approved='原文');await self.flush()
        self.history.store.rows.pop(id_)
        self.history.delivered({'review_id':id_,'id':'late','state':'sending','text':'原文'})
        self.history.delivered({'review_id':id_,'id':'late','state':'confirmed','text':'原文','message_id':'receipt'})
        await asyncio.gather(*(r['task'] for r in list(self.history.cold.values())))
        await self.flush();row=await self.history.detail(id_)
        self.assertEqual(row['delivery_state'],'confirmed');self.assertEqual(row['sends']['late']['message_id'],'receipt')

    async def test_cancelled_after_approval_is_filterable_not_sent(self):
        job=SimpleNamespace(id='cancel-me',source_id='1',sender='1',text='测试',event=None,state='cancelled',reason='session_reset')
        id_=self.history.begin('原稿','原稿',origin=self.origin,state={},mode='chat',settings=self.settings,scope={},job=job)
        self.history.update(id_,result='passed',approved='原稿');self.history.end_job(job);await self.flush()
        row=await self.history.detail(id_);self.assertEqual(row['delivery_state'],'cancelled')
        self.assertEqual(len((await self.history.list(result='failed'))['rows']),1)

    async def test_evicted_approval_still_records_cancellation_and_retention(self):
        job=SimpleNamespace(id='cold-job',source_id='1',sender='1',text='测试',event=None,state='cancelled',reason='reset')
        id_=self.history.begin('原稿','原稿',origin=self.origin,state={},mode='chat',settings=self.settings,scope={},job=job)
        self.history.update(id_,result='passed',approved='原稿');await self.flush();self.history.store.rows.pop(id_)
        self.history.end_job(job)
        await asyncio.gather(*(r['task'] for r in list(self.history.cold.values())))
        await self.flush();self.assertEqual((await self.history.detail(id_))['delivery_state'],'cancelled')
        self.history.update(id_,at=1);await self.flush();await asyncio.to_thread(self.history.store.prune)
        self.assertIsNone(await self.history.detail(id_))

    async def test_failed_attempt_then_confirmed_retry_and_voice(self):
        id_=self.history.begin('说一句','说一句',origin=self.origin,state={},mode='chat',settings=self.settings,scope={})
        self.history.update(id_,result='passed',approved='说一句')
        self.history.delivered({'review_id':id_,'id':'first','state':'failed','text':'说一句'})
        self.history.delivered({'review_id':id_,'id':'second','state':'confirmed','text':'说一句','attachments':[{'kind':'record','transcript':'说一句'}]})
        row=await self.history.detail(id_);self.assertEqual(row['delivery_state'],'confirmed')
        self.assertEqual(row['sends']['second']['attachments'][0]['kind'],'record')

    async def test_startup_storage_failure_does_not_stop_chat(self):
        other=ReviewHistory(Path(self.temp.name)/'initial-failure.db')
        with patch.object(other.store,'_load',side_effect=OSError('offline')):await other.start()
        self.assertFalse(other.ready);self.assertIsNotNone(other.recovery)
        self.assertIsNone(other.begin('原文','原文',origin=self.origin,state={},mode='chat',settings=self.settings,scope={}))
        await other.start();self.assertTrue(other.ready)
        await other.close()

    async def test_credentials_and_hidden_analysis_are_not_saved(self):
        secret='sk-'+'x'*30
        id_=self.history.begin('原文 '+secret,'原文 '+secret,origin=self.origin,state={},mode='chat',settings=self.settings,scope={})
        self.history.round(id_,'正文',{'pass':False,'reason':'Bearer '+secret,'replacement':'<think>内部测试分析</think>正文','reasoning_content':'不保存这个字段','dimensions':{'reasoning_content':'不保存这个字段'}},1)
        await self.flush();row=await self.history.detail(id_);body=json.dumps(row,ensure_ascii=False)
        self.assertNotIn(secret,body);self.assertNotIn('内部测试分析',body);self.assertNotIn('不保存这个字段',body)
        self.assertTrue(row['redacted'])
        from data.plugins.astrbot_plugin_proactive_agent.review_history import sanitize
        self.assertNotIn('未闭合的内部分析',sanitize('<think>未闭合的内部分析'))
