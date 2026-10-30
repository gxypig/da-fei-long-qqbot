"""One review switch; immutable per-task settings independent of scheduling."""
from contextvars import ContextVar
from contextlib import contextmanager
import copy,hashlib,json

_settings=ContextVar('qqbot_review_settings',default=None)
LABELS={'off':'关闭审阅','speed_first':'预测分流','all':'完整审阅'}

def configured(cfg):
    get=cfg if callable(cfg) else cfg.get
    enabled=bool(get('reply_review_enabled',False))
    requested=get('review_policy','speed_first')
    policy=requested if requested in {'all','speed_first'} else 'all'
    legacy_off=get('reply_prediction_enabled',None) is False
    if enabled and legacy_off:policy='all'
    mode=policy if enabled else 'off'
    result={'enabled':enabled,'policy':policy,'mode':mode,'label':LABELS[mode],
            'prediction':mode=='speed_first','prediction_provider_id':str(get('prediction_provider_id','') or ''),
            'legacy_prediction_disabled':legacy_off,'prediction_timeout':8.,'prediction_grace':.3}
    result['version']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]
    return result

def current(cfg,event=None):
    if event is not None:
        saved=event.get_extra('qqbot_review_settings')
        if saved is None:
            saved=copy.deepcopy(_settings.get() or configured(cfg))
            event.set_extra('qqbot_review_settings',saved)
        return copy.deepcopy(saved)
    return copy.deepcopy(_settings.get() or configured(cfg))

def begin(cfg,event=None,*,fresh=False):
    from .send_checks import begin_task
    return (_settings.set(configured(cfg) if fresh else current(cfg,event)),begin_task())
def end(token):
    from .send_checks import end_task
    _settings.reset(token[0]);end_task(token[1])
def bind(value):_settings.set(copy.deepcopy(value))
def snapshot():return copy.deepcopy(_settings.get())

@contextmanager
def scope(cfg,event=None,*,fresh=False):
    token=begin(cfg,event,fresh=fresh)
    try:yield current(cfg,event)
    finally:end(token)
