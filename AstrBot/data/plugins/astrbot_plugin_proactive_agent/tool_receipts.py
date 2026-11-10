"""Structured tool outcomes, separate from ordinary chat wording."""
import time,traceback
STATES={'not_started','processing','success','failed','unknown'}
def record(event,kind,status,reason='',**facts):
    if status not in STATES:raise ValueError('Unknown tool outcome')
    row={'tool':kind,'state':status,'reason':reason,'at':time.time(),**facts}
    state=event.get_extra('qqbot_character') or {}
    state['tool_facts']=[r for r in state.get('tool_facts',[]) if r.get('tool')!=kind]+[row]
    event.set_extra('qqbot_character',state);return row

def failure_details(exc):
    from .review_history import sanitize
    return {'error':type(exc).__name__,'details':sanitize(str(exc))[:4000],
            'trace':sanitize(''.join(traceback.format_exception(exc)))[-8000:]}

def image_text(reason,status='failed',animated=False):
    labels={
      'image_quality_rejected':'这次拍出来不对，先不给你发错图',
      'image_request_too_long':'这次画面要求太多了，拆成两张吧',
      'image_prompt_too_long':'这次画面要求太多了，拆成两张吧',
      'image_reference_missing':'这张参考图读不到了，再发一下原图',
      'image_reference_expired':'这张参考图过期了，再发一下原图',
      'gif_not_enabled':'这次没做成会动的，先不给你拿静态图充数',
      'content_rejected':'这次图片没通过检查，没能给你发出来',
      'authorization_failed':'这会儿拍不了，连接出了点问题',
      'rate_limited':'这会儿太忙了，这次没拿到图',
      'render_budget_exhausted':'这次等太久，画面还没检查好',
      'verification_unavailable':'图有了，还没检查好，先不发错图',
      'download_unavailable':'图有了，但这会儿取不回来',
      'preparation_unavailable':'刚才没连上，这次还没拍出来',
      'request_rejected':'这次没拍成，换个画面描述再试试'}
    if status=='unknown':return '还没拿到结果，暂时不确定拍好了没有'
    if str(reason).startswith('identity_reference'):return '本龙的参考图暂时读不到，这次没拍成'
    return labels.get(reason,'这次没拍成会动的' if animated else '这次没拍成')
