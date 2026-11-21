import base64
import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from nawa_demo.oidc_attempts import LoginAttempts, InvalidLoginAttempt


class LoginAttemptTests(unittest.TestCase):
    def test_invalid_attempt_limits_are_rejected(self):
        for ttl in (float('nan'), float('inf'), 0, -1, True, '300'):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                LoginAttempts(ttl=ttl)
        for limit in (float('nan'), float('inf'), 0, -1, True, 1.5, '128'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                LoginAttempts(limit=limit)
        with self.assertRaises(ValueError):
            LoginAttempts(clock=None)

    def test_pkce_and_one_time_use(self):
        attempts = LoginAttempts()
        started = attempts.create()
        result = attempts.consume(started['state'], started['state'])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(result['verifier'].encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.assertEqual(started['challenge'], challenge)
        self.assertEqual(started['nonce'], result['nonce'])
        with self.assertRaises(InvalidLoginAttempt):
            attempts.consume(started['state'], started['state'])

    def test_cookie_binding_expiry_and_capacity(self):
        current = [10]
        attempts = LoginAttempts(ttl=5, limit=1, clock=lambda: current[0])
        started = attempts.create()
        with self.assertRaises(InvalidLoginAttempt):
            attempts.consume(started['state'], 'wrong')
        with self.assertRaises(InvalidLoginAttempt):
            attempts.create()
        current[0] = 15
        with self.assertRaises(InvalidLoginAttempt):
            attempts.consume(started['state'], started['state'])
        self.assertTrue(attempts.create()['state'])

    def test_concurrent_callback_only_one_succeeds(self):
        attempts = LoginAttempts()
        state = attempts.create()['state']
        def consume(_):
            try:
                attempts.consume(state, state)
                return 'success'
            except InvalidLoginAttempt:
                return 'denied'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(consume, range(2))), ['success', 'denied'])


if __name__ == '__main__':
    unittest.main()
