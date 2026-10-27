"""A disabled hook remains a transparent link in another plugin's patch chain."""
from aiocqhttp import CQHttp


class HookLease:
    def __init__(self, name, plugin):
        registry=getattr(CQHttp,'_qqbot_hook_leases',None)
        if registry is None:
            registry={};CQHttp._qqbot_hook_leases=registry
        cfg=getattr(plugin,'_cfg',None)
        if cfg is None:cfg=lambda key,default:getattr(plugin,'values',lambda:{})().get(key,default)
        self.key=(name,cfg('platform_id','aiocqhttp'));self.registry=registry
        previous=registry.get(self.key)
        if previous:previous.active=False
        self.active=True;registry[self.key]=self

    def close(self):
        self.active=False
        if self.registry.get(self.key) is self:self.registry.pop(self.key,None)
