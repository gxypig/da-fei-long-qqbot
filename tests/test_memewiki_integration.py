import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.memewiki_engine import MemeWikiEngine
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory


class MemeWikiIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=self.root/'memes.db'
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute('CREATE TABLE memes(title TEXT PRIMARY KEY,slug TEXT,tags TEXT,usage TEXT,origin TEXT,other TEXT,summary TEXT)')
            db.executemany('INSERT INTO memes VALUES(?,?,?,?,?,?,?)',[
                ('你是什么冠军','champion','','调侃炫耀，不适合认真求助','公开来源','','反问对方有什么资格'),
                ('绷不住了','laugh','','忍不住笑，也可能表达崩溃','公开来源','','无法控制笑或情绪'),
                ('awsl','awsl','','可爱到受不了','公开来源','','夸张地表达喜欢'),
                ('AI','ai','','人工智能讨论','公开来源','','人工智能缩写')])
        self.wiki=MemeWikiEngine(self.db);await self.wiki.start()
        self.generate=AsyncMock(return_value=json.dumps({'phrases':[]}));self.search=AsyncMock()
        self.memory=MemeMemory(self.root/'memory.json',self.generate,self.search,lambda *_:None,wiki=self.wiki)

    async def test_local_primary_lookup_never_calls_model_or_network(self):
        self.wiki.remote=AsyncMock(side_effect=AssertionError('foreground network'))
        rows=self.memory.contextual('这也太离谱了，我绷不住了')
        self.assertEqual(rows[0]['provenance'],'memewiki')
        result=await self.memory.lookup('绷不住了','p:FriendMessage:1')
        self.assertEqual(result['status'],'remembered')
        self.assertEqual(self.memory.entries['绷不住了']['meaning'],'无法控制笑或情绪')
        self.generate.assert_not_called();self.search.assert_not_called();self.wiki.remote.assert_not_called()

    async def test_full_title_not_prefix_or_substring_of_latin_word(self):
        self.assertEqual(self.wiki.match('你是什么'),[])
        self.assertEqual(self.wiki.match('train'),[])
        self.assertEqual(self.wiki.match('这个也太可爱了我awsl了')[0]['phrase'],'awsl')
        self.assertEqual(self.wiki.match('你说你是什么冠军')[0]['phrase'],'你是什么冠军')

    async def test_known_query_skips_detection_unknown_mixture_still_analyzed(self):
        self.assertEqual(await self.memory._detect('绷不住了是什么梗',[],'a'),['绷不住了'])
        self.generate.assert_not_called()
        self.generate.return_value=json.dumps({'phrases':['新造梗']})
        self.assertEqual(await self.memory._detect('绷不住了，新造梗又来了',[],'a'),['新造梗'])
        self.generate.assert_awaited_once()

    async def test_understanding_and_custom_import_survive_primary_dictionary(self):
        await self.memory.lookup('绷不住了','a')
        self.generate.return_value=json.dumps({'intent':'借失控表达忍俊不禁或崩溃','avoid':'对方认真诉苦时别嘲笑',
                                    'adaptations':['差点笑出声'],'triggers':['笑死','心态崩了']})
        await self.memory.understand('绷不住了','a')
        self.assertIn('intent',self.memory.contextual('绷不住了')[0])
        self.memory.import_entries([{'phrase':'绷不住了','meaning':'自定义含义','usage':'自定义场景'}],overwrite=True)
        self.assertEqual(self.memory.contextual('绷不住了')[0]['meaning'],'自定义含义')
        self.assertEqual((await self.memory.lookup('绷不住了','a'))['entry']['meaning'],'自定义含义')

    async def test_combined_catalog_paginates_without_duplicates_or_private_data(self):
        await self.memory.lookup('绷不住了','a')
        self.memory.import_entries([{'phrase':'自造词','meaning':'测试定义','usage':'测试场景'}])
        pages=[self.memory.repository(offset=i,limit=2) for i in (0,2,4)]
        self.assertTrue(all(p['total']==5 for p in pages))
        names=[r['phrase'] for p in pages for r in p['rows']]
        self.assertEqual(len(names),len(set(names)));self.assertEqual(len(names),5)
        self.assertEqual(self.memory.repository('冠军')['total'],1)

    async def test_remote_primary_persists_source_without_second_learning_model(self):
        row=self.wiki.entry({'title':'新造梗','slug':'new','summary':'有来源的定义','usage':'用法'})
        self.wiki.remote=AsyncMock(return_value=row)
        result=await self.memory.lookup('新造梗','a')
        self.assertEqual(result['status'],'learned');self.assertTrue(result['entry']['sources'])
        self.generate.assert_not_called();self.search.assert_not_called()
        reopened=MemeMemory(self.memory.path,self.generate,self.search,lambda *_:None,wiki=self.wiki)
        self.assertEqual(reopened.match('新造梗')[0]['meaning'],'有来源的定义')

    async def test_unavailable_dictionary_does_not_break_old_memory_or_block_reply(self):
        self.memory.import_entries([{'phrase':'自造词','meaning':'已有定义','usage':'已有场景'}])
        self.db.write_bytes(b'broken SQLite')
        self.assertEqual(self.memory.contextual('自造词')[0]['meaning'],'已有定义')
        self.assertTrue(self.wiki.stats['snapshot_ready'])  # Last validated in-memory snapshot still serves chat.
        self.assertEqual(self.memory.repository()['total'],1)

    async def test_dictionary_removed_after_index_load_remains_optional(self):
        self.db.unlink()
        self.assertTrue(self.wiki._read('绷不住了'))
        self.assertTrue(self.wiki.match('绷不住了'))
        self.assertEqual(self.wiki.catalog()['total'],0)

    async def test_catalog_search_uses_primary_definition_with_old_understanding(self):
        self.memory.entries['绷不住了']={'phrase':'绷不住了','meaning':'过期解释','usage':'旧用法',
                                      'intent':'情绪反应','verified_at':1}
        page=self.memory.repository('无法控制笑')
        self.assertEqual(page['total'],1)
        self.assertNotIn('intent',page['rows'][0])  # Unbound old interpretation cannot attach to new source text.
