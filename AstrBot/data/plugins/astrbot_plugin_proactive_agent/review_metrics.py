"""Aggregate retained diagnostics on panel demand; no extra per-reply model work."""
from collections import Counter
from datetime import datetime,timedelta
import json
import statistics


def review_metrics(root,hours=24):
    cutoff=(datetime.now()-timedelta(hours=hours)).isoformat(timespec='seconds')
    rows=[]
    for path in root.glob('conversation-audit*.jsonl'):
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:row=json.loads(line)
                    except ValueError:continue
                    if isinstance(row,dict) and row.get('time','')>=cutoff:rows.append(row)
        except OSError:continue
    checked=[r for r in rows if r.get('kind')=='review'];times=sorted(r['elapsed_ms'] for r in checked if isinstance(r.get('elapsed_ms'),(int,float)))
    counts=Counter(r.get('status') for r in checked)
    return {'hours':hours,'retained_from':min((r['time'] for r in rows),default=None),
            'retained_to':max((r['time'] for r in rows),default=None),'reviews':len(checked),
            'passed':counts['passed'],'rewritten':counts['rewritten'],'blocked':counts['blocked'],
            'model_rewritten':sum(r.get('model_rewritten',r.get('rounds',0)>1) for r in checked),
            'legacy_rewrite_estimate':any('model_rewritten' not in r for r in checked),
            'format_only':sum(r.get('format_changed',False) and not r.get('model_rewritten',False) for r in checked),
            'local_passes':sum(r.get('kind')=='review_fast_path' for r in rows),
            'model_calls':sum(r.get('kind')=='review_model' for r in rows),
            'median_ms':statistics.median(times) if times else None,
            'p95_ms':times[min(len(times)-1,int(len(times)*.95))] if times else None,
            'timed':len(times),'tokens':None,
            'note':'仅统计保留日志覆盖的时间；改写不等于必然改善。旧日志按复核轮次估计模型改写，不推算未记录的token；审阅耗时不是每条回复都会增加的耗时。'}
