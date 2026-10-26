import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_maintenance import Main as ProactiveMain
from data.plugins.astrbot_plugin_bili_scroll.account import AccountSession, CredentialStore, sign_dm
from data.plugins.astrbot_plugin_bili_scroll.actions import AccountActions, Ledger
from data.plugins.astrbot_plugin_bili_scroll.main import FIELDS, validate, Main
from data.plugins.astrbot_plugin_bili_scroll.client import BiliClient, BiliUnavailable

COOKIES = {'SESSDATA': 'test-session', 'bili_jct': 'test-csrf', 'DedeUserID': '100'}
ITEM = {'bvid': 'BV1xx411c7mD', 'aid': 123, 'title': '测试抽象视频', 'url': 'https://www.bilibili.com/video/BV1xx411c7mD'}


class Sessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'login.json'
        self.a = AccountSession(self.path)

    async def asyncTearDown(self):
        await self.a.close()
        self.tmp.cleanup()

    def test_store_roundtrip_and_no_plain_windows_secrets(self):
        import os
        self.a.store.save(COOKIES)
        self.assertEqual(self.a.store.load(), COOKIES)
        if os.name == 'nt':
            self.assertNotIn('test-session', self.path.read_text())
        else:
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.a.store.clear()
        self.assertFalse(self.path.exists())

    async def test_qr_wait_scan_success_never_returns_cookies(self):
        self.a._http = AsyncMock(side_effect=[
            ({'code': 0, 'data': {'url': 'https://account.bilibili.com/h5/account-h5/auth/scan-web', 'qrcode_key': 'secret-qr-key'}}, {}),
            ({'code': 0, 'data': {'code': 86101}}, {}),
            ({'code': 0, 'data': {'code': 86090}}, {}),
            ({'code': 0, 'data': {'code': 0}}, COOKIES),
            ({'code': 0, 'data': {'isLogin': True, 'mid': 100, 'uname': '测试账号'}}, {})])
        qr = await self.a.login_start()
        self.assertTrue(qr['image'].startswith('data:image/png;base64,'))
        self.assertNotIn('secret-qr-key', json.dumps(qr))
        self.assertEqual((await self.a.login_poll())['state'], 'waiting')
        self.assertEqual((await self.a.login_poll())['state'], 'scanned')
        result = await self.a.login_poll()
        self.assertEqual(result['state'], 'ok')
        self.assertTrue(self.a.ready)
        self.assertNotIn('test-session', json.dumps(result))
        self.assertEqual(self.a.store.load(), COOKIES)
        await self.a.logout()
        self.assertFalse(self.a.ready)
        self.assertFalse(self.path.exists())

    async def test_invalid_qr_origin_and_account_mismatch_not_saved(self):
        self.a._http = AsyncMock(return_value=({'code': 0, 'data': {'url': 'https://evil.invalid/', 'qrcode_key': 'x'}}, {}))
        with self.assertRaises(BiliUnavailable):
            await self.a.login_start()
        self.a.qr = {'key': 'x', 'expires': time.monotonic() + 60}
        self.a._http = AsyncMock(side_effect=[({'code': 0, 'data': {'code': 0}}, COOKIES),
            ({'code': 0, 'data': {'isLogin': True, 'mid': 200}}, {})])
        with self.assertRaises(BiliUnavailable):
            await self.a.login_poll()
        self.assertFalse(self.path.exists())

    async def test_expired_and_incomplete_login(self):
        self.assertEqual((await self.a.login_poll())['state'], 'expired')
        self.a.qr = {'key': 'x', 'expires': time.monotonic() + 60}
        self.a._http = AsyncMock(return_value=({'code': 0, 'data': {'code': 0}}, {'DedeUserID': '100'}))
        with self.assertRaises(BiliUnavailable):
            await self.a.login_poll()
        self.assertFalse(self.path.exists())

    async def test_anonymous_does_not_send_cookie_and_unauthorized_path_blocked(self):
        self.a.cookies = dict(COOKIES)
        self.a._http = AsyncMock(return_value=({'code': 0, 'data': {}}, {}))
        await self.a.get('/x/v2/reply', {}, anonymous=True)
        self.assertEqual(self.a._http.call_args.kwargs['cookies'], {})
        await self.a.get('/x/v2/reply', {})
        self.assertEqual(self.a._http.call_args.kwargs['cookies'], COOKIES)
        with self.assertRaises(ValueError):
            await self.a.post('/x/dynamic/feed/create', {}, expected_identity=self.a.identity())

    async def test_identity_and_revocation_before_post_even_after_dm_signing(self):
        self.a.cookies = dict(COOKIES)
        identity = self.a.identity()
        self.a._http = AsyncMock(return_value=({'code': 0, 'data': {}}, {}))
        self.a.get = AsyncMock(return_value={'wbi_img': {'img_url': 'https://i/a' + 'x'*32 + '.png', 'sub_url': 'https://i/' + 'y'*32 + '.png'}})
        def revoked():
            raise BiliUnavailable('关闭了')
        with self.assertRaises(BiliUnavailable):
            await self.a.post('/web_im/v1/web_im/send_msg', {'msg[sender_uid]': '100', 'msg[receiver_id]': '200'},
                expected_identity=identity, final_check=revoked)
        self.a._http.assert_not_awaited()
        self.a.generation += 1
        with self.assertRaises(BiliUnavailable):
            await self.a.post('/x/v2/reply/add', {}, expected_identity=identity)
        self.a._http.assert_not_awaited()

    async def test_invalid_session_stops_authenticated_access(self):
        self.a.cookies = dict(COOKIES)
        self.a._http = AsyncMock(return_value=({'code': -101}, {}))
        with self.assertRaises(BiliUnavailable):
            await self.a.get('/x/web-interface/nav')
        self.assertFalse(self.a.ready)

    async def test_client_optional_login_cache_identity_and_logout(self):
        self.a.cookies = dict(COOKIES)
        enabled = [False]
        c = BiliClient(self.a, lambda: enabled[0])
        self.assertTrue(c.anonymous)
        original = c.identity()
        enabled[0] = True
        self.assertFalse(c.anonymous)
        self.assertNotEqual(original, c.identity())
        self.a.get = AsyncMock(return_value={'replies': []})
        await c.get('/x/v2/reply', {})
        self.a.get.assert_awaited_once()
        await self.a.logout()
        self.assertTrue(c.anonymous)
        await c.close()


class Writes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = AccountSession(Path(self.tmp.name) / 'account.json')
        self.a.cookies = dict(COOKIES)
        self.config = {**FIELDS, 'quiet_hours': '', 'use_account': True, 'auto_comment': True, 'mention_names': '朋友'}
        self.p = SimpleNamespace(account=self.a, values=lambda: dict(self.config), quiet=lambda: False,
            discover=AsyncMock(return_value={'items': [ITEM], 'warnings': []}),
            make_copy=AsyncMock(return_value='精神食粮，拉上朋友进步\n' + ITEM['url']),
            review_account_text=AsyncMock(side_effect=lambda text: text),
            make_account_reply=AsyncMock(return_value='本龙这叫雨露均沾😅'))
        self.db = {}
        async def get(k, d=None): return copy.deepcopy(self.db.get(k, d))
        async def put(k, v): self.db[k] = copy.deepcopy(v)
        self.p.get_kv_data, self.p.put_kv_data = get, put
        self.x = AccountActions(self.p, Path(self.tmp.name) / 'actions.sqlite3')
        self.x.mentions = AsyncMock(return_value=[{'name': '朋友', 'uid': '200'}])
        self.x.visibility = AsyncMock(return_value='public')
        self.a.post = AsyncMock(return_value={'rpid': 900, 'msg_key': 'm100'})

    async def asyncTearDown(self):
        await self.x.stop()
        await self.a.close()
        self.tmp.cleanup()

    def test_defaults_and_bad_fields(self):
        for key in ('use_account', 'auto_comment', 'reply_comments'):
            self.assertFalse(FIELDS[key])
        self.assertEqual(FIELDS['account_cruise'], 'off')
        for values in ({'dm_mode': 'only'}, {'auto_comment': True}, {'favorite_id': '-5'}, {'comment_per_round': 20}):
            with self.assertRaises(ValueError): validate({**FIELDS, **values})

    async def test_comment_reserves_before_post_dedups_across_restart_and_reviews(self):
        async def post(*args, **kwargs):
            self.assertEqual(self.x.ledger.rows('100')[0]['state'], 'reserved')
            kwargs['final_check']()
            return {'rpid': 900}
        self.a.post.side_effect = post
        await self.x.run()
        self.assertIn('@朋友', self.a.post.call_args.args[1]['message'])
        self.assertEqual(self.a.post.call_args.args[1]['oid'], 123)
        self.p.review_account_text.assert_awaited_once()
        again = AccountActions(self.p, Path(self.tmp.name) / 'actions.sqlite3')
        again.mentions, again.visibility = self.x.mentions, self.x.visibility
        await again.run()
        self.a.post.assert_awaited_once()
        row = self.x.ledger.rows('100')[0]
        self.assertEqual((row['state'], row['detail']['rpid']), ('sent', '900'))

    async def test_timeout_is_recorded_and_never_retried(self):
        self.a.post.side_effect = TimeoutError()
        await self.x.run()
        await self.x.run()
        self.a.post.assert_awaited_once()
        self.assertEqual(self.x.ledger.rows('100')[0]['state'], 'unknown')

    async def test_missing_comment_receipt_never_invented(self):
        self.a.post.return_value = {}
        await self.x.run()
        row = self.x.ledger.rows('100')[0]
        self.assertEqual(row['state'], 'unknown')
        self.assertNotIn('rpid', row['detail'])
        self.x.visibility.assert_not_awaited()

    async def test_visibility_failure_cannot_erase_successful_comment_receipt(self):
        self.x.visibility.side_effect=TimeoutError()
        await self.x.run()
        row=self.x.ledger.rows('100')[0]
        self.assertEqual(row['state'],'sent')
        self.assertEqual(row['detail']['rpid'],'900')
        self.assertEqual(row['detail']['visibility'],'unknown')
        await self.x.run();self.a.post.assert_awaited_once()

    async def test_dm_only_suppresses_comment_and_reply(self):
        self.config.update(dm_mode='only', dm_uid='300', reply_comments=True)
        self.x.replies = AsyncMock()
        await self.x.run()
        self.assertEqual(self.a.post.call_args.args[0], '/web_im/v1/web_im/send_msg')
        self.assertEqual(self.a.post.call_args.args[1]['msg[receiver_id]'], '300')
        self.x.mentions.assert_not_awaited()
        self.x.replies.assert_not_awaited()

    async def test_settings_revoked_during_generation_prevents_all_posts(self):
        async def changed(*args):
            self.config['auto_comment'] = False
            return '文案'
        self.p.make_copy.side_effect = changed
        await self.x.run()
        self.a.post.assert_not_awaited()
        self.assertEqual(self.x.ledger.rows('100'), [])

    async def test_only_owned_favorite_folder_accepted(self):
        self.config['favorite_id'] = '42'
        self.x.folders = AsyncMock(return_value=[{'id': '43', 'title': '自己的'}])
        await self.x.run()
        self.a.post.assert_not_awaited()
        self.x.folders.return_value = [{'id': '42', 'title': '自己的'}]
        await self.x.run()
        self.assertEqual(self.a.post.await_count, 2)
        self.assertEqual(self.a.post.call_args.args[1]['add_media_ids'], '42')
        self.assertEqual(self.a.post.call_args.args[1]['del_media_ids'], '')

    async def test_exact_mention_resolution_not_fuzzy(self):
        real = AccountActions.mentions
        self.a.get = AsyncMock(return_value={'result': [{'uname': '朋友同名后缀', 'mid': 200}]})
        with self.assertRaises(BiliUnavailable): await real(self.x, '朋友')
        self.a.get.return_value = {'result': [{'uname': '朋友', 'mid': 200}]}
        self.assertEqual((await real(self.x, '朋友'))[0]['uid'], '200')

    async def test_visibility_samples_have_distinct_meanings(self):
        async def visible(auth, anon):
            self.a.get = AsyncMock(side_effect=[anon, auth])
            return await AccountActions.visibility(self.x, 123, '900')
        hit = {'replies': [{'rpid': 900}]}
        self.assertEqual(await visible(hit, hit), 'public')
        self.assertEqual(await visible(hit, {}), 'only_self_in_sample')
        self.assertEqual(await visible({}, {}), 'not_in_sample')
        self.assertEqual(await visible(hit, BiliUnavailable()), 'unknown')

    async def test_replies_only_own_root_direct_children_once_per_user(self):
        key = self.x.ledger.reserve('100', 'comment', ITEM['bvid'], '200', {})
        self.x.ledger.finish(key, 'sent', {'rpid': '900', 'aid': 123})
        def child(mid, rpid, parent='900'):
            return {'member': {'mid': mid}, 'rpid': rpid, 'root': 900, 'parent': parent,
                    'ctime': time.time()+1, 'content': {'message': '你又喂我这个😅'}}
        self.a.get = AsyncMock(return_value={'root': {'member': {'mid': 100}},
            'replies': [child(100, 901), child(200, 902, '777'), child(200, 903), child(200, 904)]})
        await self.x.replies(self.p.values(), self.a.identity())
        await self.x.replies(self.p.values(), self.a.identity())
        self.a.post.assert_awaited_once()
        self.assertEqual(self.a.post.call_args.args[1]['parent'], '903')
        self.assertEqual(self.a.post.call_args.args[1]['root'], '900')
        self.a.get.return_value['root']['member']['mid'] = 999
        await self.x.replies(self.p.values(), self.a.identity())
        self.a.post.assert_awaited_once()

    async def test_scheduler_no_immediate_send_one_round_not_per_qq_target(self):
        self.config.update(account_cruise='normal', group_ids='10,11', private_ids='20,21')
        self.x.start = lambda: self.db.update(started=self.db.get('started', 0)+1)
        with patch('data.plugins.astrbot_plugin_bili_scroll.actions.time.time', return_value=1000):
            await self.x.tick()
            self.assertNotIn('started', self.db)
        with patch('data.plugins.astrbot_plugin_bili_scroll.actions.time.time', return_value=2800):
            await self.x.tick()
            await self.x.tick()
        self.assertEqual(self.db['started'], 1)


if __name__ == '__main__':
    unittest.main()
