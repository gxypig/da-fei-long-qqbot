import asyncio,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from test_round8_visual import Visual,Event
from data.plugins.astrbot_plugin_proactive_agent.incoming_cache import IncomingCache

class Continuity(Visual):
    async def prepare_cache(self):
        self.wall=time.time();self.cache=IncomingCache(Path(self.tmp.name)/'cache',clock=lambda:self.wall)
        await self.cache.start();self.cache.stop();self.service.cache=self.cache
    async def remember(self,mid='photo',sender='one',origin='bot:GroupMessage:g'):
        first=Event(images=[self.image()],mid=mid,sender=sender,origin=origin)
        row=self.service.observe(first)
        row['time']=self.wall
        path=await self.cache.retain(self.path,binding={k:row[k] for k in ('id','sender','time','text')}|{'origin':origin})
        self.service.captured(first,first.get_messages()[1],path)
        return first,path
    async def test_seven_second_elliptical_followup_recovers_received_original(self):
        self.service.observe(Event(images=[self.image()],mid='photo'));self.now+=7.2
        request=await self.enrich(Event('给你装上有没有搞头？',mid='question'))
        self.assertEqual(len(request.image_urls),1)
        self.assertIn('photo',str(request.extra_user_content_parts))
    async def test_minutes_later_and_restart_use_same_sender_one_hour_binding(self):
        await self.prepare_cache();_,path=await self.remember();self.now+=148;self.wall+=148
        self.service.rows.clear()
        reopened=IncomingCache(self.cache.root,clock=lambda:self.wall);await reopened.start();reopened.stop();self.service.cache=reopened
        with patch('data.plugins.astrbot_plugin_proactive_agent.incoming_visual.time.time',return_value=self.wall):
            request=await self.enrich(Event('给你装上有没有搞头？',mid='question'))
        self.assertEqual(request.image_urls,[path])
    async def test_cache_never_crosses_private_origin_or_other_group(self):
        await self.prepare_cache();await self.remember();self.service.rows.clear()
        for sender,origin in [('two','bot:FriendMessage:two'),('one','bot:GroupMessage:other')]:
            self.assertEqual((await self.enrich(Event('有没有搞头',sender=sender,origin=origin))).image_urls,[])
    async def test_one_hour_expiry_and_forget_remove_reference_without_gallery_delete(self):
        await self.prepare_cache();_,path=await self.remember();self.service.rows.clear()
        self.wall+=3601
        self.assertEqual(await self.cache.recent('bot:GroupMessage:g','one'),[])
        await self.cache.cleanup();self.assertFalse(Path(path).exists());self.assertTrue(self.path.exists())
        self.assertFalse(any(row.get('bindings') for row in self.cache.rows.values()))
    async def test_context_reset_forgets_only_target_origin(self):
        await self.prepare_cache();await self.remember();await self.remember('other',origin='bot:GroupMessage:other')
        await self.cache.forget_context('bot:GroupMessage:g')
        self.assertEqual(await self.cache.recent('bot:GroupMessage:g','one'),[])
        self.assertEqual(len(await self.cache.recent('bot:GroupMessage:other','one')),1)
    async def test_reset_during_waiting_binding_cannot_restore_discarded_context(self):
        await self.prepare_cache();valid=[True]
        await self.cache.lock.acquire()
        task=asyncio.create_task(self.cache.retain(self.path,binding={'origin':'bot:g','sender':'one','id':'x','time':self.wall},binding_guard=lambda:valid[0]))
        await asyncio.sleep(0);valid[0]=False;self.cache.lock.release();await task
        self.assertEqual(await self.cache.recent('bot:g','one'),[])
    async def test_ordinary_text_does_not_read_persistent_images(self):
        await self.prepare_cache();await self.remember();self.service.rows.clear()
        with patch.object(self.cache,'recent',side_effect=AssertionError('ordinary text must not scan')):
            self.assertEqual((await self.enrich(Event('今天出门买了菜'))).image_urls,[])
    async def test_topic_break_survives_restart_and_keeps_newer_images(self):
        await self.prepare_cache();await self.remember();self.wall+=1
        with patch('data.plugins.astrbot_plugin_proactive_agent.incoming_visual.time.time',return_value=self.wall):
            self.service.observe(Event('换个话题',mid='break'))
        await asyncio.gather(*self.service.cleanup_tasks)
        reopened=IncomingCache(self.cache.root,clock=lambda:self.wall);await reopened.start();reopened.stop()
        self.assertEqual(await reopened.recent('bot:GroupMessage:g','one'),[])
        self.wall+=1;await self.remember('new')
        await self.cache.forget_context('bot:GroupMessage:g','one',before=self.wall-1)
        self.assertEqual([r['id'] for r in await self.cache.recent('bot:GroupMessage:g','one')],['new'])
    async def test_independent_media_resolution_overlaps_instead_of_serial_wait(self):
        images=[self.image() for _ in range(3)];started=[];active=[0];maximum=[0]
        async def resolve(component):
            active[0]+=1;maximum[0]=max(maximum[0],active[0]);started.append(time.monotonic())
            await asyncio.sleep(.08);active[0]-=1;return str(self.path)
        with patch(type(images[0]).__module__+'.Image.convert_to_file_path',resolve):
            await self.enrich(Event('看看这几张图片',images=images))
        self.assertEqual(maximum[0],3)

class ResetHooks(unittest.IsolatedAsyncioTestCase):
    async def test_first_conversation_retains_photo_but_reset_and_clear_forget(self):
        from types import SimpleNamespace
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import install
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);photo=root/'photo.png';photo.write_bytes(b'image fixture')
            manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value=None),
                new_conversation=AsyncMock(return_value='cid'),switch_conversation=AsyncMock(),update_conversation=AsyncMock())
            plugin=SimpleNamespace(_data_dir=root,_cfg=lambda k,d:d,context=SimpleNamespace(conversation_manager=manager))
            restore=install(plugin);service=plugin._incoming_visual
            try:
                await service.cache_startup
                binding={'id':'first','origin':'bot:g','sender':'one','time':time.time()}
                await service.cache.retain(photo,binding=binding)
                await manager.new_conversation('bot:g')
                self.assertEqual(len(await service.cache.recent('bot:g','one')),1)
                manager.get_curr_conversation_id.return_value='cid'
                await manager.new_conversation(unified_msg_origin='bot:g')
                self.assertEqual(await service.cache.recent('bot:g','one'),[])
                await service.cache.retain(photo,binding=binding)
                await manager.update_conversation('bot:g','cid',history=[])
                self.assertEqual(await service.cache.recent('bot:g','one'),[])
            finally:
                restore()
                if service.cache.task:await asyncio.gather(service.cache.task,return_exceptions=True)

if __name__=='__main__':unittest.main()
