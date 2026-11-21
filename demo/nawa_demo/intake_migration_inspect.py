"""Read-only migration-history inventory for synthetic Phase 13 QA databases.

Matching records alone do not prove the database schema or data are intact.
This tool never applies a migration, resets data, or approves an upgrade.
"""
import hashlib
import json
import os

from .intake_migrate import _files

# Checksums normalized by the draft migration runner. The intermediate
# variants were published only in the Phase 13 draft, not as production releases.
ORIGINAL_001 = '96a2f7188031b48b63c268337d05d1abbaa778f22ec53466084b07c94481bccc'
VALIDATED_IN_001 = '1ddacc055df855cd8370bab5e47302a5e0c0ebccb7fbe8ab2c092f2451fce3d1'
REQUEUE_IN_001 = '0e98612bd02c5e81677659a037a0e8ab1ed2bc4e158f49171291f4ad78696c46'


def inspect_history(connection):
    """Return classifications of recorded history and table presence only."""
    expected = {name: hashlib.sha256(content).hexdigest() for name, content in _files()}
    with connection.cursor() as cur:
        cur.execute("""SELECT to_regclass('nawa_intake_schema_migrations'),
            to_regclass('nawa_intake_documents'), to_regclass('nawa_intake_jobs'),
            to_regclass('nawa_intake_versions'), to_regclass('nawa_intake_promotions'),
            to_regclass('nawa_intake_admin_events'), to_regclass('nawa_synthetic_withdrawals'),
            to_regclass('nawa_synthetic_document_grants'), to_regclass('nawa_synthetic_document_grant_events'),
            to_regclass('nawa_synthetic_grant_generation_seq'), to_regclass('nawa_synthetic_review_requests')""")
        row = cur.fetchone()
        presence = dict(zip(('history','documents','jobs','versions','promotions','admin_events','withdrawals',
                             'document_grants','document_grant_events','grant_generation_sequence','review_requests'),
                            (value is not None for value in row)))
        if not presence['history']:
            records = {}
            status = 'no_recorded_history'
        else:
            cur.execute('SELECT name,checksum FROM nawa_intake_schema_migrations ORDER BY name')
            records = dict(cur.fetchall())
            if records == expected:
                status = 'current_history'
            elif records == {'001_synthetic_intake.sql':ORIGINAL_001}:
                status = 'original_001_only'
            elif records == {'001_synthetic_intake.sql':VALIDATED_IN_001}:
                status = 'intermediate_validated_in_001'
            elif records == {'001_synthetic_intake.sql':REQUEUE_IN_001}:
                status = 'intermediate_requeue_in_001'
            else:
                status = 'unknown_or_incomplete_history'
    return {'history_status':status, 'recorded_migration_count':len(records), 'tables_present':presence,
            'note':'Recorded history does not verify schema integrity or authorize an upgrade.'}


def main():
    name = os.environ.get('POSTGRES_DB', '')
    if (os.environ.get('NAWA_INTAKE') != 'synthetic' or os.environ.get('NAWA_AUTH') != 'oidc'
            or os.environ.get('NAWA_STORAGE') != 'postgres' or not name.startswith('nawa_phase13_qa_')):
        raise ValueError('Explicit synthetic OIDC test mode and a nawa_phase13_qa_ database are required')
    import psycopg
    with psycopg.connect(host=os.environ['POSTGRES_HOST'], port=os.environ.get('POSTGRES_PORT', '5432'),
                         dbname=name, user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'],
                         connect_timeout=5, autocommit=True) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            result = inspect_history(conn)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
