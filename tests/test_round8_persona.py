import copy, unittest
import test_persona_registry as fixtures
from data.plugins.astrbot_plugin_proactive_agent.persona_authority import functional_style, generation_example


class CentralSources(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegistryTests.asyncSetUp
    asyncTearDown=fixtures.RegistryTests.asyncTearDown
    ai=fixtures.RegistryTests.ai
    draft=fixtures.RegistryTests.draft
    start_publish=fixtures.RegistryTests.start_publish
    complete=fixtures.RegistryTests.complete
    publish=fixtures.RegistryTests.publish
    async def test_new_sources_remain_visible_after_publish_and_are_merged_once(self):
        await self.publish('我自己的性格，不说固定认错台词')
        original=copy.deepcopy(self.registry.supplements())
        self.registry.supplements=lambda: original+[{'id':'new-style','text':'新发现的表达习惯','label':'新增','epoch':2}]
        state=await self.registry.inspect('dragon')
        self.assertEqual(state['source_states']['tone'],'managed')
        self.assertEqual(state['source_states']['new-style'],'new')
        seen=[]
        async def ai(system,data):
            seen.append(copy.deepcopy(data))
            return await self.ai(system,data)
        self.registry._ai=ai
        job=await self.registry.submit('dragon','merge',state['revision'],state['source_fingerprint'])
        self.assertEqual((await self.complete(job))['status'],'completed')
        rows={r['id']:r for r in seen[0]['items']}
        self.assertNotIn('补充',rows['tone']['text'])
        self.assertEqual(rows['new-style']['text'],'新发现的表达习惯')
        self.assertEqual((await self.complete(await self.start_publish()))['status'],'completed')
        state=await self.registry.inspect('dragon')
        self.assertEqual(state['source_states']['new-style'],'managed')
        await self.draft('用户下次编辑仍可正常保存')

    async def test_centralized_runtime_does_not_restore_hidden_style(self):
        await self.publish('简洁自然，按用户编辑的性格回应')
        binding=self.registry.binding('dragon','旧设定')
        self.assertTrue(binding['persona_centralized'])
        self.assertNotIn('本龙说快了',functional_style(binding))
        self.assertEqual(generation_example('你喜欢聊天吗',binding),'')
        self.assertIn('私聊资料不能跨会话',functional_style(binding))
        self.assertIn('不是索图',generation_example('别发这么多表情包',binding))

    async def test_direct_source_change_is_detected_without_native_edit(self):
        await self.publish()
        before=await self.registry.inspect('dragon')
        self.registry.supplements=lambda:[{'id':'tone','label':'口吻','text':'修改后的口吻','priority':1}]
        after=await self.registry.inspect('dragon')
        self.assertNotEqual(before['source_fingerprint'],after['source_fingerprint'])
        self.assertEqual(after['source_states']['tone'],'changed')
