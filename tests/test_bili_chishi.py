import asyncio
import copy
import time
import unittest
from unittest.mock import AsyncMock

from test_sent_routing_bili import ITEM
from data.plugins.astrbot_plugin_bili_scroll.client import BiliClient, BiliUnavailable, video
from data.plugins.astrbot_plugin_bili_scroll.scout import judge, duration_bonus, bounded_batch
from data.plugins.astrbot_plugin_bili_scroll.main import FIELDS, Main, validate


def replies():
    texts = ['@朋友甲', '@朋友乙', '@朋友丙 请你吃', '绷不住了', '评论甲', '评论乙',
             '评论丙', '评论丁', '评论戊', '评论己']
    return [{'rpid': i + 1, 'content': {'message': text}} for i, text in enumerate(texts)]


class Rules(unittest.TestCase):
    def test_original_coefficients_and_hard_gates(self):
        item = video({**ITEM, 'title': '抽象合集', 'duration': 180})
        result = judge(item, replies(), now=item['published'] + 60)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['at_ratio'], .3)
        self.assertEqual(result['pure_at_ratio'], .2)
        self.assertEqual(result['feed_ratio'], .1)
        self.assertEqual(result['score'], 61.1)
        self.assertTrue(result['series'])
        for changed, comments in [(dict(item, duration=44), replies()),
                                  (item, replies()[1:]), (item, [{'content': {'message': '@朋友'}}])]:
            self.assertFalse(judge(changed, comments)['eligible'])

    def test_fraction_not_rounded_up_and_empty_comments_never_qualify(self):
        rows = [{'rpid': i, 'content': {'message': ('@人 ' if i <= 29 else '') + f'请你吃{i}'}}
                for i in range(1, 100)]
        item = video(ITEM)
        self.assertFalse(judge(item, rows)['eligible'])
        self.assertFalse(judge(item, [])['eligible'])
        self.assertEqual(judge(item, [])['sample_count'], 0)

    def test_food_penalty_series_mygo_up_and_comment_duplicates(self):
        item = video(ITEM)
        base = judge(item, replies())
        food = judge(dict(item, title='炸鸡大胃王试吃'), replies())
        self.assertEqual(food['food_penalty'], 30)
        bonus = judge(dict(item, title='MyGO抽象TV', sources=['up:123']), replies())
        self.assertAlmostEqual(bonus['score'] - base['score'], 26)
        duplicate = judge(item, replies() + replies())
        self.assertEqual(duplicate['sample_count'], 10)
        self.assertEqual(duplicate['score'], base['score'])
        self.assertEqual([duration_bonus(x) for x in (30, 45, 120, 900, 901, 1801)], [.15, .65, 1, 1, .8, .3])

    def test_default_mode_and_fallback_copy_are_chishi_without_auto_enable(self):
        self.assertEqual(FIELDS['mode'], 'chishi')
        self.assertFalse(FIELDS['bait'])
        self.assertFalse(FIELDS['auto_enabled'])
        item = video(ITEM)
        item.update(judge(item, replies()))
        text = Main.fallback_copy(item)
        self.assertIn(item['url'], text)
        self.assertNotIn('这标题', text)
        for key, value in [('mode', 'bad'), ('up_list', 'https://wrong'), ('mygo', 'yes')]:
            with self.assertRaises(ValueError):
                validate({**FIELDS, key: value})


class Discovery(unittest.IsolatedAsyncioTestCase):
    async def test_multi_lane_recall_cache_and_per_target_exclusion(self):
        client = BiliClient()
        async def get(path, params):
            if path.endswith('/reply'):
                return {'replies': replies()}
            if path.endswith('/related'):
                return [ITEM]
            if path.endswith('/arc/search'):
                return {'list': {'vlist': [{**ITEM, 'created': int(time.time())}]}}
            return {'result': [ITEM]}
        client.get = AsyncMock(side_effect=get)
        data = await client.discover('', mode='chishi', up_list='123', mygo=True)
        self.assertEqual(len(data['items']), 1)
        self.assertTrue(data['items'][0]['fromup'])
        calls = client.get.call_args_list
        self.assertTrue(any(c.args[1].get('page') == 2 for c in calls))
        self.assertTrue(any(c.args[1].get('order') == 'pubdate' for c in calls))
        self.assertTrue(any('MyGO' in c.args[1].get('keyword', '') for c in calls))
        self.assertTrue(any(c.args[0].endswith('/related') for c in calls))
        count = client.get.await_count
        other = await client.discover('', mode='chishi', up_list='123', mygo=True, excluded=[ITEM['bvid']])
        self.assertFalse(other['items'])
        self.assertEqual(client.get.await_count, count)
        self.assertEqual(len((await client.discover('', mode='chishi', up_list='123'))['items']), 1)

    async def test_missing_comments_no_food_or_popular_substitution(self):
        client = BiliClient()
        async def get(path, params):
            if path.endswith('/reply'):
                raise BiliUnavailable('limited')
            return [ITEM] if path.endswith('/related') else {'result': [ITEM]}
        client.get = AsyncMock(side_effect=get)
        result = await client.discover('抽象', mode='chishi', mygo=False)
        self.assertFalse(result['items'])
        self.assertTrue(result['warnings'])
        self.assertFalse(any(c.args[0].endswith('/popular') for c in client.get.call_args_list))

    async def test_timeout_and_outer_cancellation_clean_all_requests(self):
        done = asyncio.Event()
        async def wait():
            try:
                await asyncio.Future()
            finally:
                done.set()
        self.assertTrue(await bounded_batch([wait()], .01))
        self.assertTrue(done.is_set())
        done.clear()
        task = asyncio.create_task(bounded_batch([wait()], 60))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(done.is_set())
