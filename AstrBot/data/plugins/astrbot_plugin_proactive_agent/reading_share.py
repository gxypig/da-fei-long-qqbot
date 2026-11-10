"""Grounding checks always run, even when optional style review is disabled."""
import re
from .reading_runtime import reading_evidence

class ReadingContentError(ValueError):pass

def material_for(plugin,candidate):
    basis=candidate.get('basis',{})
    if basis.get('operation')!='extended_reading':return None
    service=getattr(plugin,'_memory_life',None)
    return service.store.rows.get(basis.get('extended_material'),{}) if service else {}

def evidence_error(material):
    if material is None:return ''
    if not reading_evidence(material)['body_verified']:return '只有标题、摘要或简介，没有实际正文依据'
    quote=material.get('basis','');thought=material.get('thought','')
    if not isinstance(quote,str) or len(quote.strip())<4 or quote not in material.get('content',''):return '缺少可定位的正文细节'
    if not isinstance(thought,str) or len(thought.strip())<4 or re.fullmatch(r'[很真挺还太]*有意思[啊呀呢！!。 ]*|值得思考[啊呢。 ]*|各有优劣[。 ]*',thought):return '缺少具体个人看法'
    return ''

def text_error(material,text):
    error=evidence_error(material)
    if error or material is None:return error
    body=re.sub(r'https?://\S+','',text).strip()
    if len(re.sub(r'\W','',body))<16:return '去掉链接后内容不足，不能发布裸链接'
    basis=material['basis'];thought=material['thought']
    def units(value):
        chinese=re.findall(r'[\u3400-\u9fff]+',value)
        return {r[i:i+3] for r in chinese for i in range(len(r)-2)}|set(re.findall(r'[a-zA-Z]{4,}',value.lower()))
    if not units(basis).intersection(units(body)):return '动态没有对应正文的具体细节'
    if not units(thought).intersection(units(body)):return '动态没有保留有据的个人看法'
    return ''

def ensure_text(material,text):
    error=text_error(material,text)
    if error:raise ReadingContentError(error)
