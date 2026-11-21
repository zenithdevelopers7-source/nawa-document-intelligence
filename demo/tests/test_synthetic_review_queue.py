"""Synthetic review request tests; no real users or persistence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest
from uuid import uuid4

from nawa_demo.authorization import Principal
from nawa_demo.retrieval import Passage
from nawa_demo.synthetic_live_retrieval import SyntheticLiveRetrieval
from nawa_demo.synthetic_review_queue import ReviewRequestUnavailable, SyntheticReviewQueue


class SyntheticReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.analyst = Principal('analyst', 'tenant-a', 'analyst')
        self.reviewer = Principal('reviewer', 'tenant-a', 'reviewer')
        self.viewer = Principal('viewer', 'tenant-a', 'viewer')
        self.other = Principal('other', 'tenant-b', 'analyst')
        self.members = {(p.subject,p.tenant):p for p in (self.analyst,self.reviewer,self.viewer,self.other)}
        self.doc = Passage.from_dict({'passage_id':'doc-v1', 'document_id':'doc', 'version':'1',
            'tenant_id':'tenant-a', 'page':1, 'text':'Synthetic source text',
            'allowed_principals':['analyst','reviewer','viewer'], 'source':'synthetic.txt', 'method':'synthetic'})
        self.docs = [self.doc]
        self.retrieval = SyntheticLiveRetrieval(lambda s,t:self.members.get((s,t)), lambda p:list(self.docs))
        self.queue = SyntheticReviewQueue(self.retrieval)
        self.key = uuid4().hex

    def create(self):
        return self.queue.create(self.analyst, self.key, 'Check synthetic source', 'doc-v1')

    def test_create_is_pending_idempotent_and_reviewable(self):
        result = self.create()
        self.assertEqual((result['status'],result['answer'],result['created']), ('pending_review',None,True))
        self.assertNotIn('text', result['evidence'])
        self.assertEqual(self.create()['id'], result['id'])
        self.assertFalse(self.create()['created'])
        self.assertEqual(self.queue.get(self.analyst,result['id'])['question'], 'Check synthetic source')
        self.assertEqual(self.queue.get(self.reviewer,result['id'])['id'], result['id'])
        with self.assertRaises(ReviewRequestUnavailable):
            SyntheticReviewQueue(self.retrieval).get(self.analyst, result['id'])
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.get(self.viewer, result['id'])
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.get(self.other, result['id'])

    def test_concurrent_identical_requests_create_one_task(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _:self.create(), range(12)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        self.assertEqual(sum(r['created'] for r in results), 1)

    def test_conflicting_retry_and_role_denied(self):
        task = self.create()
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.create(self.analyst, self.key, 'A different question', 'doc-v1')
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.create(self.viewer, uuid4().hex, 'Not an analyst', 'doc-v1')
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.create(self.other, uuid4().hex, 'Other tenant', 'doc-v1')
        self.assertEqual(self.queue.get(self.analyst,task['id'])['status'], 'pending_review')

    def test_revoked_access_and_changed_source_block_read_and_retry(self):
        task = self.create()
        self.docs[0] = replace(self.doc, allowed_principals=('reviewer','viewer'))
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.get(self.analyst, task['id'])
        with self.assertRaises(ReviewRequestUnavailable):
            self.create()
        self.docs[0] = replace(self.doc, text='Changed synthetic source')
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.get(self.reviewer, task['id'])
        with self.assertRaises(ReviewRequestUnavailable):
            self.create()

    def test_reviewer_needs_current_source_access_and_membership(self):
        task = self.create()
        self.docs[0] = replace(self.doc, allowed_principals=('analyst','viewer'))
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.get(self.reviewer, task['id'])
        self.docs[0] = self.doc
        self.members[(self.reviewer.subject,self.reviewer.tenant)] = replace(self.reviewer, active=False)
        with self.assertRaises(ReviewRequestUnavailable):
            self.queue.get(self.reviewer, task['id'])

    def test_bad_request_and_capacity_fail_closed(self):
        for key, question, pid in (('bad','valid','doc-v1'), (uuid4().hex,' ','doc-v1'),
                                   (uuid4().hex,'x'*301,'doc-v1'), (uuid4().hex,'valid','')):
            with self.subTest(key=key, question=question), self.assertRaises(ReviewRequestUnavailable):
                self.queue.create(self.analyst,key,question,pid)
        queue = SyntheticReviewQueue(self.retrieval, limit=1)
        queue.create(self.analyst, uuid4().hex, 'First', 'doc-v1')
        with self.assertRaises(ReviewRequestUnavailable):
            queue.create(self.analyst, uuid4().hex, 'Second', 'doc-v1')


if __name__ == '__main__':
    unittest.main()
