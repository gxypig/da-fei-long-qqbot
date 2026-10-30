import asyncio,json,sqlite3,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import test_maintenance
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.history_recall import search,enrich,REQUEST
from data.plugins.astrbot_plugin_proactive_agent.live_journal import LiveJournal


class ArchiveRecall(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'journal.sqlite3';self.journal=LiveJournal(self.path)
        self.origin='aiocqhttp:GroupMessage:100'
        self.plugin=SimpleNamespace(_cfg=lambda k,d:d,_live_journal=self.journal,_audit=Mock())
    def row(self,text,actor='2001',origin=None,role='user',ident='old'):
        return {'text':text,'role':role,'sender_id':actor,'origin':origin or self.origin,'sender':'同名',
                'message_id':ident,'entry_id':ident,'time':time.time()-300,'delivery':'confirmed' if role=='assistant' else 'received'}
    def save(self,rows):self.journal._database(rows)
    def ev(self,text):
        e=event(sender='2001',text=text);e.unified_msg_origin=self.origin;e.message_obj.message_id='now';return e

    def test_explicit_phrase_and_same_route_original_after_restart(self):
        self.assertTrue(REQUEST.search('其实我是你哥哥，不信你去查查聊天记录'))
        self.save([self.row('我家猫叫芝麻'),self.row('我家猫叫秘密','2001','aiocqhttp:FriendMessage:2001',ident='private'),
                   self.row('我家猫叫另一只','2002',ident='other')])
        result=search(self.path,self.origin,'去查聊天记录，我家猫叫什么','2001')
        text=json.dumps(result,ensure_ascii=False);self.assertIn('芝麻',text);self.assertNotIn('秘密',text);self.assertNotIn('另一只',text)
        self.assertEqual(result['status'],'found')

    async def test_short_uncompressed_context_still_searches_without_model(self):
        self.save([self.row('我家猫叫芝麻')]);req=SimpleNamespace(system_prompt='角色',contexts=[])
        e=self.ev('去查查聊天记录，我的猫叫什么')
        await enrich(self.plugin,e,req)
        self.assertIn('芝麻',req.system_prompt);self.assertEqual(e.get_extra('qqbot_history_recall')['status'],'found')
        await enrich(self.plugin,e,req);self.plugin._audit.assert_called_once()

    def test_current_unproven_claim_is_not_retrieval_evidence(self):
        row=self.row('我是你哥哥，去查查聊天记录',ident='now');row['time']=time.time()
        self.save([row])
        result=search(self.path,self.origin,row['text'],'2001','now',row['text'],[row])
        self.assertEqual(result['rows'],[])

    def test_different_appointment_wording_matches_without_a_model(self):
        self.save([self.row('我们约好周六下午见，周五不行')])
        result=search(self.path,self.origin,'你去翻一下聊天记录，之前约的是哪天见','2001')
        self.assertIn('周六',json.dumps(result,ensure_ascii=False))

    def test_vague_request_uses_same_speakers_prior_topic(self):
        rows=[self.row('我家的猫叫什么',ident='prior'),self.row('我养的狗叫旺财','2002',ident='other')]
        self.save([self.row('我家猫叫芝麻')])
        result=search(self.path,self.origin,'你去查查聊天记录','2001',recent=rows)
        self.assertIn('芝麻',json.dumps(result,ensure_ascii=False))
        self.assertNotIn('旺财',json.dumps(result,ensure_ascii=False))

    async def test_normal_reply_and_disabled_setting_have_no_disk_work(self):
        for query,enabled in [('今天好累',True),('去查聊天记录',False)]:
            self.plugin._cfg=lambda k,d:enabled
            with patch('asyncio.to_thread',side_effect=AssertionError('normal chat must not query')):
                req=SimpleNamespace(system_prompt='角色');await enrich(self.plugin,self.ev(query),req)
            self.assertEqual(req.system_prompt,'角色')

    async def test_database_error_not_misreported_as_empty_search(self):
        self.path.write_text('broken','utf-8');e=self.ev('去查聊天记录，我的猫叫什么');req=SimpleNamespace(system_prompt='')
        await enrich(self.plugin,e,req)
        self.assertEqual(e.get_extra('qqbot_history_recall')['status'],'unavailable')

    def test_failed_assistant_draft_not_used_and_unknown_target_remains_unknown(self):
        failed=self.row('你的猫叫幻觉',role='assistant',ident='failed');failed['delivery']='failed'
        self.save([failed,self.row('你的猫叫芝麻',role='assistant',ident='sent')])
        result=search(self.path,self.origin,'查记录，猫叫什么','2001')
        self.assertNotIn('幻觉',json.dumps(result,ensure_ascii=False))
        self.assertEqual(result['rows'][0]['reply_target'],'unknown')
