import copy,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_round2_life as base
from data.plugins.astrbot_plugin_proactive_agent.life_reflection import LifeReflection

class ReflectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.LifeTests.asyncSetUp
    asyncTearDown=base.LifeTests.asyncTearDown
    def actual(self,scope='public'):
        return self.life.event('tool','读了《测试梗》的词条：一句话反复说就没有意思了',visibility=scope,completed=True,source_ids=['public:test'],extra={'operation':'public_wiki_read'})
    def answer(self,event):
        return {'event_id':event['id'],'quote':'一句话反复说就没有意思了','topic':'测试梗','thought':'这个梗偶尔接一句就行，刷屏没意思',
            'next_interest':'下次留意有没有新的用法','keywords':['刷屏','用法']}
    async def test_opinion_is_grounded_scoped_persistent_and_no_chat_model_call(self):
        event=self.actual(self.origin);self.plugin._generate_json.return_value=json.dumps(self.answer(event))
        await self.life.reflection.step()
        self.assertEqual(len(self.life.reflection.cache),1)
        count=self.plugin._generate_json.await_count
        self.assertIn('刷屏没意思',self.life.context(self.origin,'这个梗刷屏了'))
        self.assertNotIn('刷屏没意思',self.life.context('qzone','你最近在干嘛'))
        self.assertNotIn('刷屏没意思',self.life.context('aiocqhttp:FriendMessage:2','测试梗'))
        fresh=LifeReflection(self.life);fresh.refresh();self.assertEqual(fresh.visible(self.origin)[0]['thought'],self.answer(event)['thought'])
        self.assertEqual(count,self.plugin._generate_json.await_count)
    async def test_bad_citation_retries_without_inventing_experience(self):
        event=self.actual();answer=self.answer(event);answer['quote']='资料里没有这句话'
        self.plugin._generate_json.return_value=json.dumps(answer)
        await self.life.reflection.step();self.assertFalse(self.life.reflection.cache)
        self.assertEqual(self.life.reflection.snapshot()['state']['status'],'retry')
        self.now+=301;self.plugin._generate_json.return_value=json.dumps(self.answer(event));await self.life.reflection.step()
        self.assertEqual(len(self.life.reflection.cache),1)
    async def test_qzone_unanswered_never_blocks_but_private_still_does(self):
        self.actual();self.life.store.put('contact:qzone',{'unanswered':99})
        self.assertEqual(self.life.decide(self.life.candidate('qzone'))['decision'],'send')
        self.life.store.put('contact:'+self.origin,{'unanswered':3})
        self.assertEqual(self.life.decide(self.life.candidate(self.origin))['decision'],'defer')
    async def test_qzone_receipt_and_repeat_suppression(self):
        event=self.actual();self.plugin._post_qzone=AsyncMock()
        result=await self.life.opportunity('qzone')
        self.assertEqual(result['decision'],'confirmed');self.assertEqual(self.life.store.rows['contact:qzone']['unanswered'],0)
        self.assertIn(event['id'],self.life.store.rows['contact:qzone']['consumed_events'])
        count=self.plugin._post_qzone.await_count;await self.life.opportunity('qzone');self.assertEqual(self.plugin._post_qzone.await_count,count)
    async def test_real_event_beats_newer_fiction(self):
        event=self.actual();self.now+=3;self.life.advance()
        self.assertEqual(self.life.candidate('qzone')['event_ids'],[event['id']])
    async def test_public_read_uses_only_independent_public_catalogue(self):
        wiki=SimpleNamespace(titles={'test':'测试梗'},exact=lambda _: {'phrase':'测试梗','meaning':'公开释义','provenance':'memewiki','source_hash':'test','sources':[{'url':'https://example.org/public'}]})
        self.plugin._memory=SimpleNamespace(wiki=wiki)
        self.life.observe(self.origin,'private','私人内容不能出现在阅读资料里','sender')
        await self.life.reflection.public_read()
        rows=self.life.events('qzone');self.assertEqual(len(rows),1);self.assertNotIn('私人',str(rows))
        self.assertTrue(rows[0]['completed']);self.assertEqual(rows[0]['operation'],'public_wiki_read')
        await self.life.reflection.public_read();self.assertEqual(len(self.life.events('qzone')),1)
    async def test_disabled_foreground_and_preview_make_no_changes(self):
        self.actual();self.config['life_reflection_enabled']=False
        before=copy.deepcopy(self.life.store.rows);await self.life.reflection.step()
        self.assertEqual(before,self.life.store.rows);self.plugin._generate_json.assert_not_awaited()
        result=await self.life.opportunity('qzone',dry_run=True);self.assertTrue(result['preview']);self.assertEqual(before,self.life.store.rows)

    async def test_public_projection_only_contains_explicitly_approved_summary(self):
        event=self.actual(self.origin);event['private_detail']='私人字段';self.life.store.put('event:'+event['id'],event)
        public=self.life.approve_public_experience(event['id'],'今天发了一条语音，试了不同语气')
        self.assertEqual(public['visibility'],'public');self.assertNotIn('私人字段',str(public));self.assertNotIn(self.origin,str(public))
        self.assertEqual(self.sent,[])
        with self.assertRaises(ValueError):self.life.approve_public_experience('missing','不能凭空造经历')
    async def test_foreground_does_not_start_reflection(self):
        self.actual();self.busy=True;await self.life.reflection.step();self.plugin._generate_json.assert_not_awaited()
    async def test_confirmed_media_is_an_experience_but_unknown_and_error_are_not(self):
        row={'id':'voice1','origin':self.origin,'state':'unknown','attachments':[{'kind':'record'}],'text':'晚安'}
        self.life.confirmed(row);self.assertFalse(self.life.events(self.origin))
        self.life.confirmed({**row,'state':'confirmed','purpose':'error_notification'});self.assertFalse(self.life.events(self.origin))
        self.life.confirmed({**row,'state':'confirmed'});event=self.life.events(self.origin)[0]
        self.assertIn('语音',event['summary']);self.assertEqual(event['visibility'],self.origin)
        self.assertEqual(self.life.context(self.origin,'晚饭吃什么'),'')
        self.assertIn('语音',self.life.context(self.origin,'你刚才在做什么'))
