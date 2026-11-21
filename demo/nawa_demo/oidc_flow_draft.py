"""Draft OIDC orchestration with an injected trusted provider; not wired to HTTP.

The provider must use verified TLS, bounded timeouts and no untrusted redirects.
No credentials, tokens or login attempt values may be logged. This draft uses a
process-local attempt and session store, unsuitable for multi-worker production.
"""
from urllib.parse import urlencode, urlparse
import time

from .oidc_validation import verify_id_token


class LoginError(ValueError):
    pass


class OidcFlow:
    def __init__(self, *, issuer, client_id, redirect_uri, provider, attempts, sessions, resolve_membership):
        parsed = urlparse(issuer)
        redirect = urlparse(redirect_uri)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not client_id or not redirect.hostname
                or redirect.username or redirect.password or redirect.query or redirect.fragment
                or not (redirect.scheme == 'https' or (redirect.scheme == 'http' and redirect.hostname == '127.0.0.1'))):
            raise ValueError('Invalid trusted identity configuration')
        self.issuer, self.client_id, self.redirect_uri = issuer, client_id, redirect_uri
        self.provider, self.attempts, self.sessions, self.resolve_membership = provider, attempts, sessions, resolve_membership

    def _metadata(self):
        meta = self.provider.metadata()
        if not isinstance(meta, dict) or meta.get('issuer') != self.issuer:
            raise LoginError('Trusted identity service unavailable')
        source = urlparse(self.issuer)
        for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            route = urlparse(meta.get(name, ''))
            if (route.scheme != 'https' or route.netloc != source.netloc or route.username or route.password
                    or route.query or route.fragment):
                raise LoginError('Trusted identity service unavailable')
        return meta

    def begin(self):
        try:
            meta = self._metadata()
            attempt = self.attempts.create()
        except Exception:
            raise LoginError('Trusted identity service unavailable') from None
        query = urlencode({'response_type': 'code', 'client_id': self.client_id,
                           'redirect_uri': self.redirect_uri, 'scope': 'openid',
                           'state': attempt['state'], 'nonce': attempt['nonce'],
                           'code_challenge': attempt['challenge'], 'code_challenge_method': 'S256'})
        return {'authorization_url': meta['authorization_endpoint'] + '?' + query,
                'state_cookie': attempt['state']}

    def callback(self, *, state, state_cookie, code):
        try:
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise LoginError('Sign-in could not be completed')
            attempt = self.attempts.consume(state, state_cookie)
            meta = self._metadata()
            token = self.provider.exchange(meta['token_endpoint'], code=code,
                                           verifier=attempt['verifier'], redirect_uri=self.redirect_uri,
                                           client_id=self.client_id)
            identity = verify_id_token(token, jwks=self.provider.jwks(meta['jwks_uri']),
                                       issuer=self.issuer, audience=self.client_id, nonce=attempt['nonce'])
            principal = self.resolve_membership(identity['issuer'], identity['subject'])
            if principal is None:
                raise LoginError('Sign-in could not be completed')
            # Anchor the verified token's remaining lifetime before issuance.
            # Issuance performs another membership lookup, which may be slow.
            started = self.sessions.clock()
            remaining = identity['expires_at'] - time.time()
            if remaining < 1:
                raise LoginError('Sign-in could not be completed')
            return self.sessions.issue(principal.subject, principal.tenant, deadline=started + remaining)
        except Exception:
            raise LoginError('Sign-in could not be completed') from None
