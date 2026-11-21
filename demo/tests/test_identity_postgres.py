"""Isolated-schema Phase 12 membership tests, opt in with NAWA_TEST_POSTGRES=1."""
import os
import unittest
from uuid import uuid4


@unittest.skipUnless(os.environ.get('NAWA_TEST_POSTGRES') == '1', 'PostgreSQL integration not configured')
class IdentityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.psycopg, cls.sql = psycopg, sql
        cls.params = dict(host=os.environ['POSTGRES_HOST'], dbname=os.environ['POSTGRES_DB'],
                          user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'],
                          connect_timeout=5)

    def setUp(self):
        self.schema = 'nawa_identity_test_' + uuid4().hex
        with self.psycopg.connect(**self.params, autocommit=True) as conn:
            conn.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))

    def tearDown(self):
        with self.psycopg.connect(**self.params, autocommit=True) as conn:
            conn.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def connect(self):
        return self.psycopg.connect(**self.params, options='-c search_path=' + self.schema)

    def test_database_mode_blocks_unsafe_switch_and_legacy_assignment(self):
        from nawa_demo.postgres_state import PostgresState
        from nawa_demo.storage_mode import claim_database_mode
        state = PostgresState(self.connect)
        state.add_sample('english_quote.txt')  # Existing unassigned Phase 11 data.
        with self.assertRaisesRegex(RuntimeError, 'Legacy quotations'):
            claim_database_mode(self.connect, 'oidc')
        claim_database_mode(self.connect, 'demo')
        claim_database_mode(self.connect, 'demo')
        with self.assertRaisesRegex(RuntimeError, 'different access mode'):
            claim_database_mode(self.connect, 'oidc')

    def test_authenticated_database_cannot_start_in_demo_mode(self):
        from nawa_demo.postgres_state import PostgresState
        from nawa_demo.storage_mode import claim_database_mode
        PostgresState(self.connect)
        claim_database_mode(self.connect, 'oidc')
        claim_database_mode(self.connect, 'oidc')
        with self.assertRaisesRegex(RuntimeError, 'different access mode'):
            claim_database_mode(self.connect, 'demo')

    def test_membership_is_server_provisioned_and_revocable(self):
        from nawa_demo.migrate import migrate
        from nawa_demo.identity_repository import membership, membership_by_key
        from nawa_demo.authorization import authorized
        with self.connect() as conn:
            migrate(conn)
            migrate(conn)
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s), (%s)', ('tenant-a', 'tenant-b'))
            conn.execute('INSERT INTO nawa_identities (issuer, subject) VALUES (%s, %s)',
                         ('https://issuer.example', 'person-1'))
            conn.execute('INSERT INTO nawa_memberships (issuer, subject, tenant_id, role) VALUES (%s,%s,%s,%s)',
                         ('https://issuer.example', 'person-1', 'tenant-a', 'analyst'))
        with self.connect() as conn:
            principal = membership(conn, issuer='https://issuer.example', subject='person-1', tenant='tenant-a')
            self.assertEqual(membership_by_key(conn, identity_key=principal.subject, tenant='tenant-a'), principal)
            self.assertIsNone(membership_by_key(conn, identity_key='not-json', tenant='tenant-a'))
            self.assertTrue(authorized(principal, 'correct', tenant='tenant-a'))
            self.assertFalse(authorized(principal, 'decide', tenant='tenant-a'))
            self.assertIsNone(membership(conn, issuer='https://issuer.example', subject='person-1', tenant='tenant-b'))
            self.assertIsNone(membership(conn, issuer='https://wrong.example', subject='person-1', tenant='tenant-a'))
            self.assertIsNone(membership(conn, issuer='https://issuer.example', subject="x' OR 1=1 --", tenant='tenant-a'))
            conn.execute('UPDATE nawa_memberships SET active = false WHERE subject = %s', ('person-1',))
        with self.connect() as conn:
            self.assertIsNone(membership(conn, issuer='https://issuer.example', subject='person-1', tenant='tenant-a'))

    def test_membership_generation_rejects_unobserved_changes_and_reinsert(self):
        from nawa_demo.migrate import migrate
        from nawa_demo.identity_repository import membership, membership_by_key
        from nawa_demo.session_draft import SessionError, SessionStore
        issuer, subject, tenant = 'https://issuer.example/realm', 'synthetic-user', 'tenant-a'
        with self.connect() as conn:
            migrate(conn)
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s)', (tenant,))
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, tenant, 'analyst'))
            principal = membership(conn, issuer=issuer, subject=subject, tenant=tenant)
        def lookup(key, tenant_id):
            with self.connect() as conn:
                return membership_by_key(conn, identity_key=key, tenant=tenant_id)
        sessions = SessionStore(lookup)
        old = sessions.issue(principal.subject, tenant)['session']
        with self.connect() as conn:
            conn.execute('UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s', (issuer, subject))
            conn.execute('UPDATE nawa_memberships SET active=true WHERE issuer=%s AND subject=%s', (issuer, subject))
        # No protected request happened between deactivation and reactivation.
        with self.assertRaises(SessionError):
            sessions.authorize_current(old, 'view')
        fresh = sessions.issue(principal.subject, tenant)['session']
        self.assertEqual(sessions.authorize_current(fresh, 'view').role, 'analyst')
        with self.connect() as conn:
            conn.execute('DELETE FROM nawa_memberships WHERE issuer=%s AND subject=%s', (issuer, subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (issuer, subject, tenant, 'analyst'))
        with self.assertRaises(SessionError):
            sessions.authorize_current(fresh, 'view')
        current = sessions.issue(principal.subject, tenant)['session']
        with self.connect() as conn:
            conn.execute("UPDATE nawa_memberships SET role='reviewer' WHERE issuer=%s AND subject=%s", (issuer, subject))
            conn.execute("UPDATE nawa_memberships SET role='analyst' WHERE issuer=%s AND subject=%s", (issuer, subject))
        with self.assertRaises(SessionError):
            sessions.authorize_current(current, 'view')
        identity_session = sessions.issue(principal.subject, tenant)['session']
        with self.connect() as conn:
            conn.execute('UPDATE nawa_identities SET active=false WHERE issuer=%s AND subject=%s', (issuer, subject))
            conn.execute('UPDATE nawa_identities SET active=true WHERE issuer=%s AND subject=%s', (issuer, subject))
        with self.assertRaises(SessionError):
            sessions.authorize_current(identity_session, 'view')
        tenant_session = sessions.issue(principal.subject, tenant)['session']
        with self.connect() as conn:
            conn.execute('UPDATE nawa_tenants SET active=false WHERE id=%s', (tenant,))
            conn.execute('UPDATE nawa_tenants SET active=true WHERE id=%s', (tenant,))
        with self.assertRaises(SessionError):
            sessions.authorize_current(tenant_session, 'view')
        fresh = sessions.issue(principal.subject, tenant)['session']
        self.assertEqual(sessions.authorize_current(fresh, 'view').role, 'analyst')

    def test_generation_migration_preserves_existing_membership(self):
        from pathlib import Path
        import shutil
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from nawa_demo.migrate import migrate
        migrations = Path(__file__).resolve().parents[1] / 'nawa_demo' / 'migrations'
        with TemporaryDirectory() as directory:
            prior = Path(directory)
            for path in sorted(migrations.glob('00[1-4]_*.sql')):
                shutil.copy2(path, prior / path.name)
            with self.connect() as conn:
                with patch('nawa_demo.migrate.MIGRATIONS', prior):
                    migrate(conn)
                conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s)', ('tenant-a',))
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', ('https://issuer.example', 'old-user'))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             ('https://issuer.example', 'old-user', 'tenant-a', 'reviewer'))
                before = dict(conn.execute('SELECT name,checksum FROM nawa_schema_migrations').fetchall())
            with self.connect() as conn:
                migrate(conn)
                migrate(conn)
                row = conn.execute('SELECT role,active,membership_generation FROM nawa_memberships WHERE subject=%s',
                                   ('old-user',)).fetchone()
                after = dict(conn.execute('SELECT name,checksum FROM nawa_schema_migrations').fetchall())
            self.assertEqual(row[:2], ('reviewer', True))
            self.assertGreater(row[2], 0)
            self.assertEqual({name:after[name] for name in before}, before)
            self.assertIn('005_membership_generation.sql', after)
            self.assertIn('006_identity_tenant_generation.sql', after)

    def test_identity_tenant_generation_upgrade_preserves_existing_generations(self):
        from pathlib import Path
        import shutil
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from nawa_demo.migrate import migrate
        migrations = Path(__file__).resolve().parents[1] / 'nawa_demo' / 'migrations'
        with TemporaryDirectory() as directory:
            prior = Path(directory)
            for path in sorted(migrations.glob('00[1-5]_*.sql')):
                shutil.copy2(path, prior / path.name)
            with self.connect() as conn:
                with patch('nawa_demo.migrate.MIGRATIONS', prior):
                    migrate(conn)
                conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s)', ('tenant-a',))
                conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', ('https://issuer.example', 'old-user'))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             ('https://issuer.example', 'old-user', 'tenant-a', 'reviewer'))
                generation = conn.execute('SELECT membership_generation FROM nawa_memberships WHERE subject=%s', ('old-user',)).fetchone()[0]
                before = dict(conn.execute('SELECT name,checksum FROM nawa_schema_migrations').fetchall())
            with self.connect() as conn:
                migrate(conn)
                migrate(conn)
                row = conn.execute('''SELECT m.role,m.active,m.membership_generation,i.identity_generation,t.tenant_generation
                    FROM nawa_memberships m JOIN nawa_identities i ON i.issuer=m.issuer AND i.subject=m.subject
                    JOIN nawa_tenants t ON t.id=m.tenant_id WHERE m.subject=%s''', ('old-user',)).fetchone()
                after = dict(conn.execute('SELECT name,checksum FROM nawa_schema_migrations').fetchall())
            self.assertEqual(row[:3], ('reviewer', True, generation))
            self.assertGreater(row[3], 0)
            self.assertGreater(row[4], 0)
            self.assertEqual({name:after[name] for name in before}, before)
            self.assertIn('006_identity_tenant_generation.sql', after)

    def test_operator_grant_and_revoke_invalidates_existing_session(self):
        from nawa_demo.migrate import migrate
        from nawa_demo.identity_repository import membership, membership_by_key
        from nawa_demo.membership_admin import grant, revoke
        from nawa_demo.session_draft import SessionError, SessionStore
        issuer, subject, tenant = 'https://issuer.example/realm', 'synthetic-user', 'tenant-a'
        with self.connect() as conn:
            migrate(conn)
            with self.assertRaisesRegex(ValueError, 'provisioned first'):
                grant(conn, issuer=issuer, subject=subject, tenant=tenant, role='analyst', operator='qa-operator', reason='Synthetic test')
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s)', (tenant,))
            conn.execute('INSERT INTO nawa_identities (issuer, subject) VALUES (%s,%s)', (issuer, subject))
            grant(conn, issuer=issuer, subject=subject, tenant=tenant, role='analyst', operator='qa-operator', reason='Synthetic test')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                grant(conn, issuer=issuer, subject=subject, tenant=tenant, role='reviewer', operator='qa-operator', reason='Synthetic test')
        def lookup(identity_key, tenant_id):
            with self.connect() as conn:
                return membership_by_key(conn, identity_key=identity_key, tenant=tenant_id)
        with self.connect() as conn:
            principal = membership(conn, issuer=issuer, subject=subject, tenant=tenant)
        sessions = SessionStore(lookup)
        tokens = sessions.issue(principal.subject, tenant)
        self.assertEqual(sessions.authorize_current(tokens['session'], 'view').role, 'analyst')
        with self.connect() as conn:
            self.assertTrue(revoke(conn, issuer=issuer, subject=subject, tenant=tenant, operator='qa-operator', reason='Synthetic test'))
            self.assertFalse(revoke(conn, issuer=issuer, subject=subject, tenant=tenant, operator='qa-operator', reason='Synthetic test'))
        with self.assertRaises(SessionError):
            sessions.authorize_current(tokens['session'], 'view')
        with self.connect() as conn:
            self.assertIsNone(membership(conn, issuer=issuer, subject=subject, tenant=tenant))
            events = conn.execute('SELECT action, operator_name FROM nawa_membership_events ORDER BY id').fetchall()
            self.assertEqual(events, [('grant', 'qa-operator'), ('revoke', 'qa-operator')])
            with self.assertRaisesRegex(ValueError, 'already exists'):
                grant(conn, issuer=issuer, subject=subject, tenant=tenant, role='reviewer', operator='qa-operator', reason='Synthetic test')

    def test_audit_failure_rolls_back_membership_revocation(self):
        from nawa_demo.migrate import migrate
        from nawa_demo.membership_admin import grant, revoke
        issuer, subject, tenant = 'https://issuer.example/realm', 'synthetic-user', 'tenant-a'
        with self.connect() as conn:
            migrate(conn)
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s)', (tenant,))
            conn.execute('INSERT INTO nawa_identities (issuer, subject) VALUES (%s,%s)', (issuer, subject))
            grant(conn, issuer=issuer, subject=subject, tenant=tenant, role='analyst', operator='qa', reason='test')
            conn.execute("ALTER TABLE nawa_membership_events ADD CONSTRAINT qa_reject_revoke CHECK (action <> 'revoke')")
        with self.connect() as conn:
            with self.assertRaises(self.psycopg.errors.CheckViolation):
                revoke(conn, issuer=issuer, subject=subject, tenant=tenant, operator='qa', reason='test')
        with self.connect() as conn:
            row = conn.execute('SELECT active FROM nawa_memberships WHERE subject=%s', (subject,)).fetchone()
            self.assertEqual(row, (True,))
            self.assertEqual(conn.execute('SELECT count(*) FROM nawa_membership_events').fetchone()[0], 1)

    def test_tenant_scoped_operations_and_server_actor(self):
        from nawa_demo.migrate import migrate
        from nawa_demo.scoped_state import ScopedState, AccessDenied
        from nawa_demo.identity_repository import membership
        with self.connect() as conn:
            migrate(conn)
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s), (%s)', ('red', 'blue'))
            for subject, tenant, role in [('analyst', 'red', 'analyst'), ('reviewer', 'red', 'reviewer'),
                                          ('outsider', 'blue', 'reviewer')]:
                conn.execute('INSERT INTO nawa_identities (issuer, subject) VALUES (%s, %s)',
                             ('https://id.example', subject))
                conn.execute('INSERT INTO nawa_memberships (issuer, subject, tenant_id, role) VALUES (%s,%s,%s,%s)',
                             ('https://id.example', subject, tenant, role))
            analyst = membership(conn, issuer='https://id.example', subject='analyst', tenant='red')
            reviewer = membership(conn, issuer='https://id.example', subject='reviewer', tenant='red')
            outsider = membership(conn, issuer='https://id.example', subject='outsider', tenant='blue')
        state = ScopedState(self.connect)
        first = state.add_sample(analyst, 'english_quote.txt')
        second = state.add_sample(analyst, 'arabic_quote.txt')
        self.assertEqual(len(state.list_items(analyst)), 2)
        self.assertEqual(state.list_items(outsider), [])
        with self.assertRaisesRegex(ValueError, 'Quotation not found'):
            state.get_quote(outsider, first['id'])
        with self.assertRaisesRegex(ValueError, 'Quotation not found'):
            state.compare(outsider, [first['id'], second['id']])
        state.review_start(analyst, first['id'])
        payload = {'id': first['id'], 'actor': 'forged-actor', 'decision': 'approve',
                   'reason': 'Synthetic review', 'expected_revision': 1}
        with self.assertRaises(AccessDenied):
            state.review_decision(analyst, payload)
        with self.assertRaisesRegex(ValueError, 'Quotation not found'):
            state.review_decision(outsider, payload)
        result = state.review_decision(reviewer, payload)
        import hashlib
        self.assertEqual(result['history'][-1]['actor'], 'oidc:' + hashlib.sha256(reviewer.subject.encode()).hexdigest())
        with self.connect() as conn:
            conn.execute('UPDATE nawa_memberships SET active = false WHERE subject = %s', ('reviewer',))
        with self.assertRaises(AccessDenied):
            state.list_items(reviewer)

    def test_comparison_uses_only_approved_correction_and_preserves_source(self):
        import base64
        from pathlib import Path
        from nawa_demo.migrate import migrate
        from nawa_demo.scoped_state import ScopedState
        from nawa_demo.identity_repository import membership
        with self.connect() as conn:
            migrate(conn)
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s), (%s)', ('red', 'blue'))
            for subject, tenant, role in [('analyst', 'red', 'analyst'), ('reviewer', 'red', 'reviewer'),
                                          ('outsider', 'blue', 'reviewer')]:
                conn.execute('INSERT INTO nawa_identities (issuer, subject) VALUES (%s,%s)', ('https://id.example', subject))
                conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                             ('https://id.example', subject, tenant, role))
            analyst = membership(conn, issuer='https://id.example', subject='analyst', tenant='red')
            reviewer = membership(conn, issuer='https://id.example', subject='reviewer', tenant='red')
            outsider = membership(conn, issuer='https://id.example', subject='outsider', tenant='blue')
        state = ScopedState(self.connect)
        data = (Path(__file__).resolve().parents[1] / 'samples/english_quote.txt').read_bytes()
        self.assertIn(b'Grand Total: 1050.00', data)
        data = data.replace(b'Grand Total: 1050.00', b'Grand Total: 1051.00')
        first = state.add_upload(analyst, 'synthetic-correction.txt', base64.b64encode(data).decode('ascii'))
        second = state.add_sample(analyst, 'arabic_quote.txt')
        ids = [first['id'], second['id']]
        before = state.compare(analyst, ids)['quotations'][0]
        self.assertEqual(before['total'], '1051.00')
        self.assertFalse(before['checks']['total_matches_components'])
        state.review_start(analyst, first['id'])
        state.review_correction(analyst, {'id':first['id'], 'field':'total', 'value':'1050.00',
            'reason':'Synthetic arithmetic correction', 'source':'synthetic-correction.txt',
            'line':3, 'quote':'Subtotal: 1000.00', 'expected_revision':1})
        pending = state.compare(analyst, ids)['quotations'][0]
        self.assertFalse(pending['comparison']['eligible'])
        self.assertEqual(pending['total'], '1051.00')
        state.review_decision(reviewer, {'id':first['id'], 'decision':'approve',
                                         'reason':'Synthetic reviewer check', 'expected_revision':2})
        approved = state.compare(analyst, ids)['quotations'][0]
        self.assertEqual(approved['total'], '1050.00')
        self.assertTrue(approved['checks']['total_matches_components'])
        self.assertEqual(approved['comparison'], {'basis':'approved_review', 'eligible':True, 'review_revision':3})
        self.assertEqual(approved['evidence']['total']['source_text'], 'Grand Total: 1051.00')
        self.assertEqual(state.get_quote(analyst, first['id'])['total'], '1051.00')
        with self.assertRaisesRegex(ValueError, 'Quotation not found'):
            state.compare(outsider, ids)
        with self.connect() as conn:
            conn.execute('UPDATE nawa_reviews SET revision=99 WHERE quotation_id=%s', (first['id'],))
        with self.assertRaisesRegex(ValueError, 'source quotation'):
            state.compare(analyst, ids)


if __name__ == '__main__':
    unittest.main()

