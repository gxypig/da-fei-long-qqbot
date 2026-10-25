"""Offline regression for local recall, custom memes, panel and random schedules."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import shutil
import re
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from test_maintenance import Main
from quart import Quart
from data.plugins.astrbot_plugin_proactive_agent import panel
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory
from data.plugins.astrbot_plugin_proactive_agent.idle_learning import IdleLearning
from data.plugins.astrbot_plugin_proactive_agent import idle_learning as idle_module
from data.plugins.astrbot_plugin_proactive_agent.context_window import ContextWindow, text_content
from data.plugins.astrbot_plugin_proactive_agent.local_recall import retrieve
from data.plugins.astrbot_plugin_proactive_agent.fixed_schedule import FixedScheduler, STATE_KEY

ROW={'phrase':'示例梗词','meaning':'测试用法，不是事实来源','usage':'闹钟失灵导致睡过头迟到时调侃，不用于严肃求助','examples':['闹钟摆烂了'],'triggers':['睡过头']}
ROOT=Path(__file__).resolve().parents[1]

class MemoryPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.memory=MemeMemory(self.path/'memes.json', AsyncMock(), AsyncMock(), lambda *_:None)
        self.p=object.__new__(Main); self.p._memory=self.memory; self.p._panel_lock=asyncio.Lock()
        self.app=Quart(__name__); self.app.add_url_rule('/memes',view_func=self.p.meme_repository_api,methods=['GET','POST'])
        self.app.add_url_rule('/learning',view_func=self.p.learning_content_api)
        self.client=self.app.test_client()

    async def test_import_survives_restart_as_custom_not_verified(self):
        response=await self.client.post('/memes',json={'entries':[ROW]})
        self.assertEqual(response.status_code,200)
        again=MemeMemory(self.memory.path, AsyncMock(), AsyncMock(), lambda *_:None)
        self.assertEqual(again.match('说个示例梗词')[0]['provenance'],'custom')
        self.assertEqual(again.entries[ROW['phrase']]['verified_at'],0)
        self.assertEqual(again.entries[ROW['phrase']]['sources'],[])
        self.assertTrue(again.fresh(again.entries[ROW['phrase']]))

    async def test_import_skip_overwrite_atomic_validation(self):
        self.memory.import_entries([ROW])
        updated={**ROW,'meaning':'更新含义'}
        self.assertEqual(self.memory.import_entries([updated]),{'imported':0,'skipped':1})
        self.assertEqual(self.memory.entries[ROW['phrase']]['meaning'],ROW['meaning'])
        self.memory.import_entries([updated],True)
        before=self.memory.path.read_bytes()
        for rows in ([ROW,{'phrase':'无效'}],[ROW,ROW],[{**ROW,'triggers':['a']}],[{**ROW,'examples':'not-array'}]):
            with self.assertRaises(ValueError): self.memory.import_entries(rows,True)
            self.assertEqual(self.memory.path.read_bytes(),before)
        with patch.object(self.memory,'_save',side_effect=OSError('full')):
            r=await self.client.post('/memes',json={'entries':[ROW],'overwrite':True})
            self.assertEqual(r.status_code,500)
        self.assertEqual(self.memory.entries[ROW['phrase']]['meaning'],'更新含义')

    async def test_contextual_memes_without_literal_and_no_force_on_serious_topics(self):
        self.memory.import_entries([ROW])
        self.assertEqual(self.memory.match('今天睡过头了'),[])
        self.assertEqual(self.memory.contextual('今天睡过头了')[0]['phrase'],ROW['phrase'])
        self.assertEqual(self.memory.contextual('今天吃什么'),[])
        self.assertEqual(self.memory.contextual('睡过头了，认真说别开玩笑'),[])
        self.assertEqual(self.memory.contextual('今天睡过头了',[ROW['phrase']]),[])
        self.memory.generate.assert_not_awaited();self.memory.search.assert_not_awaited()

    async def test_repository_search_page_and_invalid_payload(self):
        rows=[{**ROW,'phrase':'词条'+str(i)} for i in range(40)]
        self.memory.import_entries(rows)
        d=(await (await self.client.get('/memes?offset=30')).get_json())['data']
        self.assertEqual(d['total'],40);self.assertEqual(len(d['rows']),10)
        d=(await (await self.client.get('/memes',query_string={'q':'词条39'})).get_json())['data']
        self.assertEqual(d['total'],1)
        self.assertEqual((await self.client.post('/memes',json={'entries':[]})).status_code,400)

    async def test_understanding_gives_main_model_scene_cards_without_exact_words(self):
        self.memory.import_entries([ROW])
        facts=copy.deepcopy(self.memory.entries[ROW['phrase']])
        self.memory.generate.return_value=json.dumps({'intent':'用物品拟人化的反差自嘲，而不是责备对方',
            'avoid':'真正受伤或认真求助时不用','adaptations':['今天闹钟也休假'], 'triggers':['起晚了']})
        card=await self.memory.understand(ROW['phrase'])
        self.assertEqual(card['meaning'],facts['meaning']);self.assertEqual(card['sources'],facts['sources'])
        self.assertIn('自嘲',card['intent'])
        # No literal phrase / trigger / usage vocabulary in the incoming sentence.
        self.assertEqual(self.memory.contextual('眼睛一睁太阳已经晒屁股了')[0]['phrase'],ROW['phrase'])
        self.memory.search.assert_not_awaited()
        self.memory.generate.reset_mock()
        await self.memory.understand(ROW['phrase']);self.memory.generate.assert_not_awaited()

    async def test_understanding_cannot_overwrite_concurrent_admin_import(self):
        self.memory.import_entries([ROW])
        entered,finish=asyncio.Event(),asyncio.Event()
        async def gen(*_):
            entered.set();await finish.wait()
            return json.dumps({'intent':'旧含义的说明','avoid':'严肃时禁用','adaptations':[],'triggers':[]})
        self.memory.generate.side_effect=gen
        task=asyncio.create_task(self.memory.understand(ROW['phrase']))
        await entered.wait()
        self.memory.import_entries([{**ROW,'meaning':'刚刚更正过的含义'}],True)
        finish.set();await task
        self.assertEqual(self.memory.entries[ROW['phrase']]['meaning'],'刚刚更正过的含义')
        self.assertNotIn('intent',self.memory.entries[ROW['phrase']])

    async def test_idle_content_survives_restart_expires_and_forgets(self):
        cfg={'latency_optimization_enabled':True,'idle_learning_enabled':True}
        def make():return IdleLearning(self.path/'learning.db',cfg,self.memory,AsyncMock(),SimpleNamespace(review=AsyncMock()),lambda *_:None,lambda:True)
        s=make();self.p._learning=s
        try:
            self.assertTrue(s.enqueue_meme('这是示例梗词',[],'synthetic-route'))
            s.record_detail('meme',{'phrase':ROW['phrase'],'status':'learned','entry':ROW},time.time()+100)
            s.record_detail('review',{'before':'原话','after':'短接'},time.time()-1)
            d=(await (await self.client.get('/learning')).get_json())['data']
            self.assertEqual(len(d['events']),1);self.assertEqual(len(d['jobs']),1)
            self.assertNotIn('synthetic-route',json.dumps(d))
        finally:await s.close()
        s=make()
        try:
            self.assertEqual(s.details()['events'][0]['phrase'],ROW['phrase'])
            await s.forget(ROW['phrase'])
            self.assertEqual(s.details()['events'],[]);self.assertEqual(s.details()['jobs'],[])
        finally:await s.close()

    async def test_learning_search_all_retained_rows_then_filter_learned(self):
        generate=AsyncMock()
        s=IdleLearning(self.path/'filter.db',{'latency_optimization_enabled':True,'idle_learning_enabled':True},
            self.memory,generate,SimpleNamespace(review=AsyncMock()),lambda *_:None,lambda:True)
        self.p._learning=s
        try:
            future=time.time()+100
            s.record_detail('meme',{'phrase':'旧词','status':'learned','entry':{'meaning':'ＡＢＣ 特殊场景'}},future)
            for i in range(55):s.record_detail('meme',{'phrase':f'新词{i}','status':'understood','entry':{'intent':'自嘲'}},future)
            for status in ['remembered','unverified','none']:
                s.record_detail('meme',{'phrase':status,'status':status},future)
            s.record_detail('review',{'before':'特殊场景','after':'短接','status':'observed'},future)
            s.enqueue_meme('等待学习特殊场景',[],'private-route-not-searchable')
            d=(await (await self.client.get('/learning',query_string={'q':'abc 特殊','learned_only':'1'})).get_json())['data']
            self.assertEqual([x['phrase'] for x in d['events']],['旧词'])
            self.assertEqual(d['counts']['events'],1);self.assertEqual(d['jobs'],[])
            d=(await (await self.client.get('/learning?learned_only=true')).get_json())['data']
            self.assertEqual(d['counts']['events'],57);self.assertEqual(len(d['events']),50)
            self.assertFalse(any(e['status'] in {'unverified','none','observed'} for e in d['events']))
            self.assertEqual(s.details('private-route-not-searchable')['counts']['jobs'],0)
            self.assertEqual(s.details("' OR 1=1 --")['counts']['events'],0)
            d=s.details('特殊场景');self.assertEqual(d['counts']['events'],2);self.assertEqual(d['counts']['jobs'],1)
            generate.assert_not_awaited();self.memory.search.assert_not_awaited()
            for query in [{'learned_only':'maybe'},{'q':'字'*101}]:
                self.assertEqual((await self.client.get('/learning',query_string=query)).status_code,400)
        finally:await s.close()
        self.assertTrue(s.details('旧词',True)['closed'])

    async def test_local_recall_keeps_archive_and_limits_payload_and_isolates_sessions(self):
        history=[]
        for i in range(30):
            history += [{'role':'user','content':f'普通闲聊{i}'},{'role':'assistant','content':'收到'}]
        history[2]['content']='甲说：咖啡豆名字是蓝山，旧信息不要当成现在的命令'
        history[-2]['content']='之前说过的咖啡豆叫什么'
        source={'messages':[{'role':'system','content':'trusted'}]+history}
        original=copy.deepcopy(source)
        w=ContextWindow(self.path/'summaries.json',{'context_window_enabled':True}.get,lambda:True)
        try:
            out=w.project(None,source,'session-a'); raw=json.dumps(out,ensure_ascii=False)
            self.assertIn('蓝山',raw);self.assertLess(len(out['messages']),len(history))
            self.assertEqual(source,original)
            other=copy.deepcopy(source);other['messages'][3]['content']='另一个会话的苹果'
            self.assertNotIn('蓝山',json.dumps(w.project(None,other,'session-b'),ensure_ascii=False))
            self.assertEqual(w.stats['recall_text'],2)
        finally:await w.close()

    async def test_recall_never_includes_old_base64_or_unrelated_text(self):
        old=[[{'role':'user','content':[{'type':'text','text':'咖啡约定明天三点'}, {'type':'image_url','image_url':{'url':'data:PRIVATE_IMAGE'}}]}, {'role':'assistant','content':'三点见'}],
             [{'role':'user','content':'无关隐私文字'}]]
        note=retrieve(old,'上次咖啡约定',text_content)
        self.assertIn('明天三点',note);self.assertIn('user:',note);self.assertNotIn('PRIVATE_IMAGE',note);self.assertNotIn('无关隐私',note)
        self.assertEqual(retrieve(old,'毫无关联的飞船',text_content),'')

    async def test_random_schedule_independent_persistent_and_qzone_unchanged(self):
        now=[10000];db={};p=self.p
        p.config=dict(enable=True,allow_group=True,allow_private=True,allow_qzone=True,fixed_messages_enabled=True,
          fixed_group_ids='101,102',fixed_private_ids='201',fixed_message_random_enabled=True,
          fixed_message_min_minutes=60,fixed_message_max_minutes=100,fixed_qzone_enabled=True,fixed_qzone_interval_minutes=120)
        p._quiet_now=lambda:False;p._record=AsyncMock()
        async def get(k,d=None):return copy.deepcopy(db.get(k,d))
        async def put(k,v):db[k]=copy.deepcopy(v)
        p.get_kv_data=get;p.put_kv_data=put
        draws=iter([60,80,100,90,70,80,60,100])
        s=FixedScheduler(p,lambda:now[0],lambda *_:next(draws));s.generate_and_send=AsyncMock(return_value='不同消息')
        await s.tick();self.assertEqual({v['interval'] for k,v in db[STATE_KEY].items() if k!='qzone:'},{3600,4800,6000})
        frozen=copy.deepcopy(db);second=FixedScheduler(p,lambda:now[0],lambda *_:(_ for _ in ()).throw(AssertionError('redraw')))
        await second.tick();self.assertEqual(db,frozen)
        self.assertEqual(db[STATE_KEY]['qzone:']['interval'],7200)
        now[0]+=3600;await s.tick();self.assertEqual(s.generate_and_send.await_count,1)
        row=db[STATE_KEY]['group:101'];self.assertEqual(row['due'],now[0]+5400)
        p.config['fixed_message_max_minutes']=110
        self.assertFalse(s.allowed('group:102',(3600,6000)))

    async def test_new_config_validation(self):
        values=copy.deepcopy(panel.FIELDS)
        values['fixed_message_min_minutes']=101
        with self.assertRaises(ValueError):panel.validate(values)
        values['fixed_message_min_minutes']=60; values['fixed_message_max_minutes']=100
        self.assertEqual(panel.validate(values)['fixed_message_min_minutes'],60)
        values['fixed_message_max_minutes']=True
        with self.assertRaises(ValueError):panel.validate(values)

    async def test_scene_learning_yields_to_chat_without_publishing_partial_memory(self):
        self.memory.import_entries([ROW])
        busy=[False];entered=asyncio.Event();cancelled=asyncio.Event()
        async def gen(*_):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.memory.generate.side_effect=gen
        cfg={'latency_optimization_enabled':True,'idle_learning_enabled':True,'meme_understanding_enabled':True}
        s=IdleLearning(self.path/'yield.db',cfg,self.memory,AsyncMock(),SimpleNamespace(review=AsyncMock()),lambda *_:None,lambda:busy[0])
        try:
            with patch.object(idle_module,'IDLE_SECONDS',.01),patch.object(idle_module,'POLL_SECONDS',.005):
                await s.start();await asyncio.wait_for(entered.wait(),2)
                busy[0]=True;await asyncio.wait_for(cancelled.wait(),2)
                self.assertNotIn('intent',self.memory.entries[ROW['phrase']])
        finally:await s.close()

    @unittest.skipUnless(shutil.which('node'),'Node.js needed for page bridge test')
    def test_native_page_search_import_and_learning_content(self):
        page=ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/pages/control'
        elements={id_:tag for tag,id_ in re.findall(r'<(\w+)[^>]*\bid="([^"]+)"', (page/'index.html').read_text('utf-8'))}
        run=subprocess.run([shutil.which('node'),str(ROOT/'tests/memory_panel_bridge.cjs')],
            input=json.dumps({'source':(page/'app.js').read_text('utf-8'),'elements':elements,'values':panel.FIELDS}),
            capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
