"""Bounded gallery I/O. Never read originals or rewrite indexes on the event loop."""
import asyncio
import base64
import copy
import io
import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def decode_upload(encoded):
    if len(encoded) > (MAX_UPLOAD_BYTES + 2) // 3 * 4:
        raise ValueError('图片超过 20MB，请先缩小后上传')
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('图片为空或超过 20MB')
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError('图片超过 4000 万像素，请先缩小后上传')
        source.verify()
    return data


def save_upload(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='upload-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SnapshotWriter:
    """One writer per destination; coalesce queued snapshots, preserve write order.

    Acknowledged mutations explicitly flush. Failed snapshots remain queued so a
    later save/flush can retry; neither cancellation nor a failed task means saved.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.pending = None
        self.task = None
        self.error = None

    def submit(self, value):
        self.pending = copy.deepcopy(value)
        self._start()

    def _start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while self.pending is not None:
            snapshot, self.pending = self.pending, None
            try:
                await asyncio.to_thread(atomic_json, self.path, snapshot)
                self.error = None
            except Exception as exc:
                if self.pending is None:
                    self.pending = snapshot
                self.error = exc
                return

    async def flush(self):
        if self.pending is not None and (self.task is None or self.task.done()):
            self._start()
        if self.task is not None:
            await asyncio.shield(self.task)
        if self.error is not None:
            raise OSError('Gallery persistence failed; changes are not confirmed saved') from self.error


def image_payload(path, thumbnail):
    path = Path(path)
    if not thumbnail:
        import mimetypes
        return mimetypes.guess_type(path.name)[0] or 'image/png', path.read_bytes()
    with Image.open(path) as source:
        source.seek(0)
        source.thumbnail((320, 320), Image.Resampling.LANCZOS)
        with ImageOps.exif_transpose(source) as frame:
            with io.BytesIO() as stream:
                frame.save(stream, 'WEBP', quality=78, method=2)
                return 'image/webp', stream.getvalue()


class ThumbnailCache:
    def __init__(self, max_bytes=8 * 1024 * 1024):
        self.entries = OrderedDict()
        self.bytes = 0
        self.max_bytes = max_bytes
        self.gate = asyncio.Semaphore(2)

    async def get(self, path, thumbnail=True):
        async with self.gate:
            # Stat and source decoding are off-thread; cache invalidates on actual
            # file changes even when a caller holds stale gallery metadata.
            stat = await asyncio.to_thread(Path(path).stat)
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            if thumbnail and key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key]
            mime, data = await asyncio.to_thread(image_payload, path, thumbnail)
            encoded = await asyncio.to_thread(lambda: 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii'))
            result = {'mime_type': mime, 'data_url': encoded, 'thumbnail': thumbnail}
            if thumbnail and len(encoded) <= self.max_bytes:
                self.entries[key] = result
                self.bytes += len(encoded)
                while self.bytes > self.max_bytes:
                    _, old = self.entries.popitem(last=False)
                    self.bytes -= len(old['data_url'])
            return result
