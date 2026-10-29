import asyncio,unittest
from unittest.mock import AsyncMock,patch
import test_round6_lifecycle as lifecycle
from data.plugins.astrbot_plugin_proactive_agent.session_mute import SessionMutes,SessionMuted
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import install_delivery_transport
from aiocqhttp import CQHttp

class MuteWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await lifecycle.FirstSessionAndReceipt.asyncSetUp(self)
        self.mutes=SessionMutes(self.ledger.store.path.with_suffix('.mutes.json'),self.q,self.ledger)
        await self.mutes.start();self.addAsyncCleanup(self.mutes.close)
        self.plugin._mutes=self.mutes;self.job.mute_epoch=self.mutes.epoch(self.job.origin)
    async def asyncTearDown(self):
        await self.mutes.close()
        await lifecycle.FirstSessionAndReceipt.asyncTearDown(self)
    async def mute(self,value=True):
        return await self.mutes.change(self.job.origin,value,None,self.mutes.status(self.job.origin)['revision'],'mute' if value else 'restore')
    async def test_all_payload_forms_blocked_while_muted(self):
        await self.mute();wire=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',wire):
            restore=install_delivery_transport(self.plugin)
            try:
                for action,payload in [('send_private_msg',{'message':'文字'}),('send_private_msg',{'message':[{'type':'record','data':{'file':'fake'}}]}),
                    ('send_private_msg',{'message':[{'type':'image','data':{'file':'fake'}}]}),('send_private_forward_msg',{'messages':[{'type':'node','data':{'content':[{'type':'text','data':{'text':'转发'}}]}}]})]:
                    with self.assertRaises(SessionMuted):await CQHttp.call_action(self.bot,action,user_id=1,**payload)
            finally:restore()
        wire.assert_not_awaited()
    async def test_mute_and_resume_during_final_durability_blocks_old_reply(self):
        entered=asyncio.Event();release=asyncio.Event();flush=self.ledger.store.flush;calls=0
        async def delayed(*args,**kw):
            nonlocal calls
            calls+=1
            if calls==2:entered.set();await release.wait()
            return await flush(*args,**kw)
        wire=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',wire),patch.object(self.ledger.store,'flush',delayed):
            restore=install_delivery_transport(self.plugin)
            try:
                task=asyncio.create_task(CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='过期草稿'))
                await entered.wait();await self.mute();await self.mute(False);release.set()
                with self.assertRaises(SessionMuted):await task
            finally:release.set();restore()
        wire.assert_not_awaited();row=self.ledger.store.rows[self.job.sends[-1]]
        self.assertEqual(row['delivery_result'],'not_sent')
    async def test_already_submitted_receipt_survives_mute(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def wire(*a,**kw):entered.set();await release.wait();return {'message_id':99}
        with patch.object(CQHttp,'call_action',wire):
            restore=install_delivery_transport(self.plugin)
            try:
                task=asyncio.create_task(CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='已经提交'))
                await entered.wait();result=await self.mute();self.assertEqual(result['submitted_waiting_receipts'],1)
                release.set();await task
            finally:release.set();restore()
        row=self.ledger.store.rows[self.job.sends[-1]];self.assertEqual(row['state'],'confirmed');self.assertEqual(row['message_id'],'99')

if __name__=='__main__':unittest.main()
