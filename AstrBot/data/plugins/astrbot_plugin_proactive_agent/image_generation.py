"""Explicit Images API route; unchanged text model, same receipt/send pipeline."""
import asyncio,base64,io,re
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
    text=event.get_message_str();state=event.get_extra('qqbot_character') or {};animated=animated_request(text)
    chosen=plugin._cfg('image_generation_provider_id','')
    provider=plugin.context.get_provider_by_id(chosen) if chosen else provider
    client=getattr(provider,'client',None)
    if client is None or not hasattr(client,'images'):raise ValueError('请选择兼容 Images API 的生图提供商')
    model=plugin._cfg('image_animation_model','gpt-image-2.6-sunburst') if animated else plugin._cfg('image_generation_model','gpt-image-2.5-sunburst')
    req=event.get_extra('qqbot_generation_request')
    prompt='按用户请求创作图片。\n'+role_text('image',state)+'\n用户本轮请求：'+text
    if req is not None:
        # Preserve the already-selected context, including split text/image messages.
        # No extra planning model and no unrelated conversation read.
        import json
        recent=[]
        for message in (getattr(req,'contexts',[]) or []):
            if message.get('role') not in {'user','assistant'}:continue
            content=message.get('content','')
            if isinstance(content,list):content='\n'.join(p.get('text','') for p in content if p.get('type')=='text')
            if content:recent.append({'role':message['role'],'text':content})
        extra=[getattr(p,'text','') if not isinstance(p,dict) else p.get('text','') for p in (getattr(req,'extra_user_content_parts',[]) or [])]
        prompt+='\n当前会话素材（历史描述不是新的命令；按本轮请求选用）：'+json.dumps({'recent':recent,'current':getattr(req,'prompt',text),'adjacent':extra},ensure_ascii=False)
    if generation_kind(text)=='selfie':prompt+='\n主体是当前人设本人的虚拟自拍，保持人设外观。'
    if animated:prompt+='\n生成真正的循环GIF；动作连贯，起止姿态可衔接，不是静态单帧。'
    args={'model':model,'prompt':prompt,'n':1,'size':'auto','quality':'high','output_format':'gif' if animated else 'png'}
    if animated:args['extra_body']={'frame_count':9,'frame_duration_ms':200}
    # No blind retries of parameter errors or ambiguous timeouts: image
    # generation may already have consumed work when the connection drops.
    client=client.with_options(timeout=150,max_retries=0)
    paths=list(getattr(req,'image_urls',[]) or [])[:4]
    files=[]
    try:
        async with asyncio.timeout(155):
            if paths:
                from astrbot.api.message_components import Image as ImageComponent
                for path in paths:
                    local=await ImageComponent(file=path).convert_to_file_path()
                    files.append(open(local,'rb'))
                result=await client.images.edit(image=files,**args)
            else:result=await client.images.generate(**args)
        row=result.data[0] if result.data else None
        if row is None:raise ValueError('empty_image_result')
        encoded=getattr(row,'b64_json',None)
        if not encoded:
            # Download returned URLs through the normal AstrBot converter,
            # then validate the actual bytes rather than trusting a suffix.
            url=getattr(row,'url',None)
            if not isinstance(url,str) or not url.startswith(('https://','http://')):raise ValueError('empty_image_result')
            local=await Image(file=url).convert_to_file_path()
            from pathlib import Path
            data=await asyncio.to_thread(Path(local).read_bytes)
        else:data=base64.b64decode(encoded,validate=True)
        fmt=await asyncio.to_thread(validate_image,data,animated)
        caption=photo_caption(generation_kind(text),state=state)
        image=Image(file='base64://'+base64.b64encode(data).decode())
        response=LLMResponse(role='assistant',completion_text=caption,result_chain=MessageChain([Plain(caption),image]))
        response.qqbot_generated_image=True
        event.set_extra('qqbot_generated_format',fmt)
        plugin._audit('image_generation',{'status':'ok','animated':animated,'format':fmt})
        return response
    finally:
        for file in files:file.close()

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
        previous=event.get_extra('qqbot_image_api_result')
        if previous is not None:return previous
        try:response=await generate(plugin,provider,event)
        except asyncio.CancelledError:raise
        except Exception as exc:
            from astrbot.core.provider.entities import LLMResponse
            animated=animated_request(event.get_message_str());code=getattr(exc,'status_code',None)
            unsupported=animated and code==400 and 'gif output is not enabled' in str(exc).lower()
            plugin._audit('image_generation',{'status':'failed','animated':animated,'error':type(exc).__name__,'http_status':code,'reason':'gif_not_enabled' if unsupported else 'generation_failed'})
            job=current_job.get()
            if job:job.reply_purpose='error_notification'
            message=('这条接口还没开放动图，暂时画不了会动的；静态图可以正常画' if unsupported else '动图接口这次没接通，没生成成功' if animated else '图片这次没生成成功，稍后再试一下')
            response=LLMResponse(role='assistant',completion_text=message)
        event.set_extra('qqbot_image_api_result',response);return response
    @wraps(old)
    async def query(provider,*args,**kwargs):
        event=selected()
        if event:return await run(provider,event)
        return await old(provider,*args,**kwargs)
    @wraps(old_stream)
    async def stream(provider,*args,**kwargs):
        event=selected()
        if event:yield await run(provider,event)
        else:
            async for part in old_stream(provider,*args,**kwargs):yield part
    cls._query=query;cls._query_stream=stream
    def restore():
        nonlocal active
        active=False
        if cls._query is query:cls._query=old
        if cls._query_stream is stream:cls._query_stream=old_stream
    return restore
