"""Bounded source concurrency, with a recoverable one-slot probation period."""
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
                'occupied':0.,'capacity':0.,'calls':[],'windows':deque(maxlen=2),'baseline2':None,
                'cooldown_until':0.,'reason':'观察中，等待必要调用样本','successes':deque(maxlen=256),
                'queued':False,'probation':False,'trial_calls':[],'last_outcome':None}
        return self.sources[key]
    def _advance(self,state):
        now=self.clock()
        while state['last']<now:
            end=min(now,state['since']+60);dt=max(0,end-state['last'])
            state['occupied']+=min(state['running'],state['limit'])*dt
            state['capacity']+=state['limit']*dt;state['last']=end
            if end>=state['since']+60:
                calls=state['calls'];good=[c for c in calls if not c[2]]
                window={'samples':len(good),'queue_p95':percentile([c[0] for c in good]),
                    'execution_p95':percentile([c[1] for c in good]),'errors':any(c[2] for c in calls),
                    'utilization':state['occupied']/max(.001,state['capacity']),'limit':state['limit']}
                if window['samples']>=20 and state['limit']==2 and state['baseline2'] is None and not window['errors'] and not state['probation']:
                    state['baseline2']=window['execution_p95']
                state['windows'].append(window);self._evaluate(state,end)
                state.update(since=end,occupied=0.,capacity=0.,calls=[])
    def _recover(self,state,now):
        while state['successes'] and state['successes'][0]<now-600:state['successes'].popleft()
        if state['limit']!=1:return
        if now<state['cooldown_until']:state['reason']='降档冷却中';return
        if not state['queued']:state['reason']='观察中，暂无必要调用排队';return
        if len(state['successes'])<10:
            state['reason']=f"观察中，最近连续成功{len(state['successes'])}/10";return
        state.update(limit=2,probation=True,trial_calls=[],reason='试运行第二个名额，暂不升至3')
        state['windows'].clear()
    def _evaluate(self,state,now):
        # A long idle gap may advance several old windows at once. Recovery
        # must use today's evidence age, not a backdated window boundary.
        if state['limit']==1:self._recover(state,self.clock());return
        if state['probation']:state['reason']='试运行第二个名额，暂不升至3';return
        windows=list(state['windows']);base=state['baseline2']
        if now<state['cooldown_until']:state['reason']='降档冷却中';return
        if any(w['errors'] for w in windows):state['reason']='窗口内有失败，继续观察';return
        if len(windows)<2 or any(w['samples']<20 for w in windows):
            state['reason']='样本不足，保持并发';return
        if not base:state['reason']='观察中，等待健康双并发基准';return
        if all(w['execution_p95']>base*1.2 for w in windows):
            self._down(state,now,'连续两窗执行变慢超过20%');return
        if all(w['queue_p95']>10 and w['utilization']>=.9 and w['execution_p95']<=base*1.1 for w in windows):
            if state['limit']<3:
                state['limit']+=1;state['windows'].clear();state['reason']='两窗必要调用拥堵且执行稳定，增加一个名额'
            else:state['reason']='已达并发上限3'
        else:state['reason']='保持并发：排队、占用率或执行耗时未满足升档条件'
    def _down(self,state,now,reason):
        state.update(limit=max(1,state['limit']-1),cooldown_until=now+300,reason=reason,probation=False,trial_calls=[])
        state['windows'].clear();state['successes'].clear()
    def touch(self,key,running,initial=2,*,queued=None):
        state=self.state(key,initial)
        if queued is not None:state['queued']=bool(queued)
        self._advance(state);state['running']=running;self._recover(state,self.clock())
        return state['limit']
    def finish(self,key,queue,elapsed,*,status=None,timed_out=False,necessary=True,already_down=False,outcome=None):
        state=self.state(key);self._advance(state)
        failed=bool(timed_out or (status is not None and status>=400) or outcome=='failed')
        cancelled=outcome in {'cancelled','paused','preempted','closed'}
        label=('cancelled' if cancelled else 'timeout' if timed_out else str(status) if status and status>=400
               else 'connection_or_other_error' if failed else 'success')
        state['last_outcome']=label
        if necessary and not cancelled:
            state['calls'].append((max(0,queue),max(0,elapsed),failed))
            if failed:
                state['successes'].clear()
                if state['probation'] and not already_down:self._down(state,self.clock(),'第二名额试运行失败，退回1并冷却')
            else:
                state['successes'].append(self.clock())
                if state['probation']:
                    state['trial_calls'].append(max(0,elapsed))
                    if len(state['trial_calls'])>=10:
                        state['baseline2']=percentile(state['trial_calls']);state['probation']=False
                        state['windows'].clear();state['reason']='第二名额试运行稳定，继续观察两窗后才可升档'
        if status==429 and not already_down:self._down(state,self.clock(),'上游429，立即降低并发')
    def rate_limit(self,key):self._down(self.state(key),self.clock(),'上游429，立即降低并发')
    def limit(self,key,initial=2):return self.state(key,initial)['limit']
    def public(self,key):
        state=self.state(key)
        return {k:state[k] for k in ('limit','baseline2','cooldown_until','reason','probation','last_outcome')}|{
            'windows':list(state['windows']),'window_samples':len(state['calls']),
            'recovery_successes':sum(t>=self.clock()-600 for t in state['successes'])}
