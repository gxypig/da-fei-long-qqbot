"""Read-only reachability checks and receipt-driven per-recipient circuit state."""
import asyncio
import time
from .delivery_ledger import digest

DEVICE_NAMES={'我的电脑','我的手机','我的平板','我的其他设备','我的设备'}


class TargetHealth:
    def __init__(self,plugin,clock=time.time):
        self.plugin=plugin;self.clock=clock;self.checked=0;self.contacts={};self.errors={};self.self_id=''
        self.lock=asyncio.Lock()

    async def refresh(self,force=False):
        if not force and self.clock()-self.checked<300:return
        async with self.lock:
            if not force and self.clock()-self.checked<300:return
            p=self.plugin
            async def call(action):
                async with asyncio.timeout(4):return await p._call_onebot(action)
            groups,friends,login=await asyncio.gather(*(call(a) for a in ('get_group_list','get_friend_list','get_login_info')),return_exceptions=True)
            if isinstance(login,dict):self.self_id=str(login.get('user_id',''))
            self.checked=self.clock()
            for kind,result,idkey,namekey in [('GroupMessage',groups,'group_id','group_name'),('FriendMessage',friends,'user_id','nickname')]:
                if not isinstance(result,list):
                    self.errors[kind]='目标列表暂时无法核实';continue
                self.errors.pop(kind,None);self.contacts[kind]={}
                for r in result:
                    if not isinstance(r,dict):continue
                    uid=str(r.get(idkey,''));name=str(r.get(namekey,'') or r.get('remark',''))
                    reason=''
                    if not uid.isdigit() or int(uid)<=0:reason='设备入口或无效账号'
                    elif kind=='FriendMessage' and uid==self.self_id:reason='机器人自身'
                    elif kind=='FriendMessage' and (r.get('is_device') or name in DEVICE_NAMES or r.get('friend_type')=='device'):reason='设备入口'
                    self.contacts[kind][uid]={'id':uid,'name':name,'reason':reason}
            if not self.self_id:self.errors['FriendMessage']='机器人登录账号尚未核实，暂不主动私聊'

    def circuit(self,origin):
        return self.plugin._delivery.store.rows.get('__target__:'+digest(origin),{})

    def status(self,origin):
        if origin=='qzone':return {'available':True,'reason':'','checked_at':self.checked}
        try:_,kind,uid=origin.split(':',2)
        except ValueError:return {'available':False,'reason':'无效目标地址'}
        circuit=self.circuit(origin);pause=circuit.get('pause_until',0)
        reason=''
        if pause>self.clock():reason='连续三次明确失败，暂停30分钟'
        elif pause and self.checked<pause:reason='暂停结束，等待重新检查可达性'
        elif kind in self.errors:reason=self.errors[kind]
        elif uid not in self.contacts.get(kind,{}):reason='当前群/好友列表未找到，保留原配置'
        else:reason=self.contacts[kind][uid]['reason']
        return {**circuit,'origin':origin,'available':not reason,'reason':reason,'checked_at':self.checked,
            'resume_at':pause or None}

    async def check(self,origin):
        circuit=self.circuit(origin);pause=circuit.get('pause_until',0)
        await self.refresh(force=bool(pause and pause<=self.clock() and self.checked<pause))
        return self.status(origin)

    async def collect(self):
        await self.refresh();platform=self.plugin._cfg('platform_id','aiocqhttp')
        return tuple([(uid,r['name']) for uid,r in self.contacts.get(kind,{}).items()
            if self.status(f'{platform}:{kind}:{uid}')['available']] for kind in ('GroupMessage','FriendMessage'))

    def snapshot(self):
        platform=self.plugin._cfg('platform_id','aiocqhttp');origins=set()
        for kind in ('GroupMessage','FriendMessage'):
            origins.update(f'{platform}:{kind}:{uid}' for uid in self.contacts.get(kind,{}))
        for field,kind in [('group_ids','GroupMessage'),('fixed_group_ids','GroupMessage'),('private_ids','FriendMessage'),('fixed_private_ids','FriendMessage')]:
            origins.update(f'{platform}:{kind}:{uid}' for uid in self.plugin._id_set(self.plugin._cfg(field,'')))
        return [self.status(origin) for origin in sorted(origins)]
