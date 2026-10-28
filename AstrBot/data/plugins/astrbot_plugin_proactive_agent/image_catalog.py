"""Local bridge to the gallery's first caption, keyed by actual file content."""
import json
import re


class GalleryFacts:
    def __init__(self,path):self.path=path;self.signature=None;self.rows={}

    def refresh(self):
        try:
            stat=self.path.stat();signature=(stat.st_mtime_ns,stat.st_size)
            if signature==self.signature:return
            data=json.loads(self.path.read_text('utf-8'));rows=data.get('images',{})
            rows=rows.values() if isinstance(rows,dict) else rows
            current={}
            for row in rows:
                if not isinstance(row,dict) or not re.fullmatch('[0-9a-f]{64}',str(row.get('sha256',''))):continue
                digest=row['sha256']
                if digest not in current or row.get('caption_status')=='done':current[digest]=row
            self.rows=current
            self.signature=signature
        except (OSError,ValueError,TypeError):return

    def semantic(self,digest):
        row=self.rows.get(digest,{})
        if row.get('caption_status')!='done':return None
        structured=row.get('caption_semantic') or {}
        if structured.get('content_hash')==digest and isinstance(structured.get('description'),str):
            return {**structured,'source':'gallery_first_caption'}
        tags=[s[:80] for s in row.get('auto_tags',[])[:32] if isinstance(s,str) and s.strip()]
        if not tags:return None
        # Old tag arrays mix scene, intent and OCR. Do not falsely turn them into
        # quoted image text. They still avoid an unnecessary second vision call.
        return {'description':'已有图库标签（画面、情绪和适用语境的混合参考，不是逐字OCR）：'+'、'.join(tags),
                'ocr':[],'emotion':[],'inferred_usage':[],'text_complete':False,'source':'gallery_legacy_tags'}
