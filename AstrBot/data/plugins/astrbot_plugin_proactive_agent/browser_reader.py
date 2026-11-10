"""Cancellable subprocess bridge; never runs a browser on the chat/model lane."""
import asyncio
import json
import os
from pathlib import Path
import sys

from .article_body import site_for, article_key


def interpreter():
    project = Path(__file__).resolve().parents[4]
    relative = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
    dedicated = project/'tools/web-reader-env'/relative
    return str(dedicated) if dedicated.is_file() else sys.executable


def stop_tree(pid, created):
    import psutil
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time()-created) > .01:
            return
        children = proc.children(recursive=True)
        # Kill owned descendants first; never target the user's Edge by name.
        for child in children:
            with __import__('contextlib').suppress(psutil.Error):
                child.terminate()
        proc.terminate()
        _, live = psutil.wait_procs([*children, proc], timeout=.5)
        for child in live:
            with __import__('contextlib').suppress(psutil.Error):
                child.kill()
    except psutil.Error:
        pass


async def read_browser(url, profile_root, reserve):
    import psutil
    if not article_key(url):
        raise ValueError('该网址没有专用正文读取规则')
    worker = Path(__file__).with_name('browser_reader_worker.py')
    process = await asyncio.create_subprocess_exec(interpreter(), '-B', '-X', 'utf8', str(worker),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=160000, **({'creationflags':0x08000000} if os.name == 'nt' else {}))
    created = psutil.Process(process.pid).create_time()
    try:
        process.stdin.write((json.dumps({'url':url, 'profile_root':str(profile_root)})+'\n').encode())
        await process.stdin.drain()
        async with asyncio.timeout(35):
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise ValueError('专用阅读器提前退出，请检查网页阅读依赖和浏览器安装')
                value = json.loads(line)
                if value.get('reserve'):
                    await reserve()
                    process.stdin.write(b'ok\n')
                    await process.stdin.drain()
                elif value.get('error'):
                    raise ValueError('原帖正文未读取：'+value['error'])
                elif value.get('result'):
                    result = value['result']
                    if not result.get('body_verified') or result.get('url') != url:
                        raise ValueError('阅读器未提供对应原帖正文')
                    await process.wait()
                    return result
    finally:
        if process.returncode is None:
            await asyncio.to_thread(stop_tree, process.pid, created)
            await process.wait()


def authorization_newer(profile_root, url, blocked_at):
    site = site_for(url)
    if not site:
        return False
    try:
        value = json.loads((Path(profile_root)/site/'status.json').read_text('utf-8'))
        return value.get('last_success', 0) > blocked_at
    except (OSError, ValueError):
        return False


def reader_status(profile_root):
    """Small management-only view. Never return cookies, HTML or request metadata."""
    result={}
    for site in ('tieba','zhihu'):
        try:
            path=Path(profile_root)/site/'status.json'
            if path.stat().st_size>64000:raise ValueError('oversized status')
            value=json.loads(path.read_text('utf-8'))
            result[site]={k:value.get(k) for k in ('state','error','last_success','title','at','reader')}
        except (OSError,ValueError):result[site]={'state':'not_initialized'}
    from .browser_bridge import bridge_status
    result['zhihu']['bridge']=bridge_status(profile_root)
    return result
