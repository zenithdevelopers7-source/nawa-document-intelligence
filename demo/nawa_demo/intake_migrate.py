"""Explicit, checksum-tracked synthetic intake test migrations.

Never run automatically on application startup. This is not an approved
production migration or a replacement for a tested backup and rollback plan.
"""
import hashlib
import os
from pathlib import Path

MIGRATIONS = Path(__file__).with_name('intake_migrations')


def _files():
    # Normalize checkout line endings so the same migration has the same
    # checksum on Windows and Linux.
    files = [(path.name, path.read_text(encoding='utf-8').encode('utf-8'))
             for path in sorted(MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql'))]
    if not files:
        raise RuntimeError('No intake migrations found')
    return files


def verify_intake_migrations(connection):
    """Fail closed on missing tables or changed migration records; do not apply."""
    expected = {name: hashlib.sha256(content).hexdigest() for name, content in _files()}
    with connection.cursor() as cur:
        cur.execute("""SELECT to_regclass('nawa_intake_schema_migrations'),
            to_regclass('nawa_intake_documents'), to_regclass('nawa_intake_jobs'),
            to_regclass('nawa_intake_versions'), to_regclass('nawa_intake_promotions'),
            to_regclass('nawa_intake_admin_events'), to_regclass('nawa_synthetic_withdrawals'),
            to_regclass('nawa_synthetic_document_grants'), to_regclass('nawa_synthetic_document_grant_events'),
            to_regclass('nawa_synthetic_grant_generation_seq'), to_regclass('nawa_synthetic_review_requests')""")
        row = cur.fetchone()
        if not row or any(value is None for value in row):
            raise ValueError('Synthetic intake schema is not installed')
        cur.execute('SELECT name,checksum FROM nawa_intake_schema_migrations')
        if dict(cur.fetchall()) != expected:
            raise ValueError('Synthetic intake migration history mismatch')


def apply_intake_migrations(connection):
    files = _files()
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (6892110313,))
            cur.execute('''CREATE TABLE IF NOT EXISTS nawa_intake_schema_migrations (
                name text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())''')
            cur.execute('SELECT name,checksum FROM nawa_intake_schema_migrations')
            applied = dict(cur.fetchall())
            if set(applied) - {name for name, _ in files}:
                raise RuntimeError('Unknown intake migration')
            pending_seen = False
            for name, content in files:
                checksum = hashlib.sha256(content).hexdigest()
                if name in applied:
                    if pending_seen or applied[name] != checksum:
                        raise RuntimeError('Intake migration history mismatch')
                    continue
                pending_seen = True
                cur.execute(content.decode('utf-8'))
                cur.execute('INSERT INTO nawa_intake_schema_migrations (name,checksum) VALUES (%s,%s)',
                            (name, checksum))


def main():
    name = os.environ.get('POSTGRES_DB', '')
    if (os.environ.get('NAWA_INTAKE') != 'synthetic' or os.environ.get('NAWA_AUTH') != 'oidc'
            or os.environ.get('NAWA_STORAGE') != 'postgres' or not name.startswith('nawa_phase13_qa_')):
        raise ValueError('Explicit synthetic OIDC test mode and a nawa_phase13_qa_ database are required')
    import psycopg
    with psycopg.connect(host=os.environ['POSTGRES_HOST'], port=os.environ.get('POSTGRES_PORT', '5432'),
                         dbname=name, user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'],
                         connect_timeout=5) as conn:
        apply_intake_migrations(conn)
    print('Synthetic Phase 13 intake migrations applied or already current.')


if __name__ == '__main__':
    main()
