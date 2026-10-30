"""Receipt evidence for extensions without standard OneBot message IDs."""
import asyncio
from .delivery_ledger import DeliveryUnconfirmed,delivery_scope

def qzone_receipt(value):
    if not isinstance(value,dict):return 'unknown',None
    if value.get('status')=='failed' or value.get('retcode',0) not in (0,1):return 'failed',None
    if value.get('retcode',0)!=0 or value.get('status','ok')!='ok':return 'unknown',None
    data=value.get('data',value)
    if not isinstance(data,dict):return 'unknown',None
    id_=next((str(data[k]) for k in ('tid','unikey','message_id') if data.get(k) not in (None,'',0,'0',False)),None)
    return ('confirmed',id_) if id_ else ('unknown',None)

async def deliver(ledger,origin,text,attachments,submit,classify):
    id_=ledger.begin(origin,None,text,attachments,prepared=True)
    try:await ledger.store.flush(id_)
    except BaseException:
        if not ledger.store.closed:ledger.result(id_,'failed',reason='outbox_persistence_unavailable')
        raise
    submitted=False
    async def transmit():
        nonlocal submitted
        try:
            from .conversation_review import _delivery_check
            check=_delivery_check.get()
            if check:await check()
            ledger.store.put(id_,{**ledger.store.rows[id_],'state':'submitting','submission_state':'intent_durable'})
            await ledger.store.flush(id_)
            if check:await check()
            submitted=True
            ledger.store.put(id_,{**ledger.store.rows[id_],'state':'sending','submission_state':'submitted'})
            value=await submit()
        except BaseException as exc:
            if not ledger.store.closed:
                explicit=not submitted or isinstance(getattr(exc,'retcode',None),int) or type(exc).__name__=='ActionFailed'
                if not submitted:ledger.store.put(id_,{**ledger.store.rows[id_],'submission_state':'not_submitted'})
                ledger.result(id_,'failed' if explicit else 'unknown',reason=type(exc).__name__)
            raise
        if ledger.store.closed:return value
        state,receipt=classify(value);ledger.result(id_,state,receipt)
        if state!='confirmed':raise DeliveryUnconfirmed('平台未确认动态发送：'+state,state)
        return value
    task=asyncio.create_task(transmit(),name='receipt-'+id_);ledger.receipt_tasks.add(task)
    def done(t):
        ledger.receipt_tasks.discard(t)
        if not t.cancelled():t.exception()
    task.add_done_callback(done)
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not submitted:task.cancel();await asyncio.gather(task,return_exceptions=True)
        elif not task.done() and not ledger.store.closed:ledger.result(id_,'unknown',reason='caller_cancelled_receipt_pending')
        raise
