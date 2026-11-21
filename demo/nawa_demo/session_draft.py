"""Development-only session primitives; not connected to dashboard endpoints.

Sessions are process-local and disappear on restart. Not suitable for a
multi-worker or production deployment. The membership lookup must obtain its
answer from trusted server-side records, never from browser-supplied roles.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from .authorization import Principal, PERMISSIONS, authorized

# Future actions require CSRF by default. New read-only actions must be
# explicitly reviewed and listed here before they can omit a CSRF token.
READ_ACTIONS = frozenset({'list', 'view', 'compare', 'review_view'})


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class SessionError(ValueError):
    pass


class SessionStore:
    def __init__(self, membership_lookup, *, ttl=300, limit=256, clock=time.monotonic):
        if (not callable(membership_lookup) or type(ttl) not in (int, float)
                or not math.isfinite(ttl) or ttl <= 0 or ttl > 300
                or type(limit) is not int or limit <= 0 or not callable(clock)):
            raise ValueError('Invalid session configuration')
        self.lookup, self.ttl, self.limit, self.clock = membership_lookup, ttl, limit, clock
        self._sessions = {}
        self._lock = threading.Lock()
        self._recovering = False
        self._recovery_epoch = 0

    def begin_recovery(self):
        """Block issuance and discard sessions in this process only.

        The caller must first quiesce and drain protected operations. This is
        not exposed as an HTTP endpoint and is not a distributed barrier.
        """
        with self._lock:
            if self._recovering:
                raise SessionError('Recovery already active')
            self._recovering = True
            self._recovery_epoch += 1
            self._sessions.clear()

    def end_recovery(self):
        """Allow fresh local sessions after externally coordinated recovery."""
        with self._lock:
            if not self._recovering:
                raise SessionError('Recovery is not active')
            self._recovering = False

    def _membership(self, subject, tenant):
        principal = self.lookup(subject, tenant)
        if (not isinstance(principal, Principal) or principal.active is not True
                or principal.subject != subject or principal.tenant != tenant
                or not isinstance(principal.role, str) or principal.role not in PERMISSIONS
                or any(type(generation) is not int or generation < 0 for generation in
                       (principal.membership_generation, principal.identity_generation, principal.tenant_generation))):
            raise SessionError('Session unavailable')
        return principal

    def issue(self, subject, tenant, *, max_age=None, deadline=None):
        if not all(isinstance(x, str) and x.strip() for x in (subject, tenant)):
            raise SessionError('Session unavailable')
        if max_age is not None and (type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age < 1):
            raise SessionError('Session unavailable')
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise SessionError('Session unavailable')
        with self._lock:
            if self._recovering:
                raise SessionError('Session unavailable')
            recovery_epoch = self._recovery_epoch
        principal = self._membership(subject, tenant)
        with self._lock:
            if self._recovering or recovery_epoch != self._recovery_epoch:
                raise SessionError('Session unavailable')
            now = self.clock()
            duration = int(min(self.ttl, max_age if max_age is not None else self.ttl,
                               deadline - now if deadline is not None else self.ttl))
            if duration < 1:
                raise SessionError('Session unavailable')
            self._sessions = {key: value for key, value in self._sessions.items() if value['expires'] > now}
            if len(self._sessions) >= self.limit:
                raise SessionError('Session unavailable')
            session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[_hash(session)] = {'subject': subject, 'tenant': tenant,
                                               'csrf_hash': _hash(csrf), 'expires': now + duration,
                                               'generations': (principal.membership_generation,
                                                               principal.identity_generation, principal.tenant_generation)}
            return {'session': session, 'csrf': csrf, 'max_age': duration}

    def authorize(self, session, action, *, tenant, csrf=None, require_csrf=False):
        if not isinstance(session, str) or not session or not isinstance(action, str) or type(require_csrf) is not bool:
            raise SessionError('Session unavailable')
        with self._lock:
            if self._recovering:
                raise SessionError('Session unavailable')
            key = _hash(session)
            record = self._sessions.get(key)
            if not record or record['expires'] <= self.clock():
                self._sessions.pop(key, None)
                raise SessionError('Session unavailable')
            if require_csrf or action not in READ_ACTIONS:
                if not isinstance(csrf, str) or not csrf:
                    raise SessionError('Request denied')
                if not secrets.compare_digest(record['csrf_hash'], _hash(csrf)):
                    raise SessionError('Request denied')
            try:
                principal = self._membership(record['subject'], record['tenant'])
            except SessionError:
                # Once a session observes an invalid membership, do not let a
                # later reactivation revive the same unexpired session.
                self._sessions.pop(key, None)
                raise
            if (principal.membership_generation, principal.identity_generation, principal.tenant_generation) != record['generations']:
                self._sessions.pop(key, None)
                raise SessionError('Session unavailable')
            # The membership lookup may take long enough for the session to
            # expire after the first check but before authorization returns.
            if record['expires'] <= self.clock():
                self._sessions.pop(key, None)
                raise SessionError('Session unavailable')
            if not authorized(principal, action, tenant=tenant):
                raise SessionError('Request denied')
            return principal

    def revoke(self, session):
        if not isinstance(session, str) or not session:
            return
        with self._lock:
            self._sessions.pop(_hash(session), None)

    def logout(self, session, csrf):
        if not isinstance(session, str) or not session or not isinstance(csrf, str) or not csrf:
            raise SessionError('Request denied')
        with self._lock:
            key = _hash(session)
            record = self._sessions.get(key)
            if (not record or record['expires'] <= self.clock()
                    or not secrets.compare_digest(record['csrf_hash'], _hash(csrf))):
                raise SessionError('Request denied')
            self._sessions.pop(key, None)

    def authorize_current(self, session, action, *, csrf=None, require_csrf=False):
        """Use the tenant bound to the server-side session, not client input."""
        if not isinstance(session, str) or not session:
            raise SessionError('Session unavailable')
        with self._lock:
            record = self._sessions.get(_hash(session))
            if not record:
                raise SessionError('Session unavailable')
            tenant = record['tenant']
        return self.authorize(session, action, tenant=tenant, csrf=csrf, require_csrf=require_csrf)

