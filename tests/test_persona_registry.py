import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.persona_registry import PersonaRegistry,PersonaConflict,PURPOSES,FALLBACKS


class Manager:
    def __init__(self):
        self.rows={k:{'persona_id':k,'system_prompt':v,'begin_dialogs':['原预设'],
            'tools':['tool-a'],'skills':['skill-b'],'custom_error_message':'原错误'} for k,v in [('dragon','旧设定'),('second','另一个人格')]}
    async def get_persona(self,k):return copy.deepcopy(self.rows.get(k))
    async def get_all_personas(self):return list(copy.deepcopy(self.rows).values())
    async def update_persona(self,k,**kwargs):self.rows[k].update(kwargs)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'roles.json';self.manager=Manager()
        self.registry=PersonaRegistry(self.path,self.manager,AsyncMock(),
            lambda:[{'id':'tone','label':'口吻','text':'补充','priority':1}],owner_id='dragon')
        self.registry._ai=AsyncMock(side_effect=self.ai)
        await self.registry.start()
    async def asyncTearDown(self):
        await self.registry.close();self.tmp.cleanup()
    async def ai(self,system,data):
        if 'coverage' in system:return {'master':'合并设定','coverage':[{'source':x['id'],'decision':'included'} for x in data['items']],'conflicts':[],'dialogue_conflicts':[]}
        if 'variants' in data:return {'ok':True,'missing':[],'conflicts':[],'reintroduced_reaction':False}
        return {'variants':dict.fromkeys(PURPOSES,data['master']),'fallbacks':dict(FALLBACKS)}
    async def draft(self,text='新设定',pid='dragon'):
        p=await self.registry.inspect(pid)
        return await self.registry.save_draft(pid,text,p['revision'],p['source_fingerprint'])
    async def start_publish(self,pid='dragon',**kw):
        p=await self.registry.inspect(pid)
        return await self.registry.submit(pid,'publish',p['revision'],p['source_fingerprint'],**kw)
    async def complete(self,job):
        task=self.registry.tasks.get(job['id'])
        if task:await task
        return self.registry.data['jobs'][job['id']]
    async def publish(self,text='新设定'):
        await self.draft(text);return await self.complete(await self.start_publish())

    async def test_publish_preserves_native_permissions_and_dialogues(self):
        before=await self.manager.get_persona('dragon');job=await self.publish()
        self.assertEqual(job['status'],'completed')
        self.assertEqual(await self.manager.get_persona('dragon'),{**before,'system_prompt':'新设定'})
        binding=self.registry.binding('dragon','旧设定')
        self.assertEqual(binding['persona_prompt'],'新设定');self.assertEqual(set(binding['persona_variants']),set(PURPOSES))

    async def test_adapters_inherit_every_master_clause_without_eight_ai_copies(self):
        master='身份：白龙；关系：老友；严肃场景认真帮助；不编造已发图片'
        async def ai(system,data):
            if 'variants' in data:
                self.assertEqual(data['composition'],'master_plus_adapter')
                self.assertEqual(data['master'],master)
                return {'ok':True,'missing':[],'conflicts':[],'reintroduced_reaction':False}
            return {'variants':{purpose:'本用途：'+purpose for purpose in PURPOSES},'fallbacks':dict(FALLBACKS)}
        self.registry._ai=ai;job=await self.publish(master)
        self.assertEqual(job['status'],'completed')
        for purpose,text in self.registry.binding('dragon','native')['persona_variants'].items():
            self.assertTrue(text.startswith(master));self.assertEqual(text.count(master),1)
            self.assertIn('本用途：'+purpose,text)

    async def test_merge_does_not_publish_and_tracks_sources(self):
        p=await self.registry.inspect('dragon')
        j=await self.registry.submit('dragon','merge',p['revision'],p['source_fingerprint']);await self.complete(j)
        self.assertEqual((await self.registry.inspect('dragon'))['draft'],'合并设定')
        self.assertEqual(self.manager.rows['dragon']['system_prompt'],'旧设定')

    async def test_two_personas_sources_remain_separate(self):
        self.assertEqual([s['id'] for s in (await self.registry.sources('second'))['items']],['native'])
        await self.publish()
        self.assertEqual(self.registry.binding('second','另一个人格')['persona_prompt'],'另一个人格')

    async def test_disabled_empty_failed_do_not_fallback(self):
        await self.publish('')
        for pid,status in [('dragon','selected'),('[%None]','disabled'),('dragon','error'),('missing','missing')]:
            self.assertEqual(self.registry.binding(pid,'',status)['persona_prompt'],'')
        self.registry._ai.assert_not_called()

    async def test_cas_saves_conflicting_draft_instead_of_overwrite(self):
        p=await self.registry.inspect('dragon');await self.draft('甲')
        with self.assertRaises(PersonaConflict):await self.registry.save_draft('dragon','乙',p['revision'],p['source_fingerprint'])
        p=await self.registry.inspect('dragon');self.assertEqual(p['draft'],'甲');self.assertEqual(p['conflicts'][-1]['text'],'乙')

    async def test_compile_keeps_old_version_and_pinned_snapshot(self):
        await self.publish('版本一');pinned=self.registry.binding('dragon','native')
        entered=asyncio.Event();resume=asyncio.Event()
        async def ai(system,data):entered.set();await resume.wait();return await self.ai(system,data)
        self.registry._ai=ai;await self.draft('版本二');job=await self.start_publish();await entered.wait()
        self.assertEqual(self.registry.binding('dragon','native')['persona_prompt'],'版本一')
        resume.set();await self.complete(job)
        self.assertEqual(pinned['persona_prompt'],'版本一');self.assertEqual(self.registry.binding('dragon','native')['persona_prompt'],'版本二')

    async def test_native_edit_during_compilation_wins(self):
        entered=asyncio.Event();resume=asyncio.Event()
        async def ai(system,data):entered.set();await resume.wait();return await self.ai(system,data)
        self.registry._ai=ai;await self.draft();job=await self.start_publish();await entered.wait()
        await self.manager.update_persona('dragon',system_prompt='外部编辑')
        resume.set();job=await self.complete(job)
        self.assertEqual(job['status'],'conflict');self.assertEqual(self.manager.rows['dragon']['system_prompt'],'外部编辑')

    async def test_failed_independent_validation_leaves_both_sides_unchanged(self):
        async def ai(system,data):
            if 'variants' in data:return {'ok':False,'missing':['外观'],'reintroduced_reaction':False}
            return await self.ai(system,data)
        self.registry._ai=ai;job=await self.publish()
        self.assertEqual(job['status'],'failed');self.assertEqual(self.manager.rows['dragon']['system_prompt'],'旧设定')
        self.assertIsNone(self.registry._person('dragon')['active'])

    async def test_atomic_commit_failure_compensates_native_and_live(self):
        await self.publish('版本一');before=self.registry.binding('dragon','native');save=self.registry._save;failed=False
        async def fail_once():
            nonlocal failed
            if not failed and self.registry._person('dragon')['draft']=='版本二' and any(j.get('status')=='completed' and j.get('master')=='版本二' for j in self.registry.data['jobs'].values()):
                failed=True;raise OSError('disk full')
            await save()
        self.registry._save=fail_once;job=await self.publish('版本二')
        self.assertTrue(failed);self.assertEqual(job['status'],'failed')
        self.assertEqual(self.manager.rows['dragon']['system_prompt'],'版本一')
        self.assertEqual(self.registry.binding('dragon','native'),before)
        self.assertEqual(self.registry._person('dragon')['active'],before['persona_version'])

    async def test_rollback_is_a_new_version_with_same_validated_text(self):
        first=await self.publish('版本一');await self.publish('版本二');p=await self.registry.inspect('dragon')
        job=await self.registry.submit('dragon','rollback',p['revision'],p['source_fingerprint'],version=first['candidate']['id'])
        job=await self.complete(job)
        self.assertEqual(job['status'],'completed');self.assertNotEqual(job['candidate']['id'],first['candidate']['id'])
        self.assertEqual(self.manager.rows['dragon']['system_prompt'],'版本一')

    async def test_duplicate_request_requires_identical_parameters(self):
        await self.draft();p=await self.registry.inspect('dragon');job=await self.start_publish(request_id='same');await self.complete(job)
        repeated=await self.registry.submit('dragon','publish',p['revision'],p['source_fingerprint'],request_id='same')
        self.assertEqual(job['id'],repeated['id'])
        with self.assertRaises(PersonaConflict):await self.registry.submit('dragon','publish',p['revision'],'changed',request_id='same')

    async def test_recover_after_native_write_is_idempotent(self):
        job=await self.publish();revision=self.registry._person('dragon')['revision'];job['status']='native_updated';await self.registry._save()
        self.registry.gate.close();await self.registry.start()
        self.assertEqual(self.registry._person('dragon')['revision'],revision)
        self.assertEqual(self.registry.data['jobs'][job['id']]['status'],'completed')

    async def test_recover_prepared_without_native_write_does_not_activate(self):
        job=await self.publish();job['status']='prepared'
        self.registry.data['people']['dragon']=copy.deepcopy(job['person_before']);self.manager.rows['dragon']=copy.deepcopy(job['native_before'])
        await self.registry._save();self.registry.gate.close();await self.registry.start()
        self.assertIsNone(self.registry._person('dragon')['active']);self.assertEqual(job['native_before']['system_prompt'],self.registry.binding('dragon','旧设定')['persona_prompt'])

    async def test_native_edit_waits_for_commit_gate(self):
        async with self.registry.gate.hold('dragon'):
            task=asyncio.create_task(self.manager.update_persona('dragon',system_prompt='另一次编辑'))
            await asyncio.sleep(0);self.assertFalse(task.done())
        await task;self.assertEqual(self.manager.rows['dragon']['system_prompt'],'另一次编辑')

    async def test_cancel_compile_preserves_active(self):
        await self.publish('版本一');entered=asyncio.Event()
        async def wait(*a):entered.set();await asyncio.Future()
        self.registry._ai=wait;await self.draft('版本二');job=await self.start_publish();await entered.wait()
        self.registry.tasks[job['id']].cancel();job=await self.complete(job)
        self.assertEqual(job['status'],'failed');self.assertEqual(self.registry.binding('dragon','native')['persona_prompt'],'版本一')

if __name__=='__main__':unittest.main()
