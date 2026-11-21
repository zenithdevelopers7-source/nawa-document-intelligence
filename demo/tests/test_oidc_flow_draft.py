"""Synthetic provider orchestration tests; not a live Keycloak login test."""
import json
import time
import unittest
from urllib.parse import parse_qs, urlparse
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from nawa_demo.authorization import Principal
from nawa_demo.oidc_attempts import LoginAttempts
from nawa_demo.oidc_flow_draft import LoginError, OidcFlow
from nawa_demo.session_draft import SessionStore


class FakeProvider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce = ''
        self.host = 'id.example'
    def metadata(self):
        return {'issuer': 'https://id.example/realm',
                'authorization_endpoint': f'https://{self.host}/authorize',
                'token_endpoint': f'https://{self.host}/token', 'jwks_uri': f'https://{self.host}/keys'}
    def exchange(self, endpoint, *, code, verifier, redirect_uri, client_id):
        if code != 'synthetic-code' or not verifier:
            raise ValueError('Bad synthetic code')
        return jwt.encode({'iss':'https://id.example/realm', 'aud':client_id, 'sub':'subject-1',
                           'nonce':self.nonce, 'iat':int(time.time()), 'exp':int(time.time())+300},
                          self.key, algorithm='RS256', headers={'kid':'key-1'})
    def jwks(self, endpoint):
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        key.update({'kid':'key-1', 'alg':'RS256', 'use':'sig'})
        return {'keys':[key]}


class OidcFlowDraftTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.principal = Principal('trusted-subject', 'tenant-a', 'viewer')
        self.sessions = SessionStore(lambda subject, tenant: self.principal if
                                     (subject, tenant) == ('trusted-subject', 'tenant-a') else None)
        self.flow = OidcFlow(issuer='https://id.example/realm', client_id='nawa',
                             redirect_uri='http://127.0.0.1:8765/auth/callback',
                             provider=self.provider, attempts=LoginAttempts(), sessions=self.sessions,
                             resolve_membership=lambda issuer, subject: self.principal if
                             (issuer, subject) == ('https://id.example/realm', 'subject-1') else None)

    def test_valid_login_and_replay_rejected(self):
        start = self.flow.begin()
        params = parse_qs(urlparse(start['authorization_url']).query)
        self.provider.nonce = params['nonce'][0]
        self.assertEqual(params['code_challenge_method'], ['S256'])
        tokens = self.flow.callback(state=params['state'][0], state_cookie=start['state_cookie'], code='synthetic-code')
        self.assertGreaterEqual(tokens['max_age'], 1)
        self.assertLessEqual(tokens['max_age'], 300)
        self.assertEqual(self.sessions.authorize_current(tokens['session'], 'view').subject, 'trusted-subject')
        with self.assertRaises(LoginError):
            self.flow.callback(state=params['state'][0], state_cookie=start['state_cookie'], code='synthetic-code')

    def test_bad_cookie_and_untrusted_discovery_denied(self):
        start = self.flow.begin()
        state = parse_qs(urlparse(start['authorization_url']).query)['state'][0]
        with self.assertRaises(LoginError):
            self.flow.callback(state=state, state_cookie='other', code='synthetic-code')
        self.provider.host = 'evil.example'
        with self.assertRaises(LoginError):
            self.flow.begin()

    def test_token_expiring_during_session_lookup_denies_login(self):
        clock = [100]
        def slow_lookup(subject, tenant):
            clock[0] += 301
            return self.principal
        self.flow.sessions = SessionStore(slow_lookup, clock=lambda: clock[0])
        start = self.flow.begin()
        params = parse_qs(urlparse(start['authorization_url']).query)
        self.provider.nonce = params['nonce'][0]
        with self.assertRaises(LoginError):
            self.flow.callback(state=params['state'][0], state_cookie=start['state_cookie'], code='synthetic-code')

    def test_nonce_and_membership_denied(self):
        start = self.flow.begin()
        params = parse_qs(urlparse(start['authorization_url']).query)
        self.provider.nonce = 'wrong'
        with self.assertRaises(LoginError):
            self.flow.callback(state=params['state'][0], state_cookie=start['state_cookie'], code='synthetic-code')
        start = self.flow.begin()
        params = parse_qs(urlparse(start['authorization_url']).query)
        self.provider.nonce = params['nonce'][0]
        self.flow.resolve_membership = lambda issuer, subject: None
        with self.assertRaises(LoginError):
            self.flow.callback(state=params['state'][0], state_cookie=start['state_cookie'], code='synthetic-code')


if __name__ == '__main__':
    unittest.main()
