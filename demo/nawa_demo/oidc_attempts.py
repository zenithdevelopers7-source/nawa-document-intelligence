"""In-memory development-only OIDC login-attempt state; not yet integrated.

This is not a distributed state store. The caller must send the returned state
in a protected browser cookie and must not log the verifier, nonce or state.
"""
from __future__ import annotations

import base64
import hashlib
import math
import secrets
import threading
import time


class InvalidLoginAttempt(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class LoginAttempts:
    def __init__(self, *, ttl=300, limit=128, clock=time.monotonic):
        if (type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0
                or type(limit) is not int or limit <= 0 or not callable(clock)):
            raise ValueError('Invalid login-attempt limits')
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self._attempts = {}
        self._lock = threading.Lock()

    def create(self):
        with self._lock:
            now = self.clock()
            self._attempts = {key: value for key, value in self._attempts.items() if value['expires'] > now}
            if len(self._attempts) >= self.limit:
                raise InvalidLoginAttempt('Login is temporarily unavailable')
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
            self._attempts[_digest(state)] = {'nonce': nonce, 'verifier': verifier, 'expires': now + self.ttl}
            return {'state': state, 'nonce': nonce, 'challenge': challenge}

    def consume(self, state, cookie):
        if not isinstance(state, str) or not isinstance(cookie, str) or not state or not cookie:
            raise InvalidLoginAttempt('Invalid or expired login attempt')
        try:
            matches = secrets.compare_digest(state, cookie)
        except TypeError:
            matches = False
        if not matches:
            raise InvalidLoginAttempt('Invalid or expired login attempt')
        with self._lock:
            value = self._attempts.pop(_digest(state), None)
            if not value or value['expires'] <= self.clock():
                raise InvalidLoginAttempt('Invalid or expired login attempt')
            return {'nonce': value['nonce'], 'verifier': value['verifier']}
