import asyncio,copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.context_prepare import ContextPrepare
from data.plugins.astrbot_plugin_proactive_agent.context_window import ContextWindow
from data.plugins.astrbot_plugin_proactive_agent.persona_authority import _bound

class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_revision_reuses_result_without_mutating_history_or_cached_output(self):
        cache=ContextPrepare();payload={'messages':[{'role':'user','content':'hello'}]};calls=[]
        def build(_):calls.append(1);return copy.deepcopy(payload)
        first=await cache.prepare(payload,('session',1),build);first['messages'][0]['content']='mutated'
        second=await cache.prepare(payload,('session',1),build)
        self.assertEqual(second['messages'][0]['content'],'hello');self.assertEqual(len(calls),1)
        payload['messages'][0]['content']='edited';await cache.prepare(payload,('session',1),build)
        await cache.prepare(payload,('session',2),build);self.assertEqual(len(calls),3)
    async def test_entries_and_bytes_are_bounded(self):
        cache=ContextPrepare(entries=2,bytes_limit=120)
        for i in range(8):
            payload={'messages':[{'role':'user','content':str(i)*20}]}
            await cache.prepare(payload,i,lambda _,payload=payload:payload)
        self.assertLessEqual(len(cache.cache),2);self.assertLessEqual(cache.bytes,120)
    async def test_disabled_and_unknown_layout_keep_original(self):
        cache=ContextPrepare();payload={'messages':[{'role':'user','content':object()}]}
        self.assertIs(await cache.prepare(payload,1,lambda _:self.fail('must not build invalid messages')),payload)
    async def test_persona_policy_and_summary_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as folder:
            config={'context_window_enabled':True,'context_recent_turns':20,'context_recent_tokens':10000}
            window=ContextWindow(Path(folder)/'summary.json',config.get,lambda:True)
            payload={'messages':[{'role':role,'content':str(i)} for i in range(24) for role in ('user','assistant')]}
            try:
                await window.prepare(None,payload,'cid');await window.prepare(None,payload,'cid')
                self.assertEqual(window.preparation.stats['hits'],1)
                token=_bound.set({'persona_id':'p','persona_version':'v2'})
                try:await window.prepare(None,payload,'cid')
                finally:_bound.reset(token)
                config['context_recent_turns']=21;await window.prepare(None,payload,'cid')
                self.assertEqual(window.preparation.stats['misses'],3)
                self.assertEqual(len(payload['messages']),48)
            finally:await window.close()
