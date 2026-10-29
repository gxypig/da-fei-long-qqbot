"""Local inverted retrieval. Similarity supplies evidence, never permission to joke."""
import copy,math,re,unicodedata
from collections import defaultdict

def fold(value):return unicodedata.normalize('NFKC',str(value or '')).casefold()

def contains(text,phrase):
    text,phrase=fold(text),fold(phrase)
    if not phrase:return False
    if re.search(r'[a-z0-9_]',phrase) and phrase.isascii():
        return bool(re.search(r'(?<![a-z0-9_])'+re.escape(phrase)+r'(?![a-z0-9_])',text))
    return phrase in text

_STOP={'什么','怎么','这个','那个','就是','一个','没有','可以','自己','时候','表示','网友','使用','形容','用来','意思','感觉','不是','还是','一下','然后','因为','所以','现在','已经','今天','真的','一种','别人','这些','大家','的话','的是','的人'}
_CONCEPTS={
    '花钱':('工资','奶茶','消费','花钱','买买买','月光','钱包','剁手','账单','付款'),
    '工作疲惫':('加班','打工','上班','老板','打卡','下班','996','社畜'),
    '游戏失误':('坑队友','团灭','送人头','掉分','零杠','操作失误','打输了'),
    '反复失败':('又失败','怎么又','再次失败','白忙','重来','又输了','努力白费'),
    '拖延':('明天再','下次一定','拖延','鸽了','下回再','改天','迟迟没做'),
    '震惊':('离谱','震惊','惊讶','没想到','竟然','居然','不可思议'),
    '讽刺敷衍':('敷衍','反讽','懒得争','不想争','随你说','反话'),
    '等待':('等了','等半天','加载','排队','还没来','迟到','转圈'),
    '自嘲':('小丑','自嘲','丢脸','尴尬','社死','出丑'),
    '起床过晚':('睡过头','起晚了','晒屁股','睡懒觉','错过闹钟'),
}

def tokens(text):
    text=fold(text);result=set(re.findall(r'[a-z0-9_]{2,}',text))
    for chunk in re.findall(r'[\u4e00-\u9fff]+',text):
        result.update(chunk[i:i+2] for i in range(len(chunk)-1) if chunk[i:i+2] not in _STOP)
    for concept,cues in _CONCEPTS.items():
        if any(contains(text,cue) for cue in cues):result.add('@'+concept)
    return result

SERIOUS=re.compile(r'救命|急救|去世|自杀|自残|验证码|密码|(?:别|不要|停止).{0,3}(?:开玩笑|玩梗|损我)|认真(?:点|回答|说)|受伤|悲伤|很难过')

def strings(value):
    if isinstance(value,str):return [value]
    if isinstance(value,(list,tuple)):return [str(x) for x in value if isinstance(x,str)]
    return []

class MemeIndex:
    def __init__(self,rows):
        self.rows={};self.aliases={};self.postings=defaultdict(set);self.features={}
        for row in rows:
            name=fold(row.get('phrase'))
            if not name:continue
            self.rows[name]=copy.deepcopy(row)
            aliases=list(dict.fromkeys([row['phrase']]+strings(row.get('aliases'))))
            self.aliases[name]=aliases
            fields={k:tokens(' '.join(strings(row.get(k)))) for k in ('meaning','usage','intent','tags','triggers','adaptations')}
            fields['title']=tokens(' '.join(aliases));self.features[name]=fields
            for word in set().union(*fields.values()):self.postings[word].add(name)
        self.idf={word:1+math.log(1+len(self.rows)/(1+len(ids))) for word,ids in self.postings.items()}

    def search(self,text,recent=(),*,limit=3,direct_only=False,exclude=()):
        if not direct_only and SERIOUS.search(str(text)):return []
        query=tokens(str(text)[:1000]);context=tokens(' '.join(str(s) for s in recent[-2:])[:1200]);excluded=set(exclude)
        candidates=set().union(*(self.postings.get(t,set()) for t in query)) if query else set()
        # Single-character titles need a complete query, not substring matches.
        exact=fold(text).strip();candidates.update(k for k in self.rows if k==exact)
        ranked=[]
        for name in candidates-excluded:
            row=self.rows[name];aliases=self.aliases[name];fields=self.features[name]
            direct=any(contains(text,alias) and (len(fold(alias))>=2 or exact==fold(alias)) for alias in aliases)
            if direct_only and not direct:continue
            triggers=[t for t in strings(row.get('triggers')) if len(fold(t))>=2 and contains(text,t)]
            allwords=set().union(*fields.values());overlap=query&allwords;literal={t for t in overlap if not t.startswith('@')};concepts={t for t in overlap if t.startswith('@')}
            concrete_scene='@起床过晚' in concepts
            # A category synonym alone does not make an unrelated joke relevant.
            if not direct and not triggers and not (len(literal)>=2 or concepts and literal or concrete_scene):continue
            scene=query&(fields['usage']|fields['intent']|fields['triggers']|fields['tags'])
            if not direct and not triggers and not scene:continue
            score=sum(self.idf.get(t,0)*(2 if t in scene else 1) for t in overlap)+.2*len(context&allwords)
            if not direct and not triggers and not concrete_scene and (score<3 or len(literal)/max(1,len(query))<.15):continue
            reason='原词或别名直接出现' if direct else '适用情境触发：'+'、'.join(triggers[:3]) if triggers else '语境线索：'+'、'.join(sorted(overlap)[:6])
            out={**copy.deepcopy(row),'retrieval_kind':'direct' if direct else 'context','retrieval_reason':reason,
                'retrieval_score':round(score,3),'applicable':row.get('intent') or row.get('usage') or '',
                'avoid':row.get('avoid') or '语境不明确、严肃求助或对方要求停下时不用'}
            ranked.append(((int(direct),int(bool(triggers)),score),out))
        ranked.sort(key=lambda pair:(pair[0],pair[1]['phrase']),reverse=True)
        return [row for _,row in ranked[:limit]]
