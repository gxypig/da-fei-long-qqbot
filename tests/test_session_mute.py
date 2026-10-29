import asyncio,copy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import ROOT
from test_dragon_character import event
from data.plugins.astrbot_plugin_proactive_agent.session_mute import SessionMutes,SessionMuted,MuteConflict,pin_recipient,pinned_epoch
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue

class MuteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1000;self.q=ReplyQueue({}.get,lambda *a:None)
        self.store=SessionMutes(Path(self.tmp.name)/'mute.json',self.q,clock=lambda:self.now)
        await self.store.start();self.key='p:GroupMessage:100'
    async def asyncTearDown(self):await self.store.close();self.tmp.cleanup()
    async def change(self,muted=True,until=None,request_id='one',key=None):
        key=key or self.key
        return await self.store.change(key,muted,until,self.store.status(key)['revision'],request_id)

    async def test_physical_group_is_shared_private_is_separate(self):
        await self.change()
        for origin in ['p:GroupMessage:100','p:GroupMessage:1_100','p:GroupMessage:2_100']:
            with self.assertRaises(SessionMuted):self.store.check(origin)
        self.store.check('p:FriendMessage:100');self.store.check('other:GroupMessage:100')

    async def test_restart_preserves_indefinite_mute(self):
        await self.change();await self.store.close()
        other=SessionMutes(self.store.path,clock=lambda:self.now);await other.start()
        try:self.assertTrue(other.status(self.key)['muted'])
        finally:await other.close()

    async def test_expiry_is_persisted_and_changes_epoch(self):
        before=self.store.capture(self.key);await self.change(until=1001);self.now=1002
        self.assertTrue(self.store.status(self.key)['expired_waiting']);await self.store.expire()
        self.assertFalse(self.store.status(self.key)['muted']);self.assertEqual(self.store.status(self.key)['revision'],2)
        with self.assertRaises(SessionMuted):self.store.check(self.key,before)

    async def test_quick_resume_never_resurrects_old_generation(self):
        before=self.store.capture(self.key);await self.change();await self.change(False,request_id='resume')
        self.store.check(self.key)
        with self.assertRaises(SessionMuted):self.store.check(self.key,before)

    async def test_queued_same_object_cancelled_other_object_kept(self):
        jobs=[]
        for origin in [self.key,'p:GroupMessage:2_100','p:GroupMessage:200']:
            e=event('hello');e.unified_msg_origin=origin;j=self.q.create(e);self.q.submit(j);j.owner=None;jobs.append(j)
        result=await self.change()
        self.assertEqual(result['cancelled'],2);self.assertEqual(jobs[0].state,'cancelled');self.assertNotEqual(jobs[2].state,'cancelled')

    async def test_cas_and_idempotency(self):
        first=await self.change()
        again=await self.store.change(self.key,True,None,0,'one');self.assertEqual(first,again)
        with self.assertRaises(MuteConflict):await self.store.change(self.key,False,None,0,'different')
        with self.assertRaises(MuteConflict):await self.store.change(self.key,False,None,0,'one')

    async def test_new_activity_elsewhere_does_not_invalidate_cas(self):
        await self.change(key='p:FriendMessage:333')
        result=await self.store.change(self.key,True,None,0,'this');self.assertEqual(result['revision'],1)

    async def test_failed_disk_write_is_fail_closed(self):
        old=self.store.capture(self.key)
        with patch.object(self.store,'_write',AsyncMock(side_effect=OSError('full'))):
            with self.assertRaises(OSError):await self.change()
        self.assertFalse(self.store.status(self.key)['durable'])
        with self.assertRaises(SessionMuted):self.store.check(self.key,old)
        await self.change(False,request_id='retry');self.assertTrue(self.store.status(self.key)['durable'])
        with self.assertRaises(SessionMuted):self.store.check(self.key,old)

    async def test_transition_blocks_while_persisting(self):
        entered=asyncio.Event();release=asyncio.Event();write=self.store._write
        async def wait(data):entered.set();await release.wait();await write(data)
        with patch.object(self.store,'_write',wait):
            task=asyncio.create_task(self.change());await entered.wait()
            with self.assertRaises(SessionMuted):self.store.check(self.key)
            release.set();await task

    async def test_background_pin_remembers_pre_mute_epoch(self):
        with pin_recipient(self.key):
            pinned=pinned_epoch(self.key);await self.change();await self.change(False,request_id='resume')
            with self.assertRaises(SessionMuted):self.store.check(self.key,pinned)
        self.assertIsNone(pinned_epoch(self.key))

    async def test_resume_cannot_replay_after_restart_expiry(self):
        await self.change(until=1001);await self.store.close();self.now=1002
        other=SessionMutes(self.store.path,clock=lambda:self.now);await other.start()
        try:self.assertFalse(other.status(self.key)['muted']);self.assertEqual(other.status(self.key)['revision'],2)
        finally:await other.close()

    async def test_bad_rules_fail_start_instead_of_silently_unmuting(self):
        path=Path(self.tmp.name)/'bad.json';path.write_text('{"rules":{"bad":{}},"actions":{}}')
        other=SessionMutes(path)
        with self.assertRaises(ValueError):await other.start()

if __name__=='__main__':unittest.main()
