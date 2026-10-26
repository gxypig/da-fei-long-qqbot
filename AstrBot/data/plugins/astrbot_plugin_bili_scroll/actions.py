"""Opt-in account automation, independent of QQ targets and delivery timers."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .client import BiliUnavailable, clean
from .scout import split_words
from .work_limits import total_budget,remaining
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import model_work

ACCOUNT_FIELDS = {'use_account': False, 'account_cruise': 'off', 'account_interval': 30,
    'auto_comment': False, 'mention_names': '', 'comment_per_round': 1,
    'dm_mode': 'off', 'dm_uid': '', 'reply_comments': False, 'reply_per_round': 1,
    'favorite_id': ''}


def validate_account(v):
    if v['account_cruise'] not in {'off', 'custom', 'normal', 'crazy'}:
        raise ValueError('巡航请选择关闭、自定义、普通或高频')
    if not 5 <= v['account_interval'] <= 10080:
        raise ValueError('账号巡航间隔为5至10080分钟')
    if v['dm_mode'] not in {'off', 'only', 'both'}:
        raise ValueError('私信模式不正确')
    for k in ('dm_uid', 'favorite_id'):
        if v[k] and not re.fullmatch(r'[1-9][0-9]{0,19}', v[k]):
            raise ValueError('私信UID和收藏夹ID只能填写正整数或留空')
    if v['dm_mode'] != 'off' and not v['dm_uid']:
        raise ValueError('开启私信前请填写接收者B站UID')
    names = split_words(v['mention_names'])
    if len(names) > 5 or any(len(n) > 40 or re.search(r'[@<>\r\t]', n) for n in names):
        raise ValueError('@名单最多5个精确昵称，不带@，逗号分隔')
    if v['auto_comment'] and not names:
        raise ValueError('按原版规则，自动评论须填写@的精确昵称')
    if not 1 <= v['comment_per_round'] <= 5 or not 1 <= v['reply_per_round'] <= 5:
        raise ValueError('每轮分享与回复数量为1至5条')


class Ledger:
    """A reserved action is never automatically retried, even after restart."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, uid TEXT, kind TEXT, bvid TEXT, target TEXT, created REAL, state TEXT, detail TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, uid, kind, bvid, target, detail):
        key = hashlib.sha256(json.dumps([uid, kind, bvid, target]).encode()).hexdigest()
        with self.connect() as db:
            n = db.execute('INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?,?,?,?)',
                (key, uid, kind, bvid, target, time.time(), 'reserved', json.dumps(detail, ensure_ascii=False))).rowcount
        return key if n else None

    def finish(self, key, state, detail):
        with self.connect() as db:
            db.execute('UPDATE actions SET state=?,detail=? WHERE key=?', (state, json.dumps(detail, ensure_ascii=False), key))

    def checked(self,uid,rpid,text,mark=False):
        """Cache completed decisions, never turn a failed model read into a decision."""
        key=hashlib.sha256(json.dumps([uid,'checked',rpid,text]).encode()).hexdigest()
        with self.connect() as db:
            if mark:
                db.execute('INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?,?)',
                    (key,uid,'checked',rpid,'',time.time(),'checked','{}'))
                db.execute("DELETE FROM actions WHERE kind='checked' AND created<?",(time.time()-7*86400,))
                return True
            return bool(db.execute("SELECT 1 FROM actions WHERE key=? AND state='checked' AND created>?",(key,time.time()-7*86400)).fetchone())

    def rows(self, uid, limit=100):
        with self.connect() as db:
            return [{**dict(r), 'detail': json.loads(r['detail'])} for r in db.execute(
                "SELECT * FROM actions WHERE uid=? AND kind!='checked' ORDER BY created DESC LIMIT ?", (uid, limit))]

    def excluded(self, uid):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT DISTINCT bvid FROM actions WHERE uid=? AND kind IN (\'comment\',\'dm\',\'favorite\')', (uid,))]


class AccountActions:
    def __init__(self, plugin, path):
        self.plugin, self.account = plugin, plugin.account
        self.ledger = Ledger(path)
        self.task = None
        self.state = {'running': False, 'message': '账号操作未运行'}
        self.lock = asyncio.Lock()

    def active(self, v):
        return (v['enabled'] and v['use_account'] and self.account.ready
                and (v['auto_comment'] or v['dm_mode'] != 'off' or v['favorite_id'] or v['reply_comments']))

    def check(self, values, identity):
        if (self.plugin.values() != values or not self.active(values)
                or self.account.identity() != identity or self.plugin.quiet()):
            raise BiliUnavailable('设置、账号或免打扰状态已变化，本轮取消')

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def start(self):
        if self.task and not self.task.done():
            raise BiliUnavailable('已有一轮正在执行，请查看记录')
        v = self.plugin.values()
        if not self.active(v):
            raise BiliUnavailable('请先登录、保存“使用登录账号”，并开启至少一种账号操作')
        if self.plugin.quiet():
            raise BiliUnavailable('当前处于免打扰时段')
        self.state = {'running': True, 'message': '正在按已保存设置执行，本轮不向QQ发消息'}
        async def scheduled():
            with model_work('scheduled'):
                async with total_budget(120):await self.run()
        self.task = asyncio.create_task(scheduled(), name='bili-account-round')
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def folders(self):
        identity = self.account.identity()
        data = await self.account.get('/x/v3/fav/folder/created/list-all', {'up_mid': identity[0]})
        if identity != self.account.identity():
            raise BiliUnavailable('账号已变化，请重新读取收藏夹')
        return [{'id': str(r['id']), 'title': clean(r.get('title'), 80)} for r in data.get('list') or []
                if str(r.get('mid', identity[0])) == identity[0]]

    async def mentions(self, names):
        resolved = []
        for name in split_words(names):
            data = await self.account.get('/x/web-interface/search/type', {'search_type': 'bili_user', 'keyword': name, 'page': 1})
            matches = [r for r in data.get('result') or [] if r.get('uname') == name and str(r.get('mid', '')).isdigit()]
            if len(matches) != 1:
                raise BiliUnavailable('无法唯一核对@昵称：' + name + '；未发布评论')
            resolved.append({'uid': str(matches[0]['mid']), 'name': name})
        return resolved

    async def visibility(self, aid, rpid, root=''):
        path = '/x/v2/reply/reply' if root else '/x/v2/reply/main'
        params = {'type': 1, 'oid': aid, 'ps': 20, **({'root': root, 'pn': 1} if root else {'mode': 2, 'next': 0})}
        found, errors = [], []
        for anonymous in (True, False):
            try:
                data = await self.account.get(path, params, anonymous=anonymous)
                rows = list(data.get('replies') or [])
                top = data.get('top_replies') or []
                rows += top if isinstance(top, list) else []
                upper = (data.get('upper') or {}).get('top')
                if isinstance(upper, dict):
                    rows.append(upper)
                found.append(any(str(r.get('rpid_str') or r.get('rpid')) == str(rpid) for r in rows))
            except BiliUnavailable:
                found.append(False)
                errors.append(anonymous)
        if found[0]:
            return 'public'
        if found[1] and not errors:
            return 'only_self_in_sample'
        return 'unknown' if errors else 'not_in_sample'

    async def perform(self, v, identity, kind, item, target, payload, path, detail, *, receipt=''):
        self.check(v, identity)
        key = await asyncio.to_thread(self.ledger.reserve,identity[0], kind, item['bvid'], str(target), detail)
        if not key:
            return
        confirmed=False
        async def save(state,values):
            await asyncio.to_thread(self.ledger.finish,key,state,values)
        try:
            data = await self.account.post(path, payload, expected_identity=identity,
                final_check=lambda: self.check(v, identity))
            if receipt == 'rpid':
                rpid = str(data.get('rpid_str') or data.get('rpid') or (data.get('reply') or {}).get('rpid') or '')
                if not rpid.isdigit():
                    await save('unknown', {**detail, 'note': '接口未返回评论编号，不能确认，不自动重发'})
                    return
                detail = {**detail, 'rpid': rpid}
                confirmed=True
                await save('sent', detail)  # persist receipt before optional reads
                try:
                    async with asyncio.timeout(remaining(8)):
                        detail['visibility'] = await self.visibility(item['aid'], rpid, detail.get('root', ''))
                except (BiliUnavailable,TimeoutError):detail['visibility']='unknown'
                returned = (data.get('reply') or {}).get('content') or {}
                detail['mention_receipt'] = [str(r.get('mid')) for r in returned.get('members') or []]
            elif receipt == 'dm':
                message_key = str(data.get('msg_key') or '')
                if not message_key:
                    await save('unknown', {**detail, 'note': '接口未返回私信编号，不能确认，不自动重发'})
                    return
                detail = {**detail, 'message_key': message_key}
            confirmed=True
            await save('sent', detail)
        except asyncio.CancelledError:
            # May have reached the server. Preserve any receipt already persisted.
            raise
        except Exception as exc:
            try:
                await save('sent' if confirmed else 'unknown', {**detail, 'note': type(exc).__name__ + ('；已有成功回执，后续记录/检查失败' if confirmed else '；结果未确认，不自动重发')})
            except Exception:pass
            if confirmed:
                self.state['message']='操作已取得成功回执，附加检查或保存失败；不会重发'
                return
            raise BiliUnavailable('账号操作未确认成功，本轮停止；请检查操作记录') from exc

    async def replies(self, v, identity):
        count = attempts = 0
        records=await asyncio.to_thread(self.ledger.rows,identity[0],1000)
        done={r['target'] for r in records if r['kind']=='reply'}
        roots = [r for r in records if r['kind'] == 'comment'
                 and r['state'] == 'sent' and r['detail'].get('rpid') and r['created'] > time.time() - 7 * 86400][:8]
        for row in roots:
            root, aid = row['detail']['rpid'], row['detail']['aid']
            data = await self.account.get('/x/v2/reply/reply', {'type': 1, 'oid': aid, 'root': root, 'pn': 1, 'ps': 20})
            # Confirm the actual root belongs to this account, not a shared/stale record.
            actual_root = data.get('root') or {}
            if str((actual_root.get('member') or {}).get('mid')) != identity[0]:
                continue
            for reply in data.get('replies') or []:
                if attempts>=6 or remaining(30)<5:return
                mid = str((reply.get('member') or {}).get('mid') or '')
                rpid = str(reply.get('rpid_str') or reply.get('rpid') or '')
                if (not mid.isdigit() or mid == identity[0] or not rpid.isdigit()
                        or str(reply.get('root')) != root or str(reply.get('parent')) not in {'0', root}
                        or int(reply.get('ctime') or 0) < row['created']):
                    continue
                target = root + ':' + mid  # once per user per root, even across restarts
                if target in done:
                    continue
                content=clean((reply.get('content') or {}).get('message'),600)
                if await asyncio.to_thread(self.ledger.checked,identity[0],rpid,content):continue
                attempts+=1
                self.state['examined_this_round']=attempts
                async with asyncio.timeout(remaining(25)):
                    text = await self.plugin.make_account_reply(content)
                await asyncio.to_thread(self.ledger.checked,identity[0],rpid,content,True)
                if not text:
                    continue
                item = {'bvid': row['bvid'], 'aid': aid}
                await self.perform(v, identity, 'reply', item, target,
                    {'oid': aid, 'type': 1, 'message': text, 'root': root, 'parent': rpid, 'plat': 1},
                    '/x/v2/reply/add', {'aid': aid, 'root': root, 'parent': rpid, 'text': text}, receipt='rpid')
                count += 1
                if count >= v['reply_per_round']:
                    return

    async def run(self):
        async with self.lock:
            v, identity = self.plugin.values(), self.account.identity()
            try:
                validate_account(v)
                self.check(v, identity)
                # DM-only really means no public comments/replies.
                public = v['dm_mode'] != 'only'
                names = await self.mentions(v['mention_names']) if public and v['auto_comment'] else []
                if v['favorite_id'] and v['favorite_id'] not in {r['id'] for r in await self.folders()}:
                    raise BiliUnavailable('收藏夹不属于当前账号，已取消本轮')
                if (public and v['auto_comment']) or v['dm_mode'] != 'off' or v['favorite_id']:
                    words = split_words(v['keywords']) or ['']
                    data = await self.plugin.discover(words[int(time.time() // 60) % len(words)],
                        excluded=await asyncio.to_thread(self.ledger.excluded,identity[0]))
                    for item in data['items'][:v['comment_per_round']]:
                        text = await self.plugin.review_account_text(await self.plugin.make_copy(item, None))
                        # The model cannot choose recipients or insert extra mentions.
                        text = text.replace('@', '＠')
                        if names:
                            message = text.split('\nhttps://')[0][:180] + ' ' + ' '.join('@' + r['name'] for r in names) + ' '
                            await self.perform(v, identity, 'comment', item, ','.join(r['uid'] for r in names),
                                {'oid': item['aid'], 'type': 1, 'message': message, 'plat': 1,
                                 'gaia_source': 'main_web', 'statistics': json.dumps({'appId': 100, 'platform': 5}),
                                 'at_name_to_mid': json.dumps({r['name']: int(r['uid']) for r in names}, ensure_ascii=False)},
                                '/x/v2/reply/add', {'aid': item['aid'], 'text': message, 'mentions': names}, receipt='rpid')
                        if v['dm_mode'] != 'off':
                            await self.perform(v, identity, 'dm', item, v['dm_uid'],
                                {'msg[sender_uid]': identity[0], 'msg[receiver_id]': v['dm_uid'], 'msg[receiver_type]': 1,
                                 'msg[msg_type]': 1, 'msg[msg_status]': 0, 'msg[content]': json.dumps({'content': text[:500]}, ensure_ascii=False),
                                 'msg[dev_id]': str(uuid.uuid4()).upper(), 'msg[new_face_version]': 0,
                                 'msg[timestamp]': int(time.time()), 'from_filework': 0, 'build': 0, 'mobi_app': 'web'},
                                '/web_im/v1/web_im/send_msg', {'aid': item['aid'], 'text': text[:500]}, receipt='dm')
                        if v['favorite_id']:
                            await self.perform(v, identity, 'favorite', item, v['favorite_id'],
                                {'rid': item['aid'], 'type': 2, 'add_media_ids': v['favorite_id'], 'del_media_ids': ''},
                                '/medialist/gateway/coll/resource/deal', {'aid': item['aid']})
                    self.state['message'] = f'本轮检查完成，找到 {len(data["items"])} 条可分享视频；实际操作见下方记录'
                if public and v['reply_comments']:
                    await self.replies(v, identity)
            except asyncio.CancelledError:
                self.state['message'] = '本轮已停止；已发出的内容不会撤回，未确认项不自动重试'
                raise
            except Exception as exc:
                self.state['message'] = str(exc) if isinstance(exc, BiliUnavailable) else '本轮异常：' + type(exc).__name__
            finally:
                self.state.update(running=False, finished=time.time())

    async def tick(self):
        v = self.plugin.values()
        interval = {'normal': 30, 'crazy': 5}.get(v['account_cruise'], v['account_interval']) * 60
        signature = json.dumps([self.account.identity()[0], v['account_cruise'], interval, bool(self.active(v))])
        row = await self.plugin.get_kv_data('account_timer', {})
        if row.get('signature') != signature:
            row = {'signature': signature, 'due': time.time() + interval}
            await self.plugin.put_kv_data('account_timer', row)
        self.state['due'] = row['due']
        if (v['account_cruise'] == 'off' or not self.active(v) or self.plugin.quiet()
                or row['due'] > time.time() or (self.task and not self.task.done())):
            return
        await self.plugin.put_kv_data('account_timer', {**row, 'due': time.time() + interval})
        self.start()
