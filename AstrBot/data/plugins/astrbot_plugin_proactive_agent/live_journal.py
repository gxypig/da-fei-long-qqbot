"""Bounded local transport journal, independent of model context and authenticated UI."""
import asyncio
from collections import OrderedDict, deque
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time
import os
import uuid
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
        self.instance=uuid.uuid4().hex;self.sequence=0
        self._io=None
        self.stats={'dropped':0,'failures':0,'last_error':None,'last_error_at':None,'recovered_at':None}

    def _database(self, batch=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, origin TEXT, body TEXT)')
            columns={r[1] for r in db.execute('PRAGMA table_info(messages)')}
            if 'entry_id' not in columns:db.execute('ALTER TABLE messages ADD COLUMN entry_id TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS messages_entry_id ON messages(entry_id)')
            if batch:
                db.executemany('INSERT OR IGNORE INTO messages(origin,body,entry_id) VALUES (?,?,?)', [(r['origin'], json.dumps(r, ensure_ascii=False),r['entry_id']) for r in batch])
                db.execute('DELETE FROM messages WHERE id <= COALESCE((SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET 9999),0)')
            else:
                return [json.loads(row[0]) for row in db.execute('SELECT body FROM messages ORDER BY id')]

    def _remember(self, row):
        self.sequence+=1;row['sequence']=self.sequence
        origin = row['origin']
        self.rows.setdefault(origin, deque(maxlen=200)).append(row)
        self.rows.move_to_end(origin)
        while len(self.rows) > 200:
            self.rows.popitem(last=False)

    async def start(self):
        try:self.stats.update(json.loads(await asyncio.to_thread(self.path.with_suffix('.status.json').read_text,'utf-8')))
        except (OSError,ValueError):pass
        try:
            for row in await asyncio.to_thread(self._database):
                self._remember(row)
        except Exception as exc:
            self.error = type(exc).__name__
        self.task = asyncio.create_task(self._write(), name='qqbot-live-journal')

    def add(self, origin, text, role, sender='', message_id=''):
        if not text:
            return
        row = {'origin': ':'.join(canonical(origin)), 'text': text, 'role': role, 'sender': str(sender)[:100], 'time': time.time(),
               'entry_id':uuid.uuid4().hex,'message_id':str(message_id),'delivery':'confirmed' if role=='assistant' else 'received'}
        self._remember(row)
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.error = 'journal queue full'
            self.stats.update(dropped=self.stats['dropped']+1,last_error=self.error,last_error_at=time.time())

    def incoming(self, event):
        if event.get_extra('qqbot_live_recorded') or str(event.get_sender_id()) == str(event.get_self_id()):
            return
        event.set_extra('qqbot_live_recorded', True)
        sender = str(event.get_sender_name() or event.get_sender_id())
        self.add(event.unified_msg_origin, text_parts(event.get_messages()) or event.get_message_str(), 'user', sender,
                 getattr(getattr(event,'message_obj',None),'message_id',''))

    def history(self, origin):
        rows = self.rows.get(':'.join(canonical(origin)), ())
        return [self._display(r) for r in rows]

    @staticmethod
    def _display(r):
        return {'role':r['role'],'content':datetime.fromtimestamp(r['time']).strftime('%m-%d %H:%M:%S')
                +' · '+(r['sender'] or ('大肥龙' if r['role']=='assistant' else '对方'))+'\n'+r['text'],
                'sequence':r['sequence'],'message_id':r.get('message_id','')}

    def delta(self,origin,after=0,instance=None):
        rows=list(self.rows.get(':'.join(canonical(origin)),()))
        reset=instance!=self.instance or (bool(rows) and after<rows[0]['sequence']-1) or after>self.sequence
        selected=rows if reset else [r for r in rows if r['sequence']>after]
        return {'instance':self.instance,'cursor':self.sequence,'reset':reset,
                'rows':[self._display(r) for r in selected],'storage':dict(self.stats),'pending':self.queue.qsize()}

    def _save_stats(self):
        path=self.path.with_suffix('.status.json');temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_text(json.dumps(self.stats,ensure_ascii=False),encoding='utf-8');os.replace(temp,path)
        finally:temp.unlink(missing_ok=True)

    async def _write(self):
        while True:
            first = await self.queue.get()
            batch = [first]
            while not self.queue.empty() and len(batch) < 100:
                batch.append(self.queue.get_nowait())
            try:
                for attempt in range(3):
                    try:await self._offload(self._database,batch);break
                    except Exception:
                        if attempt==2:raise
                        await asyncio.sleep(.2*(attempt+1))
                if self.error:self.stats.update(recovered_at=time.time(),latest_error=self.error)
                self.error = None;self.stats['last_error']=None
            except Exception as exc:
                self.error = type(exc).__name__
                self.stats.update(dropped=self.stats['dropped']+len(batch),failures=self.stats['failures']+1,
                                  last_error=self.error,last_error_at=time.time())
            finally:
                for _ in batch:
                    self.queue.task_done()
            try:await self._offload(self._save_stats)
            except OSError:pass

    async def _offload(self,function,*args):
        self._io=asyncio.create_task(asyncio.to_thread(function,*args))
        return await asyncio.shield(self._io)

    async def close(self):
        if self.task:
            try:await asyncio.wait_for(self.queue.join(),8)
            except TimeoutError:self.stats.update(last_error='shutdown pending',unconfirmed_pending=self.queue.qsize())
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self._io:
                try:await asyncio.wait_for(asyncio.shield(self._io),3)
                except TimeoutError:return
                except Exception:pass
            try:await asyncio.to_thread(self._save_stats)
            except OSError:pass


def install_live_journal(plugin):
    from astrbot.dashboard.services.conversation_service import ConversationService
    previous_send = CQHttp.call_action
    previous_detail = ConversationService.get_conversation_detail

    async def send(bot, action, **params):
        result = await previous_send(bot, action, **params)
        from .delivery_ledger import receipt_status
        delivery,message_id=receipt_status(result)
        platform = plugin._platform()
        if delivery=='confirmed' and platform is not None and getattr(platform, 'bot', None) is bot and action.removesuffix('_async') in SEND_ACTIONS:
            try:
                text = text_parts(params.get('message')) or ('[合并转发]' if params.get('messages') else '')
                plugin._live_journal.add(destination(action, params, plugin._cfg('platform_id', 'aiocqhttp')), text, 'assistant',message_id=message_id)
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
