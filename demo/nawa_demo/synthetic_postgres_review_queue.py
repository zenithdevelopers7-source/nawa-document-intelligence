"""Disposable PostgreSQL review requests; not a production task service."""
import hashlib
import json
import re
from uuid import UUID, uuid4

from .identity_repository import membership_by_key
from .synthetic_review_queue import ReviewRequestUnavailable, SyntheticReviewQueue


class SyntheticPostgresReviewQueue(SyntheticReviewQueue):
    """Persist pending synthetic requests with transaction-scoped retries."""
    def __init__(self, retrieval, connect, *, limit=128):
        super().__init__(retrieval, limit=limit)
        if not callable(connect):
            raise ValueError('A synthetic database connection is required')
        self._connect = connect

    @staticmethod
    def _actor(principal):
        return hashlib.sha256(principal.subject.encode('utf-8')).hexdigest()

    @staticmethod
    def _current_member(conn, principal):
        current = membership_by_key(conn, identity_key=principal.subject, tenant=principal.tenant, lock=True)
        if current != principal:
            raise ReviewRequestUnavailable('Review request unavailable')

    def create(self, principal, request_key, question, passage_id):
        self._require(principal, 'review_start')
        if (not isinstance(request_key, str) or not re.fullmatch('[0-9a-f]{32}', request_key)
                or not isinstance(question, str) or not question.strip() or len(question) > 300
                or not isinstance(passage_id, str) or not passage_id.strip() or len(passage_id) > 200):
            raise ReviewRequestUnavailable('Review request unavailable')
        evidence = self._evidence(self._current_passage(principal, passage_id))
        try:
            document = UUID(evidence['document_id'])
        except (ValueError, TypeError):
            raise ReviewRequestUnavailable('Review request unavailable') from None
        question = question.strip()
        request_hash = hashlib.sha256(json.dumps({'question':question, 'passage_id':passage_id},
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        owner = self._actor(principal)
        with self._connect() as conn:
            self._current_member(conn, principal)
            # A synthetic global lock bounds concurrent insertions and ensures
            # retries find the prior committed request before checking capacity.
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (6892110399,))
            if self._evidence(self._current_passage(principal, passage_id)) != evidence:
                raise ReviewRequestUnavailable('Review request unavailable')
            row = conn.execute('''SELECT id,request_hash,question,evidence,status FROM nawa_synthetic_review_requests
                WHERE tenant_id=%s AND owner_hash=%s AND request_key=%s''',
                (principal.tenant, owner, request_key)).fetchone()
            if row is not None:
                if row[1].strip() != request_hash or row[3] != evidence:
                    raise ReviewRequestUnavailable('Review request unavailable')
                task = {'id':row[0].hex, 'question':row[2], 'evidence':row[3], 'status':row[4]}
                return self._public(task, created=False)
            count = conn.execute('SELECT count(*) FROM nawa_synthetic_review_requests').fetchone()[0]
            if count >= self._limit:
                raise ReviewRequestUnavailable('Review request unavailable')
            task_id = uuid4().hex
            conn.execute('''INSERT INTO nawa_synthetic_review_requests
                (id,tenant_id,owner_hash,request_key,request_hash,question,document_id,evidence,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'pending_review')''',
                (task_id, principal.tenant, owner, request_key, request_hash, question, document,
                 json.dumps(evidence, ensure_ascii=False, allow_nan=False)))
            return self._public({'id':task_id,'question':question,'evidence':evidence,'status':'pending_review'}, created=True)

    def get(self, principal, task_id):
        self._require(principal, 'view')
        if not isinstance(task_id, str) or not re.fullmatch('[0-9a-f]{32}', task_id):
            raise ReviewRequestUnavailable('Review request unavailable')
        with self._connect() as conn:
            self._current_member(conn, principal)
            row = conn.execute('''SELECT owner_hash,question,evidence,status FROM nawa_synthetic_review_requests
                WHERE tenant_id=%s AND id=%s''', (principal.tenant, UUID(task_id))).fetchone()
        if row is None or (row[0].strip() != self._actor(principal) and principal.role != 'reviewer'):
            raise ReviewRequestUnavailable('Review request unavailable')
        evidence = row[2]
        if not isinstance(evidence, dict) or not isinstance(evidence.get('passage_id'), str):
            raise ReviewRequestUnavailable('Review request unavailable')
        if self._evidence(self._current_passage(principal, evidence['passage_id'])) != evidence:
            raise ReviewRequestUnavailable('Review request unavailable')
        return self._public({'id':task_id,'question':row[1],'evidence':evidence,'status':row[3]})
