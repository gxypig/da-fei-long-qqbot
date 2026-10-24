"""Bounded local transport journal, independent of model context and authenticated UI."""
import asyncio
from collections import OrderedDict, deque
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time
from aiocqhttp import CQHttp, Message
from .route_guard import canonical, destination, SEND_ACTIONS


def text_parts(parts):
    if isinstance(parts, str):
        parts = list(Message(parts))
    output = []
    for part in parts or []:
        if not isinstance(part, dict):
            kind = getattr(part, 'type', '')
            kind = getattr(kind, 'value', kind)
            data = {'text': getattr(part, 'text', ''), 'qq': getattr(part, 'qq', '')}
        else:
            kind, data = part.get('type'), part.get('data') or {}
        if kind in {'text', 'Plain'} or str(kind).lower() == 'plain':
            output.append(str(data.get('text', '')))
        elif str(kind).lower() == 'at':
            output.append('@' + str(data.get('qq', '')))
        elif kind != 'reply':
            output.append({'image': '[图片]', 'record': '[语音]', 'video': '[视频]', 'forward': '[合并转发]', 'file': '[文件]'}.get(str(kind).lower(), '[附件]'))
    return ''.join(output)[:6000]


class LiveJournal:
    def __init__(self, path):
        self.path = path
        self.rows = OrderedDict()
        self.queue = asyncio.Queue(maxsize=2000)
        self.task = None
        self.error = None

    def _database(self, batch=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, origin TEXT, body TEXT)')
            if batch:
                db.executemany('INSERT INTO messages(origin,body) VALUES (?,?)', [(r['origin'], json.dumps(r, ensure_ascii=False)) for r in batch])
                db.execute('DELETE FROM messages WHERE id <= COALESCE((SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET 9999),0)')
            else:
                return [json.loads(row[0]) for row in db.execute('SELECT body FROM messages ORDER BY id')]

    def _remember(self, row):
        origin = row['origin']
        self.rows.setdefault(origin, deque(maxlen=200)).append(row)
        self.rows.move_to_end(origin)
        while len(self.rows) > 200:
            self.rows.popitem(last=False)

    async def start(self):
        try:
            for row in await asyncio.to_thread(self._database):
                self._remember(row)
        except Exception as exc:
            self.error = type(exc).__name__
        self.task = asyncio.create_task(self._write(), name='qqbot-live-journal')

    def add(self, origin, text, role, sender=''):
        if not text:
            return
        row = {'origin': ':'.join(canonical(origin)), 'text': text, 'role': role, 'sender': str(sender)[:100], 'time': time.time()}
        self._remember(row)
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.error = 'journal queue full'

    def incoming(self, event):
        if event.get_extra('qqbot_live_recorded') or str(event.get_sender_id()) == str(event.get_self_id()):
            return
        event.set_extra('qqbot_live_recorded', True)
        sender = str(event.get_sender_name() or event.get_sender_id())
        self.add(event.unified_msg_origin, text_parts(event.get_messages()) or event.get_message_str(), 'user', sender)

    def history(self, origin):
        rows = self.rows.get(':'.join(canonical(origin)), ())
        return [{'role': r['role'], 'content': datetime.fromtimestamp(r['time']).strftime('%m-%d %H:%M:%S')
                 + ' · ' + (r['sender'] or ('大肥龙' if r['role'] == 'assistant' else '对方')) + '\n' + r['text']} for r in rows]

    async def _write(self):
        while True:
            first = await self.queue.get()
            batch = [first]
            while not self.queue.empty() and len(batch) < 100:
                batch.append(self.queue.get_nowait())
            try:
                await asyncio.to_thread(self._database, batch)
                self.error = None
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def close(self):
        if self.task:
            await self.queue.join()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def install_live_journal(plugin):
    from astrbot.dashboard.services.conversation_service import ConversationService
    previous_send = CQHttp.call_action
    previous_detail = ConversationService.get_conversation_detail

    async def send(bot, action, **params):
        result = await previous_send(bot, action, **params)
        platform = plugin._platform()
        if platform is not None and getattr(platform, 'bot', None) is bot and action.removesuffix('_async') in SEND_ACTIONS:
            try:
                text = text_parts(params.get('message')) or ('[合并转发]' if params.get('messages') else '')
                plugin._live_journal.add(destination(action, params, plugin._cfg('platform_id', 'aiocqhttp')), text, 'assistant')
            except Exception as exc:
                plugin._live_journal.error = type(exc).__name__
        return result

    async def detail(service, data):
        result = await previous_detail(service, data)
        # Original endpoint has already validated the selected conversation.
        result['qqbot_live_history'] = plugin._live_journal.history(result['user_id'])
        result['qqbot_live_error'] = plugin._live_journal.error
        return result

    CQHttp.call_action = send
    ConversationService.get_conversation_detail = detail
    def restore():
        if CQHttp.call_action is send:
            CQHttp.call_action = previous_send
        if ConversationService.get_conversation_detail is detail:
            ConversationService.get_conversation_detail = previous_detail
    return restore
