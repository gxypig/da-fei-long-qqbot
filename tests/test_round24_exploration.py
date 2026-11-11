from unittest.mock import AsyncMock,patch
import unittest
import test_round22_exploration as fixture
ORIGIN,OTHER=fixture.ORIGIN,fixture.OTHER
from data.plugins.astrbot_plugin_proactive_agent.reading_agency import KEY

class FocusRecovery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.Exploration.asyncSetUp
    asyncTearDown=fixture.Exploration.asyncTearDown
    setup=fixture.Exploration.setup
    enable=fixture.Exploration.enable
    decision=fixture.Exploration.decision
    step=fixture.Exploration.step
    def resting(self):
        self.a.update(source_scope=ORIGIN,goal='旧问题',state='resting',resume_on='new_information',next_at=self.now,
            source={'id':'old','scope':ORIGIN,'text':'旧问题的私人来源'},findings=[{'thought':'不可带去新任务'}],
            actions=[{'action':'wait','at':self.now-i} for i in range(3)],wake_facts=self.a.wake_facts())
        return self.a.state()['id']
    async def test_stalled_scope_yields_to_new_topic_without_leaking_old_findings(self):
        self.setup();old=self.resting()
        self.s.discovery.observe(OTHER,'fresh','水墨绘画为什么这样好看',self.now)
        with patch.object(self.a,'ask',AsyncMock(return_value=self.decision('wait'))) as model:await self.a.step()
        model.assert_awaited_once();payload=model.await_args.args[0]
        self.assertFalse(payload['task'].get('source_scope'));self.assertFalse(payload['task'].get('findings'))
        self.assertTrue(any(s['scope']==OTHER for s in payload['sources']))
        archived=self.s.store.rows['exploration:'+old]
        self.assertEqual(archived['state'],'awaiting_update');self.assertEqual(archived['findings'][0]['thought'],'不可带去新任务')
        self.assertNotEqual(self.a.state()['id'],old)
    async def test_chosen_rest_and_chat_busy_do_not_release_focus_early(self):
        self.setup();old=self.resting();self.a.update(next_at=self.now+600)
        with patch.object(self.a,'ask',AsyncMock()) as model:self.assertFalse(await self.a.step())
        model.assert_not_awaited();self.assertEqual(self.a.state()['id'],old)
        self.a.update(next_at=self.now);self.busy=True;self.assertFalse(await self.a.step())
        self.assertEqual(self.a.state()['id'],old)
    async def test_dormant_progress_is_manually_resumable(self):
        self.setup();old=self.resting();await self.a.release_stale_focus(self.a.state())
        self.a.update(state='resting');self.a.request_resume(old,'resume')
        self.assertEqual(self.a.state()['id'],old);self.assertTrue(self.a.state()['manual_wake'])
    async def test_model_pause_can_release_focus_but_user_pause_cannot(self):
        self.setup();old=self.resting();self.a.update(state='paused')
        await self.a.release_stale_focus(self.a.state())
        self.assertEqual(self.s.store.rows['exploration:'+old]['state'],'awaiting_update')
        self.a.request_resume(old,'resume');self.a.update(state='paused',manual_wake=False,manual_pause=True)
        await self.a.release_stale_focus(self.a.state())
        self.assertEqual(self.a.state()['id'],old)
    async def test_invalid_intent_is_replanned_not_replayed(self):
        self.setup()
        with patch.object(self.a,'ask',AsyncMock(return_value=self.decision('read',result_ids=['missing']))) as model:await self.a.step()
        self.assertIsNone(self.a.state().get('intent'));self.assertEqual(self.a.state()['state'],'retry')
        self.now+=61
        with patch.object(self.a,'ask',AsyncMock(return_value=self.decision('finish'))) as model:await self.a.step()
        model.assert_awaited_once();self.assertEqual(self.a.state()['state'],'finished')
    async def test_old_finished_focus_does_not_mask_other_interest(self):
        self.setup();old=self.resting()
        self.a.update(state='finished',actions=[*self.a.state()['actions'],{'action':'finish'}])
        await self.a.release_stale_focus(self.a.state())
        self.assertFalse(self.a.state().get('source_scope'))
        self.assertEqual(self.s.store.rows['exploration:'+old]['state'],'awaiting_update')
    async def test_wait_after_new_focus_without_new_facts_does_not_spin(self):
        self.setup();self.resting();await self.step(self.decision('wait',resume_on='new_information'))
        self.now+=181
        with patch.object(self.a,'ask',AsyncMock()) as model:self.assertFalse(await self.a.step())
        model.assert_not_awaited()
