import asyncio,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_maintenance import Main
from test_round9_exchange import Event
from data.plugins.astrbot_plugin_smart_imagechat_hub.main import SmartImageSenderPlugin
from data.plugins.astrbot_plugin_bili_scroll.main import Main as Bili

class Copies(unittest.IsolatedAsyncioTestCase):
    def test_soft_gallery_request_does_not_confuse_complaint_or_generation(self):
        from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.image_intent import is_library_request
        self.assertTrue(is_library_request('这微笑算交接完了，该有离职表情包了吧'))
        from data.plugins.astrbot_plugin_proactive_agent.exchange_state import protected
        self.assertTrue(protected('这微笑算交接完了，该有离职表情包了吧'))
        self.assertTrue(is_library_request('应该有个离职表情包吧'))
        for text in ('不该有离职表情包','不应该有离职表情包','不要再发表情包了','这个表情包真好笑','生成一张离职表情包'):
            self.assertFalse(is_library_request(text),text)
    async def test_gallery_empty_missing_search_and_preparation_failure_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'picture.png';path.write_bytes(b'fixture')
            for expected in ('empty','unmatched','search_failed','send_failed'):
                with self.subTest(expected=expected):
                    p=SmartImageSenderPlugin.__new__(SmartImageSenderPlugin);p.config={}
                    p._cfg_bool=lambda _:True;p._has_request_keyword=lambda _:True;p._advance_latency_turn=Mock()
                    p._sync_library=AsyncMock(side_effect=OSError('synthetic index failure') if expected=='search_failed' else None)
                    item={'id':'a','rel_path':'picture.png'};p._library_candidates=lambda:[] if expected=='empty' else [item]
                    p._rank_search_candidates=lambda text,items:({},items)
                    p._analyze_request=AsyncMock(return_value={'matched':False,'caption':'找到啦，现在就发给你'});p._select_image=lambda *_:item if expected=='send_failed' else None
                    p._abs_plugin_data_path=lambda _:path;p._tags_from_item=lambda _:[]
                    p._prepare_send_image_path=AsyncMock(side_effect=OSError('synthetic conversion failure'));p._cleanup_temp_paths=Mock()
                    e=Event('找一张表情包');e.should_call_llm=Mock();e.plain_result=lambda text:text
                    with patch('data.plugins.astrbot_plugin_smart_imagechat_hub.main._is_explicit_user_search_wake',return_value=True),patch('data.plugins.astrbot_plugin_proactive_agent.persona_authority.plugin_role',new=AsyncMock(return_value=('',{}))):
                        outputs=[r async for r in p.on_wake_message(e)]
                    self.assertEqual(e.get_extra('qqbot_gallery_result'),expected);self.assertEqual(len(outputs),1);self.assertTrue(outputs[0])
                    self.assertNotIn('找到啦，现在就发给你',outputs[0])
                    if expected in {'empty','search_failed'}:p._analyze_request.assert_not_called()

    async def test_gallery_one_call_caption_belongs_to_actual_image_and_no_followup(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'picture.png';path.write_bytes(b'fixture')
            p=SmartImageSenderPlugin.__new__(SmartImageSenderPlugin)
            p.config={}
            p._cfg_bool=lambda key:key!='use_custom_reply';p._cfg_float=lambda key:0
            p._has_request_keyword=lambda _:True;p._advance_latency_turn=Mock();p._sync_library=AsyncMock()
            candidates=[{'id':'a','rel_path':'picture.png'},{'id':'b','rel_path':'picture.png'}]
            p._rank_search_candidates=lambda *args:({},candidates);p._library_candidates=lambda:candidates
            p._analyze_request=AsyncMock(return_value={'matched':True,'image_ids':['a','b'],'image_id':'a','caption_image_id':'a','caption':'只有a图上的鱼','confidence':1})
            p._select_image=lambda *_:candidates[1];p._abs_plugin_data_path=lambda _:path
            p._prepare_send_image_path=AsyncMock(return_value=(path,[]));p._defer_send_image_style_cleanup=lambda e,paths:paths
            p._after_plugin_sent_image_for_meme_combat=AsyncMock();p._cleanup_temp_paths=Mock()
            p._request_llm_with_persona=AsyncMock(side_effect=AssertionError('extra caption model call'))
            e=Event('找一张表情包');e.should_call_llm=Mock();e.chain_result=lambda parts:parts;e.plain_result=lambda text:text
            with patch('data.plugins.astrbot_plugin_smart_imagechat_hub.main._is_explicit_user_search_wake',return_value=True),patch('data.plugins.astrbot_plugin_proactive_agent.persona_authority.plugin_role',new=AsyncMock(return_value=('',{}))):
                outputs=[r async for r in p.on_wake_message(e)]
            self.assertEqual(len(outputs),1);self.assertEqual([type(x).__name__ for x in outputs[0]],['Image'])
            self.assertEqual(p._analyze_request.await_count,1);p._request_llm_with_persona.assert_not_called()
            self.assertEqual(e.get_extra('qqbot_gallery_result'),'found')
            self.assertEqual(p._caption_for_image(candidates[0],p._analyze_request.return_value),'只有a图上的鱼')

    async def test_bili_recent_five_confirmed_only_private_cache_isolated(self):
        p=Bili.__new__(Bili);p.values=lambda:{'bait':False}
        provider=SimpleNamespace(provider_config={'model':'test'},text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='这个角色怎么还能这样整活')))
        p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        p._share_store=SimpleNamespace(rows={str(i):{'origin':'test:GroupMessage:a','state':'confirmed','at':i,'receipts':[{'state':'confirmed','text':'会话私有台词'+str(i)}]} for i in range(7)})
        p._share_store.rows['not-sent']={'origin':'test:GroupMessage:a','state':'unknown','at':999,'receipts':[{'state':'unknown','text':'不能当作已说过'}]}
        item={'title':'测试动画','url':'https://www.bilibili.com/video/BV1xx411c7mD','selection_mode':'chishi'}
        with patch('data.plugins.astrbot_plugin_proactive_agent.persona_authority.plugin_role',new=AsyncMock(return_value=('虚拟角色',{}))):
            await p.make_copy(item,'test:GroupMessage:a')
            await p.make_copy(item,'test:GroupMessage:b')
            await p.make_copy(item,'test:GroupMessage:c')
        self.assertEqual(provider.text_chat.await_count,2)
        private=provider.text_chat.await_args_list[0].kwargs['system_prompt'];public=provider.text_chat.await_args_list[1].kwargs['system_prompt']
        self.assertIn('会话私有台词6',private);self.assertNotIn('会话私有台词1',private);self.assertNotIn('不能当作已说过',private)
        self.assertNotIn('会话私有',public);self.assertNotIn('这份精神食粮，建议',public)

    async def test_old_bili_receipt_ids_recover_only_exact_confirmed_same_origin(self):
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
        with tempfile.TemporaryDirectory() as folder:
            ledger=DeliveryLedger(Path(folder)/'ledger.db');await ledger.start()
            try:
                origin='test:GroupMessage:a'
                a=ledger.begin(origin,None,'旧的教学安利 https://www.bilibili.com/video/BV1xx411c7mD',[]);ledger.result(a,'confirmed','old-receipt')
                other=ledger.begin('test:GroupMessage:b',None,'别群私有句子',[]);ledger.result(other,'confirmed','old-receipt')
                recent=ledger.begin(origin,None,'附近时间但无关的句子',[]);ledger.result(recent,'confirmed','nearby')
                p=Bili.__new__(Bili);p.values=lambda:{'bait':False}
                provider=SimpleNamespace(provider_config={'model':'test'},text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='这个角色怎么还能这样整活')))
                p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider),get_registered_star=lambda _:SimpleNamespace(activated=True,star_cls=SimpleNamespace(_delivery=ledger)))
                p._share_store=SimpleNamespace(rows={'old':{'origin':origin,'state':'confirmed','at':1,'receipts':[{'state':'confirmed','message_id':'old-receipt'}]}})
                with patch('data.plugins.astrbot_plugin_proactive_agent.persona_authority.plugin_role',new=AsyncMock(return_value=('虚拟角色',{}))):
                    await p.make_copy({'title':'测试动画','url':'https://www.bilibili.com/video/BV1xx411c7mD','selection_mode':'chishi'},origin)
                system=provider.text_chat.await_args.kwargs['system_prompt']
                self.assertIn('旧的教学安利',system);self.assertNotIn('别群私有句子',system);self.assertNotIn('附近时间但无关',system)
            finally:await ledger.close()
