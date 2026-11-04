"""Local bridge to the gallery's first caption, keyed by actual file content."""
import json
import hashlib
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
        source_revision=hashlib.sha256(json.dumps({'semantic':structured,'tags':row.get('auto_tags',[])},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if structured.get('content_hash')==digest and isinstance(structured.get('description'),str):
            return {**structured,'source':'gallery_first_caption','source_revision':source_revision}
        # Tags can guide retrieval, but are not recognized image facts.
        return None
