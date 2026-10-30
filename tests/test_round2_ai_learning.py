import asyncio
import json
from unittest.mock import AsyncMock
from test_ai_profiles import ProfileTests


class SemanticProfiles(ProfileTests):
    async def test_revoke_during_failed_analysis_cannot_restore_old_pending_state(self):
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import ModelNotReady
        for error in (ModelNotReady('initializing'),RuntimeError('temporary network error')):
            with self.subTest(error=type(error).__name__):
                entered=asyncio.Event();release=asyncio.Event()
                async def generate(*args):
                    entered.set();await release.wait();raise error
                self.service.generate=generate;self.observe('tool_calls: []');self.now+=6
                self.service.busy=lambda:False
                await asyncio.wait_for(entered.wait(),3)
                self.action('revoke');release.set()
                await asyncio.sleep(.05)
                row=self.service.store.rows[self.key]
                self.assertEqual(row['state'],'revoked');self.assertEqual(row['stage'],'revoked')
                self.assertNotIn(self.key,self.service.pending)
                self.assertEqual(row.get('analysis_attempts',0),0)

    async def test_model_initialization_wait_preserves_retry_budget_and_recovers(self):
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import ModelNotReady
        self.service.generate=AsyncMock(side_effect=ModelNotReady('initializing'))
        self.observe('tool_calls: []');self.now+=6;self.service.busy=lambda:False
        async with asyncio.timeout(3):
            while self.service.store.rows[self.key].get('stage')!='waiting_model':await asyncio.sleep(.01)
        row=self.service.store.rows[self.key]
        self.assertEqual(row.get('analysis_attempts',0),0)
        self.assertIn(self.key,self.service.pending)
        self.assertEqual(self.service.generate.await_count,1)
        self.service.generate.side_effect=None
        self.service.generate.return_value=json.dumps({'judgement_type':'insufficient','evidence':[],'counterevidence':[]})
        self.now+=6
        async with asyncio.timeout(3):
            while not self.service.store.rows[self.key].get('analyzed_at'):await asyncio.sleep(.01)
        self.assertFalse(self.service.store.rows[self.key]['analysis_pending'])
        self.assertEqual(self.service.store.rows[self.key]['analysis_attempts'],0)
        self.assertEqual(self.service.generate.await_count,2)

    async def test_restart_recovers_confirmed_probe_from_exact_delivery_without_extending_ttl(self):
        from types import SimpleNamespace
        from data.plugins.astrbot_plugin_proactive_agent.ai_profiles import AIProfiles
        await self.seed_suspect();p=await self.service.prepare_probe('qq','123','trigger','qq:GroupMessage:1',text='我这个号的程序自动回复')
        path=self.service.store.path;cfg=self.service.cfg;await self.service.close();sent_at=self.now+1;self.now+=100
        rows={'confirmed':{'id':'confirmed','state':'confirmed','origin':p['origin'],'source_id':'trigger','sender':'123',
            'at':sent_at,'confirmed_at':sent_at,'message_id':'qq-1','text':'你是手动打字还是程序自动回的？'},
            'other':{'id':'other','state':'confirmed','origin':'qq:GroupMessage:2','source_id':'trigger','sender':'123',
            'at':sent_at,'confirmed_at':sent_at,'message_id':'qq-2','text':'不属于这次试探'}}
        self.service=AIProfiles(path,cfg,clock=lambda:self.now,busy=lambda:True)
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
        ledger=DeliveryLedger(path.with_name('delivery.db'));await ledger.start()
        self.addAsyncCleanup(ledger.close)
        for key,row in rows.items():ledger.store.put(key,row)
        await self.service.start(ledger)
        outstanding=self.service.store.rows[self.key]['outstanding']
        self.assertTrue(outstanding['sent']);self.assertEqual(outstanding['expires_at'],sent_at+600)
        self.assertEqual(len(outstanding['receipts']),1);self.assertEqual(self.service.store.rows[self.key]['probes'][self.service.day()],1)
        self.now=sent_at+601
        self.assertEqual(self.service.snapshot()['rows'][0]['stage'],'probe_expired')
        self.assertFalse(self.service.snapshot()['rows'][0]['outstanding'])
        await ledger.close()

    async def test_final_rewrite_attachment_cross_group_and_ttl(self):
        await self.seed_suspect();p=await self.service.prepare_probe('qq','123','trigger','qq:GroupMessage:1',text='我这个号的程序自动回复')
        self.service.delivery(p,'你是手动回的还是程序在回？','confirmed','send-1','qq-1')
        self.service.delivery(p,'','confirmed','attachment','qq-2');self.service.end_turn(p)
        self.assertTrue(self.service.store.rows[self.key]['outstanding']['sent'])
        self.observe('我是机器人','qq:GroupMessage:2')
        self.assertIsNone(self.service.store.rows[self.key]['observations'][-1]['probe'])
        self.observe('我是机器人','qq:GroupMessage:1')
        self.assertEqual(self.service.store.rows[self.key]['observations'][-1]['probe'],p['id'])
        p=await self.service.prepare_probe('qq','123','next','qq:GroupMessage:1',text='我这个号的程序自动回复')
        self.service.delivery(p,p['question'],'confirmed');self.now+=601
        self.observe('我是机器人');self.assertIsNone(self.service.store.rows[self.key]['observations'][-1]['probe'])

    async def test_semantic_evidence_needs_three_rounds_two_types_and_no_strong_counter(self):
        for text in ['tool_calls: []','finish_reason: stop','system_prompt: token']:
            self.observe(text);self.now+=61
        ids=[x['id'] for x in self.service.store.rows[self.key]['observations']]
        evidence=[{'kind':'control_leak','message_ids':ids[:2],'strength':'strong','summary':'synthetic protocol evidence'},
            {'kind':'cross_turn_behavior','message_ids':ids[2:],'strength':'strong','summary':'synthetic independent behavior'}]
        data={'judgement_type':'ai','evidence':evidence,'counterevidence':[{'message_ids':[ids[0]],'strength':'strong','summary':'quoted protocol'}]}
        self.service.generate=AsyncMock(side_effect=lambda *a:json.dumps(data))
        await self.service.analyze(self.key,0);self.assertNotEqual(self.service.store.rows[self.key]['state'],'auto_confirmed')
        data['counterevidence']=[]
        await self.service.analyze(self.key,0);self.assertEqual(self.service.store.rows[self.key]['state'],'auto_confirmed')
        self.assertIn('弱鸡ai',self.service.reply_note('qq','123'))
        # The exact same evidence classified as ordinary automation cannot certify AI.
        data['judgement_type']='automation';await self.service.analyze(self.key,0)
        self.assertEqual(self.service.reply_note('qq','123'),'');self.assertEqual(self.service.source_label('qq','123'),'automation')

    async def test_split_messages_and_two_names_for_protocol_do_not_make_independent_rounds(self):
        for text in ['tool_calls: []','finish_reason: stop','system_prompt: token']:
            self.observe(text);self.now+=1
        ids=[x['id'] for x in self.service.store.rows[self.key]['observations']]
        data={'judgement_type':'ai','counterevidence':[],'evidence':[
            {'kind':'control_leak','message_ids':ids,'strength':'strong','summary':'protocol'},
            {'kind':'model_protocol','message_ids':ids,'strength':'strong','summary':'same protocol'}]}
        self.service.generate=AsyncMock(return_value=json.dumps(data));await self.service.analyze(self.key,0)
        row=self.service.store.rows[self.key]
        self.assertNotEqual(row['state'],'auto_confirmed');self.assertIn('1轮 / 1类',row['skip_reason'])

    async def test_account_cas_is_not_invalidated_by_other_account_message(self):
        self.observe('hello');version=self.service.store.rows[self.key]['account_version']
        self.service.observe('qq','456','hello','qq:GroupMessage:1','other')
        result=self.service.action({'instance':self.service.instance,'account_version':version,
            'action':'confirm','request_id':'account-cas-test','platform':'qq','account':'123'})
        self.assertTrue(any(r['account']=='123' and r['state']=='manual_confirmed' for r in result['rows']))

    async def test_concurrent_probe_reservation_is_one_and_durable(self):
        await self.seed_suspect()
        probes=await asyncio.gather(*(self.service.prepare_probe('qq','123',str(i),'qq:GroupMessage:'+str(i),text='我这个号的程序自动回复') for i in range(8)))
        self.assertEqual(sum(p is not None for p in probes),1)
        self.assertEqual(self.service.store.rows[self.key]['probes'][self.service.day()],1)

    async def test_background_links_rewritten_question_without_clearing_pending(self):
        await self.seed_suspect();self.service.generate=AsyncMock()
        p=await self.service.prepare_probe('qq','123','trigger','qq:GroupMessage:1',text='我这个号的程序自动回复')
        final='你这号背后到底是谁在敲字呢？'
        self.service.delivery(p,final,'confirmed','delivery-final','platform-final');self.service.end_turn(p)
        self.assertIsNotNone(self.service.store.rows[self.key]['outstanding'])
        self.now+=3;self.observe('现在是生成模型在接话');correct=self.service.store.rows[self.key]['observations'][-1]['id']
        self.observe('隔壁群无关发言','qq:GroupMessage:2');wrong=self.service.store.rows[self.key]['observations'][-1]['id']
        self.service.generate.return_value=json.dumps({'judgement_type':'insufficient','evidence':[],'counterevidence':[],
            'probe_links':[{'probe_id':p['id'],'question_quote':final,'answer_message_ids':[correct,wrong]}]})
        await self.service.analyze(self.key,0)
        row=self.service.store.rows[self.key];history=row['probe_history'][p['id']]
        self.assertTrue(history['sent']);self.assertEqual(len(history['answer_ids']),1)
        self.assertIsNone(row['observations'][-1]['probe']);self.assertEqual(row['observations'][-2]['probe'],p['id'])
