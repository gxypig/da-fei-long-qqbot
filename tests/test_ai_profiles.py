import asyncio
import json
from pathlib import Path
import tempfile
from temp_support import TemporaryDirectory
import unittest
from unittest.mock import patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.ai_profiles import AIProfiles,identity
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import QueueConflict


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_notifies_learning_even_if_effective_label_already_expired(self):
        from unittest.mock import AsyncMock
        self.observe('你好');self.action('confirm')
        self.service.on_change=AsyncMock()
        self.now+=8*86400
        self.assertEqual(self.service.source_label('qq','123'),'unverified')
        self.service.wake.set()
        async with asyncio.timeout(2):
            while not self.service.on_change.await_count:await asyncio.sleep(.01)
        self.service.on_change.assert_awaited_with('qq','123','unverified')

    async def test_weak_style_and_speed_are_display_only_no_probe_or_learning_isolation(self):
        from unittest.mock import AsyncMock
        self.observe('分点说一下我今天的计划')
        row=self.service.store.rows[self.key];message=row['observations'][-1]['id']
        self.service.generate=AsyncMock(return_value=json.dumps({'judgement_type':'ai','evidence':[
            {'kind':'style','message_ids':[message],'summary':'有分点','strength':'strong'},
            {'kind':'speed','message_ids':[message],'summary':'很快','strength':'weak'}],'counterevidence':[],'reason':'只有文风'}))
        await self.service.analyze(self.key,row['epoch'])
        current=self.service.store.rows[self.key]
        self.assertEqual(len(current['evidence']),2);self.assertFalse(current['probe_eligible'])
        self.assertEqual(self.service.source_label('qq','123'),'unverified')
        self.assertIsNone(await self.service.prepare_probe('qq','123','trigger','qq:GroupMessage:1',text='我这个号的自动回复最近出错了'))

    async def test_semantic_probe_closes_as_not_asked_when_final_text_contains_no_question(self):
        from unittest.mock import AsyncMock
        await self.seed_suspect()
        self.service.generate=AsyncMock(return_value=json.dumps({'judgement_type':'insufficient','evidence':[],'counterevidence':[], 'probe_links':[]}))
        probe=await self.service.prepare_probe('qq','123','trigger','qq:GroupMessage:1',text='我这个号的自动回复最近出错了')
        self.service.delivery(probe,'今天先聊这个吧','confirmed','delivered','message')
        await self.service.analyze(self.key,probe['epoch'])
        row=self.service.store.rows[self.key]
        self.assertIsNone(row['outstanding']);self.assertEqual(row['probe_history'][probe['id']]['outcome'],'not_asked')

    async def asyncSetUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1793000000.;self.cfg={'ai_profiles_enabled':True,'ai_probe_enabled':True}
        self.service=AIProfiles(Path(self.temp.name)/'profiles.db',self.cfg.get,busy=lambda:True,clock=lambda:self.now)
        await self.service.start();self.addAsyncCleanup(self.service.close)
        self.key=identity('qq','123');self.counter=0

    def observe(self,text,origin='qq:GroupMessage:1'):
        self.counter+=1;self.service.observe('qq','123',text,origin,str(self.counter));return str(self.counter)

    def assess(self):self.service.assess(self.key,self.service.store.rows[self.key]['epoch'])

    def action(self,action,**extra):
        return self.service.action({'action':action,'instance':self.service.instance,'version':self.service.version,
            'request_id':'operation-'+str(self.service.version),'platform':'qq','account':'123',**extra})

    async def seed_suspect(self):
        for i in range(5):
            self.observe('APIError: insufficient_quota request_id='+str(i));self.now+=20
        self.assess();self.assertEqual(self.service.store.rows[self.key]['state'],'suspected')

    async def probe_answer(self,text):
        probe=await self.service.prepare_probe('qq','123','trigger','qq:GroupMessage:1',text='我这个号的自动回复最近出错了')
        self.assertIsNotNone(probe)
        self.service.delivery(probe,probe['question'],'confirmed');self.now+=2
        self.observe(text);self.assess();return probe

    async def test_fast_formal_single_claim_never_confirms(self):
        for _ in range(8):self.observe('一、你好。二、今天天气很好。');self.now+=.1
        self.assess();self.assertEqual(self.service.store.rows[self.key]['state'],'observing')
        self.observe('我是AI机器人');self.assess()
        self.assertEqual(self.service.store.rows[self.key]['state'],'suspected')
        self.assertEqual(self.service.reply_note('qq','123'),'')

    async def test_regex_claims_and_machine_errors_only_open_observation(self):
        await self.seed_suspect();a=await self.probe_answer('我是机器人，这个账号是程序自动回复')
        self.assertEqual(self.service.store.rows[self.key]['state'],'suspected')
        b=await self.probe_answer('我是AI，这个账号由程序自动回复')
        self.assertNotEqual(a['id'],b['id']);self.assertEqual(self.service.store.rows[self.key]['state'],'suspected')
        self.assertEqual('',self.service.reply_note('qq','123'))
        self.assertNotIn('insufficient_quota',self.service.reply_note('qq','123'))

    async def test_roleplay_quotes_and_contradiction_block_confirmation(self):
        await self.seed_suspect()
        await self.probe_answer('假装我是AI机器人')
        await self.probe_answer('他说“我是AI机器人”')
        self.assertNotEqual(self.service.store.rows[self.key]['state'],'auto_confirmed')
        await self.probe_answer('我是机器人，但我现在是真人手动回复')
        self.assertNotEqual(self.service.store.rows[self.key]['state'],'auto_confirmed')

    async def test_quota_cross_session_restart_revoke_natural_day(self):
        await self.seed_suspect()
        for i in range(3):
            p=await self.service.prepare_probe('qq','123','x'+str(i),'qq:FriendMessage:123' if i%2 else 'qq:GroupMessage:1',text='我这个号的自动回复最近出错了')
            self.assertIsNotNone(p)
            self.assertIsNone(await self.service.prepare_probe('qq','123','other','qq:GroupMessage:2',text='我这个号的自动回复最近出错了'))
            self.service.delivery(p,p['question'],'unknown');self.service.end_turn(p)
            self.now+=601 # Unknown delivery cannot be retried as if it never happened.
        self.assertIsNone(await self.service.prepare_probe('qq','123','x','qq:GroupMessage:2',text='我这个号的自动回复最近出错了'))
        self.action('revoke');self.observe('我是AI机器人');self.assess()
        self.assertIsNone(await self.service.prepare_probe('qq','123','x','qq:GroupMessage:2',text='我这个号的自动回复最近出错了'))
        await self.service.close()
        restored=AIProfiles(Path(self.temp.name)/'profiles.db',self.cfg.get,busy=lambda:True,clock=lambda:self.now)
        await restored.start();self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.snapshot()['rows'][0]['today_probes'],3)
        self.now+=86400
        self.assertIsNotNone(await restored.prepare_probe('qq','123','next-day','qq:GroupMessage:3',text='我这个号的自动回复最近出错了'))

    async def test_revoke_and_expiry_reject_late_evidence_without_resetting_quota(self):
        await self.seed_suspect();epoch=self.service.store.rows[self.key]['epoch']
        self.action('confirm');self.assertIn('弱鸡ai',self.service.reply_note('qq','123'))
        expires=self.service.store.rows[self.key]['expires_at']
        self.action('settings',default_days=20)
        self.assertEqual(expires,self.service.store.rows[self.key]['expires_at'])
        self.action('revoke');self.service.assess(self.key,epoch)
        self.assertEqual(self.service.reply_note('qq','123'),'')
        self.assertEqual(self.service.store.rows[self.key]['state'],'revoked')
        self.action('confirm',expires_at=self.now+10);self.now+=11
        version=self.service.version
        self.assertEqual(self.service.reply_note('qq','123'),'')
        self.assertEqual(self.service.snapshot()['rows'][0]['state'],'expired')
        self.assertEqual(self.service.version,version) # GET is pure.

    async def test_action_idempotence_versions_and_read_only_snapshot(self):
        body={'action':'confirm','platform':'qq','account':'123','instance':self.service.instance,
              'version':self.service.version,'request_id':'fixed-action'}
        self.service.action(body);self.assertTrue(self.service.action(body)['duplicate'])
        with self.assertRaises(QueueConflict):self.service.action({**body,'action':'revoke'})
        with self.assertRaises(QueueConflict):self.service.action({**body,'request_id':'different-action'})
        self.cfg['ai_profiles_enabled']=False;self.assertEqual(self.service.reply_note('qq','123'),'')
