"""Own-process supervisor and connection checks; never kills unrelated apps."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
LOGS = ROOT/'logs'


def write_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


def api(path, payload=None, timeout=8):
    import jwt
    config=json.loads((ROOT/'AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    token=jwt.encode({'username':config['dashboard']['username'],'exp':int(time.time())+120},
                     config['dashboard']['jwt_secret'],algorithm='HS256')
    request=urllib.request.Request('http://127.0.0.1:6185'+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=timeout) as r:
        return json.load(r)


def check(deep=False):
    rows=[]
    try:
        health=api('/api/plug/astrbot_plugin_proactive_agent/health')['data']['checks']
        for key,ok in health.items():
            rows.append({'name':key,'ok':bool(ok),'detail':'正常' if ok else '连接或功能未就绪',
                         'next_step':'' if ok else '检查QQ登录、OneBot反向连接、插件日志'})
    except Exception as exc:
        rows.append({'name':'AstrBot','ok':False,'detail':'控制台健康接口不可达：'+type(exc).__name__,
                     'next_step':'运行 python manage.py start，查看 logs/astrbot.log'})
    try:
        bili=api('/api/plug/astrbot_plugin_bili_scroll/status')['data']
        rows.append({'name':'B站分享任务','ok':bool(bili.get('loop_running')),'detail':'仅检查任务，不发送消息'})
    except Exception as exc:
        rows.append({'name':'B站分享任务','ok':False,'detail':type(exc).__name__,'next_step':'在插件页面检查是否已启用刷B站插件'})
    if deep:
        try:
            config=json.loads((ROOT/'AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
            source=config['provider_sources'][0]
            from openai import OpenAI
            with OpenAI(api_key=source['key'][0],base_url=source['api_base'],timeout=35,max_retries=0) as client:
                result=client.chat.completions.create(model=config['provider'][0]['model'],
                    messages=[{'role':'user','content':'Reply with OK'}],max_tokens=10)
                ok=bool(result.choices and result.choices[0].message.content)
            rows.append({'name':'模型API实际生成','ok':ok,'detail':'已收到模型响应' if ok else '模型返回空内容'})
        except Exception as exc:
            rows.append({'name':'模型API实际生成','ok':False,'detail':type(exc).__name__,
                'next_step':'核对API地址、模型名、密钥、余额和网络；外部API不能通过重启本地程序恢复额度'})
        try:
            config=json.loads((ROOT/'AstrBot/data/config/astrbot_plugin_proactive_agent_config.json').read_text(encoding='utf-8-sig'))
            if config.get('reply_review_enabled'):
                api('/api/plug/astrbot_plugin_proactive_agent/review_preview',{'text':'今天想玩点轻松的','mode':'chat'},timeout=60)
                rows.append({'name':'可选AI二次审阅','ok':True,'detail':'已实测，未发送QQ消息'})
            else:
                rows.append({'name':'可选AI二次审阅','ok':True,'detail':'用户未启用，正常跳过'})
        except Exception as exc:
            rows.append({'name':'可选AI二次审阅','ok':False,'detail':type(exc).__name__})
    report={'time':datetime.now(timezone.utc).isoformat(),'deep':deep,'ok':all(r['ok'] for r in rows),'checks':rows}
    write_json(LOGS/'health-status.json',report)
    return report


def running():
    try:
        import psutil
        record=json.loads((LOGS/'supervisor.json').read_text())
        p=psutil.Process(record['pid'])
        return abs(p.create_time()-record['created'])<1 and str(ROOT/'manage.py') in p.cmdline() and 'supervise' in p.cmdline()
    except Exception:
        return False


def start():
    LOGS.mkdir(exist_ok=True)
    if running(): return
    if not (ROOT/'AstrBot/data/cmd_config.json').exists(): raise RuntimeError('Run bootstrap.py first')
    (LOGS/'stopped').unlink(missing_ok=True)
    with (LOGS/'supervisor.log').open('ab') as log:
        kwargs={'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS} if os.name=='nt' else {'start_new_session':True}
        subprocess.Popen([sys.executable,str(ROOT/'manage.py'),'supervise'],cwd=ROOT,stdout=log,stderr=log,stdin=subprocess.DEVNULL,
            env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},**kwargs)


def supervise():
    import psutil
    from filelock import FileLock, Timeout
    LOGS.mkdir(exist_ok=True)
    try:
        lock=FileLock(str(LOGS/'supervisor.lock'),timeout=0);lock.acquire()
    except Timeout: return
    write_json(LOGS/'supervisor.json',{'pid':os.getpid(),'created':psutil.Process().create_time()})
    child=None
    restarts=[]
    bad=0
    try:
        while not (LOGS/'stopped').exists():
            if child is None or child.poll() is not None or (LOGS/'restart-request').exists():
                restarts=[t for t in restarts if time.time()-t<3600]
                if len(restarts)>=3:
                    time.sleep(10); continue
                (LOGS/'restart-request').unlink(missing_ok=True)
                if child is not None and child.poll() is None:
                    stop_child(child)
                with (LOGS/'astrbot.log').open('ab') as output:
                    child=subprocess.Popen([sys.executable,'-m','astrbot.cli','run'],cwd=ROOT/'AstrBot',
                        stdout=output,stderr=output,stdin=subprocess.DEVNULL,
                        env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','ASTRBOT_DISABLE_METRICS':'1'})
                restarts.append(time.time()); bad=0
                time.sleep(20)
            for _ in range(6):
                if (LOGS/'stopped').exists(): break
                time.sleep(10)
            report=check()
            # Only a failed AstrBot API is locally repairable. An offline QQ or an
            # unavailable external model is diagnosed, not an excuse to restart loops.
            unavailable=any(r['name']=='AstrBot' and not r['ok'] for r in report['checks'])
            bad=bad+1 if unavailable else 0
            if bad>=2 and time.time()-restarts[-1]>=300:
                (LOGS/'restart-request').touch()
    finally:
        if child is not None and child.poll() is None: stop_child(child)
        (LOGS/'supervisor.json').unlink(missing_ok=True)
        lock.release()


def stop_child(child):
    import psutil
    try:
        parent=psutil.Process(child.pid)
        children=parent.children(recursive=True)
        child.terminate()
        _,alive=psutil.wait_procs([parent]+children,timeout=10)
        for proc in alive:
            try: proc.kill()
            except psutil.NoSuchProcess: pass
    except psutil.NoSuchProcess: pass


def panel_job(mode, job_id):
    # Runs in an independent worker; survives a supervised AstrBot restart.
    try:
        if mode=='Repair':
            if not running(): start()
            report=check(True)
            if any(r['name']=='AstrBot' and not r['ok'] for r in report['checks']):
                (LOGS/'restart-request').touch(); time.sleep(35); report=check(True)
        else: report=check(mode=='Deep')
        result={'id':job_id,'mode':mode,'status':'completed','exit_code':0 if report['ok'] else 1}
    except Exception as exc:
        result={'id':job_id,'mode':mode,'status':'failed','message':type(exc).__name__}
    write_json(LOGS/'panel-health-job.json',result)


def open_consoles():
    # Desktop-only. The server supervisor and panel jobs never open a browser.
    if os.name != 'nt':
        return
    onebot = ''
    settings = ROOT/'local/settings.json'
    if settings.exists():
        onebot = json.loads(settings.read_text(encoding='utf-8-sig')).get('onebot_console_url', '')
    command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File', str(ROOT/'scripts/open-consoles.ps1')]
    if onebot: command += ['-OneBotUrl', onebot]
    subprocess.run(command, check=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','status','check','deep','repair','supervise','panel-job'])
    parser.add_argument('--mode',choices=['Check','Deep','Repair'],default='Check')
    parser.add_argument('--job-id',default='manual')
    args=parser.parse_args()
    if args.action=='start': start(); print('Supervisor started; dashboard http://127.0.0.1:6185')
    elif args.action=='stop': LOGS.mkdir(exist_ok=True); (LOGS/'stopped').touch(); print('Stop requested; owned processes stop within 20 seconds')
    elif args.action=='supervise': supervise()
    elif args.action=='status': print('running' if running() else 'stopped')
    elif args.action=='panel-job': panel_job(args.mode,args.job_id)
    elif args.action=='repair':
        try: panel_job('Repair','manual'); print('See logs/health-status.json')
        finally: open_consoles()
    else:
        report=check(args.action=='deep'); print(json.dumps(report,ensure_ascii=True,indent=2)); sys.exit(0 if report['ok'] else 1)
