"""CI-only browser-driven OIDC test for a disposable synthetic identity stack.

The CI runner trusts its freshly generated test CA in an ephemeral NSS store.
No user-system trust store, real identity provider or real records are used.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from playwright.sync_api import Error, sync_playwright

APP = 'http://127.0.0.1:19765'
ISSUER = 'https://localhost:19443/realms/nawa-phase13'


def require_ci():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('NAWA_PHASE13_CI_QA') != '1':
        raise ValueError('Only the disposable GitHub Phase 13 QA stack is supported')


def login(browser, user, *, app=APP, tenant='tenant-a', role=None):
    context = browser.new_context(ignore_https_errors=False)
    page = context.new_page()
    if context.cookies() or page.request.get(app + '/auth/me').status != 403:
        raise AssertionError('A new browser context must start without a session')
    page.goto(app + '/auth/login', wait_until='domcontentloaded', timeout=30000)
    identity = urlparse(page.url)
    issuer = urlparse(ISSUER)
    if (identity.scheme != 'https' or identity.netloc != issuer.netloc or
            not identity.path.startswith(issuer.path + '/') or
            parse_qs(identity.query).get('code_challenge_method') != ['S256']):
        raise AssertionError('Unexpected identity endpoint or PKCE method')
    page.locator('#username').fill(user['username'])
    page.locator('#password').fill(user['password'])
    page.locator('#kc-login').click()
    page.wait_for_url(app + '/**', timeout=30000)
    identity_response = page.request.get(app + '/auth/me')
    if identity_response.status != 200 or identity_response.json() != {'tenant':tenant, 'role':role or user['role']}:
        raise AssertionError('Synthetic browser identity did not match expected membership')
    return context, page


def browser_api(page, path, payload=None):
    """Make same-origin requests from the authenticated browser page."""
    return page.evaluate('''async ({path, payload}) => {
        const csrf = document.cookie.split('; ').find(x => x.startsWith('nawa_csrf='));
        const options = payload === null ? {} : {method:'POST',
            headers:{'Content-Type':'application/json', 'X-NAWA-CSRF':csrf ? decodeURIComponent(csrf.split('=')[1]) : ''},
            body:JSON.stringify(payload)};
        const response = await fetch(path, options);
        return {status:response.status, body:await response.json()};
    }''', {'path':path, 'payload':payload})


def set_synthetic_membership(subject, active, *, component='membership'):
    """Change only disposable CI fixture state, never a real user."""
    require_ci()
    if component not in ('membership', 'identity'):
        raise ValueError('Invalid synthetic fixture component')
    code = '''import os, sys, psycopg
if (os.environ.get('NAWA_INTAKE') != 'synthetic' or os.environ.get('NAWA_AUTH') != 'oidc'
        or not os.environ.get('POSTGRES_DB','').startswith('nawa_phase13_qa_')):
    raise SystemExit(1)
with psycopg.connect(host=os.environ['POSTGRES_HOST'], dbname=os.environ['POSTGRES_DB'],
        user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'], connect_timeout=5) as conn:
    if sys.argv[4] == 'membership':
        result = conn.execute('UPDATE nawa_memberships SET active=%s WHERE issuer=%s AND subject=%s AND tenant_id=%s',
                              (sys.argv[2] == 'active', sys.argv[3], sys.argv[1], 'tenant-a'))
    elif sys.argv[4] == 'identity':
        result = conn.execute('UPDATE nawa_identities SET active=%s WHERE issuer=%s AND subject=%s',
                              (sys.argv[2] == 'active', sys.argv[3], sys.argv[1]))
    else:
        raise ValueError('Invalid synthetic fixture component')
    if result.rowcount != 1:
        raise ValueError('Synthetic membership unavailable')
'''
    subprocess.run(['docker', 'compose', '-p', 'nawa-phase13-ci', '-f', 'compose.phase13-qa.yaml',
                    'exec', '-T', 'nawa', 'python', '-c', code, subject,
                    'active' if active else 'inactive', ISSUER, component],
                   check=True, capture_output=True, timeout=30)


def set_ci_evidence_grant(document_id, reviewer_subject, target_subject, enabled):
    """Manage only the disposable browser fixture; no grant endpoint exists."""
    require_ci()
    code = '''import json, os, sys, psycopg
from nawa_demo.identity_repository import membership_by_key
from nawa_demo.intake_store import LocalIntakeStore
from nawa_demo.synthetic_postgres_passages import SyntheticPostgresPassages
if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('NAWA_SYNTHETIC_EVIDENCE') != 'ci'
        or os.environ.get('POSTGRES_DB') != 'nawa_phase13_qa_demo' or os.environ.get('NAWA_INTAKE_DIR') != '/intake'):
    raise SystemExit(1)
def connect():
    return psycopg.connect(host=os.environ['POSTGRES_HOST'], dbname=os.environ['POSTGRES_DB'],
        user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'], connect_timeout=5)
reviewer = json.dumps([sys.argv[1],sys.argv[3]], separators=(',',':'))
target = json.dumps([sys.argv[1],sys.argv[4]], separators=(',',':'))
with connect() as conn:
    principal = membership_by_key(conn, identity_key=reviewer, tenant='tenant-a')
source = SyntheticPostgresPassages(connect, LocalIntakeStore('/intake'))
if not source.set_access(principal, sys.argv[2], target, sys.argv[5] == 'grant', 'synthetic browser fixture'):
    raise ValueError('Synthetic grant was not changed')
'''
    subprocess.run(['docker', 'compose', '-p', 'nawa-phase13-ci', '-f', 'compose.phase13-qa.yaml',
                    'exec', '-T', 'nawa', 'python', '-c', code, ISSUER, document_id,
                    reviewer_subject, target_subject, 'grant' if enabled else 'revoke'],
                   check=True, capture_output=True, timeout=30)


def browser_tenant_isolation(browser, analyst_page, reviewer_page, reviewer_user, other_user):
    """Check two independently authenticated synthetic browser contexts."""
    other, other_page = login(browser, other_user, app='http://127.0.0.1:19766', tenant='tenant-b', role='analyst')
    try:
        pages = (analyst_page, other_page)
        ids = []
        for page in pages:
            result = browser_api(page, '/api/load-sample', {'name':'english_quote.txt'})
            if result['status'] != 200 or not isinstance(result['body'].get('id'), str):
                raise AssertionError('Browser tenant positive-control load failed')
            item_id = result['body']['id']
            if browser_api(page, '/api/review/start', {'id':item_id})['status'] != 200:
                raise AssertionError('Browser tenant positive-control review failed')
            ids.append(item_id)
        if ids[0] == ids[1]:
            raise AssertionError('Synthetic tenant quotations share an ID')
        for index, page in enumerate(pages):
            own, foreign = ids[index], ids[1-index]
            listing = browser_api(page, '/api/quotations')
            if listing['status'] != 200:
                raise AssertionError('Browser tenant quotation listing failed')
            listed = {item['id'] for item in listing['body']['quotations']}
            if own not in listed or foreign in listed:
                raise AssertionError('Browser cross-tenant quotation listing was not isolated')
            if browser_api(page, '/api/review/show', {'id':foreign})['status'] != 400:
                raise AssertionError('Browser cross-tenant review read was not denied')
        for index, page in enumerate(pages):
            result = browser_api(page, '/api/review/show', {'id':ids[index]})
            if result['status'] != 200 or result['body'].get('revision') != 1:
                raise AssertionError('Browser own-tenant review changed unexpectedly')
        # Give both browser sessions a completed, retained source so the
        # correction payload is valid when used by its own tenant.
        sources, reviews = [], []
        for index, page in enumerate(pages):
            name = f'browser-tenant-{index}-{uuid4().hex}.txt'
            sample = Path('demo/samples/english_quote.txt').read_bytes()
            sample += ('\n# browser-tenant-' + uuid4().hex + '\n').encode()
            result = browser_api(page, '/api/intake/submit',
                                 {'filename':name, 'content_base64':base64.b64encode(sample).decode('ascii')})
            if result['status'] != 200:
                raise AssertionError('Browser tenant synthetic upload failed')
            document = result['body']['id']
            for _ in range(120):
                result = browser_api(page, '/api/intake/status?id=' + document)
                if result['status'] == 200 and result['body'].get('state') == 'completed':
                    break
                page.wait_for_timeout(500)
            else:
                raise AssertionError('Browser tenant synthetic worker did not complete')
            result = browser_api(page, '/api/intake/promote', {'id':document})
            if result['status'] != 200 or not result['body'].get('created'):
                raise AssertionError('Browser tenant own promotion failed')
            review_id = result['body']['id']
            if browser_api(page, '/api/review/start', {'id':review_id})['status'] != 200:
                raise AssertionError('Browser tenant own intake review failed')
            sources.append(name)
            reviews.append(review_id)
        for index, page in enumerate(pages):
            other_index = 1 - index
            correction = {'id':reviews[other_index], 'field':'total', 'value':'1050',
                          'reason':'Synthetic format correction', 'source':sources[other_index],
                          'line':6, 'quote':'Grand Total: 1050.00', 'expected_revision':1}
            if browser_api(page, '/api/review/correct', correction)['status'] != 400:
                raise AssertionError('Browser cross-tenant correction was not denied')
        for index, page in enumerate(pages):
            result = browser_api(page, '/api/review/show', {'id':reviews[index]})
            if result['status'] != 200 or result['body'].get('revision') != 1:
                raise AssertionError('Browser tenant review changed after denial')
            correction = {'id':reviews[index], 'field':'total', 'value':'1050',
                          'reason':'Synthetic format correction', 'source':sources[index],
                          'line':6, 'quote':'Grand Total: 1050.00', 'expected_revision':1}
            result = browser_api(page, '/api/review/correct', correction)
            if result['status'] != 200 or result['body'].get('revision') != 2:
                raise AssertionError('Browser tenant own correction failed')
        for index, page in enumerate(pages):
            own = browser_api(page, '/api/compare', {'ids':[ids[index], reviews[index]]})
            if own['status'] != 200 or {q['id'] for q in own['body']['quotations']} != {ids[index], reviews[index]}:
                raise AssertionError('Browser own-tenant comparison failed')
            foreign = browser_api(page, '/api/compare', {'ids':[ids[index], reviews[1-index]]})
            if foreign['status'] != 400 or foreign['body'] != {'error':'Invalid or unavailable request'}:
                raise AssertionError('Browser cross-tenant comparison was not safely denied')
        for index, page in enumerate(pages):
            decision = {'id':reviews[index], 'decision':'approve',
                        'reason':'Synthetic reviewer decision', 'expected_revision':2}
            if browser_api(page, '/api/review/decide', decision)['status'] != 403:
                raise AssertionError('Browser analyst approval was not denied')
        decision = {'id':reviews[1], 'decision':'approve',
                    'reason':'Synthetic reviewer decision', 'expected_revision':2}
        if browser_api(reviewer_page, '/api/review/decide', decision)['status'] != 400:
            raise AssertionError('Browser cross-tenant reviewer approval was not denied')
        decision['id'] = reviews[0]
        result = browser_api(reviewer_page, '/api/review/decide', decision)
        if result['status'] != 200 or result['body'].get('status') != 'approved' or result['body'].get('revision') != 3:
            raise AssertionError('Browser own-tenant reviewer approval failed')
        result = browser_api(other_page, '/api/review/show', {'id':reviews[1]})
        if result['status'] != 200 or result['body'].get('status') != 'pending_review' or result['body'].get('revision') != 2:
            raise AssertionError('Browser other-tenant review changed unexpectedly')
        # Change the disposable membership twice without an intervening
        # request from the reviewer. This does not test provider revocation.
        try:
            set_synthetic_membership(reviewer_user['subject'], False)
        finally:
            set_synthetic_membership(reviewer_user['subject'], True)
        if browser_api(reviewer_page, '/api/quotations')['status'] != 403:
            raise AssertionError('Browser session survived an unseen membership change')
        if any(browser_api(page, '/api/quotations')['status'] != 200 for page in pages):
            raise AssertionError('Browser unrelated analyst session was affected')
        fresh_reviewer, fresh_page = login(browser, reviewer_user)
        if browser_api(fresh_page, '/api/quotations')['status'] != 200:
            fresh_reviewer.close()
            raise AssertionError('Browser fresh synthetic session did not recover')
        # The local identity flag changes without an intervening request.
        # This is not an identity-provider account disablement test.
        try:
            set_synthetic_membership(reviewer_user['subject'], False, component='identity')
        finally:
            set_synthetic_membership(reviewer_user['subject'], True, component='identity')
        if browser_api(fresh_page, '/api/quotations')['status'] != 403:
            raise AssertionError('Browser session survived an unseen local identity change')
        newest_reviewer, newest_page = login(browser, reviewer_user)
        if browser_api(newest_page, '/api/quotations')['status'] != 200:
            newest_reviewer.close()
            raise AssertionError('Browser identity reauthentication failed')
        fresh_reviewer.close()
        reviewer_page.context.close()
        return newest_reviewer, newest_page
    finally:
        other.close()


def expect_untrusted(browser):
    context = browser.new_context(ignore_https_errors=False)
    try:
        page = context.new_page()
        try:
            page.goto(ISSUER + '/.well-known/openid-configuration', timeout=30000)
        except Error as error:
            if 'ERR_CERT_AUTHORITY_INVALID' in str(error):
                return
            raise AssertionError('Unexpected browser TLS failure') from None
        raise AssertionError('Browser unexpectedly trusted the new synthetic CA')
    finally:
        context.close()


def synthetic_review_journey(analyst_page, reviewer_page):
    """Upload, correct and approve one synthetic quote through the browser UI."""
    sample = Path('demo/samples/english_quote.txt').read_bytes()
    if b'Grand Total: 1050.00' not in sample:
        raise AssertionError('Synthetic quotation fixture changed')
    sample = sample.replace(b'Grand Total: 1050.00', b'Grand Total: 1051.00')
    sample += ('\n# browser-oidc-' + uuid4().hex + '\n').encode()
    filename = 'browser-oidc-review.txt'
    analyst_page.locator('#intakePanel').wait_for(state='visible', timeout=15000)
    analyst_page.locator('#intakeFiles').set_input_files({'name':filename, 'mimeType':'text/plain', 'buffer':sample})
    analyst_page.locator('#intakeSubmit').click()
    result = analyst_page.locator('#intakeResults .row').first
    result.wait_for(state='visible', timeout=15000)
    match = re.search(r'ID: ([0-9a-f]{32})', result.inner_text())
    if not match:
        raise AssertionError('Synthetic browser intake did not return a document ID')
    document_id = match.group(1)
    analyst_page.locator('#intakeRefresh').click()
    row = analyst_page.locator('#intakeJobs .row').filter(has_text=document_id)
    row.wait_for(state='visible', timeout=15000)
    row.get_by_role('button', name='Use document').click()
    for _ in range(60):
        analyst_page.locator('#intakeCheck').click()
        analyst_page.wait_for_timeout(500)
        if 'completed' in analyst_page.locator('#intakeStatus').inner_text():
            break
        time.sleep(0.5)
    else:
        raise AssertionError('Synthetic browser intake did not complete')
    analyst_page.locator('#intakePromote').click()
    analyst_page.locator('#items').get_by_text(filename).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review').get_by_role('button', name='Start review').click(timeout=15000)
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 1').wait_for(state='visible', timeout=15000)
    analyst_page.locator('#details').get_by_text('Line 6: Grand Total: 1051.00').wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review select').select_option('total')
    analyst_page.get_by_placeholder('Corrected value').fill('1050.00')
    analyst_page.get_by_placeholder('Reason for correction').fill('Synthetic arithmetic correction')
    analyst_page.get_by_placeholder('Evidence line number').fill('3')
    analyst_page.get_by_placeholder('Verbatim excerpt from source (reviewer assertion)').fill('Forged synthetic source line')
    analyst_page.locator('#review').get_by_role('button', name='Record correction').click()
    analyst_page.get_by_text('Invalid or unavailable request', exact=True).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 1').wait_for(state='visible', timeout=15000)
    analyst_page.get_by_placeholder('Verbatim excerpt from source (reviewer assertion)').fill('Subtotal: 1000.00')
    analyst_page.locator('#review').get_by_role('button', name='Record correction').click()
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 2').wait_for(state='visible', timeout=15000)
    reviewer_page.reload(wait_until='domcontentloaded')
    reviewer_page.locator('#items').get_by_text(filename).click(timeout=15000)
    reviewer_page.locator('#review').get_by_text('Status: pending_review · revision 2').wait_for(state='visible', timeout=15000)
    reviewer_page.get_by_placeholder('Explain your decision').fill('Synthetic reviewer checked correction')
    reviewer_page.locator('#review').get_by_role('button', name='Approve', exact=True).click()
    reviewer_page.locator('#review').get_by_text('Status: approved · revision 3').wait_for(state='visible', timeout=15000)
    reviewer_page.reload(wait_until='domcontentloaded')
    reviewer_page.locator('#items').get_by_text(filename).click(timeout=15000)
    reviewer_page.locator('#review').get_by_text('Status: approved · revision 3').wait_for(state='visible', timeout=15000)
    # Compare the approved correction with a second synthetic quotation.
    analyst_page.locator('#sample').select_option('arabic_quote.txt')
    analyst_page.locator('#loadSample').click()
    analyst_page.get_by_role('checkbox', name='Compare arabic_quote.txt').wait_for(state='visible', timeout=15000)
    analyst_page.get_by_role('checkbox', name='Compare ' + filename).check()
    analyst_page.get_by_role('checkbox', name='Compare arabic_quote.txt').check()
    analyst_page.locator('#compareBtn').click()
    analyst_page.locator('#comparison').get_by_text(
        'Example English Supplies — AED 1050.00 — Valid · approved review revision 3', exact=True
    ).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#comparison').get_by_text(
        'Lowest valid total: شركة المثال للتوريدات', exact=True
    ).wait_for(state='visible', timeout=15000)


def synthetic_arabic_text_journey(analyst_page, reviewer_page):
    """Review a retained synthetic Arabic TXT source and compare approvals."""
    sample = Path('demo/samples/arabic_quote.txt').read_text(encoding='utf-8')
    original = 'الإجمالي شامل الضريبة: ٩٤٥٫٠٠'
    if original not in sample:
        raise AssertionError('Synthetic Arabic quotation fixture changed')
    sample = sample.replace(original, 'الإجمالي شامل الضريبة: ٩٤٦٫٠٠')
    sample += '\n# browser-arabic-' + uuid4().hex + '\n'
    filename = 'browser-arabic-reviewed.txt'
    analyst_page.locator('#intakeFiles').set_input_files(
        {'name':filename, 'mimeType':'text/plain', 'buffer':sample.encode('utf-8')})
    analyst_page.locator('#intakeSubmit').click()
    result = analyst_page.locator('#intakeResults .row').filter(has_text=filename).first
    result.wait_for(state='visible', timeout=15000)
    match = re.search(r'ID: ([0-9a-f]{32})', result.inner_text())
    if not match:
        raise AssertionError('Arabic browser intake did not return a document ID')
    document_id = match.group(1)
    analyst_page.locator('#intakeRefresh').click()
    row = analyst_page.locator('#intakeJobs .row').filter(has_text=document_id)
    row.wait_for(state='visible', timeout=15000)
    row.get_by_role('button', name='Use document').click()
    for _ in range(60):
        analyst_page.locator('#intakeCheck').click()
        analyst_page.wait_for_timeout(500)
        if 'completed' in analyst_page.locator('#intakeStatus').inner_text():
            break
        time.sleep(0.5)
    else:
        raise AssertionError('Arabic browser intake did not complete')
    analyst_page.locator('#intakePromote').click()
    analyst_page.locator('#items').get_by_text(filename).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review').get_by_role('button', name='Start review').click(timeout=15000)
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 1').wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review select').select_option('total')
    analyst_page.get_by_placeholder('Corrected value').fill('945.00')
    analyst_page.get_by_placeholder('Reason for correction').fill('Synthetic arithmetic correction')
    analyst_page.get_by_placeholder('Evidence line number').fill('3')
    analyst_page.get_by_placeholder('Verbatim excerpt from source (reviewer assertion)').fill('نص عربي غير موجود')
    analyst_page.locator('#review').get_by_role('button', name='Record correction').click()
    analyst_page.get_by_text('Invalid or unavailable request', exact=True).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 1').wait_for(state='visible', timeout=15000)
    analyst_page.get_by_placeholder('Verbatim excerpt from source (reviewer assertion)').fill('المجموع الفرعي: ٩٠٠٫٠٠')
    analyst_page.locator('#review').get_by_role('button', name='Record correction').click()
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 2').wait_for(state='visible', timeout=15000)
    reviewer_page.reload(wait_until='domcontentloaded')
    reviewer_page.locator('#items').get_by_text(filename).click(timeout=15000)
    reviewer_page.locator('#review').get_by_text('Status: pending_review · revision 2').wait_for(state='visible', timeout=15000)
    reviewer_page.get_by_placeholder('Explain your decision').fill('Synthetic reviewer checked Arabic correction')
    reviewer_page.locator('#review').get_by_role('button', name='Approve', exact=True).click()
    reviewer_page.locator('#review').get_by_text('Status: approved · revision 3').wait_for(state='visible', timeout=15000)
    analyst_page.locator('#items').get_by_role('checkbox', name='Compare ' + filename).check()
    analyst_page.locator('#items').get_by_role('checkbox', name='Compare browser-oidc-review.txt').check()
    analyst_page.locator('#compareBtn').click()
    analyst_page.locator('#comparison').get_by_text(
        'شركة المثال للتوريدات — AED 945.00 — Valid · approved review revision 3', exact=True
    ).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#comparison').get_by_text(
        'Example English Supplies — AED 1050.00 — Valid · approved review revision 3', exact=True
    ).wait_for(state='visible', timeout=15000)
    return document_id


def synthetic_evidence_journey(browser, analyst_page, reviewer_page, users, document_id):
    """Verify synthetic evidence with actual OIDC browser sessions."""
    result = browser_api(analyst_page, '/api/intake/version', {'id':document_id, 'expected_previous':0})
    if result['status'] != 200 or result['body'].get('version') != 1:
        raise AssertionError('Synthetic evidence version setup failed')
    search = '/api/synthetic-evidence/search'
    lookup = '/api/synthetic-evidence/source'
    review_handoff = '/api/synthetic-evidence/review'
    passage_id = document_id + ':v1'
    if browser_api(analyst_page, search, {'q':'المجموع'}) != {'status':200, 'body':{'passages':[]}}:
        raise AssertionError('Synthetic evidence was available without a grant')
    result = browser_api(analyst_page, review_handoff, {'q':'المجموع'})
    if result['status'] != 200 or result['body'].get('evidence') or result['body'].get('answer') is not None:
        raise AssertionError('Ungranted review handoff was not empty')
    set_ci_evidence_grant(document_id, users['reviewer']['subject'], users['analyst']['subject'], True)
    result = browser_api(analyst_page, search, {'q':'المجموع'})
    if (result['status'] != 200 or len(result['body'].get('passages', [])) != 1
            or result['body']['passages'][0].get('document_id') != document_id):
        raise AssertionError('Granted synthetic evidence search failed')
    result = browser_api(analyst_page, lookup, {'id':passage_id})
    if result['status'] != 200 or 'المجموع الفرعي' not in result['body'].get('passage', {}).get('text', ''):
        raise AssertionError('Granted synthetic source lookup failed')
    result = browser_api(analyst_page, review_handoff, {'q':'المجموع'})
    if (result['status'] != 200 or result['body'].get('status') != 'requires_human_review'
            or result['body'].get('answer') is not None or result['body'].get('citations') != []
            or len(result['body'].get('evidence', [])) != 1
            or result['body']['evidence'][0].get('document_id') != document_id
            or 'المجموع الفرعي' not in result['body']['evidence'][0].get('untrusted_source_text', '')):
        raise AssertionError('Synthetic review handoff failed')
    if browser_api(reviewer_page, review_handoff, {'q':'المجموع'})['body'].get('evidence'):
        raise AssertionError('Ungranted reviewer received handoff evidence')
    if browser_api(reviewer_page, lookup, {'id':passage_id})['status'] != 503:
        raise AssertionError('Ungranted same-tenant reviewer accessed evidence')
    create, show = '/api/synthetic-review/create', '/api/synthetic-review/show'
    request = {'request_key':uuid4().hex, 'question':'Review synthetic Arabic quotation', 'passage_id':passage_id}
    first = browser_api(analyst_page, create, request)
    if (first['status'] != 200 or first['body'].get('status') != 'pending_review'
            or first['body'].get('created') is not True or first['body'].get('answer') is not None):
        raise AssertionError('Synthetic review request was not created')
    task_id = first['body'].get('id')
    retry = browser_api(analyst_page, create, request)
    if retry['status'] != 200 or retry['body'].get('id') != task_id or retry['body'].get('created') is not False:
        raise AssertionError('Synthetic review request retry was not idempotent')
    if browser_api(reviewer_page, create, request)['status'] != 403:
        raise AssertionError('Reviewer was able to create an analyst request')
    if browser_api(reviewer_page, show, {'id':task_id})['status'] != 400:
        raise AssertionError('Ungranted reviewer could open the request')
    set_ci_evidence_grant(document_id, users['reviewer']['subject'], users['reviewer']['subject'], True)
    if browser_api(reviewer_page, show, {'id':task_id})['status'] != 200:
        raise AssertionError('Granted reviewer could not open the request')
    set_ci_evidence_grant(document_id, users['reviewer']['subject'], users['reviewer']['subject'], False)
    if browser_api(reviewer_page, show, {'id':task_id})['status'] != 400:
        raise AssertionError('Revoked reviewer could still open the request')
    other, other_page = login(browser, users['outsider'], app='http://127.0.0.1:19766', tenant='tenant-b', role='analyst')
    try:
        if browser_api(other_page, lookup, {'id':passage_id})['status'] != 503:
            raise AssertionError('Cross-tenant browser evidence access was not denied')
        if browser_api(other_page, review_handoff, {'q':'المجموع'})['body'].get('evidence'):
            raise AssertionError('Cross-tenant review handoff leaked evidence')
        if browser_api(other_page, show, {'id':task_id})['status'] != 400:
            raise AssertionError('Cross-tenant review request was accessible')
    finally:
        other.close()
    status = analyst_page.evaluate('''async () => {
        const r = await fetch('/api/synthetic-evidence/search', {method:'POST',
            headers:{'Content-Type':'application/json'}, body:JSON.stringify({q:'المجموع'})});
        return r.status;
    }''')
    if status != 403:
        raise AssertionError('Evidence request without CSRF was not denied')
    set_ci_evidence_grant(document_id, users['reviewer']['subject'], users['analyst']['subject'], False)
    if browser_api(analyst_page, lookup, {'id':passage_id})['status'] != 503:
        raise AssertionError('Revoked synthetic evidence grant still allowed access')
    if browser_api(analyst_page, review_handoff, {'q':'المجموع'})['body'].get('evidence'):
        raise AssertionError('Revoked synthetic review handoff still returned evidence')
    if browser_api(analyst_page, show, {'id':task_id})['status'] != 400:
        raise AssertionError('Request owner retained access after evidence revocation')
    print('PASS: synthetic browser review request retry, role, tenant and revocation checks.')
    print('PASS: synthetic OIDC browser evidence grant, tenant denial, CSRF and revocation checks.')


def synthetic_pdf_review_journey(analyst_page, reviewer_page, *, filename, global_line, page, sample_name=None, sample_bytes=None):
    """Exercise a synthetic PDF source check in the browser."""
    sample = sample_bytes if sample_bytes is not None else (Path('demo/samples/pdf') / sample_name).read_bytes()
    analyst_page.locator('#intakeFiles').set_input_files({'name':filename, 'mimeType':'application/pdf', 'buffer':sample})
    analyst_page.locator('#intakeSubmit').click()
    result = analyst_page.locator('#intakeResults .row').filter(has_text=filename).first
    result.wait_for(state='visible', timeout=15000)
    match = re.search(r'ID: ([0-9a-f]{32})', result.inner_text())
    if not match:
        raise AssertionError('Synthetic PDF intake did not return a document ID')
    document_id = match.group(1)
    analyst_page.locator('#intakeRefresh').click()
    row = analyst_page.locator('#intakeJobs .row').filter(has_text=document_id)
    row.wait_for(state='visible', timeout=15000)
    row.get_by_role('button', name='Use document').click()
    for _ in range(60):
        analyst_page.locator('#intakeCheck').click()
        analyst_page.wait_for_timeout(500)
        if 'completed' in analyst_page.locator('#intakeStatus').inner_text():
            break
        time.sleep(0.5)
    else:
        raise AssertionError('Synthetic PDF intake did not complete')
    analyst_page.locator('#intakePromote').click()
    analyst_page.locator('#items').get_by_text(filename).wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review').get_by_role('button', name='Start review').click(timeout=15000)
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 1').wait_for(state='visible', timeout=15000)
    analyst_page.locator('#review select').select_option('total')
    analyst_page.get_by_placeholder('Corrected value').fill('945')
    analyst_page.get_by_placeholder('Reason for correction').fill('Synthetic format correction')
    analyst_page.get_by_placeholder('Evidence line number').fill(str(global_line))
    analyst_page.get_by_placeholder('Verbatim excerpt from source (reviewer assertion)').fill('Grand Total: 945.00')
    analyst_page.locator('#review').get_by_role('button', name='Record correction').click()
    analyst_page.locator('#review').get_by_text('Status: pending_review · revision 2').wait_for(state='visible', timeout=15000)
    analyst_page.get_by_text(f'Correction recorded, revision 2 · retained PDF text matched: page {page}, line 6',
                             exact=True).wait_for(state='visible', timeout=15000)
    reviewer_page.reload(wait_until='domcontentloaded')
    reviewer_page.locator('#items').get_by_text(filename).click(timeout=15000)
    reviewer_page.locator('#review').get_by_text('Status: pending_review · revision 2').wait_for(state='visible', timeout=15000)
    reviewer_page.get_by_placeholder('Explain your decision').fill('Synthetic reviewer checked formatting')
    reviewer_page.locator('#review').get_by_role('button', name='Approve', exact=True).click()
    reviewer_page.locator('#review').get_by_text('Status: approved · revision 3').wait_for(state='visible', timeout=15000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-untrusted', action='store_true')
    args = parser.parse_args()
    require_ci()
    users = json.loads(Path('.data/phase13-identity/fixture.json').read_text(encoding='utf-8'))['users']
    users = {user['role']:user for user in users}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            if args.expect_untrusted:
                expect_untrusted(browser)
                print('PASS: browser rejected the untrusted synthetic identity certificate.')
                return
            analyst, analyst_page = login(browser, users['analyst'])
            reviewer, reviewer_page = login(browser, users['reviewer'])
            try:
                if analyst is reviewer:
                    raise AssertionError('Browser contexts are not isolated')
                # A genuine reviewer session must not gain analyst upload privileges.
                status = reviewer_page.evaluate('''async () => {
                    const csrf = document.cookie.split('; ').find(x => x.startsWith('nawa_csrf='));
                    if (!csrf) return 0;
                    const r = await fetch('/api/intake/submit', {method:'POST',
                        headers:{'Content-Type':'application/json', 'X-NAWA-CSRF':decodeURIComponent(csrf.split('=')[1])},
                        body:JSON.stringify({filename:'synthetic.txt', content_base64:'eA=='})});
                    return r.status;
                }''')
                if status != 403:
                    raise AssertionError('Synthetic reviewer upload permission was not denied')
                synthetic_review_journey(analyst_page, reviewer_page)
                arabic_document = synthetic_arabic_text_journey(analyst_page, reviewer_page)
                synthetic_evidence_journey(browser, analyst_page, reviewer_page, users, arabic_document)
                synthetic_pdf_review_journey(analyst_page, reviewer_page,
                    sample_name='arabic_text_supplier.pdf', filename='browser-synthetic-arabic.pdf', global_line=6, page=1)
                synthetic_pdf_review_journey(analyst_page, reviewer_page,
                    sample_name='second_page_supplier.pdf', filename='browser-synthetic-multipage.pdf', global_line=7, page=2)
                analyst_page.get_by_role('checkbox', name='Compare browser-synthetic-multipage.pdf').check()
                analyst_page.get_by_role('checkbox', name='Compare arabic_quote.txt').check()
                analyst_page.locator('#compareBtn').click()
                analyst_page.locator('#comparison').get_by_text(
                    'Example Page Two Supplies — AED 945 — Valid · approved review revision 3 · retained PDF text matched: page 2, line 6',
                    exact=True).wait_for(state='visible', timeout=15000)
                import pymupdf
                document = pymupdf.open()
                document.new_page().insert_text((50, 60), 'Synthetic cover')
                sheet = document.new_page()
                for index, line in enumerate(('Supplier: Browser New Supplier', 'Currency: AED',
                        'Subtotal: 900.00', 'VAT Rate: 5', 'VAT Amount: 45.00', 'Grand Total: 945.00')):
                    sheet.insert_text((50, 60 + index * 25), line)
                generated_pdf = document.tobytes()
                document.close()
                synthetic_pdf_review_journey(analyst_page, reviewer_page,
                    sample_bytes=generated_pdf, filename='browser-new-synthetic.pdf', global_line=7, page=2)
                analyst_page.get_by_role('checkbox', name='Compare browser-new-synthetic.pdf').check()
                analyst_page.get_by_role('checkbox', name='Compare arabic_quote.txt').check()
                analyst_page.locator('#compareBtn').click()
                analyst_page.locator('#comparison').get_by_text(
                    'Browser New Supplier — AED 945 — Valid · approved review revision 3 · retained PDF text matched: page 2, line 6',
                    exact=True).wait_for(state='visible', timeout=15000)
                reviewer, reviewer_page = browser_tenant_isolation(
                    browser, analyst_page, reviewer_page, users['reviewer'], users['outsider'])
                # Logout invalidates the server-side session, including a replayed cookie.
                old = next(cookie for cookie in analyst.cookies() if cookie['name'] == 'nawa_session')
                status = analyst_page.evaluate('''async () => {
                    const csrf = document.cookie.split('; ').find(x => x.startsWith('nawa_csrf='));
                    if (!csrf) return 0;
                    const r = await fetch('/auth/logout', {method:'POST',
                        headers:{'X-NAWA-CSRF':decodeURIComponent(csrf.split('=')[1])}});
                    return r.status;
                }''')
                if status != 200 or analyst_page.request.get(APP + '/auth/me').status != 403:
                    raise AssertionError('Synthetic browser logout failed')
                replay = browser.new_context(ignore_https_errors=False)
                try:
                    replay.add_cookies([{'name':'nawa_session', 'value':old['value'], 'url':APP, 'httpOnly':True}])
                    if replay.request.get(APP + '/auth/me').status != 403:
                        raise AssertionError('Logged-out synthetic session was reusable')
                finally:
                    replay.close()
                if reviewer_page.request.get(APP + '/auth/me').status != 200:
                    raise AssertionError('Other synthetic browser session was affected')
            finally:
                reviewer.close()
                analyst.close()
        finally:
            browser.close()
    print('PASS: synthetic browser sign-in, two-tenant isolation, reviewer separation and membership revocation, TXT/PDF review, and logout replay checks.')


if __name__ == '__main__':
    try:
        main()
    except AssertionError as exc:
        # Assertion messages above are static and contain no fixture values.
        print('Synthetic browser OIDC check failed:', str(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        print('Synthetic browser OIDC check failed:', type(exc).__name__, '(details withheld).')
        raise SystemExit(1) from None


