"""Explicit Images API route; unchanged text model, same receipt/send pipeline."""
import asyncio,base64,io,re,time
from functools import wraps

def animated_request(text):
    text=str(text or '').lower()
    clauses=re.split(r'[，,。；;！!？?\n]',text)
    return any(re.search(r'动图|gif|会动的(?:图|表情)|animated',s)
        and re.search(r'生成|画|做|制作|来[一张个]|整[一张个]|变成|改成|generate|create|animate',s)
        and not re.search(r'不要|别|不用|不需要|为什么|怎么|如何|是什么',s) for s in clauses)

def validate_image(data,animated=False):
    from PIL import Image
    if not data or len(data)>24*1024*1024:raise ValueError('image_size_limit')
    with Image.open(io.BytesIO(data)) as image:
        fmt=image.format.lower();frames=getattr(image,'n_frames',1)
        if animated and (fmt!='gif' or frames<2):raise ValueError('animation_not_returned')
        if image.width*image.height*frames>100_000_000:raise ValueError('image_pixel_limit')
        if fmt not in {'png','jpeg','webp','gif'}:raise ValueError('unsupported_image_format')
        image.verify()
    return fmt

async def generate(plugin,provider,event):
    from astrbot.core.provider.entities import LLMResponse
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image,Plain
    from .dragon_character import photo_caption
    from .persona_authority import role_text
    from .visual_intent import generation_kind
    from . import image_spec
    from .image_animation import frames_to_gif,sample_sheet
    from .conversation_review import json_object
    from .work_scheduler import model_work
    from .image_transport import ImageWait
    wait=ImageWait(plugin,event);wait.phase('preparing')
    text=event.get_extra('qqbot_visual_followup_request') or event.get_message_str();state=event.get_extra('qqbot_character') or {};animated=animated_request(text)
    text_provider=provider
    chosen=plugin._cfg('image_generation_provider_id','')
    provider=plugin.context.get_provider_by_id(chosen) if chosen else provider
    client=getattr(provider,'client',None)
    if client is None or not hasattr(client,'images'):raise ValueError('请选择兼容 Images API 的生图提供商')
    req=event.get_extra('qqbot_generation_request');material=image_spec.context_material(event,req)
    canonical=await asyncio.to_thread(image_spec.identity,plugin)
    checked=plugin._cfg('image_quality_check_enabled',False)
    async def analyze(system,payload,images=()):
        import json
        # Only image requests enter here. Auxiliary kind prevents recursion into
        # this image route; ordinary text replies gain no new model work.
        with model_work('auxiliary'):
            async with asyncio.timeout(55):
                result=await text_provider.text_chat(system_prompt=system,prompt=json.dumps(payload,ensure_ascii=False),
                    image_urls=list(images),request_max_retries=1)
        return json_object(result.completion_text)
    # No blind retries of parameter errors or ambiguous timeouts: image
    # generation may already have consumed work when the connection drops.
    files=[];reference_uris=[];user_files=[];user_uris=[]
    try:
        from pathlib import Path
        from PIL import Image as PILImage
        def reference_bytes(local):
            path=Path(local)
            if path.stat().st_size>24*1024*1024:raise ValueError('reference_size_limit')
            with PILImage.open(path) as picture:
                if picture.width*picture.height>40_000_000:raise ValueError('reference_pixel_limit')
                picture.seek(0);image=picture.convert('RGBA');buffer=io.BytesIO();image.save(buffer,'PNG');image.close()
            return buffer.getvalue()
        async def load_reference(path,index):
            cache=getattr(getattr(plugin,'_incoming_visual',None),'cache',None)
            if cache and cache.expired_ref(path):raise ValueError('image_reference_expired')
            async with asyncio.timeout(12):local=await Image(file=path).convert_to_file_path()
            raw=await asyncio.to_thread(reference_bytes,local)
            return (f'reference-{index}.png',raw,'image/png'),'data:image/png;base64,'+base64.b64encode(raw).decode()
        canonical_file=canonical_uri=None
        if canonical:canonical_file,canonical_uri=await load_reference(canonical['path'],'identity')
        unavailable=0
        for index,path in enumerate(image_spec.reference_paths(event,req,plugin)):
            try:
                file,uri=await load_reference(path,index);user_files.append(file);user_uris.append(uri)
            except (OSError,ValueError,TimeoutError):unavailable+=1
        service=getattr(plugin,'_incoming_visual',None)
        if not user_files and service and image_spec.REFERENCE_CUE.search(text):
            wait.phase('waiting_reference')
            for index,path in enumerate(await service.wait_reference(event)):
                try:
                    file,uri=await load_reference(path,index);user_files.append(file);user_uris.append(uri)
                except (OSError,ValueError,TimeoutError):unavailable+=1
        planner_views=([canonical_uri] if canonical_uri else [])+user_uris
        wait.phase('planning')
        layout=([{'index':1,'role':'identity'}] if canonical_uri else [])+[{'index':i+1+bool(canonical_uri),'role':'user_reference'} for i in range(len(user_uris))]
        # Intent/extraction is required even when optional post-render review is
        # disabled. It replaces the old extraction call, not an added preflight.
        plan=image_spec.normalize_plan(await analyze(image_spec.PLAN,{**material,'identity':canonical['description'] if canonical else role_text('image',state)[:6000],
            'reference_layout':layout,'unavailable_references':unavailable,'caption_style':role_text('image',state)[:1800]},planner_views),material,canonical)
        event.set_extra('qqbot_visual_route',plan['intent'])
        if plan['intent']=='none':
            state['visual_intent']='none'
            return None
        animated=plan['intent']=='animation';event.set_extra('qqbot_requested_animation',animated)
        if not plan['identity'] and plan['subject_self']:plan['identity']=role_text('image',state)[:6000]
        model=plugin._cfg('image_animation_model','gpt-image-2.6-sunburst') if animated else plugin._cfg('image_generation_model','gpt-image-2.5-sunburst')
        base_args={'model':model,'n':1,'size':'auto','quality':'high'}
        if canonical_file and plan['subject_self']:files.append(canonical_file);reference_uris.append(canonical_uri)
        files.extend(user_files);reference_uris.extend(user_uris)
        plugin._audit('image_request_plan',{'intent':plan['intent'],'subject_self':plan['subject_self'],'camera_mode':plan['camera_mode'],
            'user_references':len(user_files),'unavailable_references':unavailable,'render_prompt_chars':len(image_spec.prompt(plan))})
        async def request(frames,failures):
            args={**base_args,'prompt':image_spec.prompt(plan,frames,failures),'output_format':'gif' if animated and not frames else 'png'}
            if frames:args['size']='1536x1024'
            elif animated:args['extra_body']={'frame_count':9,'frame_duration_ms':200}
            async def call():
                return await wait.request(client,files,args)
            wait.phase('waiting_slot')
            scheduler=getattr(getattr(plugin,'_work',None),'scheduler',None)
            result=await scheduler.run(provider,call,kind='interactive') if scheduler else await call()
            row=result.data[0] if result.data else None
            if row is None:raise ValueError('empty_image_result')
            encoded=getattr(row,'b64_json',None)
            if encoded:raw=base64.b64decode(encoded,validate=True)
            else:
                url=getattr(row,'url',None)
                if not isinstance(url,str) or not url.startswith(('https://','http://')):raise ValueError('empty_image_result')
                wait.phase('downloading')
                async with asyncio.timeout(45):
                    local=await Image(file=url).convert_to_file_path();raw=await asyncio.to_thread(Path(local).read_bytes)
            await asyncio.to_thread(validate_image,raw,animated and not frames)
            return raw
        fallback=animated and plugin._cfg('image_animation_frames_fallback',False)
        capability=getattr(plugin,'_image_gif_unavailable',{})
        cache_key=(id(provider),model)
        frames=fallback and capability.get(cache_key,0)>time.monotonic()
        failures=[]
        for attempt in range(2 if checked else 1):
            try:data=await request(frames,failures)
            except Exception as exc:
                unavailable=getattr(exc,'status_code',None)==400 and 'gif output is not enabled' in str(exc).lower()
                if not fallback or frames or not unavailable:raise
                capability[cache_key]=time.monotonic()+1800;plugin._image_gif_unavailable=capability
                frames=True;data=await request(True,failures)
            if checked:
                wait.phase('verifying')
                preview=await asyncio.to_thread(sample_sheet,data) if animated and not frames else data
                views=['data:image/png;base64,'+base64.b64encode(preview).decode()]
                views.extend(reference_uris)
                layout=([{'index':2,'role':'identity'}] if canonical and plan['subject_self'] else [])+[{'index':i+2+bool(canonical and plan['subject_self']),'role':'user_reference'} for i in range(len(user_uris))]
                verdict=await analyze(image_spec.VERIFY,{**plan,'reference_layout':layout,'format':'frames_sheet' if frames else 'native_gif' if animated else 'static'},views)
                passed=all(verdict.get(k) is True for k in ('pass','identity_ok','requirements_ok','camera_ok','motion_ok'))
                if not passed:
                    failures=[str(v)[:250] for v in verdict.get('failures',[])][:8] or ['未通过本轮画面要求核对']
                    plugin._audit('image_generation_check',{'passed':False,'attempt':attempt+1,'issues':failures})
                    if attempt==0:continue
                    raise ValueError('image_quality_rejected')
                plugin._audit('image_generation_check',{'passed':True,'attempt':attempt+1})
            if frames:data=await asyncio.to_thread(frames_to_gif,data)
            break
        fmt=await asyncio.to_thread(validate_image,data,animated)
        wait.phase('ready')
        caption=photo_caption('selfie' if plan['camera_mode'] in {'selfie','mirror_selfie'} else 'scene',state=state)
        proposed=plan.get('caption','').strip()
        # The semantic planner already composed a persona-aware success caption.
        # Use it for static pictures too, rather than the old technical fallback.
        usable=lambda s:bool(s and re.search(r'拍|录',s) and not re.search(r'生成|合成|渲染|制作|接口|模型|AI',s,re.I))
        caption=proposed if usable(proposed) else caption if usable(caption) else '刚拍的，收好'
        image=Image(file='base64://'+base64.b64encode(data).decode())
        response=LLMResponse(role='assistant',completion_text=caption,result_chain=MessageChain([Plain(caption),image]))
        response.qqbot_generated_image=True
        event.set_extra('qqbot_generated_format',fmt)
        event.set_extra('qqbot_image_provenance',{'method':'generated_action_frames' if frames else 'native_gif' if animated else 'static',
            'requirements':plan['requirements'],'camera_mode':plan['camera_mode'],'identity_sha256':canonical['sha256'] if canonical and plan['subject_self'] else None,'checked':checked})
        plugin._audit('image_generation',{'status':'ok','animated':animated,'format':fmt,'method':'generated_action_frames' if frames else 'native','checked':checked})
        return response
    finally:
        files.clear();reference_uris.clear();user_files.clear();user_uris.clear()

def install(plugin):
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from .reply_jobs import current_job
    from .visual_intent import generation_kind
    cls=ProviderOpenAIOfficial;old=cls._query;old_stream=cls._query_stream;active=True
    def selected():
        from .work_scheduler import _kind
        job=current_job.get();event=getattr(job,'event',None)
        if (active and plugin._cfg('image_generation_api_enabled',False) and event and event.get_extra('qqbot_visual_route')!='none'
            and _kind.get() not in {'background','review','auxiliary','scheduled'}):
            if generation_kind(event.get_message_str())!='none':return event
            from .image_spec import followup_request
            resumed=followup_request(event,event.get_extra('qqbot_generation_request'))
            if resumed:event.set_extra('qqbot_visual_followup_request',resumed);return event
    async def run(provider,event):
        lock=event.get_extra('qqbot_image_api_lock')
        if lock is None:lock=asyncio.Lock();event.set_extra('qqbot_image_api_lock',lock)
        async with lock:
            if event.get_extra('qqbot_image_api_cancelled'):raise asyncio.CancelledError()
            return await once(provider,event)
    async def once(provider,event):
        if event.get_extra('qqbot_visual_route')=='none':return None
        previous=event.get_extra('qqbot_image_api_result')
        if previous is not None:return previous
        try:response=await generate(plugin,provider,event)
        except asyncio.CancelledError:
            progress=event.get_extra('qqbot_image_progress') or {}
            event.set_extra('qqbot_image_progress',{**progress,'stage':'cancelled'})
            plugin._audit('image_generation',{'status':'cancelled','stage':progress.get('stage'),
                'outcome':'unknown' if progress.get('submitted') and not progress.get('received') else 'not_sent'})
            event.set_extra('qqbot_image_api_cancelled',True);raise
        except Exception as exc:
            from astrbot.core.provider.entities import LLMResponse
            animated=event.get_extra('qqbot_requested_animation') or animated_request(event.get_message_str());code=getattr(exc,'status_code',None)
            unsupported=animated and code==400 and 'gif output is not enabled' in str(exc).lower()
            reason=str(exc)
            known=bool(re.fullmatch(r'(?:image|animation|reference|identity)_[a-z_]{1,60}',reason))
            from .image_transport import failure
            progress=event.get_extra('qqbot_image_progress') or {}
            detail=failure(exc,progress) if not unsupported else None
            outcome=detail[1] if detail else 'failed'
            plugin._audit('image_generation',{'status':'failed','outcome':outcome,'stage':progress.get('stage'),
                'animated':animated,'error':type(exc).__name__,'http_status':code,
                'reason':detail[0] if detail else 'gif_not_enabled' if unsupported else reason if known else 'generation_failed'})
            event.set_extra('qqbot_image_progress',{**progress,'stage':'finished','outcome':outcome})
            job=current_job.get()
            if job:job.reply_purpose='error_notification'
            message=('这次画出来的内容没通过检查，先不给你发错图' if reason=='image_quality_rejected'
                else '这段画面要求太长了，拆成两次画吧' if reason in {'image_request_too_long','image_prompt_too_long'}
                else '角色参考图暂时读不到，这次没生成' if reason.startswith('identity_reference')
                else '这张动作参考图现在读不到了，再发一下原图，本龙照着拍' if reason in {'image_reference_missing','image_reference_expired'}
                else '这条接口还没开放动图，暂时画不了会动的；静态图可以正常画' if unsupported
                else '动图接口这次没接通，没生成成功' if animated else '图片这次没生成成功，稍后再试一下')
            if detail:message=detail[2]
            response=LLMResponse(role='assistant',completion_text=message)
            response.qqbot_status_receipt=message
        event.set_extra('qqbot_image_api_result',response);return response
    def declined(args,kwargs,event):
        if not event or event.get_extra('qqbot_visual_route')!='none':return args,kwargs
        payload=args[0] if args else kwargs.get('payloads')
        if not isinstance(payload,dict):return args,kwargs
        note='\n本轮画面需求理解已确认：用户没有要求现在创作图片或动图。忽略基于词面加入的生图提示，正常回答原消息，不调用生图或承诺附图。'
        messages=list(payload.get('messages',[]))
        messages.insert(0,{'role':'system','content':note});payload={**payload,'messages':messages}
        return ((payload,*args[1:]),kwargs) if args else (args,{**kwargs,'payloads':payload})
    @wraps(old)
    async def query(provider,*args,**kwargs):
        event=selected()
        if event:
            payload=args[0] if args else kwargs.get('payloads',{})
            if isinstance(payload,dict):event.set_extra('qqbot_image_projected_context',payload.get('messages',[]))
            result=await run(provider,event)
            if result is not None:return result
        if active:args,kwargs=declined(args,kwargs,event or getattr(current_job.get(),'event',None))
        return await old(provider,*args,**kwargs)
    @wraps(old_stream)
    async def stream(provider,*args,**kwargs):
        event=selected()
        if event:
            payload=args[0] if args else kwargs.get('payloads',{})
            if isinstance(payload,dict):event.set_extra('qqbot_image_projected_context',payload.get('messages',[]))
            result=await run(provider,event)
            if result is not None:
                yield result
                return
        if active:args,kwargs=declined(args,kwargs,event or getattr(current_job.get(),'event',None))
        async for part in old_stream(provider,*args,**kwargs):yield part
    cls._query=query;cls._query_stream=stream
    def restore():
        nonlocal active
        active=False
        if cls._query is query:cls._query=old
        if cls._query_stream is stream:cls._query_stream=old_stream
    return restore

def apply_status_receipt(plugin,event,run_context,response):
    """Internal, factual failure receipts must not become invented capability denials."""
    text=getattr(response,'qqbot_status_receipt',None)
    if not isinstance(text,str) or not text:return False
    from astrbot.api.message_components import Plain
    from astrbot.core.agent.message import TextPart
    from .conversation_review import approve_once
    response.completion_text=text
    chain=getattr(response,'result_chain',None)
    if chain:
        plains=[p for p in chain.chain if isinstance(p,Plain)]
        for i,p in enumerate(plains):p.text=text if i==0 else ''
        if not plains:chain.chain.insert(0,Plain(text))
    messages=getattr(run_context,'messages',[]) or []
    if messages and messages[-1].role=='assistant':
        content=messages[-1].content
        messages[-1].content=([p for p in content if not isinstance(p,TextPart)] if isinstance(content,list) else [])+[TextPart(text=text)]
    event.set_extra('qqbot_send_path','template_receipt')
    event.set_extra('qqbot_agent_final_done',True);event.set_extra('qqbot_final_text',text)
    approve_once(event.unified_msg_origin,text,policy_version=plugin.review_policy_version())
    if getattr(plugin,'_work',None):plugin._work.mark(event,'final_ready')
    return True
