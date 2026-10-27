"""Faults at Bili reservation boundaries and independent panel editors."""
import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
from test_maintenance import Main as ProactiveMain
from aiocqhttp import CQHttp
from quart import Quart
from data.plugins.astrbot_plugin_proactive_agent import panel
from data.plugins.astrbot_plugin_proactive_agent.record_store import AsyncRecordStore
from data.plugins.astrbot_plugin_bili_scroll.main import Main,FIELDS
from data.plugins.astrbot_plugin_bili_scroll.receipts import install


class BiliBoundaries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=object.__new__(Main);self.p.config={**FIELDS,'enabled':True};self.p._locks={}
        self.p.logger=Mock();self.p.get_kv_data=AsyncMock(return_value=[]);self.p.put_kv_data=AsyncMock()
        self.store=AsyncRecordStore(Path(self.tmp.name)/'shares.db');await self.store.start();self.addAsyncCleanup(self.store.close)
        self.p._share_store=self.store;self.bot=object.__new__(CQHttp)
        self.transport=AsyncMock(return_value={'message_id':1});self.bot._api=SimpleNamespace(call_action=self.transport)
        self.item={'bvid':'BV1xx411c7mD','url':'https://www.bilibili.com/video/BV1xx411c7mD','title':'合成视频',
                   'description':'合成简介','comments':['测试评论'],'score':1}
        async def discover(keyword,excluded=(),mode=None):return {'items':[] if self.item['bvid'] in excluded else [self.item],'warnings':[]}
        self.p.discover=AsyncMock(side_effect=discover);self.p.make_copy=AsyncMock(return_value='看看这条标题\n'+self.item['url'])
        self.events=[]
        life=SimpleNamespace(enabled=lambda:True,event=lambda *a,**k:self.events.append((a,k)))
        async def send(origin,chain):await self.bot.call_action('send_private_msg',user_id=1,message=chain);return True
        self.p.context=SimpleNamespace(send_message=send,get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=SimpleNamespace(_life=life)),
            conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='cid')))
        self.restore=install(self.p);self.addCleanup(self.restore);self.origin='aiocqhttp:FriendMessage:1'

    async def test_flush_failure_releases_video_before_any_send(self):
        with patch.object(self.store,'flush',side_effect=TimeoutError('disk')):
            with self.assertRaises(TimeoutError):await self.p.share(self.origin)
        self.transport.assert_not_awaited();self.assertEqual(next(iter(self.store.rows.values()))['state'],'failed')
        self.assertNotIn('share_confirmed',[kw['extra']['operation'] for _,kw in self.events])
        self.assertTrue((await self.p.share(self.origin)).startswith('已向'))
        self.assertEqual(self.transport.await_count,1)

    async def test_unknown_delivery_is_not_reselected_or_claimed_as_fact(self):
        self.transport.side_effect=TimeoutError('response lost')
        with self.assertRaises(TimeoutError):await self.p.share(self.origin)
        self.assertEqual(next(iter(self.store.rows.values()))['state'],'unknown')
        self.assertIn('没有找到',await self.p.share(self.origin));self.assertEqual(self.transport.await_count,1)
        operations=[kw['extra']['operation'] for _,kw in self.events]
        self.assertEqual(operations,['discovered','metadata_read','comments_read','metadata_analyzed'])
        self.assertTrue(all(kw['visibility']==self.origin and kw['extra']['video_watched'] is False for _,kw in self.events))

    async def test_confirmed_postsend_storage_failure_cannot_report_not_found(self):
        self.p.put_kv_data.side_effect=OSError('disk')
        self.assertTrue((await self.p.share(self.origin)).startswith('已向'))
        self.assertEqual(next(iter(self.store.rows.values()))['state'],'confirmed')
        self.assertEqual([kw['extra']['operation'] for _,kw in self.events][-1],'share_confirmed')
        self.assertEqual(self.transport.await_count,1)


class PanelSections(unittest.IsolatedAsyncioTestCase):
    async def test_independent_sections_save_without_lost_update_and_same_section_conflicts(self):
        class Config(dict):
            def save_config(self):return True
        p=object.__new__(ProactiveMain);p.config=Config(copy.deepcopy(panel.FIELDS));p._panel_lock=asyncio.Lock()
        app=Quart(__name__);app.add_url_rule('/settings',view_func=p.panel_settings_api,methods=['GET','POST']);client=app.test_client()
        before=(await (await client.get('/settings')).get_json())['data']['sections']
        a={'section':'groups','revision':before['groups']['revision'],'values':{'group_chat_probability':21}}
        b={'section':'proactive','revision':before['proactive']['revision'],'values':{'interval_minutes':37}}
        results=await asyncio.gather(client.post('/settings',json=a),client.post('/settings',json=b))
        self.assertEqual([r.status_code for r in results],[200,200]);self.assertEqual(p.config['group_chat_probability'],21);self.assertEqual(p.config['interval_minutes'],37)
        a['values']['group_chat_probability']=30
        self.assertEqual((await client.post('/settings',json=a)).status_code,409)
        self.assertEqual(p.config['group_chat_probability'],21)
