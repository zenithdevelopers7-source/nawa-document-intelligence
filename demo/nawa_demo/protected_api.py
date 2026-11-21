"""Draft protected API handler. Not wired into the dashboard entrypoint.

The supplied session service must resolve current server-side membership, and
the scoped service must enforce tenant boundaries in every database operation.
No unauthenticated fallback is allowed for protected paths.
"""
from http.cookies import SimpleCookie, CookieError
import base64
import json
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from .dashboard import MAX_BODY, make_handler
from .session_draft import SessionError
from .scoped_state import AccessDenied, ReviewNotInitialized
from .intake_postgres import IntakeRejected
from .synthetic_request_gate import GateClosed, SyntheticRequestGate
from .synthetic_live_retrieval import EvidenceUnavailable, SyntheticLiveRetrieval
from .grounded_qa import format_review_handoff
from .synthetic_review_queue import SyntheticReviewQueue


def make_protected_handler(scoped, sessions, health_state, *, flow=None, secure_cookies=False,
                           allowed_origin='http://127.0.0.1:8765', intake=None,
                           synthetic_request_gate=None, synthetic_retrieval=None, synthetic_review_queue=None):
    if synthetic_request_gate is not None and not isinstance(synthetic_request_gate, SyntheticRequestGate):
        raise ValueError('Invalid synthetic request gate')
    if synthetic_retrieval is not None and not isinstance(synthetic_retrieval, SyntheticLiveRetrieval):
        raise ValueError('Invalid synthetic retrieval')
    if (synthetic_review_queue is not None and
            (not isinstance(synthetic_review_queue, SyntheticReviewQueue)
             or not synthetic_review_queue.uses_retrieval(synthetic_retrieval))):
        raise ValueError('Invalid synthetic review queue')
    Base = make_handler(health_state)

    class Handler(Base):
        def _cookie(self, name):
            raw = self.headers.get('Cookie', '')
            if sum(part.strip().startswith(name + '=') for part in raw.split(';')) != 1:
                raise SessionError('Session unavailable')
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except CookieError:
                raise SessionError('Session unavailable') from None
            if name not in cookie:
                raise SessionError('Session unavailable')
            return cookie[name].value

        def _session(self):
            return self._cookie('nawa_session')

        def _redirect(self, location, cookies):
            if '\r' in location or '\n' in location:
                raise ValueError('Invalid redirect')
            self.send_response(303)
            self.send_header('Location', location)
            self.send_header('Cache-Control', 'no-store')
            for cookie in cookies:
                self.send_header('Set-Cookie', cookie)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def _set_cookie(self, name, value, *, http_only, max_age):
            flags = '; Path=/; SameSite=Lax; Max-Age=' + str(max_age)
            if http_only:
                flags += '; HttpOnly'
            if secure_cookies:
                flags += '; Secure'
            return name + '=' + value + flags

        def _principal(self, action, *, require_csrf=False):
            if require_csrf:
                return sessions.authorize_current(self._session(), action, csrf=self.headers.get('X-NAWA-CSRF'),
                                                  require_csrf=True)
            return sessions.authorize_current(self._session(), action, csrf=self.headers.get('X-NAWA-CSRF'))

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/auth/config':
                return self._json({'mode': 'oidc', 'intake_enabled': intake is not None})
            if path == '/auth/login' and flow is not None:
                try:
                    result = flow.begin()
                    return self._redirect(result['authorization_url'],
                        [self._set_cookie('nawa_oidc_state', result['state_cookie'], http_only=True, max_age=300)])
                except Exception:
                    return self._json({'error': 'Sign-in unavailable'}, 503)
            if path == '/auth/callback' and flow is not None:
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if len(query.get('code', [])) != 1 or len(query.get('state', [])) != 1:
                        raise ValueError('Invalid callback')
                    tokens = flow.callback(state=query['state'][0], state_cookie=self._cookie('nawa_oidc_state'),
                                           code=query['code'][0])
                    return self._redirect('/', [
                        self._set_cookie('nawa_oidc_state', '', http_only=True, max_age=0),
                        self._set_cookie('nawa_session', tokens['session'], http_only=True, max_age=tokens['max_age']),
                        self._set_cookie('nawa_csrf', tokens['csrf'], http_only=False, max_age=tokens['max_age'])])
                except Exception:
                    return self._json({'error': 'Sign-in could not be completed'}, 403)
            if path == '/auth/me':
                try:
                    principal = self._principal('view')
                    return self._json({'tenant': principal.tenant, 'role': principal.role})
                except (SessionError, AccessDenied):
                    return self._json({'error': 'Authentication or permission required'}, 403)
                except Exception:
                    return self._json({'error': 'Identity service unavailable'}, 503)
            if path == '/api/intake/documents' and intake is not None:
                try:
                    principal = self._principal('list')
                    return self._json({'documents':intake.list_documents(principal)})
                except (SessionError, AccessDenied, PermissionError):
                    return self._json({'error':'Authentication or permission required'}, 403)
                except Exception:
                    return self._json({'error':'Intake unavailable'}, 503)
            if path in ('/api/intake/status', '/api/intake/result') and intake is not None:
                try:
                    principal = self._principal('view')
                    ids = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('id', [])
                    if len(ids) != 1:
                        raise ValueError('Invalid request')
                    value = intake.state(principal, ids[0]) if path.endswith('/status') else intake.result(principal, ids[0])
                    if value is None:
                        return self._json({'error': 'Not found'}, 404)
                    if path.endswith('/status'):
                        return self._json({'state': value[0], 'attempts': value[1]})
                    return self._json({'result': value})
                except (SessionError, AccessDenied, PermissionError):
                    return self._json({'error': 'Authentication or permission required'}, 403)
                except (ValueError, TypeError):
                    return self._json({'error': 'Invalid request'}, 400)
                except Exception:
                    return self._json({'error': 'Intake unavailable'}, 503)
            if path == '/api/intake/versions' and intake is not None:
                try:
                    principal = self._principal('view')
                    families = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('family', [])
                    if len(families) != 1:
                        raise ValueError('Invalid request')
                    versions = intake.version_history(principal, families[0])
                    if not versions:
                        return self._json({'error': 'Not found'}, 404)
                    return self._json({'versions': [{'version': version, 'id': document} for version, document in versions]})
                except (SessionError, AccessDenied, PermissionError):
                    return self._json({'error': 'Authentication or permission required'}, 403)
                except (ValueError, TypeError):
                    return self._json({'error': 'Invalid request'}, 400)
                except Exception:
                    return self._json({'error': 'Intake unavailable'}, 503)
            if path == '/api/quotations':
                try:
                    principal = self._principal('list')
                    return self._json({'quotations': scoped.list_items(principal), 'storage': 'postgres'})
                except (SessionError, AccessDenied):
                    return self._json({'error': 'Authentication or permission required'}, 403)
                except Exception:
                    return self._json({'error': 'Quotation storage unavailable'}, 503)
            # Only static files, public sample names and readiness can be served
            # by the base handler; no other base API path is reachable here.
            if path in ('/', '/app.js', '/style.css', '/favicon.ico', '/api/samples', '/api/ready'):
                return super().do_GET()
            return self._json({'error': 'Not found'}, 404)

        def do_POST(self):
            path = urlsplit(self.path).path
            evidence_path = path in ('/api/synthetic-evidence/search', '/api/synthetic-evidence/source',
                                     '/api/synthetic-evidence/review') and synthetic_retrieval is not None
            task_path = path in ('/api/synthetic-review/create', '/api/synthetic-review/show') and synthetic_review_queue is not None
            if evidence_path or task_path:
                try:
                    if (urlsplit(self.path).query or self.headers.get_content_type() != 'application/json'
                            or self.headers.get('Transfer-Encoding')):
                        self.close_connection = True
                        raise ValueError('Invalid request')
                    length = int(self.headers.get('Content-Length', '-1'))
                    if length < 1 or length > 1024:
                        self.close_connection = True
                        raise ValueError('Invalid request')
                    self.connection.settimeout(10)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('Invalid request')
                    if self.headers.get('Origin') != allowed_origin:
                        raise SessionError('Request denied')
                    principal = self._principal('review_start' if path.endswith('/create') else 'view', require_csrf=True)
                    def unique_pairs(pairs):
                        if len({key for key, _ in pairs}) != len(pairs):
                            raise ValueError('Invalid request')
                        return dict(pairs)
                    payload = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_pairs)
                    if task_path:
                        fields = {'request_key','question','passage_id'} if path.endswith('/create') else {'id'}
                        if not isinstance(payload, dict) or set(payload) != fields:
                            raise ValueError('Invalid request')
                        if path.endswith('/create'):
                            return self._json(synthetic_review_queue.create(principal, payload['request_key'],
                                                                           payload['question'], payload['passage_id']))
                        return self._json(synthetic_review_queue.get(principal, payload['id']))
                    field = 'id' if path.endswith('/source') else 'q'
                    if not isinstance(payload, dict) or set(payload) != {field}:
                        raise ValueError('Invalid request')
                    value = payload[field]
                    if not isinstance(value, str) or not value.strip() or len(value) > 200:
                        raise ValueError('Invalid request')
                    if field == 'q':
                        hits = synthetic_retrieval.search(principal.subject, principal.tenant, value,
                                                          expected_principal=principal)
                        return self._json(format_review_handoff(hits) if path.endswith('/review') else {'passages':hits})
                    passage = synthetic_retrieval.resolve(principal.subject, principal.tenant, value,
                                                          expected_principal=principal)
                    return self._json({'passage':{'passage_id':passage.passage_id, 'document_id':passage.document_id,
                        'version':passage.version, 'page':passage.page, 'source':passage.source,
                        'method':passage.method, 'text':passage.text}})
                except (SessionError, AccessDenied):
                    return self._json({'error': 'Authentication or permission required'}, 403)
                except EvidenceUnavailable:
                    return self._json({'error': 'Evidence unavailable'}, 503)
                except (ValueError, TypeError, UnicodeError):
                    return self._json({'error': 'Invalid request'}, 400)
                except Exception:
                    return self._json({'error': 'Evidence unavailable'}, 503)
            if path == '/auth/logout':
                try:
                    if self.headers.get('Origin') != allowed_origin:
                        raise SessionError('Request denied')
                    sessions.logout(self._session(), self.headers.get('X-NAWA-CSRF'))
                    return self._redirect('/', [self._set_cookie('nawa_session', '', http_only=True, max_age=0),
                                                self._set_cookie('nawa_csrf', '', http_only=False, max_age=0)])
                except SessionError:
                    return self._json({'error': 'Authentication or permission required'}, 403)
                except Exception:
                    return self._json({'error': 'Identity service unavailable'}, 503)
            actions = {'/api/load-sample': 'load', '/api/upload': 'load', '/api/compare': 'compare',
                       '/api/review/start': 'review_start', '/api/review/show': 'review_view',
                       '/api/review/decide': 'decide', '/api/review/correct': 'correct'}
            if intake is not None:
                actions['/api/intake/submit'] = 'load'
                actions['/api/intake/batch'] = 'load'
                actions['/api/intake/version'] = 'load'
                actions['/api/intake/promote'] = 'load'
                actions['/api/intake/revalidate'] = 'load'
                actions['/api/intake/retry'] = 'load'
            action = actions.get(path)
            if action is None:
                # Consume only bounded bodies so a 404 is delivered reliably
                # on Windows instead of resetting a socket with unread bytes.
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if 0 < length <= MAX_BODY and not self.headers.get('Transfer-Encoding'):
                        self.connection.settimeout(10)
                        self.rfile.read(length)
                except (ValueError, OSError):
                    self.close_connection = True
                return self._json({'error': 'Not found'}, 404)
            try:
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('Content-Type must be application/json')
                length = int(self.headers.get('Content-Length', '-1'))
                if length < 1 or length > MAX_BODY or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Request body size invalid')
                # Consume a bounded body before returning an authorization error.
                # Otherwise Windows can reset the connection instead of delivering
                # the 403 when unread bytes remain in the socket.
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('Incomplete request body')
                # All POST requests require an expected browser origin. State
                # changes additionally require the session-bound CSRF token.
                if self.headers.get('Origin') != allowed_origin:
                    return self._json({'error': 'Request denied'}, 403)
                principal = self._principal(action)
                payload = json.loads(raw.decode('utf-8'))
                if not isinstance(payload, dict):
                    raise ValueError('Expected JSON object')
                if action == 'load':
                    if path in ('/api/intake/revalidate', '/api/intake/retry'):
                        operation = 'revalidate' if path.endswith('/revalidate') else 'retry'
                        result = intake.requeue(principal, payload.get('id'), action=operation, reason=payload.get('reason'))
                    elif path == '/api/intake/promote':
                        result = intake.promote(principal, payload.get('id'))
                    elif path == '/api/intake/version':
                        family = payload.get('family_id') or uuid4().hex
                        document_id = payload.get('id')
                        expected = payload.get('expected_previous')
                        if (not isinstance(family, str) or not isinstance(document_id, str)
                                or type(expected) is not int or expected < 0):
                            raise ValueError('Invalid version')
                        version = intake.link_version(principal, document_id, family, expected_previous=expected)
                        result = {'family_id': family, 'version': version}
                    elif path == '/api/intake/batch':
                        files = payload.get('files')
                        if not isinstance(files, list) or not 1 <= len(files) <= 10:
                            raise ValueError('Batch must contain 1 to 10 files')
                        results = []
                        for item in files:
                            try:
                                if not isinstance(item, dict):
                                    raise ValueError('Invalid item')
                                encoded = item.get('content_base64')
                                if not isinstance(encoded, str) or len(encoded) > MAX_BODY:
                                    raise ValueError('Invalid upload')
                                content = base64.b64decode(encoded, validate=True)
                                document_id, created = intake.submit(principal, item.get('filename'), content)
                                results.append({'id': document_id, 'created': created})
                            except IntakeRejected:
                                results.append({'error': 'Synthetic document limit reached'})
                            except (ValueError, TypeError):
                                # Each file is independent: do not return contents or parser errors.
                                results.append({'error': 'Invalid or unsupported file'})
                        result = {'files': results}
                    elif path == '/api/intake/submit':
                        encoded = payload.get('content_base64')
                        if not isinstance(encoded, str) or len(encoded) > MAX_BODY:
                            raise ValueError('Invalid upload')
                        content = base64.b64decode(encoded, validate=True)
                        document_id, created = intake.submit(principal, payload.get('filename'), content)
                        result = {'id': document_id, 'created': created}
                    elif path == '/api/upload':
                        result = scoped.add_upload(principal, payload.get('filename'), payload.get('content_base64'))
                    else:
                        result = scoped.add_sample(principal, payload.get('name'))
                elif action == 'compare':
                    result = scoped.compare(principal, payload.get('ids'))
                elif action == 'review_start':
                    result = scoped.review_start(principal, payload.get('id'))
                elif action == 'review_view':
                    result = scoped.review_show(principal, payload.get('id'))
                elif action == 'decide':
                    result = scoped.review_decision(principal, payload)
                else:
                    result = scoped.review_correction(principal, payload)
                return self._json(result)
            except (SessionError, AccessDenied, PermissionError):
                return self._json({'error': 'Authentication or permission required'}, 403)
            except ReviewNotInitialized:
                return self._json({'error': 'Review is not initialized'}, 404)
            except IntakeRejected:
                return self._json({'error': 'Synthetic document limit reached'}, 400)
            except (ValueError, TypeError, KeyError, UnicodeError):
                return self._json({'error': 'Invalid or unavailable request'}, 400)
            except Exception:
                return self._json({'error': 'Quotation storage unavailable'}, 503)

    if synthetic_request_gate is None:
        return Handler

    class SyntheticGatedHandler(Handler):
        """Opt-in single-process QA handler; not a recovery service."""
        def _gated(self, method):
            operation = synthetic_request_gate.operation()
            try:
                operation.__enter__()
            except GateClosed:
                # A rejected POST may have an unread body. Close the connection
                # so it cannot be interpreted as another HTTP request.
                self.close_connection = True
                return self._json({'error': 'Synthetic recovery in progress'}, 503)
            try:
                return method()
            finally:
                operation.__exit__(None, None, None)

        def do_GET(self):
            return self._gated(super().do_GET)

        def do_POST(self):
            return self._gated(super().do_POST)

    return SyntheticGatedHandler
