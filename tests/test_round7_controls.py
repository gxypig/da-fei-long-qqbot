import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from test_fullchain_foundation import event
from test_persona_registry import RegistryTests
from test_data_backup import BackupTests
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
from data.plugins.astrbot_plugin_proactive_agent.conversation_controls import ConversationControls,image_stop
from data.plugins.astrbot_plugin_proactive_agent.data_backup import snapshot,restore,retain

class QueueControls(unittest.IsolatedAsyncioTestCase):
    async def test_error_failure_and_cancel_release_but_confirmed_ttl_does_not_renew(self):
        now=[100.];q=ReplyQueue({}.get,clock=lambda:now[0])
        def make(id_):return q.create(event(text='APIError: quota denied',id_=id_))
        a=make('a');q.submit(a);q.finish(a,'failed')
        b=make('b');q.submit(b);self.assertEqual(b.state,'running');q.finish(b,'confirmed')
        now[0]=200;c=make('c');q.submit(c);self.assertEqual(c.state,'merged')
        now[0]=221;d=make('d');q.submit(d);self.assertEqual(d.state,'running');q.finish(d,'cancelled')
        e=make('e');q.submit(e);self.assertEqual(e.state,'running');q.finish(e,'unknown')
        now[0]=1000;f=make('f');q.submit(f);self.assertEqual(f.state,'merged')
        q.reconcile_echo(e.id,'failed');g=make('g');q.submit(g);self.assertEqual(g.state,'running')
        q.finish(g,'confirmed')

    async def test_empty_mention_same_sender_links_but_new_content_survives(self):
        q=ReplyQueue({}.get);a=q.create(event(text='问题',id_='a'));q.submit(a)
        b=q.create(event(text='',id_='b'));b.event.get_messages=lambda:[__import__('astrbot.api.message_components',fromlist=['At']).At(qq='self')];q.submit(b);self.assertEqual(b.state,'merged');self.assertEqual(b.linked_job,a.id)
        c=q.create(event(text='还有新问题',id_='c'));q.submit(c);self.assertEqual(c.state,'pending')
        q.finish(a,'confirmed');q.finish(c,'confirmed')

    async def test_restart_keeps_unknown_error_but_not_expired_confirmation(self):
        from types import SimpleNamespace
        now=[1000.];q=ReplyQueue({}.get,clock=lambda:now[0])
        original=q.create(event(text='APIError: quota denied',id_='a'))
        row={'job_id':original.id,'origin':original.origin,'sender':original.sender,'source_text':original.text,'state':'unknown','at':1}
        ledger=SimpleNamespace(store=SimpleNamespace(rows={'receipt':row}))
        q.restore_echoes(ledger,wall_clock=lambda:1000)
        b=q.create(event(text=original.text,id_='b'));q.submit(b);self.assertEqual(b.state,'merged')
        row.update(state='confirmed',confirmed_at=1)
        fresh=ReplyQueue({}.get,clock=lambda:1000);fresh.restore_echoes(ledger,wall_clock=lambda:1000)
        c=fresh.create(event(text=original.text,id_='c'));fresh.submit(c);self.assertEqual(c.state,'running');fresh.finish(c,'confirmed')

    async def test_goodbye_requires_real_receipt_and_new_question_reopens(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=DeliveryLedger(Path(folder)/'ledger.db');await ledger.start();controls=ConversationControls(ledger)
            q=ReplyQueue({}.get);q.controls=controls
            a=q.create(event(text='晚安',id_='a'));q.submit(a);id_=ledger.begin(a.origin,None,'晚安',[],a)
            ledger.result(id_,'failed');controls.delivered(ledger.store.rows[id_]);q.finish(a,'failed')
            b=q.create(event(text='晚安',id_='b'));q.submit(b);self.assertEqual(b.state,'running')
            id_=ledger.begin(b.origin,None,'晚安',[],b);ledger.result(id_,'confirmed','m1');controls.delivered(ledger.store.rows[id_]);q.finish(b,'confirmed')
            c=q.create(event(text='晚安',id_='c'));q.submit(c);self.assertEqual(c.state,'ignored')
            d=q.create(event(text='等下这个怎么办',id_='d'));q.submit(d);self.assertEqual(d.state,'running');q.finish(d,'confirmed')
            e=q.create(event(text='晚安',id_='e'));q.submit(e);self.assertEqual(e.state,'running');q.finish(e,'confirmed')
            await ledger.store.close()

    def test_image_complaints_are_not_requests(self):
        from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.image_intent import is_library_request
        for t in ['你这表情包库存清仓发这么多喵','别再发表情包了','图片太多了']:
            self.assertTrue(image_stop(t));self.assertFalse(is_library_request(t))
        self.assertTrue(is_library_request('给我找张开心的表情包'))

    def test_negative_quote_requests_stay_unquoted(self):
        from data.plugins.astrbot_plugin_proactive_agent.conversation_controls import explicit_quote_requested
        for text in ['不要引用回复我','不用引用原消息','别用引用回答','正常回答就行']:
            self.assertFalse(explicit_quote_requested(text),text)
        for text in ['请引用原消息回复','用引用回答','别引用前面那句，请引用这条回复']:
            self.assertTrue(explicit_quote_requested(text),text)

class DraftSafety(RegistryTests):
    async def test_external_edit_does_not_rebase_existing_draft_silently(self):
        from data.plugins.astrbot_plugin_proactive_agent.persona_registry import PersonaConflict
        await self.draft('本地草稿');await self.manager.update_persona('dragon',system_prompt='外部新设定')
        latest=await self.registry.inspect('dragon')
        with self.assertRaises(PersonaConflict):await self.registry.submit('dragon','publish',latest['revision'],latest['source_fingerprint'])
        self.assertEqual(self.registry._person('dragon')['draft'],'本地草稿')
        self.assertTrue(self.registry._person('dragon')['conflicts'])

    async def test_first_publication_keeps_old_binding_until_durable_commit(self):
        old_save=self.registry._save;observed=[]
        async def save():
            if any(j.get('status')=='native_updated' for j in self.registry.data['jobs'].values()):
                observed.append(self.registry.binding('dragon',self.manager.rows['dragon']['system_prompt'])['persona_prompt'])
            await old_save()
        self.registry._save=save;result=await self.publish('新设定')
        self.assertEqual(result['status'],'completed');self.assertEqual(observed,['旧设定'])

class BlobSafety(BackupTests):
    def test_corrupted_cached_blob_is_repaired_before_success(self):
        first=snapshot(self.source,self.backups)
        blob=next((self.backups/'blobs').iterdir());blob.write_bytes(b'corrupt')
        second=snapshot(self.source,self.backups)
        restore(self.backups/'snapshots'/second['snapshot']/'manifest.json',self.root/'restored-second')
        restore(self.backups/'snapshots'/first['snapshot']/'manifest.json',self.root/'restored-first')

    def test_orphans_can_be_collected_without_another_success(self):
        snapshot(self.source,self.backups);orphan=self.backups/'blobs'/('f'*64);orphan.write_bytes(b'orphan')
        retain(self.backups);self.assertFalse(orphan.exists())
