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
    text=event.get_message_str();state=event.get_extra('qqbot_character') or {};animated=animated_request(text)
    text_provider=provider
    chosen=plugin._cfg('image_generation_provider_id','')
    provider=plugin.context.get_provider_by_id(chosen) if chosen else provider
    client=getattr(provider,'client',None)
    if client is None or not hasattr(client,'images'):raise ValueError('请选择兼容 Images API 的生图提供商')
    model=plugin._cfg('image_animation_model','gpt-image-2.6-sunburst') if animated else plugin._cfg('image_generation_model','gpt-image-2.5-sunburst')
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
    if checked:
        plan=image_spec.normalize_plan(await analyze(image_spec.PLAN,{**material,'identity':canonical['description'] if canonical else role_text('image',state)[:6000]}),material,canonical)
    else:
        plan=image_spec.normalize_plan({'scene':text,'requirements':[],'subject_self':generation_kind(text)=='selfie'},material,canonical)
    # No unbounded history and no conversation-specific personality overrides of
    # an explicitly installed visual identity. Caption persona remains unchanged.
    if not plan['identity'] and plan['subject_self']:plan['identity']=role_text('image',state)[:6000]
    base_args={'model':model,'n':1,'size':'auto','quality':'high'}
    # No blind retries of parameter errors or ambiguous timeouts: image
    # generation may already have consumed work when the connection drops.
    client=client.with_options(timeout=150,max_retries=0)
    paths=([canonical['path']] if canonical and plan['subject_self'] else [])+image_spec.reference_paths(event,req)
    files=[];reference_uris=[]
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
        for index,path in enumerate(paths):
            local=await Image(file=path).convert_to_file_path();raw=await asyncio.to_thread(reference_bytes,local)
            files.append((f'reference-{index+1}.png',raw,'image/png'))
            reference_uris.append('data:image/png;base64,'+base64.b64encode(raw).decode())
        async def request(frames,failures):
            args={**base_args,'prompt':image_spec.prompt(plan,frames,failures),'output_format':'gif' if animated and not frames else 'png'}
            if frames:args['size']='1536x1024'
            elif animated:args['extra_body']={'frame_count':9,'frame_duration_ms':200}
            async def call():
                async with asyncio.timeout(155):
                    return await client.images.edit(image=list(files),**args) if files else await client.images.generate(**args)
            scheduler=getattr(getattr(plugin,'_work',None),'scheduler',None)
            result=await scheduler.run(provider,call,kind='interactive') if scheduler else await call()
            row=result.data[0] if result.data else None
            if row is None:raise ValueError('empty_image_result')
            encoded=getattr(row,'b64_json',None)
            if encoded:raw=base64.b64decode(encoded,validate=True)
            else:
                url=getattr(row,'url',None)
                if not isinstance(url,str) or not url.startswith(('https://','http://')):raise ValueError('empty_image_result')
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
                preview=await asyncio.to_thread(sample_sheet,data) if animated and not frames else data
                views=['data:image/png;base64,'+base64.b64encode(preview).decode()]
                if canonical and plan['subject_self']:views.append(reference_uris[0])
                verdict=await analyze(image_spec.VERIFY,{**plan,'format':'frames_sheet' if frames else 'native_gif' if animated else 'static'},views)
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
        caption=photo_caption(generation_kind(text),state=state)
        image=Image(file='base64://'+base64.b64encode(data).decode())
        response=LLMResponse(role='assistant',completion_text=caption,result_chain=MessageChain([Plain(caption),image]))
        response.qqbot_generated_image=True
        event.set_extra('qqbot_generated_format',fmt)
        event.set_extra('qqbot_image_provenance',{'method':'generated_action_frames' if frames else 'native_gif' if animated else 'static',
            'requirements':plan['requirements'],'camera_mode':plan['camera_mode'],'identity_sha256':canonical['sha256'] if canonical and plan['subject_self'] else None,'checked':checked})
        plugin._audit('image_generation',{'status':'ok','animated':animated,'format':fmt,'method':'generated_action_frames' if frames else 'native','checked':checked})
        return response
    finally:
        files.clear();reference_uris.clear()

def install(plugin):
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from .reply_jobs import current_job
    from .visual_intent import generation_kind
    cls=ProviderOpenAIOfficial;old=cls._query;old_stream=cls._query_stream;active=True
    def selected():
        from .work_scheduler import _kind
        job=current_job.get();event=getattr(job,'event',None)
        if (active and plugin._cfg('image_generation_api_enabled',False) and event
            and _kind.get() not in {'background','review','auxiliary','scheduled'}
            and generation_kind(event.get_message_str())!='none'):return event
    async def run(provider,event):
        lock=event.get_extra('qqbot_image_api_lock')
        if lock is None:lock=asyncio.Lock();event.set_extra('qqbot_image_api_lock',lock)
        async with lock:
            if event.get_extra('qqbot_image_api_cancelled'):raise asyncio.CancelledError()
            return await once(provider,event)
    async def once(provider,event):
        previous=event.get_extra('qqbot_image_api_result')
        if previous is not None:return previous
        try:response=await generate(plugin,provider,event)
        except asyncio.CancelledError:
            event.set_extra('qqbot_image_api_cancelled',True);raise
        except Exception as exc:
            from astrbot.core.provider.entities import LLMResponse
            animated=animated_request(event.get_message_str());code=getattr(exc,'status_code',None)
            unsupported=animated and code==400 and 'gif output is not enabled' in str(exc).lower()
            plugin._audit('image_generation',{'status':'failed','animated':animated,'error':type(exc).__name__,'http_status':code,'reason':'gif_not_enabled' if unsupported else 'generation_failed'})
            job=current_job.get()
            if job:job.reply_purpose='error_notification'
            reason=str(exc)
            message=('这次画出来的内容没通过检查，先不给你发错图' if reason=='image_quality_rejected'
                else '这段画面要求太长了，拆成两次画吧' if reason in {'image_request_too_long','image_prompt_too_long'}
                else '角色参考图暂时读不到，这次没生成' if reason.startswith('identity_reference')
                else '这条接口还没开放动图，暂时画不了会动的；静态图可以正常画' if unsupported
                else '动图接口这次没接通，没生成成功' if animated else '图片这次没生成成功，稍后再试一下')
            response=LLMResponse(role='assistant',completion_text=message)
        event.set_extra('qqbot_image_api_result',response);return response
    @wraps(old)
    async def query(provider,*args,**kwargs):
        event=selected()
        if event:
            payload=args[0] if args else kwargs.get('payloads',{})
            if isinstance(payload,dict):event.set_extra('qqbot_image_projected_context',payload.get('messages',[]))
            return await run(provider,event)
        return await old(provider,*args,**kwargs)
    @wraps(old_stream)
    async def stream(provider,*args,**kwargs):
        event=selected()
        if event:
            payload=args[0] if args else kwargs.get('payloads',{})
            if isinstance(payload,dict):event.set_extra('qqbot_image_projected_context',payload.get('messages',[]))
            yield await run(provider,event)
        else:
            async for part in old_stream(provider,*args,**kwargs):yield part
    cls._query=query;cls._query_stream=stream
    def restore():
        nonlocal active
        active=False
        if cls._query is query:cls._query=old
        if cls._query_stream is stream:cls._query_stream=old_stream
    return restore
