"""Reconcile persisted share reservations against the platform delivery outbox."""
def reconcile(reservation,facts):
    token=reservation.get('attempt_id')
    matches=[r for r in facts if token and r.get('external_operation',{}).get('token')==token
             and r.get('origin')==reservation.get('origin')]
    # A segmented caption receipt alone does not prove the video URL was sent.
    # Partial confirmation is uncertain and must never permit an automatic retry.
    url=reservation.get('url','')
    if url and any(r.get('state')=='confirmed' and url in r.get('text','') for r in matches):
        return {'state':'confirmed','recovery':'confirmed_outbox','delivery_ids':[r['id'] for r in matches if r.get('state')=='confirmed']}
    if any(r.get('state')=='confirmed' for r in matches):
        return {'state':'unknown','recovery':'partial_confirmation_without_link'}
    if any(r.get('submission_state') in {'submitted','intent_durable'} and r.get('state')!='failed'
           or r.get('state')=='unknown' for r in matches):return {'state':'unknown','recovery':'submission_not_confirmed'}
    if matches and all(r.get('state')=='failed' for r in matches):return {'state':'failed','recovery':'explicit_failure'}
    if not matches and token and reservation.get('ledger_expected'):
        return {'state':'failed','recovery':'no_submission_intent'}
    return {'state':'unknown','recovery':'legacy_or_unresolved_reservation'}
