import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from unittest.mock import MagicMock
from types import SimpleNamespace

import test_round20_memory as fixture
from data.plugins.astrbot_plugin_proactive_agent.article_body import extract_article, article_key
from data.plugins.astrbot_plugin_proactive_agent.safe_reader import read_web
from data.plugins.astrbot_plugin_proactive_agent.browser_reader import authorization_newer,read_browser

M='data.plugins.astrbot_plugin_proactive_agent.'
TIEBA='https://tieba.baidu.com/p/11180200380'
ZHIHU='https://www.zhihu.com/question/536912143/answer/2526139229'


class Body(unittest.TestCase):
    def test_only_post_containers_not_sidebar(self):
        result=extract_article('<title>测试帖</title><aside>推荐广告不应学习</aside><div class="l_post" data-field=\'{"content":{"post_no":1}}\'><div class="d_post_content">楼主实际原文</div></div><div class="l_post"><div class="d_post_content">另一人回复<img alt="作者给出的说明" src="x"></div></div>',TIEBA)
        self.assertNotIn('推荐广告',result['content']);self.assertIn('楼主实际原文',result['content']);self.assertIn('另一人回复',result['content'])
        self.assertEqual(result['image_count'],1);self.assertIn('未识别',result['scope']);self.assertTrue(result['body_verified'])
    def test_answer_identity_does_not_mix_recommendations(self):
        html='<title>问题</title><div class="AnswerItem" data-aid="999"><div class="RichContent-inner">推荐回答</div></div><div class="AnswerItem" data-zop=\'{"itemId":2526139229}\'><div class="RichContent-inner"><span class="RichText">目标回答的实际原文</span></div></div>'
        r=extract_article(html,ZHIHU);self.assertIn('实际原文',r['content']);self.assertNotIn('推荐回答',r['content'])
    def test_modern_tieba_body_comments_and_hidden_login(self):
        html='<title>新帖子</title><div class="pc-pb-box"><div class="image-text"><div class="pb-content-wrap"><p>楼主原文</p><div tid="11180200380"><img data-src="https://tiebapic.baidu.com/a.jpg"></div></div><div class="pb-comment-item"><div class="head-line"><b class="head-name">甲</b></div><div class="comment-content"><div class="pb-rich-text">甲的看法</div></div><div class="pb-lzl-item"><div class="head-line"><b class="head-name">乙</b></div><div class="comment-content"><div class="pb-rich-text">乙的回复</div></div></div></div></div></div><aside>推荐广告</aside><div hidden>登录后查看全部评论</div>'
        r=extract_article(html,TIEBA)
        self.assertIn('楼主原文',r['content']);self.assertIn('甲的观点',r['content']);self.assertEqual(r['content'].count('乙的回复'),1)
        self.assertNotIn('推荐广告',r['content']);self.assertTrue(r['comments_partial'])
        self.assertEqual(r['body_images'],['https://tiebapic.baidu.com/a.jpg'])
    def test_folded_recommendation_does_not_mark_target_as_excerpt(self):
        target='<div class="AnswerItem" data-aid="2526139229"><div class="RichContent-inner">目标完整正文</div></div>'
        recommendation='<div class="AnswerItem" data-aid="999"><div class="RichContent is-collapsed"><div class="RichContent-inner">别的回答</div></div></div>'
        result=extract_article(target+recommendation,ZHIHU)
        self.assertFalse(result['folded']);self.assertNotIn('折叠',result['scope'])
    def test_target_folded_dom_is_reported_without_inventing_missing_text(self):
        html='<div class="AnswerItem" data-aid="2526139229"><div class="RichContent is-collapsed"><div class="RichContent-inner">已加载文字</div></div></div>'
        result=extract_article(html,ZHIHU)
        self.assertTrue(result['folded']);self.assertIn('已加载文字',result['scope'])
    def test_modern_tieba_wrong_thread_id_rejected(self):
        with self.assertRaisesRegex(ValueError,'编号'):
            extract_article('<div class="pc-pb-box"><div class="image-text"><div class="pb-content-wrap"><div tid="12">另一篇原文</div></div></div></div>',TIEBA)
    def test_network_error_is_not_mislabeled_login(self):
        with self.assertRaisesRegex(ValueError,'接口未返回'):
            extract_article('<div class="network-error">网络错误</div><aside>扫码登录</aside>',TIEBA)
    def test_gate_empty_shell_wrong_article_are_not_body(self):
        for html in ('<title>百度安全验证</title>请拖动滑块', '<title>登录知乎</title>扫码登录', '<title>文章</title><nav>网页导航和搜索摘要</nav>', '<link rel="canonical" href="https://www.zhihu.com/question/1/answer/2"><div class="RichContent-inner">错文</div>'):
            with self.subTest(html=html),self.assertRaises(ValueError):extract_article(html,ZHIHU)
    def test_short_answer_image_post_and_truncation_honest(self):
        self.assertIn('是',extract_article('<div class="RichContent-inner">是</div>',ZHIHU)['content'])
        r=extract_article('<div class="d_post_content"><img src="x"></div>',TIEBA)
        self.assertIn('未识别',r['content']);self.assertNotIn('看懂',r['content'])
        r=extract_article('<div class="d_post_content">'+'长文'*9000+'</div>',TIEBA)
        self.assertTrue(r['truncated']);self.assertEqual(len(r['content']),16000)
    def test_unrelated_urls_rejected(self):
        for u in ('https://www.zhihu.com/','https://www.zhihu.com.evil/question/1/answer/2','https://tieba.baidu.com/f?kw=xx'):
            self.assertIsNone(article_key(u))
    def test_authorization_marker_newer_than_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'zhihu';p.mkdir();(p/'status.json').write_text('{"last_success":20}',encoding='utf-8')
            self.assertTrue(authorization_newer(folder,ZHIHU,19));self.assertFalse(authorization_newer(folder,ZHIHU,21))


class Routing(unittest.IsolatedAsyncioTestCase):
    async def test_failure_uses_isolated_reader_and_preserves_original_url(self):
        body={'url':ZHIHU,'body_verified':True,'content':'目标正文'}
        with patch(M+'safe_reader._read_http',AsyncMock(side_effect=ValueError('缺少正文'))),patch(M+'browser_reader.read_browser',AsyncMock(return_value=body)) as browser:
            r=await read_web(ZHIHU,['www.zhihu.com'],AsyncMock(),profile_root='dedicated')
            self.assertEqual(r,body);self.assertEqual(browser.call_args.args[:2],(ZHIHU,'dedicated'))
    async def test_direct_success_does_not_launch_browser(self):
        with patch(M+'safe_reader._read_http',AsyncMock(return_value={'body_verified':True})),patch(M+'browser_reader.read_browser',AsyncMock()) as browser:
            await read_web(ZHIHU,['www.zhihu.com'],AsyncMock(),profile_root='x');browser.assert_not_called()
    async def test_cancellation_and_allowlist_do_not_fallback(self):
        with patch(M+'safe_reader._read_http',AsyncMock(side_effect=asyncio.CancelledError)),patch(M+'browser_reader.read_browser',AsyncMock()) as browser:
            with self.assertRaises(asyncio.CancelledError):await read_web(ZHIHU,['www.zhihu.com'],AsyncMock(),profile_root='x')
            with self.assertRaises(ValueError):await read_web(ZHIHU,['tieba.baidu.com'],AsyncMock(),profile_root='x')
            browser.assert_not_called()
    async def test_quota_error_never_starts_a_browser(self):
        with patch(M+'safe_reader._read_http',AsyncMock(side_effect=ValueError('今日网页读取已达50次'))),patch(M+'browser_reader.read_browser',AsyncMock()) as browser:
            with self.assertRaises(ValueError):await read_web(ZHIHU,['www.zhihu.com'],AsyncMock(),profile_root='x')
            browser.assert_not_called()
    async def test_background_cancel_cleans_only_owned_process_tree(self):
        started=asyncio.Event()
        async def readline():started.set();await asyncio.sleep(30)
        proc=SimpleNamespace(pid=987654,returncode=None,stdout=SimpleNamespace(readline=readline),stdin=SimpleNamespace(write=MagicMock(),drain=AsyncMock()),wait=AsyncMock())
        with patch(M+'browser_reader.asyncio.create_subprocess_exec',AsyncMock(return_value=proc)),patch('psutil.Process',return_value=SimpleNamespace(create_time=lambda:123)),patch(M+'browser_reader.stop_tree') as stop:
            task=asyncio.create_task(read_browser(ZHIHU,'isolated',AsyncMock()));await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        stop.assert_called_once_with(987654,123);proc.wait.assert_awaited_once()


class Integration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.Runtime.asyncSetUp
    asyncTearDown=fixture.Runtime.asyncTearDown
    enable=fixture.Runtime.enable
    async def test_bridge_unavailable_does_not_prevent_memory_start(self):
        with patch.object(self.s.db,'start'),patch.object(self.s.store,'start',AsyncMock()),patch(M+'browser_bridge.start_bridge',AsyncMock(side_effect=OSError('端口占用'))):
            await self.s.start()
        self.assertTrue(self.s.ready);self.assertFalse(self.s.start_error)
    async def test_no_search_excerpt_learning_when_article_fails(self):
        self.enable(active_reading_enabled=True,reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        job=await self.s.queue_read('web',ZHIHU)
        self.s.store.put(job['id'],{**job,'search_excerpt':{'url':ZHIHU,'excerpt':'摘要不是原文'*100}})
        with patch(M+'safe_reader.read_web',AsyncMock(side_effect=ValueError('需要验证'))):await self.s.reading()
        self.assertFalse(self.s.materials);self.p._generate_json.assert_not_called()
        self.assertEqual(self.s.store.rows[job['id']]['state'],'retry')
    async def test_old_excerpt_cache_cannot_replace_body(self):
        self.enable(active_reading_enabled=True,reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        job=await self.s.queue_read('web',ZHIHU)
        self.s.store.put('material:old',dict(type='material',url=ZHIHU,visibility='public',at=self.now,scope='搜索摘要',content='旧摘要',title='旧'))
        with patch(M+'safe_reader.read_web',AsyncMock(return_value={'body_verified':True,'content':'新原文'})) as fetch:
            r=await self.s._read(job)
        fetch.assert_awaited_once();self.assertEqual(r['content'],'新原文')
    async def test_confirmed_body_cache_retains_provenance(self):
        self.enable(active_reading_enabled=True,reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        job=await self.s.queue_read('web',ZHIHU)
        self.s.store.put('material:body',dict(type='material',url=ZHIHU,visibility='public',at=self.now,scope='原文',content='正文',title='题',body_verified=True,reader='dedicated_browser'))
        with patch(M+'safe_reader.read_web',AsyncMock()) as fetch:r=await self.s._read(job)
        fetch.assert_not_called();self.assertTrue(r['body_verified'])
    async def test_cooldown_applies_even_with_search_excerpt(self):
        self.enable(active_reading_enabled=True,reading_web_enabled=True,reading_allowed_domains='www.zhihu.com')
        job=await self.s.queue_read('web',ZHIHU)
        self.s.store.put(job['id'],{**job,'search_excerpt':{'url':ZHIHU,'excerpt':'摘要'*100}})
        self.s.discovery.failure(job,ValueError('需要验证'))
        with patch.object(self.s,'_read',AsyncMock()) as fetch:await self.s.reading()
        fetch.assert_not_called()
    async def test_successful_owner_verification_unblocks_discovery(self):
        self.s.discovery.failure({'kind':'web','value':ZHIHU},ValueError('验证'))
        self.assertTrue(self.s.discovery.host_blocked('www.zhihu.com'))
        path=self.s.db.path.parent/'web-reader/zhihu';path.mkdir(parents=True)
        (path/'status.json').write_text(json.dumps({'last_success':self.now+1}),encoding='utf-8')
        self.assertFalse(self.s.discovery.host_blocked('www.zhihu.com'))


class BrowserNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import data.plugins.astrbot_plugin_proactive_agent.article_body as body
        path=Path(body.__file__).with_name('browser_reader_worker.py')
        spec=importlib.util.spec_from_file_location('tested_browser_worker',path);cls.worker=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'article_body':body}):spec.loader.exec_module(cls.worker)
    def test_only_site_asset_families_allowed(self):
        self.assertTrue(self.worker.allowed_host('static.zhihu.com','zhihu'))
        self.assertTrue(self.worker.allowed_host('static.zhimg.com','zhihu'))
        for host in ('127.0.0.1','localhost','169.254.169.254','zhihu.com.evil.test','evilzhihu.com','www.baidu.com'):
            self.assertFalse(self.worker.allowed_host(host,'zhihu'))
    def test_only_known_read_post_endpoint_allowed(self):
        self.assertTrue(self.worker.readonly_post('https://tieba.baidu.com/c/f/pb/page_pc','tieba'))
        for u in ('https://tieba.baidu.com/c/c/post/add','https://tieba.baidu.com/c/c/agree/opAgree','https://tieba.baidu.com.evil/c/f/pb/page_pc','https://www.zhihu.com/api/v4/answers/2/voters'):
            self.assertFalse(self.worker.readonly_post(u,'tieba'))
    def test_dns_mixed_private_public_rejected(self):
        def row(ip):return socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)
        with patch.object(self.worker.socket,'getaddrinfo',return_value=[row('1.1.1.1'),row('127.0.0.1')]):
            with self.assertRaises(ValueError):self.worker.public_addresses('static.zhihu.com')
        with patch.object(self.worker.socket,'getaddrinfo',return_value=[row('1.1.1.1')]):
            self.assertEqual(self.worker.public_addresses('static.zhihu.com'),['1.1.1.1'])
    def test_profile_exclusive_lock_and_release(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'reader'
            with self.worker.profile_lock(folder):
                with self.assertRaises(RuntimeError):
                    with self.worker.profile_lock(folder):pass
            with self.worker.profile_lock(folder):pass
    def test_existing_system_proxy_is_used_without_exporting_credentials(self):
        with patch.object(self.worker,'getproxies',return_value={'https':'http://127.0.0.1:10878'}):
            self.assertEqual(self.worker.configured_proxy(),('127.0.0.1',10878))
        with patch.object(self.worker,'getproxies',return_value={'https':'http://user:secret@127.0.0.1:10878'}):
            with self.assertRaises(ValueError):self.worker.configured_proxy()
    def test_proxy_tunnel_connects_to_pinned_public_ip(self):
        remote=MagicMock();remote.recv.return_value=b'HTTP/1.1 200 Connection Established\r\n\r\n'
        with patch.object(self.worker.socket,'create_connection',return_value=remote) as connect:
            self.assertIs(self.worker.connect_pinned('1.1.1.1',('127.0.0.1',10878)),remote)
        connect.assert_called_once_with(('127.0.0.1',10878),timeout=6)
        self.assertIn(b'CONNECT 1.1.1.1:443',remote.sendall.call_args.args[0])


if __name__=='__main__':unittest.main()
