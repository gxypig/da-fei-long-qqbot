"""Observe real OneBot receipts, including when the optional companion is disabled."""
from contextvars import ContextVar
from contextlib import contextmanager
from aiocqhttp import CQHttp
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import receipt_status
from data.plugins.astrbot_plugin_proactive_agent.route_guard import destination,canonical,SEND_ACTIONS

capture=ContextVar('bili_actual_receipts',default=None)

@contextmanager
def observe(origin):
    rows=[];token=capture.set((canonical(origin),rows))
    try:yield rows
    finally:capture.reset(token)

def install(plugin):
    previous=CQHttp.call_action;active=True
    async def call(bot,action,**params):
        scope=capture.get()
        if not active or scope is None or action.removesuffix('_async') not in SEND_ACTIONS:
            return await previous(bot,action,**params)
        origin=destination(action,params,plugin.values()['platform_id'])
        if canonical(origin)!=scope[0]:return await previous(bot,action,**params)
        try:result=await previous(bot,action,**params)
        except BaseException as exc:
            state='failed' if type(exc).__name__=='ActionFailed' else 'unknown'
            scope[1].append({'state':state,'message_id':None});raise
        state,message_id=receipt_status(result)
        scope[1].append({'state':state,'message_id':message_id})
        return result
    CQHttp.call_action=call
    def restore():
        nonlocal active
        active=False
        if CQHttp.call_action is call:CQHttp.call_action=previous
    return restore
