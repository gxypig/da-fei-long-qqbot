"""Adapter-to-receipt integration. Permission stages remain the framework's."""
import asyncio
from contextvars import ContextVar
import time
from functools import wraps
from contextlib import nullcontext
from aiocqhttp import CQHttp
from .reply_jobs import ReplyJob, ReplyCancelled, current_job, TERMINAL
from .group_flow import mergeable
from .route_guard import bind_route

_conversion=ContextVar('qqbot_conversion',default=None)


def install_fullchain(plugin):
    from .db_pressure import DatabasePressure
    plugin._db_pressure=DatabasePressure(plugin)
    restore_db=plugin._db_pressure.install()
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from .chain_metrics import operation,stage as metric_stage,add as metric_add
    queue=plugin._reply_queue;ledger=plugin._delivery
    old_convert=AiocqhttpAdapter.convert_message;old_execute=PipelineScheduler.execute
    old_process=ProcessStage.process;old_action=CQHttp.call_action
    old_tool=FunctionToolExecutor.execute;tool_descriptor=FunctionToolExecutor.__dict__['execute']

    async def tool_execute(cls,tool,run_context,**arguments):
        metric_stage('tool');started=time.monotonic();iterator=old_tool(tool,run_context,**arguments)
        try:
            async for result in iterator:yield result
        finally:
            try:await iterator.aclose()
            finally:metric_add('tool_ms',(time.monotonic()-started)*1000)

    @wraps(old_convert)
    async def convert(adapter,event):
        if event.get('post_type')!='message':return await old_convert(adapter,event)
        platform=plugin._cfg('platform_id','aiocqhttp')
        if adapter is not plugin._platform():return await old_convert(adapter,event)
        group=event.get('group_id');sender=str(event.get('user_id',''))
        origin=f"{platform}:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"
        job=ReplyJob(origin,sender,str(event.get('message_id','')),str(event.get('raw_message','')))
        queue.jobs[job.id]=job;job.queue=queue;token=_conversion.set(job);started=time.monotonic()
        try:
            message=await old_convert(adapter,event)
            if message is not None:message._qqbot_ingress_job=job
            else:queue.finish(job,'ignored','adapter_filter')
            return message
        except BaseException:
            queue.finish(job,'failed','conversion_failed');raise
        finally:
            job.timings['conversion_ms']=(time.monotonic()-started)*1000
            _conversion.reset(token)

    @wraps(old_action)
    async def action(bot,action,**params):
        job=_conversion.get()
        if job and action in {'get_msg','get_group_member_info','get_stranger_info'}:
            started=time.monotonic()
            try:
                async with asyncio.timeout(4):return await old_action(bot,action,**params)
            finally:
                name='reference_query_ms' if action=='get_msg' else 'sender_query_ms'
                job.timings[name]=job.timings.get(name,0)+(time.monotonic()-started)*1000
        return await old_action(bot,action,**params)

    @wraps(old_execute)
    async def execute(scheduler,event):
        if event.get_platform_id()!=plugin._cfg('platform_id','aiocqhttp'):
            return await old_execute(scheduler,event)
        job=getattr(getattr(event,'message_obj',None),'_qqbot_ingress_job',None)
        if job is None:job=queue.create(event)
        else:
            job.event=event;job.origin=event.unified_msg_origin;job.text=event.get_message_str() or ''
            job.sources[0]['text']=job.text;event.set_extra('qqbot_reply_job',job)
        job.owner=asyncio.current_task()
        event.set_extra('qqbot_received_at',job.received)
        token=current_job.set(job)
        try:return await old_execute(scheduler,event)
        except asyncio.CancelledError:
            queue.finish(job,ledger.state(job) if job.sends else 'cancelled','execution_cancelled');raise
        except Exception as exc:
            queue.finish(job,ledger.state(job) if job.sends else 'failed',type(exc).__name__);raise
        finally:
            if job.state not in TERMINAL:queue.finish(job,ledger.state(job),'pipeline_completed' if job.sends else 'filtered_or_no_reply')
            profiles=getattr(plugin,'_ai_profiles',None)
            if profiles:profiles.end_turn(event.get_extra('qqbot_ai_probe'))
            learning=getattr(plugin,'_learning',None)
            if learning:
                try:learning.confirmed_job(job,ledger,profiles)
                except Exception as exc:plugin._audit('learning_ingress',{'status':'failed','error':type(exc).__name__})
            plugin._audit('reply_job_end',{'job_id':job.id,'state':job.state,'reason':job.reason,
                'elapsed_ms':round((time.monotonic()-job.received)*1000),
                'stages_ms':{k:round(v,2) for k,v in job.timings.items() if k.endswith('_ms')}})
            current_job.reset(token)

    @wraps(old_process)
    async def process(stage,event):
        job=event.get_extra('qqbot_reply_job')
        profiles=getattr(plugin,'_ai_profiles',None)
        if job and profiles and str(event.get_sender_id())!=str(event.get_self_id()):
            profiles.observe(event.get_platform_id(),str(event.get_sender_id()),job.text,job.origin,job.source_id)
        # Observer hooks may run on passive messages. Only real response opportunities queue.
        from astrbot.core.star.filter.command import CommandFilter
        command=any(isinstance(f,CommandFilter) for h in event.get_extra('activated_handlers') or [] for f in h.event_filters)
        wanted=bool(getattr(event,'is_at_or_wake_command',False) or command or event.get_extra('provider_request'))
        if not job or not wanted:
            async for x in old_process(stage,event):yield x
            return
        work=getattr(plugin,'_work',None)
        with work.reply_work(event) if work else nullcontext():
            entered=time.monotonic()
            job.timings['wake_permissions_ms']=max(0,(entered-job.received)*1000-job.timings.get('conversion_ms',0))
            if plugin._cfg('reply_queue_enabled',False):
                try:
                    queue.submit(job,mergeable=not command and mergeable(event),probability=event.get_extra('qqbot_group_wake')=='probability')
                    await queue.wait(job)
                except ReplyCancelled:
                    event.stop_event();return
                event.set_extra('qqbot_unified_admitted',True)
                event.set_extra('qqbot_flow_started',True)
            else:
                if queue.closed:
                    queue.finish(job,'cancelled','service_shutdown');event.stop_event();return
                job.state='running';job.stage='preparing';job.timings['started']=time.monotonic()
                queue.active[job.id]=job
            job.timings['reply_queue_ms']=(time.monotonic()-entered)*1000
            manager=getattr(plugin.context,'conversation_manager',None)
            async with asyncio.timeout(5):
                job.cid=await manager.get_curr_conversation_id(job.origin) if manager else None
            job.generation=ledger.scope(job.origin,job.cid)
            bind_route(job.origin,job.sender,job.source_id,bool(event.get_group_id()))
            async for x in old_process(stage,event):yield x

    patches=[(AiocqhttpAdapter,'convert_message',convert,old_convert),(PipelineScheduler,'execute',execute,old_execute),
             (ProcessStage,'process',process,old_process),(CQHttp,'call_action',action,old_action)]
    wrapped_tool=classmethod(tool_execute);FunctionToolExecutor.execute=wrapped_tool
    for cls,name,wrapper,_ in patches:setattr(cls,name,wrapper)
    def restore():
        restore_db()
        queue.close()
        if FunctionToolExecutor.__dict__['execute'] is wrapped_tool:FunctionToolExecutor.execute=tool_descriptor
        for cls,name,wrapper,previous in reversed(patches):
            if getattr(cls,name) is wrapper:setattr(cls,name,previous)
    return restore
