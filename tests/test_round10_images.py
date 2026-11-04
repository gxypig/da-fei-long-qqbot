import asyncio,copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.image_memory import ImageMemory
from data.plugins.astrbot_plugin_proactive_agent.image_semantics import normalize,automatic
from data.plugins.astrbot_plugin_proactive_agent.emoji_match import accepted,contradiction,fingerprint,shortcut
from data.plugins.astrbot_plugin_proactive_agent.image_catalog import GalleryFacts
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.caption_facts import CaptionTags
from test_async_emoji import Event

def semantic(action='goodnight',ocr='晚安',**changes):
    return normalize({'version':2,'description':'单张表情','ocr':[{'text':ocr,'confidence':.99}] if ocr else [],
        'text_complete':True,'emotion':['中性'],'intensity':0,'actions':[action],'target':'shared','image_type':'single_meme',
        'conditions':['语境匹配'],'exclusions':[],'relationship':'broad','confidence':.95,**changes},content_hash='a'*64)

def item(action='goodnight',ocr='晚安',**changes):return {'id':'one','content_hash':'a'*64,'semantic':semantic(action,ocr,**changes)}
def decision(text):return {'image_id':'one','final_sha256':fingerprint(text),'reason':'对应语境','checks':{k:True for k in ('intent','ocr','target','emotion','relationship')}}

class Matching(unittest.TestCase):
    def test_real_wrong_matches_rejected_even_with_model_approval(self):
        for text,pic in [('睡得还行就好，没耽误事吧？',item('confirm_guess','你猜对了')),('路上别为了赶时间着急',item('replace','换一个'))]:
            self.assertFalse(accepted(text,{},pic,decision(text)))
        for text,pic in [('你猜对了',item('confirm_guess','你猜对了')),('那换一个吧',item('replace','换一个'))]:
            self.assertTrue(accepted(text,{},pic,decision(text)))

    def test_exact_goodnight_only_local_shortcut(self):
        self.assertTrue(shortcut('晚安，做个好梦',{},item()))
        for text in ['别说晚安','晚安个屁','他说“晚安”是什么意思','这个字是晚安才怪','我还在工作']:
            self.assertFalse(shortcut(text,{},item()),text)
        self.assertFalse(shortcut('哈哈笑死',{},item('amusement','哈哈')))
        self.assertFalse(accepted('哈哈笑死',{},item('amusement','哈哈')))

    def test_requires_all_five_checks_and_exact_final_body(self):
        pic=item('amusement','哈哈');text='哈哈笑死';proof=decision(text)
        self.assertTrue(accepted(text,{},pic,proof))
        for key in proof['checks']:
            wrong=copy.deepcopy(proof);wrong['checks'][key]=False
            self.assertFalse(accepted(text,{},pic,wrong))
        self.assertFalse(accepted(text+'但是现在难过',{},pic,proof))
        self.assertFalse(accepted(text,{'mood':'serious'},pic,proof))
        self.assertFalse(accepted(text,{},item('amusement','哈哈',relationship='close'),proof))
        self.assertFalse(accepted('别难过',{},item('tease','哈哈'),decision('别难过')))

    def test_old_tags_partial_semantics_collections_never_automatic(self):
        old=normalize({'description':'已有标签：开心','emotion':['开心']})
        self.assertEqual(old['version'],1);self.assertFalse(automatic(old))
        for changes in ({'image_type':'collection'},{'image_type':'screenshot'},{'image_type':'photo'},{'confidence':.7},{'text_complete':False},{'target':'unknown'}):
            self.assertFalse(automatic(semantic(**changes)))
        self.assertTrue(automatic(semantic()))

    def test_caption_reuses_same_full_schema_not_tag_inference(self):
        raw=semantic();caption=CaptionTags(['晚安'],raw,'b'*64)
        self.assertEqual(caption.semantic['version'],2);self.assertEqual(caption.semantic['content_hash'],'b'*64)
        old=CaptionTags(['你猜对了'],{'description':'角色表情'},'c'*64)
        self.assertEqual(old.semantic['version'],1)

class ImageFacts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.plugin=SimpleNamespace(_cfg=lambda k,d:True if k=='image_memory_enabled' else d,_work=SimpleNamespace(busy=lambda:False),_data_dir=self.root/'agent')
        self.mem=ImageMemory(self.plugin,self.root/'memory');await self.mem.store.start();self.addAsyncCleanup(self.mem.close)

    async def test_real_legacy_vision_remains_recallable_but_tags_are_not_facts(self):
        digest='a'*64;old={'version':1,'description':'白龙举手','reliable_text':['晚安'],'emotion':[],'inferred_usage':[],'text_complete':True}
        row={'hash':digest,'learned_at':100,'semantic':old}
        self.mem.store.put(digest,row)
        self.assertEqual(self.mem.details({'content_hash':digest})['reliable_text'],['晚安'])
        self.assertEqual(self.mem.candidates([item()],'test'),[])
        self.mem.store.put(digest,{**row,'semantic_source':'gallery_legacy_tags'})
        self.assertNotIn('description',self.mem.details({'content_hash':digest}))

    async def test_original_image_endpoint_validates_exact_content(self):
        from quart import Quart
        from data.plugins.astrbot_plugin_proactive_agent.round2_panel import register_round2_panel
        app=Quart(__name__)
        def register(path,handler,methods,description):app.add_url_rule('/'+path.rsplit('/',1)[-1],view_func=handler,methods=methods)
        self.plugin.context=SimpleNamespace(register_web_api=register);self.plugin._image_memory=self.mem
        register_round2_panel(self.plugin);client=app.test_client()
        data=b'GIF89a'+b'test';p=self.root/'original.gif';p.write_bytes(data);digest=hashlib.sha256(data).hexdigest()
        self.mem.store.put('match:test',{'content_hash':digest});self.mem.store.put(digest,{'path':str(p),'hash':digest})
        response=await client.get('/panel_match_image?id=test');self.assertEqual(response.status_code,200)
        p.write_bytes(b'changed');self.assertEqual((await client.get('/panel_match_image?id=test')).status_code,404)
        self.assertEqual((await client.get('/panel_match_image?id=missing')).status_code,404)
        response=await client.post('/panel_image_feedback',json={'id':'test','kind':'disable'})
        self.assertEqual(response.status_code,200);self.assertTrue(p.exists());self.assertTrue(self.mem.store.rows['feedback:'+digest]['disabled'])

    async def test_recognition_feedback_rechecks_then_reenables_but_explicit_disable_stays(self):
        data=b'GIF89atest';p=self.root/'image.gif';p.write_bytes(data);digest=hashlib.sha256(data).hexdigest()
        self.mem.store.put(digest,{'hash':digest,'path':str(p)})
        self.mem.gallery.refresh=lambda:None;self.mem.gallery.rows={}
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(semantic(content_hash=digest)))))
        self.plugin.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        for reason,disabled in [('recognition_wrong',False),('disable',True)]:
            self.mem.store.put(digest,{'hash':digest,'path':str(p)})
            self.mem.store.put('feedback:'+digest,{'reason':reason,'disabled':True})
            await self.mem.learn(digest,None)
            self.assertEqual(self.mem.store.rows['feedback:'+digest]['disabled'],disabled)

    async def test_tags_only_not_semantically_ready(self):
        index=self.root/'index.json';index.write_text(json.dumps({'images':{'x':{'sha256':'a'*64,'caption_status':'done','auto_tags':['开心']}}}),'utf-8')
        facts=GalleryFacts(index);facts.refresh();self.assertIsNone(facts.semantic('a'*64))
        self.mem.gallery=facts;self.assertFalse(self.mem.reuse_gallery('a'*64))

    async def test_conversion_content_verified_and_crop_not_inherited(self):
        from PIL import Image
        src=self.root/'src.png';out=self.root/'out.gif';crop=self.root/'crop.gif'
        Image.new('RGB',(24,24),(20,40,80)).save(src);Image.open(src).save(out,'GIF');Image.new('RGB',(10,10),'red').save(crop,'GIF')
        digest,path=self.mem._capture(str(src));self.mem.store.put(digest,{'hash':digest,'path':path,'semantic':semantic(content_hash=digest)})
        await self.mem.register_conversion(src,out,'asset-one')
        a=await self.mem.describe_transport({'type':'image','data':{'file':str(out)}})
        self.assertEqual(a['original_hash'],digest);self.assertEqual(a['source_id'],'asset-one')
        self.assertEqual(self.mem.details(a)['reliable_text'],['晚安'])
        await self.mem.register_conversion(src,crop,'asset-one')
        c=await self.mem.describe_transport({'type':'image','data':{'file':str(crop)}})
        self.assertNotIn('original_hash',c)

    async def test_only_confirmed_actual_automatic_images_count_and_restart_order(self):
        for n in reversed(range(12)):
            digest=hashlib.sha256(str(n).encode()).hexdigest();self.mem.store.put('match:'+str(n),{'content_hash':digest})
            row={'id':str(n),'origin':'p:FriendMessage:1','state':'confirmed','at':n+1,'emoji_match':str(n),'attachments':[{'kind':'image','content_hash':digest}]}
            self.mem.confirmed(row,recovery=True)
        recent=self.mem.store.rows['recent:p:FriendMessage:1']['hashes']
        self.assertEqual(recent,[hashlib.sha256(str(n).encode()).hexdigest() for n in range(2,12)])
        before=copy.deepcopy(recent)
        for state,parts in [('failed',[{'kind':'image','content_hash':'a'*64}]),('unknown',[{'kind':'image','content_hash':'a'*64}]),('confirmed',[])]:
            self.mem.store.put('match:x',{'content_hash':'a'*64})
            self.mem.confirmed({'id':'x','origin':'p:FriendMessage:1','state':state,'at':20,'emoji_match':'x','attachments':parts})
        self.assertEqual(self.mem.store.rows['recent:p:FriendMessage:1']['hashes'],before)

    async def test_feedback_preserves_image_and_does_not_change_persona(self):
        event=Event();ident=self.mem.match_record(event,content_hash='a'*64,final_text='晚安',semantic=semantic())
        result=self.mem.feedback(ident,'disable')
        self.assertFalse(result['asset_deleted']);self.assertFalse(result['persona_changed'])
        self.assertEqual(self.mem.candidates([item()],event.unified_msg_origin),[])

    async def test_plural_recall_and_quoted_message_scope(self):
        from astrbot.api.message_components import Reply
        rows=[]
        for n in range(3):
            digest=str(n)*64;sem=semantic(content_hash=digest);sem['description']='图片'+str(n)
            self.mem.store.put(digest,{'hash':digest,'semantic':sem})
            rows.append({'message_id':str(n),'sender':'1','at':n,'attachments':[{'kind':'image','content_hash':digest}]})
        self.plugin._delivery=SimpleNamespace(recent=lambda *a,**k:rows,store=SimpleNamespace(select=lambda **k:[rows[0]]))
        event=Event('刚才这几张图什么意思');event.get_messages=lambda:[]
        req=SimpleNamespace(system_prompt='',conversation=SimpleNamespace(cid='c'),image_urls=[])
        await self.mem.enrich(event,req)
        for n in range(3):self.assertIn('图片'+str(n),req.system_prompt)
        event.get_messages=lambda:[Reply(id='0')];req.system_prompt=''
        await self.mem.enrich(event,req);self.assertIn('图片0',req.system_prompt);self.assertNotIn('图片2',req.system_prompt)

class FinalModel(unittest.IsolatedAsyncioTestCase):
    async def test_no_input_model_one_final_call_and_context_evidence(self):
        p=object.__new__(Main);p.config={};p._audit=lambda *a:None;p._work=None
        p._generate_json=AsyncMock(return_value=json.dumps(decision('哈哈笑死')))
        e=Event();pic=item('amusement','哈哈')
        self.assertIsNone(p.prepare_emoji_selection(e,[pic]));p._generate_json.assert_not_awaited()
        task=p.prepare_emoji_selection(e,[pic],final_text='哈哈笑死')
        result=await task;self.assertEqual(result['image_ids'],['one'])
        self.assertIs(p.prepare_emoji_selection(e,[pic],final_text='新的正文'),task)
        self.assertEqual(p._generate_json.await_count,1)
        data=p._generate_json.await_args.args[1]
        self.assertEqual(data['final_text'],'哈哈笑死');self.assertIn('semantic',data['candidates'][0])

    async def test_exact_goodnight_no_model(self):
        p=object.__new__(Main);p.config={};p._audit=lambda *a:None;p._work=None;p._generate_json=AsyncMock()
        result=await p.prepare_emoji_selection(Event('睡啦'),[item()],final_text='晚安')
        self.assertEqual(result['source'],'local_exact');p._generate_json.assert_not_awaited()
