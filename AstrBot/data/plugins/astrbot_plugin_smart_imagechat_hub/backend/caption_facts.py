"""First-caption facts travel with tags; the optional companion can reuse them."""
class CaptionTags(list):
    def __init__(self,tags,raw,digest):
        super().__init__(tags);self.semantic=None
        if not isinstance(raw,dict) or not isinstance(raw.get('description'),str) or not raw['description'].strip():return
        ocr=[]
        for row in raw.get('ocr',[])[:20] if isinstance(raw.get('ocr'),list) else []:
            if isinstance(row,dict) and isinstance(row.get('text'),str) and type(row.get('confidence')) in (int,float) and 0<=row['confidence']<=1:
                ocr.append({'text':row['text'][:300],'confidence':row['confidence']})
        self.semantic={'content_hash':digest,'description':raw['description'][:600],'ocr':ocr,'text_complete':raw.get('text_complete') is True}
        for field in ('emotion','inferred_usage'):
            self.semantic[field]=[x[:100] for x in raw.get(field,[])[:8] if isinstance(x,str)] if isinstance(raw.get(field),list) else []
