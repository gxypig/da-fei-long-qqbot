"""Optional localhost bridge to a user-installed, narrowly scoped Edge extension.

This stays disabled unless the owner explicitly installs the extension and its
local config. It never reads another browser's profile or authentication data.
"""
import asyncio
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from .article_body import article_key,extract_article

_instances={}


def configured(root):
    try:
        path=Path(root)/'bridge.json'
        if path.stat().st_size>8000:return None
        cfg=json.loads(path.read_text('utf-8'))
        if cfg.get('enabled') is not True or len(cfg.get('token',''))<40:return None
        if cfg.get('port')!=6197:return None
        return cfg
    except (OSError,ValueError,TypeError):return None


class BodyBridge:
    def __init__(self,config):
        self.config=config;self.runner=None;self.jobs={};self.last_seen=0;self.lock=asyncio.Lock()
    async def start(self):
        if self.runner:return
        async with self.lock:
            if self.runner:return
            app=web.Application(client_max_size=2200000)
            app.router.add_get('/reading/task',self.poll)
            app.router.add_post('/reading/result',self.result)
            app.router.add_options('/reading/{tail:.*}',self.options)
            runner=web.AppRunner(app,access_log=None);await runner.setup()
            try:await web.TCPSite(runner,'127.0.0.1',self.config['port']).start()
            except BaseException:await runner.cleanup();raise
            self.runner=runner
    def headers(self,request):
        origin=request.headers.get('Origin','')
        allowed=self.config.get('extension_origin','')
        if origin and (origin!=allowed or not origin.startswith('chrome-extension://')):raise web.HTTPForbidden()
        return {'Access-Control-Allow-Origin':allowed,'Vary':'Origin','Cache-Control':'no-store'}
    def authorize(self,request):
        headers=self.headers(request)
        expected='Bearer '+self.config['token']
        if not hmac.compare_digest(request.headers.get('Authorization',''),expected):raise web.HTTPUnauthorized()
        self.last_seen=time.time()
        return headers
    async def options(self,request):
        return web.Response(headers={**self.headers(request),'Access-Control-Allow-Methods':'GET,POST,OPTIONS','Access-Control-Allow-Headers':'Authorization,Content-Type'})
    async def poll(self,request):
        headers=self.authorize(request)
        now=time.time()
        active=request.query.get('active')
        if active:
            job=self.jobs.get(active)
            return web.json_response({'active':bool(job and not job['future'].done() and job['until']>=now)},headers=headers)
        for ident,job in list(self.jobs.items()):
            if job['future'].done() or job['until']<now:continue
            if not job['claimed']:
                job['claimed']=True
                return web.json_response({'id':ident,'url':job['url'],'until':job['until']},headers=headers)
        return web.json_response({'idle':True},headers=headers)
    async def result(self,request):
        headers=self.authorize(request)
        value=await request.json()
        if not isinstance(value,dict):raise web.HTTPBadRequest()
        job=self.jobs.get(value.get('id'))
        if not job or job['future'].done() or job['until']<time.time():return web.json_response({'expired':True},headers=headers)
        if value.get('error'):
            job['future'].set_exception(ValueError('普通浏览器读取失败：'+str(value['error'])[:180]))
        else:
            target=value.get('url','');requested=job['url']
            if urlsplit(target).scheme!='https' or urlsplit(target).hostname!=urlsplit(requested).hostname or article_key(target)!=article_key(requested):
                raise web.HTTPBadRequest(text='wrong article')
            html=value.get('html')
            if not isinstance(html,str) or len(html.encode())>2*1024*1024:raise web.HTTPBadRequest(text='invalid body')
            try:
                body=await asyncio.to_thread(extract_article,html,requested)
                body['reader']='owner_browser_bridge'
            except ValueError as exc:
                if not job['future'].done():job['future'].set_exception(exc)
            else:
                if not job['future'].done():job['future'].set_result(body)
        return web.json_response({'accepted':True},headers=headers)
    async def read(self,url,reserve):
        if not article_key(url):raise ValueError('浏览器桥接仅接受已支持的原帖网址')
        await self.start();await reserve()
        ident=secrets.token_hex(16);future=asyncio.get_running_loop().create_future()
        self.jobs[ident]={'url':url,'future':future,'until':time.time()+65,'claimed':False}
        try:
            return await asyncio.wait_for(future,65)
        except TimeoutError:raise ValueError('普通 Edge 阅读桥接未返回正文，请确认已安装扩展且该浏览器正在运行') from None
        finally:
            self.jobs.pop(ident,None)
            if not future.done():future.cancel()
    async def close(self):
        for job in self.jobs.values():
            if not job['future'].done():job['future'].cancel()
        self.jobs.clear()
        if self.runner:await self.runner.cleanup();self.runner=None


async def start_bridge(root):
    config=configured(root)
    if not config:return None
    key=str(Path(root).resolve());bridge=_instances.get(key)
    if bridge is None:bridge=_instances[key]=BodyBridge(config)
    await bridge.start()
    return bridge


def bridge_status(root):
    bridge=_instances.get(str(Path(root).resolve()))
    return {'enabled':bool(configured(root)), 'connected':bool(bridge and time.time()-bridge.last_seen<45)}


async def read_owner_browser(url,root,reserve):
    bridge=await start_bridge(root)
    if bridge is None:raise ValueError('尚未安装普通 Edge 正文读取扩展')
    from .article_body import site_for
    folder=Path(root)/site_for(url);folder.mkdir(parents=True,exist_ok=True)
    def save(**fields):
        try:previous=json.loads((folder/'status.json').read_text('utf-8'))
        except (OSError,ValueError):previous={}
        temp=folder/'bridge-status.tmp'
        temp.write_text(json.dumps({**previous,**fields,'at':time.time(),'reader':'owner_browser_bridge'},ensure_ascii=False),'utf-8')
        temp.replace(folder/'status.json')
    try:result=await bridge.read(url,reserve)
    except asyncio.CancelledError:raise
    except Exception as exc:
        await asyncio.to_thread(save,state='body_unavailable',error=str(exc)[:250])
        raise
    await asyncio.to_thread(save,state='ready',error='',last_success=time.time(),title=result['title'])
    return result


async def close_bridge(root):
    bridge=_instances.pop(str(Path(root).resolve()),None)
    if bridge:await bridge.close()
