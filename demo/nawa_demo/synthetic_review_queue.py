"""Process-local synthetic review requests; no decisions or production queue."""
import hashlib
import json
import re
import threading
from uuid import uuid4

from .authorization import Principal, authorized
from .synthetic_live_retrieval import EvidenceUnavailable, SyntheticLiveRetrieval


class ReviewRequestUnavailable(ValueError):
    pass


class SyntheticReviewQueue:
    """Explicitly experimental, bounded, and lost on process restart."""
    def __init__(self, retrieval, *, limit=128):
        if not isinstance(retrieval, SyntheticLiveRetrieval) or type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('Invalid synthetic review queue')
        self._retrieval, self._limit = retrieval, limit
        self._lock = threading.Lock()
        self._tasks, self._requests = {}, {}

    def uses_retrieval(self, retrieval):
        return self._retrieval is retrieval

    @staticmethod
    def _require(principal, action):
        if (not isinstance(principal, Principal) or not authorized(principal, action, tenant=principal.tenant)):
            raise ReviewRequestUnavailable('Review request unavailable')

    def _current_passage(self, principal, passage_id):
        try:
            return self._retrieval.resolve(principal.subject, principal.tenant, passage_id,
                                           expected_principal=principal)
        except EvidenceUnavailable:
            raise ReviewRequestUnavailable('Review request unavailable') from None

    @staticmethod
    def _evidence(passage):
        return {'passage_id':passage.passage_id, 'document_id':passage.document_id,
                'version':passage.version, 'page':passage.page, 'source':passage.source,
                'passage_text_sha256':hashlib.sha256(passage.text.encode('utf-8')).hexdigest()}

    def create(self, principal, request_key, question, passage_id):
        self._require(principal, 'review_start')
        if (not isinstance(request_key, str) or not re.fullmatch('[0-9a-f]{32}', request_key)
                or not isinstance(question, str) or not question.strip() or len(question) > 300
                or not isinstance(passage_id, str) or not passage_id.strip() or len(passage_id) > 200):
            raise ReviewRequestUnavailable('Review request unavailable')
        passage = self._current_passage(principal, passage_id)
        evidence = self._evidence(passage)
        question = question.strip()
        request_hash = hashlib.sha256(json.dumps({'question':question, 'passage_id':passage_id},
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        key = (principal.tenant, principal.subject, request_key)
        with self._lock:
            if self._evidence(self._current_passage(principal, passage_id)) != evidence:
                raise ReviewRequestUnavailable('Review request unavailable')
            existing = self._requests.get(key)
            if existing is not None:
                if existing[0] != request_hash:
                    raise ReviewRequestUnavailable('Review request unavailable')
                task = self._tasks[existing[1]]
                if task['evidence'] != evidence:
                    raise ReviewRequestUnavailable('Review request unavailable')
                return self._public(task, created=False)
            if len(self._tasks) >= self._limit:
                raise ReviewRequestUnavailable('Review request unavailable')
            task_id = uuid4().hex
            task = {'id':task_id, 'tenant':principal.tenant, 'owner':principal.subject,
                    'question':question, 'status':'pending_review', 'evidence':evidence}
            self._tasks[task_id] = task
            self._requests[key] = (request_hash, task_id)
            return self._public(task, created=True)

    def get(self, principal, task_id):
        self._require(principal, 'view')
        if not isinstance(task_id, str) or not re.fullmatch('[0-9a-f]{32}', task_id):
            raise ReviewRequestUnavailable('Review request unavailable')
        with self._lock:
            task = self._tasks.get(task_id)
            if (task is None or task['tenant'] != principal.tenant
                    or (task['owner'] != principal.subject and principal.role != 'reviewer')):
                raise ReviewRequestUnavailable('Review request unavailable')
            task = dict(task)
        passage = self._current_passage(principal, task['evidence']['passage_id'])
        if self._evidence(passage) != task['evidence']:
            raise ReviewRequestUnavailable('Review request unavailable')
        return self._public(task)

    @staticmethod
    def _public(task, *, created=None):
        result = {'id':task['id'], 'question':task['question'], 'status':task['status'],
                  'evidence':dict(task['evidence']), 'answer':None}
        if created is not None:
            result['created'] = created
        return result
