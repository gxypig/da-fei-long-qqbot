import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from test_maintenance import Main
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.image_memory import ImageMemory,safe_semantic
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
from data.plugins.astrbot_plugin_proactive_agent.context_window import ContextWindow,split_rounds

PNG=b'\x89PNG\r\n\x1a\nfixture-image-content'


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.cfg={'image_memory_enabled':True}
        self.ledger=DeliveryLedger(root/'ledger.db');await self.ledger.start();self.addAsyncCleanup(self.ledger.close)
        self.provider=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({
            'description':'白色小龙翻白眼','ocr':[{'text':'看不清的猜测','confidence':.4},{'text':'无语','confidence':.99}],
            'emotion':['无语'],'inferred_usage':['打趣'],'text_complete':False},ensure_ascii=False))))
        self.plugin=SimpleNamespace(_cfg=self.cfg.get,_delivery=self.ledger,_work=SimpleNamespace(busy=lambda:True),
            _data_dir=root,context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=self.provider)))
        self.images=ImageMemory(self.plugin,root/'images');await self.images.start();self.addAsyncCleanup(self.images.close)
        self.ledger.image_details=self.images.details
        self.origin='p:GroupMessage:1';self.cid='cid'

    async def capture(self,data=PNG):
        return await self.images.describe_transport({'type':'image','data':{'file':'base64://'+base64.b64encode(data).decode()}})

    async def send(self,attachment,state='confirmed'):
        id_=self.ledger.begin(self.origin,self.cid,'',[attachment]);self.ledger.result(id_,state,'123' if state=='confirmed' else None)
        self.images.confirmed(self.ledger.store.rows[id_]);return id_

    async def test_pending_failed_images_never_described_as_sent(self):
        attachment=await self.capture();await self.send(attachment,'failed')
        self.assertEqual(self.images.stats['confirmed_images'],0);self.assertEqual(self.ledger.note(self.origin,self.cid),'')
        await self.send(attachment)
        self.assertEqual(self.images.stats['confirmed_images'],1);self.assertIn('content_hash',self.ledger.note(self.origin,self.cid))
        self.assertEqual(self.ledger.note(self.origin,'reset-cid'),'')

    async def test_ocr_uncertainty_uses_only_actual_confirmed_original(self):
        attachment=await self.capture();await self.send(attachment)
        self.plugin._work.busy=lambda:False
        await self.images.learn(attachment['content_hash'],self.origin)
        note=self.ledger.note(self.origin,self.cid)
        self.assertIn('无语',note);self.assertNotIn('看不清的猜测',note)
        e=event(text='你刚发的那张图上写了什么字')
        req=SimpleNamespace(conversation=SimpleNamespace(cid=self.cid),system_prompt='',image_urls=[])
        await self.images.enrich(e,req)
        self.assertEqual(len(req.image_urls),1);self.assertTrue(Path(req.image_urls[0]).is_file())
        req.conversation.cid='reset-cid';req.image_urls=[];await self.images.enrich(e,req)
        self.assertFalse(req.image_urls)

    async def test_path_replacement_gets_new_hash_old_semantics_never_cover_new_bytes(self):
        path=Path(self.temp.name)/'same.png';path.write_bytes(PNG)
        first=await self.images.describe_transport({'type':'image','data':{'file':str(path)}})
        self.plugin._work.busy=lambda:False;await self.images.learn(first['content_hash'],None)
        path.write_bytes(PNG+b'changed')
        second=await self.images.describe_transport({'type':'image','data':{'file':str(path)}})
        self.assertNotEqual(first['content_hash'],second['content_hash'])
        self.assertNotIn('description',self.images.details(second));self.assertIn('description',self.images.details(first))

    async def test_tampered_content_cache_rejects_old_result(self):
        attachment=await self.capture();asset=self.images.store.rows[attachment['content_hash']]
        Path(asset['path']).write_bytes(PNG+b'tamper');self.plugin._work.busy=lambda:False
        with self.assertRaises(ValueError):await self.images.learn(attachment['content_hash'],None)
        self.provider.text_chat.assert_not_awaited()

    async def test_quoted_old_image_wins_over_recent_images_and_no_cross_session_fallback(self):
        old=await self.capture(PNG+b'old');id_=await self.send(old)
        for i in range(10):
            other=await self.capture(PNG+str(i).encode());sent=await self.send(other)
            self.ledger.result(sent,'confirmed',str(200+i))
        e=event(text='那张图上写了什么字');e.get_messages=lambda:[SimpleNamespace(type='Reply',id='123')]
        req=SimpleNamespace(conversation=SimpleNamespace(cid=self.cid),system_prompt='',image_urls=[])
        await self.images.enrich(e,req)
        self.assertEqual(req.image_urls,[self.images.store.rows[old['content_hash']]['path']])
        e.get_messages=lambda:[SimpleNamespace(type='Reply',id='not-this-session')]
        req.image_urls=[];req.system_prompt='';await self.images.enrich(e,req)
        self.assertEqual(req.image_urls,[]);self.assertEqual(req.system_prompt,'')


class AdaptiveContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_view_keeps_recall_and_tool_pairs_complex_budget(self):
        with tempfile.TemporaryDirectory() as d:
            cfg={'context_window_enabled':True,'adaptive_context_enabled':True,'context_recent_turns':24,'context_recent_tokens':12000}
            w=ContextWindow(Path(d)/'cache.json',cfg.get,busy=lambda:True);self.addAsyncCleanup(w.close)
            rows=[{'role':'system','content':'私人提示保持不动'}]
            for i in range(30):rows.extend([{'role':'user','content':f'普通闲聊{i}'},{'role':'assistant','content':'随便聊几句'}])
            short=w.project(None,{'messages':rows},'cid')['messages']
            self.assertLessEqual(len(split_rounds(short)[1]),13);self.assertEqual(short[0],rows[0])
            rows[-2]['content']='请分析一下这个问题的完整步骤，比较两种方案'
            complex_=w.project(None,{'messages':rows},'cid')['messages']
            self.assertGreater(len(complex_),len(short))

    async def test_old_picture_only_retrieves_matching_round_and_keeps_current_image(self):
        with tempfile.TemporaryDirectory() as d:
            cfg={'context_window_enabled':True,'adaptive_context_enabled':True,'context_recent_turns':4,'context_recent_tokens':2000}
            w=ContextWindow(Path(d)/'cache.json',cfg.get,busy=lambda:True);self.addAsyncCleanup(w.close)
            rows=[{'role':'system','content':'private'}]
            for i in range(20):rows.extend([{'role':'user','content':[{'type':'text','text':('红色汽车' if i==2 else '普通照片')},
                {'type':'image_url','image_url':{'url':f'https://example.invalid/{i}.png'}}]},{'role':'assistant','content':'看到了'}])
            rows.append({'role':'user','content':'之前那张红色汽车图片还有什么细节'})
            out=w.project(None,{'messages':rows},'cid')['messages'];serialized=json.dumps(out)
            self.assertIn('/2.png',serialized);self.assertIn('/19.png',serialized);self.assertNotIn('/3.png',serialized)
