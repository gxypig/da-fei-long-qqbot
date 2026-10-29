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
import urllib.error
from tools.ops_runtime import atomic_json,diagnostic,identity,owned,_pipe
import threading
import signal

ROOT = Path(__file__).resolve().parent
LOGS = ROOT/'logs'

class UnsafeShutdown(RuntimeError):pass


def write_json(path, value):
    atomic_json(path,value)


def api(path, payload=None, timeout=8):
    import jwt
    config=json.loads((ROOT/'AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    token=jwt.encode({'username':config['dashboard']['username'],'exp':int(time.time())+120},
                     config['dashboard']['jwt_secret'],algorithm='HS256')
    port=int(config['dashboard'].get('port',6185))
    request=urllib.request.Request('http://127.0.0.1:'+str(port)+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=timeout) as r:
        return json.load(r)


def check(deep=False):
    rows=[]
    try:
        state=api('/api/plug/astrbot_plugin_proactive_agent/health')['data'];health=state['checks']
        for key,ok in health.items():
            rows.append({'name':key,'ok':bool(ok),'detail':'正常' if ok else '投递未完成或待核实' if key=='necessary_delivery' else '连接或功能未就绪',
                         'repairable':key not in {'necessary_delivery','persistence'},
                         'next_step':'' if ok else '检查QQ登录、OneBot反向连接、插件日志'})
        delivery=state.get('delivery',{})
    except Exception as exc:
        auth_error=isinstance(exc,urllib.error.HTTPError) and exc.code in {400,401,403,404,429}
        rows.append({'name':'AstrBot','ok':False,'detail':'控制台健康接口不可达：'+type(exc).__name__,
                     'repairable':not auth_error,
                     'next_step':'检查控制台鉴权和插件版本，不反复重启' if auth_error else '运行 python manage.py start，查看 logs/astrbot.log'})
    try:
        bili=api('/api/plug/astrbot_plugin_bili_scroll/status')['data']
        rows.append({'name':'B站分享任务','ok':bool(bili.get('loop_running')),'detail':'仅检查任务，不发送消息'})
    except Exception as exc:
        rows.append({'name':'B站分享任务','ok':False,'detail':type(exc).__name__,'next_step':'在插件页面检查是否已启用刷B站插件'})
    if deep:
        try:
            result=api('/api/plug/astrbot_plugin_proactive_agent/panel_model_probe',{},timeout=28)
            state=result.get('data',{}).get('state')
            rows.append({'name':'模型API空闲实测','ok':state in {'verified','deferred'},'probe_state':state,
                'detail':'已取得模型回复' if state=='verified' else '聊天繁忙，留待空闲；本轮未验证生成' if state=='deferred' else '未取得有效回复'})
        except Exception as exc:
            rows.append({'name':'模型API空闲实测','ok':False,'detail':type(exc).__name__,
                'next_step':'检查API地址、模型、密钥、余额和网络；不会通过反复重启恢复外部额度'})
    report={'time':datetime.now(timezone.utc).isoformat(),'deep':deep,'ok':all(r['ok'] for r in rows),'checks':rows,
        'delivery':locals().get('delivery',{})}
    report.update(generated_at=time.time(),expires_at=time.time()+240)
    diagnostic(LOGS/'health-status.json',report)
    return report


def supervisor_process():
    try:
        record=json.loads((LOGS/'supervisor.json').read_text())
        p=owned(record,ROOT)
        return p if p and p.cmdline()==record['command'] else None
    except Exception:return None

def running():
    if supervisor_process():return True
    try:return bool(owned(json.loads((LOGS/'astrbot-process.json').read_text()),ROOT))
    except Exception:return False


def start():
    from filelock import FileLock
    LOGS.mkdir(exist_ok=True)
    with FileLock(str(LOGS/'lifecycle.lock'),timeout=35):return _start()

def _start():
    LOGS.mkdir(exist_ok=True)
    if running() and (LOGS/'stopped').exists():
        end=time.monotonic()+25
        while running() and time.monotonic()<end:time.sleep(.1)
        if running():raise RuntimeError('Previous supervisor is still stopping; start was not accepted')
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
    write_json(LOGS/'supervisor.json',{**identity(psutil.Process(),ROOT),'command':psutil.Process().cmdline()})
    def stop_signal(*_):(LOGS/'stopped').touch()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM,stop_signal);signal.signal(signal.SIGINT,stop_signal)
    child=None
    state_path=LOGS/'restart-budget.json'
    try:restarts=json.loads(state_path.read_text())
    except (OSError,ValueError):restarts=[]
    bad=0
    try:
        while True:
            if (LOGS/'stopped').exists():
                try:
                    if child is not None and child.poll() is None:stop_child(child)
                except UnsafeShutdown as exc:
                    diagnostic(LOGS/'stop-failure.json',{'at':time.time(),'reason':str(exc),'stopped':False})
                    (LOGS/'stopped').unlink(missing_ok=True)
                    time.sleep(1);continue
                child=None;break
            if child is None or child.poll() is not None or (LOGS/'restart-request').exists():
                restarts=[t for t in restarts if time.time()-t<3600]
                if len(restarts)>=3:
                    time.sleep(10); continue
                (LOGS/'restart-request').unlink(missing_ok=True)
                if child is not None and child.poll() is None:
                    try:stop_child(child)
                    except UnsafeShutdown as exc:
                        diagnostic(LOGS/'stop-failure.json',{'at':time.time(),'reason':str(exc),'stopped':False})
                        time.sleep(1);continue
                child=subprocess.Popen([sys.executable,'-m','astrbot.cli','run'],cwd=ROOT/'AstrBot',
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                    env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','ASTRBOT_DISABLE_METRICS':'1'},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                child._qqbot_identity=identity(psutil.Process(child.pid),ROOT)
                write_json(LOGS/'astrbot-process.json',child._qqbot_identity)
                threading.Thread(target=_pipe,args=(child.stdout,LOGS/'astrbot.log'),daemon=True).start()
                restarts.append(time.time());write_json(state_path,restarts);bad=0
                time.sleep(20)
            for _ in range(6):
                if (LOGS/'stopped').exists(): break
                time.sleep(10)
            if (LOGS/'stopped').exists():continue
            started=time.time();diagnostic(LOGS/'watchdog-heartbeat.json',{'heartbeat':started,'state':'checking'})
            try:
                completed=subprocess.run([sys.executable,str(ROOT/'manage.py'),'check'],cwd=ROOT,
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=50,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                report=json.loads(completed.stdout)
                diagnostic(LOGS/'watchdog-heartbeat.json',{'heartbeat':time.time(),'state':'finished','exit_code':completed.returncode})
            except (subprocess.TimeoutExpired,ValueError) as exc:
                diagnostic(LOGS/'watchdog-heartbeat.json',{'heartbeat':time.time(),'state':'timeout' if isinstance(exc,subprocess.TimeoutExpired) else 'invalid_report','exit_code':124})
                continue
            # Only a failed AstrBot API is locally repairable. An offline QQ or an
            # unavailable external model is diagnosed, not an excuse to restart loops.
            unavailable=any(r['name']=='AstrBot' and not r['ok'] and r.get('repairable',True) for r in report['checks'])
            bad=bad+1 if unavailable else 0
            if bad>=2 and time.time()-restarts[-1]>=300:
                (LOGS/'restart-request').touch()
    finally:
        if child is not None and child.poll() is None:
            try:stop_child(child)
            except UnsafeShutdown as exc:
                diagnostic(LOGS/'stop-failure.json',{'at':time.time(),'reason':str(exc),'stopped':False})
        (LOGS/'supervisor.json').unlink(missing_ok=True)
        lock.release()


def drain():
    """Quiesce admissions and require durable proof; never stops a process."""
    try:reply=api('/api/plug/astrbot_plugin_proactive_agent/panel_drain',{},timeout=40)
    except Exception as exc:raise UnsafeShutdown('排空与落盘未确认，未停止进程：'+type(exc).__name__) from exc
    data=reply.get('data',{})
    if reply.get('status')!='ok' or data.get('drained') is not True or data.get('durable') is not True or data.get('pending_persistence') or data.get('pending_receipts'):
        raise UnsafeShutdown('排空或持久化尚未完成；检查待写记录和平台回执后重试')
    return data


def stop_child(child):
    import psutil
    try:
        if not hasattr(child,'_qqbot_identity') or not owned(child._qqbot_identity,ROOT):return
        drain()
        parent=psutil.Process(child.pid)
        children=parent.children(recursive=True)
        child.terminate()
        _,alive=psutil.wait_procs([parent]+children,timeout=10)
        for proc in alive:
            try: proc.kill()
            except psutil.NoSuchProcess: pass
        (LOGS/'astrbot-process.json').unlink(missing_ok=True)
    except psutil.NoSuchProcess: pass


def panel_job(mode, job_id):
    # Runs in an independent worker; survives a supervised AstrBot restart.
    try:
        if mode=='Repair':
            if not running(): start()
            report=check(True)
            if any(r['name']=='AstrBot' and not r['ok'] and r.get('repairable',True) for r in report['checks']):
                (LOGS/'restart-request').touch(); time.sleep(35); report=check(True)
        else: report=check(mode=='Deep')
        result={'id':job_id,'mode':mode,'status':'completed','exit_code':0 if report['ok'] else 1}
    except Exception as exc:
        result={'id':job_id,'mode':mode,'status':'failed','message':type(exc).__name__}
    write_json(LOGS/'panel-health-job.json',result)
    return result

def stop():
    from filelock import FileLock
    LOGS.mkdir(exist_ok=True)
    with FileLock(str(LOGS/'lifecycle.lock'),timeout=35):
        started=time.time()
        if not supervisor_process():
            try:record=json.loads((LOGS/'astrbot-process.json').read_text())
            except (OSError,ValueError):record=None
            process=owned(record,ROOT) if record else None
            if process:
                from types import SimpleNamespace
                stop_child(SimpleNamespace(pid=process.pid,_qqbot_identity=record,terminate=process.terminate))
                return
        (LOGS/'stopped').touch()
        end=time.monotonic()+60
        while running() and time.monotonic()<end:
            try:failure=json.loads((LOGS/'stop-failure.json').read_text())
            except (OSError,ValueError):failure={}
            if failure.get('at',0)>=started:raise UnsafeShutdown(failure.get('reason','停止未完成'))
            time.sleep(.1)
        if running():raise RuntimeError('Graceful stop is still in progress; no unrelated process was killed')


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
    parser.add_argument('action',choices=['start','stop','drain','status','check','deep','repair','supervise','panel-job'])
    parser.add_argument('--mode',choices=['Check','Deep','Repair'],default='Check')
    parser.add_argument('--job-id',default='manual')
    args=parser.parse_args()
    if args.action=='start': start(); print('Supervisor started; dashboard http://127.0.0.1:6185')
    elif args.action=='stop': stop(); print('Owned supervisor stopped')
    elif args.action=='drain': print(json.dumps(drain(),ensure_ascii=False))
    elif args.action=='supervise': supervise()
    elif args.action=='status': print('running' if running() else 'stopped')
    elif args.action=='panel-job': panel_job(args.mode,args.job_id)
    elif args.action=='repair':
        try:
            result=panel_job('Repair','manual');print('See logs/health-status.json')
        finally: open_consoles()
        sys.exit(result.get('exit_code',1))
    else:
        report=check(args.action=='deep'); print(json.dumps(report,ensure_ascii=True,indent=2)); sys.exit(0 if report['ok'] else 1)
