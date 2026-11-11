"""Cheap visual-reference recall; selection never admits an extra reply."""
import re

REFERENCE=re.compile(r'这(?:张|个|种|些|东西|玩意|里面|上面|下面|边|是|啥)|那(?:张|个|种|些|东西|玩意)|刚才.{0,8}(?:发|图|说)|上(?:一张|面的)|图里|图中|截图|图片|表情包|它|这能|这样')
ELLIPSIS=re.compile(r'有没有搞头|有搞头吗|给你装|给你吃|给你尝|请你吃|敢吃吗|吃得下|要不要来|来一口|尝一口|好吃吗|能吃吗|装上.{0,12}(?:行|好|用|搞头)|好不好用|能用吗|行不行|怎么样|你怎么看|你觉得呢|什么意思|啥意思|咋回事|怎么回事|为什么会这样|为啥会这样|好看吗|正常吗|对不对|这对吗')

def may_refer(text):
    """Use the existing primary vision request, never a classifier round-trip."""
    return bool(REFERENCE.search(text) or len(text)<=100 and ELLIPSIS.search(text))

def choose(records,*,now,cutoff=0,limit=3):
    valid=[r for r in records if cutoff<r.get('time',0)<=now and now-r['time']<3600]
    valid.sort(key=lambda r:r['time'],reverse=True)
    if not valid:return []
    # Keep one latest image message / adjacent image burst, not unrelated old albums.
    latest=valid[0]['time']
    return list(reversed([r for r in valid if latest-r['time']<=15][:limit]))
