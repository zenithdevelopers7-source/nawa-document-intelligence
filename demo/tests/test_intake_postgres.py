"""Runs only against an explicitly configured disposable synthetic database."""
import os
import json
import base64
import hashlib
import threading
import subprocess
import sys
import shutil
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

try:
    import psycopg
except ImportError:
    psycopg = None

from nawa_demo.intake_postgres import IntakeRejected, MAX_BYTES_PER_TENANT, MAX_DOCUMENTS_PER_TENANT, PostgresIntakeJobs
from nawa_demo.intake_validation import MAX_DOCUMENT_BYTES, check_document
from nawa_demo.intake_pipeline import SyntheticIntakePipeline
from nawa_demo.intake_store import LocalIntakeStore
from nawa_demo.intake_worker import process_one
from nawa_demo.authorization import Principal
from nawa_demo.intake_scoped import ScopedIntake
from nawa_demo.dashboard import DashboardState
from nawa_demo.identity_repository import membership_by_key
from nawa_demo.protected_api import make_protected_handler
from nawa_demo.session_draft import SessionStore
from nawa_demo.intake_runtime import build_synthetic_intake
from nawa_demo.intake_migrate import apply_intake_migrations, verify_intake_migrations
from nawa_demo.migrate import migrate
from nawa_demo.intake_migration_inspect import (inspect_history, ORIGINAL_001,
                                                VALIDATED_IN_001, REQUEUE_IN_001)
from nawa_demo.phase13_qa_snapshot import snapshot
from nawa_demo.scoped_state import ScopedState

DSN = os.environ.get('NAWA_PHASE13_TEST_DSN')


def make_principal(connect, identity_key, tenant, role):
    # Integration fixtures must use the same server-resolved generation as a
    # real authenticated session, rather than inventing a Principal locally.
    with connect() as conn:
        result = membership_by_key(conn, identity_key=identity_key, tenant=tenant)
    if result is None or result.role != role:
        raise AssertionError('Synthetic membership fixture unavailable')
    return result


@unittest.skipUnless(DSN and psycopg, 'isolated PostgreSQL test not configured')
class PostgresIntakeTests(unittest.TestCase):
    def test_synthetic_stale_restore_can_revive_unobserved_session_until_restart(self):
        """Characterize an open recovery risk, not a production safety gate."""
        from nawa_demo.session_draft import SessionError
        from nawa_demo.synthetic_recovery_coordinator import prepare_recovery
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        if (params.get('host') != '127.0.0.1' or 'hostaddr' in params
                or not params.get('dbname', '').startswith('nawa_phase13_qa_')):
            raise ValueError('Disposable localhost QA database required')
        issuer, subject = 'https://synthetic-restore', uuid4().hex
        identity = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        active_connection = [self.connect]
        def lookup(key, tenant):
            with active_connection[0]() as conn:
                return membership_by_key(conn, identity_key=key, tenant=tenant)
        sessions = SessionStore(lookup)
        other_sessions = SessionStore(lookup)
        old = sessions.issue(identity, self.tenant)['session']
        other_old = other_sessions.issue(identity, self.tenant)['session']
        control = sessions.issue(identity, self.tenant)['session']
        restore_name = 'nawa_phase13_qa_restore_' + uuid4().hex[:12]
        pg_bin = Path(r'C:\Program Files\PostgreSQL\18\bin')
        def pg_tool(name):
            return shutil.which(name) or str(pg_bin / (name + '.exe'))
        common = ['-h', params['host'], '-p', params.get('port', '5432'), '-U', params['user']]
        env = os.environ.copy()
        env['PGPASSWORD'] = params.get('password', '')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        created_names = []
        try:
            with TemporaryDirectory() as folder:
                dump = Path(folder) / 'synthetic-stale.dump'
                subprocess.run([pg_tool('pg_dump'), *common, '-Fc', '-f', str(dump), params['dbname']],
                               check=True, capture_output=True, env=env, creationflags=flags)
                with self.connect() as conn:
                    conn.execute('UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s', (issuer, subject))
                    conn.execute('UPDATE nawa_memberships SET active=true WHERE issuer=%s AND subject=%s', (issuer, subject))
                with self.assertRaises(SessionError):
                    sessions.authorize_current(control, 'view')
                with psycopg.connect(DSN, autocommit=True) as admin:
                    admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(restore_name)))
                created_names.append(restore_name)
                subprocess.run([pg_tool('pg_restore'), *common, '-d', restore_name, '--no-owner', str(dump)],
                               check=True, capture_output=True, env=env, creationflags=flags)
                restored = dict(params, dbname=restore_name)
                active_connection[0] = lambda: psycopg.connect(**restored)
                # The old snapshot lacks the later change. This illustrates the
                # unresolved replay risk; it is not an acceptable safety claim.
                self.assertEqual(sessions.authorize_current(old, 'view').role, 'analyst')
                self.assertEqual(other_sessions.authorize_current(other_old, 'view').role, 'analyst')
                # Model a second restore after pausing the first process.
                active_connection[0] = self.connect
                sessions.begin_recovery()
                active_connection[0] = lambda: psycopg.connect(**restored)
                with self.assertRaises(SessionError):
                    sessions.authorize_current(old, 'view')
                with self.assertRaises(SessionError):
                    sessions.issue(identity, self.tenant)
                # Pausing one process does not pause another process.
                self.assertEqual(other_sessions.authorize_current(other_old, 'view').role, 'analyst')
                other_sessions.begin_recovery()
                with self.assertRaises(SessionError):
                    other_sessions.authorize_current(other_old, 'view')
                sessions.end_recovery()
                other_sessions.end_recovery()
                with self.assertRaises(SessionError):
                    sessions.authorize_current(old, 'view')
                with self.assertRaises(SessionError):
                    other_sessions.authorize_current(other_old, 'view')
                restarted = SessionStore(lookup)
                with self.assertRaises(SessionError):
                    restarted.authorize_current(old, 'view')
                fresh = restarted.issue(identity, self.tenant)['session']
                self.assertEqual(restarted.authorize_current(fresh, 'view').role, 'analyst')
                # Exercise coordinator ordering against a second newly restored
                # disposable database. Quiesce/drain are test callbacks, not a
                # real traffic or in-flight request barrier.
                active_connection[0] = self.connect
                prior_tokens = [store.issue(identity, self.tenant)['session'] for store in (sessions, other_sessions)]
                coordinated_name = 'nawa_phase13_qa_restore_' + uuid4().hex[:12]
                with psycopg.connect(DSN, autocommit=True) as admin:
                    admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(coordinated_name)))
                created_names.append(coordinated_name)
                coordinated = dict(params, dbname=coordinated_name)
                def restore():
                    subprocess.run([pg_tool('pg_restore'), *common, '-d', coordinated_name, '--no-owner', str(dump)],
                                   check=True, capture_output=True, env=env, creationflags=flags)
                    active_connection[0] = lambda: psycopg.connect(**coordinated)
                    return True
                def verify():
                    with active_connection[0]() as conn:
                        return membership_by_key(conn, identity_key=identity, tenant=self.tenant) is not None
                outcome = prepare_recovery([sessions, other_sessions], quiesce=lambda:True, drain=lambda:True,
                                           restore=restore, verify=verify)
                self.assertEqual(outcome, {'status':'ready_for_manual_resume', 'completed_pause_calls':2})
                for store, token in zip((sessions, other_sessions), prior_tokens):
                    with self.assertRaises(SessionError):
                        store.authorize_current(token, 'view')
                    with self.assertRaises(SessionError):
                        store.issue(identity, self.tenant)
                    store.end_recovery()
                    with self.assertRaises(SessionError):
                        store.authorize_current(token, 'view')
                    new = store.issue(identity, self.tenant)['session']
                    self.assertEqual(store.authorize_current(new, 'view').role, 'analyst')
        finally:
            with psycopg.connect(DSN, autocommit=True) as admin:
                for name in created_names:
                    admin.execute(psycopg.sql.SQL('DROP DATABASE {}').format(psycopg.sql.Identifier(name)))

    def test_old_principal_rejected_after_unobserved_membership_cycle(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        old = make_principal(self.connect, identity_key, self.tenant, 'analyst')
        with TemporaryDirectory() as folder:
            service = ScopedIntake(self.connect, LocalIntakeStore(folder))
            self.assertEqual(service.list_documents(old), [])
            with self.connect() as conn:
                conn.execute('UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s', (issuer, subject))
                conn.execute('UPDATE nawa_memberships SET active=true WHERE issuer=%s AND subject=%s', (issuer, subject))
            with self.assertRaises(PermissionError):
                service.list_documents(old)
            fresh = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            self.assertEqual(service.list_documents(fresh), [])

    def setUp(self):
        self.tenant = 'synthetic-' + uuid4().hex
        self.other = 'synthetic-' + uuid4().hex
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s),(%s)', (self.tenant, self.other))
        self.jobs = PostgresIntakeJobs(self.connect)

    def test_registration_dedup_and_claim_boundary(self):
        meta = check_document('عرض.txt', 'عرض سعر'.encode('utf-8'))
        document = uuid4().hex
        self.assertEqual(self.jobs.register(self.tenant, document, meta), (document, True))
        self.assertEqual(self.jobs.register(self.tenant, uuid4().hex, meta), (document, False))
        job, token = self.jobs.claim(self.tenant)
        self.assertIsNone(self.jobs.claim(self.other))
        with self.assertRaises(ValueError):
            self.jobs.finish(self.other, job, token, success=True, result={'ok': True})
        with self.assertRaises(ValueError):
            self.jobs.finish(self.tenant, job, 'wrong-token', success=True, result={'ok': True})
        with self.connect() as conn:
            stored = conn.execute('SELECT claim_token_hash FROM nawa_intake_jobs WHERE id=%s', (job,)).fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(self.jobs.finish(self.tenant, job, token, success=True, result={'ok': True}), 'completed')

    def test_concurrent_workers_claim_distinct_jobs(self):
        for content in (b'one', b'two'):
            self.jobs.register(self.tenant, uuid4().hex, check_document('synthetic.txt', content))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.jobs.claim(self.tenant), range(2)))
        self.assertTrue(all(claims))
        self.assertNotEqual(claims[0][0], claims[1][0])

    def test_document_versions_preserve_previous_source_and_reject_stale_update(self):
        family = uuid4().hex
        first, second = uuid4().hex, uuid4().hex
        self.jobs.register(self.tenant, first, check_document('quote.txt', b'version one'))
        self.jobs.register(self.tenant, second, check_document('quote.txt', b'version two'))
        self.assertEqual(self.jobs.link_version(self.tenant, first, family, expected_previous=0), 1)
        self.assertEqual(self.jobs.link_version(self.tenant, second, family, expected_previous=1), 2)
        self.assertEqual(self.jobs.version_history(self.tenant, family), [(1, first), (2, second)])
        self.assertEqual(self.jobs.version_history(self.other, family), [])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.jobs.link_version(self.tenant, first, family, expected_previous=1)

    def test_concurrent_document_versions_allow_only_one_head_update(self):
        family = uuid4().hex
        documents = [uuid4().hex, uuid4().hex]
        for index, document in enumerate(documents):
            self.jobs.register(self.tenant, document, check_document('x.txt', f'version {index}'.encode()))
        def attempt(document):
            try:
                return self.jobs.link_version(self.tenant, document, family, expected_previous=0)
            except ValueError:
                return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, documents))
        self.assertEqual(sorted(map(str, outcomes)), ['1', 'stale'])
        self.assertEqual(len(self.jobs.version_history(self.tenant, family)), 1)

    def test_retries_and_expired_claim(self):
        self.jobs.register(self.tenant, uuid4().hex, check_document('x.txt', b'retry'))
        job, old_token = self.jobs.claim(self.tenant)
        with self.connect() as conn:
            conn.execute("UPDATE nawa_intake_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job,))
        new_job, new_token = self.jobs.claim(self.tenant)
        self.assertEqual(job, new_job)
        with self.assertRaises(ValueError):
            self.jobs.finish(self.tenant, job, old_token, success=True, result={'ok': True})
        self.assertEqual(self.jobs.finish(self.tenant, job, new_token, success=False), 'retry-pending')
        _, final_token = self.jobs.claim(self.tenant)
        self.assertEqual(self.jobs.finish(self.tenant, job, final_token, success=False), 'failed')
        self.assertIsNone(self.jobs.claim(self.tenant))

    def test_interrupted_worker_job_is_reclaimed_after_lease_expiry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = LocalIntakeStore(root / 'files')
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, _ = SyntheticIntakePipeline(store, self.jobs).submit(self.tenant, 'english.txt', content)
            marker = root / 'claimed'
            env = os.environ.copy()
            env['NAWA_SYNTHETIC_TENANT'] = self.tenant
            env['NAWA_SYNTHETIC_MARKER'] = str(marker)
            code = ('import os,time,psycopg; from pathlib import Path; '
                    'from nawa_demo.intake_postgres import PostgresIntakeJobs; '
                    'jobs=PostgresIntakeJobs(lambda: psycopg.connect(os.environ["NAWA_PHASE13_TEST_DSN"])); '
                    'claim=jobs.claim(os.environ["NAWA_SYNTHETIC_TENANT"],lease_seconds=4); '
                    'assert claim is not None; Path(os.environ["NAWA_SYNTHETIC_MARKER"]).write_text("claimed"); '
                    'time.sleep(60)')
            child = subprocess.Popen([sys.executable, '-c', code], env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), 'Synthetic worker did not claim its job')
                self.assertEqual(self.jobs.state(self.tenant, document), ('processing', 1))
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                state = process_one(self.jobs, store, self.tenant)
                if state == 'completed':
                    break
                time.sleep(0.25)
            else:
                self.fail('Synthetic job was not recovered after the worker stopped')
            self.assertEqual(self.jobs.state(self.tenant, document), ('completed', 2))
            self.assertEqual(self.jobs.result(self.tenant, document)['total'], '1050.00')

    def test_unvalidated_received_job_is_not_processed(self):
        document = uuid4().hex
        self.jobs.register(self.tenant, document, check_document('x.txt', b'synthetic'))
        with self.connect() as conn:
            conn.execute("UPDATE nawa_intake_jobs SET state='received' WHERE tenant_id=%s AND document_id=%s",
                         (self.tenant, document))
        self.assertIsNone(self.jobs.claim(self.tenant))
        with self.connect() as conn:
            conn.execute("UPDATE nawa_intake_jobs SET state='validated' WHERE tenant_id=%s AND document_id=%s",
                         (self.tenant, document))
        self.assertIsNotNone(self.jobs.claim(self.tenant))

    def test_revalidate_and_retry_are_scoped_audited_and_concurrent_safe(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.other, 'viewer'))
        analyst = make_principal(self.connect, identity_key, self.tenant, 'analyst')
        viewer = make_principal(self.connect, identity_key, self.other, 'viewer')
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, _ = service.submit(analyst, 'english.txt', content)
            self.assertEqual([item['id'] for item in service.list_documents(analyst)], [document])
            self.assertEqual(service.list_documents(viewer), [])
            with self.connect() as conn:
                conn.execute("UPDATE nawa_intake_jobs SET state='received' WHERE tenant_id=%s AND document_id=%s",
                             (self.tenant, document))
            with self.assertRaises(PermissionError):
                service.requeue(viewer, document, action='revalidate', reason='Synthetic QA')
            (Path(folder) / document).write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                service.requeue(analyst, document, action='revalidate', reason='Synthetic QA')
            self.assertEqual(self.jobs.state(self.tenant, document), ('received', 0))
            (Path(folder) / document).write_bytes(content)
            def revalidate(_):
                try:
                    return service.requeue(analyst, document, action='revalidate', reason='Synthetic QA')
                except ValueError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(revalidate, range(2)))
            self.assertEqual(sum(value is not None for value in outcomes), 1)
            with self.connect() as conn:
                conn.execute("UPDATE nawa_intake_jobs SET state='failed',attempts=3 WHERE tenant_id=%s AND document_id=%s",
                             (self.tenant, document))
            with self.assertRaises(ValueError):
                service.requeue(analyst, document, action='retry', reason=' ')
            self.assertEqual(service.requeue(analyst, document, action='retry', reason='Synthetic retry')['state'], 'validated')
            self.assertEqual(self.jobs.state(self.tenant, document), ('validated', 0))
            with self.connect() as conn:
                events = conn.execute('''SELECT action,actor_hash,reason FROM nawa_intake_admin_events
                    WHERE tenant_id=%s AND document_id=%s ORDER BY id''', (self.tenant, document)).fetchall()
            actor = hashlib.sha256(identity_key.encode()).hexdigest()
            self.assertEqual(events, [('revalidate', actor, 'Synthetic QA'), ('retry', actor, 'Synthetic retry')])
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')

    def test_retry_event_failure_rolls_back_state(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        analyst = make_principal(self.connect, identity_key, self.tenant, 'analyst')
        suffix = uuid4().hex
        function, trigger = 'nawa_test_failure_' + suffix, 'nawa_test_trigger_' + suffix
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            document, _ = service.submit(analyst, 'x.txt', b'synthetic')
            with self.connect() as conn:
                conn.execute("UPDATE nawa_intake_jobs SET state='failed',attempts=3 WHERE tenant_id=%s AND document_id=%s",
                             (self.tenant, document))
                conn.execute(psycopg.sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic event failure'; END $$").format(psycopg.sql.Identifier(function)))
                conn.execute(psycopg.sql.SQL('CREATE TRIGGER {} BEFORE INSERT ON nawa_intake_admin_events FOR EACH ROW WHEN (NEW.tenant_id = {}) EXECUTE FUNCTION {}()').format(
                    psycopg.sql.Identifier(trigger), psycopg.sql.Literal(self.tenant), psycopg.sql.Identifier(function)))
            try:
                with self.assertRaises(psycopg.Error):
                    service.requeue(analyst, document, action='retry', reason='Synthetic retry')
                self.assertEqual(self.jobs.state(self.tenant, document), ('failed', 3))
                with self.connect() as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM nawa_intake_admin_events WHERE tenant_id=%s',
                                                  (self.tenant,)).fetchone()[0], 0)
            finally:
                with self.connect() as conn:
                    conn.execute(psycopg.sql.SQL('DROP TRIGGER {} ON nawa_intake_admin_events').format(psycopg.sql.Identifier(trigger)))
                    conn.execute(psycopg.sql.SQL('DROP FUNCTION {}()').format(psycopg.sql.Identifier(function)))

    def test_synthetic_file_to_result_through_postgres(self):
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            content = (Path(__file__).resolve().parents[1] / 'samples/arabic_quote.txt').read_bytes()
            pipeline = SyntheticIntakePipeline(store, self.jobs)
            document, created = pipeline.submit(self.tenant, 'arabic.txt', content)
            self.assertTrue(created)
            self.assertEqual(pipeline.submit(self.tenant, 'copy.txt', content), (document, False))
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            self.assertEqual(self.jobs.state(self.tenant, document), ('completed', 1))
            self.assertEqual(self.jobs.result(self.tenant, document)['total'], '945.00')
            self.assertIsNone(self.jobs.result(self.other, document))

    def test_synthetic_retrieval_uses_approved_latest_verified_tenant_source(self):
        from nawa_demo.synthetic_live_retrieval import EvidenceUnavailable, SyntheticLiveRetrieval
        from nawa_demo.synthetic_postgres_passages import SyntheticPostgresPassages
        from nawa_demo.synthetic_postgres_review_queue import SyntheticPostgresReviewQueue
        from nawa_demo.synthetic_review_queue import ReviewRequestUnavailable
        issuer = 'https://synthetic-retrieval'
        users = [(uuid4().hex, self.tenant), (uuid4().hex, self.other)]
        reviewer_users = [(uuid4().hex, self.tenant), (uuid4().hex, self.other)]
        viewer_subject = uuid4().hex
        with self.connect() as conn:
            for subject, tenant in users:
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer, subject, tenant, 'analyst'))
            for subject, tenant in reviewer_users:
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer, subject, tenant, 'reviewer'))
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, viewer_subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, viewer_subject, self.tenant, 'viewer'))
        keys = [json.dumps([issuer, subject], separators=(',', ':')) for subject, _ in users]
        reviewer_keys = [json.dumps([issuer, subject], separators=(',', ':')) for subject, _ in reviewer_users]
        viewer_key = json.dumps([issuer, viewer_subject], separators=(',', ':'))
        principals = [make_principal(self.connect, key, tenant, 'analyst')
                      for key, (_, tenant) in zip(keys, users)]
        reviewers = [make_principal(self.connect, key, tenant, 'reviewer')
                     for key, (_, tenant) in zip(reviewer_keys, reviewer_users)]
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            intake = ScopedIntake(self.connect, store)
            review = ScopedState(self.connect)
            source = SyntheticPostgresPassages(self.connect, store)
            retrieval = SyntheticLiveRetrieval(source.principal, source.passages)
            sample = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            families = [uuid4().hex, uuid4().hex]
            def create(index, suffix, expected):
                principal = principals[index]
                data = sample + ('\n# synthetic-' + suffix + '\n').encode()
                document, _ = intake.submit(principal, suffix + '.txt', data)
                intake.link_version(principal, document, families[index], expected_previous=expected)
                self.assertEqual(process_one(self.jobs, store, principal.tenant), 'completed')
                quote = intake.promote(principal, document)['id']
                review.review_start(principal, quote)
                return document, quote
            def approve(index, quote):
                review.review_decision(reviewers[index], {'id':quote, 'decision':'approve', 'reason':'synthetic review',
                                                  'expected_revision':1})
            first, first_quote = create(0, 'first', 0)
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
            approve(0, first_quote)
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
            with self.assertRaises(ValueError):
                source.set_access(principals[0], first, keys[0], True, 'analyst cannot grant')
            self.assertTrue(source.set_access(reviewers[0], first, keys[0], True, 'synthetic grant'))
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total')[0]['document_id'], first)
            other, other_quote = create(1, 'other', 0)
            approve(1, other_quote)
            self.assertTrue(source.set_access(reviewers[1], other, keys[1], True, 'synthetic grant'))
            self.assertEqual(retrieval.search(keys[1], self.other, 'Grand Total')[0]['document_id'], other)
            self.assertEqual(retrieval.search(viewer_key, self.tenant, 'Grand Total'), [])
            with self.assertRaises(ValueError):
                source.set_access(reviewers[0], other, keys[0], True, 'cross tenant')
            with self.assertRaises(ValueError):
                source.set_access(reviewers[0], first, keys[1], True, 'cross tenant target')
            self.assertTrue(source.set_access(reviewers[0], first, viewer_key, True, 'viewer grant'))
            self.assertEqual(retrieval.search(viewer_key, self.tenant, 'Grand Total')[0]['document_id'], first)
            self.assertTrue(source.set_access(reviewers[0], first, viewer_key, False, 'viewer revocation'))
            self.assertFalse(source.set_access(reviewers[0], first, viewer_key, False, 'duplicate revocation'))
            self.assertEqual(retrieval.search(viewer_key, self.tenant, 'Grand Total'), [])
            with self.assertRaises(EvidenceUnavailable):
                retrieval.resolve(viewer_key, self.tenant, first + ':v1')
            with self.assertRaises(EvidenceUnavailable):
                retrieval.resolve(keys[1], self.other, first + ':v1')
            second, second_quote = create(0, 'second', 1)
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
            with self.assertRaises(EvidenceUnavailable):
                retrieval.resolve(keys[0], self.tenant, first + ':v1')
            approve(0, second_quote)
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
            self.assertTrue(source.set_access(reviewers[0], second, keys[0], True, 'second version grant'))
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total')[0]['document_id'], second)
            calls = []
            def changed_grant(principal):
                calls.append(1)
                if len(calls) == 2:
                    self.assertTrue(source.set_access(reviewers[0], second, keys[0], False, 'synthetic revoke'))
                    self.assertTrue(source.set_access(reviewers[0], second, keys[0], True, 'synthetic regrant'))
                return source.passages(principal)
            changing_retrieval = SyntheticLiveRetrieval(source.principal, changed_grant)
            with self.assertRaises(EvidenceUnavailable):
                changing_retrieval.search(keys[0], self.tenant, 'Grand Total')
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total')[0]['document_id'], second)
            # Characterize restoring a backup taken before a grant revocation.
            # The target is a new disposable database, never the source DB.
            params = psycopg.conninfo.conninfo_to_dict(DSN)
            if (params.get('host') != '127.0.0.1' or 'hostaddr' in params
                    or not params.get('dbname', '').startswith('nawa_phase13_qa_')):
                raise ValueError('Disposable localhost QA database required')
            restored_name = 'nawa_phase13_qa_restore_' + uuid4().hex[:12]
            pg_bin = Path(r'C:\Program Files\PostgreSQL\18\bin')
            def pg_tool(name):
                return shutil.which(name) or str(pg_bin / (name + '.exe'))
            common = ['-h', params['host'], '-p', params.get('port', '5432'), '-U', params['user']]
            pg_env = os.environ.copy()
            pg_env['PGPASSWORD'] = params.get('password', '')
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            created_restore = False
            try:
                with TemporaryDirectory() as backup_dir:
                    dump = Path(backup_dir) / 'synthetic-grants.dump'
                    subprocess.run([pg_tool('pg_dump'), *common, '-Fc', '-f', str(dump), params['dbname']],
                                   check=True, capture_output=True, env=pg_env, creationflags=flags)
                    self.assertTrue(source.set_access(reviewers[0], second, keys[0], False, 'before restore'))
                    self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
                    with psycopg.connect(DSN, autocommit=True) as admin:
                        admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(restored_name)))
                    created_restore = True
                    subprocess.run([pg_tool('pg_restore'), *common, '-d', restored_name, '--no-owner', str(dump)],
                                   check=True, capture_output=True, env=pg_env, creationflags=flags)
                    restored_params = dict(params, dbname=restored_name)
                    restored_connect = lambda: psycopg.connect(**restored_params)
                    restored_source = SyntheticPostgresPassages(restored_connect, store)
                    restored_search = SyntheticLiveRetrieval(restored_source.principal, restored_source.passages)
                    # Risk characterization: this old backup reintroduces access
                    # that was revoked later; it is not a recovery safety pass.
                    self.assertEqual(restored_search.search(keys[0], self.tenant, 'Grand Total')[0]['document_id'], second)
                    # An explicit QA-only reset on the separate restored target
                    # denies all restored grants until individually regranted.
                    with restored_connect() as conn:
                        reset = conn.execute('UPDATE nawa_synthetic_document_grants SET active=false WHERE active')
                        self.assertGreaterEqual(reset.rowcount, 1)
                    self.assertEqual(restored_search.search(keys[0], self.tenant, 'Grand Total'), [])
                    self.assertEqual(restored_search.search(keys[1], self.other, 'Grand Total'), [])
                    self.assertTrue(restored_source.set_access(reviewers[0], second, keys[0], True, 'synthetic reapproval'))
                    self.assertEqual(restored_search.search(keys[0], self.tenant, 'Grand Total')[0]['document_id'], second)
                    self.assertEqual(restored_search.search(keys[1], self.other, 'Grand Total'), [])
                    self.assertTrue(source.set_access(reviewers[0], second, keys[0], True, 'restore test cleanup'))
            finally:
                if created_restore:
                    with psycopg.connect(DSN, autocommit=True) as admin:
                        admin.execute(psycopg.sql.SQL('DROP DATABASE {}').format(psycopg.sql.Identifier(restored_name)))
            http_sessions = SessionStore(source.principal)
            owner_token = http_sessions.issue(keys[0], self.tenant)
            viewer_token = http_sessions.issue(viewer_key, self.tenant)
            other_token = http_sessions.issue(keys[1], self.other)
            reviewer_token = http_sessions.issue(reviewer_keys[0], self.tenant)
            queue = SyntheticPostgresReviewQueue(retrieval, self.connect)
            handler = make_protected_handler(None, http_sessions, DashboardState(), synthetic_retrieval=retrieval,
                                             synthetic_review_queue=queue)
            http_server = HTTPServer(('127.0.0.1', 0), handler)
            http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            http_thread.start()
            base = 'http://127.0.0.1:%s' % http_server.server_port
            def evidence_request(path, payload, token=None):
                headers = {'Origin':'http://127.0.0.1:8765', 'Content-Type':'application/json'}
                if token:
                    headers.update({'Cookie':'nawa_session=' + token['session'], 'X-NAWA-CSRF':token['csrf']})
                try:
                    with urlopen(Request(base + path, data=json.dumps(payload).encode(), headers=headers), timeout=5) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    with error:
                        return error.code, json.load(error)
            search = '/api/synthetic-evidence/search'
            source_path = '/api/synthetic-evidence/source'
            review_path = '/api/synthetic-evidence/review'
            query, identifier = {'q':'Grand Total'}, {'id':second + ':v2'}
            try:
                self.assertEqual(evidence_request(search, query)[0], 403)
                self.assertEqual(evidence_request(search, query, owner_token)[1]['passages'][0]['document_id'], second)
                self.assertEqual(evidence_request(source_path, identifier, owner_token)[1]['passage']['document_id'], second)
                handoff = evidence_request(review_path, query, owner_token)[1]
                self.assertEqual((handoff['status'], handoff['answer'], handoff['citations']),
                                 ('requires_human_review', None, []))
                self.assertEqual(handoff['evidence'][0]['document_id'], second)
                self.assertEqual(evidence_request(search, query, viewer_token)[1]['passages'], [])
                self.assertEqual(evidence_request(review_path, query, viewer_token)[1]['evidence'], [])
                self.assertEqual(evidence_request(source_path, identifier, viewer_token)[0], 503)
                self.assertEqual(evidence_request(source_path, identifier, other_token)[0], 503)
                task_request = {'request_key':uuid4().hex, 'question':'Review synthetic evidence',
                                'passage_id':second + ':v2'}
                created = evidence_request('/api/synthetic-review/create', task_request, owner_token)
                self.assertEqual((created[0],created[1]['status'],created[1]['created']), (200,'pending_review',True))
                task_id = created[1]['id']
                retry = evidence_request('/api/synthetic-review/create', task_request, owner_token)
                self.assertEqual((retry[1]['id'],retry[1]['created']), (task_id,False))
                fresh = SyntheticPostgresReviewQueue(retrieval, self.connect)
                self.assertEqual(fresh.get(principals[0], task_id)['question'], 'Review synthetic evidence')
                with self.assertRaises(ReviewRequestUnavailable):
                    fresh.create(principals[0], task_request['request_key'], 'Different question', second + ':v2')
                with self.connect() as conn:
                    saved = conn.execute('SELECT question,evidence FROM nawa_synthetic_review_requests WHERE id=%s',
                                         (task_id,)).fetchone()
                self.assertEqual(saved[0], 'Review synthetic evidence')
                self.assertNotIn('untrusted_source_text', saved[1])
                child_code = '''
import os, sys, psycopg
from nawa_demo.intake_store import LocalIntakeStore
from nawa_demo.synthetic_postgres_passages import SyntheticPostgresPassages
from nawa_demo.synthetic_live_retrieval import SyntheticLiveRetrieval
from nawa_demo.synthetic_postgres_review_queue import SyntheticPostgresReviewQueue
dsn = os.environ['NAWA_PHASE13_TEST_DSN']
params = psycopg.conninfo.conninfo_to_dict(dsn)
assert params.get('host') == '127.0.0.1' and 'hostaddr' not in params
assert params.get('dbname', '').startswith('nawa_phase13_qa_')
def connect(): return psycopg.connect(dsn)
source = SyntheticPostgresPassages(connect, LocalIntakeStore(sys.argv[4]))
retrieval = SyntheticLiveRetrieval(source.principal, source.passages)
task = SyntheticPostgresReviewQueue(retrieval, connect).get(source.principal(sys.argv[2], sys.argv[1]), sys.argv[3])
assert task['status'] == 'pending_review' and task['question'] == 'Review synthetic evidence'
print('Synthetic request found in a fresh process')
'''
                child = subprocess.run([sys.executable, '-c', child_code, self.tenant, keys[0], task_id, folder],
                                       capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertIn('Synthetic request found', child.stdout)
                concurrent_key = uuid4().hex
                with ThreadPoolExecutor(max_workers=4) as workers:
                    results = list(workers.map(lambda _: fresh.create(principals[0], concurrent_key,
                                   'Concurrent synthetic review', second + ':v2'), range(4)))
                self.assertEqual(len({item['id'] for item in results}), 1)
                self.assertEqual(sum(item['created'] for item in results), 1)
                self.assertEqual(evidence_request('/api/synthetic-review/show', {'id':task_id}, reviewer_token)[0], 400)
                self.assertTrue(source.set_access(reviewers[0], second, reviewer_keys[0], True, 'reviewer access'))
                self.assertEqual(evidence_request('/api/synthetic-review/show', {'id':task_id}, reviewer_token)[0], 200)
                self.assertTrue(source.set_access(reviewers[0], second, reviewer_keys[0], False, 'reviewer access revoked'))
                self.assertEqual(evidence_request('/api/synthetic-review/show', {'id':task_id}, reviewer_token)[0], 400)
                self.assertEqual(evidence_request('/api/synthetic-review/show', {'id':task_id}, other_token)[0], 400)
                self.assertTrue(source.set_access(reviewers[0], second, viewer_key, True, 'http grant'))
                self.assertEqual(evidence_request(source_path, identifier, viewer_token)[0], 200)
                self.assertEqual(evidence_request(review_path, query, viewer_token)[1]['evidence'][0]['document_id'], second)
                self.assertTrue(source.set_access(reviewers[0], second, viewer_key, False, 'http revoke'))
                self.assertEqual(evidence_request(source_path, identifier, viewer_token)[0], 503)
                self.assertEqual(evidence_request(review_path, query, viewer_token)[1]['evidence'], [])
            finally:
                http_server.shutdown()
                http_server.server_close()
                http_thread.join(5)
            with self.connect() as conn:
                conn.execute('UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s',
                             (issuer, users[0][0], self.tenant))
                conn.execute('UPDATE nawa_memberships SET active=true WHERE issuer=%s AND subject=%s AND tenant_id=%s',
                             (issuer, users[0][0], self.tenant))
            principals[0] = make_principal(self.connect, keys[0], self.tenant, 'analyst')
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
            self.assertTrue(source.set_access(reviewers[0], second, keys[0], True, 'regrant after reactivation'))
            original_second = (Path(folder) / second).read_bytes()
            (Path(folder) / second).write_bytes(b'tampered')
            with self.assertRaises(EvidenceUnavailable):
                retrieval.search(keys[0], self.tenant, 'Grand Total')
            with self.assertRaises(ReviewRequestUnavailable):
                queue.get(principals[0], task_id)
            self.assertEqual(retrieval.search(keys[1], self.other, 'Grand Total')[0]['document_id'], other)
            (Path(folder) / second).write_bytes(original_second)
            with self.assertRaises(ValueError):
                source.withdraw(principals[0], second, 'synthetic withdrawal')
            reviewer = reviewers[0]
            with self.assertRaises(ValueError):
                source.withdraw(reviewer, other, 'other tenant source')
            self.assertTrue(source.withdraw(reviewer, second, 'synthetic withdrawal'))
            with self.assertRaises(ReviewRequestUnavailable):
                queue.get(principals[0], task_id)
            self.assertFalse(source.withdraw(reviewer, second, 'repeated withdrawal'))
            self.assertEqual(retrieval.search(keys[0], self.tenant, 'Grand Total'), [])
            with self.assertRaises(EvidenceUnavailable):
                retrieval.resolve(keys[0], self.tenant, second + ':v2')
            # A withdrawn newest version must not revive the approved older one.
            with self.assertRaises(EvidenceUnavailable):
                retrieval.resolve(keys[0], self.tenant, first + ':v1')
            self.assertEqual(retrieval.search(keys[1], self.other, 'Grand Total')[0]['document_id'], other)
            with self.connect() as conn:
                withdrawn = conn.execute('''SELECT actor_hash,reason FROM nawa_synthetic_withdrawals
                    WHERE tenant_id=%s AND document_id=%s''', (self.tenant, second)).fetchone()
            self.assertEqual(withdrawn, (hashlib.sha256(reviewer_keys[0].encode()).hexdigest(), 'synthetic withdrawal'))
            with self.connect() as conn:
                events = conn.execute('''SELECT action,reason FROM nawa_synthetic_document_grant_events
                    WHERE tenant_id=%s AND document_id=%s AND subject_hash=%s ORDER BY id''',
                    (self.tenant, first, hashlib.sha256(viewer_key.encode()).hexdigest())).fetchall()
            self.assertEqual(events, [('grant', 'viewer grant'), ('revoke', 'viewer revocation')])
            with self.connect() as conn:
                conn.execute('UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s',
                             (issuer, users[0][0], self.tenant))
            with self.assertRaises(EvidenceUnavailable):
                retrieval.search(keys[0], self.tenant, 'Grand Total')

    def test_scoped_access_rechecks_roles_tenant_and_revocation(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.other, 'viewer'))
        analyst = make_principal(self.connect, identity_key, self.tenant, 'analyst')
        viewer = make_principal(self.connect, identity_key, self.other, 'viewer')
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, _ = service.submit(analyst, 'english.txt', content)
            family = uuid4().hex
            self.assertEqual(service.link_version(analyst, document, family, expected_previous=0), 1)
            self.assertEqual(service.version_history(viewer, family), [])
            with self.assertRaises(PermissionError):
                service.submit(viewer, 'no.txt', content)
            with self.assertRaises(PermissionError):
                service.link_version(viewer, document, family, expected_previous=0)
            self.assertIsNone(service.state(viewer, document))
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            self.assertEqual(service.result(analyst, document)['total'], '1050.00')
            promoted = service.promote(analyst, document)
            self.assertTrue(promoted['created'])
            self.assertEqual(promoted['quotation']['total'], '1050.00')
            repeated = service.promote(analyst, document)
            self.assertFalse(repeated['created'])
            self.assertEqual(repeated['id'], promoted['id'])
            with self.assertRaises(PermissionError):
                service.promote(viewer, document)
            self.assertIsNone(service.result(viewer, document))
            with self.connect() as conn:
                conn.execute('UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s',
                             (issuer, subject, self.tenant))
            with self.assertRaises(PermissionError):
                service.result(analyst, document)
            with self.assertRaises(PermissionError):
                service.list_documents(analyst)
            with self.assertRaises(PermissionError):
                service.version_history(analyst, family)
            with self.assertRaises(PermissionError):
                service.promote(analyst, document)
            with self.assertRaises(PermissionError):
                service.submit(analyst, 'again.txt', content)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_scoped_uncertain_commit_preserves_committed_file(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))

        @contextmanager
        def uncertain_connection():
            with self.connect() as conn:
                yield conn
            raise RuntimeError('simulated error after commit')

        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(uncertain_connection, store)
            principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            with self.assertRaisesRegex(RuntimeError, 'after commit'):
                service.submit(principal, 'x.txt', b'synthetic')
            stored = list(Path(folder).iterdir())
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].read_bytes(), b'synthetic')
            self.assertEqual(self.jobs.state(self.tenant, stored[0].name), ('validated', 0))

    def test_concurrent_promotions_create_one_quotation(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, _ = service.submit(principal, 'english.txt', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: service.promote(principal, document), range(4)))
            self.assertEqual(sum(item['created'] for item in results), 1)
            self.assertEqual(len({item['id'] for item in results}), 1)
            with self.connect() as conn:
                count = conn.execute('SELECT count(*) FROM nawa_quotation_tenants WHERE tenant_id=%s',
                                     (self.tenant,)).fetchone()[0]
            self.assertEqual(count, 1)

    def test_tenant_quota_is_atomic_and_rejected_files_are_removed(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            first = None
            for index in range(MAX_DOCUMENTS_PER_TENANT - 1):
                document, _ = service.submit(principal, f'{index}.txt', f'synthetic-{index}'.encode())
                first = first or document
            def submit(index):
                try:
                    return service.submit(principal, f'last-{index}.txt', f'new-{index}'.encode())
                except IntakeRejected:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(submit, range(2)))
            self.assertEqual(sum(item is not None for item in outcomes), 1)
            self.assertEqual(len(list(Path(folder).iterdir())), MAX_DOCUMENTS_PER_TENANT)
            self.assertEqual(service.submit(principal, 'duplicate.txt', b'synthetic-0'), (first, False))
            self.assertEqual(len(list(Path(folder).iterdir())), MAX_DOCUMENTS_PER_TENANT)
            with self.connect() as conn:
                count = conn.execute('SELECT count(*) FROM nawa_intake_documents WHERE tenant_id=%s',
                                     (self.tenant,)).fetchone()[0]
            self.assertEqual(count, MAX_DOCUMENTS_PER_TENANT)

    def test_tenant_byte_quota_is_atomic_and_allows_duplicates(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            first, _ = service.submit(principal, 'one.txt', b'A' * MAX_DOCUMENT_BYTES)
            service.submit(principal, 'two.txt', b'B' * MAX_DOCUMENT_BYTES)
            service.submit(principal, 'three.txt', b'C' * MAX_DOCUMENT_BYTES)
            service.submit(principal, 'four.txt', b'D' * (MAX_DOCUMENT_BYTES - 1))
            def submit(content):
                try:
                    return service.submit(principal, 'last.txt', content)
                except IntakeRejected:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(submit, (b'x', b'y')))
            self.assertEqual(sum(item is not None for item in outcomes), 1)
            self.assertEqual(service.submit(principal, 'duplicate.txt', b'A' * MAX_DOCUMENT_BYTES), (first, False))
            self.assertEqual(len(list(Path(folder).iterdir())), 5)
            self.assertEqual(sum(path.stat().st_size for path in Path(folder).iterdir()), MAX_BYTES_PER_TENANT)
            with self.connect() as conn:
                size = conn.execute('SELECT sum(size_bytes) FROM nawa_intake_documents WHERE tenant_id=%s',
                                    (self.tenant,)).fetchone()[0]
            self.assertEqual(size, MAX_BYTES_PER_TENANT)

    def test_promotion_rejects_ocr_marker_without_creating_quotation(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, _ = service.submit(principal, 'english.txt', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            original = self.jobs.result(self.tenant, document)
            self.assertEqual(original['extraction_origin'], 'synthetic_text_worker_v1')
            with self.connect() as conn:
                stored_origin = conn.execute('SELECT result_origin FROM nawa_intake_jobs WHERE tenant_id=%s AND document_id=%s',
                                             (self.tenant, document)).fetchone()[0]
            self.assertEqual(stored_origin, 'synthetic_text_worker_v1')
            marked = dict(original, input_type='ocr_pdf')
            with self.connect() as conn:
                conn.execute('UPDATE nawa_intake_jobs SET result=%s::jsonb WHERE tenant_id=%s AND document_id=%s',
                             (json.dumps(marked), self.tenant, document))
            with self.assertRaisesRegex(ValueError, 'OCR'):
                service.promote(principal, document)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM nawa_quotation_tenants WHERE tenant_id=%s',
                                              (self.tenant,)).fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT count(*) FROM nawa_intake_promotions WHERE tenant_id=%s',
                                              (self.tenant,)).fetchone()[0], 0)
                unmarked = dict(original)
                del unmarked['extraction_origin']
                conn.execute('UPDATE nawa_intake_jobs SET result=%s::jsonb WHERE tenant_id=%s AND document_id=%s',
                             (json.dumps(unmarked), self.tenant, document))
            with self.assertRaisesRegex(ValueError, 'origin'):
                service.promote(principal, document)
            with self.connect() as conn:
                conn.execute('UPDATE nawa_intake_jobs SET result=%s::jsonb WHERE tenant_id=%s AND document_id=%s',
                             (json.dumps(original), self.tenant, document))
            self.assertTrue(service.promote(principal, document)['created'])

    def test_optional_recorded_origin_policy_preserves_legacy_read_access(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            compatible = ScopedIntake(self.connect, store)
            strict = ScopedIntake(self.connect, store, origin_policy='require_recorded')
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            legacy, _ = compatible.submit(principal, 'legacy.txt', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            legacy_result = self.jobs.result(self.tenant, legacy)
            del legacy_result['extraction_origin']
            with self.connect() as conn:
                conn.execute('''UPDATE nawa_intake_jobs SET result=%s::jsonb,result_origin=NULL
                                WHERE tenant_id=%s AND document_id=%s''',
                             (json.dumps(legacy_result), self.tenant, legacy))
            self.assertEqual(strict.result(principal, legacy)['total'], '1050.00')
            with self.assertRaisesRegex(ValueError, 'origin required'):
                strict.promote(principal, legacy)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM nawa_quotation_tenants WHERE tenant_id=%s',
                                              (self.tenant,)).fetchone()[0], 0)
            self.assertTrue(compatible.promote(principal, legacy)['created'])
            current, _ = strict.submit(principal, 'current.txt', content + b'\n# second synthetic source\n')
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            paused = ScopedIntake(self.connect, store, origin_policy='promotion_disabled')
            self.assertEqual(paused.result(principal, current)['total'], '1050.00')
            with self.assertRaisesRegex(ValueError, 'promotion is disabled'):
                paused.promote(principal, current)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM nawa_intake_promotions WHERE tenant_id=%s',
                                              (self.tenant,)).fetchone()[0], 1)
            self.assertTrue(strict.promote(principal, current)['created'])
            with self.assertRaisesRegex(ValueError, 'promotion is disabled'):
                paused.promote(principal, current)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM nawa_intake_promotions WHERE tenant_id=%s',
                                              (self.tenant,)).fetchone()[0], 2)

    def test_required_correction_source_validates_replays_and_rejects_tampering(self):
        issuer = 'https://synthetic-issuer'
        analyst_subject, reviewer_subject = uuid4().hex, uuid4().hex
        with self.connect() as conn:
            for subject, role in ((analyst_subject, 'analyst'), (reviewer_subject, 'reviewer')):
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer, subject, self.tenant, role))
        analyst = make_principal(self.connect, json.dumps([issuer, analyst_subject], separators=(',', ':')), self.tenant, 'analyst')
        reviewer = make_principal(self.connect, json.dumps([issuer, reviewer_subject], separators=(',', ':')), self.tenant, 'reviewer')
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            intake = ScopedIntake(self.connect, store)
            state = ScopedState(self.connect, correction_source_policy='require_intake_txt', correction_source_store=store)
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            content = content.replace(b'Grand Total: 1050.00', b'Grand Total: 1051.00')
            document, _ = intake.submit(analyst, 'source.txt', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            item = intake.promote(analyst, document)['id']
            second = state.add_sample(analyst, 'arabic_quote.txt')['id']
            state.review_start(analyst, item)
            payload = {'id':item, 'field':'total', 'value':'1050.00', 'reason':'Synthetic correction',
                       'source':'source.txt', 'line':3, 'quote':'forged', 'expected_revision':1}
            with self.assertRaisesRegex(ValueError, 'does not match source'):
                state.review_correction(analyst, payload)
            self.assertEqual(state.review_show(analyst, item)['revision'], 1)
            payload['quote'] = 'Subtotal: 1000.00'
            self.assertEqual(state.review_correction(analyst, payload)['revision'], 2)
            source = Path(folder) / document
            source.write_bytes(b'tampered')
            decision = {'id':item, 'decision':'approve', 'reason':'Synthetic review', 'expected_revision':2}
            with self.assertRaisesRegex(ValueError, 'integrity'):
                state.review_decision(reviewer, decision)
            self.assertEqual(state.review_show(analyst, item)['revision'], 2)
            source.write_bytes(content)
            self.assertEqual(state.review_decision(reviewer, decision)['status'], 'approved')
            self.assertEqual(state.compare(analyst, [item, second])['quotations'][0]['total'], '1050.00')
            source.write_bytes(b'tampered after approval')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                state.compare(analyst, [item, second])
            state.review_start(analyst, second)
            with self.assertRaisesRegex(ValueError, 'source unavailable'):
                state.review_correction(analyst, {'id':second, 'field':'total', 'value':'946.00',
                    'reason':'Synthetic test', 'source':'arabic_quote.txt', 'line':6,
                    'quote':'الإجمالي شامل الضريبة: ٩٤٥٫٠٠', 'expected_revision':1})
            self.assertEqual(state.review_show(analyst, second)['revision'], 1)

    def test_pinned_synthetic_pdf_correction_rechecks_retained_source(self):
        issuer = 'https://synthetic-issuer'
        analyst_subject, reviewer_subject = uuid4().hex, uuid4().hex
        with self.connect() as conn:
            for subject, role in ((analyst_subject, 'analyst'), (reviewer_subject, 'reviewer')):
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer, subject, self.tenant, role))
        analyst = make_principal(self.connect, json.dumps([issuer, analyst_subject], separators=(',', ':')), self.tenant, 'analyst')
        reviewer = make_principal(self.connect, json.dumps([issuer, reviewer_subject], separators=(',', ':')), self.tenant, 'reviewer')
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            intake = ScopedIntake(self.connect, store)
            strict = ScopedState(self.connect, correction_source_policy='require_intake_fixture', correction_source_store=store)
            txt_only = ScopedState(self.connect, correction_source_policy='require_intake_txt', correction_source_store=store)
            content = (Path(__file__).resolve().parents[1] / 'samples/pdf/arabic_text_supplier.pdf').read_bytes()
            document, _ = intake.submit(analyst, 'synthetic-arabic.pdf', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            item = intake.promote(analyst, document)['id']
            other = strict.add_sample(analyst, 'english_quote.txt')['id']
            strict.review_start(analyst, item)
            correction = {'id':item, 'field':'total', 'value':'945', 'reason':'Synthetic format correction',
                          'source':'synthetic-arabic.pdf', 'line':6, 'quote':'forged', 'expected_revision':1}
            with self.assertRaises(ValueError):
                strict.review_correction(analyst, correction)
            correction['quote'] = 'Grand Total: 945.00'
            with self.assertRaises(ValueError):
                txt_only.review_correction(analyst, correction)
            self.assertEqual(strict.review_correction(analyst, correction)['revision'], 2)
            source = Path(folder) / document
            source.write_bytes(b'changed synthetic source')
            decision = {'id':item, 'decision':'approve', 'reason':'Synthetic review', 'expected_revision':2}
            with self.assertRaisesRegex(ValueError, 'integrity'):
                strict.review_decision(reviewer, decision)
            source.write_bytes(content)
            self.assertEqual(strict.review_decision(reviewer, decision)['status'], 'approved')
            self.assertEqual(strict.compare(analyst, [item, other])['quotations'][0]['total'], '945')
            source.write_bytes(b'changed again')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                strict.compare(analyst, [item, other])

    def test_multipage_pdf_correction_reports_verified_page_and_line(self):
        issuer = 'https://synthetic-issuer'
        analyst_subject, reviewer_subject = uuid4().hex, uuid4().hex
        with self.connect() as conn:
            for subject, role in ((analyst_subject, 'analyst'), (reviewer_subject, 'reviewer')):
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer, subject, self.tenant, role))
        analyst = make_principal(self.connect, json.dumps([issuer, analyst_subject], separators=(',', ':')), self.tenant, 'analyst')
        reviewer = make_principal(self.connect, json.dumps([issuer, reviewer_subject], separators=(',', ':')), self.tenant, 'reviewer')
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            intake = ScopedIntake(self.connect, store)
            state = ScopedState(self.connect, correction_source_policy='require_intake_fixture', correction_source_store=store)
            content = (Path(__file__).resolve().parents[1] / 'samples/pdf/second_page_supplier.pdf').read_bytes()
            document, _ = intake.submit(analyst, 'synthetic-multipage.pdf', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            item = intake.promote(analyst, document)['id']
            other = state.add_sample(analyst, 'english_quote.txt')['id']
            state.review_start(analyst, item)
            correction = {'id':item, 'field':'total', 'value':'945', 'reason':'Synthetic format correction',
                          'source':'synthetic-multipage.pdf', 'line':6, 'quote':'Grand Total: 945.00', 'expected_revision':1}
            with self.assertRaises(ValueError):
                state.review_correction(analyst, correction)
            correction['line'] = 7
            expected = {'kind':'pdf_text_layer', 'global_line':7, 'page':2, 'page_line':6}
            self.assertEqual(state.review_correction(analyst, correction)['source_checks'], [expected])
            decision = {'id':item, 'decision':'approve', 'reason':'Synthetic review', 'expected_revision':2}
            self.assertEqual(state.review_decision(reviewer, decision)['source_checks'], [expected])
            compared = state.compare(analyst, [item, other])['quotations'][0]
            self.assertEqual(compared['total'], '945')
            self.assertEqual(compared['comparison']['source_checks'], [expected])

    def test_opt_in_text_layer_correction_for_new_synthetic_pdf(self):
        import pymupdf
        issuer = 'https://synthetic-issuer'
        analyst_subject, reviewer_subject = uuid4().hex, uuid4().hex
        with self.connect() as conn:
            for subject, role in ((analyst_subject, 'analyst'), (reviewer_subject, 'reviewer')):
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer, subject, self.tenant, role))
        analyst = make_principal(self.connect, json.dumps([issuer, analyst_subject], separators=(',', ':')), self.tenant, 'analyst')
        reviewer = make_principal(self.connect, json.dumps([issuer, reviewer_subject], separators=(',', ':')), self.tenant, 'reviewer')
        pdf = pymupdf.open()
        pdf.new_page().insert_text((50, 60), 'Synthetic cover')
        page = pdf.new_page()
        for index, line in enumerate(('Supplier: Synthetic New Supplier', 'Currency: AED', 'Subtotal: 300.00',
                                      'VAT Rate: 5', 'VAT Amount: 15.00', 'Grand Total: 315.00')):
            page.insert_text((50, 60 + index * 25), line)
        content = pdf.tobytes()
        pdf.close()
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            intake = ScopedIntake(self.connect, store)
            state = ScopedState(self.connect, correction_source_policy='require_intake_text_layer', correction_source_store=store)
            fixtures_only = ScopedState(self.connect, correction_source_policy='require_intake_fixture', correction_source_store=store)
            document, _ = intake.submit(analyst, 'new-synthetic.pdf', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            item = intake.promote(analyst, document)['id']
            other = state.add_sample(analyst, 'english_quote.txt')['id']
            state.review_start(analyst, item)
            correction = {'id':item, 'field':'total', 'value':'315', 'reason':'Synthetic format correction',
                          'source':'new-synthetic.pdf', 'line':7, 'quote':'Grand Total: 315.00', 'expected_revision':1}
            with self.assertRaises(ValueError):
                fixtures_only.review_correction(analyst, correction)
            incorrect = dict(correction, line=6)
            with self.assertRaises(ValueError):
                state.review_correction(analyst, incorrect)
            location = {'kind':'pdf_text_layer', 'global_line':7, 'page':2, 'page_line':6}
            self.assertEqual(state.review_correction(analyst, correction)['source_checks'], [location])
            decision = {'id':item, 'decision':'approve', 'reason':'Synthetic review', 'expected_revision':2}
            self.assertEqual(state.review_decision(reviewer, decision)['source_checks'], [location])
            compared = state.compare(analyst, [item, other])['quotations'][0]
            self.assertEqual(compared['total'], '315')
            self.assertEqual(compared['comparison']['source_checks'], [location])
            (Path(folder) / document).write_bytes(b'changed synthetic source')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                state.compare(analyst, [item, other])

    def test_promotion_refuses_changed_source_before_and_after_creation(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            service = ScopedIntake(self.connect, store)
            principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, _ = service.submit(principal, 'english.txt', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            source = Path(folder) / document
            source.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                service.promote(principal, document)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM nawa_quotation_tenants WHERE tenant_id=%s',
                                              (self.tenant,)).fetchone()[0], 0)
            source.write_bytes(content)
            self.assertTrue(service.promote(principal, document)['created'])
            source.write_bytes(b'tampered again')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                service.promote(principal, document)

    def test_http_synthetic_session_to_postgres_result(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))

        def lookup(key, tenant):
            with self.connect() as conn:
                return membership_by_key(conn, identity_key=key, tenant=tenant)

        sessions = SessionStore(lookup)
        tokens = sessions.issue(identity_key, self.tenant)
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            handler = make_protected_handler(ScopedState(self.connect), sessions, DashboardState(),
                                             intake=ScopedIntake(self.connect, store))
            server = HTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
                body = json.dumps({'filename': 'english.txt', 'content_base64': base64.b64encode(content).decode()}).encode()
                base = f'http://127.0.0.1:{server.server_port}'
                headers = {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:8765',
                           'Cookie': 'nawa_session=' + tokens['session'], 'X-NAWA-CSRF': tokens['csrf']}
                with urlopen(Request(base + '/api/intake/submit', data=body, headers=headers), timeout=10) as response:
                    document = json.load(response)['id']
                version_payload = json.dumps({'id': document, 'expected_previous': 0}).encode()
                with urlopen(Request(base + '/api/intake/version', data=version_payload, headers=headers), timeout=10) as response:
                    version = json.load(response)
                self.assertEqual(version['version'], 1)
                with urlopen(Request(base + '/api/intake/versions?family=' + version['family_id'],
                                     headers={'Cookie': headers['Cookie']}), timeout=10) as response:
                    self.assertEqual(json.load(response)['versions'], [{'version': 1, 'id': document}])
                self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
                with urlopen(Request(base + '/api/intake/result?id=' + document, headers={'Cookie': headers['Cookie']}), timeout=10) as response:
                    self.assertEqual(json.load(response)['result']['total'], '1050.00')
                promotion = json.dumps({'id':document}).encode()
                with urlopen(Request(base + '/api/intake/promote', data=promotion, headers=headers), timeout=10) as response:
                    first = json.load(response)
                self.assertTrue(first['created'])
                with urlopen(Request(base + '/api/intake/promote', data=promotion, headers=headers), timeout=10) as response:
                    repeated = json.load(response)
                self.assertFalse(repeated['created'])
                self.assertEqual(first['id'], repeated['id'])
                review_payload = json.dumps({'id':first['id']}).encode()
                with self.assertRaises(HTTPError) as no_review:
                    urlopen(Request(base + '/api/review/show', data=review_payload, headers=headers), timeout=10)
                self.assertEqual(no_review.exception.code, 404)
                self.assertEqual(json.load(no_review.exception)['error'], 'Review is not initialized')
                no_review.exception.close()
                with urlopen(Request(base + '/api/review/start', data=review_payload, headers=headers), timeout=10) as response:
                    self.assertEqual(json.load(response)['status'], 'pending_review')
                arabic = (Path(__file__).resolve().parents[1] / 'samples/arabic_quote.txt').read_bytes()
                batch = {'files': [{'filename': 'duplicate.txt', 'content_base64': base64.b64encode(content).decode()},
                                   {'filename': 'arabic.txt', 'content_base64': base64.b64encode(arabic).decode()},
                                   {'filename': 'invalid.txt', 'content_base64': '%%%'}]}
                with urlopen(Request(base + '/api/intake/batch', data=json.dumps(batch).encode(), headers=headers), timeout=10) as response:
                    batch_results = json.load(response)['files']
                self.assertEqual(batch_results[0], {'id': document, 'created': False})
                self.assertTrue(batch_results[1]['created'])
                self.assertEqual(batch_results[2], {'error': 'Invalid or unsupported file'})
                self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
                self.assertEqual(self.jobs.result(self.tenant, batch_results[1]['id'])['total'], '945.00')
                with self.assertRaises(HTTPError) as denied:
                    urlopen(base + '/api/intake/status?id=' + document, timeout=10)
                self.assertEqual(denied.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_configured_runtime_and_one_shot_worker(self):
        issuer, subject = 'https://synthetic-issuer', uuid4().hex
        identity_key = json.dumps([issuer, subject], separators=(',', ':'))
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, self.tenant, 'analyst'))
        principal = make_principal(self.connect, identity_key, self.tenant, 'analyst')
        with TemporaryDirectory() as folder:
            service = build_synthetic_intake(self.connect, str(Path(folder).resolve()))
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, created = service.submit(principal, 'english.txt', content)
            self.assertTrue(created)
            params = psycopg.conninfo.conninfo_to_dict(DSN)
            env = os.environ.copy()
            env.update({'NAWA_INTAKE':'synthetic', 'NAWA_AUTH':'oidc', 'NAWA_TENANT_ID':self.tenant,
                        'NAWA_INTAKE_DIR':str(Path(folder).resolve()), 'POSTGRES_HOST':params['host'],
                        'POSTGRES_PORT':params['port'], 'POSTGRES_DB':params['dbname'],
                        'POSTGRES_USER':params['user'], 'POSTGRES_PASSWORD':params.get('password', 'synthetic-unused')})
            worker = subprocess.run([sys.executable, '-m', 'nawa_demo.intake_worker_cli'],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            self.assertIn('completed', worker.stdout)
            self.assertEqual(service.result(principal, document)['total'], '1050.00')

    def test_quiesced_database_and_file_backup_restore(self):
        # This uses only the disposable local cluster created by the test harness.
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        pg_bin = Path(r'C:\Program Files\PostgreSQL\18\bin')
        def pg_tool(name):
            return shutil.which(name) or str(pg_bin / (name + '.exe'))
        restore_name = 'nawa_restore_' + uuid4().hex[:12]
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        common = ['-h', params['host'], '-p', params['port'], '-U', params['user']]
        issuer = 'https://synthetic-backup'
        subjects = {'analyst':uuid4().hex, 'reviewer':uuid4().hex}
        with self.connect() as conn:
            for role, subject in subjects.items():
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer,subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             (issuer,subject,self.tenant,role))
        principal = lambda role: make_principal(self.connect, json.dumps([issuer,subjects[role]], separators=(',',':')), self.tenant, role)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = LocalIntakeStore(root / 'source')
            content = (Path(__file__).resolve().parents[1] / 'samples/arabic_quote.txt').read_bytes()
            intake = ScopedIntake(self.connect, store)
            document, _ = intake.submit(principal('analyst'), 'arabic.txt', content)
            self.assertEqual(process_one(self.jobs, store, self.tenant), 'completed')
            quotation = intake.promote(principal('analyst'), document)['id']
            reviews = ScopedState(self.connect)
            reviews.review_start(principal('analyst'), quotation)
            original_review = reviews.review_decision(principal('reviewer'), {
                'id':quotation, 'decision':'approve', 'reason':'Synthetic backup check', 'expected_revision':1})
            self.assertEqual((original_review['status'], original_review['revision']), ('approved', 2))
            digest = check_document('arabic.txt', content).sha256

            # No intake or worker writes occur while the two snapshots are made.
            dump = root / 'synthetic.dump'
            subprocess.run([pg_tool('pg_dump'), *common, '-Fc', '-f', str(dump), params['dbname']],
                           check=True, capture_output=True, creationflags=flags)
            shutil.copytree(store.directory, root / 'backup-files')
            subprocess.run([pg_tool('createdb'), *common, restore_name],
                           check=True, capture_output=True, creationflags=flags)
            subprocess.run([pg_tool('pg_restore'), *common, '-d', restore_name, '--no-owner', str(dump)],
                           check=True, capture_output=True, creationflags=flags)
            restored_params = dict(params, dbname=restore_name)
            restored = PostgresIntakeJobs(lambda: psycopg.connect(**restored_params))
            restored_store = LocalIntakeStore(root / 'backup-files')
            restored_result = restored.result(self.tenant, document)
            self.assertEqual(restored_result['total'], '945.00')
            self.assertEqual(restored_result['extraction_origin'], 'synthetic_text_worker_v1')
            with psycopg.connect(**restored_params) as conn:
                origin = conn.execute('SELECT result_origin FROM nawa_intake_jobs WHERE tenant_id=%s AND document_id=%s',
                                      (self.tenant, document)).fetchone()[0]
                self.assertEqual(origin, 'synthetic_text_worker_v1')
            self.assertEqual(restored_store.read_verified(document, digest), content)
            restored_review = ScopedState(lambda: psycopg.connect(**restored_params)).review_show(
                principal('reviewer'), quotation)
            self.assertEqual(restored_review, original_review)
            # Mutate only the restored test copy: removing the JSON marker must
            # not bypass the independently stored origin check.
            altered = dict(restored_result)
            del altered['extraction_origin']
            with psycopg.connect(**restored_params) as conn:
                conn.execute('UPDATE nawa_intake_jobs SET result=%s::jsonb WHERE tenant_id=%s AND document_id=%s',
                             (json.dumps(altered), self.tenant, document))
            restored_intake = ScopedIntake(lambda: psycopg.connect(**restored_params), restored_store)
            with self.assertRaisesRegex(ValueError, 'origin'):
                restored_intake.promote(principal('analyst'), document)
            self.assertEqual(ScopedState(lambda: psycopg.connect(**restored_params)).review_show(
                principal('reviewer'), quotation), original_review)

    def test_explicit_intake_migration_is_idempotent_and_detects_changed_history(self):
        name = 'nawa_phase13_qa_' + uuid4().hex[:12]
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
        params['dbname'] = name
        with psycopg.connect(**params) as conn:
            migrate(conn)
            apply_intake_migrations(conn)
            apply_intake_migrations(conn)
            verify_intake_migrations(conn)
            self.assertEqual(conn.execute('SELECT count(*) FROM nawa_intake_schema_migrations').fetchone()[0], 7)
            self.assertIsNotNone(conn.execute("SELECT to_regclass('nawa_intake_versions')").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT to_regclass('nawa_synthetic_withdrawals')").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT to_regclass('nawa_synthetic_document_grants')").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT to_regclass('nawa_synthetic_grant_generation_seq')").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT to_regclass('nawa_synthetic_review_requests')").fetchone()[0])
            conn.execute("UPDATE nawa_intake_schema_migrations SET checksum='altered'")
            conn.commit()
            with self.assertRaisesRegex(ValueError, 'history mismatch'):
                verify_intake_migrations(conn)
            with self.assertRaisesRegex(RuntimeError, 'history mismatch'):
                apply_intake_migrations(conn)

    def test_old_synthetic_schema_upgrades_without_losing_records(self):
        name = 'nawa_phase13_qa_' + uuid4().hex[:12]
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
        params['dbname'] = name
        connect = lambda: psycopg.connect(**params)
        issuer, tenant = 'https://synthetic-upgrade', 'tenant-upgrade'
        subjects = {'analyst':uuid4().hex, 'reviewer':uuid4().hex}
        principal = lambda role: make_principal(connect, json.dumps([issuer,subjects[role]], separators=(',',':')), tenant, role)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            old_migrations = root / 'old-migrations'
            old_migrations.mkdir()
            original = Path(__file__).resolve().parents[1] / 'nawa_demo/intake_migrations/001_synthetic_intake.sql'
            shutil.copy2(original, old_migrations / original.name)
            with connect() as conn:
                migrate(conn)
                with patch('nawa_demo.intake_migrate.MIGRATIONS', old_migrations):
                    apply_intake_migrations(conn)
                previous_checksum = conn.execute('SELECT checksum FROM nawa_intake_schema_migrations WHERE name=%s',
                                                 (original.name,)).fetchone()[0]
                conn.execute('INSERT INTO nawa_tenants(id) VALUES (%s)', (tenant,))
                for role, subject in subjects.items():
                    conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer,subject))
                    conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                                 (issuer,subject,tenant,role))
            store = LocalIntakeStore(root / 'files')
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
            document, metadata = store.save('queued.txt', content)
            legacy_document, legacy_metadata = store.save('legacy.txt', b'synthetic legacy record')
            with connect() as conn:
                conn.execute('''INSERT INTO nawa_intake_documents(id,tenant_id,filename,sha256,size_bytes)
                                VALUES (%s,%s,%s,%s,%s)''',
                             (document,tenant,metadata.filename,metadata.sha256,metadata.size))
                conn.execute('INSERT INTO nawa_intake_jobs(id,tenant_id,document_id,state) VALUES (%s,%s,%s,%s)',
                             (uuid4(),tenant,document,'received'))
                conn.execute('''INSERT INTO nawa_intake_documents(id,tenant_id,filename,sha256,size_bytes)
                                VALUES (%s,%s,%s,%s,%s)''',
                             (legacy_document,tenant,legacy_metadata.filename,legacy_metadata.sha256,legacy_metadata.size))
                conn.execute('''INSERT INTO nawa_intake_jobs(id,tenant_id,document_id,state,result)
                                VALUES (%s,%s,%s,%s,%s::jsonb)''',
                             (uuid4(),tenant,legacy_document,'completed',json.dumps({'legacy':'unchanged'})))
            reviews = ScopedState(connect)
            quotation = reviews.add_sample(principal('analyst'), 'english_quote.txt')['id']
            reviews.review_start(principal('analyst'), quotation)
            before = reviews.review_decision(principal('reviewer'), {
                'id':quotation,'decision':'approve','reason':'Synthetic upgrade','expected_revision':1})
            with connect() as conn:
                apply_intake_migrations(conn)
                verify_intake_migrations(conn)
                self.assertEqual(conn.execute('SELECT checksum FROM nawa_intake_schema_migrations WHERE name=%s',
                                              (original.name,)).fetchone()[0], previous_checksum)
                legacy = conn.execute('SELECT result,result_origin FROM nawa_intake_jobs WHERE tenant_id=%s AND document_id=%s',
                                      (tenant, legacy_document)).fetchone()
                self.assertEqual(legacy, ({'legacy':'unchanged'}, None))
            jobs = PostgresIntakeJobs(connect)
            self.assertEqual(jobs.state(tenant, document), ('received', 0))
            self.assertIsNone(jobs.claim(tenant))
            service = ScopedIntake(connect, store)
            self.assertEqual(service.requeue(principal('analyst'), document, action='revalidate',
                                             reason='Synthetic upgrade')['state'], 'validated')
            self.assertEqual(process_one(jobs, store, tenant), 'completed')
            self.assertEqual(store.read_verified(document, metadata.sha256), content)
            self.assertEqual(reviews.review_show(principal('reviewer'), quotation), before)

    def test_read_only_migration_inspector_classifies_known_and_unknown_histories(self):
        name = 'nawa_phase13_qa_' + uuid4().hex[:12]
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
        params['dbname'] = name
        with psycopg.connect(**params) as conn:
            self.assertEqual(inspect_history(conn)['history_status'], 'no_recorded_history')
            migrate(conn)
            apply_intake_migrations(conn)
            self.assertEqual(inspect_history(conn)['history_status'], 'current_history')
            conn.commit()
            with psycopg.connect(**params, autocommit=True) as read_only:
                with read_only.transaction():
                    read_only.execute('SET TRANSACTION READ ONLY')
                    self.assertEqual(inspect_history(read_only)['history_status'], 'current_history')
            # Construct an 001-only history regardless of later migration count.
            conn.execute("DELETE FROM nawa_intake_schema_migrations WHERE name <> '001_synthetic_intake.sql'")
            for checksum, status in ((ORIGINAL_001,'original_001_only'),
                                     (VALIDATED_IN_001,'intermediate_validated_in_001'),
                                     (REQUEUE_IN_001,'intermediate_requeue_in_001'),
                                     ('unknown','unknown_or_incomplete_history')):
                conn.execute("UPDATE nawa_intake_schema_migrations SET checksum=%s WHERE name='001_synthetic_intake.sql'", (checksum,))
                before = conn.execute('SELECT name,checksum FROM nawa_intake_schema_migrations ORDER BY name').fetchall()
                report = inspect_history(conn)
                after = conn.execute('SELECT name,checksum FROM nawa_intake_schema_migrations ORDER BY name').fetchall()
                self.assertEqual(report['history_status'], status)
                self.assertEqual(before, after)
                self.assertIn('does not verify', report['note'])

    def test_qa_snapshot_checks_result_and_source_integrity(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO nawa_tenants(id) VALUES ('tenant-a') ON CONFLICT DO NOTHING")
            for subject, role in (('snapshot-analyst', 'analyst'), ('snapshot-reviewer', 'reviewer')):
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', ('https://snapshot',subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             ('https://snapshot',subject,'tenant-a',role))
        with TemporaryDirectory() as folder:
            store = LocalIntakeStore(folder)
            content = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes() + b'\n# qa-test\n'
            analyst = make_principal(self.connect, json.dumps(['https://snapshot','snapshot-analyst'], separators=(',',':')), 'tenant-a', 'analyst')
            reviewer = make_principal(self.connect, json.dumps(['https://snapshot','snapshot-reviewer'], separators=(',',':')), 'tenant-a', 'reviewer')
            intake = ScopedIntake(self.connect, store)
            doc, _ = intake.submit(analyst, 'synthetic.txt', content)
            self.assertEqual(process_one(self.jobs, store, 'tenant-a'), 'completed')
            quotation = intake.promote(analyst, doc)['id']
            reviews = ScopedState(self.connect)
            reviews.review_start(analyst, quotation)
            reviews.review_decision(reviewer, {'id':quotation,'decision':'approve',
                                               'reason':'Synthetic test','expected_revision':1})
            result = snapshot(self.connect, folder)
            self.assertEqual((result['document_id'], result['total']), (doc, '1050.00'))
            self.assertEqual((result['review_status'], result['review_revision']), ('approved', 2))
            (Path(folder) / doc).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                snapshot(self.connect, folder)

