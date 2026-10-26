"""Portable ownership, atomic diagnostics and rotating service logs."""
import json,os,sys,uuid,logging,time
from pathlib import Path
from logging.handlers import RotatingFileHandler
ROOT=Path(__file__).resolve().parents[1]

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        for attempt in range(6):
            try:os.replace(temp,path);break
            except OSError as exc:
                if getattr(exc,'winerror',None) not in {5,32,33} or attempt==5:raise
                time.sleep(.01*2**attempt)
    finally:temp.unlink(missing_ok=True)


def diagnostic(path,value):
    try:atomic_json(path,value);return True
    except OSError as exc:
        print('Diagnostic write failed: '+type(exc).__name__,file=sys.stderr);return False


def identity(process,root=ROOT):
    return {'pid':process.pid,'created':process.create_time(),'exe':process.exe(),'cwd':process.cwd(),'root':str(root.resolve())}


def owned(record,root=ROOT):
    import psutil
    try:
        if Path(record['root']).resolve()!=root.resolve():return None
        process=psutil.Process(record['pid'])
        if abs(process.create_time()-record['created'])>.01:return None
        if os.path.normcase(process.exe())!=os.path.normcase(record['exe']):return None
        if not Path(process.cwd()).resolve().is_relative_to(root.resolve()):return None
        return process
    except (psutil.Error,KeyError,TypeError,OSError):return None


def _pipe(stream,path):
    handler=RotatingFileHandler(path,maxBytes=4*1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        for line in iter(stream.readline,b''):
            handler.emit(logging.LogRecord('service',logging.INFO,'',0,line.decode('utf-8',errors='replace').rstrip('\r\n'),(),None))
    finally:handler.close();stream.close()
