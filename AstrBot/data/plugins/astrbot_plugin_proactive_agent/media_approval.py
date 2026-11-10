"""Bind required attachment identities through serialization, without rereading files."""
import hashlib,json
from urllib.parse import unquote

KINDS={'image','record','video','file'}
def identity(kind,source):
    source=str(source or '')
    if source.startswith('file://'):source=unquote(source[7:]).lstrip('/') if len(source)>9 and source[9]==':' else unquote(source[7:])
    if not source.startswith(('http://','https://','base64://')):source=source.replace('\\','/').casefold()
    return {'kind':kind,'identity':hashlib.sha256(source.encode()).hexdigest()}
def component(part):
    kind=type(part).__name__.lower()
    if kind not in KINDS:return None
    return identity(kind,getattr(part,'file',None) or getattr(part,'url',None) or getattr(part,'path',None))
def components(chain):return [r for p in (getattr(chain,'chain',None) or []) if (r:=component(p))]
def wire_key(segment):
    data=segment.get('data',{});return hashlib.sha256(json.dumps([segment.get('type'),data.get('file'),data.get('url')],ensure_ascii=False).encode()).hexdigest()
def converted(part,segment):
    """Use an existing serializer result, no download, model, or file IO."""
    from .conversation_review import _request_scope
    scope=_request_scope.get();row=component(part)
    if scope is not None and row and isinstance(segment,dict):scope.setdefault('media_wire_map',{})[wire_key(segment)]=row
def wire(message,scope):
    result=[];mapping=scope.get('media_wire_map',{})
    for s in message if isinstance(message,list) else []:
        if not isinstance(s,dict) or s.get('type') not in KINDS:continue
        d=s.get('data',{});result.append(mapping.get(wire_key(s)) or identity(s['type'],d.get('file') or d.get('url')))
    return result
def changed(required,actual):
    # A transport may split already approved attachments into several sends.
    # A replacement/new required attachment is not covered by that approval.
    return any(r not in required for r in actual)
