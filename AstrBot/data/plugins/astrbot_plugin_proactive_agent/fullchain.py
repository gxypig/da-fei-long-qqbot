"""Adapter-to-receipt integration. Permission stages remain the framework's."""
import asyncio
from contextvars import ContextVar
import time
from functools import wraps
from contextlib import nullcontext, asynccontextmanager
from aiocqhttp import CQHttp
from .reply_jobs import ReplyJob, ReplyCancelled, current_job, TERMINAL
from .group_flow import mergeable
from .route_guard import bind_route
from .hook_lifecycle import HookLease
from .session_fence import get_fence
from .reply_admission import has_agent_content, initial_admission, is_command

_conversion=ContextVar('qqbot_conversion',default=None)


def install_fullchain(plugin):
    lease=HookLease('fullchain',plugin)
    from .db_pressure import DatabasePressure
    plugin._db_pressure=DatabasePressure(plugin)
    restore_db=plugin._db_pressure.install()
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.pipeline.process_stage.method.agent_request import AgentRequestSubStage
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.utils.session_waiter import SessionWaiter
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
    from .chain_metrics import operation,stage as metric_stage,add as metric_add
    queue=plugin._reply_queue;ledger=plugin._delivery
    from .conversation_controls import ConversationControls
    controls=ConversationControls(ledger);queue.controls=controls;ledger.controls=controls;ledger.reply_queue=queue
    queue.restore_echoes(ledger)
    old_convert=AiocqhttpAdapter.convert_message;old_execute=PipelineScheduler.execute
    old_process=ProcessStage.process;old_action=CQHttp.call_action
    old_agent=AgentRequestSubStage.process
    old_tool=FunctionToolExecutor.execute;tool_descriptor=FunctionToolExecutor.__dict__['execute']
    old_wait=SessionWaiter.register_wait;old_error=InternalAgentSubStage._send_llm_error_message

    async def register_wait(waiter,handler,timeout=30):
        job=current_job.get()
        if lease.active and job and getattr(job,'admission',None)=='empty_mention_reply':
            queue.finish(job,ledger.state(job),'awaiting_new_input_without_execution_slot')
        return await old_wait(waiter,handler,timeout)

    async def error_message(stage,event,message):
        if not lease.active:return await old_error(stage,event,message)
        from .delivery_ledger import delivery_purpose
        job=current_job.get()
        if lease.active and job:job.execution_result='failed';job.reply_purpose='error_notification'
        with delivery_purpose('error_notification'):
            return await old_error(stage,event,message)

    async def tool_execute(cls,tool,run_context,**arguments):
        if not lease.active:
            async for result in old_tool(tool,run_context,**arguments):yield result
            return
        metric_stage('tool');started=time.monotonic();iterator=old_tool(tool,run_context,**arguments)
        from .stage_budgets import total_budget
        try:
            async with total_budget(50):
                while True:
                    try:
                        async with queue.parked(current_job.get(),'等待工具返回'):
                            result=await anext(iterator)
                    except StopAsyncIteration:break
                    yield result
        finally:
            try:await iterator.aclose()
            finally:metric_add('tool_ms',(time.monotonic()-started)*1000)

    @wraps(old_convert)
    async def convert(adapter,event):
        if not lease.active:return await old_convert(adapter,event)
        if event.get('post_type')!='message':return await old_convert(adapter,event)
        platform=plugin._cfg('platform_id','aiocqhttp')
        if adapter is not plugin._platform():return await old_convert(adapter,event)
        group=event.get('group_id');sender=str(event.get('user_id',''))
        origin=f"{platform}:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"
        job=ReplyJob(origin,sender,str(event.get('message_id','')),str(event.get('raw_message','')))
        mutes=getattr(plugin,'_mutes',None)
        if mutes:job.mute_epoch=mutes.epoch(origin)
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
        if not lease.active:return await old_action(bot,action,**params)
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
        if not lease.active:return await old_execute(scheduler,event)
        if event.get_platform_id()!=plugin._cfg('platform_id','aiocqhttp'):
            return await old_execute(scheduler,event)
        job=getattr(getattr(event,'message_obj',None),'_qqbot_ingress_job',None)
        if job is None or job.state in TERMINAL or job.event is not None and job.event is not event:
            job=queue.create(event,received=job.received if job else None)
        else:
            job.event=event;job.origin=event.unified_msg_origin;job.text=event.get_message_str() or ''
            job.sources[0]['text']=job.text;event.set_extra('qqbot_reply_job',job)
        job.owner=asyncio.current_task()
        event.set_extra('qqbot_received_at',job.received)
        token=current_job.set(job)
        from .persona_authority import begin_task,end_task
        persona_token=begin_task()
        from .review_modes import begin as begin_review,end as end_review
        review_token=begin_review(plugin._cfg,event)
        try:return await old_execute(scheduler,event)
        except asyncio.CancelledError:
            job.execution_result='cancelled'
            queue.finish(job,ledger.state(job) if job.sends else 'cancelled','execution_cancelled');raise
        except Exception as exc:
            job.execution_result='failed'
            queue.finish(job,ledger.state(job) if job.sends else 'failed',type(exc).__name__);raise
        finally:
            if job.state not in TERMINAL:queue.finish(job,ledger.state(job),'pipeline_completed' if job.sends else 'filtered_or_no_reply')
            usage=getattr(plugin,'_meme_usage',None)
            if usage:
                try:usage.finished(event,job.state)
                except Exception as exc:plugin.logger.warning('梗使用过程记录未完成：%s',type(exc).__name__)
            profiles=getattr(plugin,'_ai_profiles',None)
            if profiles:profiles.end_turn(event.get_extra('qqbot_ai_probe'))
            learning=getattr(plugin,'_learning',None)
            if learning:
                try:learning.confirmed_job(job,ledger,profiles)
                except Exception as exc:plugin._audit('learning_ingress',{'status':'failed','error':type(exc).__name__})
            plugin._audit('reply_job_end',{'job_id':job.id,'state':job.state,'reason':job.reason,
                'admission':getattr(job,'admission',None),
                'execution_result':job.execution_result,'delivery_result':job.delivery_result,'reply_purpose':job.reply_purpose,
                'elapsed_ms':round((time.monotonic()-job.received)*1000),
                'stages_ms':{k:round(v,2) for k,v in job.timings.items() if k.endswith('_ms')}})
            current_job.reset(token)
            end_task(persona_token)
            end_review(review_token)

    @asynccontextmanager
    async def admit(event,job,reason):
        mutes=getattr(plugin,'_mutes',None)
        if mutes:
            try:mutes.check(job.origin,job.mute_epoch)
            except ReplyCancelled:
                queue.finish(job,'cancelled','会话静默，未进入执行队列');event.stop_event();yield False;return
        command=is_command(event)
        if not command and not queue.precheck(job):event.stop_event();yield False;return
        work=getattr(plugin,'_work',None)
        with work.reply_work(event) if work else nullcontext():
            entered=time.monotonic()
            job.admission=reason
            job.timings['wake_permissions_ms']=max(0,(entered-job.received)*1000-job.timings.get('conversion_ms',0))
            if plugin._cfg('reply_queue_enabled',False):
                try:
                    queue.submit(job,mergeable=not command and mergeable(event),probability=event.get_extra('qqbot_group_wake')=='probability')
                    async with asyncio.timeout(180):await queue.wait(job)
                except ReplyCancelled:
                    event.stop_event();yield False;return
                event.set_extra('qqbot_unified_admitted',True)
                event.set_extra('qqbot_flow_started',True)
            else:
                if queue.closed:
                    queue.finish(job,'cancelled','service_shutdown');event.stop_event();yield False;return
                job.state='running';job.stage='preparing';job.timings['started']=time.monotonic()
                queue.active[job.id]=job
                queue.inflight[job.id]=job
            job.timings['reply_queue_ms']=(time.monotonic()-entered)*1000
            manager=getattr(plugin.context,'conversation_manager',None)
            async with asyncio.timeout(5):
                job.cid=await manager.get_curr_conversation_id(job.origin) if manager else None
            job.generation=ledger.scope(job.origin,job.cid)
            fence=get_fence(manager)
            job.session_epoch=fence.epoch(job.origin) if fence else 0
            bind_route(job.origin,job.sender,job.source_id,bool(event.get_group_id()))
            yield True

    @wraps(old_process)
    async def process(stage,event):
        if not lease.active:
            async for x in old_process(stage,event):yield x
            return
        job=event.get_extra('qqbot_reply_job')
        profiles=getattr(plugin,'_ai_profiles',None)
        if job and profiles and str(event.get_sender_id())!=str(event.get_self_id()):
            profiles.observe(event.get_platform_id(),str(event.get_sender_id()),job.text,job.origin,job.source_id)
        mutes=getattr(plugin,'_mutes',None)
        if job and mutes:
            try:mutes.check(job.origin,job.mute_epoch)
            except ReplyCancelled:
                # Preserve known read/learning observers without running command
                # handlers or generating a response merely to suppress it later.
                await plugin.track_session(event)
                learning=getattr(plugin,'_learning',None)
                if learning and not is_command(event):
                    learning.enqueue_meme(job.text,[r.get('text','') for r in (event.get_extra('qqbot_meme_recent') or [])],job.origin)
                queue.finish(job,'cancelled','会话静默：已记录输入，无回复任务');event.stop_event();return
        reason=initial_admission(stage,event)
        if not job or not reason:
            # Preserve journal, group context, learning and plugin callbacks.
            # Requests produced by observers are admitted at agent() below.
            async for x in old_process(stage,event):yield x
            return
        async with admit(event,job,reason) as accepted:
            if accepted:
                async for x in old_process(stage,event):yield x

    @wraps(old_agent)
    async def agent(stage,event):
        job=event.get_extra('qqbot_reply_job')
        if not lease.active or not job or job.state!='received' or not has_agent_content(event):
            async for x in old_agent(stage,event):yield x
            return
        async with admit(event,job,'handler_provider_request') as accepted:
            if accepted:
                async for x in old_agent(stage,event):yield x

    patches=[(AiocqhttpAdapter,'convert_message',convert,old_convert),(PipelineScheduler,'execute',execute,old_execute),
             (ProcessStage,'process',process,old_process),(AgentRequestSubStage,'process',agent,old_agent),
             (SessionWaiter,'register_wait',register_wait,old_wait),(InternalAgentSubStage,'_send_llm_error_message',error_message,old_error),
             (CQHttp,'call_action',action,old_action)]
    wrapped_tool=classmethod(tool_execute);FunctionToolExecutor.execute=wrapped_tool
    for cls,name,wrapper,_ in patches:setattr(cls,name,wrapper)
    def restore():
        lease.close()
        restore_db()
        queue.close()
        if FunctionToolExecutor.__dict__['execute'] is wrapped_tool:FunctionToolExecutor.execute=tool_descriptor
        for cls,name,wrapper,previous in reversed(patches):
            if getattr(cls,name) is wrapper:setattr(cls,name,previous)
    return restore
