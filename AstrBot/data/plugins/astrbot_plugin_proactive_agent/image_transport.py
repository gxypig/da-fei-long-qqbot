"""Bounded image wait and sanitized phase diagnostics; no generation retries."""
import asyncio
import time
import httpx

async def analyze_image(plugin,provider,system,payload,images,*,planning=False,timeout_seconds=90):
    """Bounded read-only preparation; never retry a submitted image render."""
    import json
    from .conversation_review import json_object
    deadline=time.monotonic()+(timeout_seconds if planning else 55)
    for attempt in range(2 if planning else 1):
        try:
            async with asyncio.timeout(max(.01,deadline-time.monotonic())):
                result=await provider.text_chat(system_prompt=system,prompt=json.dumps(payload,ensure_ascii=False),
                    image_urls=list(images),request_max_retries=1)
            return json_object(result.completion_text)
        except Exception as exc:
            transient=isinstance(exc,(TimeoutError,httpx.TransportError)) or type(exc).__name__ in {'APITimeoutError','APIConnectionError'} or getattr(exc,'status_code',None) in {502,503,504}
            if not planning or attempt or not transient or deadline-time.monotonic()<5:raise
            plugin._audit('image_preparation_retry',{'attempt':attempt+2,'error':type(exc).__name__,'remaining_seconds':round(deadline-time.monotonic(),1)})


def rejection_details(exc):
    """Keep only public, finite moderation categories, never provider prose."""
    body=getattr(exc,'body',None)
    if isinstance(body,dict):body=body.get('error',body)
    if not isinstance(body,dict):return {}
    code=body.get('code')
    if code not in {'content_policy_violation','moderation_blocked','safety_violation'}:return {}
    value={'provider_code':code}
    details=body.get('moderation_details')
    if isinstance(details,dict):
        stage=details.get('moderation_stage')
        if stage in {'input','output','unknown'}:value['moderation_stage']=stage
        categories=details.get('categories')
        if isinstance(categories,list):value['moderation_categories']=sorted({x for x in categories if isinstance(x,str) and x in {'sexual','violence','harassment','self-harm','hate','illicit'}})
    return value


def wait_seconds(cfg):
    value=cfg('image_generation_timeout_seconds',600)
    return value if type(value) is int and 180<=value<=900 else 600


class ImageWait:
    def __init__(self,plugin,event,clock=time.monotonic):
        self.plugin,self.event=plugin,event
        self.clock=clock
        self.seconds=wait_seconds(plugin._cfg)
        self.started=clock();self.deadline=None;self.attempts=0
        self.received=False;self.submitted=False

    def phase(self,name,**extra):
        value={'stage':name,'elapsed_ms':round((self.clock()-self.started)*1000),
               'attempts':self.attempts,'submitted':self.submitted,'received':self.received,
               'wait_limit_seconds':self.seconds,**extra}
        self.event.set_extra('qqbot_image_progress',value)
        self.plugin._audit('image_generation_stage',value)

    async def request(self,client,files,args):
        # One total render budget, shared by explicit quality correction / GIF
        # capability fallback. Queue wait before the first POST is not render time.
        now=self.clock()
        if self.deadline is None:self.deadline=now+self.seconds
        remaining=self.deadline-now
        if remaining<=0:raise TimeoutError('image_render_budget_exhausted')
        self.attempts+=1;self.submitted=True;self.received=False
        self.phase('rendering')
        client=client.with_options(timeout=httpx.Timeout(remaining,connect=min(10,remaining),
            write=min(60,remaining),pool=min(10,remaining)),max_retries=0)
        async with asyncio.timeout(remaining):
            result=await client.images.edit(image=list(files),**args) if files else await client.images.generate(**args)
        self.received=True;self.phase('result_received')
        return result


def failure(exc,progress):
    """Do not log provider bodies (they may contain prompts, URLs or tokens)."""
    code=getattr(exc,'status_code',None)
    phase=progress.get('stage','preparing')
    reason=str(exc)
    timed=isinstance(exc,TimeoutError) or type(exc).__name__=='APITimeoutError'
    network=isinstance(exc,httpx.TransportError) or type(exc).__name__=='APIConnectionError'
    body=getattr(exc,'body',None)
    if isinstance(body,dict):
        body=body.get('error',body)
    rejection=body.get('code') if isinstance(body,dict) else None
    if rejection in {'content_policy_violation','moderation_blocked','safety_violation'}:
        if rejection_details(exc).get('moderation_stage')=='output':
            return 'content_rejected','rejected','这次图片服务生成的结果未通过内容审核，没有返回可发送的图片；这不代表缺少生图工具'
        return 'content_rejected','rejected','图片服务的内容审核拒绝了这次请求，未返回图片；这不是缺少生图工具，请换一张合适的参考图或重新描述新的画面'
    if code in {401,403}:
        return 'authorization_failed','failed','图片服务的授权暂时不可用，需要检查接口配置'
    if code==429:
        return 'rate_limited','failed','图片服务暂时限流或额度不足，这次没有拿到图片'
    if reason=='image_render_budget_exhausted':
        return 'render_budget_exhausted','unverified','这次画面还没通过检查，等待期限已经到了，没有继续重画'
    if phase=='verifying' and (timed or network or code):
        return 'verification_unavailable','unverified','画面已经返回，但检查暂时没完成，先不把未检查的图发给你'
    if phase=='downloading':
        return 'download_unavailable','unretrieved','图片服务返回了结果，但图片暂时取不回来，这次没有重新画'
    if (timed or network or code in {408,502,503,504}) and progress.get('submitted') and not progress.get('received'):
        return 'render_timeout' if timed else 'render_connection_lost','unknown','这次等了较久，仍没收到图片结果；无法确认服务端是否已完成，没有重复提交'
    if timed or network:
        if timed:return 'preparation_unavailable','failed','这次整理画面要求超时了，尚未提交生图；可以重新发来本次参考图继续'
        return 'preparation_unavailable','failed','这次整理画面要求时连接中断，还没拿到可发送的图片'
    if code==400:
        return 'request_rejected','rejected','图片服务拒绝了这次请求，暂时没有可发送的图片'
    return None
