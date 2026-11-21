"""Automated local synthetic Keycloak/intake check; not a browser login test."""
import base64
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4
import psycopg

from . import phase12_live_check as oidc


def main():
    oidc.ISSUER = 'https://localhost:19443/realms/nawa-phase13'
    oidc.ORIGIN = 'http://127.0.0.1:19765'
    fixture = json.loads(Path('/identity/fixture.json').read_text(encoding='utf-8'))
    users = {user['role']: user for user in fixture['users']}
    for _ in range(60):
        try:
            _, opener = oidc.client()
            status, _, _ = oidc.request(opener, oidc.ISSUER + '/.well-known/openid-configuration')
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        raise AssertionError('Synthetic identity provider unavailable')

    jar, analyst = oidc.login(users['analyst'])
    status, _, body = oidc.request(analyst, oidc.APP + '/auth/config')
    if status != 200 or json.loads(body).get('intake_enabled') is not True:
        raise AssertionError('Synthetic intake unavailable')
    csrf = next(cookie.value for cookie in jar if cookie.name == 'nawa_csrf')
    headers = {'Content-Type': 'application/json', 'Origin': oidc.ORIGIN, 'X-NAWA-CSRF': csrf}
    sample = Path('/app/demo/samples/english_quote.txt').read_bytes() + ('\n# qa-' + uuid4().hex + '\n').encode()
    body = json.dumps({'filename': 'synthetic.txt', 'content_base64': base64.b64encode(sample).decode()}).encode()
    status, _, _ = oidc.request(analyst, oidc.APP + '/api/intake/submit', data=body,
                                headers={'Content-Type':'application/json', 'Origin':oidc.ORIGIN})
    if status != 403:
        raise AssertionError('Missing CSRF token was accepted')
    status, _, result = oidc.request(analyst, oidc.APP + '/api/intake/submit', data=body, headers=headers)
    if status != 200:
        raise AssertionError('Synthetic upload failed')
    document_id = json.loads(result)['id']
    status, _, listing = oidc.request(analyst, oidc.APP + '/api/intake/documents')
    if status != 200 or document_id not in {item['id'] for item in json.loads(listing)['documents']}:
        raise AssertionError('Synthetic document was not listed')
    for _ in range(60):
        status, _, result = oidc.request(analyst, oidc.APP + '/api/intake/status?id=' + document_id)
        if status == 200 and json.loads(result)['state'] == 'completed':
            break
        time.sleep(1)
    else:
        raise AssertionError('Synthetic worker did not complete document')
    status, _, result = oidc.request(analyst, oidc.APP + '/api/intake/result?id=' + document_id)
    if status != 200 or json.loads(result)['result']['total'] != '1050.00':
        raise AssertionError('Synthetic result mismatch')
    promote_payload = json.dumps({'id':document_id}).encode()
    status, _, promoted_body = oidc.request(analyst, oidc.APP + '/api/intake/promote', data=promote_payload, headers=headers)
    if status != 200 or not json.loads(promoted_body)['created']:
        raise AssertionError('Synthetic review promotion failed')
    quotation_id = json.loads(promoted_body)['id']
    status, _, repeated = oidc.request(analyst, oidc.APP + '/api/intake/promote', data=promote_payload, headers=headers)
    if status != 200 or json.loads(repeated)['created'] or json.loads(repeated)['id'] != quotation_id:
        raise AssertionError('Synthetic review promotion was not idempotent')
    status, _, _ = oidc.request(analyst, oidc.APP + '/api/review/start',
                                data=json.dumps({'id':quotation_id}).encode(), headers=headers)
    if status != 200:
        raise AssertionError('Synthetic promoted review could not start')

    # This user is provisioned for tenant B only and must not sign in to A.
    oidc.login(users['outsider'], expected=403)
    app_a, origin_a = oidc.APP, oidc.ORIGIN
    oidc.APP, oidc.ORIGIN = 'http://127.0.0.1:8766', 'http://127.0.0.1:19766'
    for _ in range(30):
        try:
            status, _, _ = oidc.request(analyst, oidc.APP + '/api/ready')
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise AssertionError('Tenant B application unavailable')
    other_jar, other = oidc.login(users['outsider'])
    other_csrf = next(cookie.value for cookie in other_jar if cookie.name == 'nawa_csrf')
    other_headers = {'Content-Type':'application/json', 'Origin':oidc.ORIGIN, 'X-NAWA-CSRF':other_csrf}
    for endpoint in ('status', 'result'):
        status, _, _ = oidc.request(other, oidc.APP + '/api/intake/' + endpoint + '?id=' + document_id)
        if status != 404:
            raise AssertionError('Cross-tenant document access was not denied')
    status, _, other_result = oidc.request(other, oidc.APP + '/api/intake/submit', data=body, headers=other_headers)
    if status != 200 or not json.loads(other_result)['created']:
        raise AssertionError('Cross-tenant duplicate handling mismatch')
    other_document = json.loads(other_result)['id']
    if other_document == document_id:
        raise AssertionError('Tenant documents share an identifier')
    status, _, listing = oidc.request(other, oidc.APP + '/api/intake/documents')
    other_ids = {item['id'] for item in json.loads(listing)['documents']} if status == 200 else set()
    if other_document not in other_ids or document_id in other_ids:
        raise AssertionError('Tenant document listing was not isolated')
    for _ in range(60):
        status, _, other_state = oidc.request(other, oidc.APP + '/api/intake/status?id=' + other_document)
        if status == 200 and json.loads(other_state)['state'] == 'completed':
            break
        time.sleep(1)
    else:
        raise AssertionError('Tenant B synthetic worker did not complete document')
    # Each tenant may promote and review its own quotation, but cannot use
    # the other tenant's IDs for either operation.
    status, _, _ = oidc.request(other, oidc.APP + '/api/intake/promote',
                                data=json.dumps({'id':document_id}).encode(), headers=other_headers)
    if status != 400:
        raise AssertionError('Cross-tenant promotion was not denied')
    status, _, _ = oidc.request(other, oidc.APP + '/api/review/show',
                                data=json.dumps({'id':quotation_id}).encode(), headers=other_headers)
    if status != 400:
        raise AssertionError('Cross-tenant review access was not denied')
    status, _, listing = oidc.request(other, oidc.APP + '/api/quotations')
    if status != 200 or quotation_id in {item['id'] for item in json.loads(listing)['quotations']}:
        raise AssertionError('Cross-tenant quotation was listed')
    status, _, result = oidc.request(other, oidc.APP + '/api/intake/promote',
                                     data=json.dumps({'id':other_document}).encode(), headers=other_headers)
    if status != 200 or not json.loads(result)['created']:
        raise AssertionError('Tenant B own promotion failed')
    other_quotation = json.loads(result)['id']
    status, _, _ = oidc.request(other, oidc.APP + '/api/review/start',
                                data=json.dumps({'id':other_quotation}).encode(), headers=other_headers)
    if status != 200:
        raise AssertionError('Tenant B own review could not start')
    status, _, _ = oidc.request(other, oidc.APP + '/api/review/show',
                                data=json.dumps({'id':other_quotation}).encode(), headers=other_headers)
    if status != 200:
        raise AssertionError('Tenant B own review could not be read')
    correction = {'id':quotation_id, 'field':'total', 'value':'1050',
                  'reason':'Synthetic format correction', 'source':'synthetic.txt',
                  'line':6, 'quote':'Grand Total: 1050.00', 'expected_revision':1}
    status, _, _ = oidc.request(other, oidc.APP + '/api/review/correct',
                                data=json.dumps(correction).encode(), headers=other_headers)
    if status != 400:
        raise AssertionError('Cross-tenant review change was not denied')
    own_correction = dict(correction, id=other_quotation)
    status, _, result = oidc.request(other, oidc.APP + '/api/review/correct',
                                     data=json.dumps(own_correction).encode(), headers=other_headers)
    if status != 200 or json.loads(result)['revision'] != 2:
        raise AssertionError('Tenant B own correction failed')
    oidc.APP, oidc.ORIGIN = app_a, origin_a
    status, _, _ = oidc.request(analyst, oidc.APP + '/api/intake/status?id=' + other_document)
    if status != 404:
        raise AssertionError('Reverse cross-tenant document access was not denied')
    for endpoint, foreign_id in (('/api/intake/promote', other_document), ('/api/review/show', other_quotation)):
        status, _, _ = oidc.request(analyst, oidc.APP + endpoint,
                                    data=json.dumps({'id':foreign_id}).encode(), headers=headers)
        if status != 400:
            raise AssertionError('Reverse cross-tenant operation was not denied')
    status, _, _ = oidc.request(analyst, oidc.APP + '/api/review/correct',
        data=json.dumps(dict(correction, id=other_quotation, expected_revision=2)).encode(), headers=headers)
    if status != 400:
        raise AssertionError('Reverse cross-tenant review change was not denied')
    status, _, result = oidc.request(analyst, oidc.APP + '/api/review/show',
                                     data=json.dumps({'id':quotation_id}).encode(), headers=headers)
    if status != 200 or json.loads(result)['revision'] != 1:
        raise AssertionError('Tenant A review changed unexpectedly')
    oidc.APP, oidc.ORIGIN = 'http://127.0.0.1:8766', 'http://127.0.0.1:19766'
    status, _, result = oidc.request(other, oidc.APP + '/api/review/show',
                                     data=json.dumps({'id':other_quotation}).encode(), headers=other_headers)
    if status != 200 or json.loads(result)['revision'] != 2:
        raise AssertionError('Tenant B review changed unexpectedly')
    oidc.APP, oidc.ORIGIN = app_a, origin_a

    review_jar, reviewer = oidc.login(users['reviewer'])
    review_csrf = next(cookie.value for cookie in review_jar if cookie.name == 'nawa_csrf')
    status, _, _ = oidc.request(reviewer, oidc.APP + '/api/intake/submit', data=body,
                                headers={**headers, 'X-NAWA-CSRF':review_csrf})
    if status != 403:
        raise AssertionError('Reviewer upload was not denied')
    invalid = ('unstructured synthetic quotation ' + uuid4().hex).encode()
    invalid_body = json.dumps({'filename':'invalid-quote.txt',
                               'content_base64':base64.b64encode(invalid).decode()}).encode()
    status, _, failed_result = oidc.request(analyst, oidc.APP + '/api/intake/submit', data=invalid_body, headers=headers)
    if status != 200:
        raise AssertionError('Synthetic failed-job fixture could not be submitted')
    failed_id = json.loads(failed_result)['id']
    for _ in range(60):
        status, _, failed_state = oidc.request(analyst, oidc.APP + '/api/intake/status?id=' + failed_id)
        if status == 200 and json.loads(failed_state)['state'] == 'failed':
            break
        time.sleep(1)
    else:
        raise AssertionError('Synthetic invalid document did not reach failed state')
    oidc.APP, oidc.ORIGIN = 'http://127.0.0.1:8766', 'http://127.0.0.1:19766'
    status, _, result = oidc.request(other, oidc.APP + '/api/intake/submit', data=invalid_body, headers=other_headers)
    if status != 200:
        raise AssertionError('Tenant B failed-job fixture could not be submitted')
    other_failed = json.loads(result)['id']
    for _ in range(60):
        status, _, result = oidc.request(other, oidc.APP + '/api/intake/status?id=' + other_failed)
        if status == 200 and json.loads(result)['state'] == 'failed':
            break
        time.sleep(1)
    else:
        raise AssertionError('Tenant B invalid document did not reach failed state')
    status, _, _ = oidc.request(other, oidc.APP + '/api/intake/retry',
        data=json.dumps({'id':failed_id, 'reason':'Synthetic QA retry'}).encode(), headers=other_headers)
    if status != 400:
        raise AssertionError('Cross-tenant retry was not denied')
    oidc.APP, oidc.ORIGIN = app_a, origin_a
    status, _, _ = oidc.request(analyst, oidc.APP + '/api/intake/retry',
        data=json.dumps({'id':other_failed, 'reason':'Synthetic QA retry'}).encode(), headers=headers)
    if status != 400:
        raise AssertionError('Reverse cross-tenant retry was not denied')
    status, _, result = oidc.request(analyst, oidc.APP + '/api/intake/status?id=' + failed_id)
    if status != 200 or json.loads(result)['state'] != 'failed':
        raise AssertionError('Tenant A job changed unexpectedly')
    oidc.APP, oidc.ORIGIN = 'http://127.0.0.1:8766', 'http://127.0.0.1:19766'
    status, _, result = oidc.request(other, oidc.APP + '/api/intake/status?id=' + other_failed)
    if status != 200 or json.loads(result)['state'] != 'failed':
        raise AssertionError('Tenant B job changed unexpectedly')
    status, _, result = oidc.request(other, oidc.APP + '/api/intake/retry',
        data=json.dumps({'id':other_failed, 'reason':'Synthetic QA retry'}).encode(), headers=other_headers)
    if status != 200 or json.loads(result)['state'] != 'validated':
        raise AssertionError('Tenant B own retry failed')
    oidc.APP, oidc.ORIGIN = app_a, origin_a
    retry_body = json.dumps({'id':failed_id, 'reason':'Synthetic QA retry'}).encode()
    status, _, _ = oidc.request(reviewer, oidc.APP + '/api/intake/retry', data=retry_body,
                                headers={**headers, 'X-NAWA-CSRF':review_csrf})
    if status != 403:
        raise AssertionError('Reviewer retry was not denied')
    status, _, retry_result = oidc.request(analyst, oidc.APP + '/api/intake/retry', data=retry_body, headers=headers)
    if status != 200 or json.loads(retry_result)['state'] != 'validated':
        raise AssertionError('Analyst retry failed')
    identity = json.dumps([oidc.ISSUER, users['analyst']['subject']], ensure_ascii=False, separators=(',', ':'))
    actor_hash = hashlib.sha256(identity.encode()).hexdigest()
    with psycopg.connect(host=os.environ['POSTGRES_HOST'], dbname=os.environ['POSTGRES_DB'],
                         user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'],
                         connect_timeout=5) as conn:
        row = conn.execute('''SELECT action,actor_hash,reason FROM nawa_intake_admin_events
            WHERE tenant_id=%s AND document_id=%s ORDER BY id DESC LIMIT 1''', ('tenant-a', failed_id)).fetchone()
    if row != ('retry', actor_hash, 'Synthetic QA retry'):
        raise AssertionError('Synthetic retry event was not recorded')
    decision = json.dumps({'id':quotation_id, 'decision':'approve',
                           'reason':'Synthetic QA review', 'expected_revision':1}).encode()
    status, _, review_body = oidc.request(reviewer, oidc.APP + '/api/review/decide', data=decision,
                                          headers={**headers, 'X-NAWA-CSRF':review_csrf})
    if status != 200 or json.loads(review_body)['status'] != 'approved' or json.loads(review_body)['revision'] != 2:
        raise AssertionError('Synthetic reviewer decision failed')
    print('Synthetic Phase 13 QA passed: login, intake, worker, review, retry, CSRF, role and cross-tenant denial.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Synthetic Phase 13 QA failed:', type(exc).__name__, '(details withheld).')
        raise SystemExit(1) from None


