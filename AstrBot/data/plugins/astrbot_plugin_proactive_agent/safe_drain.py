"""Quiesce producers, await wire receipts and prove durability before stopping."""
import asyncio

async def drain_project(plugin,timeout=8):
    plugin._draining=True
    companions=[plugin];getter=getattr(plugin.context,'get_registered_star',None)
    meta=getter('astrbot_plugin_bili_scroll') if getter else None
    if meta and meta.activated:companions.append(meta.star_cls)
    tasks=set();stores=[];snapshots=[];queues=[];errors=[]
    wiki=getattr(plugin,'_wiki_updates',None)
    if wiki:
        try:
            async with asyncio.timeout(timeout):await wiki.close()
        except Exception as exc:errors.append('词库更新尚未安全停止：'+type(exc).__name__)
    backups=getattr(plugin,'_backups',None)
    if backups:
        try:await backups.close()
        except Exception as exc:errors.append('备份工作尚未停止：'+type(exc).__name__)
    for owner in companions:
        for name in ('_task','_fixed_task','_account_task'):
            task=getattr(owner,name,None)
            if task and not task.done():tasks.add(task)
        for name in ('_life','_image_memory','_ai_profiles','_learning','_meme_usage','_mutes','actions'):
            service=getattr(owner,name,None)
            if not service:continue
            for field in ('task','_task','_runner','prewarm_task','recovery_task'):
                task=getattr(service,field,None)
                if task and not task.done():tasks.add(task)
        registry=getattr(owner,'_personas',None)
        if registry:tasks.update(t for t in registry.tasks.values() if not t.done())
        for name in ('_delivery','_image_memory','_ai_profiles','_life','_meme_usage'):
            store=getattr(getattr(owner,name,None),'store',None)
            if store:stores.append(store)
        share=getattr(owner,'_share_store',None)
        if share:stores.append(share)
        for name in ('_learning','_sent_memory'):
            persistence=getattr(getattr(owner,name,None),'_persistence',None)
            if persistence:snapshots.append(persistence)
    for name in ('_live_journal','_audit_writer'):
        queue=getattr(getattr(plugin,name,None),'queue',None)
        if queue:queues.append(queue)
    for task in tasks:task.cancel()
    if tasks:
        _,left=await asyncio.wait(tasks,timeout=timeout)
        if left:errors.append('后台生产任务尚未退出')
    await plugin._reply_queue.drain(timeout)
    ledger=getattr(plugin,'_delivery',None)
    receipts=[t for t in getattr(ledger,'receipt_tasks',()) if not t.done()]
    if receipts:
        _,left=await asyncio.wait(receipts,timeout=timeout)
        if left:errors.append('平台发送回执仍在途中')
    async def flush_store(store):
        if store.closed:
            if store.pending:raise RuntimeError('持久化已关闭且仍有待写记录')
            return True
        while store.pending:await store.flush(next(iter(store.pending)))
        return True
    jobs=[flush_store(store) for store in stores]+[s.flush() for s in snapshots]+[q.join() for q in queues]
    if jobs:
        try:
            async with asyncio.timeout(timeout):results=await asyncio.gather(*jobs,return_exceptions=True)
            for result in results:
                if isinstance(result,BaseException):errors.append('保存失败：'+type(result).__name__)
                elif result is False:errors.append('仍有未落盘的学习或记忆数据')
        except TimeoutError:errors.append('等待落盘超时，停止已中止')
    active=getattr(plugin._reply_queue,'inflight',plugin._reply_queue.active)
    if active:errors.append('仍有在途回复')
    count=sum(len(s.pending) for s in stores)
    if count:errors.append('投递等记录尚未写完')
    return {'drained':not errors,'durable':not errors,'replay':False,'pending_persistence':count,
        'pending_receipts':sum(not t.done() for t in getattr(ledger,'receipt_tasks',())),
        'errors':list(dict.fromkeys(errors)),'progress':'可以停止' if not errors else '停止中止，请查看未完成项后重试'}
