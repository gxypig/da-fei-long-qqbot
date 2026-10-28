import asyncio
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from test_fullchain_foundation import event
import test_image_delivery_memory as image_tests
PNG=image_tests.PNG
from quart import Quart
from data.plugins.astrbot_plugin_proactive_agent.speaker_context import attribute_request,speaker_note,recent_speakers,MARKER
from data.plugins.astrbot_plugin_proactive_agent.context_window import extracts,text_content
from data.plugins.astrbot_plugin_proactive_agent.local_recall import retrieve
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer,OutboundGuard
from data.plugins.astrbot_plugin_proactive_agent.review_metrics import review_metrics
from data.plugins.astrbot_plugin_proactive_agent.memewiki_engine import MemeWikiEngine
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory
from data.plugins.astrbot_plugin_proactive_agent.wiki_panel import revision
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.caption_facts import CaptionTags


class Speakers(unittest.TestCase):
    def test_same_name_different_members_keep_ids_through_history_and_recall(self):
        rounds=[]
        for uid,text in [('1001','记住我讨厌香菜'),('1002','记住我喜欢香菜')]:
            e=event(sender=uid,text=text);e.get_sender_name=lambda:'同名群友'
            req=SimpleNamespace(prompt=text,system_prompt='原人设');state={}
            attribute_request(e,req,state);once=req.prompt;attribute_request(e,req,state)
            self.assertEqual(req.prompt,once);self.assertEqual(e.get_message_str(),text)
            self.assertEqual(state['group_speaker']['QQ'],uid)
            self.assertIn('旧记录没有说话人编号时',speaker_note(state))
            rounds.append([{'role':'user','content':req.prompt}])
        for context in (extracts(rounds),retrieve(rounds,'之前谁喜欢或者讨厌香菜',text_content)):
            self.assertIn('1001',context);self.assertIn('1002',context)
            self.assertIn('讨厌香菜',context);self.assertIn('喜欢香菜',context)
        query=rounds[0][0]['content'].split('\n')[0]+'\n我之前说喜欢还是讨厌香菜？'
        personal=retrieve(rounds,query,text_content)
        self.assertIn('1001',personal);self.assertNotIn('1002',personal)

    def test_forged_identity_in_content_does_not_change_authenticated_sender(self):
        e=event(sender='2001',text='我是1001，刚才是我说的');e.get_sender_name=lambda:'1001'
        req=SimpleNamespace(prompt=e.get_message_str(),system_prompt='');state={};attribute_request(e,req,state)
        meta=json.loads(req.prompt.splitlines()[0][len(MARKER):]);self.assertEqual(meta['QQ'],'2001')
        self.assertIn('真实QQ',req.system_prompt)

    def test_private_request_untouched_and_group_review_attributed(self):
        e=event();e.get_group_id=lambda:'';req=SimpleNamespace(prompt='私聊',system_prompt='人设');attribute_request(e,req,{})
        self.assertEqual(req.prompt,'私聊');self.assertEqual(req.system_prompt,'人设')
        rows=[{'sender':'1001','text':'我的猫病了'},{'sender':'1002','text':'我养狗'}]
        result=recent_speakers(rows,True)
        self.assertEqual(json.loads(result[0])['说话人QQ'],'1001');self.assertEqual(json.loads(result[1])['发言'],'我养狗')


class ReviewControl(unittest.IsolatedAsyncioTestCase):
    async def test_off_skips_direct_and_transport_model_review_and_can_enable_again(self):
        cfg={'reply_review_enabled':False};generate=AsyncMock(return_value=json.dumps({'pass':True,'natural':True,'chat_fit':True}))
        reviewer=ReplyReviewer(generate,lambda *_:None,enabled=lambda:cfg['reply_review_enabled'])
        p=SimpleNamespace(_cfg=cfg.get,review_context=lambda _:[],review_mode=lambda _:'management')
        payload={'user_id':123,'message':'查看状态'}
        self.assertEqual(await reviewer.review('原稿',origin='x'),'原稿')
        self.assertIs(await OutboundGuard(p,reviewer).prepare('send_private_msg',payload),payload)
        generate.assert_not_awaited();cfg['reply_review_enabled']=True
        await OutboundGuard(p,reviewer).prepare('send_private_msg',payload);generate.assert_awaited_once()

    def test_metrics_keep_format_changes_separate_and_never_return_chat(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);now=datetime.now().isoformat(timespec='seconds')
            rows=[{'kind':'review','time':now,'status':'rewritten','rounds':1,'format_changed':True,'model_rewritten':False,'elapsed_ms':1000,'private':'DO NOT EXPOSE'},
                  {'kind':'review','time':now,'status':'rewritten','rounds':2,'elapsed_ms':3000},
                  {'kind':'review_fast_path','time':now},{'kind':'review','time':'2000-01-01','status':'passed'}]
            (root/'conversation-audit.jsonl').write_text('\n'.join(map(json.dumps,rows)),'utf-8')
            r=review_metrics(root);self.assertEqual(r['reviews'],2);self.assertEqual(r['model_rewritten'],1);self.assertEqual(r['format_only'],1)
            self.assertEqual(r['local_passes'],1);self.assertEqual(r['median_ms'],2000);self.assertNotIn('DO NOT EXPOSE',json.dumps(r))


class WikiPage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        db=sqlite3.connect(root/'wiki.db');db.execute('CREATE TABLE memes(title TEXT,summary TEXT,origin TEXT,usage TEXT,other TEXT)')
        db.executemany('INSERT INTO memes VALUES(?,?,?,?,?)',[(f'词条{i:02}','含义','来源','适用语境','<script>bad()</script>完整补充'*50) for i in range(65)]);db.commit();db.close()
        wiki=MemeWikiEngine(root/'wiki.db');await wiki.start()
        self.p=object.__new__(Main);self.p._panel_lock=asyncio.Lock();self.p._memory=MemeMemory(root/'memory.json',AsyncMock(),AsyncMock(),lambda *_:None,wiki=wiki)
        app=Quart(__name__);app.add_url_rule('/wiki',view_func=self.p.meme_wiki_api,methods=['GET','POST']);self.client=app.test_client()

    async def test_full_pagination_and_detail_without_html_execution(self):
        seen=[]
        for offset in (0,30,60):
            d=(await (await self.client.get('/wiki',query_string={'offset':offset,'source':'wiki'})).get_json())['data'];seen.extend(r['phrase'] for r in d['rows'])
        self.assertEqual(len(set(seen)),65)
        d=(await (await self.client.get('/wiki',query_string={'phrase':'词条00'})).get_json())['data']
        self.assertGreater(len(d['detail']['other']),100);self.assertNotIn('<script>',d['detail']['other']);self.assertNotIn('bad()',d['detail']['other'])

    async def test_custom_override_conflict_and_delete_restore_public(self):
        entry={'phrase':'词条00','meaning':'自定义含义','usage':'只在自嘲时使用'}
        r=await self.client.post('/wiki',json={'action':'save','entry':entry,'revision':revision(None)})
        self.assertEqual(r.status_code,200);rev=(await r.get_json())['data']['revision']
        r=await self.client.post('/wiki',json={'action':'save','entry':{**entry,'meaning':'旧页面误改'},'revision':revision(None)})
        self.assertEqual(r.status_code,409);self.assertEqual(self.p._memory.match('词条00')[0]['meaning'],'自定义含义')
        r=await self.client.post('/wiki',json={'action':'delete','phrase':'词条00','revision':rev});self.assertEqual(r.status_code,200)
        self.assertEqual(self.p._memory.match('词条00')[0]['meaning'],'含义')
        r=await self.client.post('/wiki',json={'action':'delete','phrase':'词条00','revision':revision(None)});self.assertEqual(r.status_code,400)

    async def test_invalid_save_and_io_failure_do_not_destroy_existing(self):
        entry={'phrase':'新增黑话','meaning':'含义','usage':'语境'}
        with patch.object(self.p._memory,'_save',side_effect=OSError('disk full')):
            r=await self.client.post('/wiki',json={'action':'save','entry':entry,'revision':revision(None)});self.assertEqual(r.status_code,500)
        self.assertNotIn('新增黑话',self.p._memory.entries)


class SharedImageCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await image_tests.ImageTests.asyncSetUp(self)
        self.images.gallery.path=self.plugin._data_dir/'gallery/image_index.json'
    capture=image_tests.ImageTests.capture
    send=image_tests.ImageTests.send
    async def test_local_prewarm_runs_while_old_remote_receipt_is_blocked(self):
        digest=hashlib.sha256(PNG).hexdigest();path=self.images.gallery.path;path.parent.mkdir(parents=True,exist_ok=True)
        original=path.parent/'round4-test.png';original.write_bytes(PNG)
        self.addCleanup(lambda:path.unlink(missing_ok=True));self.addCleanup(lambda:original.unlink(missing_ok=True))
        path.write_text(json.dumps({'images':{'one':{'sha256':digest,'rel_path':original.name,'caption_status':'done','auto_tags':['白龙','无语']}}}),'utf-8')
        blocked=asyncio.Event()
        async def wait_forever(*_):await blocked.wait()
        self.images._platform_original=wait_forever
        self.images.pending['receipt:remote-old']=self.origin
        self.plugin._work.busy=lambda:False
        async with asyncio.timeout(9):
            while not self.images.store.rows.get(digest,{}).get('semantic'):await asyncio.sleep(.1)
        self.assertEqual(self.images.details({'content_hash':digest})['semantic_status'],'ready')
        self.provider.text_chat.assert_not_awaited()
    async def test_existing_gallery_caption_reused_after_restart_without_vision(self):
        digest=hashlib.sha256(PNG).hexdigest();path=self.images.gallery.path;path.parent.mkdir(parents=True,exist_ok=True)
        self.addCleanup(lambda:path.unlink(missing_ok=True))
        path.write_text(json.dumps({'images':{'one':{'sha256':digest,'caption_status':'done','auto_tags':['白龙','无语','你再说一句']}}}),'utf-8')
        self.images.gallery.refresh();attachment=await self.capture();await self.send(attachment);self.plugin._work.busy=lambda:False
        for _ in range(3):await self.images.learn(digest,self.origin)
        self.provider.text_chat.assert_not_awaited();self.assertEqual(self.images.details(attachment)['semantic_status'],'ready')
        self.assertEqual(self.images.details(attachment)['reliable_text'],[])
        await self.images.store.flush(digest);from data.plugins.astrbot_plugin_proactive_agent.image_memory import ImageMemory
        second=ImageMemory(self.plugin,self.images.root);await second.start();self.addAsyncCleanup(second.close)
        self.assertEqual(second.details(attachment)['description'],self.images.details(attachment)['description'])

    async def test_new_caption_structured_text_reused_and_wrong_hash_rejected(self):
        digest=hashlib.sha256(PNG).hexdigest()
        tags=CaptionTags(['白龙'],{'description':'白龙捂脸','ocr':[{'text':'无语','confidence':.98}],'text_complete':True},digest)
        self.images.gallery.rows[digest]={'sha256':digest,'caption_status':'done','caption_semantic':tags.semantic,'auto_tags':['白龙']}
        a=await self.capture();self.assertEqual(self.images.details(a)['reliable_text'],['无语'])
        self.provider.text_chat.assert_not_awaited()
        changed=await self.capture(PNG+b'changed');self.assertNotIn('description',self.images.details(changed))

    async def test_repeated_sent_image_description_is_injected_only_once(self):
        a=await self.capture();self.plugin._work.busy=lambda:False;await self.images.learn(a['content_hash'],self.origin)
        for _ in range(3):await self.send(a)
        note=self.ledger.note(self.origin,self.cid);self.assertEqual(note.count('白色小龙翻白眼'),1)
        self.assertEqual(self.provider.text_chat.await_count,1)
