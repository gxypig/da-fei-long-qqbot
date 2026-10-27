"""Observe real OneBot receipts, including when the optional companion is disabled."""
from contextvars import ContextVar
from contextlib import contextmanager
from aiocqhttp import CQHttp
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import receipt_status,delivery_scope
from data.plugins.astrbot_plugin_proactive_agent.route_guard import destination,canonical,SEND_ACTIONS
from data.plugins.astrbot_plugin_proactive_agent.hook_lifecycle import HookLease

capture=ContextVar('bili_actual_receipts',default=None)

@contextmanager
def observe(origin):
    rows=[];token=capture.set((canonical(origin),rows))
    try:yield rows
    finally:capture.reset(token)

def install(plugin):
    previous=CQHttp.call_action;lease=HookLease('bili_receipts',plugin)
    async def call(bot,action,**params):
        scope=capture.get()
        if not lease.active or scope is None or action.removesuffix('_async') not in SEND_ACTIONS:
            return await previous(bot,action,**params)
        origin=destination(action,params,plugin.values()['platform_id'])
        if canonical(origin)!=scope[0]:return await previous(bot,action,**params)
        attempt=delivery_scope.get()
        leases=getattr(CQHttp,'_qqbot_hook_leases',{})
        ledger_lease=leases.get(('delivery',plugin.values()['platform_id']))
        if attempt is not None and not (ledger_lease and ledger_lease.active):attempt['state']='submitted'
        try:result=await previous(bot,action,**params)
        except BaseException as exc:
            state='failed' if type(exc).__name__=='ActionFailed' or attempt is not None and attempt.get('state') in {'preparing','failed'} else 'unknown'
            if attempt is not None:attempt['state']=state
            scope[1].append({'state':state,'message_id':None});raise
        state,message_id=receipt_status(result)
        if attempt is not None:attempt.update(state=state,message_id=message_id)
        scope[1].append({'state':state,'message_id':message_id})
        return result
    CQHttp.call_action=call
    def restore():
        lease.close()
        if CQHttp.call_action is call:CQHttp.call_action=previous
    return restore
