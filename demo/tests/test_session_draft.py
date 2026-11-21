"""Session primitives only; these tests do not establish HTTP security."""
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from nawa_demo.authorization import Principal
from nawa_demo.authorization import PERMISSIONS
from nawa_demo.session_draft import SessionError, SessionStore


class SessionDraftTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0]
        self.members = {('issuer|subject', 'tenant-a'): Principal('issuer|subject', 'tenant-a', 'analyst')}
        self.store = SessionStore(lambda s, t: self.members.get((s, t)), ttl=10, clock=lambda: self.clock[0])
        self.tokens = self.store.issue('issuer|subject', 'tenant-a')

    def test_csrf_role_and_tenant_checks(self):
        token, csrf = self.tokens['session'], self.tokens['csrf']
        self.assertEqual(self.store.authorize(token, 'correct', tenant='tenant-a', csrf=csrf).role, 'analyst')
        for kwargs in ({'tenant':'tenant-b', 'csrf':csrf}, {'tenant':'tenant-a'},
                       {'tenant':'tenant-a', 'csrf':'wrong'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SessionError):
                self.store.authorize(token, 'correct', **kwargs)
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'decide', tenant='tenant-a', csrf=csrf)

    def test_invalid_session_capacity_and_clock_are_rejected(self):
        for limit in (float('nan'), float('inf'), 0, -1, True, 1.5, '256'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                SessionStore(lambda s, t: None, limit=limit)
        with self.assertRaises(ValueError):
            SessionStore(lambda s, t: None, clock=None)

    def test_membership_changes_and_revocation_take_effect(self):
        token = self.tokens['session']
        self.members[('issuer|subject','tenant-a')] = Principal('issuer|subject','tenant-a','viewer')
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'correct', tenant='tenant-a', csrf=self.tokens['csrf'])
        del self.members[('issuer|subject','tenant-a')]
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')

    def test_observed_membership_revocation_does_not_revive_old_session(self):
        token = self.tokens['session']
        del self.members[('issuer|subject','tenant-a')]
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        self.members[('issuer|subject','tenant-a')] = Principal('issuer|subject','tenant-a','analyst')
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        fresh = self.store.issue('issuer|subject', 'tenant-a')['session']
        self.assertEqual(self.store.authorize(fresh, 'view', tenant='tenant-a').role, 'analyst')

    def test_membership_change_between_requests_invalidates_session(self):
        token = self.tokens['session']
        self.members[('issuer|subject','tenant-a')] = Principal(
            'issuer|subject', 'tenant-a', 'analyst', membership_generation=2)
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        fresh = self.store.issue('issuer|subject', 'tenant-a')['session']
        self.assertEqual(self.store.authorize(fresh, 'view', tenant='tenant-a').role, 'analyst')

    def test_identity_or_tenant_generation_change_invalidates_session(self):
        for field in ('identity_generation', 'tenant_generation'):
            with self.subTest(field=field):
                self.members[('issuer|subject','tenant-a')] = Principal('issuer|subject', 'tenant-a', 'analyst')
                token = self.store.issue('issuer|subject', 'tenant-a')['session']
                self.members[('issuer|subject','tenant-a')] = Principal(
                    'issuer|subject', 'tenant-a', 'analyst', **{field:2})
                with self.assertRaises(SessionError):
                    self.store.authorize(token, 'view', tenant='tenant-a')

    def test_transient_membership_lookup_error_does_not_revoke_session(self):
        token = self.tokens['session']
        lookup = self.store.lookup
        self.store.lookup = lambda *_: (_ for _ in ()).throw(OSError('synthetic outage'))
        with self.assertRaises(OSError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        self.store.lookup = lookup
        self.assertEqual(self.store.authorize(token, 'view', tenant='tenant-a').role, 'analyst')

    def test_session_expiring_during_membership_lookup_is_denied(self):
        token = self.tokens['session']
        lookup = self.store.lookup
        def slow_lookup(subject, tenant):
            self.clock[0] = 10
            return lookup(subject, tenant)
        self.store.lookup = slow_lookup
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        self.clock[0] = 0
        self.store.lookup = lookup
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')

    def test_recovery_blocks_sessions_and_requires_fresh_login(self):
        token = self.tokens['session']
        self.store.begin_recovery()
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        with self.assertRaises(SessionError):
            self.store.issue('issuer|subject', 'tenant-a')
        with self.assertRaises(SessionError):
            self.store.begin_recovery()
        self.store.end_recovery()
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        fresh = self.store.issue('issuer|subject', 'tenant-a')['session']
        self.assertEqual(self.store.authorize(fresh, 'view', tenant='tenant-a').role, 'analyst')
        with self.assertRaises(SessionError):
            self.store.end_recovery()

    def test_issuance_in_flight_before_recovery_cannot_resume_afterwards(self):
        started, release = threading.Event(), threading.Event()
        original = self.store.lookup
        def slow_lookup(subject, tenant):
            started.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic test timed out')
            return original(subject, tenant)
        self.store.lookup = slow_lookup
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.store.issue, 'issuer|subject', 'tenant-a')
            try:
                self.assertTrue(started.wait(5))
                self.store.begin_recovery()
                self.store.end_recovery()
            finally:
                release.set()
            with self.assertRaises(SessionError):
                future.result(timeout=5)

    def test_logout_and_expiration(self):
        token = self.tokens['session']
        with self.assertRaises(SessionError):
            self.store.logout(token, 'wrong')
        self.store.logout(token, self.tokens['csrf'])
        with self.assertRaises(SessionError):
            self.store.authorize(token, 'view', tenant='tenant-a')
        new = self.store.issue('issuer|subject', 'tenant-a')['session']
        self.clock[0] = 10
        with self.assertRaises(SessionError):
            self.store.authorize(new, 'view', tenant='tenant-a')

    def test_tokens_are_not_stored_as_plaintext(self):
        self.assertNotIn(self.tokens['session'], repr(self.store._sessions))
        self.assertNotIn(self.tokens['csrf'], repr(self.store._sessions))
        with self.assertRaises(SessionError):
            self.store.issue('unknown', 'tenant-a')

    def test_session_lifetime_is_bounded_by_identity_expiry(self):
        tokens = self.store.issue('issuer|subject', 'tenant-a', max_age=3)
        self.assertEqual(tokens['max_age'], 3)
        self.clock[0] = 3
        with self.assertRaises(SessionError):
            self.store.authorize(tokens['session'], 'view', tenant='tenant-a')
        with self.assertRaises(SessionError):
            self.store.issue('issuer|subject', 'tenant-a', max_age=0)
        with self.assertRaises(ValueError):
            SessionStore(lambda s, t: None, ttl=301)

    def test_identity_deadline_is_rechecked_after_slow_membership_lookup(self):
        lookup = self.store.lookup
        def slow_lookup(subject, tenant):
            self.clock[0] = 4
            return lookup(subject, tenant)
        self.store.lookup = slow_lookup
        tokens = self.store.issue('issuer|subject', 'tenant-a', deadline=7)
        self.assertEqual(tokens['max_age'], 3)
        self.clock[0] = 7
        with self.assertRaises(SessionError):
            self.store.authorize(tokens['session'], 'view', tenant='tenant-a')
        with self.assertRaises(SessionError):
            self.store.issue('issuer|subject', 'tenant-a', deadline=4)

    def test_new_permission_requires_csrf_by_default(self):
        token, csrf = self.tokens['session'], self.tokens['csrf']
        with patch.dict(PERMISSIONS, {'analyst': PERMISSIONS['analyst'] | {'future_action'}}):
            with self.assertRaises(SessionError):
                self.store.authorize(token, 'future_action', tenant='tenant-a')
            self.assertEqual(self.store.authorize(token, 'future_action', tenant='tenant-a', csrf=csrf).role, 'analyst')


if __name__ == '__main__':
    unittest.main()

