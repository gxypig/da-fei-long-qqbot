import asyncio
import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_maintenance import ROOT
from PIL import Image
from quart import Quart
from data.plugins.astrbot_plugin_smart_imagechat_hub.main import SmartImageSenderPlugin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.gallery_io import SnapshotWriter, ThumbnailCache, atomic_json


class Gallery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = p = object.__new__(SmartImageSenderPlugin)
        p.data_dir = self.root
        p.index_path = self.root / 'image_index.json'
        p.config = {'library_builder': {'global_tags': ['自定义']}}
        p.context = SimpleNamespace(get_all_providers=lambda: [], get_config=lambda: {})
        p._index = {'version': 1, 'images': {}}
        p._image_digest_cache = {}
        p._lock = asyncio.Lock()
        p._caption_task = None
        p._external_import_state = {}
        p._imagebed_import_state = {}
        p._caption_progress = p._make_caption_progress()
        p._start_caption_background_task = Mock()
        self.app = Quart(__name__)
        with Image.new('RGB', (1600, 900), 'purple') as image:
            stream = io.BytesIO(); image.save(stream, 'PNG'); self.raw = stream.getvalue()
        self.payload = {'filename': '样例.png', 'content_base64': base64.b64encode(self.raw).decode(),
                        'selected_global_tags': ['自定义'], 'request_id': 'one-upload'}

    async def asyncTearDown(self):
        await self.p._flush_gallery_io()

    async def upload(self, payload=None):
        async with self.app.test_request_context('/upload', method='POST', json=payload or self.payload):
            response = await self.p._caption_upload_image_json(payload or self.payload)
            return await response.get_json()

    async def test_upload_is_incremental_durable_and_idempotent(self):
        self.p._discover_uploaded_images = Mock(side_effect=AssertionError('must not scan whole library'))
        first = await self.upload()
        second = await self.upload()
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(first['data']['uploaded'], second['data']['uploaded'])
        self.assertEqual(len(self.p._index['images']), 1)
        saved = json.loads(self.p.index_path.read_text('utf-8'))
        item = next(iter(saved['images'].values()))
        self.assertEqual(item['selected_global_tags'], ['自定义'])
        self.assertEqual(item['upload_request_id'], 'one-upload')
        self.assertEqual((self.root / item['rel_path']).read_bytes(), self.raw)

    async def test_concurrent_same_name_preserves_both_files(self):
        results = await asyncio.gather(self.upload(), self.upload({**self.payload, 'request_id': 'second'}))
        self.assertTrue(all(r['status'] == 'ok' for r in results))
        self.assertEqual(len(self.p._index['images']), 2)

    async def test_invalid_image_is_not_published(self):
        result = await self.upload({**self.payload, 'content_base64': base64.b64encode(b'not a png').decode()})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.p._index['images'], {})

    async def test_compact_delta_never_scans_files(self):
        await self.upload()
        item = next(iter(self.p._index['images'].values())); item['caption_status'] = 'done'
        self.p._abs_plugin_data_path = Mock(side_effect=AssertionError('metadata read must not stat originals'))
        async with self.app.test_request_context('/library?compact=1'):
            data = await (await self.p.caption_library_api()).get_json()
        self.assertNotIn('all_images', data['data'])
        self.assertEqual(len(data['data']['manual_images']), 1)
        async with self.app.test_request_context('/library?since=' + data['data']['revision']):
            delta = await (await self.p.caption_library_api()).get_json()
        self.assertTrue(delta['data']['unchanged'])

    async def test_thumbnail_is_small_original_unchanged_and_cache_invalidates(self):
        path = self.root / 'original.png'; path.write_bytes(self.raw)
        cache = ThumbnailCache(max_bytes=20000)
        thumb = await cache.get(path)
        with Image.open(io.BytesIO(base64.b64decode(thumb['data_url'].split(',', 1)[1]))) as image:
            self.assertLessEqual(max(image.size), 320)
        original = await cache.get(path, False)
        self.assertEqual(base64.b64decode(original['data_url'].split(',', 1)[1]), self.raw)
        self.assertEqual(path.read_bytes(), self.raw)
        with Image.new('RGB', (17, 13), 'red') as image: image.save(path)
        self.assertNotEqual((await cache.get(path))['data_url'], thumb['data_url'])
        self.assertLessEqual(cache.bytes, cache.max_bytes)

    async def test_slow_disk_work_does_not_block_other_requests(self):
        ticks = []
        real = self.p._cached_sha256
        def slow(path):
            time.sleep(.12)
            return real(path)
        async def heartbeat():
            for _ in range(20):
                ticks.append(time.perf_counter()); await asyncio.sleep(.01)
        with patch.object(self.p, '_cached_sha256', slow):
            await asyncio.gather(self.upload(), heartbeat())
        self.assertLess(max(b-a for a,b in zip(ticks, ticks[1:])), .08)


class Persistence(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_save_never_overwrites_newer_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.json'; writer = SnapshotWriter(path)
            source = {'images': ['a']}; writer.submit(source); source['images'].append('b')
            await asyncio.sleep(0)
            writer.submit(source)
            await writer.flush()
            self.assertEqual(json.loads(path.read_text('utf-8')), source)

    async def test_failed_flush_is_explicit_and_retry_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.json'; atomic_json(path, {'old': True})
            writer = SnapshotWriter(path)
            with patch('data.plugins.astrbot_plugin_smart_imagechat_hub.backend.gallery_io.atomic_json', side_effect=OSError('disk full')):
                writer.submit({'new': True})
                with self.assertRaises(OSError): await writer.flush()
            self.assertEqual(json.loads(path.read_text('utf-8')), {'old': True})
            await writer.flush()
            self.assertEqual(json.loads(path.read_text('utf-8')), {'new': True})
