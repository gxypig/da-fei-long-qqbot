"""Controlled admission/I/O benchmark. No live model or QQ delivery claims."""
import asyncio,json,statistics,sys,time,tempfile
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'tests'),str(ROOT)]
from test_maintenance import Main
from test_work_scheduler import provider
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import ModelScheduler,BackgroundPaused
from data.plugins.astrbot_plugin_proactive_agent.reading_runtime import ReadingRuntime
from data.plugins.astrbot_plugin_proactive_agent.record_store import AsyncRecordStore
from data.plugins.astrbot_plugin_proactive_agent.safe_reader import parse_raw

def stats(rows):
    good=sorted(r['all_confirmed_ms'] for r in rows if r['ok'])
    return {'n':len(rows),'valid':len(good),'median_ms':statistics.median(good),'p95_ms':good[max(0,(len(good)*95+99)//100-1)],
        'failure_rate':1-len(good)/len(rows),'duplicate_rate':sum(r['sends']!=1 for r in rows)/len(rows),
        'max_yield_ms':max((r.get('yield_ms',0) for r in rows),default=0)}

async def sample(mode,case,index,store):
    events=[];scheduler=ModelScheduler({'model_concurrency':2},lambda *a:events.append(a),idle_seconds=0)
    service=SimpleNamespace(plugin=SimpleNamespace(_work=scheduler),store=store,db=SimpleNamespace(path=store.path),clock=time.time,closed=False,generation=1)
    def check(*_):
        if scheduler.busy():raise BackgroundPaused()
    service.check=check;runtime=ReadingRuntime(service);background=None;started=asyncio.Event();cancelled=asyncio.Event();signals=[]
    async def background_model():
        started.set()
        try:await asyncio.sleep(5)
        except asyncio.CancelledError:signals.append(time.perf_counter());cancelled.set();await asyncio.sleep(.005);raise
    async def page():
        started.set();await asyncio.sleep(.65)
        return await asyncio.to_thread(parse_raw,{'url':'https://example.org/article','text':'<article>'+('绘画颜色关系、留白、构图。'*3000)+'</article>','ctype':'text/html','bytes':90000})
    if mode=='on' and case=='background_model':background=asyncio.create_task(scheduler.run(provider(),background_model,kind='background'))
    elif mode=='on' and case=='slow_page':background=asyncio.create_task(runtime.read(['probe',index],page))
    if background:await started.wait()
    async def reply(n):
        begin=time.perf_counter();send_count=0
        with scheduler.foreground():
            await asyncio.sleep(.002) # same input preparation on both sides
            async def generate():await asyncio.sleep(.25);return '相同内容与设置'
            result=await scheduler.run(provider(),generate,kind='interactive')
            # Same bounded durable write and acknowledged transport delay.
            key=f'outbox:{mode}:{index}:{n}';store.put(key,{'text':result,'state':'submitting'});await store.flush(key)
            await asyncio.sleep(.012);send_count+=1;store.put(key,{'state':'confirmed'})
            elapsed=(time.perf_counter()-begin)*1000
        return {'ok':True,'all_confirmed_ms':elapsed,'first_confirmed_ms':elapsed,'sends':send_count,'scenario':case,'mode':mode,
            'yield_ms':max(0,(signals[0]-begin)*1000) if signals else 0}
    try:
        rows=await asyncio.gather(*(reply(i) for i in range(3 if case=='burst' else 1)))
        if background:
            result=await asyncio.gather(background,return_exceptions=True)
            if case=='slow_page':assert isinstance(result[0],dict),'Read-only request cancelled instead of saved'
            else:assert cancelled.is_set(),'Background was never preempted'
        return rows
    finally:await runtime.close();await scheduler.close()

async def main():
    (ROOT/'logs/round22').mkdir(parents=True,exist_ok=True)
    report={'kind':'controlled_scheduler_io_with_simulated_model_and_transport','rounds':[],'production_idle_seconds':10,'controlled_idle_seconds':0}
    with tempfile.TemporaryDirectory(prefix='round22-controlled-') as d:
        store=AsyncRecordStore(Path(d)/'store.sqlite3');await store.start()
        try:
            for round_ in range(3):
                rows={'off':[],'on':[]}
                for i in range(20):
                    case=('ordinary','burst','slow_page','background_model')[i%4]
                    for mode in (('off','on') if (round_+i)%2==0 else ('on','off')):
                        rows[mode].extend(await sample(mode,case,round_*100+i,store))
                summary={k:stats(v) for k,v in rows.items()};delta={k:(summary['on'][k]/summary['off'][k]-1)*100 for k in ('median_ms','p95_ms')}
                report['rounds'].append({'round':round_+1,'summary':summary,'degradation_percent':delta,'samples':rows})
                (ROOT/'logs/round22/controlled-performance.json').write_bytes(json.dumps(report,ensure_ascii=False,indent=2).encode())
                print(json.dumps({'round':round_+1,'summary':summary,'degradation_percent':delta}),flush=True)
        finally:await store.close()
    report['passed']=all(max(r['degradation_percent'].values())<=5 and r['summary']['on']['max_yield_ms']<=200 for r in report['rounds'])
    (ROOT/'logs/round22/controlled-performance.json').write_bytes(json.dumps(report,ensure_ascii=False,indent=2).encode())
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':asyncio.run(main())
