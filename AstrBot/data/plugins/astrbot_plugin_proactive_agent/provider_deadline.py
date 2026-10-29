"""A per-operation SDK deadline without mutating the shared chat client."""
import copy

def with_deadline(provider,seconds):
    client=getattr(provider,'client',None)
    from .wire_admission import native
    if not native(provider) or not callable(getattr(client,'with_options',None)):return provider
    isolated=copy.copy(provider)
    # The SDK copy shares the pool, so callers must never close this view.
    # API-key selection and timeout live on the view, not on the chat client.
    isolated.client=client.with_options(timeout=float(seconds))
    return isolated
