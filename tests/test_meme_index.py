import unittest
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.meme_index import MemeIndex,contains

class IndexTests(unittest.TestCase):
    def row(self,phrase='下次一定',**kw):return {'phrase':phrase,'meaning':'表示拖延或者暂时不兑现承诺','usage':'答应以后再做，拖延约定','provenance':'custom',**kw}
    def test_aliases_and_latin_boundaries_are_shared(self):
        index=MemeIndex([self.row('awsl',aliases=['AWSL'],meaning='表达对可爱事物的喜爱',usage='夸可爱')])
        self.assertEqual(index.search('这个awsl了')[0]['phrase'],'awsl')
        self.assertEqual(index.search('rawsling',direct_only=True),[])
        self.assertFalse(contains('train','AI'));self.assertTrue(contains('我AI了','ai'))
    def test_semantics_cover_meaning_usage_tags_and_intent(self):
        index=MemeIndex([self.row(tags=['拖延'],intent='调侃反复拖延的承诺')])
        rows=index.search('这次答应的约定又拖延了')
        self.assertEqual(rows[0]['retrieval_kind'],'context');self.assertIn('语境',rows[0]['retrieval_reason'])
        self.assertTrue(rows[0]['applicable']);self.assertTrue(rows[0]['avoid'])
    def test_no_hash_filler_for_unrelated_scene(self):
        index=MemeIndex([self.row(intent='调侃拖延'),self.row('工资小偷',usage='工作摸鱼',intent='自嘲工作')])
        for text in ['我想吃火锅','新养的猫是白色的','楼下下雨了']:
            self.assertEqual(index.search(text),[],text)
    def test_serious_and_stop_return_no_joke_candidates(self):
        index=MemeIndex([self.row()])
        for text in ['下次一定，认真回答','不要玩梗，下次一定','我很难过，下次一定']:
            self.assertEqual(index.search(text),[])
    def test_ambiguous_one_character_is_not_substring_trigger(self):
        index=MemeIndex([self.row('典')])
        self.assertEqual(index.search('典型案例分析',direct_only=True),[])
        self.assertEqual(index.search('典',direct_only=True)[0]['phrase'],'典')
    def test_returns_at_most_three_with_source_evidence(self):
        index=MemeIndex([self.row('测试'+str(i),triggers=['又拖延'],sources=[{'url':'https://example.test'}]) for i in range(8)])
        rows=index.search('又拖延了');self.assertEqual(len(rows),3)
        self.assertTrue(all(row['sources'] and row['retrieval_reason'] for row in rows))

if __name__=='__main__':unittest.main()
