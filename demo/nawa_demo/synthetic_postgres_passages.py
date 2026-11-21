"""Disposable-DB retrieval fixture, not a production authorization service."""
import hashlib
from uuid import UUID
from .authorization import Principal, authorized
from .identity_repository import membership_by_key
from .retrieval import Passage
from .review import inspect
from .synthetic_live_retrieval import PassageSnapshot


class SyntheticPostgresPassages:
    """Load approved, latest-version retained TXT sources for synthetic tests.

    Its document grants and one-way withdrawal are synthetic experiments;
    this class does not implement exports or an authenticated API.
    """
    def __init__(self, connect, store):
        self.connect = connect
        self.store = store

    def principal(self, subject, tenant):
        with self.connect() as conn:
            return membership_by_key(conn, identity_key=subject, tenant=tenant)

    def passages(self, principal):
        if not authorized(principal, 'view', tenant=principal.tenant):
            raise ValueError('Evidence unavailable')
        with self.connect() as conn:
            current = membership_by_key(conn, identity_key=principal.subject, tenant=principal.tenant, lock=True)
            if current != principal:
                raise ValueError('Evidence unavailable')
            rows = conn.execute('''SELECT d.id,d.filename,d.sha256,v.version,j.result,r.revision,r.ledger,g.grant_generation
                FROM nawa_intake_versions v
                JOIN nawa_intake_documents d ON d.tenant_id=v.tenant_id AND d.id=v.document_id
                JOIN nawa_intake_jobs j ON j.tenant_id=d.tenant_id AND j.document_id=d.id
                JOIN nawa_intake_promotions p ON p.tenant_id=d.tenant_id AND p.document_id=d.id
                JOIN nawa_reviews r ON r.quotation_id=p.quotation_id
                JOIN nawa_synthetic_document_grants g ON g.tenant_id=d.tenant_id AND g.document_id=d.id
                WHERE v.tenant_id=%s AND j.state='completed'
                  AND g.subject_hash=%s AND g.active AND g.membership_generation=%s
                  AND g.identity_generation=%s AND g.tenant_generation=%s
                  AND NOT EXISTS (SELECT 1 FROM nawa_synthetic_withdrawals w
                    WHERE w.tenant_id=d.tenant_id AND w.document_id=d.id)
                  AND NOT EXISTS (SELECT 1 FROM nawa_intake_versions n
                    WHERE n.tenant_id=v.tenant_id AND n.family_id=v.family_id AND n.version>v.version)
                ORDER BY d.id LIMIT 21''', (principal.tenant, hashlib.sha256(principal.subject.encode('utf-8')).hexdigest(),
                    principal.membership_generation, principal.identity_generation, principal.tenant_generation)).fetchall()
            if len(rows) > 20:
                raise ValueError('Evidence unavailable')
            result, generations = [], []
            for document, filename, digest, version, extracted, revision, ledger, grant_generation in rows:
                review = inspect(ledger)
                if (review['revision'] != revision or review['document'] != filename
                        or not isinstance(extracted, dict) or review['source_evidence'] != extracted.get('evidence')):
                    raise ValueError('Evidence unavailable')
                if review['status'] != 'approved' or not filename.lower().endswith('.txt'):
                    continue
                content = self.store.read_verified(document.hex, digest.strip())
                if len(content) > 65536:
                    raise ValueError('Evidence unavailable')
                text = content.decode('utf-8')
                result.append(Passage.from_dict({'passage_id':document.hex + ':v' + str(version),
                    'document_id':document.hex, 'version':str(version), 'tenant_id':principal.tenant,
                    'page':1, 'text':text, 'allowed_principals':[principal.subject],
                    'source':filename, 'method':'synthetic'}))
                generations.append((result[-1].passage_id, grant_generation))
            return PassageSnapshot(tuple(result), tuple(generations))

    def set_access(self, principal, document_id, target_key, enabled, reason):
        """Change a synthetic view grant using current server-side members.

        Reviewer administration is a test policy, not an approved enterprise
        delegation model. The caller must not pass untrusted identity context.
        """
        if (not isinstance(principal, Principal) or principal.role != 'reviewer'
                or not authorized(principal, 'view', tenant=principal.tenant)
                or not isinstance(document_id, str) or not isinstance(target_key, str)
                or type(enabled) is not bool or not isinstance(reason, str)
                or not reason.strip() or len(reason.strip()) > 500):
            raise ValueError('Document unavailable')
        try:
            document = UUID(document_id)
        except ValueError:
            raise ValueError('Document unavailable') from None
        with self.connect() as conn:
            current = membership_by_key(conn, identity_key=principal.subject, tenant=principal.tenant, lock=True)
            target = membership_by_key(conn, identity_key=target_key, tenant=principal.tenant, lock=True)
            if (current != principal or target is None or target.subject != target_key
                    or not authorized(target, 'view', tenant=principal.tenant)):
                raise ValueError('Document unavailable')
            if conn.execute('SELECT 1 FROM nawa_intake_documents WHERE tenant_id=%s AND id=%s FOR UPDATE',
                            (principal.tenant, document)).fetchone() is None:
                raise ValueError('Document unavailable')
            subject_hash = hashlib.sha256(target_key.encode('utf-8')).hexdigest()
            generations = (target.membership_generation, target.identity_generation, target.tenant_generation)
            row = conn.execute('''INSERT INTO nawa_synthetic_document_grants
                (tenant_id,document_id,subject_hash,membership_generation,identity_generation,tenant_generation,active)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,document_id,subject_hash) DO UPDATE SET
                  membership_generation=EXCLUDED.membership_generation, identity_generation=EXCLUDED.identity_generation,
                  tenant_generation=EXCLUDED.tenant_generation, active=EXCLUDED.active
                WHERE (nawa_synthetic_document_grants.membership_generation,
                       nawa_synthetic_document_grants.identity_generation,
                       nawa_synthetic_document_grants.tenant_generation,nawa_synthetic_document_grants.active)
                      IS DISTINCT FROM (EXCLUDED.membership_generation, EXCLUDED.identity_generation,
                                        EXCLUDED.tenant_generation, EXCLUDED.active)
                RETURNING document_id''', (principal.tenant, document, subject_hash, *generations, enabled)).fetchone()
            if row is None:
                return False
            actor_hash = hashlib.sha256(principal.subject.encode('utf-8')).hexdigest()
            conn.execute('''INSERT INTO nawa_synthetic_document_grant_events
                (tenant_id,document_id,subject_hash,actor_hash,action,reason) VALUES (%s,%s,%s,%s,%s,%s)''',
                (principal.tenant, document, subject_hash, actor_hash, 'grant' if enabled else 'revoke', reason.strip()))
            return True

    def withdraw(self, principal, document_id, reason):
        """Record a one-way synthetic withdrawal by a current reviewer."""
        if (not isinstance(principal, Principal) or principal.role != 'reviewer'
                or not authorized(principal, 'view', tenant=principal.tenant)
                or not isinstance(document_id, str) or not isinstance(reason, str)
                or not reason.strip() or len(reason.strip()) > 500):
            raise ValueError('Document unavailable')
        try:
            document = UUID(document_id)
        except ValueError:
            raise ValueError('Document unavailable') from None
        with self.connect() as conn:
            current = membership_by_key(conn, identity_key=principal.subject, tenant=principal.tenant, lock=True)
            if current != principal:
                raise ValueError('Document unavailable')
            row = conn.execute('''SELECT 1 FROM nawa_intake_documents
                WHERE tenant_id=%s AND id=%s FOR UPDATE''', (principal.tenant, document)).fetchone()
            if row is None:
                raise ValueError('Document unavailable')
            actor = hashlib.sha256(principal.subject.encode('utf-8')).hexdigest()
            inserted = conn.execute('''INSERT INTO nawa_synthetic_withdrawals
                (tenant_id,document_id,actor_hash,reason) VALUES (%s,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING document_id''',
                (principal.tenant, document, actor, reason.strip())).fetchone()
            return inserted is not None
