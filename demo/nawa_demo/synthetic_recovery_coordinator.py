"""Trusted-callback ordering experiment; not a distributed recovery service."""


def prepare_recovery(stores, *, quiesce, drain, restore, verify):
    """Run a synthetic recovery sequence and leave session stores paused.

    Callbacks are trusted test fixtures, not verified infrastructure signals.
    A failure does not automatically resume traffic or any paused store.
    The caller must handle uncertain callback side effects and coordinate all
    application processes; this function cannot enforce either requirement.
    """
    if (not isinstance(stores, (list, tuple)) or not 1 <= len(stores) <= 32
            or len({id(store) for store in stores}) != len(stores)
            or any(not callable(getattr(store, 'begin_recovery', None)) for store in stores)
            or any(not callable(callback) for callback in (quiesce, drain, restore, verify))):
        raise ValueError('Invalid synthetic recovery inputs')
    completed_pauses = 0
    stage = 'quiesce'
    try:
        if quiesce() is not True:
            raise ValueError('Synthetic quiesce not confirmed')
        stage = 'drain'
        if drain() is not True:
            raise ValueError('Synthetic drain not confirmed')
        stage = 'pause_sessions'
        for store in stores:
            store.begin_recovery()
            completed_pauses += 1
        stage = 'restore'
        if restore() is not True:
            raise ValueError('Synthetic restore not confirmed')
        stage = 'verify'
        if verify() is not True:
            raise ValueError('Synthetic verification not confirmed')
        return {'status':'ready_for_manual_resume', 'completed_pause_calls':completed_pauses}
    except Exception:
        return {'status':'blocked', 'stage':stage, 'completed_pause_calls':completed_pauses}
