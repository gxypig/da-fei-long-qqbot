import asyncio,copy,json,sqlite3,tempfile,time,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.media_approval import component,converted,wire,changed
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import bind_request,request_scope,approve_once,consume_approval
from data.plugins.astrbot_plugin_proactive_agent.review_history import ReviewHistory
from data.plugins.astrbot_plugin_proactive_agent.memory_panel import register_memory_panel
from data.plugins.astrbot_plugin_proactive_agent.panel_subview import bundled_view
from astrbot.api.message_components import Image
from test_round20_memory import Runtime,ORIGIN

class MediaBinding(unittest.TestCase):
 def test_serialization_maps_without_io_and_replacement_invalidates(self):
  bind_request('test',[],False,{'persona_version':'one'})
  part=Image.fromFileSystem('E:/synthetic/a.png');s={'type':'image','data':{'file':'base64://AAA'}}
  converted(part,s);scope=request_scope('test');required=[component(part)];scope['character']['media_semantics']=required
  approve_once('test','看图');self.assertEqual(wire([s],scope),required)
  other={'type':'image','data':{'file':'base64://BBB'}};actual=wire([other],scope);self.assertTrue(changed(required,actual))
  scope['character']['media_semantics']=actual;self.assertFalse(consume_approval('test','看图'))
 def test_split_subset_is_approved_but_new_attachment_is_not(self):
  required=[component(Image.fromFileSystem('E:/synthetic/a.png')),component(Image.fromFileSystem('E:/synthetic/b.png'))]
  self.assertFalse(changed(required,required[:1]));self.assertTrue(changed(required,[component(Image.fromFileSystem('E:/synthetic/c.png'))]))

class Management(Runtime):
 async def test_disabling_source_removes_derived_context_and_sharing(self):
  self.enable(long_memory_enabled=True,experience_sharing_enabled=True,experience_share_auto=True)
  e=self.life.event('chat_confirmed','喜欢画画',visibility=ORIGIN,completed=True,extra={'extended_material':'memory:1','memory_id':1})
  self.assertTrue(self.s.share_allowed(e,ORIGIN));self.assertTrue(self.life.events(ORIGIN))
  self.enable(long_memory_enabled=False)
  self.assertFalse(self.s.share_allowed(e,ORIGIN));self.assertEqual(self.life.events(ORIGIN),[])
  self.assertNotIn('喜欢画画',self.s.life_projection(ORIGIN,'画画'))
 async def test_forget_removes_corrected_chain_experiences_and_opinions(self):
  from quart import Quart
  from test_round20_memory import memory
  a=self.s.db.remember(memory());b=self.s.db.remember(memory(content='现在喜欢红茶',at=200));event=self.life.event('chat_confirmed','饮品偏好',visibility=ORIGIN,completed=True,extra={'memory_id':a['id']})
  self.life.store.put('reflection:old',{'record_type':'reflection','id':'reflection:old','event_id':event['id'],'at':1,'thought':'喜欢红茶','basis':'饮品','history':[{'previous':'旧偏好'}],'status':'active'})
  app=Quart('forget-test');routes={};self.p.context.register_web_api=lambda path,fn,methods,description:routes.update({path:(fn,methods)})
  register_memory_panel(self.p)
  for i,(path,(fn,methods)) in enumerate(routes.items()):app.add_url_rule(path,str(i),fn,methods=methods)
  response=await app.test_client().post('/astrbot_plugin_proactive_agent/panel_memory_action',json={'kind':'memory','id':b['id'],'action':'forget','version':1,'request_id':'forget-chain'})
  self.assertEqual(response.status_code,200);self.assertEqual(self.life.store.rows['reflection:old']['thought'],'已遗忘');self.assertEqual(self.life.reflection.cache,[])
  self.assertEqual(self.life.store.rows['event:'+event['id']]['summary'],'已遗忘')
 async def test_startup_storage_failure_does_not_disable_bot_and_retries(self):
  from data.plugins.astrbot_plugin_proactive_agent.memory_life import MemoryLife
  s=MemoryLife(self.p,self.root/'failing',clock=lambda:self.now)
  with patch.object(s.db,'start',side_effect=OSError()):await s.start()
  self.assertFalse(s.ready);self.assertEqual(s.snapshot()['error'],'OSError');self.now+=61;await s.step();self.assertTrue(s.ready);await s.close()
 async def test_reflection_late_result_cannot_undo_admin_archive(self):
  self.enable(life_growth_enabled=True);e=self.life.event('chat_confirmed','一起讨论画画',visibility=ORIGIN,completed=True)
  gate=asyncio.Event();entered=asyncio.Event()
  async def slow(*_):entered.set();await gate.wait();return json.dumps({'event_id':e['id'],'quote':'讨论画画','topic':'画画','thought':'这个挺有意思'})
  self.p._generate_json=slow
  with patch('data.plugins.astrbot_plugin_proactive_agent.life_state.role_context',AsyncMock(return_value='')):
   task=asyncio.create_task(self.life.reflection.step());await entered.wait();key='event:'+e['id'];self.life.store.put(key,{**self.life.store.rows[key],'status':'archived','archived_at':self.now});gate.set();await task
  self.assertEqual(self.life.reflection.cache,[])
 async def test_growth_retains_history_and_twelve_active_opinions(self):
  self.enable(life_growth_enabled=True)
  async def answer(system,payload,origin):
   event=payload['event'];return json.dumps({'event_id':event['id'],'quote':event['summary'],'topic':event['summary'],'thought':'这个活动挺有意思'})
  self.p._generate_json=answer
  with patch('data.plugins.astrbot_plugin_proactive_agent.life_state.role_context',AsyncMock(return_value='')):
   for i in range(14):
    self.now+=1900;self.life.event('tool','活动'+str(i),visibility='public',completed=True);await self.life.reflection.step()
  rows=[r for r in self.life.store.rows.values() if r.get('record_type')=='reflection'];self.assertEqual(len(rows),14);self.assertEqual(len(self.life.reflection.cache),12)
  self.assertEqual(sum(r['status']=='archived' for r in rows),2)
 async def test_daily_web_quota_and_cache_do_not_duplicate_requests(self):
  self.enable(active_reading_enabled=True,reading_web_enabled=True,reading_allowed_domains='example.org')
  async def fetch(url,allowed,reserve):await reserve();return {'title':'例子','content':'阅读内容','scope':'web_body','url':url}
  job={'reader':'web','value':'https://example.org/a','visibility':'public'}
  # _read consumes the reader field named kind in persisted jobs.
  job={'kind':'web',**job}
  with patch('data.plugins.astrbot_plugin_proactive_agent.safe_reader.read_web',fetch):
   for i in range(50):await self.s._read(job)
   with self.assertRaises(ValueError):await self.s._read(job)
   self.s.store.put('cached',{'type':'material','url':job['value'],'visibility':'public','at':self.now,'title':'缓存','content':'内容','scope':'web_body'})
   self.assertEqual((await self.s._read(job))['title'],'缓存')
 async def test_parent_worker_cancellation_is_not_swallowed(self):
  self.enable(long_memory_enabled=True);self.observe();self.now+=301;gate=asyncio.Event()
  async def slow(*_):await gate.wait();return self.answer()
  self.p._generate_json=slow;task=asyncio.create_task(self.s.step());await asyncio.sleep(.02);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
  self.assertIsNone(self.s.task)
 async def test_authenticated_routes_pagination_versions_and_draft_payload(self):
  from quart import Quart
  app=Quart('memory-test');routes={};self.p.context.register_web_api=lambda path,fn,methods,description:routes.update({path:(fn,methods)})
  register_memory_panel(self.p)
  for i,(path,(fn,methods)) in enumerate(routes.items()):app.add_url_rule(path,str(i),fn,methods=methods)
  client=app.test_client();base='/astrbot_plugin_proactive_agent/'
  from test_round20_memory import memory
  result=self.s.db.remember(memory());body={'kind':'memory','id':result['id'],'action':'correct','version':1,'content':'喜欢红茶','request_id':'test-request'}
  response=await client.post(base+'panel_memory_action',json=body);self.assertEqual(response.status_code,200)
  self.assertEqual((await client.post(base+'panel_memory_action',json=body)).status_code,200)
  self.assertEqual((await client.post(base+'panel_memory_action',json={**body,'request_id':'other-request'})).status_code,409)
  listing=await (await client.get(base+'panel_memory',query_string={'kind':'memory','q':'红茶'})).get_json();self.assertEqual(len(listing['data']['rows']),1)
  event=self.life.event('tool','读了测试标题',visibility='public',completed=True)
  listing=await (await client.get(base+'panel_memory?kind=experience')).get_json();id_=listing['data']['rows'][0]['id']
  detail=await client.get(base+'panel_memory_detail?kind=experience&id='+id_);self.assertEqual(detail.status_code,200)
  self.assertTrue(id_.startswith('event:'))
 def test_bundled_page_has_no_external_relative_assets(self):
  html=bundled_view('memory');self.assertNotIn('src="./',html);self.assertNotIn('href="./',html)
  self.assertIn('panel_memory_action',html);self.assertIn('qqbot-draft-dirty',html)
  self.assertNotIn('setInterval(',html)

class FeedbackPersistence(unittest.IsolatedAsyncioTestCase):
 async def test_regression_survives_history_expiry(self):
  with tempfile.TemporaryDirectory() as tmp:
   history=ReviewHistory(Path(tmp)/'review.db');await history.start()
   try:
    bind_request('test',[],False,{});id_=history.begin('原稿','原稿',origin='test',state={},mode='chat',settings={},scope=request_scope('test'))
    await history.action({'action':'regression','review_id':id_,'request_id':'regression-once','feedback_version':0},SimpleNamespace())
    await history.store.flush(id_)
    with closing(sqlite3.connect(history.store.path)) as db,db:
     self.assertEqual(db.execute('SELECT COUNT(*) FROM regression_samples').fetchone()[0],1)
     db.execute('UPDATE history SET at=1')
    await asyncio.to_thread(history.store.prune)
    with closing(sqlite3.connect(history.store.path)) as db:
     self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0],0)
     self.assertEqual(db.execute('SELECT COUNT(*) FROM regression_samples').fetchone()[0],1)
   finally:await history.close()
