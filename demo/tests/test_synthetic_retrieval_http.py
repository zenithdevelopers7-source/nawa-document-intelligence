"""Opt-in HTTP tests with synthetic in-memory identities and passages."""
import json
import threading
import unittest
from dataclasses import replace
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nawa_demo.authorization import Principal
from nawa_demo.dashboard import DashboardState
from nawa_demo.protected_api import make_protected_handler
from nawa_demo.retrieval import Passage
from nawa_demo.session_draft import SessionStore
from nawa_demo.synthetic_live_retrieval import SyntheticLiveRetrieval
from nawa_demo.synthetic_review_queue import SyntheticReviewQueue


class SyntheticRetrievalHttpTests(unittest.TestCase):
    def setUp(self):
        self.members = {('alice','a'):Principal('alice','a','analyst'),
                        ('bob','a'):Principal('bob','a','viewer'),
                        ('reviewer','a'):Principal('reviewer','a','reviewer'),
                        ('eve','b'):Principal('eve','b','viewer')}
        self.docs = [Passage.from_dict({'passage_id':'a-v1','document_id':'doc-a','version':'1',
            'tenant_id':'a','page':1,'text':'alpha shared','allowed_principals':['alice','reviewer'],'source':'a.txt'}),
            Passage.from_dict({'passage_id':'b-v1','document_id':'doc-b','version':'1',
            'tenant_id':'b','page':1,'text':'beta shared','allowed_principals':['eve'],'source':'b.txt'})]
        lookup = lambda s,t:self.members.get((s,t))
        self.sessions = SessionStore(lookup)
        self.tokens = {key:self.sessions.issue(*key) for key in self.members}
        self.retrieval = SyntheticLiveRetrieval(lookup, lambda p:list(self.docs))
        self.queue = SyntheticReviewQueue(self.retrieval)
        handler = make_protected_handler(None, self.sessions, DashboardState(), synthetic_retrieval=self.retrieval,
                                         synthetic_review_queue=self.queue)
        self.server = HTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, payload=None, token=None, headers=None, raw=None):
        values = {'Origin':'http://127.0.0.1:8765', 'Content-Type':'application/json'}
        if token is not None:
            values['Cookie'] = 'nawa_session=' + token['session']
            values['X-NAWA-CSRF'] = token['csrf']
        values.update(headers or {})
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        try:
            with urlopen(Request(self.url + path, data=data, headers=values), timeout=5) as response:
                return response.status, json.load(response), response.headers.get('Cache-Control')
        except HTTPError as error:
            with error:
                return error.code, json.load(error), error.headers.get('Cache-Control')

    def test_session_scopes_search_and_source(self):
        search = '/api/synthetic-evidence/search'
        self.assertEqual(self.request(search, {'q':'shared'})[0], 403)
        status, result, cache = self.request(search, {'q':'shared'}, self.tokens[('alice','a')],
                                              {'X-Tenant':'b', 'X-Role':'reviewer'})
        self.assertEqual((status, cache), (200, 'no-store'))
        self.assertEqual([p['document_id'] for p in result['passages']], ['doc-a'])
        self.assertNotIn('tenant_id', result['passages'][0])
        self.assertNotIn('allowed_principals', result['passages'][0])
        self.assertEqual(self.request(search, {'q':'shared'}, self.tokens[('bob','a')])[1]['passages'], [])
        self.assertEqual(self.request(search, {'q':'shared'}, self.tokens[('eve','b')])[1]['passages'][0]['document_id'], 'doc-b')
        review = '/api/synthetic-evidence/review'
        self.assertEqual(self.request(review, {'q':'shared'})[0], 403)
        status, handoff, _ = self.request(review, {'q':'shared'}, self.tokens[('alice','a')])
        self.assertEqual((status, handoff['status'], handoff['answer'], handoff['citations']),
                         (200, 'requires_human_review', None, []))
        self.assertEqual([e['document_id'] for e in handoff['evidence']], ['doc-a'])
        self.assertEqual(handoff['evidence'][0]['untrusted_source_text'], 'alpha shared')
        self.assertEqual(self.request(review, {'q':'shared'}, self.tokens[('bob','a')])[1]['evidence'], [])
        self.assertEqual(self.request(review, {'q':'shared'}, self.tokens[('eve','b')])[1]['evidence'][0]['document_id'], 'doc-b')
        self.assertEqual(self.request(review, {'q':'shared'}, self.tokens[('alice','a')], {'X-NAWA-CSRF':'bad'})[0], 403)
        own = '/api/synthetic-evidence/source'
        self.assertEqual(self.request(own, {'id':'a-v1'}, self.tokens[('alice','a')])[1]['passage']['text'], 'alpha shared')
        self.assertEqual(self.request(own, {'id':'a-v1'}, self.tokens[('bob','a')])[0], 503)
        self.assertEqual(self.request(own, {'id':'a-v1'}, self.tokens[('eve','b')])[0], 503)
        self.docs.pop(0)
        self.assertEqual(self.request(own, {'id':'a-v1'}, self.tokens[('alice','a')])[0], 503)
        self.assertEqual(self.request(review, {'q':'shared'}, self.tokens[('alice','a')])[1]['evidence'], [])

    def test_invalid_parameters_and_membership_revocation(self):
        token = self.tokens[('alice','a')]
        path = '/api/synthetic-evidence/search'
        self.assertEqual(self.request(path, token=token, raw=b'{"q":"x","q":"y"}')[0], 400)
        self.assertEqual(self.request(path, {'q':'x', 'tenant':'b'}, token)[0], 400)
        self.assertEqual(self.request(path + '?q=shared', {'q':'x'}, token)[0], 400)
        self.assertEqual(self.request(path, {'q':'shared'}, token, {'X-NAWA-CSRF':''})[0], 403)
        self.assertEqual(self.request(path, {'q':'shared'}, token, {'X-NAWA-CSRF':'bad'})[0], 403)
        self.assertEqual(self.request(path, {'q':'shared'}, token, {'Origin':'https://evil.example'})[0], 403)
        self.assertEqual(self.request(path, {'q':'x' * 201}, token)[0], 400)
        self.assertEqual(self.request(path + '?q=shared')[0], 404)
        self.members.pop(('alice','a'))
        self.assertEqual(self.request(path, {'q':'shared'}, token)[0], 403)

    def test_review_request_is_idempotent_scoped_and_rechecked(self):
        create, show = '/api/synthetic-review/create', '/api/synthetic-review/show'
        payload = {'request_key':'a' * 32, 'question':'Review this source', 'passage_id':'a-v1'}
        alice, reviewer = self.tokens[('alice','a')], self.tokens[('reviewer','a')]
        self.assertEqual(self.request(create, payload)[0], 403)
        self.assertEqual(self.request(create, payload, alice, {'X-NAWA-CSRF':'bad'})[0], 403)
        self.assertEqual(self.request(create, payload, self.tokens[('bob','a')])[0], 403)
        self.assertEqual(self.request(create, {**payload,'actor':'forged'}, alice)[0], 400)
        status, first, _ = self.request(create, payload, alice)
        self.assertEqual((status,first['status'],first['created'],first['answer']), (200,'pending_review',True,None))
        retry = self.request(create, payload, alice)[1]
        self.assertEqual((retry['id'],retry['created']), (first['id'],False))
        self.assertEqual(self.request(create, {**payload,'question':'Different'}, alice)[0], 400)
        self.assertEqual(self.request(show, {'id':first['id']}, reviewer)[0], 200)
        self.assertEqual(self.request(show, {'id':first['id']}, self.tokens[('bob','a')])[0], 400)
        self.assertEqual(self.request(show, {'id':first['id']}, self.tokens[('eve','b')])[0], 400)
        self.docs[0] = replace(self.docs[0], allowed_principals=('reviewer',))
        self.assertEqual(self.request(show, {'id':first['id']}, alice)[0], 400)
        self.assertEqual(self.request(show, {'id':first['id']}, reviewer)[0], 200)
        self.docs[0] = replace(self.docs[0], allowed_principals=('alice',))
        self.assertEqual(self.request(show, {'id':first['id']}, reviewer)[0], 400)


if __name__ == '__main__':
    unittest.main()
