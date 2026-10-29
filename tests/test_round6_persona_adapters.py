import asyncio,copy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_maintenance import ROOT
from test_dragon_character import event
from test_persona_registry import Manager
from test_conversation import encoded
from data.plugins.astrbot_plugin_proactive_agent.persona_authority import (
    install_persona_runtime,bind_persona,bind_origin,role_text,current_binding,begin_task,end_task,fallback_text)
from data.plugins.astrbot_plugin_proactive_agent.persona_registry import PURPOSES,FALLBACKS
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer
from data.plugins.astrbot_plugin_proactive_agent.dragon_character import character_note,photo_caption
from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_note

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.manager=Manager()
        async def resolve(**kw):
            pid=kw.get('conversation_persona_id') or 'dragon';row=self.manager.rows.get(pid)
            return pid,{'name':pid,'prompt':row['system_prompt']} if row else None,None,False
        self.manager.resolve_selected_persona=AsyncMock(side_effect=resolve)
        self.manager.get_persona_v3_by_id=lambda pid:{'name':pid,'prompt':self.manager.rows[pid]['system_prompt']} if pid in self.manager.rows else None
        self.plugin=SimpleNamespace(_data_dir=Path(self.tmp.name),_generate_json=AsyncMock(),_cfg={}.get,
            context=SimpleNamespace(persona_manager=self.manager,get_config=lambda **kw:{'agent_runner':{'runner_type':'local','config':{'persona':{'persona_id':'dragon'}}}}))
        self.close=await install_persona_runtime(self.plugin)
    async def asyncTearDown(self):await self.close();self.tmp.cleanup()
    def active(self,pid,version,text):
        self.plugin._personas.live[pid]={'active':version,'versions':[{'id':version,'master':text,
            'variants':{k:text+':'+k for k in PURPOSES},'fallbacks':{**FALLBACKS,'image_selfie':'这张是我的自拍','bili_share':'来看《{title}》'}}]}
    async def test_native_selection_is_pinned_once_for_generation_and_review(self):
        self.active('dragon','v1','第一版');e=event('你好');job=SimpleNamespace(event=e,origin=e.unified_msg_origin);token=current_job.set(job)
        try:
            resolved=await self.manager.resolve_selected_persona(umo=job.origin,conversation_persona_id='dragon',platform_name='qq',provider_settings={})
            self.active('dragon','v2','第二版')
            req=SimpleNamespace(system_prompt='\n# Persona Instructions\n\n'+resolved[1]['prompt']+'\n',conversation=SimpleNamespace(persona_id='dragon'));state={}
            await bind_persona(self.plugin,e,req,state)
            self.assertEqual(state['persona_version'],'v1');self.assertEqual(req.system_prompt.count('第一版:chat'),1);self.assertNotIn('第二版',req.system_prompt)
            generate=AsyncMock(return_value=encoded(True));await ReplyReviewer(generate,lambda *a:None).review('说得有道理',origin=job.origin,character=state)
            self.assertIn('第一版:review',generate.call_args.args[0]);self.assertNotIn('第二版',generate.call_args.args[0])
        finally:current_job.reset(token)
    async def test_personas_never_cross_bind(self):
        self.active('dragon','d','龙');self.active('second','s','学者')
        for pid,text in [('dragon','龙'),('second','学者')]:
            state={};req=SimpleNamespace(system_prompt='',conversation=SimpleNamespace(persona_id=pid))
            await bind_persona(self.plugin,event('你好'),req,state)
            self.assertEqual(state['persona_prompt'],text)
            if pid=='second':self.assertNotIn('本龙',character_note(state));self.assertNotIn('白毛',generation_note('selfie',state))
    async def test_disabled_is_not_default_persona(self):
        self.active('dragon','d','龙');state={};req=SimpleNamespace(system_prompt='工具协议',conversation=SimpleNamespace(persona_id='[%None]'))
        await bind_persona(self.plugin,event('你好'),req,state)
        self.assertEqual(state['persona_status'],'disabled');self.assertEqual(req.system_prompt,'工具协议');self.assertEqual(role_text('image',state),'')
    async def test_background_binding_rereads_active_version(self):
        self.active('dragon','d1','第一版');state=await bind_origin(self.plugin,'qzone');self.assertEqual(role_text('qzone',state),'第一版:qzone')
        self.active('dragon','d2','第二版');state2=await bind_origin(self.plugin,'qzone');self.assertEqual(role_text('qzone',state2),'第二版:qzone')
        self.assertEqual(role_text('qzone',state),'第一版:qzone')
    def test_role_fallback_and_image_caption_are_versioned(self):
        self.active('dragon','v','角色');state=self.plugin._personas.binding('dragon','native')
        self.assertEqual(photo_caption('selfie',True,state),'这张是我的自拍')
        self.assertEqual(fallback_text('bili_share',state,title='test'),'来看《test》')
    async def test_broken_selection_does_not_resurrect_stale_bound_role(self):
        self.active('dragon','d','龙');await bind_origin(self.plugin,'qzone')
        state={};await bind_persona(SimpleNamespace(),event('你好'),SimpleNamespace(system_prompt=''),state)
        self.assertEqual(current_binding()['persona_status'],'error');self.assertEqual(current_binding()['persona_prompt'],'')
    async def test_task_boundary_clears_inherited_persona(self):
        await bind_origin(self.plugin,'qzone');old=current_binding();token=begin_task()
        self.assertEqual(current_binding(),{});end_task(token);self.assertEqual(current_binding(),old)
    async def test_second_persona_can_answer_code_without_legacy_role_guard(self):
        state={'persona_status':'selected','persona_prompt':'程序员，可以给出代码','persona_variants':{'review':'程序员，可以给出代码'}}
        generate=AsyncMock(return_value=encoded(True))
        result=await ReplyReviewer(generate,lambda *a:None).review('print(1)',origin='test',recent=['写一行代码'],character=state)
        self.assertEqual(result,'print(1)');self.assertEqual(generate.await_count,1)

if __name__=='__main__':unittest.main()
