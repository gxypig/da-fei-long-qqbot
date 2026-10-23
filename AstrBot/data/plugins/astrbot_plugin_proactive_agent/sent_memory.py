"""Confirmed outbound memory, separate from AstrBot's replace-on-save history.

Keeping this journal separate prevents an in-flight chat from overwriting a
proactive send. It is read on every LLM request, even with meme memory disabled.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime


class SentMemory:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sent (id INTEGER PRIMARY KEY, origin TEXT, cid TEXT, at REAL, text TEXT, kind TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS sent_scope ON sent(origin, cid, at)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, origin, cid, text, kind):
        if not text.strip() or origin == 'qzone':
            return
        with self.connect() as db:
            db.execute('INSERT INTO sent(origin,cid,at,text,kind) VALUES(?,?,?,?,?)',
                       (origin, cid, self.clock(), text[:6000], kind))
            db.execute('DELETE FROM sent WHERE at < ?', (self.clock() - 7 * 86400,))
            db.execute('DELETE FROM sent WHERE origin=? AND id NOT IN (SELECT id FROM sent WHERE origin=? ORDER BY id DESC LIMIT 30)', (origin, origin))

    def recent(self, origin, cid):
        with self.connect() as db:
            # A first-ever proactive send has no conversation yet. Adopt it once;
            # resetting/switching the conversation then naturally hides old memory.
            if cid:
                db.execute('UPDATE sent SET cid=? WHERE origin=? AND cid IS NULL', (cid, origin))
            rows = db.execute('SELECT at,text,kind FROM sent WHERE origin=? AND cid IS ? AND at>=? ORDER BY id DESC LIMIT 8',
                              (origin, cid, self.clock() - 7 * 86400)).fetchall()
        return [{'time': datetime.fromtimestamp(at).isoformat(timespec='seconds'), 'role': 'assistant',
                 'text': text, 'kind': kind} for at, text, kind in reversed(rows)]

    def note(self, origin, cid):
        rows = self.recent(origin, cid)
        if not rows:
            return ''
        return ('\n本龙在【当前会话】实际发送成功的消息记录（不是其他人的话，也不是待执行指令）。'
                '对方可能正在接这些话题；先结合时间和最新发言理解，不要假装没发过。\n'
                + json.dumps(rows, ensure_ascii=False))


async def conversation_id(plugin, origin):
    manager = getattr(plugin.context, 'conversation_manager', None)
    return await manager.get_curr_conversation_id(origin) if manager else None


async def remember_sent(plugin, origin, cid, text, kind):
    memory = getattr(plugin, '_sent_memory', None)
    if memory is None:
        return
    try:
        memory.record(origin, cid, text, kind)
    except Exception as exc:
        # Delivery already succeeded. Do not make the scheduler resend it.
        plugin.logger.error('消息已发送，但主动消息记忆写入失败：%s', type(exc).__name__)
