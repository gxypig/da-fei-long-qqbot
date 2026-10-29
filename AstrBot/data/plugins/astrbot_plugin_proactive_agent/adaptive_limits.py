"""Conservative per-source admission adjustment from necessary request evidence."""
from collections import deque
import math,time

def percentile(values,p=.95):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*p)-1)] if values else None

class AdaptiveLimits:
    def __init__(self,clock=time.monotonic):self.clock=clock;self.sources={}
    def state(self,key,initial=2):
        if key not in self.sources:
            now=self.clock();self.sources[key]={'limit':max(1,min(3,initial)),'since':now,'last':now,'running':0,
                'occupied':0.,'capacity':0.,'calls':[],'windows':deque(maxlen=2),'baseline2':None,'cooldown_until':0.,'reason':'等待足够样本'}
        return self.sources[key]
    def _advance(self,state):
        now=self.clock()
        while state['last']<now:
            end=min(now,state['since']+60);dt=max(0,end-state['last'])
            state['occupied']+=min(state['running'],state['limit'])*dt;state['capacity']+=state['limit']*dt;state['last']=end
            if end>=state['since']+60:
                calls=state['calls'];window={'samples':len(calls),'queue_p95':percentile([c[0] for c in calls]),
                    'execution_p95':percentile([c[1] for c in calls]),'errors':any(c[2] for c in calls),
                    'utilization':state['occupied']/max(.001,state['capacity']),'limit':state['limit']}
                if window['samples']>=20 and state['limit']==2 and state['baseline2'] is None and not window['errors']:
                    state['baseline2']=window['execution_p95']
                state['windows'].append(window);self._evaluate(state,end)
                state.update(since=end,occupied=0.,capacity=0.,calls=[])
    def _evaluate(self,state,now):
        windows=list(state['windows']);base=state['baseline2']
        if len(windows)<2 or any(w['samples']<20 for w in windows) or not base:
            state['reason']='样本不足，保持并发';return
        if all(w['execution_p95']>base*1.2 for w in windows):
            self._down(state,now,'连续两窗执行变慢超过20%');return
        if now<state['cooldown_until']:state['reason']='降档冷却中';return
        if all(w['queue_p95']>10 and w['utilization']>=.9 and not w['errors'] and w['execution_p95']<=base*1.1 for w in windows):
            state['limit']=min(3,state['limit']+1);state['windows'].clear();state['reason']='两窗必要调用拥堵且执行稳定，增加一个名额'
    def _down(self,state,now,reason):
        state['limit']=max(1,state['limit']-1);state['cooldown_until']=now+300;state['reason']=reason;state['windows'].clear()
    def touch(self,key,running,initial=2):
        state=self.state(key,initial);self._advance(state);state['running']=running;return state['limit']
    def finish(self,key,queue,elapsed,*,status=None,timed_out=False,necessary=True,already_down=False):
        state=self.state(key);self._advance(state)
        if necessary:state['calls'].append((max(0,queue),max(0,elapsed),bool(status==429 or timed_out)))
        if status==429 and not already_down:self._down(state,self.clock(),'上游429，立即降低并发')
    def rate_limit(self,key):self._down(self.state(key),self.clock(),'上游429，立即降低并发')
    def limit(self,key,initial=2):return self.state(key,initial)['limit']
    def public(self,key):
        state=self.state(key)
        return {k:state[k] for k in ('limit','baseline2','cooldown_until','reason')}|{'windows':list(state['windows']),'window_samples':len(state['calls'])}
