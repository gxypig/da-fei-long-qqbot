"""A durable recall epoch keeps retained delivery/audit facts out of new chats."""
import json,math
from pathlib import Path
def cutoff(path):
    try:
        record=json.loads(Path(path).read_text('utf-8'));value=record.get('at')
        if record.get('state','complete')!='complete' or type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError('上下文重置尚未完成或标记无效')
        return float(value)
    except FileNotFoundError:return 0
    except (ValueError,TypeError):raise ValueError('上下文重置标记损坏，不能安全恢复旧记忆')
def install(plugin):
    at=cutoff(plugin._data_dir/'context-reset.json');plugin._context_reset_at=at
    for name in ('_delivery','_sent_memory'):
        obj=getattr(plugin,name,None)
        if obj is not None:obj.context_cutoff=at
    return at
