"""A failed optional interjection should not become another interruption."""
def quiet_failure(job=None,state=None):
    state=state or {}
    if state.get('proactive') and not state.get('required'):return True
    if not job or not getattr(job,'passive',False) or getattr(job,'manually_adjusted',False):return False
    from .exchange_state import protected
    return not protected(job.text) and ':GroupMessage:' in str(job.origin)
