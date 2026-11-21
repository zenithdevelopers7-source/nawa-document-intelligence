"""Fixture-only ordering checks; no real recovery or network operations."""
import unittest

from nawa_demo.authorization import Principal
from nawa_demo.session_draft import SessionError, SessionStore
from nawa_demo.synthetic_recovery_coordinator import prepare_recovery


class SyntheticRecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal('synthetic-user', 'synthetic-tenant', 'analyst')
        self.stores = [SessionStore(lambda *_: self.principal) for _ in range(2)]
        self.tokens = [store.issue(self.principal.subject, self.principal.tenant)['session'] for store in self.stores]
        self.events = []

    def callback(self, name, result=True):
        def run():
            self.events.append(name)
            return result
        return run

    def invoke(self, **overrides):
        args = {name:self.callback(name) for name in ('quiesce', 'drain', 'restore', 'verify')}
        args.update(overrides)
        return prepare_recovery(self.stores, **args)

    def test_success_keeps_every_store_paused_until_manual_resume(self):
        def restore():
            self.events.append('restore')
            for store, token in zip(self.stores, self.tokens):
                with self.assertRaises(SessionError):
                    store.authorize_current(token, 'view')
                with self.assertRaises(SessionError):
                    store.issue(self.principal.subject, self.principal.tenant)
            return True
        result = self.invoke(restore=restore)
        self.assertEqual(result, {'status':'ready_for_manual_resume', 'completed_pause_calls':2})
        self.assertEqual(self.events, ['quiesce', 'drain', 'restore', 'verify'])
        for store, token in zip(self.stores, self.tokens):
            with self.assertRaises(SessionError):
                store.issue(self.principal.subject, self.principal.tenant)
            store.end_recovery()
            with self.assertRaises(SessionError):
                store.authorize_current(token, 'view')
            fresh = store.issue(self.principal.subject, self.principal.tenant)['session']
            self.assertEqual(store.authorize_current(fresh, 'view').role, 'analyst')

    def test_unconfirmed_drain_does_not_restore_or_pause(self):
        result = self.invoke(drain=self.callback('drain', False))
        self.assertEqual(result, {'status':'blocked', 'stage':'drain', 'completed_pause_calls':0})
        self.assertEqual(self.events, ['quiesce', 'drain'])
        for store, token in zip(self.stores, self.tokens):
            self.assertEqual(store.authorize_current(token, 'view').role, 'analyst')

    def test_unconfirmed_quiesce_does_not_claim_a_pause(self):
        result = self.invoke(quiesce=self.callback('quiesce', False))
        self.assertEqual(result, {'status':'blocked', 'stage':'quiesce', 'completed_pause_calls':0})
        self.assertEqual(self.events, ['quiesce'])

    def test_partial_pause_does_not_restore_and_does_not_resume(self):
        self.stores[1].begin_recovery()
        result = self.invoke()
        self.assertEqual(result, {'status':'blocked', 'stage':'pause_sessions', 'completed_pause_calls':1})
        self.assertEqual(self.events, ['quiesce', 'drain'])
        for store in self.stores:
            with self.assertRaises(SessionError):
                store.issue(self.principal.subject, self.principal.tenant)

    def test_failed_restore_or_verification_keeps_stores_paused(self):
        for failed in ('restore', 'verify'):
            with self.subTest(failed=failed):
                stores = [SessionStore(lambda *_: self.principal) for _ in range(2)]
                callbacks = {name:(lambda:True) for name in ('quiesce', 'drain', 'restore', 'verify')}
                callbacks[failed] = lambda:False
                result = prepare_recovery(stores, **callbacks)
                self.assertEqual(result['stage'], failed)
                self.assertEqual(result['status'], 'blocked')
                for store in stores:
                    with self.assertRaises(SessionError):
                        store.issue(self.principal.subject, self.principal.tenant)

    def test_duplicate_store_and_missing_callback_rejected(self):
        with self.assertRaises(ValueError):
            prepare_recovery([self.stores[0], self.stores[0]], quiesce=lambda:True,
                             drain=lambda:True, restore=lambda:True, verify=lambda:True)
        with self.assertRaises(ValueError):
            self.invoke(verify=None)

    def test_restore_exception_does_not_expose_details_or_resume(self):
        def failed_restore():
            raise RuntimeError('private synthetic diagnostic')
        result = self.invoke(restore=failed_restore)
        self.assertEqual(result, {'status':'blocked', 'stage':'restore', 'completed_pause_calls':2})
        self.assertNotIn('private synthetic diagnostic', str(result))
        for store in self.stores:
            with self.assertRaises(SessionError):
                store.issue(self.principal.subject, self.principal.tenant)


if __name__ == '__main__':
    unittest.main()
